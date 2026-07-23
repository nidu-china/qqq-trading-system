from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from qqq_trader.domain import Bar


@pytest.fixture
def bullish_bars() -> list[Bar]:
    """Completed QQQ 1m bars ending in a valid bullish EMA pullback signal.
    
    40 bars spanning 9:30-10:10 ET (13:30-14:10 UTC).
    Pattern: uptrend with pullback and reversal in the EMA trend window.
    """
    start = datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc)
    closes = [
        Decimal("100")
        + Decimal(str((index % 4) - 1.5)) * Decimal("0.25")
        + Decimal(index) * Decimal("0.015")
        for index in range(30)
    ]
    closes[-1] = Decimal("101.5")
    return [
        Bar(
            symbol="QQQ.US",
            start=start + timedelta(minutes=index),
            end=start + timedelta(minutes=index + 1),
            open=close - Decimal("0.05"),
            high=close + Decimal("0.08"),
            low=close - Decimal("0.08"),
            close=close,
            volume=2500 if index == len(closes) - 1 else 1000,
        )
        for index, close in enumerate(closes)
    ]
