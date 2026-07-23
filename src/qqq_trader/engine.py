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
from .reporting import TradeSummary
from .risk import ContractSelector, RiskEngine
from .strategy import strategy_from_settings
from .volatility import VolatilityFilter, VolatilitySnapshot


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
        self.strategy = strategy_from_settings(settings)
        self.selector = ContractSelector(settings.strike_offset)
        self.risk = RiskEngine(settings)
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
        """Atomically apply settings, deferring while an order or position is active."""
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
        self.strategy = strategy_from_settings(settings)
        self.selector = ContractSelector(settings.strike_offset)
        self.risk = RiskEngine(settings)
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
            deadline = asyncio.get_running_loop().time() + self.settings.order_timeout_seconds
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
            position.opened_at = signal.decision_at
            position.initial_quantity = signal.quantity
            if position.quantity < signal.quantity:
                position.first_target_taken = True
                position.stop_price = position.entry_price
            self.position = position
            self.position_config_version = self.config_version
            self.entry_reference = signal.reference_price
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
        async with self._lock:
            if self.state is not SystemState.READY or self.position is not None or not bars:
                return
            now = now or datetime.now(timezone.utc)
            local_now = now.astimezone(NY_TZ)
            account: AccountSnapshot | None = None
            if self.trading_date != local_now.date():
                self.trading_date = local_now.date()
                self.trades_today = 0
                self.realized_pnl = Decimal(0)
                self.cooldown_until = None
                self.last_signal_bar = None
                account = await self._local_account(None)
                self.opening_equity = account.equity
                await self.journal.event(
                    "trading_day_started",
                    f"daily signal and risk counters reset for {self.trading_date}",
                )
            local_time = local_now.time().replace(tzinfo=None)
            if not self.settings.entry_start <= local_time <= self.settings.entry_end:
                return
            if self.cooldown_until and now < self.cooldown_until:
                return
            if self.trades_today >= self.settings.max_trades_per_day:
                return
            newest = bars[-1]
            if self.last_signal_bar == newest.end:
                return

            account = account or await self._local_account(None)
            if self.risk.daily_loss_breached(account, self.opening_equity):
                await self._halt("daily loss limit reached")
                return
            spot_quote = await self.market.latest_quote(self.settings.underlying_symbol)
            quote_age = Decimal(str((now - spot_quote.timestamp).total_seconds()))
            if quote_age < 0 or quote_age > self.settings.max_quote_age_seconds:
                await self.journal.event("signal_rejected", "stale underlying quote")
                return
            signal = self.strategy.evaluate(bars, spot_quote.last)
            if signal is None:
                return
            self.last_signal_bar = newest.end
            self.log.info(
                "signal detected | %s | spot=%.2f | bar_end=%s",
                signal.direction.value, spot_quote.last, newest.end.isoformat(),
            )

            if self.settings.volatility_filter_enabled:
                snapshot = self.volatility_filter.evaluate(
                    volatility_bars or [],
                    newest.end,
                    volatility_daily_bars or [],
                )
                self.last_volatility = snapshot
                await self.journal.event(
                    "volatility_regime",
                    snapshot.regime.value,
                    {"bar_end": newest.end.isoformat(), **snapshot.as_dict()},
                )
                if not snapshot.allows(signal.direction):
                    reason = f"volatility_{snapshot.regime.value}"
                    if snapshot.reason:
                        reason = f"{reason}_{snapshot.reason}"
                    self.log.info("signal rejected | volatility filter | %s", reason)
                    await self.journal.signal(signal, False, reason)
                    return

            contracts = await self.market.option_chain(
                self.settings.underlying_symbol, local_now.date()
            )
            contract = self.selector.select(contracts, signal.direction, spot_quote.last)
            if contract is None:
                await self.journal.signal(signal, False, "same_day_contract_not_found")
                return
            await self.market.subscribe([contract.symbol])
            option_quote = await self.market.latest_quote(contract.symbol)
            problem = self.risk.quote_problem(option_quote, now)
            if problem:
                await self.journal.signal(signal, False, problem)
                return
            assert option_quote.ask is not None
            quantity = self.risk.position_size(account, option_quote.ask)
            if quantity < 1:
                await self.journal.signal(signal, False, "risk_budget_too_small")
                return

            await self.journal.signal(signal, True)
            request = OrderRequest(
                symbol=contract.symbol,
                side=OrderSide.BUY,
                quantity=quantity,
                limit_price=option_quote.ask,
                reason=f"entry_{signal.direction.value}",
            )
            await self._publish_trade_signal(
                request,
                signal.direction,
                signal.bar_end,
                signal.indicators,
            )
            self.state = SystemState.ENTRY_PENDING
            filled = await self.executor.entry(request, self.market.latest_quote)
            if filled is None or filled.average_price is None:
                await self.journal.trade_signal_status(request.intent_id, "failed")
                self.state = SystemState.READY
                return
            await self.journal.trade_signal_status(request.intent_id, "executed")
            self.position = Position(
                symbol=contract.symbol,
                direction=signal.direction,
                quantity=filled.filled_quantity,
                entry_price=filled.average_price,
                opened_at=filled.submitted_at,
                broker_order_id=filled.order_id,
            )
            self.position_config_version = self.config_version
            self.position_mae = Decimal(0)
            self.position_mfe = Decimal(0)
            self.entry_reference = option_quote.ask
            self.trades_today += 1
            self.state = SystemState.OPEN
            await self.journal.event(
                "position_opened",
                f"opened {contract.symbol}",
                {"quantity": self.position.quantity, "price": str(self.position.entry_price)},
            )

    async def on_position_quote(self, quote: Quote, now: datetime | None = None) -> None:
        async with self._lock:
            if self.state is not SystemState.OPEN or self.position is None:
                return
            if quote.symbol != self.position.symbol or quote.bid is None:
                return
            now = now or datetime.now(timezone.utc)
            mark_pnl = (
                (quote.bid - self.position.entry_price) * Decimal(100) * self.position.quantity
            )
            self.position_mae = min(self.position_mae, mark_pnl)
            self.position_mfe = max(self.position_mfe, mark_pnl)
            account = await self._local_account(quote.bid)
            daily_breach = self.risk.daily_loss_breached(account, self.opening_equity)
            decision = self.risk.exit_decision(self.position, quote.bid, now, daily_breach)
            if decision is None:
                return

            self.log.info(
                "exit triggered | %s | bid=%.4f | reason=%s | qty=%d",
                self.position.symbol, quote.bid, decision.reason.value, decision.quantity,
            )
            self.state = SystemState.EXIT_PENDING
            request = OrderRequest(
                symbol=self.position.symbol,
                side=OrderSide.SELL,
                quantity=decision.quantity,
                limit_price=quote.bid,
                reason=decision.reason.value,
            )
            await self._publish_trade_signal(
                request,
                self.position.direction,
                now,
                {
                    "entry_price": str(self.position.entry_price),
                    "mark_pnl": str(mark_pnl),
                    "mae": str(self.position_mae),
                    "mfe": str(self.position_mfe),
                },
            )
            filled = await self.executor.exit(request, self.market.latest_quote)
            if filled is None or filled.average_price is None:
                await self.journal.trade_signal_status(request.intent_id, "failed")
                await self._halt("critical exit failure; broker state must be reconciled")
                return
            await self.journal.trade_signal_status(request.intent_id, "executed")

            pnl = (filled.average_price - self.position.entry_price) * Decimal(
                100
            ) * filled.filled_quantity - self.settings.fee_per_contract * filled.filled_quantity
            self.realized_pnl += pnl
            summary = TradeSummary(
                symbol=self.position.symbol,
                direction=self.position.direction.value,
                quantity=filled.filled_quantity,
                entry_price=self.position.entry_price,
                exit_price=filled.average_price,
                pnl=pnl,
                fees=self.settings.fee_per_contract * filled.filled_quantity,
                entry_at=self.position.opened_at.isoformat(),
                exit_at=now.isoformat(),
                exit_reason=decision.reason.value,
                slippage=abs(self.position.entry_price - self.entry_reference)
                + abs(filled.average_price - quote.bid),
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
                    "entry_at": datetime.fromisoformat(summary.entry_at),
                    "exit_at": datetime.fromisoformat(summary.exit_at),
                    "exit_reason": summary.exit_reason,
                    "slippage": summary.slippage,
                    "mae": summary.mae,
                    "mfe": summary.mfe,
                }
            )
            self.position.quantity -= filled.filled_quantity
            if self.position.quantity <= 0:
                self.position = None
                self.position_config_version = None
                self.position_mae = Decimal(0)
                self.position_mfe = Decimal(0)
                self.entry_reference = Decimal(0)
                self.cooldown_until = now + timedelta(minutes=self.settings.cooldown_minutes)
                self.state = SystemState.HALTED if daily_breach else SystemState.READY
                if self._pending_settings is not None:
                    pending_settings, pending_version = self._pending_settings
                    self._activate_settings(pending_settings, pending_version)
            else:
                self.position.first_target_taken = True
                self.position.stop_price = decision.new_stop
                self.state = SystemState.OPEN
            await self.journal.event(
                "position_reduced",
                decision.reason.value,
                {"quantity": filled.filled_quantity, "pnl": str(pnl)},
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
            "volatility": self.last_volatility.as_dict() if self.last_volatility else None,
            "last_error": self.last_error,
            "config_version": self.config_version,
            "position_config_version": self.position_config_version,
            "pending_config_version": self.pending_config_version,
        }
