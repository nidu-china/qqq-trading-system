from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from qqq_trader.config import Settings
from qqq_trader.domain import Bar, Direction
from qqq_trader.volatility import VolatilityFilter, VolatilityRegime


def make_bar(end: datetime, close: str, duration_minutes: int = 5) -> Bar:
    value = Decimal(close)
    return Bar(
        symbol=".VIX.US",
        start=end - timedelta(minutes=duration_minutes),
        end=end,
        open=value,
        high=value,
        low=value,
        close=value,
        volume=0,
    )


def daily_history(decision_at: datetime) -> list[Bar]:
    result = []
    for index in range(20):
        start = decision_at - timedelta(days=30 - index)
        value = Decimal(10 + index)
        result.append(
            Bar(
                symbol=".VIX.US",
                start=start,
                end=start + timedelta(days=1),
                open=value,
                high=value,
                low=value,
                close=value,
                volume=0,
            )
        )
    return result


def snapshot(values: tuple[str, str, str]):
    decision_at = datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc)
    intraday = [
        make_bar(decision_at - timedelta(minutes=15), values[0]),
        make_bar(decision_at - timedelta(minutes=5), values[1]),
        make_bar(decision_at, values[2]),
    ]
    return VolatilityFilter(Settings()).evaluate(intraday, decision_at, daily_history(decision_at))


def test_risk_off_allows_only_put():
    result = snapshot(("33", "34", "35"))
    assert result.regime is VolatilityRegime.RISK_OFF
    assert result.allows(Direction.PUT)
    assert not result.allows(Direction.CALL)


def test_recovery_allows_only_call():
    result = snapshot(("27", "26", "25"))
    assert result.regime is VolatilityRegime.RECOVERY
    assert result.allows(Direction.CALL)
    assert not result.allows(Direction.PUT)


def test_shock_blocks_both_directions():
    result = snapshot(("34", "35", "40"))
    assert result.regime is VolatilityRegime.SHOCK
    assert not result.allows(Direction.CALL)
    assert not result.allows(Direction.PUT)


def test_missing_history_fails_closed():
    decision_at = datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc)
    result = VolatilityFilter(Settings()).evaluate([], decision_at)
    assert result.regime is VolatilityRegime.UNAVAILABLE
    assert result.reason == "missing_intraday_data"
