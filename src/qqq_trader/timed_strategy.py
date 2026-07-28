from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from statistics import median

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
from .policy import RULES
from .strategy import (
    MarketContext,
    StrategyEngine,
    atr,
    ema_series,
    rsi,
    vwap,
)

ZERO = Decimal(0)


class TimedTrendStrategy(StrategyEngine):
    """Time-partitioned opening momentum and EMA/VWAP pullback strategy."""

    def __init__(self, settings) -> None:
        super().__init__(settings)
        self._last_main_bar: datetime | None = None
        self._last_main_context: MarketContext | None = None

    @staticmethod
    def _close_location(bar: Bar) -> Decimal:
        span = bar.high - bar.low
        return (bar.close - bar.low) / span if span > 0 else Decimal("0.5")

    @staticmethod
    def _same_minute_history(
        bars: Sequence[Bar], current: Bar
    ) -> tuple[list[int], list[Decimal]]:
        current_local = current.end.astimezone(NY_TZ)
        matches = [
            bar
            for bar in bars
            if bar.end < current.end
            and bar.end.astimezone(NY_TZ).date() < current_local.date()
            and bar.end.astimezone(NY_TZ).time().replace(tzinfo=None)
            == current_local.time().replace(tzinfo=None)
        ][-20:]
        return (
            [bar.volume for bar in matches],
            [bar.high - bar.low for bar in matches],
        )

    def _opening_signal(
        self,
        ctx: MarketContext,
        today: Sequence[Bar],
        visible: Sequence[Bar],
        spot: Decimal | None,
    ) -> Signal | None:
        if len(today) < 3 or ctx.atr_val <= 0:
            return None
        current = today[-1]
        historical_volume, historical_range = self._same_minute_history(
            visible, current
        )
        if len(historical_volume) < 5 or len(historical_range) < 5:
            return None
        normal_volume = median(Decimal(value) for value in historical_volume)
        normal_range = median(historical_range)
        candle_range = current.high - current.low
        if normal_volume <= 0 or normal_range <= 0 or candle_range <= 0:
            return None
        volume_ratio = Decimal(current.volume) / normal_volume
        range_ratio = candle_range / normal_range
        body_ratio = abs(current.close - current.open) / candle_range
        close_location = self._close_location(current)
        previous = list(today[-3:-1])
        common = (
            volume_ratio >= RULES.timed_opening_volume_ratio
            and range_ratio >= RULES.timed_opening_range_ratio
            and body_ratio >= RULES.timed_opening_body_ratio
        )
        if not common:
            return None
        extras = {
            "profile": "timed_trend",
            "size_factor": str(RULES.timed_opening_size_factor),
            "volume_ratio": str(volume_ratio),
            "range_ratio": str(range_ratio),
            "body_ratio": str(body_ratio),
            "ema21": str(ctx.ema20),
            "indicator_timeframe": "1m",
        }
        if (
            current.close > current.open
            and close_location >= Decimal(1) - RULES.timed_opening_close_extreme
            and current.close > ctx.vwap_value
            and ctx.ema9 > ctx.ema20
            and current.close > max(bar.high for bar in previous)
        ):
            stop = current.low - RULES.stop_atr_buffer * ctx.atr_val
            return self._signal(
                ctx,
                Direction.CALL,
                "timed_opening_scalp",
                MarketState.TREND_UP,
                stop,
                spot,
                **extras,
            )
        if (
            current.close < current.open
            and close_location <= RULES.timed_opening_close_extreme
            and current.close < ctx.vwap_value
            and ctx.ema9 < ctx.ema20
            and current.close < min(bar.low for bar in previous)
        ):
            stop = current.high + RULES.stop_atr_buffer * ctx.atr_val
            return self._signal(
                ctx,
                Direction.PUT,
                "timed_opening_scalp",
                MarketState.TREND_DOWN,
                stop,
                spot,
                **extras,
            )
        return None

    def _one_minute_context(
        self, bars_1m: Sequence[Bar]
    ) -> tuple[MarketContext, list[Bar], Decimal, Decimal] | None:
        visible = sorted(
            (bar for bar in bars_1m if bar.complete and self._rth(bar)),
            key=lambda item: item.end,
        )
        if not visible:
            return None
        current = visible[-1]
        trading_day = current.end.astimezone(NY_TZ).date()
        today_1m = [
            bar
            for bar in visible
            if bar.start.astimezone(NY_TZ).date() == trading_day
        ]
        closes = [bar.close for bar in visible[-500:]]
        if len(closes) < RULES.timed_slow_ema_period + 3:
            return None
        ema9_values = ema_series(closes, 9)
        ema21_values = ema_series(closes, RULES.timed_slow_ema_period)
        atr_value = atr(visible[-500:], self.settings.atr_period)
        if atr_value <= 0:
            return None
        current_vwap = vwap(today_1m)
        old_vwap = (
            vwap(today_1m[:-15]) if len(today_1m) > 15 else current_vwap
        )
        context = MarketContext(
            structure_high=max(
                (bar.high for bar in today_1m[-6:-1]), default=ZERO
            ),
            structure_low=min(
                (bar.low for bar in today_1m[-6:-1]), default=ZERO
            ),
            vwap_value=current_vwap,
            vwap_slope_val=current_vwap - old_vwap,
            ema9=ema9_values[-1],
            ema9_prev=ema9_values[-2],
            ema20=ema21_values[-1],
            atr_val=atr_value,
            day_high=max(bar.high for bar in today_1m),
            day_low=min(bar.low for bar in today_1m),
            current_open=current.open,
            current_high=current.high,
            current_low=current.low,
            current_close=current.close,
            current_volume=current.volume,
            bar_time=current.end.astimezone(NY_TZ).time().replace(tzinfo=None),
            bar_end=current.end,
            rsi_val=rsi(closes, self.settings.rsi_period),
        )
        return context, today_1m, ema9_values[-4], ema21_values[-4]

    def _trend_signal(
        self,
        ctx: MarketContext,
        today_1m: Sequence[Bar],
        ema9_three_bars_ago: Decimal,
        ema21_three_bars_ago: Decimal,
        spot: Decimal | None,
    ) -> Signal | None:
        if len(today_1m) < 4 or ctx.atr_val <= 0:
            return None
        current = today_1m[-1]
        extras = {
            "profile": "timed_trend",
            "ema21": str(ctx.ema20),
            "ema9_three_bars_ago": str(ema9_three_bars_ago),
            "ema21_three_bars_ago": str(ema21_three_bars_ago),
            "indicator_timeframe": "1m",
        }
        if (
            ctx.ema9 > ctx.ema20
            and ctx.ema9 > ema9_three_bars_ago
            and ctx.ema20 >= ema21_three_bars_ago
            and ctx.vwap_slope_val > 0
            and current.close > ctx.vwap_value
        ):
            stop = current.low - RULES.stop_atr_buffer * ctx.atr_val
            return self._signal(
                ctx,
                Direction.CALL,
                "timed_trend_signal",
                MarketState.TREND_UP,
                stop,
                spot,
                **extras,
            )
        if (
            ctx.ema9 < ctx.ema20
            and ctx.ema9 < ema9_three_bars_ago
            and ctx.ema20 <= ema21_three_bars_ago
            and ctx.vwap_slope_val < 0
            and current.close < ctx.vwap_value
        ):
            stop = current.high + RULES.stop_atr_buffer * ctx.atr_val
            return self._signal(
                ctx,
                Direction.PUT,
                "timed_trend_signal",
                MarketState.TREND_DOWN,
                stop,
                spot,
                **extras,
            )
        return None

    def evaluate(
        self, bars_1m: Sequence[Bar], spot: Decimal | None = None
    ) -> Signal | None:
        computed = self._one_minute_context(bars_1m)
        if computed is None:
            return None
        ctx, today, ema9_before, ema21_before = computed
        self.last_context = ctx
        current_time = ctx.bar_time
        if current_time < RULES.timed_opening_start:
            ctx.market_state = MarketState.OBSERVATION
            self.last_state = ctx.market_state
            return None
        signal: Signal | None = None
        if current_time < RULES.timed_opening_last_signal:
            visible = sorted(
                (bar for bar in bars_1m if bar.complete and self._rth(bar)),
                key=lambda item: item.end,
            )
            signal = self._opening_signal(ctx, today, visible, spot)
        elif current_time < RULES.timed_opening_flat:
            ctx.market_state = MarketState.UNKNOWN
        elif current_time <= RULES.timed_main_last_signal:
            self._last_main_context = ctx
            if ctx.bar_end != self._last_main_bar:
                self._last_main_bar = ctx.bar_end
                signal = self._trend_signal(
                    ctx,
                    today,
                    ema9_before,
                    ema21_before,
                    spot,
                )
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
        if position.underlying_stop is not None:
            stopped = (
                ctx.current_close <= position.underlying_stop
                if position.direction is Direction.CALL
                else ctx.current_close >= position.underlying_stop
            )
            if stopped:
                return ExitDecision(ExitReason.STRUCTURE_STOP, position.quantity)
        if (
            position.strategy_name == "timed_opening_scalp"
            and ctx.bar_time >= RULES.timed_opening_flat
        ):
            return ExitDecision(ExitReason.OPENING_CUTOFF, position.quantity)
        main = self._last_main_context
        if (
            position.strategy_name in {"timed_trend_signal", "timed_trend_retest"}
            and ctx.bar_time >= RULES.reduce_at
            and main is not None
            and main.bar_end == ctx.bar_end
        ):
            invalid = (
                main.current_close < main.vwap_value or main.ema9 < main.ema20
                if position.direction is Direction.CALL
                else main.current_close > main.vwap_value or main.ema9 > main.ema20
            )
            if invalid:
                return ExitDecision(ExitReason.VWAP_CROSS, position.quantity)
        return None
