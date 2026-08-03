"""Event-driven replay using the same strategy and risk rules as live trading."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

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

    def reject(self, reason: str) -> None:
        self.rejected[reason] = self.rejected.get(reason, 0) + 1

    def record_regime(self, regime: VolatilityRegime) -> None:
        self.volatility_regimes[regime.value] = (
            self.volatility_regimes.get(regime.value, 0) + 1
        )

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
    theoretical_price: Decimal
    delta: Decimal
    last_spot: Decimal
    last_time: datetime

    def quote(self, spot: Decimal, timestamp: datetime) -> Quote:
        minutes = max(
            Decimal(0),
            Decimal(str((timestamp - self.last_time).total_seconds())) / Decimal(60),
        )
        spot_change = spot - self.last_spot
        self.theoretical_price = max(
            Decimal("0.01"),
            self.theoretical_price
            + self.delta * spot_change
            + Decimal("0.5") * RULES.synthetic_gamma * spot_change * spot_change
            + RULES.synthetic_theta * minutes / Decimal(390),
        )
        self.delta += RULES.synthetic_gamma * spot_change
        if self.contract.right is Direction.CALL:
            self.delta = min(Decimal("0.95"), max(Decimal("0.05"), self.delta))
        else:
            self.delta = min(Decimal("-0.05"), max(Decimal("-0.95"), self.delta))
        self.last_spot = spot
        self.last_time = timestamp
        return Quote(
            symbol=self.contract.symbol,
            timestamp=timestamp,
            last=self.theoretical_price,
            bid=max(Decimal("0.01"), self.theoretical_price - RULES.slippage_quote),
            ask=self.theoretical_price + RULES.slippage_quote,
            volume=1000,
            open_interest=5000,
            extra={
                "delta": str(self.delta),
                "gamma": str(RULES.synthetic_gamma),
                "theta": str(RULES.synthetic_theta),
                "vega": str(RULES.synthetic_vega),
                "source": "synthetic",
            },
        )


@dataclass(slots=True)
class OpenTrade:
    position: Position
    quantity: int
    entry_at: datetime
    quote_source: str
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
        signal: Signal, trading_day: date
    ) -> SyntheticOption:
        strike = signal.spot.quantize(Decimal("1"))
        marker = "C" if signal.direction is Direction.CALL else "P"
        symbol = (
            f"QQQ{trading_day.strftime('%y%m%d')}{marker}"
            f"{int(strike * 1000):08d}.US"
        )
        contract = OptionContract(
            symbol, "QQQ.US", trading_day, strike, signal.direction
        )
        local = signal.bar_end.astimezone(NY_TZ)
        close = local.replace(hour=16, minute=0, second=0, microsecond=0)
        remaining_minutes = max(
            Decimal(0),
            Decimal(str((close - local).total_seconds())) / Decimal(60),
        )
        intrinsic = (
            max(Decimal(0), signal.spot - strike)
            if signal.direction is Direction.CALL
            else max(Decimal(0), strike - signal.spot)
        )
        initial = max(
            RULES.synthetic_min_price,
            intrinsic + abs(RULES.synthetic_theta) * remaining_minutes / Decimal(390),
        )
        return SyntheticOption(
            contract,
            initial,
            RULES.synthetic_call_delta
            if signal.direction is Direction.CALL
            else RULES.synthetic_put_delta,
            signal.spot,
            signal.bar_end,
        )

    @staticmethod
    def _frame_at(
        frames: dict[datetime, OptionFrame], timestamp: datetime
    ) -> OptionFrame | None:
        direct = frames.get(timestamp)
        if direct is not None:
            return direct
        candidates = [
            frame
            for key, frame in frames.items()
            if timestamp <= key
            and (key - timestamp).total_seconds() <= RULES.signal_ttl_seconds
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
    ) -> BacktestResult:
        result = BacktestResult(starting_equity, starting_equity)
        available = sorted(
            (bar for bar in (warmup_bars or []) if bar.complete),
            key=lambda item: item.end,
        )
        position: Position | None = None
        open_trade: OpenTrade | None = None
        synthetic: SyntheticOption | None = None
        last_real_quote: Quote | None = None
        realized = Decimal(0)
        current_day: date | None = None
        trades_today = 0
        cooldown_until: datetime | None = None
        last_processed: Bar | None = None
        cancelled = False

        def close_leg(decision: ExitDecision, quote: Quote, timestamp: datetime) -> None:
            nonlocal position, open_trade, synthetic, last_real_quote, realized, cooldown_until
            assert position is not None and open_trade is not None and quote.bid is not None
            quantity = min(decision.quantity, position.quantity)
            fees = RULES.fee_per_contract * Decimal(2) * quantity
            pnl = (
                (quote.bid - position.entry_price) * Decimal(100) * quantity - fees
            )
            slippage = (
                RULES.slippage_quote * Decimal(2) * Decimal(100) * quantity
                if open_trade.quote_source == "synthetic"
                else Decimal(0)
            )
            realized += pnl
            leg = BacktestExitLeg(
                timestamp,
                quote.bid,
                quantity,
                pnl,
                fees,
                slippage,
                decision.reason.value,
            )
            open_trade.legs.append(leg)
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
                    "indicators": {"pnl": str(pnl)},
                }
            )
            position.quantity -= quantity
            if decision.reason in {
                ExitReason.TAKE_PROFIT_1,
                ExitReason.BOLLINGER_MIDDLE,
            }:
                position.first_target_taken = True
                position.range_middle_taken |= (
                    decision.reason is ExitReason.BOLLINGER_MIDDLE
                )
            if decision.reason is ExitReason.MIDDAY_REDUCE:
                position.midday_reduced = True
            if decision.new_stop is not None:
                position.stop_price = decision.new_stop
            if position.quantity <= 0:
                result.trades.append(self._finalize_trade(open_trade))
                position = None
                open_trade = None
                synthetic = None
                last_real_quote = None
                cooldown_until = timestamp + timedelta(minutes=RULES.cooldown_minutes)

        ordered = sorted((bar for bar in bars if bar.complete), key=lambda item: item.end)
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
            available.append(bar)
            signal = self.strategy.evaluate(available)

            if position is not None:
                frame = self._frame_at(option_frames, bar.end)
                if synthetic is not None:
                    quote = synthetic.quote(bar.close, bar.end)
                elif frame is not None and position.symbol in frame.quotes:
                    quote = frame.quotes[position.symbol]
                    last_real_quote = quote
                elif last_real_quote is not None:
                    result.option_data_complete = False
                    continue
                else:
                    result.option_data_complete = False
                    continue
                if quote.bid is None:
                    continue
                price_decision = self.risk.exit_decision(
                    position, quote.bid, bar.end,
                )
                bar_decision = self.strategy.bar_exit_decision(position)
                decision = price_decision
                if (
                    price_decision is None
                    or price_decision.reason is not ExitReason.FORCED_CLOSE
                ):
                    decision = bar_decision or price_decision
                if decision is not None:
                    close_leg(decision, quote, bar.end)
                continue

            if (
                signal is None
                or (trade_start is not None and trading_day < trade_start)
            ):
                continue
            result.signals += 1
            if trades_today >= RULES.timed_max_trades_per_day:
                result.reject("max_trades_per_day")
                result.record_signal(signal, "rejected", "max_trades_per_day")
                continue
            if cooldown_until is not None and bar.end < cooldown_until:
                result.reject("cooldown")
                result.record_signal(signal, "rejected", "cooldown")
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
                synthetic = self._synthetic_option(signal, trading_day)
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
                result.record_signal(
                    signal, "rejected", "risk_budget_too_small", contract.symbol
                )
                synthetic = None
                continue
            entry_price = quote.ask
            result.record_signal(
                signal,
                "accepted",
                f"entry_{signal.strategy}",
                contract.symbol,
                entry_price,
                quantity,
            )
            execution_at = (
                quote.timestamp if quote_source == "real" else signal.bar_end
            )
            position = Position(
                contract.symbol,
                signal.direction,
                quantity,
                entry_price,
                execution_at,
                initial_quantity=quantity,
                stop_price=entry_price
                * (Decimal(1) - RULES.option_stop_loss_pct),
                strategy_name=signal.strategy,
                market_state=signal.market_state,
                entry_spot=signal.spot,
                highest_bid=entry_price,
                entry_vwap=signal.vwap,
            )
            open_trade = OpenTrade(position, quantity, execution_at, quote_source)
            last_real_quote = quote if quote_source == "real" else None
            trades_today += 1

        if position is not None and open_trade is not None and last_processed is not None:
            final_bar = last_processed
            quote = (
                synthetic.quote(final_bar.close, final_bar.end)
                if synthetic is not None
                else last_real_quote
            )
            if quote is not None and quote.bid is not None:
                close_leg(
                    ExitDecision(
                        ExitReason.SHUTDOWN if cancelled else ExitReason.FORCED_CLOSE,
                        position.quantity,
                    ),
                    quote,
                    final_bar.end,
                )
        result.ending_equity = starting_equity + realized
        return result
