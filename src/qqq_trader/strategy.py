from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

from .domain import Bar, Direction, Signal

NY_TZ = ZoneInfo("America/New_York")


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


def vwap(bars: Sequence[Bar]) -> Decimal:
    """Calculate VWAP from a sequence of bars (typically intraday from market open)."""
    cum_pv = Decimal(0)
    cum_vol = 0
    for bar in bars:
        typical_price = (bar.high + bar.low + bar.close) / Decimal(3)
        cum_pv += typical_price * Decimal(bar.volume)
        cum_vol += bar.volume
    if cum_vol == 0:
        return bars[-1].close if bars else Decimal(0)
    return cum_pv / Decimal(cum_vol)


# ---------------------------------------------------------------------------
# Strategy 1: VWAP + 5-Minute Opening Range Breakout (ORB)
# Active: 9:35 - 10:05 ET (after the 5-min ORB window is established)
# ---------------------------------------------------------------------------


class OrbStrategy:
    """VWAP + 5-min Opening Range Breakout."""

    def __init__(
        self,
        min_volume_ratio: Decimal = Decimal("1.5"),
        volume_average_period: int = 20,
    ) -> None:
        self.min_volume_ratio = min_volume_ratio
        self.volume_average_period = volume_average_period

    def evaluate(self, bars: Sequence[Bar], spot: Decimal | None = None) -> Signal | None:
        if not bars or not bars[-1].complete:
            return None
        complete = [bar for bar in bars if bar.complete]
        if len(complete) < 5:
            return None

        current = complete[-1]
        bar_time_et = current.end.astimezone(NY_TZ).time()

        if bar_time_et < time(9, 35) or bar_time_et > time(10, 5):
            return None

        orb_start = time(9, 30)
        orb_end = time(9, 35)
        orb_bars = [
            b for b in complete
            if orb_start <= b.start.astimezone(NY_TZ).time() < orb_end
        ]
        if not orb_bars:
            return None

        orb_high = max(b.high for b in orb_bars)
        orb_low = min(b.low for b in orb_bars)

        today_bars = [
            b for b in complete
            if b.start.astimezone(NY_TZ).time() >= time(9, 30)
        ]
        current_vwap = vwap(today_bars) if today_bars else current.close

        volume_window = complete[-(self.volume_average_period + 1):-1]
        if not volume_window:
            return None
        avg_vol = Decimal(sum(b.volume for b in volume_window)) / Decimal(len(volume_window))
        volume_ratio = Decimal(current.volume) / avg_vol if avg_vol > 0 else Decimal(0)
        volume_ok = volume_ratio > self.min_volume_ratio

        signal_spot = spot if spot is not None else current.close
        orb_mid = (orb_high + orb_low) / Decimal(2)
        indicators = {
            "strategy": "orb",
            "orb_high": str(orb_high),
            "orb_low": str(orb_low),
            "orb_mid": str(orb_mid),
            "vwap": str(current_vwap),
            "volume_ratio": str(volume_ratio),
        }

        body_close_above = current.close > orb_high
        if body_close_above and current.close > current_vwap and volume_ok:
            return Signal(
                direction=Direction.CALL,
                bar_end=current.end,
                spot=signal_spot,
                ema_fast=current_vwap,
                ema_slow=orb_mid,
                vwap=current_vwap,
                breakout_level=orb_high,
                indicators=indicators,
            )

        body_close_below = current.close < orb_low
        if body_close_below and current.close < current_vwap and volume_ok:
            return Signal(
                direction=Direction.PUT,
                bar_end=current.end,
                spot=signal_spot,
                ema_fast=current_vwap,
                ema_slow=orb_mid,
                vwap=current_vwap,
                breakout_level=orb_low,
                indicators=indicators,
            )
        return None


# ---------------------------------------------------------------------------
# Strategy 2: 9/21 EMA + VWAP Trend Pullback (Trending Days)
# Active: 10:00 - 11:30 ET
# ---------------------------------------------------------------------------


