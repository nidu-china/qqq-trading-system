from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, time
from decimal import Decimal

from .config import NY_TZ
from .domain import (
    Bar,
    Direction,
    ExitDecision,
    ExitReason,
    MarketState,
    Position,
    Signal,
)
from .indicators import (
    MarketContext,
    bollinger_bands,
    macd_histogram,
    macd_histogram_series,
    rsi,
)
from .policy import RULES

ZERO = Decimal(0)


class StrategyEngine:
    """One-minute BOLL/MACD strategy with fixed time partitions."""

    def __init__(self, settings) -> None:
        self.settings = settings
        self.last_signal_bar: datetime | None = None
        self.last_context: MarketContext | None = None
        self.last_state = MarketState.UNKNOWN
        self._last_today_1m: list[Bar] = []
        self._last_boll_middle_by_end: dict[datetime, Decimal] = {}
        self._last_macd_hist_by_end: dict[datetime, Decimal] = {}

    @staticmethod
    def _rth(bar: Bar) -> bool:
        local_time = bar.start.astimezone(NY_TZ).time().replace(tzinfo=None)
        return time(9, 30) <= local_time < time(16, 0)

    def _cache_reversal_indicators(
        self,
        bars: Sequence[Bar],
        closes: Sequence[Decimal],
    ) -> None:
        """Cache BOLL middle and MACD histogram for the latest five bars."""
        self._last_boll_middle_by_end = {}
        self._last_macd_hist_by_end = {}
        cache_size = max(
            RULES.timed_reversal_window,
            RULES.timed_trend_cross_lookback,
        )
        first = max(0, len(bars) - cache_size)
        macd_required = (
            RULES.timed_macd_slow + RULES.timed_macd_signal - 1
        )
        histogram = (
            macd_histogram_series(
                closes,
                RULES.timed_macd_fast,
                RULES.timed_macd_slow,
                RULES.timed_macd_signal,
            )
            if len(closes) >= macd_required
            else []
        )
        for index in range(first, len(bars)):
            prefix = closes[: index + 1]
            if len(prefix) >= RULES.timed_boll_period:
                _, middle, _ = bollinger_bands(
                    prefix,
                    RULES.timed_boll_period,
                    RULES.timed_boll_stddev,
                )
                self._last_boll_middle_by_end[bars[index].end] = middle
            histogram_index = index - macd_required + 1
            if histogram_index >= 0:
                self._last_macd_hist_by_end[bars[index].end] = histogram[
                    histogram_index
                ]

    @staticmethod
    def _prior_boll_middle_crosses(trend_sides: Sequence[bool]) -> int:
        """Count completed crosses before the current bar's transition."""
        prior_sides = trend_sides[:-1]
        return sum(
            current_side != previous_side
            for previous_side, current_side in zip(
                prior_sides,
                prior_sides[1:],
                strict=False,
            )
        )

    def _one_minute_context(
        self, bars_1m: Sequence[Bar]
    ) -> tuple[MarketContext, list[Bar]] | None:
        visible = sorted(
            (bar for bar in bars_1m if bar.complete and self._rth(bar)),
            key=lambda item: item.end,
        )
        if not visible:
            return None
        current = visible[-1]
        trading_day = current.end.astimezone(NY_TZ).date()
        today = [
            bar
            for bar in visible
            if bar.start.astimezone(NY_TZ).date() == trading_day
        ]
        indicator_bars = visible[-500:]
        closes = [bar.close for bar in indicator_bars]
        minimum = max(
            RULES.timed_boll_period,
            RULES.timed_macd_slow + RULES.timed_macd_signal,
            RULES.timed_rsi_period + 1,
        )
        if len(closes) < minimum:
            return None

        self._cache_reversal_indicators(indicator_bars, closes)
        upper, middle, lower = bollinger_bands(
            closes,
            RULES.timed_boll_period,
            RULES.timed_boll_stddev,
        )
        _, previous_middle, _ = bollinger_bands(
            closes[:-1],
            RULES.timed_boll_period,
            RULES.timed_boll_stddev,
        )
        _, two_bars_ago_middle, _ = bollinger_bands(
            closes[:-2],
            RULES.timed_boll_period,
            RULES.timed_boll_stddev,
        )
        macd_line, macd_signal, macd_hist = macd_histogram(
            closes,
            RULES.timed_macd_fast,
            RULES.timed_macd_slow,
            RULES.timed_macd_signal,
        )
        _, _, previous_hist = macd_histogram(
            closes[:-1],
            RULES.timed_macd_fast,
            RULES.timed_macd_slow,
            RULES.timed_macd_signal,
        )
        previous_today = today[:-1]
        if len(previous_today) >= RULES.timed_volume_lookback:
            historical_volume = previous_today[-RULES.timed_volume_lookback :]
        else:
            current_local = current.end.astimezone(NY_TZ)
            historical_volume = [
                bar
                for bar in visible[:-1]
                if bar.end.astimezone(NY_TZ).date() < trading_day
                and bar.end.astimezone(NY_TZ).time().replace(tzinfo=None)
                == current_local.time().replace(tzinfo=None)
            ][-RULES.timed_volume_lookback :]
        if not historical_volume:
            return None
        average_volume = (
            Decimal(sum(bar.volume for bar in historical_volume))
            / Decimal(len(historical_volume))
        )
        volume_ratio = (
            Decimal(current.volume) / average_volume
            if average_volume > 0
            else ZERO
        )
        trend_bars = today[-RULES.timed_trend_cross_lookback :]
        trend_sides = [
            bar.close >= self._last_boll_middle_by_end[bar.end]
            for bar in trend_bars
            if bar.end in self._last_boll_middle_by_end
        ]
        boll_middle_crosses = self._prior_boll_middle_crosses(trend_sides)
        context = MarketContext(
            macd_line=macd_line,
            macd_signal=macd_signal,
            macd_hist=macd_hist,
            macd_hist_prev=previous_hist,
            rvol_val=volume_ratio,
            day_high=max(bar.high for bar in today),
            day_low=min(bar.low for bar in today),
            current_open=current.open,
            current_high=current.high,
            current_low=current.low,
            current_close=current.close,
            current_volume=current.volume,
            prev_close=closes[-2],
            prev2_close=closes[-3],
            bar_time=current.end.astimezone(NY_TZ).time().replace(tzinfo=None),
            bar_end=current.end,
            rsi_val=rsi(closes, RULES.timed_rsi_period),
            boll_upper=upper,
            boll_middle=middle,
            boll_middle_prev=previous_middle,
            boll_middle_prev2=two_bars_ago_middle,
            boll_lower=lower,
            boll_middle_crosses=boll_middle_crosses,
        )
        return context, today

    @staticmethod
    def _signal(
        ctx: MarketContext,
        direction: Direction,
        strategy: str,
        spot: Decimal | None,
    ) -> Signal:
        state = (
            MarketState.TREND_UP
            if direction is Direction.CALL
            else MarketState.TREND_DOWN
        )
        return Signal(
            direction=direction,
            bar_end=ctx.bar_end,
            spot=spot or ctx.current_close,
            strategy=strategy,
            market_state=state,
            indicators={
                "profile": "timed_trend",
                "indicator_timeframe": "1m",
                "boll_period": str(RULES.timed_boll_period),
                "boll_stddev": str(RULES.timed_boll_stddev),
                "boll_upper": str(ctx.boll_upper),
                "boll_middle": str(ctx.boll_middle),
                "boll_middle_prev": str(ctx.boll_middle_prev),
                "boll_middle_prev2": str(ctx.boll_middle_prev2),
                "boll_lower": str(ctx.boll_lower),
                "boll_middle_crosses": str(ctx.boll_middle_crosses),
                "macd_fast": str(RULES.timed_macd_fast),
                "macd_slow": str(RULES.timed_macd_slow),
                "macd_signal_period": str(RULES.timed_macd_signal),
                "macd_line": str(ctx.macd_line),
                "macd_signal": str(ctx.macd_signal),
                "macd_hist": str(ctx.macd_hist),
                "macd_hist_prev": str(ctx.macd_hist_prev),
                "volume_ratio": str(ctx.rvol_val),
                "rsi": str(ctx.rsi_val),
                "previous_close": str(ctx.prev_close),
                "two_bars_ago_close": str(ctx.prev2_close),
            },
        )

    def _entry_signal(
        self,
        ctx: MarketContext,
        strategy: str,
        spot: Decimal | None,
    ) -> Signal | None:
        if ctx.boll_middle_crosses > RULES.timed_trend_max_crosses:
            return None
        volume_confirmed = ctx.rvol_val > RULES.timed_volume_ratio
        if (
            ctx.current_close > ctx.boll_middle
            and ctx.macd_hist > 0
            and ctx.macd_hist > ctx.macd_hist_prev
            and volume_confirmed
            and ctx.rsi_val < RULES.timed_call_rsi_max
        ):
            return self._signal(ctx, Direction.CALL, strategy, spot)
        if (
            ctx.current_close < ctx.boll_middle
            and ctx.macd_hist < 0
            and ctx.macd_hist < ctx.macd_hist_prev
            and volume_confirmed
            and ctx.rsi_val > RULES.timed_put_rsi_min
        ):
            return self._signal(ctx, Direction.PUT, strategy, spot)
        return None

    def _opening_signal(
        self,
        ctx: MarketContext,
        spot: Decimal | None,
    ) -> Signal | None:
        """Trade opening volume expansion without ordinary MACD/RSI filters."""
        if ctx.rvol_val <= RULES.timed_volume_ratio:
            return None
        if (
            ctx.current_close > ctx.boll_middle
            and ctx.current_close > ctx.current_open
            and ctx.current_close > ctx.prev_close
        ):
            return self._signal(ctx, Direction.CALL, "timed_opening_signal", spot)
        if (
            ctx.current_close < ctx.boll_middle
            and ctx.current_close < ctx.current_open
            and ctx.current_close < ctx.prev_close
        ):
            return self._signal(ctx, Direction.PUT, "timed_opening_signal", spot)
        return None

    def evaluate(
        self, bars_1m: Sequence[Bar], spot: Decimal | None = None
    ) -> Signal | None:
        computed = self._one_minute_context(bars_1m)
        if computed is None:
            return None
        ctx, today = computed
        self.last_context = ctx
        self._last_today_1m = today
        current_time = ctx.bar_time
        signal: Signal | None = None

        if current_time < RULES.timed_opening_start:
            ctx.market_state = MarketState.OBSERVATION
        elif current_time < RULES.timed_opening_last_signal:
            signal = self._opening_signal(ctx, spot)
        elif current_time < RULES.timed_opening_flat:
            ctx.market_state = MarketState.UNKNOWN
        elif current_time < RULES.timed_main_last_signal:
            signal = self._entry_signal(ctx, "timed_boll_macd_signal", spot)
        else:
            ctx.market_state = MarketState.UNKNOWN

        if signal is not None and signal.bar_end == self.last_signal_bar:
            signal = None
        if signal is not None:
            self.last_signal_bar = signal.bar_end
            ctx.market_state = signal.market_state
        self.last_state = ctx.market_state
        return signal

    def bar_exit_decision(self, position: Position) -> ExitDecision | None:
        ctx = self.last_context
        if ctx is None:
            return None
        if (
            position.strategy_name == "timed_opening_signal"
            and ctx.bar_time >= RULES.timed_opening_flat
        ):
            return ExitDecision(ExitReason.OPENING_CUTOFF, position.quantity)

        bars_after_entry = [
            bar for bar in self._last_today_1m if bar.end > position.opened_at
        ]
        if len(bars_after_entry) < RULES.timed_reversal_min_bars:
            return None

        window = bars_after_entry[-RULES.timed_reversal_window :]
        boll_values = [
            self._last_boll_middle_by_end.get(bar.end) for bar in window[-2:]
        ]
        macd_values = [
            self._last_macd_hist_by_end.get(bar.end) for bar in window[-3:]
        ]
        if any(value is None for value in boll_values + macd_values):
            return None

        if position.direction is Direction.CALL:
            candle_condition = (
                sum(bar.close < bar.open for bar in window) >= 3
            )
            net_price_condition = window[-1].close < window[0].open
            boll_condition = all(
                bar.close < middle
                for bar, middle in zip(
                    window[-2:], boll_values, strict=True
                )
            )
            macd_condition = (
                macd_values[0] > macd_values[1] > macd_values[2]
                and macd_values[2] <= ZERO
            )
        else:
            candle_condition = (
                sum(bar.close > bar.open for bar in window) >= 3
            )
            net_price_condition = window[-1].close > window[0].open
            boll_condition = all(
                bar.close > middle
                for bar, middle in zip(
                    window[-2:], boll_values, strict=True
                )
            )
            macd_condition = (
                macd_values[0] < macd_values[1] < macd_values[2]
                and macd_values[2] >= ZERO
            )

        reversed_direction = (
            boll_condition
            and macd_condition
            and (candle_condition or net_price_condition)
        )
        if reversed_direction:
            return ExitDecision(
                ExitReason.DIRECTION_REVERSAL,
                position.quantity,
            )
        return None


def strategy_from_settings(settings) -> StrategyEngine:
    """Return the project's single production strategy."""
    return StrategyEngine(settings)
