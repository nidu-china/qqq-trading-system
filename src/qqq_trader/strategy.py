"""Trading strategy: 5-min bar state machine with trend/reversal/range classification.

Uses 5-minute K-lines for signal generation, 1-minute bars for execution precision.
Indicators: VWAP, EMA9/20, MACD(12,26,9), ADX(14), ATR(14), RVOL.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, time
from decimal import Decimal
from enum import StrEnum
from zoneinfo import ZoneInfo

from .domain import Bar, Direction, MarketState, Signal

NY_TZ = ZoneInfo("America/New_York")
ZERO = Decimal(0)


# ---------------------------------------------------------------------------
# Indicator functions
# ---------------------------------------------------------------------------


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
    """Return (macd_line, signal_line, histogram)."""
    if not 0 < fast < slow:
        raise ValueError("MACD periods must satisfy 0 < fast < slow")
    required = slow + signal_period - 1
    if len(values) < required:
        raise ValueError(f"at least {required} values are required")
    fast_vals = ema_series(values, fast)
    slow_vals = ema_series(values, slow)
    offset = slow - fast
    macd_vals = [f - s for f, s in zip(fast_vals[offset:], slow_vals, strict=True)]
    signal_val = ema(macd_vals, signal_period)
    macd_line = macd_vals[-1]
    hist = macd_line - signal_val
    return macd_line, signal_val, hist


def macd_histogram_series(
    values: Sequence[Decimal],
    fast: int = 12,
    slow: int = 26,
    signal_period: int = 9,
) -> list[Decimal]:
    """Return histogram series for exhaustion detection."""
    if not 0 < fast < slow:
        raise ValueError("MACD periods must satisfy 0 < fast < slow")
    required = slow + signal_period - 1
    if len(values) < required:
        raise ValueError(f"at least {required} values are required")
    fast_vals = ema_series(values, fast)
    slow_vals = ema_series(values, slow)
    offset = slow - fast
    macd_vals = [f - s for f, s in zip(fast_vals[offset:], slow_vals, strict=True)]
    signal_vals = ema_series(macd_vals, signal_period)
    sig_offset = signal_period - 1
    return [m - s for m, s in zip(macd_vals[sig_offset:], signal_vals, strict=True)]


def vwap(bars: Sequence[Bar]) -> Decimal:
    """Volume-weighted average price."""
    total_vol = sum(b.volume for b in bars)
    if total_vol == 0:
        return bars[-1].close if bars else ZERO
    typical_volume = sum(
        (b.high + b.low + b.close) / Decimal(3) * Decimal(b.volume) for b in bars
    )
    return typical_volume / Decimal(total_vol)


def vwap_slope(bars: Sequence[Bar], lookback: int = 3) -> Decimal:
    """VWAP slope over last `lookback` completed 5-min bars."""
    if len(bars) < lookback + 1:
        return ZERO
    vwaps = []
    for i in range(lookback + 1):
        window = bars[: len(bars) - lookback + i]
        if window:
            vwaps.append(vwap(window))
    if len(vwaps) < 2:
        return ZERO
    return vwaps[-1] - vwaps[0]


def atr_series(bars: Sequence[Bar], period: int = 14) -> list[Decimal]:
    """Average True Range series."""
    if len(bars) < period + 1:
        raise ValueError(f"at least {period + 1} bars are required for ATR")
    true_ranges: list[Decimal] = []
    for i in range(1, len(bars)):
        high_low = bars[i].high - bars[i].low
        high_prev_close = abs(bars[i].high - bars[i - 1].close)
        low_prev_close = abs(bars[i].low - bars[i - 1].close)
        true_ranges.append(max(high_low, high_prev_close, low_prev_close))
    multiplier = Decimal(2) / Decimal(period + 1)
    current = sum(true_ranges[:period], ZERO) / Decimal(period)
    result = [current]
    for tr in true_ranges[period:]:
        current = (tr - current) * multiplier + current
        result.append(current)
    return result


def atr(bars: Sequence[Bar], period: int = 14) -> Decimal:
    return atr_series(bars, period)[-1]


def adx(bars: Sequence[Bar], period: int = 14) -> Decimal:
    """Average Directional Index."""
    if len(bars) < period * 2 + 1:
        return ZERO
    plus_dm_list: list[Decimal] = []
    minus_dm_list: list[Decimal] = []
    tr_list: list[Decimal] = []
    for i in range(1, len(bars)):
        high_diff = bars[i].high - bars[i - 1].high
        low_diff = bars[i - 1].low - bars[i].low
        plus_dm = high_diff if high_diff > low_diff and high_diff > 0 else ZERO
        minus_dm = low_diff if low_diff > high_diff and low_diff > 0 else ZERO
        plus_dm_list.append(plus_dm)
        minus_dm_list.append(minus_dm)
        tr = max(
            bars[i].high - bars[i].low,
            abs(bars[i].high - bars[i - 1].close),
            abs(bars[i].low - bars[i - 1].close),
        )
        tr_list.append(tr)

    def smooth(values: list[Decimal], p: int) -> list[Decimal]:
        s = sum(values[:p], ZERO)
        result = [s]
        for v in values[p:]:
            s = s - s / Decimal(p) + v
            result.append(s)
        return result

    smoothed_tr = smooth(tr_list, period)
    smoothed_plus = smooth(plus_dm_list, period)
    smoothed_minus = smooth(minus_dm_list, period)

    dx_list: list[Decimal] = []
    for i in range(len(smoothed_tr)):
        if smoothed_tr[i] == 0:
            dx_list.append(ZERO)
            continue
        plus_di = Decimal(100) * smoothed_plus[i] / smoothed_tr[i]
        minus_di = Decimal(100) * smoothed_minus[i] / smoothed_tr[i]
        di_sum = plus_di + minus_di
        if di_sum == 0:
            dx_list.append(ZERO)
        else:
            dx_list.append(Decimal(100) * abs(plus_di - minus_di) / di_sum)

    if len(dx_list) < period:
        return ZERO
    adx_val = sum(dx_list[:period], ZERO) / Decimal(period)
    for dx in dx_list[period:]:
        adx_val = (adx_val * Decimal(period - 1) + dx) / Decimal(period)
    return adx_val


def rvol(current_volume: int, historical_volumes: Sequence[int]) -> Decimal:
    """Relative volume vs historical same-time-of-day average."""
    if not historical_volumes or current_volume == 0:
        return Decimal("1.0")
    avg = sum(historical_volumes) / len(historical_volumes)
    if avg == 0:
        return Decimal("1.0")
    return Decimal(str(current_volume / avg))


# ---------------------------------------------------------------------------
# Bar Aggregator
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


# ---------------------------------------------------------------------------
# Market Context
# ---------------------------------------------------------------------------


@dataclass
class MarketContext:
    """Snapshot of all indicators at a given 5-min bar."""
    orh: Decimal = ZERO
    orl: Decimal = ZERO
    or_width: Decimal = ZERO
    or_width_percentile: Decimal = Decimal("0.5")
    vwap_value: Decimal = ZERO
    vwap_slope_val: Decimal = ZERO
    ema9: Decimal = ZERO
    ema20: Decimal = ZERO
    macd_hist: Decimal = ZERO
    macd_hist_prev: Decimal = ZERO
    adx_val: Decimal = ZERO
    atr_val: Decimal = ZERO
    rvol_val: Decimal = Decimal("1.0")
    prev_day_high: Decimal = ZERO
    prev_day_low: Decimal = ZERO
    prev_close: Decimal = ZERO
    day_high: Decimal = ZERO
    day_low: Decimal = ZERO
    current_close: Decimal = ZERO
    current_high: Decimal = ZERO
    current_low: Decimal = ZERO
    current_open: Decimal = ZERO
    current_volume: int = 0
    bar_time: time = time(9, 30)


# ---------------------------------------------------------------------------
# Market State Classifier
# ---------------------------------------------------------------------------


class MarketStateClassifier:
    """Classifies the current market as TREND, REVERSAL, or RANGE."""

    def __init__(self, settings) -> None:
        self.range_adx_max = getattr(settings, "range_adx_max", Decimal("18"))
        self._is_range = False
        self._range_since: datetime | None = None

    def classify(self, ctx: MarketContext, bars_5m: Sequence[Bar]) -> MarketState:
        if len(bars_5m) < 6:
            return MarketState.OBSERVATION

        range_score = 0
        recent = bars_5m[-6:]

        # 1. VWAP crossings in last 6 bars
        vwap_crosses = 0
        for i in range(1, len(recent)):
            prev_above = recent[i - 1].close > ctx.vwap_value
            curr_above = recent[i].close > ctx.vwap_value
            if prev_above != curr_above:
                vwap_crosses += 1
        if vwap_crosses >= 3:
            range_score += 1

        # 2. EMA9/EMA20 near flat or crossed
        ema_diff = abs(ctx.ema9 - ctx.ema20)
        if ctx.ema20 > 0 and ema_diff / ctx.ema20 < Decimal("0.0003"):
            range_score += 1

        # 3. VWAP nearly flat
        if abs(ctx.vwap_slope_val) < Decimal("0.05"):
            range_score += 1

        # 4. ADX < threshold (only valid when ADX is computable)
        if ctx.adx_val > 0 and ctx.adx_val < self.range_adx_max:
            range_score += 1

        # 5. Both ORH and ORL breakouts failed
        broke_orh = any(b.close > ctx.orh for b in recent)
        broke_orl = any(b.close < ctx.orl for b in recent)
        if not broke_orh and not broke_orl:
            range_score += 1

        # 6. Small bodies with large wicks
        wick_bars = 0
        for b in recent:
            body = abs(b.close - b.open)
            total_range = b.high - b.low
            if total_range > 0 and body / total_range < Decimal("0.3"):
                wick_bars += 1
        if wick_bars >= 3:
            range_score += 1

        # 7. MACD around zero (only when computable)
        if (ctx.macd_hist != ZERO or ctx.macd_hist_prev != ZERO):
            if abs(ctx.macd_hist) < Decimal("0.05") and abs(ctx.macd_hist_prev) < Decimal("0.05"):
                range_score += 1

        # 8. Volume declining
        if len(bars_5m) >= 6:
            first_half_vol = sum(b.volume for b in bars_5m[-6:-3])
            second_half_vol = sum(b.volume for b in bars_5m[-3:])
            if first_half_vol > 0 and second_half_vol < first_half_vol * Decimal("0.8"):
                range_score += 1

        if range_score >= 2:
            self._is_range = True
            if self._range_since is None:
                self._range_since = bars_5m[-1].end
            return MarketState.RANGE

        self._is_range = False
        self._range_since = None
        return MarketState.TREND


# ---------------------------------------------------------------------------
# Strategy Engine
# ---------------------------------------------------------------------------


class StrategyEngine:
    """State machine strategy using 5-min bars for signals.

    Phases:
    - Observation (9:30-9:45): record ORH/ORL, classify open
    - Active (9:45-11:25): trend breakout or reversal entry
    - Management (11:25+): no new entries, position management only
    """

    def __init__(self, settings) -> None:
        self.settings = settings
        self._classifier = MarketStateClassifier(settings)
        self._current_date: object = None
        self._orh: Decimal = ZERO
        self._orl: Decimal = ZERO
        self._or_width: Decimal = ZERO
        self._lod: Decimal = ZERO
        self._hod: Decimal = ZERO
        self._prev_day_high: Decimal = ZERO
        self._prev_day_low: Decimal = ZERO
        self._prev_close: Decimal = ZERO
        self._or_widths_history: list[Decimal] = []
        # Trend state
        self._breakout_detected: bool = False
        self._breakout_direction: Direction | None = None
        self._breakout_bar_high: Decimal = ZERO
        self._breakout_bar_low: Decimal = ZERO
        self._waiting_pullback: bool = False
        self._pullback_bar: Bar | None = None
        # Reversal state
        self._reversal_state: str = "idle"
        self._reversal_direction: Direction | None = None
        self._reversal_lod: Decimal = ZERO
        self._reversal_hod: Decimal = ZERO
        self._reversal_breakdown_end: datetime | None = None
        self._reversal_pullback_high: Decimal = ZERO
        self._reversal_pullback_low: Decimal = ZERO

    def _reset_day(self, bar_date) -> None:
        self._current_date = bar_date
        self._orh = ZERO
        self._orl = Decimal("999999")
        self._or_width = ZERO
        self._lod = Decimal("999999")
        self._hod = ZERO
        self._breakout_detected = False
        self._breakout_direction = None
        self._waiting_pullback = False
        self._pullback_bar = None
        self._reversal_state = "idle"
        self._reversal_direction = None

    def _compute_context(
        self, bars_5m: Sequence[Bar], today_bars: Sequence[Bar]
    ) -> MarketContext | None:
        """Compute MarketContext from today's 5-min bars."""
        if len(today_bars) < 4:
            return None

        current = today_bars[-1]
        closes = [b.close for b in today_bars]

        # EMA - use available periods, don't require full lookback
        fast_p = min(self.settings.ema_fast_period, len(closes))
        slow_p = min(self.settings.ema_slow_period, len(closes))
        ema9_val = ema(closes, fast_p) if fast_p >= 2 else closes[-1]
        ema20_val = ema(closes, slow_p) if slow_p >= 2 else closes[-1]

        # MACD
        macd_fast = self.settings.macd_fast
        macd_slow = self.settings.macd_slow
        macd_sig = self.settings.macd_signal
        required_macd = macd_slow + macd_sig - 1
        hist_val = ZERO
        hist_prev = ZERO
        if len(closes) >= required_macd:
            hist_series = macd_histogram_series(closes, macd_fast, macd_slow, macd_sig)
            if len(hist_series) >= 2:
                hist_val = hist_series[-1]
                hist_prev = hist_series[-2]
            elif hist_series:
                hist_val = hist_series[-1]

        # ADX
        adx_val = ZERO
        if len(today_bars) >= self.settings.adx_period * 2 + 1:
            adx_val = adx(list(today_bars), self.settings.adx_period)

        # ATR
        atr_val = ZERO
        if len(today_bars) >= self.settings.atr_period + 1:
            atr_val = atr(list(today_bars), self.settings.atr_period)

        # VWAP
        vwap_val = vwap(today_bars)
        slope = vwap_slope(today_bars, lookback=3)

        # OR width percentile
        or_pct = Decimal("0.5")
        if self._or_widths_history:
            below = sum(1 for w in self._or_widths_history if w < self._or_width)
            or_pct = Decimal(str(below / len(self._or_widths_history)))

        return MarketContext(
            orh=self._orh,
            orl=self._orl,
            or_width=self._or_width,
            or_width_percentile=or_pct,
            vwap_value=vwap_val,
            vwap_slope_val=slope,
            ema9=ema9_val,
            ema20=ema20_val,
            macd_hist=hist_val,
            macd_hist_prev=hist_prev,
            adx_val=adx_val,
            atr_val=atr_val,
            rvol_val=Decimal("1.0"),
            prev_day_high=self._prev_day_high,
            prev_day_low=self._prev_day_low,
            prev_close=self._prev_close,
            day_high=self._hod,
            day_low=self._lod,
            current_close=current.close,
            current_high=current.high,
            current_low=current.low,
            current_open=current.open,
            current_volume=current.volume,
            bar_time=current.end.astimezone(NY_TZ).time(),
        )

    def evaluate(self, bars_1m: Sequence[Bar], spot: Decimal | None = None) -> Signal | None:
        """Main entry point: evaluate 1-min bars, aggregate to 5-min, produce signal."""
        if not bars_1m:
            return None

        current_1m = bars_1m[-1]
        if not current_1m.complete:
            return None

        bar_date = current_1m.end.astimezone(NY_TZ).date()
        bar_time_et = current_1m.end.astimezone(NY_TZ).time()

        # Day boundary
        if bar_date != self._current_date:
            if self._current_date is not None:
                prev_day_1m = [
                    b for b in bars_1m
                    if b.start.astimezone(NY_TZ).date() == self._current_date
                    and b.start.astimezone(NY_TZ).time() >= time(9, 30)
                ]
                if prev_day_1m:
                    self._prev_day_high = max(b.high for b in prev_day_1m)
                    self._prev_day_low = min(b.low for b in prev_day_1m)
                    self._prev_close = prev_day_1m[-1].close
            self._reset_day(bar_date)

        # Filter to today's market hours bars
        today_1m = [
            b for b in bars_1m
            if b.start.astimezone(NY_TZ).date() == bar_date
            and b.start.astimezone(NY_TZ).time() >= time(9, 30)
        ]

        # Aggregate to 5-min
        bars_5m = BarAggregator.to_five_minutes(today_1m)
        if not bars_5m:
            return None

        # Only evaluate on 5-min bar completions
        last_5m = bars_5m[-1]
        if last_5m.end != current_1m.end:
            return None

        # Update ORH/ORL during observation
        for b in bars_5m:
            bt = b.start.astimezone(NY_TZ).time()
            if time(9, 30) <= bt < time(9, 45):
                self._orh = max(self._orh, b.high)
                if self._orl == Decimal("999999"):
                    self._orl = b.low
                else:
                    self._orl = min(self._orl, b.low)

        self._or_width = self._orh - self._orl if self._orh > 0 and self._orl < Decimal("999999") else ZERO

        # Update day high/low
        for b in bars_5m:
            self._hod = max(self._hod, b.high)
            if self._lod == Decimal("999999"):
                self._lod = b.low
            else:
                self._lod = min(self._lod, b.low)

        # Phase 1: Observation (9:30-9:45) -- no signals
        if bar_time_et < time(9, 45):
            return None

        # Phase 3: No new positions after entry_end
        entry_end = getattr(self.settings, "entry_end", time(11, 25))
        if bar_time_et > entry_end:
            return None

        # Need OR to be established
        if self._or_width <= 0:
            return None

        # Compute context
        today_5m = [
            b for b in bars_5m
            if b.start.astimezone(NY_TZ).time() >= time(9, 30)
        ]
        ctx = self._compute_context(bars_5m, today_5m)
        if ctx is None:
            return None

        # Classify market state
        state = self._classifier.classify(ctx, today_5m)
        if state == MarketState.RANGE:
            return None

        # --- State A: Trend breakout ---
        signal = self._evaluate_trend(ctx, today_5m, last_5m, spot)
        if signal is not None:
            return signal

        # --- State B: Reversal ---
        signal = self._evaluate_reversal(ctx, today_5m, last_5m, spot)
        if signal is not None:
            return signal

        return None

    def _evaluate_trend(
        self, ctx: MarketContext, today_5m: Sequence[Bar], current: Bar, spot: Decimal | None
    ) -> Signal | None:
        """State A: Trend breakout with pullback entry."""
        signal_spot = spot if spot is not None else ctx.current_close

        # Check for new breakout
        if not self._breakout_detected:
            # Bullish breakout: hard conditions
            if (
                ctx.current_close > ctx.orh
                and ctx.current_close > ctx.vwap_value
                and ctx.vwap_slope_val > 0
                and ctx.ema9 > ctx.ema20
            ):
                if self._check_soft_conditions(ctx, today_5m, Direction.CALL):
                    touched_support = (
                        ctx.current_low <= ctx.orh
                        or ctx.current_low <= ctx.ema9
                        or ctx.current_low <= ctx.vwap_value
                    )
                    if touched_support and ctx.current_close > ctx.orh:
                        stop = self._compute_stop(ctx, today_5m, Direction.CALL)
                        if stop is not None:
                            stop_dist = signal_spot - stop
                            if stop_dist > 0 and (ctx.atr_val <= 0 or stop_dist <= ctx.atr_val * self.settings.max_stop_atr_ratio):
                                r_val = self._compute_r(signal_spot, stop)
                                return Signal(
                                    direction=Direction.CALL,
                                    bar_end=current.end,
                                    spot=signal_spot,
                                    strategy="trend",
                                    stop_price=stop,
                                    atr=ctx.atr_val,
                                    r_value=r_val,
                                    breakout_level=ctx.orh,
                                    vwap=ctx.vwap_value,
                                    indicators={
                                        "strategy": "trend",
                                        "orh": str(ctx.orh),
                                        "orl": str(ctx.orl),
                                        "vwap": str(ctx.vwap_value),
                                        "ema9": str(ctx.ema9),
                                        "ema20": str(ctx.ema20),
                                        "adx": str(ctx.adx_val),
                                        "atr": str(ctx.atr_val),
                                        "macd_hist": str(ctx.macd_hist),
                                    },
                                )
                    self._breakout_detected = True
                    self._breakout_direction = Direction.CALL
                    self._breakout_bar_high = ctx.current_high
                    self._breakout_bar_low = ctx.current_low
                    self._waiting_pullback = True
                    self._pullback_bar = None
                    return None

            # Bearish breakout
            if (
                ctx.current_close < ctx.orl
                and ctx.current_close < ctx.vwap_value
                and ctx.vwap_slope_val < 0
                and ctx.ema9 < ctx.ema20
            ):
                if self._check_soft_conditions(ctx, today_5m, Direction.PUT):
                    touched_support = (
                        ctx.current_high >= ctx.orl
                        or ctx.current_high >= ctx.ema9
                        or ctx.current_high >= ctx.vwap_value
                    )
                    if touched_support and ctx.current_close < ctx.orl:
                        stop = self._compute_stop(ctx, today_5m, Direction.PUT)
                        if stop is not None:
                            stop_dist = stop - signal_spot
                            if stop_dist > 0 and (ctx.atr_val <= 0 or stop_dist <= ctx.atr_val * self.settings.max_stop_atr_ratio):
                                r_val = self._compute_r(signal_spot, stop)
                                return Signal(
                                    direction=Direction.PUT,
                                    bar_end=current.end,
                                    spot=signal_spot,
                                    strategy="trend",
                                    stop_price=stop,
                                    atr=ctx.atr_val,
                                    r_value=r_val,
                                    breakout_level=ctx.orl,
                                    vwap=ctx.vwap_value,
                                    indicators={
                                        "strategy": "trend",
                                        "orh": str(ctx.orh),
                                        "orl": str(ctx.orl),
                                        "vwap": str(ctx.vwap_value),
                                        "ema9": str(ctx.ema9),
                                        "ema20": str(ctx.ema20),
                                        "adx": str(ctx.adx_val),
                                        "atr": str(ctx.atr_val),
                                        "macd_hist": str(ctx.macd_hist),
                                    },
                                )
                    self._breakout_detected = True
                    self._breakout_direction = Direction.PUT
                    self._breakout_bar_high = ctx.current_high
                    self._breakout_bar_low = ctx.current_low
                    self._waiting_pullback = True
                    self._pullback_bar = None
                    return None

        # Wait for pullback and entry
        if self._waiting_pullback and self._breakout_direction is not None:
            chase_limit = ctx.atr_val * self.settings.chase_atr_factor if ctx.atr_val > 0 else Decimal("2")

            if self._breakout_direction == Direction.CALL:
                # Check if price already too far from breakout
                if ctx.current_close - ctx.orh > chase_limit:
                    self._breakout_detected = False
                    self._waiting_pullback = False
                    return None
                # Pullback: price touched ORH, EMA9, or VWAP
                pullback_to = (
                    ctx.current_low <= ctx.orh
                    or ctx.current_low <= ctx.ema9
                    or ctx.current_low <= ctx.vwap_value
                )
                # Still holding above support and bullish close
                holding = ctx.current_close > ctx.orh and ctx.current_close > ctx.current_open
                if pullback_to and holding:
                    stop = self._compute_stop(ctx, today_5m, Direction.CALL)
                    if stop is None:
                        return None
                    stop_dist = signal_spot - stop
                    if stop_dist <= 0:
                        return None
                    if ctx.atr_val > 0 and stop_dist > ctx.atr_val * self.settings.max_stop_atr_ratio:
                        self._breakout_detected = False
                        self._waiting_pullback = False
                        return None
                    r_val = self._compute_r(signal_spot, stop)
                    self._breakout_detected = False
                    self._waiting_pullback = False
                    return Signal(
                        direction=Direction.CALL,
                        bar_end=current.end,
                        spot=signal_spot,
                        strategy="trend",
                        stop_price=stop,
                        atr=ctx.atr_val,
                        r_value=r_val,
                        breakout_level=ctx.orh,
                        vwap=ctx.vwap_value,
                        indicators={
                            "strategy": "trend",
                            "orh": str(ctx.orh),
                            "orl": str(ctx.orl),
                            "vwap": str(ctx.vwap_value),
                            "ema9": str(ctx.ema9),
                            "ema20": str(ctx.ema20),
                            "adx": str(ctx.adx_val),
                            "atr": str(ctx.atr_val),
                            "macd_hist": str(ctx.macd_hist),
                        },
                    )

            elif self._breakout_direction == Direction.PUT:
                if ctx.orl - ctx.current_close > chase_limit:
                    self._breakout_detected = False
                    self._waiting_pullback = False
                    return None
                pullback_to = (
                    ctx.current_high >= ctx.orl
                    or ctx.current_high >= ctx.ema9
                    or ctx.current_high >= ctx.vwap_value
                )
                holding = ctx.current_close < ctx.orl and ctx.current_close < ctx.current_open
                if pullback_to and holding:
                    stop = self._compute_stop(ctx, today_5m, Direction.PUT)
                    if stop is None:
                        return None
                    stop_dist = stop - signal_spot
                    if stop_dist <= 0:
                        return None
                    if ctx.atr_val > 0 and stop_dist > ctx.atr_val * self.settings.max_stop_atr_ratio:
                        self._breakout_detected = False
                        self._waiting_pullback = False
                        return None
                    r_val = self._compute_r(signal_spot, stop)
                    self._breakout_detected = False
                    self._waiting_pullback = False
                    return Signal(
                        direction=Direction.PUT,
                        bar_end=current.end,
                        spot=signal_spot,
                        strategy="trend",
                        stop_price=stop,
                        atr=ctx.atr_val,
                        r_value=r_val,
                        breakout_level=ctx.orl,
                        vwap=ctx.vwap_value,
                        indicators={
                            "strategy": "trend",
                            "orh": str(ctx.orh),
                            "orl": str(ctx.orl),
                            "vwap": str(ctx.vwap_value),
                            "ema9": str(ctx.ema9),
                            "ema20": str(ctx.ema20),
                            "adx": str(ctx.adx_val),
                            "atr": str(ctx.atr_val),
                            "macd_hist": str(ctx.macd_hist),
                        },
                    )
        return None

    def _evaluate_reversal(
        self, ctx: MarketContext, today_5m: Sequence[Bar], current: Bar, spot: Decimal | None
    ) -> Signal | None:
        """State B: Opening direction failure + reversal."""
        signal_spot = spot if spot is not None else ctx.current_close

        # --- Bullish reversal (price broke ORL then reversed up) ---
        if self._reversal_state == "idle":
            if ctx.current_close < ctx.orl or ctx.current_low < ctx.orl:
                self._reversal_state = "breakdown_put"
                self._reversal_direction = Direction.CALL
                self._reversal_lod = min(ctx.day_low, ctx.current_low)
                self._reversal_breakdown_end = current.end
            elif ctx.current_close > ctx.orh or ctx.current_high > ctx.orh:
                self._reversal_state = "breakdown_call"
                self._reversal_direction = Direction.PUT
                self._reversal_hod = max(ctx.day_high, ctx.current_high)
                self._reversal_breakdown_end = current.end
            return None

        # Bullish reversal path
        if self._reversal_state == "breakdown_put":
            self._reversal_lod = min(self._reversal_lod, ctx.current_low)
            # Check timeout (3 five-min bars = 15 min)
            elapsed = len([
                b for b in today_5m if b.end > (self._reversal_breakdown_end or current.end)
            ])
            if elapsed > 3 and ctx.current_close < ctx.orl:
                self._reversal_state = "idle"
                return None
            if ctx.current_close > ctx.orl:
                self._reversal_state = "reclaimed_put"
            return None

        if self._reversal_state == "reclaimed_put":
            if ctx.current_low < self._reversal_lod:
                self._reversal_state = "idle"
                return None
            # Higher low + MACD exhaustion
            has_higher_low = ctx.current_low > self._reversal_lod
            macd_exhaustion = (
                ctx.macd_hist > ctx.macd_hist_prev
                or (ctx.macd_hist < 0 and ctx.macd_hist > ctx.macd_hist_prev)
            )
            if has_higher_low and macd_exhaustion and ctx.current_close > ctx.vwap_value:
                self._reversal_state = "vwap_reclaimed_put"
                self._reversal_pullback_high = ctx.current_high
            return None

        if self._reversal_state == "vwap_reclaimed_put":
            if ctx.current_low < self._reversal_lod:
                self._reversal_state = "idle"
                return None
            if ctx.current_close < ctx.vwap_value:
                self._reversal_state = "reclaimed_put"
                return None
            # VWAP pullback held
            near_vwap = ctx.current_low <= ctx.vwap_value * Decimal("1.003")
            if near_vwap and ctx.current_close > ctx.vwap_value:
                # Entry: break above pullback bar high
                if ctx.current_close > self._reversal_pullback_high:
                    stop = self._reversal_lod - ctx.atr_val * self.settings.atr_stop_buffer
                    stop_dist = signal_spot - stop
                    if stop_dist <= 0 or (ctx.atr_val > 0 and stop_dist > ctx.atr_val * self.settings.max_stop_atr_ratio):
                        self._reversal_state = "idle"
                        return None
                    r_val = self._compute_r(signal_spot, stop)
                    self._reversal_state = "idle"
                    return Signal(
                        direction=Direction.CALL,
                        bar_end=current.end,
                        spot=signal_spot,
                        strategy="reversal",
                        stop_price=stop,
                        atr=ctx.atr_val,
                        r_value=r_val,
                        breakout_level=ctx.orl,
                        vwap=ctx.vwap_value,
                        indicators={
                            "strategy": "reversal",
                            "orh": str(ctx.orh),
                            "orl": str(ctx.orl),
                            "lod": str(self._reversal_lod),
                            "vwap": str(ctx.vwap_value),
                            "macd_hist": str(ctx.macd_hist),
                            "adx": str(ctx.adx_val),
                            "atr": str(ctx.atr_val),
                        },
                    )
            self._reversal_pullback_high = max(self._reversal_pullback_high, ctx.current_high)
            return None

        # Bearish reversal path (mirror)
        if self._reversal_state == "breakdown_call":
            self._reversal_hod = max(self._reversal_hod, ctx.current_high)
            elapsed = len([
                b for b in today_5m if b.end > (self._reversal_breakdown_end or current.end)
            ])
            if elapsed > 3 and ctx.current_close > ctx.orh:
                self._reversal_state = "idle"
                return None
            if ctx.current_close < ctx.orh:
                self._reversal_state = "reclaimed_call"
            return None

        if self._reversal_state == "reclaimed_call":
            if ctx.current_high > self._reversal_hod:
                self._reversal_state = "idle"
                return None
            has_lower_high = ctx.current_high < self._reversal_hod
            macd_exhaustion = (
                ctx.macd_hist < ctx.macd_hist_prev
                or (ctx.macd_hist > 0 and ctx.macd_hist < ctx.macd_hist_prev)
            )
            if has_lower_high and macd_exhaustion and ctx.current_close < ctx.vwap_value:
                self._reversal_state = "vwap_reclaimed_call"
                self._reversal_pullback_low = ctx.current_low
            return None

        if self._reversal_state == "vwap_reclaimed_call":
            if ctx.current_high > self._reversal_hod:
                self._reversal_state = "idle"
                return None
            if ctx.current_close > ctx.vwap_value:
                self._reversal_state = "reclaimed_call"
                return None
            near_vwap = ctx.current_high >= ctx.vwap_value * Decimal("0.997")
            if near_vwap and ctx.current_close < ctx.vwap_value:
                if ctx.current_close < self._reversal_pullback_low:
                    stop = self._reversal_hod + ctx.atr_val * self.settings.atr_stop_buffer
                    stop_dist = stop - signal_spot
                    if stop_dist <= 0 or (ctx.atr_val > 0 and stop_dist > ctx.atr_val * self.settings.max_stop_atr_ratio):
                        self._reversal_state = "idle"
                        return None
                    r_val = self._compute_r(signal_spot, stop)
                    self._reversal_state = "idle"
                    return Signal(
                        direction=Direction.PUT,
                        bar_end=current.end,
                        spot=signal_spot,
                        strategy="reversal",
                        stop_price=stop,
                        atr=ctx.atr_val,
                        r_value=r_val,
                        breakout_level=ctx.orh,
                        vwap=ctx.vwap_value,
                        indicators={
                            "strategy": "reversal",
                            "orh": str(ctx.orh),
                            "orl": str(ctx.orl),
                            "hod": str(self._reversal_hod),
                            "vwap": str(ctx.vwap_value),
                            "macd_hist": str(ctx.macd_hist),
                            "adx": str(ctx.adx_val),
                            "atr": str(ctx.atr_val),
                        },
                    )
            self._reversal_pullback_low = min(self._reversal_pullback_low, ctx.current_low)
            return None

        return None

    def _check_soft_conditions(
        self, ctx: MarketContext, bars_5m: Sequence[Bar], direction: Direction
    ) -> bool:
        """Check soft confirmation. Hard conditions are the real gate; soft is quality."""
        score = 0

        # 1. Directional momentum in recent bars
        if len(bars_5m) >= 3:
            recent = bars_5m[-3:]
            if direction == Direction.CALL:
                if recent[-1].high > recent[-2].high or recent[-1].close > recent[-2].close:
                    score += 1
            else:
                if recent[-1].low < recent[-2].low or recent[-1].close < recent[-2].close:
                    score += 1

        # 2. Volume above average on breakout bar
        if len(bars_5m) >= 6:
            avg_vol = sum(b.volume for b in bars_5m[-6:-1]) / 5
            if avg_vol > 0 and bars_5m[-1].volume > avg_vol:
                score += 1

        # 3. MACD histogram confirmation (only if data available)
        if ctx.macd_hist != ZERO or ctx.macd_hist_prev != ZERO:
            if direction == Direction.CALL:
                if ctx.macd_hist > 0 or ctx.macd_hist > ctx.macd_hist_prev:
                    score += 1
            else:
                if ctx.macd_hist < 0 or ctx.macd_hist < ctx.macd_hist_prev:
                    score += 1

        # 4. ADX indicates trending (only if data available)
        if ctx.adx_val > 0:
            if ctx.adx_val > self.settings.trend_adx_min:
                score += 1

        # Require at least 1 soft confirmation
        return score >= 1

    def _compute_stop(
        self, ctx: MarketContext, bars_5m: Sequence[Bar], direction: Direction
    ) -> Decimal | None:
        """Compute stop at recent swing point + ATR buffer."""
        if len(bars_5m) < 3:
            return None
        atr_buffer = ctx.atr_val * self.settings.atr_stop_buffer

        if direction == Direction.CALL:
            recent_lows = [b.low for b in bars_5m[-5:]]
            swing_low = min(recent_lows)
            return swing_low - atr_buffer
        else:
            recent_highs = [b.high for b in bars_5m[-5:]]
            swing_high = max(recent_highs)
            return swing_high + atr_buffer

    def _compute_r(self, entry: Decimal, stop: Decimal) -> Decimal:
        """Compute 1R value (risk per share)."""
        return abs(entry - stop)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def strategy_from_settings(settings) -> StrategyEngine:
    """Create strategy engine from settings."""
    return StrategyEngine(settings)
