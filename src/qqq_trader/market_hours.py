from __future__ import annotations

from collections.abc import Iterable
from datetime import time

from .config import NY_TZ
from .domain import Bar

REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)


def is_regular_session_bar(bar: Bar) -> bool:
    local_time = bar.start.astimezone(NY_TZ).time().replace(tzinfo=None)
    return REGULAR_OPEN <= local_time < REGULAR_CLOSE


def regular_session_bars(bars: Iterable[Bar]) -> list[Bar]:
    return [bar for bar in bars if is_regular_session_bar(bar)]
