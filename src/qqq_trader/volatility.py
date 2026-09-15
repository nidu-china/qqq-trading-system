from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from .config import NY_TZ, Settings
from .domain import Bar, Direction
from .market_hours import is_vix_session_bar


class VolatilityRegime(StrEnum):
    NORMAL = "normal"
    RISK_OFF = "risk_off"
    RECOVERY = "recovery"
    SHOCK = "shock"
    VIX_MACD_RISING = "vix_macd_rising"
    VIX_MACD_FALLING = "vix_macd_falling"
    UNAVAILABLE = "unavailable"


def is_one_minute_bar(bar: Bar) -> bool:
    return int((bar.end - bar.start).total_seconds() / 60) == 1


# Cover 04:00-16:00 ET (720 1-minute bars) so the open does not drop early-session VIX.
_VIX_MACD_LOOKBACK_BARS = 720


def classify_vix_one_minute_macd(
    bars: Sequence[Bar],
    decision_at: datetime,
    max_staleness_minutes: int,
    fast: int,
    slow: int,
    signal_period: int,
) -> tuple[VolatilityRegime, Decimal | None, str]:
    """Classify VIX 1-minute MACD without look-ahead.

    Histogram > 0 is an uptrend (block Call, allow Put).
    Histogram < 0 is a downtrend (block Put, allow Call).
    Uses only the current day's 04:00-16:00 ET bars.
    """
    from .indicators import macd_histogram

    decision_day = decision_at.astimezone(NY_TZ).date()
    visible: list[Bar] = []
    for bar in reversed(bars):
        if not bar.complete or bar.end > decision_at:
            continue
        if not is_one_minute_bar(bar) or not is_vix_session_bar(bar):
            continue
        if bar.start.astimezone(NY_TZ).date() != decision_day:
            continue
        visible.append(bar)
        if len(visible) >= _VIX_MACD_LOOKBACK_BARS:
            break
    visible.reverse()
    required = slow + signal_period - 1
    if len(visible) < required:
        return VolatilityRegime.UNAVAILABLE, None, "insufficient_intraday_history"
    current = visible[-1]
    if decision_at - current.end > timedelta(minutes=max_staleness_minutes):
        return VolatilityRegime.UNAVAILABLE, None, "stale_intraday_data"
    closes = [bar.close for bar in visible]
    _, _, histogram = macd_histogram(closes, fast, slow, signal_period)
    if histogram > 0:
        return VolatilityRegime.VIX_MACD_RISING, histogram, ""
    if histogram < 0:
        return VolatilityRegime.VIX_MACD_FALLING, histogram, ""
    return VolatilityRegime.NORMAL, histogram, ""


@dataclass(frozen=True, slots=True)
class VolatilitySnapshot:
    timestamp: datetime
    symbol: str
    value: Decimal | None
    regime: VolatilityRegime
    reason: str = ""
    macd_hist: Decimal | None = None
    block_vix_macd_rising: bool = True
    block_vix_macd_falling: bool = True

    def allows(self, direction: Direction) -> bool:
        if self.regime in {VolatilityRegime.NORMAL, VolatilityRegime.UNAVAILABLE}:
            return True
        if self.regime is VolatilityRegime.VIX_MACD_RISING:
            if not self.block_vix_macd_rising:
                return True
            return direction is Direction.PUT
        if self.regime is VolatilityRegime.VIX_MACD_FALLING:
            if not self.block_vix_macd_falling:
                return True
            return direction is Direction.CALL
        return False

    def as_dict(self) -> dict[str, str | None]:
        return {
            "symbol": self.symbol,
            "value": str(self.value) if self.value is not None else None,
            "macd_hist": str(self.macd_hist) if self.macd_hist is not None else None,
            "regime": self.regime.value,
            "reason": self.reason,
        }


class VolatilityFilter:
    """Classify volatility without using observations later than the decision time."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def evaluate(
        self,
        intraday_bars: Sequence[Bar],
        decision_at: datetime,
        daily_bars: Sequence[Bar] = (),
    ) -> VolatilitySnapshot:
        del daily_bars
        vix_bars = [
            bar
            for bar in intraday_bars
            if bar.symbol == self.settings.volatility_symbol
        ]
        if not any(
            bar.complete and bar.end <= decision_at and is_one_minute_bar(bar)
            for bar in vix_bars
        ):
            return self._unavailable(decision_at, "missing_intraday_data")
        regime, histogram, reason = classify_vix_one_minute_macd(
            vix_bars,
            decision_at,
            self.settings.volatility_max_staleness_minutes,
            int(self.settings.timed_macd_fast),
            int(self.settings.timed_macd_slow),
            int(self.settings.timed_macd_signal),
        )
        visible = [
            bar
            for bar in vix_bars
            if bar.complete and bar.end <= decision_at and is_one_minute_bar(bar)
        ]
        value = max(visible, key=lambda bar: bar.end).close if visible else None
        return VolatilitySnapshot(
            timestamp=decision_at,
            symbol=self.settings.volatility_symbol,
            value=value,
            regime=regime,
            reason=reason,
            macd_hist=histogram,
            block_vix_macd_rising=self.settings.volatility_vix_macd_rising_block,
            block_vix_macd_falling=True,
        )

    def _unavailable(self, timestamp: datetime, reason: str) -> VolatilitySnapshot:
        return VolatilitySnapshot(
            timestamp=timestamp,
            symbol=self.settings.volatility_symbol,
            value=None,
            regime=VolatilityRegime.UNAVAILABLE,
            reason=reason,
            macd_hist=None,
        )