class EmaTrendStrategy:
    """9/21 EMA + VWAP pullback strategy for trending days."""

    def __init__(
        self,
        ema_fast_period: int = 9,
        ema_slow_period: int = 21,
        min_volume_ratio: Decimal = Decimal("1.0"),
        volume_average_period: int = 20,
    ) -> None:
        self.ema_fast_period = ema_fast_period
        self.ema_slow_period = ema_slow_period
        self.min_volume_ratio = min_volume_ratio
        self.volume_average_period = volume_average_period

    def evaluate(self, bars: Sequence[Bar], spot: Decimal | None = None) -> Signal | None:
        if not bars or not bars[-1].complete:
            return None
        complete = [bar for bar in bars if bar.complete]
        required = self.ema_slow_period + 2
        if len(complete) < required:
            return None

        current = complete[-1]
        bar_time_et = current.end.astimezone(NY_TZ).time()

        in_window = (time(10, 0) <= bar_time_et <= time(11, 30))
        if not in_window:
            return None

        closes = [bar.close for bar in complete]
        ema9_values = ema_series(closes, self.ema_fast_period)
        ema21_values = ema_series(closes, self.ema_slow_period)

        offset = self.ema_slow_period - self.ema_fast_period
        ema9_current = ema9_values[-1]
        ema21_current = ema21_values[-1]
        ema9_prev = ema9_values[-2]
        ema21_prev = ema21_values[-2]

        today_bars = [
            b for b in complete
            if b.start.astimezone(NY_TZ).time() >= time(9, 30)
        ]
        current_vwap = vwap(today_bars) if today_bars else current.close

        volume_window = complete[-(self.volume_average_period + 1):-1]
        avg_vol = Decimal(sum(b.volume for b in volume_window)) / Decimal(len(volume_window)) if volume_window else Decimal(1)
        volume_ratio = Decimal(current.volume) / avg_vol if avg_vol > 0 else Decimal(0)

        signal_spot = spot if spot is not None else current.close
        indicators = {
            "strategy": "ema_trend",
            "ema9": str(ema9_current),
            "ema21": str(ema21_current),
            "vwap": str(current_vwap),
            "volume_ratio": str(volume_ratio),
        }

        bullish_trend = ema9_current > ema21_current and current.close > current_vwap
        ema_spread_bull = (ema9_current - ema21_current) / ema21_current
        if bullish_trend and ema_spread_bull > Decimal("0.0005"):
            pullback_to_support = current.low <= ema9_current or current.low <= current_vwap
            bullish_reversal = current.close > current.open and (
                current.close - current.low > (current.high - current.low) * Decimal("0.6")
            )
            if pullback_to_support and bullish_reversal:
                indicators["ema_spread"] = str(ema_spread_bull)
                return Signal(
                    direction=Direction.CALL,
                    bar_end=current.end,
                    spot=signal_spot,
                    ema_fast=ema9_current,
                    ema_slow=ema21_current,
                    vwap=current_vwap,
                    breakout_level=ema9_current,
                    indicators=indicators,
                )

        bearish_trend = ema9_current < ema21_current and current.close < current_vwap
        ema_spread_bear = (ema21_current - ema9_current) / ema21_current
        if bearish_trend and ema_spread_bear > Decimal("0.0005"):
            pullback_to_resistance = current.high >= ema9_current or current.high >= current_vwap
            bearish_reversal = current.close < current.open and (
                current.high - current.close > (current.high - current.low) * Decimal("0.6")
            )
            if pullback_to_resistance and bearish_reversal:
                indicators["ema_spread"] = str(ema_spread_bear)
                return Signal(
                    direction=Direction.PUT,
                    bar_end=current.end,
                    spot=signal_spot,
                    ema_fast=ema9_current,
                    ema_slow=ema21_current,
                    vwap=current_vwap,
                    breakout_level=ema9_current,
                    indicators=indicators,
                )

        return None


# ---------------------------------------------------------------------------
# Strategy 3: Bollinger Bands + RSI Mean Reversion (Range-Bound Days)
# Active: 11:30 - 14:00 ET (midday low-volatility period)
# ---------------------------------------------------------------------------


