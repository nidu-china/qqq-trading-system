from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, time
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
from .volatility import VixFiveMinuteTrend, vix_five_minute_trend

ZERO = Decimal(0)


class StrategyEngine:
    """One-minute BOLL/MACD strategy with fixed time partitions."""

    def __init__(
        self,
        settings,
        *,
        normal_fresh_macd_filter: bool = False,
        normal_cross2_filter: bool = False,
    ) -> None:
        self.settings = settings
        self.normal_fresh_macd_filter = normal_fresh_macd_filter
        self.normal_cross2_filter = normal_cross2_filter
        self.last_signal_bar: datetime | None = None
        self.last_context: MarketContext | None = None
        self.last_state = MarketState.UNKNOWN
        self._last_today_1m: list[Bar] = []
        self._last_boll_middle_by_end: dict[datetime, Decimal] = {}
        self._last_macd_hist_by_end: dict[datetime, Decimal] = {}
        self.vix_trend = VixFiveMinuteTrend.NEUTRAL
        self._continuation_day: date | None = None
        self._profitable_exit_directions: set[Direction] = set()
        self._last_cross_reset = False

    def _set_continuation_day(self, trading_day: date) -> None:
        if self._continuation_day != trading_day:
            self._continuation_day = trading_day
            self._profitable_exit_directions.clear()

    def record_profitable_exit(
        self,
        direction: Direction,
        exited_at: datetime,
    ) -> None:
        """Permit a same-direction continuation entry after a profitable exit."""
        self._set_continuation_day(exited_at.astimezone(NY_TZ).date())
        self._profitable_exit_directions.add(direction)

    def record_entry(self, direction: Direction, entered_at: datetime) -> None:
        """Consume continuation eligibility only after an entry actually fills."""
        self._set_continuation_day(entered_at.astimezone(NY_TZ).date())
        self._profitable_exit_directions.discard(direction)

    def set_volatility_context(
        self,
        volatility_bars: Sequence[Bar],
        decision_at: datetime,
    ) -> None:
        self.vix_trend = vix_five_minute_trend(
            volatility_bars,
            decision_at,
            self.settings.volatility_max_staleness_minutes,
            RULES.timed_vix_trend_min_change,
        )

    def _entry_thresholds(
        self,
        direction: Direction,
    ) -> tuple[Decimal, Decimal, int]:
        favorable = (
            direction is Direction.CALL
            and self.vix_trend is VixFiveMinuteTrend.FALLING
        ) or (
            direction is Direction.PUT
            and self.vix_trend is VixFiveMinuteTrend.RISING
        )
        adverse = (
            direction is Direction.CALL
            and self.vix_trend is VixFiveMinuteTrend.RISING
        ) or (
            direction is Direction.PUT
            and self.vix_trend is VixFiveMinuteTrend.FALLING
        )
        volume = RULES.timed_volume_ratio
        rsi = (
            RULES.timed_call_rsi_max
            if direction is Direction.CALL
            else RULES.timed_put_rsi_min
        )
        crosses = RULES.timed_trend_max_crosses
        if favorable:
            volume *= Decimal(1) - RULES.timed_vix_volume_adjustment
        elif adverse:
            volume *= Decimal(1) + RULES.timed_vix_volume_adjustment
        return volume, rsi, crosses

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

    def _effective_boll_middle_crosses(
        self,
        trend_sides: Sequence[bool],
    ) -> int:
        crosses = self._prior_boll_middle_crosses(trend_sides)
        self._last_cross_reset = False
        if len(trend_sides) < 5:
            return crosses
        stable_side = trend_sides[-1]
        stable_direction = Direction.CALL if stable_side else Direction.PUT
        if (
            stable_direction in self._profitable_exit_directions
            and all(side == stable_side for side in trend_sides[-5:])
        ):
            self._last_cross_reset = True
            return 0
        return crosses

    @staticmethod
    def _relative_volume(
        visible: Sequence[Bar],
        today: Sequence[Bar],
        index: int,
        trading_day: date,
    ) -> Decimal:
        current = today[index]
        previous_today = today[:index]
        if len(previous_today) >= RULES.timed_volume_lookback:
            historical_volume = previous_today[-RULES.timed_volume_lookback :]
        else:
            current_local = current.end.astimezone(NY_TZ)
            historical_volume = [
                bar
                for bar in visible
                if bar.end < current.end
                and bar.end.astimezone(NY_TZ).date() < trading_day
                and bar.end.astimezone(NY_TZ).time().replace(tzinfo=None)
                == current_local.time().replace(tzinfo=None)
            ][-RULES.timed_volume_lookback :]
        if not historical_volume:
            return ZERO
        average_volume = (
            Decimal(sum(bar.volume for bar in historical_volume))
            / Decimal(len(historical_volume))
        )
        return Decimal(current.volume) / average_volume if average_volume > 0 else ZERO

    @staticmethod
    def _band_extension(ctx: MarketContext, direction: Direction) -> Decimal:
        if direction is Direction.CALL:
            half_width = ctx.boll_upper - ctx.boll_middle
            distance = ctx.current_close - ctx.boll_middle
        else:
            half_width = ctx.boll_middle - ctx.boll_lower
            distance = ctx.boll_middle - ctx.current_close
        return distance / half_width if half_width > ZERO else Decimal("999")

    def _continuation_confirmed(
        self,
        ctx: MarketContext,
        direction: Direction,
        volume_threshold: Decimal,
    ) -> bool:
        if not self._last_cross_reset:
            return True
        if (
            self._band_extension(ctx, direction)
            > RULES.timed_continuation_max_band_extension
        ):
            return False
        if ctx.rvol_val <= ctx.rvol_prev:
            return False
        fresh_macd_cross = (
            direction is Direction.CALL and ctx.macd_hist_prev <= ZERO
        ) or (
            direction is Direction.PUT and ctx.macd_hist_prev >= ZERO
        )
        if fresh_macd_cross and ctx.rvol_val <= (
            volume_threshold
            * RULES.timed_continuation_fresh_macd_volume_multiplier
        ):
            return False
        return True

    def _normal_entry_confirmed(
        self,
        ctx: MarketContext,
        direction: Direction,
        volume_threshold: Decimal,
    ) -> bool:
        """Apply experimental quality gates only to ordinary first entries."""
        if self._last_cross_reset:
            return True
        fresh_macd_cross = (
            direction is Direction.CALL and ctx.macd_hist_prev <= ZERO
        ) or (
            direction is Direction.PUT and ctx.macd_hist_prev >= ZERO
        )
        if self.normal_fresh_macd_filter and fresh_macd_cross:
            if ctx.rvol_val <= ctx.rvol_prev:
                return False
            if ctx.rvol_val <= (
                volume_threshold
                * RULES.timed_normal_fresh_macd_volume_multiplier
            ):
                return False
        if (
            self.normal_cross2_filter
            and ctx.boll_middle_crosses == RULES.timed_trend_max_crosses
        ):
            if ctx.rvol_val <= ctx.rvol_prev:
                return False
            if (
                self._band_extension(ctx, direction)
                > RULES.timed_normal_cross2_max_band_extension
            ):
                return False
        return True

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
        self._set_continuation_day(trading_day)
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
        volume_ratio = self._relative_volume(
            visible,
            today,
            len(today) - 1,
            trading_day,
        )
        previous_volume_ratio = (
            self._relative_volume(
                visible,
                today,
                len(today) - 2,
                trading_day,
            )
            if len(today) >= 2
            else ZERO
        )
        if volume_ratio <= ZERO:
            return None
        trend_bars = today[-RULES.timed_trend_cross_lookback :]
        trend_sides = [
            bar.close >= self._last_boll_middle_by_end[bar.end]
            for bar in trend_bars
            if bar.end in self._last_boll_middle_by_end
        ]
        boll_middle_crosses = self._effective_boll_middle_crosses(trend_sides)
        context = MarketContext(
            macd_line=macd_line,
            macd_signal=macd_signal,
            macd_hist=macd_hist,
            macd_hist_prev=previous_hist,
            rvol_val=volume_ratio,
            rvol_prev=previous_volume_ratio,
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

    def _signal(
        self,
        ctx: MarketContext,
        direction: Direction,
        strategy: str,
        spot: Decimal | None,
    ) -> Signal:
        volume_threshold, rsi_threshold, cross_threshold = self._entry_thresholds(
            direction
        )
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
                "boll_middle_cross_limit": str(cross_threshold),
                "boll_middle_crosses_reset": str(self._last_cross_reset).lower(),
                "macd_fast": str(RULES.timed_macd_fast),
                "macd_slow": str(RULES.timed_macd_slow),
                "macd_signal_period": str(RULES.timed_macd_signal),
                "macd_line": str(ctx.macd_line),
                "macd_signal": str(ctx.macd_signal),
                "macd_hist": str(ctx.macd_hist),
                "macd_hist_prev": str(ctx.macd_hist_prev),
                "volume_ratio": str(ctx.rvol_val),
                "previous_volume_ratio": str(ctx.rvol_prev),
                "volume_ratio_threshold": str(volume_threshold),
                "boll_band_extension": str(self._band_extension(ctx, direction)),
                "continuation_quality_filter": str(
                    self._last_cross_reset
                ).lower(),
                "normal_fresh_macd_filter": str(
                    self.normal_fresh_macd_filter
                ).lower(),
                "normal_cross2_filter": str(self.normal_cross2_filter).lower(),
                "rsi": str(ctx.rsi_val),
                "rsi_threshold": str(rsi_threshold),
                "vix_5m_trend": self.vix_trend.value,
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
        call_volume, call_rsi, call_crosses = self._entry_thresholds(Direction.CALL)
        put_volume, put_rsi, put_crosses = self._entry_thresholds(Direction.PUT)
        if (
            ctx.boll_middle_crosses <= call_crosses
            and ctx.current_close > ctx.boll_middle
            and ctx.boll_middle > ctx.boll_middle_prev > ctx.boll_middle_prev2
            and ctx.macd_hist > 0
            and ctx.macd_hist > ctx.macd_hist_prev
            and ctx.rvol_val > call_volume
            and ctx.rsi_val < call_rsi
            and self._continuation_confirmed(
                ctx,
                Direction.CALL,
                call_volume,
            )
            and self._normal_entry_confirmed(
                ctx,
                Direction.CALL,
                call_volume,
            )
        ):
            return self._signal(
                ctx,
                Direction.CALL,
                "timed_trend_continuation" if self._last_cross_reset else strategy,
                spot,
            )
        if (
            ctx.boll_middle_crosses <= put_crosses
            and ctx.current_close < ctx.boll_middle
            and ctx.boll_middle < ctx.boll_middle_prev < ctx.boll_middle_prev2
            and ctx.macd_hist < 0
            and ctx.macd_hist < ctx.macd_hist_prev
            and ctx.rvol_val > put_volume
            and ctx.rsi_val > put_rsi
            and self._continuation_confirmed(
                ctx,
                Direction.PUT,
                put_volume,
            )
            and self._normal_entry_confirmed(
                ctx,
                Direction.PUT,
                put_volume,
            )
        ):
            return self._signal(
                ctx,
                Direction.PUT,
                "timed_trend_continuation" if self._last_cross_reset else strategy,
                spot,
            )
        return None

    def _opening_signal(
        self,
        ctx: MarketContext,
        spot: Decimal | None,
    ) -> Signal | None:
        """Trade opening volume expansion without ordinary MACD/RSI filters."""
        call_volume, _, _ = self._entry_thresholds(Direction.CALL)
        put_volume, _, _ = self._entry_thresholds(Direction.PUT)
        if (
            ctx.rvol_val > call_volume
            and ctx.current_close > ctx.boll_middle
            and ctx.current_close > ctx.current_open
            and ctx.current_close > ctx.prev_close
        ):
            return self._signal(ctx, Direction.CALL, "timed_opening_signal", spot)
        if (
            ctx.rvol_val > put_volume
            and ctx.current_close < ctx.boll_middle
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

        if position.trend_runner:
            if position.direction is Direction.CALL:
                if ctx.current_close < ctx.boll_middle:
                    return ExitDecision(ExitReason.BOLLINGER_MIDDLE, position.quantity)
                if ctx.macd_hist <= ZERO and ctx.macd_hist < ctx.macd_hist_prev:
                    return ExitDecision(ExitReason.DIRECTION_REVERSAL, position.quantity)
            else:
                if ctx.current_close > ctx.boll_middle:
                    return ExitDecision(ExitReason.BOLLINGER_MIDDLE, position.quantity)
                if ctx.macd_hist >= ZERO and ctx.macd_hist > ctx.macd_hist_prev:
                    return ExitDecision(ExitReason.DIRECTION_REVERSAL, position.quantity)

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
