from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from conftest import make_settings
from qqq_trader.domain import Bar, Direction
from qqq_trader.market_hours import vix_session_bars
from qqq_trader.volatility import VolatilityFilter, VolatilityRegime

NY = ZoneInfo("America/New_York")


def make_bar(end: datetime, close: str, duration_minutes: int = 1) -> Bar:
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


def one_minute_series(decision_at: datetime, closes: list[Decimal]) -> list[Bar]:
    count = len(closes)
    return [
        make_bar(decision_at - timedelta(minutes=count - 1 - index), str(close))
        for index, close in enumerate(closes)
    ]


def snapshot_from_closes(closes: list[Decimal]):
    decision_at = datetime(2026, 7, 15, 11, 0, tzinfo=NY)
    return VolatilityFilter(make_settings()).evaluate(
        one_minute_series(decision_at, closes),
        decision_at,
    )


def test_vix_macd_rising_blocks_call_allows_put():
    closes = [Decimal("15")] * 20 + [
        Decimal("15") + Decimal(index) * Decimal("0.25") for index in range(25)
    ]
    result = snapshot_from_closes(closes)
    assert result.regime is VolatilityRegime.VIX_MACD_RISING
    assert result.macd_hist is not None and result.macd_hist > 0
    assert result.allows(Direction.PUT)
    assert not result.allows(Direction.CALL)


def test_vix_macd_rising_block_can_be_disabled():
    closes = [Decimal("15")] * 20 + [
        Decimal("15") + Decimal(index) * Decimal("0.25") for index in range(25)
    ]
    settings = make_settings(volatility_vix_macd_rising_block=False)
    decision_at = datetime(2026, 7, 15, 11, 0, tzinfo=NY)
    result = VolatilityFilter(settings).evaluate(
        one_minute_series(decision_at, closes),
        decision_at,
    )
    assert result.regime is VolatilityRegime.VIX_MACD_RISING
    assert result.allows(Direction.CALL)
    assert result.allows(Direction.PUT)


def test_vix_macd_falling_blocks_put_allows_call():
    closes = [Decimal("20")] * 20 + [
        Decimal("20") - Decimal(index) * Decimal("0.25") for index in range(25)
    ]
    result = snapshot_from_closes(closes)
    assert result.regime is VolatilityRegime.VIX_MACD_FALLING
    assert result.macd_hist is not None and result.macd_hist < 0
    assert result.allows(Direction.CALL)
    assert not result.allows(Direction.PUT)


def test_missing_history_fails_closed():
    decision_at = datetime(2026, 7, 15, 11, 0, tzinfo=NY)
    result = VolatilityFilter(make_settings()).evaluate([], decision_at)
    assert result.regime is VolatilityRegime.UNAVAILABLE
    assert result.reason == "missing_intraday_data"
    assert result.allows(Direction.CALL)
    assert result.allows(Direction.PUT)


def test_short_one_minute_history_is_unavailable():
    decision_at = datetime(2026, 7, 15, 11, 0, tzinfo=NY)
    closes = [Decimal("16") for _ in range(10)]
    result = VolatilityFilter(make_settings()).evaluate(
        one_minute_series(decision_at, closes),
        decision_at,
    )
    assert result.regime is VolatilityRegime.UNAVAILABLE
    assert result.reason == "insufficient_intraday_history"
    assert result.allows(Direction.CALL)
    assert result.allows(Direction.PUT)


def test_vix_macd_includes_early_session_at_open():
    """09:40 only has 10 RTH minutes; MACD(8,17,9) needs early-session bars from 04:00."""
    decision_at = datetime(2026, 7, 15, 9, 40, tzinfo=NY)
    closes = [Decimal("15")] * 15 + [
        Decimal("15") + Decimal(index) * Decimal("0.25") for index in range(25)
    ]
    result = VolatilityFilter(make_settings()).evaluate(
        one_minute_series(decision_at, closes),
        decision_at,
    )
    assert result.regime is VolatilityRegime.VIX_MACD_RISING
    assert result.allows(Direction.PUT)
    assert not result.allows(Direction.CALL)


def test_vix_macd_keeps_four_am_session_and_drops_before():
    decision_at = datetime(2026, 7, 15, 11, 0, tzinfo=NY)
    session = one_minute_series(
        decision_at,
        [Decimal("15")] * 20 + [
            Decimal("15") + Decimal(index) * Decimal("0.25") for index in range(25)
        ],
    )
    kept = make_bar(datetime(2026, 7, 15, 4, 1, tzinfo=NY), "14")
    dropped = make_bar(datetime(2026, 7, 15, 3, 59, tzinfo=NY), "40")
    assert vix_session_bars([dropped, kept]) == [kept]
    result = VolatilityFilter(make_settings()).evaluate(
        [dropped, kept, *session],
        decision_at,
    )
    assert result.regime is VolatilityRegime.VIX_MACD_RISING
