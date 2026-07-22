from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

from .domain import Bar, Direction, Signal


def ema_series(values: Sequence[Decimal], period: int) -> list[Decimal]:
    """Return seeded EMA values aligned from source index period - 1."""
    if period < 1 or len(values) < period:
        raise ValueError(f"at least {period} values are required")
    multiplier = Decimal(2) / Decimal(period + 1)
    current = sum(values[:period], Decimal(0)) / Decimal(period)
    result = [current]
    for value in values[period:]:
        current = (value - current) * multiplier + current
        result.append(current)
    return result


def ema(values: Sequence[Decimal], period: int) -> Decimal:
    return ema_series(values, period)[-1]


def macd(
    values: Sequence[Decimal],
    fast_period: int = 8,
    slow_period: int = 17,
    signal_period: int = 9,
) -> tuple[Decimal, Decimal]:
    if not 0 < fast_period < slow_period:
        raise ValueError("MACD periods must satisfy 0 < fast < slow")
    required = slow_period + signal_period - 1
    if len(values) < required:
        raise ValueError(f"at least {required} values are required")
    fast_values = ema_series(values, fast_period)
    slow_values = ema_series(values, slow_period)
    offset = slow_period - fast_period
    macd_values = [
        fast - slow for fast, slow in zip(fast_values[offset:], slow_values, strict=True)
    ]
    return macd_values[-1], ema(macd_values, signal_period)


def bollinger_bands(
    values: Sequence[Decimal],
    period: int = 20,
    stddev_multiplier: Decimal = Decimal("2"),
) -> tuple[Decimal, Decimal, Decimal]:
    if period < 2 or len(values) < period:
        raise ValueError(f"at least {period} values are required")
    window = values[-period:]
    middle = sum(window, Decimal(0)) / Decimal(period)
    variance = sum(((value - middle) ** 2 for value in window), Decimal(0)) / Decimal(period)
    width = variance.sqrt() * stddev_multiplier
    return middle, middle + width, middle - width


def rsi(values: Sequence[Decimal], period: int = 14) -> Decimal:
    """Wilder RSI calculated only from completed closes."""
    if period < 1 or len(values) < period + 1:
        raise ValueError(f"at least {period + 1} values are required")
    changes = [current - previous for previous, current in zip(values, values[1:], strict=False)]
    initial = changes[:period]
    average_gain = sum((max(change, Decimal(0)) for change in initial), Decimal(0)) / Decimal(
        period
    )
    average_loss = sum((max(-change, Decimal(0)) for change in initial), Decimal(0)) / Decimal(
        period
    )
    for change in changes[period:]:
        gain = max(change, Decimal(0))
        loss = max(-change, Decimal(0))
        average_gain = (average_gain * Decimal(period - 1) + gain) / Decimal(period)
        average_loss = (average_loss * Decimal(period - 1) + loss) / Decimal(period)
    if average_loss == 0:
        return Decimal(100) if average_gain > 0 else Decimal(50)
    relative_strength = average_gain / average_loss
    return Decimal(100) - Decimal(100) / (Decimal(1) + relative_strength)


