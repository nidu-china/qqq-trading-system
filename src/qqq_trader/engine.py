"""Live/Paper engine sharing the STRATEGY.md state machine and risk policy."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from .config import NY_TZ, Settings
from .domain import (
    AccountSnapshot,
    Bar,
    BrokerOrder,
    Direction,
    ExitDecision,
    ExitReason,
    MarketState,
    OrderRequest,
    OrderSide,
    Position,
    Quote,
    SystemState,
    TradeSignal,
    TradingMode,
)
from .execution import TERMINAL_STATUSES, OrderExecutor
from .interfaces import Broker, Journal, MarketDataProvider
from .policy import RULES
from .reporting import TradeSummary
from .risk import ContractSelector, RiskEngine
from .strategy import StrategyEngine, strategy_from_settings
from .volatility import VolatilityFilter, VolatilityRegime, VolatilitySnapshot


class TradingEngine:
    def __init__(
        self,
        settings: Settings,
        market: MarketDataProvider,
        broker: Broker,
        journal: Journal,
    ) -> None:
        self.settings = settings
        self.market = market
        self.broker = broker
        self.journal = journal
        self.log = logging.getLogger("qqq_trader.engine")
        self.selector = ContractSelector()
        self.risk = RiskEngine(settings)
        self.strategy: StrategyEngine = strategy_from_settings(settings)
        self.volatility_filter = VolatilityFilter(settings)
        self.executor = OrderExecutor(broker, journal, settings)
        self.state = SystemState.STARTING
        self.position: Position | None = None
        self.opening_equity = Decimal(0)
        self.realized_pnl = Decimal(0)
        self.trades_today = 0
        self.trading_date: date | None = None
        self.last_signal_bar: datetime | None = None
        self.cooldown_until: datetime | None = None
        self.closed_trades: list[TradeSummary] = []
        self.last_error: str | None = None
        self.position_mae = Decimal(0)
        self.position_mfe = Decimal(0)
        self.entry_reference = Decimal(0)
        self.last_volatility: VolatilitySnapshot | None = None
        self.config_version = 0
        self.position_config_version: int | None = None
        self.pending_config_version: int | None = None
        self._pending_settings: tuple[Settings, int] | None = None
        self._lock = asyncio.Lock()

    async def apply_settings(self, settings: Settings, version: int) -> bool:
        async with self._lock:
            if self.position is not None or self.state in {
                SystemState.ENTRY_PENDING,
                SystemState.EXIT_PENDING,
            }:
                self._pending_settings = (settings, version)
                self.pending_config_version = version
                await self.journal.event(
                    "config_staged",
                    f"configuration v{version} will apply after the position is flat",
                )
                return False
            self._activate_settings(settings, version)
            await self.journal.event("config_applied", f"configuration v{version} applied")
            return True

    def _activate_settings(self, settings: Settings, version: int) -> None:
        self.settings = settings
        self.selector = ContractSelector()
        self.risk = RiskEngine(settings)
        self.strategy = strategy_from_settings(settings)
        self.volatility_filter = VolatilityFilter(settings)
        self.executor = OrderExecutor(self.broker, self.journal, settings)
        self.config_version = version
        self.pending_config_version = None
        self._pending_settings = None

    async def start(self) -> None:
        async with self._lock:
            try:
                self.settings.assert_live_authorized()
                await self.market.connect()
                await self.broker.connect()
                self.log.info("connected to market and broker")
                problems = await self.broker.preflight(self.settings.account_id)
                account = await self.broker.account_snapshot()
                if account.equity <= 0:
                    problems.append("account equity is not positive")
                existing_positions = await self.broker.positions()
                existing_orders = await self.broker.open_orders()
                trading_day_check = getattr(self.market, "is_trading_day", None)
                if trading_day_check is not None:
                    today = datetime.now(timezone.utc).astimezone(NY_TZ).date()
                    if not await trading_day_check(today):
                        problems.append("today is not a US trading day")
                    elif self.settings.trading_mode is TradingMode.LIVE:
                        option_check = getattr(self.market, "preflight_options", None)
                        if option_check is not None:
                            problems.extend(
                                await option_check(self.settings.underlying_symbol, today)
                            )
                if problems:
                    await self._halt("; ".join(problems))
                    return
                recovery_problems = await self._recover_broker_state(
                    existing_positions, existing_orders
                )
                if recovery_problems:
                    await self._halt("; ".join(recovery_problems))
                    return
                today = datetime.now(timezone.utc).astimezone(NY_TZ).date()
                self.trading_date = today
                db_pnl, db_trades = await self.journal.today_realized_pnl_and_trades(today)
                self.opening_equity = account.equity - db_pnl
                self.realized_pnl = db_pnl
                self.trades_today = db_trades
                if self.position is not None:
                    self.trades_today = max(self.trades_today, 1)
                symbols = [self.settings.underlying_symbol]
                if self.position is not None:
                    symbols.append(self.position.symbol)
                await self.market.subscribe(symbols)
                self.state = SystemState.OPEN if self.position is not None else SystemState.READY
                self.log.info(
                    "engine ready | state=%s | equity=%.2f | trades_today=%d | pnl=%.2f",
                    self.state.value, account.equity, self.trades_today, db_pnl,
                )
                await self.journal.event(
                    "ready",
                    (
                        "startup checks passed and broker position was adopted"
                        if self.position is not None
                        else "startup checks passed"
                    ),
                )
            except Exception as exc:
                await self._halt(f"startup failed: {exc}")

    async def _recover_broker_state(
        self,
        positions: list[Position],
        orders: list[BrokerOrder],
    ) -> list[str]:
        self.log.info(
            "recovery check | positions=%d | open_orders=%d",
            len(positions), len(orders),
        )
        problems: list[str] = []
        for order in orders:
            signal = await self.journal.trade_signal_by_intent(order.intent_id)
            if (
                signal is None
                or signal.symbol != order.symbol
                or signal.action != order.side
            ):
                problems.append(
                    f"unmatched open broker order {order.order_id} requires reconciliation"
                )
        if problems:
            return problems

        for order in orders:
            await self.broker.cancel_order(order.order_id)
            deadline = asyncio.get_running_loop().time() + RULES.order_timeout_seconds
            final = order
            while asyncio.get_running_loop().time() < deadline:
                final = await self.broker.order(order.order_id)
                await self.journal.broker_order(final)
                if final.status.lower() in TERMINAL_STATUSES:
                    break
                await asyncio.sleep(0.5)
            if final.status.lower() not in TERMINAL_STATUSES:
                problems.append(
                    f"broker order {order.order_id} did not cancel during startup recovery"
                )
        if problems:
            return problems
        if orders:
            positions = await self.broker.positions()

        if len(positions) > 1:
            return ["multiple broker positions cannot be adopted safely"]
        if positions:
            position = positions[0]
            signal = await self.journal.trade_signal_for_position(
                position.symbol, position.quantity
            )
            if signal is None:
                return [f"unmatched broker position {position.symbol} requires reconciliation"]
            if signal.direction is not position.direction or position.quantity > signal.quantity:
                return [f"broker position {position.symbol} does not match its persisted signal"]
            opened_at = signal.indicators.get("opened_at")
            if opened_at:
                parsed = datetime.fromisoformat(str(opened_at))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                position.opened_at = parsed
            else:
                position.opened_at = signal.decision_at
            position.initial_quantity = int(
                signal.indicators.get("initial_quantity") or signal.quantity
            )
            if position.quantity < position.initial_quantity:
                position.first_target_taken = True
                position.stop_price = position.entry_price
            self.position = position
            self.position_config_version = self.config_version
            self.entry_reference = signal.reference_price
            position.strategy_name = str(signal.indicators.get("strategy") or "")
            try:
                position.market_state = MarketState(
                    str(signal.indicators.get("market_state") or "unknown")
                )
            except ValueError:
                position.market_state = MarketState.UNKNOWN
            if signal.indicators.get("spot"):
                position.entry_spot = Decimal(str(signal.indicators["spot"]))
            position.highest_bid = position.entry_price
            if signal.indicators.get("highest_bid"):
                position.highest_bid = Decimal(str(signal.indicators["highest_bid"]))
            if signal.indicators.get("stop_price"):
                position.stop_price = Decimal(str(signal.indicators["stop_price"]))
            if signal.indicators.get("entry_vwap"):
                position.entry_vwap = Decimal(str(signal.indicators["entry_vwap"]))
            position.range_middle_taken = bool(
                signal.indicators.get("range_middle_taken", False)
            )
            position.first_target_taken = bool(
                signal.indicators.get(
                    "first_target_taken", position.first_target_taken
                )
            )
            position.trend_runner = bool(
                signal.indicators.get("trend_runner", False)
            )
            position.macd_reversal_pending = bool(
                signal.indicators.get("macd_reversal_pending", False)
            )
            pending_at = signal.indicators.get("macd_reversal_pending_at")
            if pending_at:
                parsed_pending = datetime.fromisoformat(str(pending_at))
                if parsed_pending.tzinfo is None:
                    parsed_pending = parsed_pending.replace(tzinfo=timezone.utc)
                position.macd_reversal_pending_at = parsed_pending
            else:
                position.macd_reversal_pending_at = None
            position.entry_intent_id = signal.intent_id
            self.trading_date = datetime.now(timezone.utc).astimezone(NY_TZ).date()
            await self.journal.trade_signal_status(signal.intent_id, "executed")
            self.log.info(
                "recovered position | %s %s | qty=%d | entry=%s",
                position.direction.value, position.symbol,
                position.quantity, position.entry_price,
            )

        today_orders = await self.broker.today_orders()
        for broker_order in today_orders:
            await self.journal.broker_order(broker_order)

        recovered = await self.journal.recover_trade_signal_statuses()
        if positions or orders or any(recovered.values()):
            await self.journal.event(
                "startup_recovered",
                "startup broker and signal state reconciled",
                {
                    "position": self.position.symbol if self.position else None,
                    "cancelled_orders": len(orders),
                    "signals_executed": recovered["executed"],
                    "signals_failed": recovered["failed"],
                },
            )
        return []

    async def reconcile(self) -> bool:
        async with self._lock:
            positions = await self.broker.positions()
            orders = await self.broker.open_orders()
            if positions or orders:
                await self._halt("broker state is not flat; manual reconciliation required")
                return False
            self.position = None
            if self._pending_settings is not None:
                pending_settings, pending_version = self._pending_settings
                self._activate_settings(pending_settings, pending_version)
            if self.opening_equity <= 0:
                self.opening_equity = (await self.broker.account_snapshot()).equity
            self.state = SystemState.READY
            self.last_error = None
            await self.journal.event("reconciled", "broker is flat and engine is ready")
            return True

    async def on_completed_bars(
        self,
        bars: list[Bar],
        now: datetime | None = None,
        volatility_bars: list[Bar] | None = None,
        volatility_daily_bars: list[Bar] | None = None,
    ) -> None:
        """Evaluate every completed 1-minute bar without look-ahead."""
        async with self._lock:
            if not bars:
                return
            if self.state in {
                SystemState.STARTING,
                SystemState.ENTRY_PENDING,
                SystemState.EXIT_PENDING,
                SystemState.HALTED,
            }:
                return
            decision_at = now or bars[-1].end
            trading_day = bars[-1].end.astimezone(NY_TZ).date()
            if self.trading_date != trading_day:
                account = await self.broker.account_snapshot()
                self.trading_date = trading_day
                self.opening_equity = account.equity
                self.realized_pnl = Decimal(0)
                self.trades_today = 0
                self.cooldown_until = None

            set_volatility_context = getattr(
                self.strategy, "set_volatility_context", None
            )
            if callable(set_volatility_context):
                set_volatility_context(volatility_bars or [], bars[-1].end)
            signal = self.strategy.evaluate(bars)
            if self.position is not None:
                previous_macd_pending = self.position.macd_reversal_pending
                previous_macd_pending_at = self.position.macd_reversal_pending_at
                bar_decision = self.strategy.bar_exit_decision(self.position)
                if (
                    self.position.macd_reversal_pending != previous_macd_pending
                    or self.position.macd_reversal_pending_at
                    != previous_macd_pending_at
                ):
                    await self._persist_position_metadata(self.position)
                    if bar_decision is None:
                        pending = self.position.macd_reversal_pending
                        await self.journal.event(
                            (
                                "macd_reversal_pending"
                                if pending
                                else "macd_reversal_cancelled"
                            ),
                            (
                                "MACD reversal is waiting for volume confirmation"
                                if pending
                                else "MACD reversal warning was cancelled"
                            ),
                            {
                                "symbol": self.position.symbol,
                                "direction": self.position.direction.value,
                                "bar_end": bars[-1].end.isoformat(),
                                "macd_hist": str(self.strategy.last_context.macd_hist),
                                "macd_hist_prev": str(
                                    self.strategy.last_context.macd_hist_prev
                                ),
                                "rvol": str(self.strategy.last_context.rvol_val),
                                "rvol_prev": str(
                                    self.strategy.last_context.rvol_prev
                                ),
                            },
                        )
                quote = await self.market.latest_quote(self.position.symbol)
                if quote.bid is not None:
                    previous_highest = self.position.highest_bid
                    previous_trend_runner = self.position.trend_runner
                    local_t = decision_at.astimezone(NY_TZ).time().replace(tzinfo=None)
                    price_decision = self.risk.exit_decision(
                        self.position,
                        quote.bid,
                        decision_at,
                        allow_trailing_stop=local_t >= RULES.phase_opening_end,
                    )
                    if (
                        self.position.highest_bid != previous_highest
                        or self.position.trend_runner != previous_trend_runner
                    ):
                        await self._persist_position_metadata(self.position)
                    decision = price_decision
                    if price_decision is None or price_decision.reason not in {
                        ExitReason.FORCED_CLOSE,
                        ExitReason.STOP_LOSS,
                    }:
                        decision = bar_decision or price_decision
                    if decision is not None:
                        await self._exit_position(decision, quote, decision_at)
                return
            if self.state is not SystemState.READY or signal is None:
                return
            self.log.info(
                "signal | %s %s | spot=%.2f | bar_end=%s",
                signal.direction.value, signal.strategy,
                signal.spot, signal.bar_end.astimezone(NY_TZ).strftime("%H:%M:%S"),
            )
            local_time = decision_at.astimezone(NY_TZ).time().replace(tzinfo=None)
            if not (
                RULES.phase_collect_end
                <= local_time
                < RULES.phase_main_end
            ):
                self.log.warning("signal rejected | outside_entry_window | time=%s", local_time)
                await self.journal.signal(signal, False, "outside_entry_window")
                return
            signal_age = (decision_at - signal.bar_end).total_seconds()
            if signal_age < 0 or signal_age > RULES.signal_ttl_seconds:
                self.log.warning("signal rejected | signal_expired | age=%.1fs", signal_age)
                await self.journal.signal(signal, False, "signal_expired")
                return
            if self.trades_today >= RULES.timed_max_trades_per_day:
                self.log.warning(
                    "signal rejected | max_trades_per_day | trades=%d",
                    self.trades_today,
                )
                await self.journal.signal(signal, False, "max_trades_per_day")
                return
            if self.cooldown_until is not None and decision_at < self.cooldown_until:
                self.log.warning("signal rejected | cooldown")
                await self.journal.signal(signal, False, "cooldown")
                return
            account = await self.broker.account_snapshot()
            if self.settings.volatility_filter_enabled:
                snapshot = self.volatility_filter.evaluate(
                    volatility_bars or [],
                    signal.bar_end,
                    volatility_daily_bars or [],
                )
                self.last_volatility = snapshot
                if snapshot.regime is VolatilityRegime.UNAVAILABLE:
                    self.log.warning(
                        "VIX data unavailable (%s), allowing signal through",
                        snapshot.reason,
                    )
                if not snapshot.allows(signal.direction):
                    reason = f"volatility_{snapshot.regime.value}"
                    if snapshot.reason:
                        reason = f"{reason}_{snapshot.reason}"
                    self.log.warning(
                        "signal rejected | %s | regime=%s", reason, snapshot.regime.value
                    )
                    await self.journal.signal(signal, False, reason)
                    return

            try:
                contracts = await self.market.option_chain(
                    self.settings.underlying_symbol, trading_day
                )
                candidates = self.selector.shortlist(
                    contracts, signal.direction, signal.spot
                )
                snapshots = await asyncio.gather(
                    *(self.market.latest_quote(contract.symbol) for contract in candidates),
                    return_exceptions=True,
                )
            except Exception as exc:
                self.log.warning("signal rejected | option_chain_error | %s", exc)
                await self.journal.signal(signal, False, f"option_chain_error:{exc}")
                return
            liquid_quotes: dict[str, Quote] = {}
            rejection_reasons: list[str] = []
            for contract, snapshot in zip(candidates, snapshots, strict=True):
                if isinstance(snapshot, Exception):
                    rejection_reasons.append("quote_error")
                    continue
                problem = self.risk.quote_problem(snapshot, decision_at)
                if problem is None:
                    liquid_quotes[contract.symbol] = snapshot
                else:
                    rejection_reasons.append(problem)
            contract = self.selector.select(
                candidates, signal.direction, signal.spot, liquid_quotes
            )
            if contract is None or contract.symbol not in liquid_quotes:
                reason = rejection_reasons[0] if rejection_reasons else "no_liquid_contract"
                self.log.warning(
                    "signal rejected | %s | candidates=%d liquid=%d reasons=%s",
                    reason, len(candidates), len(liquid_quotes), rejection_reasons[:3],
                )
                await self.journal.signal(signal, False, reason)
                return
            quote = liquid_quotes[contract.symbol]
            assert quote.ask is not None
            size_factor = Decimal(1)
            quantity = self.risk.position_size(
                account.equity, quote.ask, size_factor
            )
            if quantity < 1:
                self.log.warning(
                    "signal rejected | risk_budget_too_small | ask=%.2f equity=%.2f",
                    quote.ask, account.equity,
                )
                await self.journal.signal(signal, False, "risk_budget_too_small")
                return
            self.log.info(
                "signal accepted | %s | qty=%d | ask=%.2f | contract=%s",
                signal.direction.value, quantity, quote.ask, contract.symbol,
            )
            await self.journal.signal(signal, True, "accepted")
            from .execution import tick_price
            # Entry pricing: start at bid - entry_initial_discount, reprice up to
            # ask + slippage_quote.  Wide initial discount avoids inflated fills
            # while the ask-based ceiling guarantees we can always get filled.
            entry_ask = quote.ask or (quote.bid + RULES.slippage_quote)
            entry_ceiling = tick_price(entry_ask + RULES.slippage_quote)
            request = OrderRequest(
                symbol=contract.symbol,
                side=OrderSide.BUY,
                quantity=quantity,
                limit_price=tick_price(quote.bid - RULES.entry_initial_discount),
                reason=f"entry_{signal.strategy}",
            )
            indicators = {
                **signal.indicators,
                "spot": str(signal.spot),
                "config_version": str(self.config_version),
            }
            self.state = SystemState.ENTRY_PENDING
            await self._publish_trade_signal(
                request, signal.direction, signal.bar_end, indicators
            )
            filled = await self.executor.entry(
                request, self.market.latest_quote, ceiling_price=entry_ceiling
            )
            fully_filled = filled is not None and filled.filled_quantity > 0
            await self.journal.trade_signal_status(
                request.intent_id, "executed" if fully_filled else "failed"
            )
            if not fully_filled or filled is None or filled.average_price is None:
                self.state = SystemState.READY
                return
            self.position = Position(
                symbol=contract.symbol,
                direction=signal.direction,
                quantity=filled.filled_quantity,
                entry_price=filled.average_price,
                opened_at=filled.submitted_at,
                initial_quantity=filled.filled_quantity,
                stop_price=filled.average_price
                * (Decimal(1) - RULES.option_stop_loss_pct),
                broker_order_id=filled.order_id,
                strategy_name=signal.strategy,
                market_state=signal.market_state,
                entry_spot=signal.spot,
                highest_bid=filled.average_price,
                entry_vwap=signal.vwap,
                entry_intent_id=request.intent_id,
            )
            self.entry_reference = signal.spot
            self.position_config_version = self.config_version
            self.position_mae = Decimal(0)
            self.position_mfe = Decimal(0)
            self.trades_today += 1
            record_entry = getattr(self.strategy, "record_entry", None)
            if callable(record_entry):
                record_entry(signal.direction, filled.submitted_at)
            self.realized_pnl -= (
                RULES.fee_per_contract * filled.filled_quantity
            )
            await self._persist_position_metadata(self.position)
            await self.market.subscribe([contract.symbol])
            self.state = SystemState.OPEN

    async def on_position_quote(self, quote: Quote, now: datetime | None = None) -> None:
        """Manage option-price exits on every executable quote."""
        async with self._lock:
            if self.state is not SystemState.OPEN or self.position is None:
                return
            if quote.symbol != self.position.symbol or quote.bid is None:
                return
            decision_at = now or quote.timestamp
            move = (quote.bid - self.position.entry_price) * Decimal(100)
            self.position_mae = min(self.position_mae, move)
            self.position_mfe = max(self.position_mfe, move)
            previous_highest = self.position.highest_bid
            previous_trend_runner = self.position.trend_runner
            local_t = decision_at.astimezone(NY_TZ).time().replace(tzinfo=None)
            decision = self.risk.exit_decision(
                self.position,
                quote.bid,
                decision_at,
                allow_stop_loss=False,
                allow_trailing_stop=local_t >= RULES.phase_opening_end,
            )
            if (
                self.position.highest_bid != previous_highest
                or self.position.trend_runner != previous_trend_runner
            ):
                await self._persist_position_metadata(self.position)
            if decision is not None:
                await self._exit_position(decision, quote, decision_at)

    async def _exit_position(
        self,
        decision: ExitDecision,
        quote: Quote,
        decision_at: datetime,
    ) -> None:
        position = self.position
        if position is None or quote.bid is None:
            return
        quantity = min(decision.quantity, position.quantity)
        if quantity <= 0:
            return
        request = OrderRequest(
            symbol=position.symbol,
            side=OrderSide.SELL,
            quantity=quantity,
            limit_price=quote.bid,
            reason=decision.reason.value,
        )
        self.state = SystemState.EXIT_PENDING
        await self._publish_trade_signal(
            request,
            position.direction,
            decision_at,
            {
                "entry_price": str(position.entry_price),
                "strategy": position.strategy_name or "",
                "market_state": position.market_state.value,
                "execution_style": (
                    "market" if decision.reason is ExitReason.STOP_LOSS else "limit"
                ),
                "trigger_bid": str(quote.bid),
                "stop_price": (
                    str(position.stop_price)
                    if position.stop_price is not None
                    else None
                ),
            },
        )
        if decision.reason is ExitReason.STOP_LOSS:
            hard_stop_price = position.stop_price or (
                position.entry_price
                * (Decimal(1) - RULES.option_stop_loss_pct)
            )
            await self.journal.event(
                "hard_stop_triggered",
                "one-minute close confirmed stop; submitting emergency market exit",
                {
                    "symbol": position.symbol,
                    "quantity": quantity,
                    "stop_price": str(hard_stop_price),
                    "trigger_bid": str(quote.bid),
                    "triggered_at": decision_at.isoformat(),
                },
            )
            filled = await self.executor.emergency_exit(
                request,
                self.market.latest_quote,
            )
        else:
            filled = await self.executor.exit(request, self.market.latest_quote)
        successful = filled is not None and filled.filled_quantity > 0
        await self.journal.trade_signal_status(
            request.intent_id, "executed" if successful else "failed"
        )
        if not successful or filled is None or filled.average_price is None:
            self.state = SystemState.OPEN
            return
        sold = min(filled.filled_quantity, position.quantity)
        fees = RULES.fee_per_contract * Decimal(2) * sold
        gross_pnl = (
            (filled.average_price - position.entry_price) * Decimal(100) * sold
        )
        pnl = gross_pnl - fees
        if decision.reason is ExitReason.STOP_LOSS:
            stop_price = position.stop_price or (
                position.entry_price
                * (Decimal(1) - RULES.option_stop_loss_pct)
            )
            penetration = max(Decimal(0), stop_price - filled.average_price)
            await self.journal.event(
                "hard_stop_filled",
                "emergency market exit filled",
                {
                    "symbol": position.symbol,
                    "quantity": sold,
                    "stop_price": str(stop_price),
                    "trigger_bid": str(quote.bid),
                    "fill_price": str(filled.average_price),
                    "penetration": str(penetration),
                    "penetration_pct": str(
                        penetration / position.entry_price
                    ),
                },
            )
        record_profitable_exit = getattr(
            self.strategy, "record_profitable_exit", None
        )
        if pnl > 0 and callable(record_profitable_exit):
            record_profitable_exit(position.direction, decision_at)
        self.realized_pnl += gross_pnl - RULES.fee_per_contract * sold
        summary = TradeSummary(
            symbol=position.symbol,
            direction=position.direction.value,
            quantity=sold,
            entry_price=position.entry_price,
            exit_price=filled.average_price,
            pnl=pnl,
            fees=fees,
            entry_at=position.opened_at.isoformat(),
            exit_at=decision_at.isoformat(),
            exit_reason=decision.reason.value,
            slippage=max(Decimal(0), quote.bid - filled.average_price),
            mae=self.position_mae,
            mfe=self.position_mfe,
        )
        self.closed_trades.append(summary)
        await self.journal.trade_summary(
            {
                "symbol": summary.symbol,
                "direction": summary.direction,
                "quantity": summary.quantity,
                "entry_price": summary.entry_price,
                "exit_price": summary.exit_price,
                "pnl": summary.pnl,
                "fees": summary.fees,
                "slippage": summary.slippage,
                "mae": summary.mae,
                "mfe": summary.mfe,
                "entry_at": position.opened_at,
                "exit_at": decision_at,
                "exit_reason": summary.exit_reason,
            }
        )
        position.quantity -= sold
        if (
            decision.reason is ExitReason.TRAILING_STOP
            and position.quantity > 0
        ):
            position.trend_runner = True
        if decision.reason in {
            ExitReason.TAKE_PROFIT_1,
            ExitReason.BOLLINGER_MIDDLE,
        }:
            position.first_target_taken = True
            if decision.reason is ExitReason.BOLLINGER_MIDDLE:
                position.range_middle_taken = True
        if decision.new_stop is not None:
            position.stop_price = decision.new_stop
        if position.quantity > 0:
            await self._persist_position_metadata(position)
            self.state = SystemState.OPEN
            return
        self.position = None
        self.position_config_version = None
        self.cooldown_until = decision_at + timedelta(minutes=RULES.cooldown_minutes)
        self.state = SystemState.READY
        if self._pending_settings is not None and self.state is SystemState.READY:
            pending_settings, pending_version = self._pending_settings
            self._activate_settings(pending_settings, pending_version)

    async def _persist_position_metadata(self, position: Position) -> None:
        if position.entry_intent_id is None:
            return
        writer = getattr(self.journal, "trade_signal_metadata", None)
        if writer is None:
            return
        metadata = {
            "strategy": position.strategy_name or "",
            "market_state": position.market_state.value,
            "entry_spot": str(position.entry_spot) if position.entry_spot is not None else None,
            "highest_bid": (
                str(position.highest_bid) if position.highest_bid is not None else None
            ),
            "trend_runner": position.trend_runner,
            "range_middle_taken": position.range_middle_taken,
            "first_target_taken": position.first_target_taken,
            "stop_price": (
                str(position.stop_price) if position.stop_price is not None else None
            ),
            "entry_vwap": (
                str(position.entry_vwap) if position.entry_vwap is not None else None
            ),
            "macd_reversal_pending": position.macd_reversal_pending,
            "macd_reversal_pending_at": (
                position.macd_reversal_pending_at.isoformat()
                if position.macd_reversal_pending_at is not None
                else None
            ),
            "initial_quantity": position.initial_quantity,
            "opened_at": position.opened_at.isoformat(),
        }
        await writer(
            position.entry_intent_id,
            {key: value for key, value in metadata.items() if value is not None},
        )

    async def shutdown(self) -> None:
        self.log.info("shutdown requested")
        if self.position is not None:
            try:
                quote = await self.market.latest_quote(self.position.symbol)
                if quote.bid is not None:
                    requested_quantity = self.position.quantity
                    request = OrderRequest(
                        symbol=self.position.symbol,
                        side=OrderSide.SELL,
                        quantity=requested_quantity,
                        limit_price=quote.bid,
                        reason="shutdown",
                    )
                    await self._publish_trade_signal(
                        request,
                        self.position.direction,
                        datetime.now(timezone.utc),
                        {"entry_price": str(self.position.entry_price)},
                    )
                    filled = await self.executor.exit(request, self.market.latest_quote)
                    fully_closed = (
                        filled is not None and filled.filled_quantity >= requested_quantity
                    )
                    await self.journal.trade_signal_status(
                        request.intent_id, "executed" if fully_closed else "failed"
                    )
                    if filled is not None:
                        self.position.quantity -= filled.filled_quantity
                    if self.position.quantity <= 0:
                        self.position = None
                    else:
                        await self.journal.event(
                            "shutdown_exit_incomplete",
                            "shutdown could not confirm a complete exit",
                            {
                                "symbol": request.symbol,
                                "remaining_quantity": self.position.quantity,
                                "intent_id": str(request.intent_id),
                            },
                        )
                else:
                    await self.journal.event(
                        "shutdown_exit_failed",
                        "shutdown could not publish a sell signal because no executable bid exists",
                        {"symbol": self.position.symbol},
                    )
            except Exception as exc:
                await self.journal.event("shutdown_exit_failed", str(exc))
        await self.broker.close()
        await self.market.close()

    async def _local_account(self, executable_bid: Decimal | None) -> AccountSnapshot:
        broker_account = await self.broker.account_snapshot()
        unrealized = Decimal(0)
        if self.position is not None and executable_bid is not None:
            unrealized = (
                (executable_bid - self.position.entry_price) * Decimal(100) * self.position.quantity
            )
        return AccountSnapshot(
            timestamp=broker_account.timestamp,
            equity=broker_account.equity,
            cash_usd=broker_account.cash_usd,
            day_realized_pnl=self.realized_pnl,
            day_unrealized_pnl=unrealized,
            risk_level=broker_account.risk_level,
            margin_call=broker_account.margin_call,
        )

    async def _halt(self, reason: str) -> None:
        self.state = SystemState.HALTED
        self.last_error = reason
        self.log.error("HALTED: %s", reason)
        await self.journal.event("halted", reason)

    async def _publish_trade_signal(
        self,
        request: OrderRequest,
        direction: Direction,
        decision_at: datetime,
        indicators: dict[str, str],
    ) -> None:
        signal = TradeSignal(
            intent_id=request.intent_id,
            decision_at=decision_at,
            action=request.side,
            direction=direction,
            symbol=request.symbol,
            reference_price=request.limit_price,
            quantity=request.quantity,
            reason=request.reason,
            indicators=indicators,
        )
        await self.journal.trade_signal(signal)
        await self.journal.event(
            f"{request.side.value}_signal",
            f"{request.side.value} {request.quantity} {request.symbol}",
            {
                "intent_id": str(request.intent_id),
                "direction": direction.value,
                "reference_price": str(request.limit_price),
                "reason": request.reason,
            },
        )
        self.log.info(
            "%s %s SIGNAL | %s | %s | QTY=%d | REF=%s | REASON=%s",
            self.settings.trading_mode.value.upper(),
            request.side.value.upper(),
            direction.value.upper(),
            request.symbol,
            request.quantity,
            request.limit_price,
            request.reason,
        )

    def status(self) -> dict:
        return {
            "state": self.state.value,
            "trading_mode": self.settings.trading_mode.value,
            "underlying": self.settings.underlying_symbol,
            "opening_equity": str(self.opening_equity),
            "realized_pnl": str(self.realized_pnl),
            "trades_today": self.trades_today,
            "trading_date": self.trading_date.isoformat() if self.trading_date else None,
            "position": self.position.symbol if self.position else None,
            "market_state": (
                self.position.market_state.value
                if self.position is not None
                else self.strategy.last_state.value
            ),
            "volatility": self.last_volatility.as_dict() if self.last_volatility else None,
            "last_error": self.last_error,
            "config_version": self.config_version,
            "position_config_version": self.position_config_version,
            "pending_config_version": self.pending_config_version,
        }
