"""Technical indicator functions and market data infrastructure.

Reusable building blocks for strategy implementations:
- Indicator functions (EMA, MACD, VWAP, ATR, ADX, RSI, Bollinger, RVOL)
- BarAggregator (1-min → 5-min K-line aggregation)
- MarketContext (indicator snapshot dataclass)
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from .domain import (
    Bar,
    Direction,
    ExitDecision,
    ExitReason,
    MarketState,
    Position,
    Signal,
)
from .policy import RULES

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
    typical_volume = sum((b.high + b.low + b.close) / Decimal(3) * Decimal(b.volume) for b in bars)
    return typical_volume / Decimal(total_vol)


def vwap_slope(bars: Sequence[Bar], lookback: int = 3) -> Decimal:
    """VWAP slope over last `lookback` completed bars."""
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


def rsi(values: Sequence[Decimal], period: int = 14) -> Decimal:
    """Wilder RSI; returns neutral 50 while warming up."""
    if period < 2:
        raise ValueError("RSI period must be >= 2")
    if len(values) < period + 1:
        return Decimal("50")
    gains: list[Decimal] = []
    losses: list[Decimal] = []
    for prev, curr in zip(values, values[1:], strict=False):
        change = curr - prev
        gains.append(max(change, ZERO))
        losses.append(max(-change, ZERO))
    avg_gain = sum(gains[:period], ZERO) / Decimal(period)
    avg_loss = sum(losses[:period], ZERO) / Decimal(period)
    for gain, loss in zip(gains[period:], losses[period:], strict=True):
        avg_gain = (avg_gain * Decimal(period - 1) + gain) / Decimal(period)
        avg_loss = (avg_loss * Decimal(period - 1) + loss) / Decimal(period)
    if avg_loss == ZERO:
        return Decimal("100") if avg_gain > ZERO else Decimal("50")
    rs = avg_gain / avg_loss
    return Decimal("100") - Decimal("100") / (Decimal(1) + rs)


def bollinger_bands(
    values: Sequence[Decimal], period: int = 20, std_dev: Decimal = Decimal("2")
) -> tuple[Decimal, Decimal, Decimal]:
    """Return (upper, middle, lower) Bollinger Bands."""
    if period < 2 or len(values) < period:
        mid = values[-1] if values else ZERO
        return mid, mid, mid
    window = values[-period:]
    middle = sum(window, ZERO) / Decimal(period)
    variance = sum((v - middle) ** 2 for v in window) / Decimal(period)
    deviation = variance.sqrt()
    return middle + std_dev * deviation, middle, middle - std_dev * deviation


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
    """Snapshot of indicators at the strategy's active signal timeframe."""

    structure_high: Decimal = ZERO
    structure_low: Decimal = ZERO
    vwap_value: Decimal = ZERO
    vwap_slope_val: Decimal = ZERO
    ema9: Decimal = ZERO
    ema9_prev: Decimal = ZERO
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
    bar_end: datetime | None = None
    rsi_val: Decimal = Decimal("50")
    rsi_prev: Decimal = Decimal("50")
    boll_upper: Decimal = ZERO
    boll_middle: Decimal = ZERO
    boll_lower: Decimal = ZERO
    market_state: MarketState = MarketState.UNKNOWN


@dataclass(slots=True)
class ReversalWatch:
    direction: Direction
    started_at: datetime
    extreme: Decimal
    reclaim_level: Decimal
    saw_rsi_extreme: bool