class MacdBollingerStrategy:
    """QQQ 5m MACD direction + Bollinger breakout + volume and RSI filters."""

    def __init__(
        self,
        macd_fast: int = 8,
        macd_slow: int = 17,
        macd_signal: int = 9,
        bollinger_period: int = 20,
        bollinger_stddev: Decimal = Decimal("2"),
        volume_average_period: int = 20,
        min_volume_ratio: Decimal = Decimal("1.2"),
        rsi_period: int = 14,
        rsi_call_max: Decimal = Decimal("70"),
        rsi_put_min: Decimal = Decimal("30"),
    ) -> None:
        if not 0 < macd_fast < macd_slow:
            raise ValueError("MACD fast period must be shorter than slow period")
        if min(macd_signal, bollinger_period, volume_average_period, rsi_period) < 1:
            raise ValueError("indicator periods must be positive")
        if bollinger_period < 2 or bollinger_stddev <= 0 or min_volume_ratio <= 0:
            raise ValueError("Bollinger and volume settings must be positive")
        if not Decimal(0) < rsi_put_min < rsi_call_max < Decimal(100):
            raise ValueError("RSI thresholds must satisfy 0 < put < call < 100")
        self.macd_fast = macd_fast
        self.macd_slow = macd_slow
        self.macd_signal = macd_signal
        self.bollinger_period = bollinger_period
        self.bollinger_stddev = bollinger_stddev
        self.volume_average_period = volume_average_period
        self.min_volume_ratio = min_volume_ratio
        self.rsi_period = rsi_period
        self.rsi_call_max = rsi_call_max
        self.rsi_put_min = rsi_put_min

    def evaluate(self, bars: Sequence[Bar], spot: Decimal | None = None) -> Signal | None:
        if not bars or not bars[-1].complete:
            return None
        complete = [bar for bar in bars if bar.complete]
        required = max(
            self.macd_slow + self.macd_signal - 1,
            self.bollinger_period + 1,
            self.volume_average_period + 1,
            self.rsi_period + 1,
        )
        if len(complete) < required:
            return None

        closes = [bar.close for bar in complete]
        current = complete[-1]
        previous = complete[-2]
        macd_line, signal_line = macd(closes, self.macd_fast, self.macd_slow, self.macd_signal)
        middle, upper, lower = bollinger_bands(closes, self.bollinger_period, self.bollinger_stddev)
        _, previous_upper, previous_lower = bollinger_bands(
            closes[:-1], self.bollinger_period, self.bollinger_stddev
        )
        rsi_value = rsi(closes, self.rsi_period)
        volume_window = complete[-(self.volume_average_period + 1) : -1]
        average_volume = Decimal(sum((bar.volume for bar in volume_window), 0)) / Decimal(
            len(volume_window)
        )
        if average_volume <= 0:
            return None
        volume_ratio = Decimal(current.volume) / average_volume
        volume_confirmed = volume_ratio > self.min_volume_ratio
        signal_spot = spot if spot is not None else current.close
        indicators = {
            "macd": str(macd_line),
            "macd_signal": str(signal_line),
            "macd_histogram": str(macd_line - signal_line),
            "bollinger_middle": str(middle),
            "bollinger_upper": str(upper),
            "bollinger_lower": str(lower),
            "rsi": str(rsi_value),
            "volume": str(current.volume),
            "average_volume": str(average_volume),
            "volume_ratio": str(volume_ratio),
        }

        call_breakout = previous.close <= previous_upper and current.close > upper
        if (
            macd_line > signal_line
            and call_breakout
            and volume_confirmed
            and rsi_value < self.rsi_call_max
        ):
            return Signal(
                direction=Direction.CALL,
                bar_end=current.end,
                spot=signal_spot,
                ema_fast=macd_line,
                ema_slow=signal_line,
                vwap=middle,
                breakout_level=upper,
                indicators=indicators,
            )

        put_breakout = previous.close >= previous_lower and current.close < lower
        if (
            macd_line < signal_line
            and put_breakout
            and volume_confirmed
            and rsi_value > self.rsi_put_min
        ):
            return Signal(
                direction=Direction.PUT,
                bar_end=current.end,
                spot=signal_spot,
                ema_fast=macd_line,
                ema_slow=signal_line,
                vwap=middle,
                breakout_level=lower,
                indicators=indicators,
            )
        return None


def strategy_from_settings(settings) -> MacdBollingerStrategy:
    return MacdBollingerStrategy(
        macd_fast=settings.macd_fast,
        macd_slow=settings.macd_slow,
        macd_signal=settings.macd_signal,
        bollinger_period=settings.bollinger_period,
        bollinger_stddev=settings.bollinger_stddev,
        volume_average_period=settings.volume_average_period,
        min_volume_ratio=settings.min_volume_ratio,
        rsi_period=settings.rsi_period,
        rsi_call_max=settings.rsi_call_max,
        rsi_put_min=settings.rsi_put_min,
    )


class BarAggregator:
    """Deterministically derives completed five-minute bars from one-minute bars."""

    @staticmethod
    def to_five_minutes(bars: Sequence[Bar]) -> list[Bar]:
        groups: dict[datetime, list[Bar]] = {}
        for bar in sorted(bars, key=lambda item: item.start):
            minute = bar.start.minute - (bar.start.minute % 5)
            bucket = bar.start.replace(minute=minute, second=0, microsecond=0)
            groups.setdefault(bucket, []).append(bar)

        result: list[Bar] = []
        for bucket, items in sorted(groups.items()):
            if len(items) != 5 or not all(item.complete for item in items):
                continue
            result.append(
                Bar(
                    symbol=items[0].symbol,
                    start=bucket,
                    end=bucket + (items[-1].end - items[-1].start) * 5,
                    open=items[0].open,
                    high=max(item.high for item in items),
                    low=min(item.low for item in items),
                    close=items[-1].close,
                    volume=sum(item.volume for item in items),
                    turnover=sum((item.turnover for item in items), Decimal(0)),
                    complete=True,
                )
            )
        return result
