from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pyarrow.parquet as pq

from .config import Settings
from .domain import AccountSnapshot, Bar, Direction, OptionContract, Position, Quote
from .risk import ContractSelector, RiskEngine
from .strategy import TimeBasedStrategyRouter
from .volatility import VolatilityFilter, VolatilityRegime


def _round_strike(value: Decimal) -> Decimal:
    """Round to the nearest whole dollar (QQQ standard strike interval)."""
    return value.quantize(Decimal(1))


@dataclass(frozen=True, slots=True)
class OptionFrame:
    timestamp: datetime
    spot: Decimal
    contracts: tuple[OptionContract, ...]
    quotes: dict[str, Quote]


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
    reason: str


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
        self.volatility_regimes[regime.value] = self.volatility_regimes.get(regime.value, 0) + 1

    def record_signal(
        self,
        signal,
        status: str,
        reason: str,
        *,
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


class EventDrivenBacktester:
    """Replay completed bars and executable option quotes without look-ahead."""

    settings_timezone = __import__("zoneinfo").ZoneInfo("America/New_York")

    def __init__(
        self,
        settings: Settings,
        strategy: TimeBasedStrategyRouter,
        selector: ContractSelector,
        risk: RiskEngine,
    ) -> None:
        self.settings = settings
        self.strategy = strategy
        self.selector = selector
        self.risk = risk
        self.volatility_filter = VolatilityFilter(settings)

    def _synthetic_frame(
        self, spot: Decimal, bar_end: datetime, trading_day: date
    ) -> OptionFrame:
        """Generate a synthetic OptionFrame using Greeks-based pricing.
        
        Model: Delta=0.45, Gamma=0.05, Theta=-3/day, Vega=0.10
        For 0DTE options, theta decays entirely within the trading session (6.5h).
        """
        offset = self.settings.strike_offset
        call_strike = _round_strike(spot + offset)
        put_strike = _round_strike(spot - offset)

        market_close = bar_end.astimezone(self.settings_timezone).replace(
            hour=16, minute=0, second=0, microsecond=0
        )
        hours_left = max(
            Decimal(str((market_close - bar_end.astimezone(self.settings_timezone)).total_seconds()))
            / Decimal(3600),
            Decimal("0.1"),
        )
        half_spread = Decimal("0.03")

        contracts = []
        quotes: dict[str, Quote] = {}
        for direction, strike in (
            (Direction.CALL, call_strike),
            (Direction.PUT, put_strike),
        ):
            symbol = (
                f"QQQ{trading_day.strftime('%y%m%d')}"
                f"{'C' if direction is Direction.CALL else 'P'}"
                f"{int(strike * 1000):08d}.US"
            )
            mid = self._greeks_price(spot, strike, direction, hours_left)
            contracts.append(
                OptionContract(
                    symbol=symbol,
                    underlying="QQQ.US",
                    expiry=trading_day,
                    strike=strike,
                    right=direction,
                )
            )
            quotes[symbol] = Quote(
                symbol=symbol,
                timestamp=bar_end,
                last=mid,
                bid=mid - half_spread,
                ask=mid + half_spread,
                volume=500,
                open_interest=5000,
            )
        return OptionFrame(
            timestamp=bar_end,
            spot=spot,
            contracts=tuple(contracts),
            quotes=quotes,
        )

    def _greeks_price(
        self,
        spot: Decimal,
        strike: Decimal,
        direction: Direction,
        hours_left: Decimal,
    ) -> Decimal:
        """Calculate synthetic option price using fixed Greeks.
        
        Delta=0.45, Gamma=0.05, Theta=-$3/day, Vega=0.10 (unused without IV).
        """
        delta = Decimal("0.45")
        gamma = Decimal("0.05")
        theta_daily = Decimal("3")
        trading_hours = Decimal("6.5")

        if direction is Direction.CALL:
            intrinsic = max(spot - strike, Decimal(0))
            distance = spot - strike
        else:
            intrinsic = max(strike - spot, Decimal(0))
            distance = strike - spot

        time_value = theta_daily * hours_left / trading_hours

        abs_distance = abs(distance)
        if abs_distance > Decimal(0):
            moneyness_decay = max(Decimal("0.05"), Decimal(1) - abs_distance / Decimal(8))
            time_value *= moneyness_decay

        extrinsic_delta = Decimal(0)
        if distance > Decimal(0):
            extrinsic_delta = (delta - Decimal("0.5")) * distance + Decimal("0.5") * gamma * distance * distance
            extrinsic_delta = max(Decimal(0), extrinsic_delta)

        mid = intrinsic + time_value + extrinsic_delta
        return max(mid, Decimal("0.05"))

    def _synthetic_position_quote(
        self, position: Position, spot: Decimal, bar_end: datetime
    ) -> Quote:
        """Compute a simulated option quote for an existing position using Greeks."""
        market_close = bar_end.astimezone(self.settings_timezone).replace(
            hour=16, minute=0, second=0, microsecond=0
        )
        hours_left = max(
            Decimal(str((market_close - bar_end.astimezone(self.settings_timezone)).total_seconds()))
            / Decimal(3600),
            Decimal("0.1"),
        )
        half_spread = Decimal("0.03")

        parts = position.symbol.split(".")[0]
        if "C" in parts[-10:]:
            idx = parts.rindex("C")
            strike = Decimal(parts[idx + 1:]) / Decimal(1000)
            direction = Direction.CALL
        elif "P" in parts[-10:]:
            idx = parts.rindex("P")
            strike = Decimal(parts[idx + 1:]) / Decimal(1000)
            direction = Direction.PUT
        else:
            strike = spot
            direction = Direction.CALL

        mid = self._greeks_price(spot, strike, direction, hours_left)
        return Quote(
            symbol=position.symbol,
            timestamp=bar_end,
            last=mid,
            bid=mid - half_spread,
            ask=mid + half_spread,
            volume=500,
            open_interest=5000,
        )

    def run(
        self,
        bars: list[Bar],
        option_frames: dict[datetime, OptionFrame],
        starting_equity: Decimal,
        volatility_bars: list[Bar] | None = None,
        volatility_daily_bars: list[Bar] | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> BacktestResult:
        result = BacktestResult(starting_equity, starting_equity)
        available: list[Bar] = []
        position: Position | None = None
        entry_time: datetime | None = None
        realized = Decimal(0)
        day_realized = Decimal(0)
        trades_today = 0
        current_day = None
        day_opening_equity = starting_equity
        cooldown_until: datetime | None = None

        for bar in sorted((item for item in bars if item.complete), key=lambda item: item.end):
            if cancel_check is not None and cancel_check():
                break
            trading_day = bar.end.astimezone(self.settings_timezone).date()
            if trading_day != current_day:
                current_day = trading_day
                trades_today = 0
                day_realized = Decimal(0)
                day_opening_equity = starting_equity + realized
            available.append(bar)
            frame = option_frames.get(bar.end)
            if frame is None:
                minute = bar.end.minute - (bar.end.minute % 5)
                bucket_end = bar.end.replace(minute=minute, second=0, microsecond=0)
                frame = option_frames.get(bucket_end)

            if position is not None:
                if frame is not None and position.symbol in frame.quotes:
                    quote = frame.quotes[position.symbol]
                else:
                    result.option_data_complete = False
                    quote = self._synthetic_position_quote(
                        position, bar.close, bar.end
                    )
                if quote.bid is None:
                    result.option_data_complete = False
                    continue
                account = AccountSnapshot(
                    timestamp=bar.end,
                    equity=starting_equity + realized,
                    cash_usd=starting_equity + realized,
                    day_realized_pnl=day_realized,
                )
                decision = self.risk.exit_decision(
                    position,
                    quote.bid,
                    bar.end,
                    self.risk.daily_loss_breached(account, day_opening_equity),
                )
                if decision:
                    pnl = (quote.bid - position.entry_price) * Decimal(100) * decision.quantity
                    pnl -= self.settings.fee_per_contract * decision.quantity
                    realized += pnl
                    day_realized += pnl
                    result.trades.append(
                        BacktestTrade(
                            symbol=position.symbol,
                            direction=position.direction,
                            quantity=decision.quantity,
                            entry_at=entry_time or position.opened_at,
                            entry_price=position.entry_price,
                            exit_at=bar.end,
                            exit_price=quote.bid,
                            pnl=pnl,
                            reason=decision.reason.value,
                        )
                    )
                    result.signal_records.append(
                        {
                            "id": f"sell:{len(result.trades)}:{bar.end.isoformat()}",
                            "action": "sell",
                            "decision_at": bar.end.isoformat(),
                            "direction": position.direction.value,
                            "symbol": position.symbol,
                            "price": str(quote.bid),
                            "quantity": decision.quantity,
                            "status": "executed",
                            "reason": decision.reason.value,
                            "indicators": {"pnl": str(pnl)},
                        }
                    )
                    position.quantity -= decision.quantity
                    if position.quantity == 0:
                        position = None
                        cooldown_until = bar.end + timedelta(minutes=self.settings.cooldown_minutes)
                    else:
                        position.first_target_taken = True
                        position.stop_price = decision.new_stop
                continue

            local_time = bar.end.astimezone(self.settings_timezone).time().replace(tzinfo=None)
            if not self.settings.entry_start <= local_time <= self.settings.entry_end:
                continue
            if trades_today >= self.settings.max_trades_per_day:
                continue
            if cooldown_until and bar.end < cooldown_until:
                continue
            signal = self.strategy.evaluate(available, spot=frame.spot if frame else None)
            if signal is None:
                continue
            result.signals += 1
            if self.settings.volatility_filter_enabled:
                snapshot = self.volatility_filter.evaluate(
                    volatility_bars or [],
                    bar.end,
                    volatility_daily_bars or [],
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
            if frame is None:
                frame = self._synthetic_frame(bar.close, bar.end, trading_day)
                result.option_data_complete = False
            contract = self.selector.select(frame.contracts, signal.direction, frame.spot)
            if contract is None:
                result.reject("missing_contract")
                result.record_signal(signal, "rejected", "missing_contract")
                continue
            quote = frame.quotes.get(contract.symbol)
            if quote is None:
                result.reject("missing_option_quote")
                result.record_signal(signal, "rejected", "missing_option_quote")
                result.option_data_complete = False
                continue
            problem = self.risk.quote_problem(quote, bar.end)
            if problem:
                result.reject(problem)
                result.record_signal(signal, "rejected", problem, symbol=contract.symbol)
                continue
            assert quote.ask is not None
            account = AccountSnapshot(
                timestamp=bar.end,
                equity=starting_equity + realized,
                cash_usd=starting_equity + realized,
                day_realized_pnl=day_realized,
            )
            if self.risk.daily_loss_breached(account, day_opening_equity):
                result.reject("daily_loss_limit")
                result.record_signal(signal, "rejected", "daily_loss_limit", symbol=contract.symbol)
                continue
            quantity = self.risk.position_size(account, quote.ask)
            if quantity < 1:
                result.reject("risk_budget_too_small")
                result.record_signal(
                    signal, "rejected", "risk_budget_too_small", symbol=contract.symbol
                )
                continue
            result.record_signal(
                signal,
                "accepted",
                f"entry_{signal.direction.value}",
                symbol=contract.symbol,
                price=quote.ask,
                quantity=quantity,
            )
            position = Position(
                symbol=contract.symbol,
                direction=signal.direction,
                quantity=quantity,
                entry_price=quote.ask,
                opened_at=bar.end,
            )
            entry_time = bar.end
            trades_today += 1

        if position is not None:
            last_bar = bars[-1] if bars else None
            last_frame = option_frames.get(last_bar.end) if last_bar else None
            if last_frame and position.symbol in last_frame.quotes:
                closing_quote = last_frame.quotes[position.symbol]
                close_price = closing_quote.bid if closing_quote.bid is not None else Decimal(0)
            else:
                close_price = Decimal(0)
            pnl = (close_price - position.entry_price) * Decimal(100) * position.quantity
            pnl -= self.settings.fee_per_contract * position.quantity
            realized += pnl
            result.trades.append(
                BacktestTrade(
                    symbol=position.symbol,
                    direction=position.direction,
                    quantity=position.quantity,
                    entry_at=entry_time or position.opened_at,
                    entry_price=position.entry_price,
                    exit_at=last_bar.end if last_bar else position.opened_at,
                    exit_price=close_price,
                    pnl=pnl,
                    reason="backtest_end",
                )
            )

        result.ending_equity = starting_equity + realized
        return result

    @property
    def settings_timezone(self):
        from .config import NY_TZ

        return NY_TZ


def load_option_frames(path: Path) -> dict[datetime, OptionFrame]:
    """Load self-contained candidate option snapshots captured by TradingService."""
    rows = pq.ParquetFile(path).read().to_pylist()
    grouped: dict[datetime, list[dict]] = {}
    for row in rows:
        key = row.get("bar_end") or row["captured_at"]
        if isinstance(key, str):
            key = datetime.fromisoformat(key)
        grouped.setdefault(key, []).append(row)

    frames: dict[datetime, OptionFrame] = {}
    for timestamp, items in grouped.items():
        earliest_items: dict[str, dict] = {}
        for item in items:
            previous = earliest_items.get(item["symbol"])
            if previous is None or str(item["captured_at"]) < str(previous["captured_at"]):
                earliest_items[item["symbol"]] = item
        items = list(earliest_items.values())
        contracts: list[OptionContract] = []
        quotes: dict[str, Quote] = {}
        for row in items:
            expiry = row["expiry"]
            if isinstance(expiry, str):
                expiry = date.fromisoformat(expiry)
            contract = OptionContract(
                symbol=row["symbol"],
                underlying=row.get("underlying", "QQQ.US"),
                expiry=expiry,
                strike=Decimal(row["strike"]),
                right=Direction(row["direction"]),
            )
            contracts.append(contract)
            captured_at = row["captured_at"]
            if isinstance(captured_at, str):
                captured_at = datetime.fromisoformat(captured_at)
            quotes[contract.symbol] = Quote(
                symbol=contract.symbol,
                timestamp=captured_at,
                last=Decimal(row["last"]),
                bid=Decimal(row["bid"]) if row.get("bid") else None,
                ask=Decimal(row["ask"]) if row.get("ask") else None,
                volume=int(row.get("volume", 0)),
                open_interest=int(row.get("open_interest", 0)),
            )
        frames[timestamp] = OptionFrame(
            timestamp=timestamp,
            spot=Decimal(items[0]["spot"]),
            contracts=tuple(contracts),
            quotes=quotes,
        )
    return frames


def load_option_frames_path(path: Path) -> dict[datetime, OptionFrame]:
    """Load one capture file or merge partitioned candidate-option captures."""
    if path.is_file():
        return load_option_frames(path)
    frames: dict[datetime, OptionFrame] = {}
    for candidate in sorted(path.rglob("data.parquet")):
        frames.update(load_option_frames(candidate))
    return frames
