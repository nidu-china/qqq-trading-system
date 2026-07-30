"""Technical indicators and one-minute market-data helpers."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, time
from decimal import Decimal

from .domain import Bar, MarketState

ZERO = Decimal(0)


def ema_series(values: Sequence[Decimal], period: int) -> list[Decimal]:
    """Return seeded EMA values aligned from source index period - 1."""
    if period < 1 or len(values) < period:
        raise ValueError(f"at least {period} values are required")
    multiplier = Decimal(2) / Decimal(period + 1)
    current = sum(values[:period], ZERO) / Decimal(period)
    result = [current]
    for value in values[period:]:
        current = (value - current) * multiplier + current
        result.append(current)
    return result


def ema(values: Sequence[Decimal], period: int) -> Decimal:
    return ema_series(values, period)[-1]


def macd_histogram(
    values: Sequence[Decimal],
    fast: int = 12,
    slow: int = 26,
    signal_period: int = 9,
) -> tuple[Decimal, Decimal, Decimal]:
    """Return (MACD line, signal line, histogram)."""
    if not 0 < fast < slow:
        raise ValueError("MACD periods must satisfy 0 < fast < slow")
    required = slow + signal_period - 1
    if len(values) < required:
        raise ValueError(f"at least {required} values are required")
    fast_values = ema_series(values, fast)
    slow_values = ema_series(values, slow)
    offset = slow - fast
    macd_values = [
        fast_value - slow_value
        for fast_value, slow_value in zip(
            fast_values[offset:],
            slow_values,
            strict=True,
        )
    ]
    signal_value = ema(macd_values, signal_period)
    macd_line = macd_values[-1]
    return macd_line, signal_value, macd_line - signal_value


def macd_histogram_series(
    values: Sequence[Decimal],
    fast: int = 12,
    slow: int = 26,
    signal_period: int = 9,
) -> list[Decimal]:
    """Return the aligned MACD histogram series."""
    if not 0 < fast < slow:
        raise ValueError("MACD periods must satisfy 0 < fast < slow")
    required = slow + signal_period - 1
    if len(values) < required:
        raise ValueError(f"at least {required} values are required")
    fast_values = ema_series(values, fast)
    slow_values = ema_series(values, slow)
    offset = slow - fast
    macd_values = [
        fast_value - slow_value
        for fast_value, slow_value in zip(
            fast_values[offset:],
            slow_values,
            strict=True,
        )
    ]
    signal_values = ema_series(macd_values, signal_period)
    signal_offset = signal_period - 1
    return [
        macd_value - signal_value
        for macd_value, signal_value in zip(
            macd_values[signal_offset:],
            signal_values,
            strict=True,
        )
    ]


def vwap(bars: Sequence[Bar]) -> Decimal:
    """Return volume-weighted typical price."""
    total_volume = sum(bar.volume for bar in bars)
    if total_volume == 0:
        return bars[-1].close if bars else ZERO
    weighted = sum(
        (bar.high + bar.low + bar.close)
        / Decimal(3)
        * Decimal(bar.volume)
        for bar in bars
    )
    return weighted / Decimal(total_volume)


def rsi(values: Sequence[Decimal], period: int = 14) -> Decimal:
    """Return Wilder RSI."""
    if period < 1 or len(values) < period + 1:
        return Decimal("50")
    gains: list[Decimal] = []
    losses: list[Decimal] = []
    for previous, current in zip(values, values[1:], strict=False):
        change = current - previous
        gains.append(max(change, ZERO))
        losses.append(max(-change, ZERO))
    average_gain = sum(gains[:period], ZERO) / Decimal(period)
    average_loss = sum(losses[:period], ZERO) / Decimal(period)
    for gain, loss in zip(gains[period:], losses[period:], strict=True):
        average_gain = (
            average_gain * Decimal(period - 1) + gain
        ) / Decimal(period)
        average_loss = (
            average_loss * Decimal(period - 1) + loss
        ) / Decimal(period)
    if average_loss == ZERO:
        return Decimal("100") if average_gain > ZERO else Decimal("50")
    relative_strength = average_gain / average_loss
    return Decimal("100") - Decimal("100") / (
        Decimal(1) + relative_strength
    )


def bollinger_bands(
    values: Sequence[Decimal],
    period: int = 20,
    std_dev: Decimal = Decimal("2"),
) -> tuple[Decimal, Decimal, Decimal]:
    """Return (upper, middle, lower) Bollinger bands."""
    if period < 2 or len(values) < period:
        middle = values[-1] if values else ZERO
        return middle, middle, middle
    window = values[-period:]
    middle = sum(window, ZERO) / Decimal(period)
    variance = sum(
        (value - middle) ** 2 for value in window
    ) / Decimal(period)
    deviation = variance.sqrt()
    return (
        middle + std_dev * deviation,
        middle,
        middle - std_dev * deviation,
    )


class BarAggregator:
    """Deterministically derive completed five-minute bars."""

    @staticmethod
    def to_five_minutes(bars: Sequence[Bar]) -> list[Bar]:
        groups: dict[datetime, list[Bar]] = {}
        for bar in sorted(bars, key=lambda item: item.start):
            minute = bar.start.minute - bar.start.minute % 5
            bucket = bar.start.replace(
                minute=minute,
                second=0,
                microsecond=0,
            )
            groups.setdefault(bucket, []).append(bar)

        result: list[Bar] = []
        for bucket in sorted(groups):
            items = groups[bucket]
            if len(items) != 5 or any(not item.complete for item in items):
                continue
            expected = [
                bucket.replace(minute=bucket.minute + offset)
                for offset in range(5)
            ]
            if [item.start for item in items] != expected:
                continue
            result.append(
                Bar(
                    symbol=items[0].symbol,
                    start=bucket,
                    end=items[-1].end,
                    open=items[0].open,
                    high=max(item.high for item in items),
                    low=min(item.low for item in items),
                    close=items[-1].close,
                    volume=sum(item.volume for item in items),
                    turnover=sum(item.turnover for item in items),
                )
            )
        return result


@dataclass
class MarketContext:
    """Snapshot of the strategy's one-minute indicators."""

    structure_high: Decimal = ZERO
    structure_low: Decimal = ZERO
    vwap_value: Decimal = ZERO
    vwap_slope_val: Decimal = ZERO
    ema9: Decimal = ZERO
    ema9_prev: Decimal = ZERO
    ema20: Decimal = ZERO
    macd_line: Decimal = ZERO
    macd_signal: Decimal = ZERO
    macd_hist: Decimal = ZERO
    macd_hist_prev: Decimal = ZERO
    adx_val: Decimal = ZERO
    atr_val: Decimal = ZERO
    rvol_val: Decimal = Decimal("1.0")
    prev_day_high: Decimal = ZERO
    prev_day_low: Decimal = ZERO
    prev_close: Decimal = ZERO
    prev2_close: Decimal = ZERO
    day_high: Decimal = ZERO
    day_low: Decimal = ZERO
    current_close: Decimal = ZERO
    current_high: Decimal = ZERO
    current_low: Decimal = ZERO
    current_open: Decimal = ZERO
    current_volume: int = 0
    bar_time: time = time(9, 30)
    bar_end: datetime | None = None
    rsi_val: Decimal = Decimal("50")
    rsi_prev: Decimal = Decimal("50")
    boll_upper: Decimal = ZERO
    boll_middle: Decimal = ZERO
    boll_middle_prev: Decimal = ZERO
    boll_middle_prev2: Decimal = ZERO
    boll_lower: Decimal = ZERO
    boll_middle_crosses: int = 0
    market_state: MarketState = MarketState.UNKNOWN
