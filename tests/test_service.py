"""Live service loads QQQ/VIX bars into memory and does not persist them."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from conftest import make_settings
from qqq_trader.domain import Bar, SystemState
from qqq_trader.service import TradingService

ET = ZoneInfo("America/New_York")


def _bar(symbol: str, start: datetime) -> Bar:
    return Bar(
        symbol,
        start,
        start + timedelta(minutes=1),
        Decimal("700"),
        Decimal("701"),
        Decimal("699"),
        Decimal("700"),
        100,
    )


@pytest.mark.asyncio
async def test_live_warmup_loads_session_bars_without_writing():
    settings = make_settings()
    now_utc = datetime.now(timezone.utc)
    today = now_utc.astimezone(ET).date()
    keep = _bar("QQQ.US", datetime.combine(today, time(9, 0), ET))
    drop = _bar("QQQ.US", datetime.combine(today, time(8, 59), ET))

    async def historical_bars(symbol, start, end, period="1m", all_sessions=False):
        assert all_sessions is True
        assert start == today
        assert end == today
        return [drop, keep]

    engine = MagicMock()
    engine.settings = settings
    engine.state = SystemState.READY
    engine.market.historical_bars = historical_bars
    engine.journal.event = AsyncMock()

    store = MagicMock()
    store.write_bars.side_effect = AssertionError("live must not write bars")
    store.replace_bars.side_effect = AssertionError("live must not write bars")

    service = TradingService(settings, engine, store, MagicMock())
    await service._warm_strategy_history()

    starts = [
        bar.start.astimezone(ET).time().replace(tzinfo=None)
        for bar in service.strategy_warmup_bars
    ]
    assert time(8, 59) not in starts
    if keep.end <= now_utc:
        assert time(9, 0) in starts
    store.write_bars.assert_not_called()
    store.replace_bars.assert_not_called()