class BollingerRsiStrategy:
    """Bollinger Bands + RSI mean reversion for range-bound midday sessions."""

    def __init__(
        self,
        bollinger_period: int = 20,
        bollinger_stddev: Decimal = Decimal("2"),
        rsi_period: int = 14,
        rsi_oversold: Decimal = Decimal("30"),
        rsi_overbought: Decimal = Decimal("70"),
        bb_width_max: Decimal = Decimal("0.02"),
    ) -> None:
        self.bollinger_period = bollinger_period
        self.bollinger_stddev = bollinger_stddev
        self.rsi_period = rsi_period
        self.rsi_oversold = rsi_oversold
        self.rsi_overbought = rsi_overbought
        self.bb_width_max = bb_width_max

    def evaluate(self, bars: Sequence[Bar], spot: Decimal | None = None) -> Signal | None:
        if not bars or not bars[-1].complete:
            return None
        complete = [bar for bar in bars if bar.complete]
        required = max(self.bollinger_period + 1, self.rsi_period + 1)
        if len(complete) < required:
            return None

        current = complete[-1]
        bar_time_et = current.end.astimezone(NY_TZ).time()

        if bar_time_et < time(11, 30) or bar_time_et > time(14, 0):
            return None

        closes = [bar.close for bar in complete]
        middle, upper, lower = bollinger_bands(closes, self.bollinger_period, self.bollinger_stddev)
        rsi_value = rsi(closes, self.rsi_period)

        bb_width = (upper - lower) / middle if middle > 0 else Decimal(0)

        signal_spot = spot if spot is not None else current.close
        indicators = {
            "strategy": "bb_rsi_reversion",
            "bb_upper": str(upper),
            "bb_middle": str(middle),
            "bb_lower": str(lower),
            "bb_width": str(bb_width),
            "rsi": str(rsi_value),
        }

        if bb_width > self.bb_width_max:
            return None

        if current.close <= lower and rsi_value < self.rsi_oversold:
            reversal = current.close > current.open
            if reversal:
                return Signal(
                    direction=Direction.CALL,
                    bar_end=current.end,
                    spot=signal_spot,
                    ema_fast=middle,
                    ema_slow=lower,
                    vwap=middle,
                    breakout_level=lower,
                    indicators=indicators,
                )

        if current.close >= upper and rsi_value > self.rsi_overbought:
            reversal = current.close < current.open
            if reversal:
                return Signal(
                    direction=Direction.PUT,
                    bar_end=current.end,
                    spot=signal_spot,
                    ema_fast=middle,
                    ema_slow=upper,
                    vwap=middle,
                    breakout_level=upper,
                    indicators=indicators,
                )

        return None


# ---------------------------------------------------------------------------
# Time-Based Strategy Router
# ---------------------------------------------------------------------------


class TimeBasedStrategyRouter:
    """Routes to the appropriate strategy based on the current bar time (ET)."""

    def __init__(
        self,
        orb: OrbStrategy,
        ema_trend: EmaTrendStrategy,
        bb_rsi: BollingerRsiStrategy,
        signal_cutoff: time = time(13, 50),
    ) -> None:
        self.orb = orb
        self.ema_trend = ema_trend
        self.bb_rsi = bb_rsi
        self.signal_cutoff = signal_cutoff

    def evaluate(self, bars: Sequence[Bar], spot: Decimal | None = None) -> Signal | None:
        if not bars or not bars[-1].complete:
            return None

        current = bars[-1] if bars[-1].complete else None
        if current is None:
            return None

        bar_time_et = current.end.astimezone(NY_TZ).time()

        if bar_time_et >= self.signal_cutoff:
            return None

        if time(9, 35) <= bar_time_et <= time(10, 5):
            signal = self.orb.evaluate(bars, spot)
            if signal is not None:
                return signal

        if time(10, 0) <= bar_time_et <= time(11, 30):
            signal = self.ema_trend.evaluate(bars, spot)
            if signal is not None:
                return signal

        if time(11, 30) <= bar_time_et <= time(14, 0):
            signal = self.bb_rsi.evaluate(bars, spot)
            if signal is not None:
                return signal

        return None


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def strategy_from_settings(settings) -> TimeBasedStrategyRouter:
    forced = getattr(settings, "forced_close", time(14, 0))
    cutoff_minutes = forced.hour * 60 + forced.minute - 10
    signal_cutoff = time(cutoff_minutes // 60, cutoff_minutes % 60)

    orb = OrbStrategy(
        min_volume_ratio=getattr(settings, "orb_min_volume_ratio", Decimal("1.5")),
        volume_average_period=getattr(settings, "volume_average_period", 20),
    )
    ema_trend = EmaTrendStrategy(
        ema_fast_period=getattr(settings, "ema_fast_period", 9),
        ema_slow_period=getattr(settings, "ema_slow_period", 21),
        min_volume_ratio=getattr(settings, "min_volume_ratio", Decimal("1.0")),
        volume_average_period=getattr(settings, "volume_average_period", 20),
    )
    bb_rsi = BollingerRsiStrategy(
        bollinger_period=getattr(settings, "bollinger_period", 20),
        bollinger_stddev=getattr(settings, "bollinger_stddev", Decimal("2")),
        rsi_period=getattr(settings, "rsi_period", 14),
        rsi_oversold=getattr(settings, "rsi_put_min", Decimal("30")),
        rsi_overbought=getattr(settings, "rsi_call_max", Decimal("70")),
        bb_width_max=getattr(settings, "bb_width_max", Decimal("0.02")),
    )
    return TimeBasedStrategyRouter(orb, ema_trend, bb_rsi, signal_cutoff=signal_cutoff)


# ---------------------------------------------------------------------------
# Bar Aggregator (kept for 5-minute bar generation)
# ---------------------------------------------------------------------------


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
