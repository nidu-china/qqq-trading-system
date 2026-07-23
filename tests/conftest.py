from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from qqq_trader.domain import Bar


@pytest.fixture
def bullish_bars() -> list[Bar]:
    """Completed QQQ 1-min bars that trigger the new StrategyEngine.

    50 bars spanning 9:30-10:20 ET (13:30-14:20 UTC).
    Pattern:
    - Bars 0-14 (9:30-9:45): ORB observation phase, range ~100-100.5
    - Bars 15-29 (9:45-10:00): breakout above ORH with volume (triggers breakout detection)
    - Bars 30-44 (10:00-10:15): pullback toward ORH/VWAP then bullish close (triggers entry)
    - Bars 45-49 (10:15-10:20): continuation
    """
    start = datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc)  # 9:30 ET
    bars: list[Bar] = []

    # Phase 1: ORB observation (9:30-9:45) - range bound 99.5-100.5
    for i in range(15):
        base = Decimal("100") + Decimal(str(i * 0.02))
        bars.append(Bar(
            symbol="QQQ.US",
            start=start + timedelta(minutes=i),
            end=start + timedelta(minutes=i + 1),
            open=base,
            high=base + Decimal("0.3"),
            low=base - Decimal("0.2"),
            close=base + Decimal("0.1"),
            volume=1000,
        ))

    # Phase 2: Breakout (9:45-10:00) - strong move above 100.5 (ORH)
    for i in range(15):
        idx = 15 + i
        base = Decimal("100.5") + Decimal(str(i * 0.15))
        bars.append(Bar(
            symbol="QQQ.US",
            start=start + timedelta(minutes=idx),
            end=start + timedelta(minutes=idx + 1),
            open=base,
            high=base + Decimal("0.3"),
            low=base - Decimal("0.1"),
            close=base + Decimal("0.2"),
            volume=2000,
        ))

    # Phase 3: Pullback + re-entry (10:00-10:15)
    pullback_start = Decimal("102.9")
    for i in range(15):
        idx = 30 + i
        if i < 5:
            # Pullback toward VWAP/ORH area
            base = pullback_start - Decimal(str(i * 0.3))
        elif i < 10:
            # Stabilize near support
            base = Decimal("101.5") + Decimal(str((i - 5) * 0.05))
        else:
            # Re-entry (bullish close above ORH)
            base = Decimal("101.7") + Decimal(str((i - 10) * 0.2))
        bars.append(Bar(
            symbol="QQQ.US",
            start=start + timedelta(minutes=idx),
            end=start + timedelta(minutes=idx + 1),
            open=base - Decimal("0.05"),
            high=base + Decimal("0.2"),
            low=base - Decimal("0.15"),
            close=base + Decimal("0.1"),
            volume=1500,
        ))

    # Phase 4: Continuation (10:15-10:20)
    for i in range(5):
        idx = 45 + i
        base = Decimal("102.5") + Decimal(str(i * 0.1))
        bars.append(Bar(
            symbol="QQQ.US",
            start=start + timedelta(minutes=idx),
            end=start + timedelta(minutes=idx + 1),
            open=base,
            high=base + Decimal("0.2"),
            low=base - Decimal("0.1"),
            close=base + Decimal("0.15"),
            volume=1200,
        ))

    return bars
