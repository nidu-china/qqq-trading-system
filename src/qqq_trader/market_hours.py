from __future__ import annotations

from collections.abc import Iterable
from datetime import time

from .config import NY_TZ
from .domain import Bar

REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
INDICATOR_OPEN = time(9, 0)
VIX_SESSION_OPEN = time(4, 0)


def is_regular_session_bar(bar: Bar) -> bool:
    local_time = bar.start.astimezone(NY_TZ).time().replace(tzinfo=None)
    return REGULAR_OPEN <= local_time < REGULAR_CLOSE


def regular_session_bars(bars: Iterable[Bar]) -> list[Bar]:
    return [bar for bar in bars if is_regular_session_bar(bar)]


def is_indicator_session_bar(bar: Bar) -> bool:
    """QQQ premarket 09:00 plus RTH, excluding after-hours."""
    local_time = bar.start.astimezone(NY_TZ).time().replace(tzinfo=None)
    return INDICATOR_OPEN <= local_time < REGULAR_CLOSE


def indicator_session_bars(bars: Iterable[Bar]) -> list[Bar]:
    return [bar for bar in bars if is_indicator_session_bar(bar)]


def is_vix_session_bar(bar: Bar) -> bool:
    """VIX extended session from 04:00 ET through the RTH close."""
    local_time = bar.start.astimezone(NY_TZ).time().replace(tzinfo=None)
    return VIX_SESSION_OPEN <= local_time < REGULAR_CLOSE


def vix_session_bars(bars: Iterable[Bar]) -> list[Bar]:
    return [bar for bar in bars if is_vix_session_bar(bar)]