class MarketStateClassifier:
    """Deterministic market classification using only completed observations."""

    def classify(
        self,
        ctx: MarketContext,
        today_1m: Sequence[Bar],
    ) -> MarketState:
        if ctx.bar_time < RULES.entry_start:
            return MarketState.OBSERVATION
        if ctx.atr_val <= 0 or len(today_1m) < 20:
            return MarketState.UNKNOWN
        recent = list(today_1m[-20:])
        price_span = max(bar.high for bar in recent) - min(bar.low for bar in recent)
        progressive_vwaps: list[Decimal] = []
        start_index = max(1, len(today_1m) - 20)
        for index in range(start_index, len(today_1m) + 1):
            progressive_vwaps.append(vwap(today_1m[:index]))
        crosses = 0
        recent_for_cross = today_1m[-len(progressive_vwaps) :]
        for index in range(1, min(len(progressive_vwaps), len(recent_for_cross))):
            before = recent_for_cross[index - 1].close >= progressive_vwaps[index - 1]
            after = recent_for_cross[index].close >= progressive_vwaps[index]
            crosses += before != after
        score = 0
        score += price_span <= RULES.range_price_span_atr * ctx.atr_val
        score += ctx.adx_val > 0 and ctx.adx_val <= RULES.range_adx_max
        score += (
            ctx.atr_val > 0
            and abs(ctx.ema9 - ctx.ema20) <= RULES.range_ema_distance_atr * ctx.atr_val
        )
        score += (
            ctx.atr_val > 0
            and abs(ctx.vwap_slope_val) <= RULES.range_vwap_change_atr * ctx.atr_val
        )
        score += crosses >= 3
        close_path = sum(
            abs(current.close - previous.close)
            for previous, current in zip(recent, recent[1:], strict=False)
        )
        directional_efficiency = (
            abs(recent[-1].close - recent[0].close) / close_path
            if close_path > 0
            else ZERO
        )
        no_directional_impulse = (
            directional_efficiency <= Decimal("0.45")
            and recent[-1].high - recent[-1].low <= Decimal("2") * ctx.atr_val
        )
        away_from_previous_low = (
            ctx.prev_day_low <= 0
            or ctx.atr_val <= 0
            or ctx.current_close - ctx.prev_day_low
            >= RULES.range_prior_low_distance_atr * ctx.atr_val
        )
        return (
            MarketState.RANGE
            if score >= 4 and no_directional_impulse and away_from_previous_low
            else MarketState.UNKNOWN
        )


