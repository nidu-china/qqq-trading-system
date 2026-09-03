"""Event-driven replay using the same strategy and risk rules as live trading."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from statistics import median

import pyarrow.parquet as pq

from .config import NY_TZ, Settings
from .domain import (
    Bar,
    Direction,
    ExitDecision,
    ExitReason,
    MarketState,
    OptionContract,
    Position,
    Quote,
    Signal,
)
from .market_hours import regular_session_bars
from .option_pricing import (
    black_scholes_0dte,
    historical_daily_volatility,
    implied_volatility_from_mid,
    latest_index_volatility,
    quoted_bid_ask,
)
from .policy import RULES
from .risk import ContractSelector, RiskEngine
from .strategy import StrategyEngine, strategy_from_settings
from .volatility import VolatilityFilter, VolatilityRegime


@dataclass(frozen=True, slots=True)
class OptionFrame:
    timestamp: datetime
    spot: Decimal
    contracts: tuple[OptionContract, ...]
    quotes: dict[str, Quote]


@dataclass(frozen=True, slots=True)
class BacktestExitLeg:
    exit_at: datetime
    price: Decimal
    quantity: int
    pnl: Decimal
    fees: Decimal
    slippage: Decimal
    reason: str
    stop_price: Decimal | None = None
    trigger_bid: Decimal | None = None
    fill_bid: Decimal | None = None
    stop_penetration: Decimal | None = None
    stop_penetration_pct: Decimal | None = None


@dataclass(frozen=True, slots=True)
class BacktestEquityPoint:
    timestamp: datetime
    equity: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    position_symbol: str | None = None


@dataclass(frozen=True, slots=True)
class BacktestTrade:
    symbol: str
    direction: Direction
    quantity: int
    entry_at: datetime
    entry_price: Decimal
    exit_at: datetime
    exit_price: Decimal
    pnl: Decimal
    exit_reason: str
    strategy: str = ""
    market_state: MarketState = MarketState.UNKNOWN
    quote_source: str = "synthetic"
    pricing_model: str | None = None
    entry_iv: Decimal | None = None
    iv_source: str | None = None
    entry_spread: Decimal | None = None
    modeled_quote_bars: int = 0
    fees: Decimal = Decimal(0)
    slippage: Decimal = Decimal(0)
    exit_legs: tuple[BacktestExitLeg, ...] = ()


@dataclass(slots=True)
class BacktestResult:
    starting_equity: Decimal
    ending_equity: Decimal
    trades: list[BacktestTrade] = field(default_factory=list)
    signals: int = 0
    rejected: dict[str, int] = field(default_factory=dict)
    option_data_complete: bool = True
    volatility_data_complete: bool = True
    volatility_regimes: dict[str, int] = field(default_factory=dict)
    signal_records: list[dict] = field(default_factory=list)
    equity_curve: list[BacktestEquityPoint] = field(default_factory=list)

    def reject(self, reason: str) -> None:
        self.rejected[reason] = self.rejected.get(reason, 0) + 1

    def record_regime(self, regime: VolatilityRegime) -> None:
        self.volatility_regimes[regime.value] = self.volatility_regimes.get(regime.value, 0) + 1

    def record_signal(
        self,
        signal: Signal,
        status: str,
        reason: str,
        symbol: str | None = None,
        price: Decimal | None = None,
        quantity: int | None = None,
    ) -> None:
        self.signal_records.append(
            {
                "id": f"buy:{signal.id}",
                "action": "buy",
                "decision_at": signal.bar_end.isoformat(),
                "direction": signal.direction.value,
                "symbol": symbol,
                "price": str(price if price is not None else signal.spot),
                "quantity": quantity,
                "status": status,
                "reason": reason,
                "indicators": signal.indicators,
            }
        )


@dataclass(slots=True)
class SyntheticOption:
    contract: OptionContract
    implied_volatility: Decimal
    iv_source: str

    def quote(
        self,
        spot: Decimal,
        timestamp: datetime,
        implied_volatility: Decimal | None = None,
        iv_source: str | None = None,
    ) -> Quote:
        if implied_volatility is not None:
            self.implied_volatility = implied_volatility
        if iv_source is not None:
            self.iv_source = iv_source
        valuation = black_scholes_0dte(
            spot,
            self.contract.strike,
            timestamp,
            self.implied_volatility,
            self.contract.right,
            risk_free_rate=RULES.synthetic_risk_free_rate,
            dividend_yield=RULES.synthetic_dividend_yield,
            minutes_per_year=RULES.synthetic_minutes_per_year,
        )
        theoretical, bid, ask = quoted_bid_ask(
            valuation.price,
            minimum_price=RULES.synthetic_min_price,
            minimum_spread=RULES.synthetic_min_spread,
            maximum_spread=RULES.synthetic_max_spread,
            spread_ratio=RULES.synthetic_spread_ratio,
        )
        quoted_mid = (bid + ask) / Decimal(2)
        return Quote(
            symbol=self.contract.symbol,
            timestamp=timestamp,
            last=theoretical,
            bid=bid,
            ask=ask,
            volume=1000,
            open_interest=5000,
            extra={
                "delta": str(valuation.delta),
                "gamma": str(valuation.gamma),
                "theta": str(valuation.theta),
                "vega": str(valuation.vega),
                "iv": str(self.implied_volatility),
                "iv_source": self.iv_source,
                "mid": str(quoted_mid),
                "theoretical": str(theoretical),
                "model": "black_scholes_0dte",
                "source": "synthetic",
            },
        )


@dataclass(slots=True)
class OpenTrade:
    position: Position
    quantity: int
    entry_at: datetime
    quote_source: str
    entry_mid: Decimal | None = None
    pricing_model: str | None = None
    entry_iv: Decimal | None = None
    iv_source: str | None = None
    entry_spread: Decimal | None = None
    modeled_quote_bars: int = 0
    legs: list[BacktestExitLeg] = field(default_factory=list)


def load_option_frames(path: Path) -> dict[datetime, OptionFrame]:
    rows = pq.ParquetFile(path).read().to_pylist()
    grouped: dict[datetime, list[dict]] = {}
    for row in rows:
        key = row.get("bar_end") or row["captured_at"]
        if isinstance(key, str):
            key = datetime.fromisoformat(key)
        grouped.setdefault(key, []).append(row)
    frames: dict[datetime, OptionFrame] = {}
    for timestamp, items in grouped.items():
        earliest: dict[str, dict] = {}
        for item in items:
            previous = earliest.get(item["symbol"])
            if previous is None or str(item["captured_at"]) < str(previous["captured_at"]):
                earliest[item["symbol"]] = item
        contracts: list[OptionContract] = []
        quotes: dict[str, Quote] = {}
        for row in earliest.values():
            expiry = row["expiry"]
            if isinstance(expiry, str):
                expiry = date.fromisoformat(expiry)
            captured_at = row["captured_at"]
            if isinstance(captured_at, str):
                captured_at = datetime.fromisoformat(captured_at)
            contract = OptionContract(
                row["symbol"],
                row.get("underlying", "QQQ.US"),
                expiry,
                Decimal(str(row["strike"])),
                Direction(row["direction"]),
            )
            contracts.append(contract)
            quotes[contract.symbol] = Quote(
                symbol=contract.symbol,
                timestamp=captured_at,
                last=Decimal(str(row.get("last") or 0)),
                bid=Decimal(str(row["bid"])) if row.get("bid") is not None else None,
                ask=Decimal(str(row["ask"])) if row.get("ask") is not None else None,
                volume=int(row.get("volume") or 0),
                open_interest=int(row.get("open_interest") or 0),
                extra={
                    key: str(row[key])
                    for key in ("iv", "delta", "gamma", "theta", "vega", "rho")
                    if row.get(key) is not None
                },
            )
        if contracts:
            first = next(iter(earliest.values()))
            frames[timestamp] = OptionFrame(
                timestamp,
                Decimal(str(first.get("spot") or 0)),
                tuple(contracts),
                quotes,
            )
    return frames


def load_option_frames_path(path: Path) -> dict[datetime, OptionFrame]:
    if path.is_file():
        return load_option_frames(path)
    frames: dict[datetime, OptionFrame] = {}
    for candidate in sorted(path.rglob("data.parquet")):
        frames.update(load_option_frames(candidate))
    return frames


class EventDrivenBacktester:
    def __init__(
        self,
        settings: Settings,
        strategy: StrategyEngine | None = None,
        selector: ContractSelector | None = None,
        risk: RiskEngine | None = None,
    ) -> None:
        self.settings = settings
        self.strategy = strategy or strategy_from_settings(settings)
        self.selector = selector or ContractSelector()
        self.risk = risk or RiskEngine(settings)
        self.volatility_filter = VolatilityFilter(settings)

    @staticmethod
    def _synthetic_option(
        signal: Signal,
        trading_day: date,
        implied_volatility: Decimal,
        iv_source: str,
    ) -> SyntheticOption:
        if signal.direction is Direction.CALL:
            strike = (signal.spot + RULES.strike_offset).to_integral_value(rounding=ROUND_FLOOR)
        else:
            strike = (signal.spot - RULES.strike_offset).to_integral_value(rounding=ROUND_CEILING)
        marker = "C" if signal.direction is Direction.CALL else "P"
        symbol = f"QQQ{trading_day.strftime('%y%m%d')}{marker}{int(strike * 1000):08d}.US"
        contract = OptionContract(symbol, "QQQ.US", trading_day, strike, signal.direction)
        return SyntheticOption(contract, implied_volatility, iv_source)

    @staticmethod
    def _observed_option_iv(
        frames: dict[datetime, OptionFrame],
        timestamp: datetime,
        direction: Direction,
    ) -> Decimal | None:
        decision_date = timestamp.astimezone(NY_TZ).date()
        minute = timestamp.replace(second=0, microsecond=0)
        candidates: list[OptionFrame] = []
        seen: set[datetime] = set()
        direct = frames.get(timestamp)
        if direct is not None:
            candidates.append(direct)
            seen.add(direct.timestamp)
        for offset in range(RULES.synthetic_observed_iv_max_age_minutes + 1):
            frame = frames.get(minute - timedelta(minutes=offset))
            if frame is not None and frame.timestamp not in seen:
                candidates.append(frame)
                seen.add(frame.timestamp)
        for frame in candidates:
            if frame.timestamp.astimezone(NY_TZ).date() != decision_date:
                continue
            age = (timestamp - frame.timestamp).total_seconds() / 60
            if age < 0 or age > RULES.synthetic_observed_iv_max_age_minutes:
                continue
            values: list[Decimal] = []
            for contract in frame.contracts:
                if contract.right is not direction:
                    continue
                quote = frame.quotes.get(contract.symbol)
                if quote is None or quote.timestamp > timestamp:
                    continue
                mid = quote.mid
                value = None
                if mid is not None and frame.spot > 0:
                    value = implied_volatility_from_mid(
                        frame.spot,
                        contract.strike,
                        quote.timestamp,
                        mid,
                        direction,
                        risk_free_rate=RULES.synthetic_risk_free_rate,
                        dividend_yield=RULES.synthetic_dividend_yield,
                        minutes_per_year=RULES.synthetic_minutes_per_year,
                        floor=RULES.synthetic_iv_floor,
                        cap=RULES.synthetic_iv_cap,
                    )
                if value is None and quote.extra.get("iv") is not None:
                    value = Decimal(str(quote.extra["iv"]))
                if value is None:
                    continue
                if RULES.synthetic_iv_floor <= value <= RULES.synthetic_iv_cap:
                    values.append(value)
            if values:
                return Decimal(str(median(values)))
        return None

    @classmethod
    def _synthetic_iv(
        cls,
        bars: list[Bar],
        volatility_bars: list[Bar],
        frames: dict[datetime, OptionFrame],
        timestamp: datetime,
        direction: Direction,
    ) -> tuple[Decimal, str]:
        observed = cls._observed_option_iv(frames, timestamp, direction)
        if observed is not None:
            value, source = observed, "observed_option"
        else:
            vix = latest_index_volatility(volatility_bars, timestamp)
            if vix is not None:
                value, source = vix * RULES.synthetic_vix_multiplier, "vix"
            else:
                historical = historical_daily_volatility(bars, timestamp)
                if historical is not None:
                    value, source = historical, "historical_20d"
                else:
                    value, source = RULES.synthetic_default_iv, "default"
        if direction is Direction.PUT and source != "observed_option":
            value += RULES.synthetic_put_iv_skew
        return (
            min(RULES.synthetic_iv_cap, max(RULES.synthetic_iv_floor, value)),
            source,
        )

    @staticmethod
    def _frame_at(frames: dict[datetime, OptionFrame], timestamp: datetime) -> OptionFrame | None:
        direct = frames.get(timestamp)
        if direct is not None:
            return direct
        candidates = [
            frame
            for key, frame in frames.items()
            if timestamp <= key and (key - timestamp).total_seconds() <= RULES.signal_ttl_seconds
        ]
        return min(candidates, key=lambda item: item.timestamp) if candidates else None

    @staticmethod
    def _finalize_trade(open_trade: OpenTrade) -> BacktestTrade:
        legs = tuple(open_trade.legs)
        sold = sum(leg.quantity for leg in legs)
        weighted_exit = (
            sum((leg.price * leg.quantity for leg in legs), Decimal(0)) / Decimal(sold)
            if sold
            else Decimal(0)
        )
        return BacktestTrade(
            symbol=open_trade.position.symbol,
            direction=open_trade.position.direction,
            quantity=open_trade.quantity,
            entry_at=open_trade.entry_at,
            entry_price=open_trade.position.entry_price,
            exit_at=legs[-1].exit_at,
            exit_price=weighted_exit,
            pnl=sum((leg.pnl for leg in legs), Decimal(0)),
            exit_reason=legs[-1].reason,
            strategy=open_trade.position.strategy_name or "",
            market_state=open_trade.position.market_state,
            quote_source=open_trade.quote_source,
            pricing_model=open_trade.pricing_model,
            entry_iv=open_trade.entry_iv,
            iv_source=open_trade.iv_source,
            entry_spread=open_trade.entry_spread,
            modeled_quote_bars=open_trade.modeled_quote_bars,
            fees=sum((leg.fees for leg in legs), Decimal(0)),
            slippage=sum((leg.slippage for leg in legs), Decimal(0)),
            exit_legs=legs,
        )

    def run(
        self,
        bars: list[Bar],
        option_frames: dict[datetime, OptionFrame],
        starting_equity: Decimal,
        volatility_bars: list[Bar] | None = None,
        volatility_daily_bars: list[Bar] | None = None,
        cancel_check: Callable[[], bool] | None = None,
        trade_start: date | None = None,
        warmup_bars: list[Bar] | None = None,
        reset_daily_context: bool = False,
        enable_pyramid_scaling: bool = False,
    ) -> BacktestResult:
        result = BacktestResult(starting_equity, starting_equity)
        available = sorted(
            regular_session_bars(bar for bar in (warmup_bars or []) if bar.complete),
            key=lambda item: item.end,
        )
        position: Position | None = None
        open_trade: OpenTrade | None = None
        synthetic: SyntheticOption | None = None
        last_real_quote: Quote | None = None
        last_mark_bid: Decimal | None = None
        realized = Decimal(0)
        current_day: date | None = None
        day_opening_equity = starting_equity
        day_start_realized = Decimal(0)
        daily_halted = False
        trades_today = 0
        cooldown_until: datetime | None = None
        last_processed: Bar | None = None
        cancelled = False

        def record_equity(
            timestamp: datetime,
            executable_bid: Decimal | None = None,
        ) -> None:
            nonlocal last_mark_bid
            if executable_bid is not None:
                last_mark_bid = executable_bid
            unrealized = Decimal(0)
            position_symbol = None
            if position is not None:
                position_symbol = position.symbol
                mark_bid = last_mark_bid or position.entry_price
                unrealized = (mark_bid - position.entry_price) * Decimal(
                    100
                ) * position.quantity - RULES.fee_per_contract * Decimal(2) * position.quantity
            point = BacktestEquityPoint(
                timestamp,
                starting_equity + realized + unrealized,
                realized,
                unrealized,
                position_symbol,
            )
            if result.equity_curve and result.equity_curve[-1].timestamp == timestamp:
                result.equity_curve[-1] = point
            else:
                result.equity_curve.append(point)

        def close_leg(decision: ExitDecision, quote: Quote, timestamp: datetime) -> None:
            nonlocal position, open_trade, synthetic, last_real_quote, last_mark_bid
            nonlocal realized, cooldown_until
            assert position is not None and open_trade is not None and quote.bid is not None
            quantity = min(decision.quantity, position.quantity)
            fees = RULES.fee_per_contract * Decimal(2) * quantity
            pnl = (quote.bid - position.entry_price) * Decimal(100) * quantity - fees
            if open_trade.quote_source == "synthetic":
                exit_mid = Decimal(str(quote.extra.get("mid", quote.mid or quote.bid)))
                entry_mid = open_trade.entry_mid or position.entry_price
                slippage = (
                    (
                        max(Decimal(0), position.entry_price - entry_mid)
                        + max(Decimal(0), exit_mid - quote.bid)
                    )
                    * Decimal(100)
                    * quantity
                )
            else:
                slippage = Decimal(0)
            realized += pnl
            stop_price = None
            trigger_bid = None
            fill_bid = None
            stop_penetration = None
            stop_penetration_pct = None
            if decision.reason is ExitReason.STOP_LOSS:
                stop_price = position.stop_price or (
                    position.entry_price * (Decimal(1) - RULES.option_stop_loss_pct)
                )
                trigger_bid = quote.bid
                fill_bid = quote.bid
                stop_penetration = max(Decimal(0), stop_price - fill_bid)
                stop_penetration_pct = (
                    stop_penetration / position.entry_price
                    if position.entry_price > 0
                    else Decimal(0)
                )
            leg = BacktestExitLeg(
                timestamp,
                quote.bid,
                quantity,
                pnl,
                fees,
                slippage,
                decision.reason.value,
                stop_price,
                trigger_bid,
                fill_bid,
                stop_penetration,
                stop_penetration_pct,
            )
            open_trade.legs.append(leg)
            record_profitable_exit = getattr(self.strategy, "record_profitable_exit", None)
            if pnl > 0 and callable(record_profitable_exit):
                record_profitable_exit(position.direction, timestamp)
            record_stop_loss = getattr(self.strategy, "record_stop_loss", None)
            if decision.reason.value == "stop_loss" and callable(record_stop_loss):
                record_stop_loss(position.direction)
            result.signal_records.append(
                {
                    "id": f"sell:{len(result.signal_records)}:{timestamp.isoformat()}",
                    "action": "sell",
                    "decision_at": timestamp.isoformat(),
                    "direction": position.direction.value,
                    "symbol": position.symbol,
                    "price": str(quote.bid),
                    "quantity": quantity,
                    "status": "executed",
                    "reason": decision.reason.value,
                    "indicators": {
                        "pnl": str(pnl),
                        "stop_price": str(stop_price) if stop_price is not None else None,
                        "trigger_bid": str(trigger_bid) if trigger_bid is not None else None,
                        "fill_bid": str(fill_bid) if fill_bid is not None else None,
                        "stop_penetration": (
                            str(stop_penetration) if stop_penetration is not None else None
                        ),
                        "stop_penetration_pct": (
                            str(stop_penetration_pct) if stop_penetration_pct is not None else None
                        ),
                    },
                }
            )
            position.quantity -= quantity
            if decision.reason is ExitReason.TRAILING_STOP and position.quantity > 0:
                position.trend_runner = True
            if decision.reason in {
                ExitReason.TAKE_PROFIT_1,
                ExitReason.BOLLINGER_MIDDLE,
            }:
                position.first_target_taken = True
                position.range_middle_taken |= decision.reason is ExitReason.BOLLINGER_MIDDLE
            if decision.new_stop is not None:
                position.stop_price = decision.new_stop
            if position.quantity <= 0:
                result.trades.append(self._finalize_trade(open_trade))
                position = None
                open_trade = None
                synthetic = None
                last_real_quote = None
                last_mark_bid = None
                cooldown_until = timestamp + timedelta(minutes=RULES.cooldown_minutes)

        # Include 09:00-09:30 premarket for indicator warmup on trading days
        from datetime import time as time_type
        all_intraday_bars = sorted(
            (bar for bar in bars if bar.complete),
            key=lambda item: item.end,
        )
        ordered = [
            bar for bar in all_intraday_bars
            if time_type(9, 0) <= bar.start.astimezone(NY_TZ).time() < time_type(16, 0)
        ]
        for bar in ordered:
            if cancel_check is not None and cancel_check():
                cancelled = True
                break
            last_processed = bar
            trading_day = bar.end.astimezone(NY_TZ).date()
            if trading_day != current_day:
                current_day = trading_day
                trades_today = 0
                cooldown_until = None
                day_opening_equity = starting_equity + realized
                day_start_realized = realized
                daily_halted = False
                if reset_daily_context:
                    # Simulate daily engine restart: each day starts with only today's
                    # premarket bars (09:00-09:29 ET). The strategy's _one_minute_context
                    # uses these 30 bars to warm up BOLL/MACD/EMA before RTH opens.
                    from datetime import time as _time_type
                    available = [
                        b for b in available
                        if b.end.astimezone(NY_TZ).date() == trading_day
                        and b.start.astimezone(NY_TZ).time() < _time_type(9, 30)
                    ]
            available.append(bar)
            set_volatility_context = getattr(self.strategy, "set_volatility_context", None)
            if callable(set_volatility_context):
                set_volatility_context(volatility_bars or [], bar.end)
            signal = self.strategy.evaluate(available)

            if position is not None:
                frame = option_frames.get(bar.end)
                if frame is not None and position.symbol in frame.quotes:
                    quote = frame.quotes[position.symbol]
                    last_real_quote = quote
                elif synthetic is not None:
                    implied_volatility, iv_source = self._synthetic_iv(
                        available,
                        volatility_bars or [],
                        option_frames,
                        bar.end,
                        position.direction,
                    )
                    quote = synthetic.quote(
                        bar.close,
                        bar.end,
                        implied_volatility,
                        iv_source,
                    )
                    result.option_data_complete = False
                    assert open_trade is not None
                    open_trade.modeled_quote_bars += 1
                    if open_trade.pricing_model is None:
                        open_trade.pricing_model = str(quote.extra["model"])
                        open_trade.iv_source = str(quote.extra["iv_source"])
                elif last_real_quote is not None:
                    result.option_data_complete = False
                    record_equity(bar.end)
                    continue
                else:
                    result.option_data_complete = False
                    record_equity(bar.end)
                    continue
                if quote.bid is None:
                    record_equity(bar.end)
                    continue

                # ── Pyramid add: scale into winning position ─────────────────
                if (
                    enable_pyramid_scaling
                    and position.pyramid_stage < 2
                    and position.pyramid_target_qty > 0
                    and quote.ask is not None
                    and open_trade is not None
                ):
                    pyramid_fn = getattr(self.strategy, "pyramid_add_decision", None)
                    if callable(pyramid_fn):
                        add_result = pyramid_fn(position)
                        if add_result is not None:
                            add_qty, add_reason = add_result
                            add_price = quote.ask
                            old_qty = position.quantity
                            new_qty = old_qty + add_qty
                            # Weighted-average entry price
                            avg_entry = (
                                position.entry_price * Decimal(old_qty)
                                + add_price * Decimal(add_qty)
                            ) / Decimal(new_qty)
                            position.entry_price = avg_entry
                            position.quantity = new_qty
                            open_trade.quantity += add_qty
                            position.pyramid_stage += 1
                            # Update stop to reflect new average
                            position.stop_price = avg_entry * (
                                Decimal(1) - RULES.option_stop_loss_pct
                            )
                            result.signal_records.append({
                                "id": f"pyramid:{add_reason}:{bar.end.isoformat()}",
                                "action": "buy",
                                "decision_at": bar.end.isoformat(),
                                "direction": position.direction.value,
                                "symbol": position.symbol,
                                "price": str(add_price),
                                "quantity": add_qty,
                                "status": "executed",
                                "reason": add_reason,
                                "indicators": {
                                    "pyramid_stage": str(position.pyramid_stage),
                                    "total_qty": str(new_qty),
                                    "avg_entry": str(avg_entry),
                                },
                            })

                day_realized_pnl = realized - day_start_realized
                unrealized_pnl = (quote.bid - position.entry_price) * Decimal(
                    100
                ) * position.quantity - RULES.fee_per_contract * Decimal(2) * position.quantity
                if self.risk.daily_loss_breached(
                    day_opening_equity,
                    day_realized_pnl,
                    unrealized_pnl,
                ):
                    close_leg(
                        ExitDecision(ExitReason.DAILY_LOSS, position.quantity),
                        quote,
                        bar.end,
                    )
                    daily_halted = True
                    record_equity(bar.end, quote.bid)
                    continue
                bar_local_time = bar.end.astimezone(NY_TZ).time().replace(tzinfo=None)
                price_decision = self.risk.exit_decision(
                    position,
                    quote.bid,
                    bar.end,
                    allow_trailing_stop=bar_local_time >= RULES.phase_opening_end,
                )
                previous_macd_pending = position.macd_reversal_pending
                previous_macd_pending_at = position.macd_reversal_pending_at
                bar_decision = self.strategy.bar_exit_decision(position)
                pending_changed = (
                    position.macd_reversal_pending != previous_macd_pending
                    or position.macd_reversal_pending_at != previous_macd_pending_at
                )
                if pending_changed and bar_decision is None:
                    ctx = self.strategy.last_context
                    pending = position.macd_reversal_pending
                    result.signal_records.append(
                        {
                            "id": f"macd-warning:{bar.end.isoformat()}",
                            "action": "sell",
                            "decision_at": bar.end.isoformat(),
                            "direction": position.direction.value,
                            "symbol": position.symbol,
                            "price": str(quote.bid),
                            "quantity": position.quantity,
                            "status": "rejected",
                            "reason": (
                                "macd_reversal_pending_volume_confirmation"
                                if pending
                                else "macd_reversal_pending_cancelled"
                            ),
                            "indicators": {
                                "macd_hist": str(ctx.macd_hist),
                                "macd_hist_prev": str(ctx.macd_hist_prev),
                                "rvol": str(ctx.rvol_val),
                                "rvol_prev": str(ctx.rvol_prev),
                            },
                        }
                    )
                decision = price_decision
                if price_decision is None or price_decision.reason not in {
                    ExitReason.FORCED_CLOSE,
                    ExitReason.STOP_LOSS,
                }:
                    decision = bar_decision or price_decision
                if decision is not None:
                    close_leg(decision, quote, bar.end)
                record_equity(bar.end, quote.bid)
                continue

            if signal is None or (trade_start is not None and trading_day < trade_start):
                record_equity(bar.end)
                continue
            result.signals += 1
            if daily_halted or self.risk.daily_loss_breached(
                day_opening_equity,
                realized - day_start_realized,
            ):
                daily_halted = True
                result.reject("daily_loss")
                result.record_signal(signal, "rejected", "daily_loss")
                record_equity(bar.end)
                continue
            if trades_today >= RULES.timed_max_trades_per_day:
                result.reject("max_trades_per_day")
                result.record_signal(signal, "rejected", "max_trades_per_day")
                record_equity(bar.end)
                continue
            if cooldown_until is not None and bar.end < cooldown_until:
                result.reject("cooldown")
                result.record_signal(signal, "rejected", "cooldown")
                record_equity(bar.end)
                continue
            if self.settings.volatility_filter_enabled:
                snapshot = self.volatility_filter.evaluate(
                    volatility_bars or [], bar.end, volatility_daily_bars or []
                )
                result.record_regime(snapshot.regime)
                if snapshot.regime is VolatilityRegime.UNAVAILABLE:
                    result.volatility_data_complete = False
                if not snapshot.allows(signal.direction):
                    reason = f"volatility_{snapshot.regime.value}"
                    if snapshot.reason:
                        reason = f"{reason}_{snapshot.reason}"
                    result.reject(reason)
                    result.record_signal(signal, "rejected", reason)
                    record_equity(bar.end)
                    continue
            frame = self._frame_at(option_frames, signal.bar_end)
            contract: OptionContract | None = None
            quote: Quote | None = None
            quote_source = "real"
            if frame is not None:
                valid_quotes = {
                    symbol: item
                    for symbol, item in frame.quotes.items()
                    if self.risk.quote_problem(item, item.timestamp) is None
                }
                contract = self.selector.select(
                    frame.contracts, signal.direction, signal.spot, valid_quotes
                )
                if contract is not None:
                    quote = valid_quotes.get(contract.symbol)
            if contract is None or quote is None or quote.ask is None:
                implied_volatility, iv_source = self._synthetic_iv(
                    available,
                    volatility_bars or [],
                    option_frames,
                    signal.bar_end,
                    signal.direction,
                )
                synthetic = self._synthetic_option(
                    signal,
                    trading_day,
                    implied_volatility,
                    iv_source,
                )
                quote = synthetic.quote(signal.spot, signal.bar_end)
                contract = synthetic.contract
                quote_source = "synthetic"
                result.option_data_complete = False
            size_factor = Decimal(1)
            quantity = self.risk.position_size(
                starting_equity + realized,
                quote.ask,
                size_factor,
            )
            if quantity < 1:
                result.reject("risk_budget_too_small")
                result.record_signal(signal, "rejected", "risk_budget_too_small", contract.symbol)
                synthetic = None
                record_equity(bar.end)
                continue
            full_target_quantity = quantity
            if enable_pyramid_scaling:
                quantity = max(1, round(full_target_quantity * Decimal("0.30")))
            entry_price = quote.ask
            result.record_signal(
                signal,
                "accepted",
                f"entry_{signal.strategy}",
                contract.symbol,
                entry_price,
                quantity,
            )
            if quote_source == "synthetic":
                result.signal_records[-1]["indicators"] = {
                    **result.signal_records[-1]["indicators"],
                    "option_pricing_model": quote.extra["model"],
                    "synthetic_iv": quote.extra["iv"],
                    "synthetic_iv_source": quote.extra["iv_source"],
                    "synthetic_strike": str(contract.strike),
                    "synthetic_spread": str(quote.spread),
                }
            execution_at = quote.timestamp if quote_source == "real" else signal.bar_end
            position = Position(
                contract.symbol,
                signal.direction,
                quantity,
                entry_price,
                execution_at,
                initial_quantity=quantity,
                stop_price=entry_price * (Decimal(1) - RULES.option_stop_loss_pct),
                strategy_name=signal.strategy,
                market_state=signal.market_state,
                entry_spot=signal.spot,
                highest_bid=entry_price,
                entry_vwap=signal.vwap,
                pyramid_stage=0,
                pyramid_target_qty=full_target_quantity if enable_pyramid_scaling else 0,
            )
            record_entry = getattr(self.strategy, "record_entry", None)
            if callable(record_entry):
                record_entry(signal.direction, execution_at)
            entry_mid = (
                Decimal(str(quote.extra.get("mid", quote.mid or entry_price)))
                if quote_source == "synthetic"
                else None
            )
            open_trade = OpenTrade(
                position,
                quantity,
                execution_at,
                quote_source,
                entry_mid,
                str(quote.extra.get("model")) if quote_source == "synthetic" else None,
                Decimal(str(quote.extra["iv"])) if quote_source == "synthetic" else None,
                str(quote.extra.get("iv_source")) if quote_source == "synthetic" else None,
                quote.spread if quote_source == "synthetic" else None,
            )
            if synthetic is None:
                implied_volatility, iv_source = self._synthetic_iv(
                    available,
                    volatility_bars or [],
                    option_frames,
                    execution_at,
                    signal.direction,
                )
                synthetic = SyntheticOption(
                    contract,
                    implied_volatility,
                    iv_source,
                )
            last_real_quote = quote if quote_source == "real" else None
            last_mark_bid = quote.bid
            trades_today += 1
            record_equity(bar.end, quote.bid)

        if position is not None and open_trade is not None and last_processed is not None:
            final_bar = last_processed
            if synthetic is not None:
                implied_volatility, iv_source = self._synthetic_iv(
                    available,
                    volatility_bars or [],
                    option_frames,
                    final_bar.end,
                    position.direction,
                )
                quote = synthetic.quote(
                    final_bar.close,
                    final_bar.end,
                    implied_volatility,
                    iv_source,
                )
            else:
                quote = last_real_quote
            if quote is not None and quote.bid is not None:
                close_leg(
                    ExitDecision(
                        ExitReason.SHUTDOWN if cancelled else ExitReason.FORCED_CLOSE,
                        position.quantity,
                    ),
                    quote,
                    final_bar.end,
                )
                record_equity(final_bar.end, quote.bid)
        result.ending_equity = starting_equity + realized
        return result
