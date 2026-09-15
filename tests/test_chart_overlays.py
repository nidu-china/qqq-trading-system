from datetime import datetime, timedelta, timezone
from decimal import Decimal

from qqq_trader.domain import Bar
from qqq_trader.indicators import overlay_series


def _session_bars(count: int) -> list[Bar]:
    base = datetime(2026, 9, 14, 13, 0, tzinfo=timezone.utc)
    bars: list[Bar] = []
    for index in range(count):
        start = base + timedelta(minutes=index)
        close = Decimal("500") + Decimal(index) * Decimal("0.05")
        bars.append(
            Bar(
                "QQQ.US",
                start,
                start + timedelta(minutes=1),
                close,
                close + Decimal("0.2"),
                close - Decimal("0.2"),
                close,
                1000,
            )
        )
    return bars


def test_overlay_series_has_indicators_by_open():
    points = overlay_series(
        _session_bars(40),
        ema_fast=9,
        ema_slow=21,
        boll_period=20,
        boll_std=Decimal("2"),
        macd_fast=8,
        macd_slow=17,
        macd_signal=9,
        timestamp="end",
    )
    rth_open = points[30]
    assert rth_open["ema9"]
    assert rth_open["ema21"]
    assert rth_open["boll_mid"]
    assert rth_open["macd_line"]
    assert rth_open["macd_signal"]
    assert rth_open["vwap"]
    assert rth_open["open"] == rth_open["price"]