class StrategyEngine:
    """QQQ state-machine strategy specified by STRATEGY.md."""

    def __init__(self, settings) -> None:
        self.settings = settings
        self.classifier = MarketStateClassifier()
        self.last_signal_bar: datetime | None = None
        self.last_context: MarketContext | None = None
        self.last_state = MarketState.UNKNOWN
        self._trading_date: date | None = None
        self._bullish_reversal: ReversalWatch | None = None
        self._bearish_reversal: ReversalWatch | None = None

    @staticmethod
    def _rth(bar: Bar) -> bool:
        local = bar.start.astimezone(NY_TZ)
        return time(9, 30) <= local.time().replace(tzinfo=None) < time(16, 0)

    @staticmethod
    def _safe_ema(values: Sequence[Decimal], period: int) -> Decimal:
        return ema(values, period) if len(values) >= period else (values[-1] if values else ZERO)

    @staticmethod
    def _safe_macd(
        values: Sequence[Decimal], fast: int, slow: int, signal_period: int
    ) -> tuple[Decimal, Decimal]:
        if len(values) < slow + signal_period:
            return ZERO, ZERO
        series = macd_histogram_series(values, fast, slow, signal_period)
        return series[-1], series[-2] if len(series) > 1 else ZERO

    @staticmethod
    def _previous_session(bars: Sequence[Bar], trading_day: date) -> list[Bar]:
        dates = sorted(
            {
                bar.start.astimezone(NY_TZ).date()
                for bar in bars
                if StrategyEngine._rth(bar)
                and bar.start.astimezone(NY_TZ).date() < trading_day
            }
        )
        if not dates:
            return []
        previous = dates[-1]
        return [
            bar
            for bar in bars
            if StrategyEngine._rth(bar) and bar.start.astimezone(NY_TZ).date() == previous
        ]

    def _context(self, bars: Sequence[Bar]) -> tuple[MarketContext, list[Bar]] | None:
        visible = sorted((bar for bar in bars if bar.complete), key=lambda item: item.end)
        if not visible:
            return None
        current = visible[-1]
        trading_day = current.end.astimezone(NY_TZ).date()
        if trading_day != self._trading_date:
            self._trading_date = trading_day
            self._bullish_reversal = None
            self._bearish_reversal = None
            self.last_signal_bar = None
        rth = [bar for bar in visible if self._rth(bar)]
        today = [bar for bar in rth if bar.start.astimezone(NY_TZ).date() == trading_day]
        if not today:
            return None
        previous = self._previous_session(rth, trading_day)
        prior_structure = today[-(RULES.structure_lookback + 1) : -1]
        structure_high = max((bar.high for bar in prior_structure), default=ZERO)
        structure_low = min((bar.low for bar in prior_structure), default=ZERO)
        rolling_1m = rth[-500:]
        closes = [bar.close for bar in rolling_1m]
        ema_fast = self._safe_ema(closes, self.settings.ema_fast_period)
        ema_fast_prev = self._safe_ema(closes[:-1], self.settings.ema_fast_period)
        ema_slow = self._safe_ema(closes, self.settings.ema_slow_period)
        macd_now, macd_prev = self._safe_macd(
            closes,
            self.settings.macd_1m_fast,
            self.settings.macd_1m_slow,
            self.settings.macd_1m_signal,
        )
        rsi_now = rsi(closes, self.settings.rsi_period)
        rsi_before = (
            rsi(closes[:-1], self.settings.rsi_period)
            if len(closes) > 1
            else Decimal("50")
        )
        upper, middle, lower = bollinger_bands(
            closes, self.settings.bollinger_period, self.settings.bollinger_stddev
        )
        atr_now = (
            atr(rolling_1m, self.settings.atr_period)
            if len(rolling_1m) >= self.settings.atr_period + 1
            else ZERO
        )
        adx_now = adx(rolling_1m, self.settings.adx_period)
        current_time = current.end.astimezone(NY_TZ).time().replace(tzinfo=None)
        current_vwap = vwap(today)
        old_vwap = vwap(today[:-5]) if len(today) > 5 else current_vwap
        ctx = MarketContext(
            structure_high=structure_high,
            structure_low=structure_low,
            vwap_value=current_vwap,
            vwap_slope_val=current_vwap - old_vwap,
            ema9=ema_fast,
            ema9_prev=ema_fast_prev,
            ema20=ema_slow,
            macd_hist=macd_now,
            macd_hist_prev=macd_prev,
            adx_val=adx_now,
            atr_val=atr_now,
            prev_day_high=max((bar.high for bar in previous), default=ZERO),
            prev_day_low=min((bar.low for bar in previous), default=ZERO),
            prev_close=previous[-1].close if previous else ZERO,
            day_high=max(bar.high for bar in today),
            day_low=min(bar.low for bar in today),
            current_close=current.close,
            current_high=current.high,
            current_low=current.low,
            current_open=current.open,
            current_volume=current.volume,
            bar_time=current_time,
            bar_end=current.end,
            rsi_val=rsi_now,
            rsi_prev=rsi_before,
            boll_upper=upper,
            boll_middle=middle,
            boll_lower=lower,
        )
        return ctx, today

    def evaluate(self, bars_1m: Sequence[Bar], spot: Decimal | None = None) -> Signal | None:
        computed = self._context(bars_1m)
        if computed is None:
            return None
        ctx, today = computed
        self.last_context = ctx
        if ctx.bar_time < RULES.entry_start:
            ctx.market_state = MarketState.OBSERVATION
            self.last_state = ctx.market_state
            return None
        if ctx.bar_time >= RULES.entry_end or ctx.atr_val <= 0 or len(today) < 3:
            ctx.market_state = MarketState.UNKNOWN
            self.last_state = ctx.market_state
            return None
        signal = self._evaluate_reversal(ctx, today, spot)
        if signal is None:
            signal = self._evaluate_retest(ctx, today, spot)
        if signal is None:
            signal = self._evaluate_trend(ctx, today, spot)
        if signal is None:
            ctx.market_state = self.classifier.classify(ctx, today)
            if ctx.market_state is MarketState.RANGE:
                signal = self._evaluate_range(ctx, today, spot)
        if signal is not None and signal.bar_end == self.last_signal_bar:
            signal = None
        if signal is not None:
            self.last_signal_bar = signal.bar_end
            ctx.market_state = signal.market_state
        self.last_state = ctx.market_state
        return signal

    def _signal(
        self,
        ctx: MarketContext,
        direction: Direction,
        strategy: str,
        state: MarketState,
        stop: Decimal,
        spot: Decimal | None,
        structure_level: Decimal | None = None,
        **extra: str,
    ) -> Signal | None:
        entry = spot if spot is not None else ctx.current_close
        distance = abs(entry - stop)
        if distance <= 0 or distance > RULES.max_stop_atr_ratio * ctx.atr_val:
            return None
        indicators = {
            "strategy": strategy,
            "market_state": state.value,
            "structure_high": str(ctx.structure_high),
            "structure_low": str(ctx.structure_low),
            "vwap": str(ctx.vwap_value),
            "ema9": str(ctx.ema9),
            "ema9_prev": str(ctx.ema9_prev),
            "ema20": str(ctx.ema20),
            "macd_hist": str(ctx.macd_hist),
            "adx": str(ctx.adx_val),
            "atr": str(ctx.atr_val),
            "rsi": str(ctx.rsi_val),
            "boll_upper": str(ctx.boll_upper),
            "boll_middle": str(ctx.boll_middle),
            "boll_lower": str(ctx.boll_lower),
            "underlying_stop": str(stop),
            **extra,
        }
        return Signal(
            direction=direction,
            bar_end=ctx.bar_end or datetime.now(tz=NY_TZ),
            spot=entry,
            strategy=strategy,
            market_state=state,
            stop_price=stop,
            atr=ctx.atr_val,
            r_value=distance,
            breakout_level=structure_level,
            vwap=ctx.vwap_value,
            indicators=indicators,
        )

    def _evaluate_trend(
        self,
        ctx: MarketContext,
        today: Sequence[Bar],
        spot: Decimal | None,
    ) -> Signal | None:
        if len(today) < 3:
            return None
        structure_window = list(
            today[-(RULES.structure_lookback + 1) : -1]
        )
        if len(structure_window) < 2:
            return None
        resistance = max(bar.high for bar in structure_window)
        support = min(bar.low for bar in structure_window)
        recent = list(today[-3:])
        up_structure = sum(
            recent[index].high > recent[index - 1].high
            or recent[index].low > recent[index - 1].low
            for index in range(1, len(recent))
        )
        down_structure = sum(
            recent[index].low < recent[index - 1].low
            or recent[index].high < recent[index - 1].high
            for index in range(1, len(recent))
        )
        previous_volumes = [bar.volume for bar in today[-21:-1]]
        average_volume = (
            Decimal(sum(previous_volumes)) / Decimal(len(previous_volumes))
            if previous_volumes
            else ZERO
        )
        volume_confirm = average_volume > 0 and Decimal(ctx.current_volume) >= (
            RULES.breakout_volume_ratio * average_volume
        )
        call_ema_ok = ctx.ema9 > ctx.ema20 or (
            RULES.early_ema_tolerance_atr > 0
            and ctx.ema9 > ctx.ema9_prev
            and ctx.ema9
            >= ctx.ema20 - RULES.early_ema_tolerance_atr * ctx.atr_val
        )
        put_ema_ok = ctx.ema9 < ctx.ema20 or (
            RULES.early_ema_tolerance_atr > 0
            and ctx.ema9 < ctx.ema9_prev
            and ctx.ema9
            <= ctx.ema20 + RULES.early_ema_tolerance_atr * ctx.atr_val
        )
        call_confirmations = sum(
            (
                ctx.macd_hist > 0 and ctx.macd_hist >= ctx.macd_hist_prev,
                ctx.adx_val >= RULES.trend_adx_min,
                volume_confirm,
                ctx.current_close > recent[-2].high,
            )
        )
        put_confirmations = sum(
            (
                ctx.macd_hist < 0 and ctx.macd_hist <= ctx.macd_hist_prev,
                ctx.adx_val >= RULES.trend_adx_min,
                volume_confirm,
                ctx.current_close < recent[-2].low,
            )
        )
        vwap_distance_limit = RULES.max_vwap_distance_atr * ctx.atr_val
        call_prior_ok = (
            ctx.prev_day_high <= 0
            or ctx.current_close
            > ctx.prev_day_high + RULES.structure_break_atr * ctx.atr_val
            or ctx.prev_day_high - ctx.current_close >= RULES.prior_level_distance_atr * ctx.atr_val
        )
        if (
            ctx.current_close
            > resistance + RULES.structure_break_atr * ctx.atr_val
            and ctx.current_close > ctx.vwap_value
            and ctx.vwap_slope_val > 0
            and call_ema_ok
            and up_structure >= RULES.trend_structure_confirmations
            and ctx.rsi_val >= RULES.trend_call_rsi_min
            and ctx.rsi_val < self.settings.rsi_overbought
            and (not RULES.require_directional_macd or ctx.macd_hist > 0)
            and abs(ctx.macd_hist) >= RULES.min_macd_hist_atr * ctx.atr_val
            and ctx.current_close - ctx.vwap_value <= vwap_distance_limit
            and call_confirmations >= 2
            and call_prior_ok
        ):
            stop = max(
                resistance - RULES.stop_atr_buffer * ctx.atr_val,
                ctx.current_low - RULES.stop_atr_buffer * ctx.atr_val,
            )
            return self._signal(
                ctx,
                Direction.CALL,
                "trend",
                MarketState.TREND_UP,
                stop,
                spot,
                resistance,
            )
        put_prior_ok = (
            ctx.prev_day_low <= 0
            or ctx.current_close
            < ctx.prev_day_low - RULES.structure_break_atr * ctx.atr_val
            or ctx.current_close - ctx.prev_day_low >= RULES.prior_level_distance_atr * ctx.atr_val
        )
        if (
            ctx.current_close
            < support - RULES.structure_break_atr * ctx.atr_val
            and ctx.current_close < ctx.vwap_value
            and ctx.vwap_slope_val < 0
            and put_ema_ok
            and down_structure >= RULES.trend_structure_confirmations
            and ctx.rsi_val > self.settings.rsi_oversold
            and ctx.rsi_val <= RULES.trend_put_rsi_max
            and (not RULES.require_directional_macd or ctx.macd_hist < 0)
            and abs(ctx.macd_hist) >= RULES.min_macd_hist_atr * ctx.atr_val
            and ctx.vwap_value - ctx.current_close <= vwap_distance_limit
            and put_confirmations >= 2
            and put_prior_ok
        ):
            stop = min(
                support + RULES.stop_atr_buffer * ctx.atr_val,
                ctx.current_high + RULES.stop_atr_buffer * ctx.atr_val,
            )
            return self._signal(
                ctx,
                Direction.PUT,
                "trend",
                MarketState.TREND_DOWN,
                stop,
                spot,
                support,
            )
        return None

    def _evaluate_retest(
        self,
        ctx: MarketContext,
        today: Sequence[Bar],
        spot: Decimal | None,
    ) -> Signal | None:
        """Enter a confirmed higher-low/lower-high continuation without fixed OR levels."""
        required = RULES.retest_lookback + 1
        if len(today) < required or ctx.atr_val <= 0:
            return None
        current = today[-1]
        pullback = list(today[-required:-1])
        pullback_low = min(bar.low for bar in pullback)
        pullback_high = max(bar.high for bar in pullback)
        low_index = min(
            range(len(pullback)),
            key=lambda index: pullback[index].low,
        )
        high_index = max(
            range(len(pullback)),
            key=lambda index: pullback[index].high,
        )
        fast_retest = ctx.bar_time <= RULES.fast_retest_end
        low_stabilized = fast_retest or low_index <= len(pullback) - 3
        high_stabilized = fast_retest or high_index <= len(pullback) - 3
        prior_close_high = max(bar.close for bar in pullback)
        prior_close_low = min(bar.close for bar in pullback)
        candle_range = current.high - current.low
        if candle_range <= 0:
            return None
        upper_half_close = current.close >= current.low + Decimal("0.5") * candle_range
        lower_half_close = current.close <= current.low + Decimal("0.5") * candle_range
        day_span = ctx.day_high - ctx.day_low
        day_midpoint = (ctx.day_high + ctx.day_low) / Decimal(2)
        pullback_down = (
            prior_close_high - pullback[-1].close
            >= RULES.retest_min_excursion_atr * ctx.atr_val
        )
        pullback_up = (
            pullback[-1].close - prior_close_low
            >= RULES.retest_min_excursion_atr * ctx.atr_val
        )
        call_ema_ok = (
            ctx.ema9
            >= ctx.ema20 - RULES.retest_ema_tolerance_atr * ctx.atr_val
        )
        put_ema_ok = (
            ctx.ema9
            <= ctx.ema20 + RULES.retest_ema_tolerance_atr * ctx.atr_val
        )
        reclaim_buffer = RULES.structure_break_atr * ctx.atr_val
        if (
            day_span >= Decimal("2") * ctx.atr_val
            and ctx.current_close > day_midpoint
            and pullback_down
            and low_stabilized
            and ctx.current_low > pullback_low
            and upper_half_close
            and ctx.current_close >= pullback[-1].high - reclaim_buffer
            and ctx.macd_hist > ctx.macd_hist_prev
            and ctx.macd_hist > 0
            and ctx.rsi_val >= RULES.retest_call_rsi_min
            and ctx.rsi_val < self.settings.rsi_overbought
            and call_ema_ok
            and ctx.current_close
            >= ctx.vwap_value - RULES.retest_vwap_tolerance_atr * ctx.atr_val
        ):
            stop = pullback_low - RULES.stop_atr_buffer * ctx.atr_val
            return self._signal(
                ctx,
                Direction.CALL,
                "trend_retest",
                MarketState.TREND_RETEST_UP,
                stop,
                spot,
                pullback[-1].high,
                pullback_low=str(pullback_low),
            )
        if (
            day_span >= Decimal("2") * ctx.atr_val
            and ctx.current_close < day_midpoint
            and pullback_up
            and high_stabilized
            and ctx.current_high < pullback_high
            and lower_half_close
            and ctx.current_close <= pullback[-1].low + reclaim_buffer
            and ctx.macd_hist < ctx.macd_hist_prev
            and ctx.macd_hist < 0
            and ctx.rsi_val <= RULES.retest_put_rsi_max
            and ctx.rsi_val > self.settings.rsi_oversold
            and put_ema_ok
            and ctx.current_close
            <= ctx.vwap_value + RULES.retest_vwap_tolerance_atr * ctx.atr_val
        ):
            stop = pullback_high + RULES.stop_atr_buffer * ctx.atr_val
            return self._signal(
                ctx,
                Direction.PUT,
                "trend_retest",
                MarketState.TREND_RETEST_DOWN,
                stop,
                spot,
                pullback[-1].low,
                pullback_high=str(pullback_high),
            )
        return None

    def _evaluate_reversal(
        self, ctx: MarketContext, today: Sequence[Bar], spot: Decimal | None
    ) -> Signal | None:
        assert ctx.bar_end is not None
        if len(today) < RULES.structure_lookback + 2:
            return None
        level_window = list(
            today[-(RULES.structure_lookback + 2) : -2]
        )
        support = min(bar.low for bar in level_window)
        resistance = max(bar.high for bar in level_window)
        two_below = all(bar.close < support for bar in today[-2:])
        two_above = all(bar.close > resistance for bar in today[-2:])
        if self._bullish_reversal is None and (
            two_below
            or ctx.current_low
            < support - RULES.structure_excursion_atr * ctx.atr_val
        ):
            self._bullish_reversal = ReversalWatch(
                Direction.CALL,
                ctx.bar_end,
                ctx.current_low,
                support,
                ctx.rsi_val <= self.settings.rsi_oversold,
            )
        if self._bearish_reversal is None and (
            two_above
            or ctx.current_high
            > resistance + RULES.structure_excursion_atr * ctx.atr_val
        ):
            self._bearish_reversal = ReversalWatch(
                Direction.PUT,
                ctx.bar_end,
                ctx.current_high,
                resistance,
                ctx.rsi_val >= self.settings.rsi_overbought,
            )
        bull = self._bullish_reversal
        if bull is not None:
            prior_extreme = bull.extreme
            bull.saw_rsi_extreme |= ctx.rsi_val <= self.settings.rsi_oversold
            if ctx.bar_end - bull.started_at > timedelta(minutes=RULES.reversal_timeout_minutes):
                self._bullish_reversal = None
            elif (
                bull.saw_rsi_extreme
                and ctx.current_close > bull.reclaim_level
                and ctx.rsi_prev <= self.settings.rsi_oversold
                and ctx.rsi_val > self.settings.rsi_oversold
                and ctx.macd_hist > ctx.macd_hist_prev
                and ctx.current_low >= prior_extreme
                and ctx.current_close
                >= ctx.current_low + Decimal("0.5") * (ctx.current_high - ctx.current_low)
                and abs(ctx.current_close - ctx.vwap_value)
                <= RULES.max_vwap_distance_atr * ctx.atr_val
            ):
                self._bullish_reversal = None
                stop = prior_extreme - RULES.stop_atr_buffer * ctx.atr_val
                size_factor = "1" if ctx.current_close >= ctx.vwap_value else "0.5"
                return self._signal(
                    ctx,
                    Direction.CALL,
                    "reversal",
                    MarketState.REVERSAL_UP,
                    stop,
                    spot,
                    bull.reclaim_level,
                    size_factor=size_factor,
                )
            else:
                bull.extreme = min(bull.extreme, ctx.current_low)
        bear = self._bearish_reversal
        if bear is not None:
            prior_extreme = bear.extreme
            bear.saw_rsi_extreme |= ctx.rsi_val >= self.settings.rsi_overbought
            if ctx.bar_end - bear.started_at > timedelta(minutes=RULES.reversal_timeout_minutes):
                self._bearish_reversal = None
            elif (
                bear.saw_rsi_extreme
                and ctx.current_close < bear.reclaim_level
                and ctx.rsi_prev >= self.settings.rsi_overbought
                and ctx.rsi_val < self.settings.rsi_overbought
                and ctx.macd_hist < ctx.macd_hist_prev
                and ctx.current_high <= prior_extreme
                and ctx.current_close
                <= ctx.current_low + Decimal("0.5") * (ctx.current_high - ctx.current_low)
                and abs(ctx.current_close - ctx.vwap_value)
                <= RULES.max_vwap_distance_atr * ctx.atr_val
            ):
                self._bearish_reversal = None
                stop = prior_extreme + RULES.stop_atr_buffer * ctx.atr_val
                size_factor = "1" if ctx.current_close <= ctx.vwap_value else "0.5"
                return self._signal(
                    ctx,
                    Direction.PUT,
                    "reversal",
                    MarketState.REVERSAL_DOWN,
                    stop,
                    spot,
                    bear.reclaim_level,
                    size_factor=size_factor,
                )
            else:
                bear.extreme = max(bear.extreme, ctx.current_high)
        return None

    def _evaluate_range(
        self, ctx: MarketContext, today: Sequence[Bar], spot: Decimal | None
    ) -> Signal | None:
        touched_lower = ctx.current_low <= ctx.boll_lower
        rsi_ready = ctx.rsi_val <= self.settings.rsi_oversold or (
            ctx.rsi_prev <= self.settings.rsi_oversold
            and ctx.rsi_val > self.settings.rsi_oversold
        )
        if (
            touched_lower
            and ctx.current_close > ctx.boll_lower
            and rsi_ready
            and ctx.macd_hist > ctx.macd_hist_prev
        ):
            recent_low = min(bar.low for bar in today[-5:])
            stop = recent_low - RULES.stop_atr_buffer * ctx.atr_val
            return self._signal(
                ctx, Direction.CALL, "range", MarketState.RANGE, stop, spot
            )
        return None

    def bar_exit_decision(self, position: Position) -> ExitDecision | None:
        ctx = self.last_context
        if ctx is None:
            return None
        if position.underlying_stop is not None:
            stopped = (
                ctx.current_close <= position.underlying_stop
                if position.direction is Direction.CALL
                else ctx.current_close >= position.underlying_stop
            )
            if stopped:
                return ExitDecision(ExitReason.STRUCTURE_STOP, position.quantity)
        if position.strategy_name == "range":
            if ctx.current_close >= ctx.boll_upper:
                return ExitDecision(ExitReason.BOLLINGER_UPPER, position.quantity)
            if (
                not position.range_middle_taken
                and ctx.current_close >= ctx.boll_middle
            ):
                quantity = (
                    position.quantity
                    if position.quantity == 1
                    else (position.quantity + 1) // 2
                )
                return ExitDecision(ExitReason.BOLLINGER_MIDDLE, quantity, position.entry_price)
            recent = ctx.current_close < ctx.boll_lower
            if (
                recent
                and ctx.rsi_val < self.settings.rsi_oversold
                and ctx.macd_hist < ctx.macd_hist_prev
            ):
                return ExitDecision(ExitReason.STATE_INVALIDATION, position.quantity)
        if ctx.bar_time >= RULES.reduce_at:
            if position.direction is Direction.CALL:
                invalid = (
                    ctx.current_close < ctx.vwap_value and ctx.macd_hist < 0
                ) or ctx.current_close < ctx.ema20 - RULES.range_vwap_change_atr * ctx.atr_val
            else:
                invalid = (
                    ctx.current_close > ctx.vwap_value and ctx.macd_hist > 0
                ) or ctx.current_close > ctx.ema20 + RULES.range_vwap_change_atr * ctx.atr_val
            if invalid:
                return ExitDecision(ExitReason.VWAP_CROSS, position.quantity)
        return None


def strategy_from_settings(settings) -> StrategyEngine:
    if getattr(settings, "strategy_profile", "dynamic") == "timed_trend":
        from .timed_strategy import TimedTrendStrategy

        return TimedTrendStrategy(settings)
    return StrategyEngine(settings)
