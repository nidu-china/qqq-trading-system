from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from .config import NY_TZ, Settings
from .domain import Bar, Direction


class VolatilityRegime(StrEnum):
    NORMAL = "normal"
    RISK_OFF = "risk_off"
    RECOVERY = "recovery"
    SHOCK = "shock"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class VolatilitySnapshot:
    timestamp: datetime
    symbol: str
    value: Decimal | None
    percentile: Decimal | None
    change_5m: Decimal | None
    change_15m: Decimal | None
    regime: VolatilityRegime
    reason: str = ""

    def allows(self, direction: Direction) -> bool:
        if self.regime is VolatilityRegime.NORMAL:
            return True
        if self.regime is VolatilityRegime.RISK_OFF:
            return direction is Direction.PUT
        if self.regime is VolatilityRegime.RECOVERY:
            return direction is Direction.CALL
        return False

    def as_dict(self) -> dict[str, str | None]:
        return {
            "symbol": self.symbol,
            "value": str(self.value) if self.value is not None else None,
            "percentile": str(self.percentile) if self.percentile is not None else None,
            "change_5m": str(self.change_5m) if self.change_5m is not None else None,
            "change_15m": str(self.change_15m) if self.change_15m is not None else None,
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
        visible = sorted(
            (bar for bar in intraday_bars if bar.complete and bar.end <= decision_at),
            key=lambda bar: bar.end,
        )
        if not visible:
            return self._unavailable(decision_at, "missing_intraday_data")
        current = visible[-1]
        if decision_at - current.end > timedelta(
            minutes=self.settings.volatility_max_staleness_minutes
        ):
            return self._unavailable(decision_at, "stale_intraday_data")

        five_ago = self._asof(visible, decision_at - timedelta(minutes=5))
        fifteen_ago = self._asof(visible, decision_at - timedelta(minutes=15))
        if five_ago is None or fifteen_ago is None:
            return self._unavailable(decision_at, "insufficient_intraday_history")

        previous_closes = self._previous_session_closes([*daily_bars, *intraday_bars], decision_at)
        if len(previous_closes) < self.settings.volatility_lookback_days:
            return self._unavailable(decision_at, "insufficient_daily_history")
        previous_closes = previous_closes[-self.settings.volatility_lookback_days :]

        value = current.close
        change_5m = value / five_ago.close - Decimal(1)
        change_15m = value / fifteen_ago.close - Decimal(1)
        percentile = Decimal(sum(item <= value for item in previous_closes)) / Decimal(
            len(previous_closes)
        )

        if (
            change_5m >= self.settings.volatility_shock_5m
            or change_15m >= self.settings.volatility_shock_15m
        ):
            regime = VolatilityRegime.SHOCK
        elif percentile >= self.settings.volatility_risk_off_percentile and (
            change_5m >= self.settings.volatility_rise_5m
            or change_15m >= self.settings.volatility_rise_15m
        ):
            regime = VolatilityRegime.RISK_OFF
        elif percentile >= self.settings.volatility_recovery_percentile and (
            change_5m <= self.settings.volatility_fall_5m
            or change_15m <= self.settings.volatility_fall_15m
        ):
            regime = VolatilityRegime.RECOVERY
        else:
            regime = VolatilityRegime.NORMAL

        return VolatilitySnapshot(
            timestamp=current.end,
            symbol=self.settings.volatility_symbol,
            value=value,
            percentile=percentile,
            change_5m=change_5m,
            change_15m=change_15m,
            regime=regime,
        )

    @staticmethod
    def _asof(bars: Sequence[Bar], target: datetime) -> Bar | None:
        result = None
        for bar in bars:
            if bar.end > target:
                break
            result = bar
        return result

    def _previous_session_closes(self, bars: Sequence[Bar], decision_at: datetime) -> list[Decimal]:
        decision_date = decision_at.astimezone(NY_TZ).date()
        closes: dict[object, tuple[datetime, Decimal]] = {}
        for bar in bars:
            if not bar.complete or bar.end > decision_at:
                continue
            duration = bar.end - bar.start
            session_timestamp = bar.start if duration >= timedelta(hours=12) else bar.end
            session_date = session_timestamp.astimezone(NY_TZ).date()
            if session_date >= decision_date:
                continue
            previous = closes.get(session_date)
            if previous is None or bar.end > previous[0]:
                closes[session_date] = (bar.end, bar.close)
        return [closes[key][1] for key in sorted(closes)]

    def _unavailable(self, timestamp: datetime, reason: str) -> VolatilitySnapshot:
        return VolatilitySnapshot(
            timestamp=timestamp,
            symbol=self.settings.volatility_symbol,
            value=None,
            percentile=None,
            change_5m=None,
            change_15m=None,
            regime=VolatilityRegime.UNAVAILABLE,
            reason=reason,
        )
