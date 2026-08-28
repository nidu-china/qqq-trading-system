"""Regime-adaptive QQQ 0DTE strategy.

The strategy first classifies the current one-minute market regime with the
same indicators used for entry. Trend regimes trade directional momentum;
range regimes trade only confirmed mean reversion at a Bollinger outer band.
All calculations use completed candles and are shared by live and backtest.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, time
from decimal import Decimal

from .config import NY_TZ
from .domain import Bar, Direction, ExitDecision, ExitReason, MarketState, Position, Signal
from .indicators import bollinger_bands, ema_series
from .policy import RULES
from .strategy import StrategyEngine
from .volatility import VixFiveMinuteTrend

ZERO = Decimal(0)


class HybridEngine:
    """EMA/BOLL/MACD/RSI/volume regime-adaptive strategy.

    Time state machine (America/New_York):
      * 09:30-09:40: collect data and warm indicators, never enter;
      * 09:40-11:30: classify every completed bar and allow entries;
      * 11:30-13:55: manage existing positions only;
      * 13:55+: RiskEngine unconditionally closes every position.
    """

    def __init__(self, settings) -> None:
        self.settings = settings
        # Reuse the production-tested indicator context and volume calculation.
        self.indicators = StrategyEngine(settings)
        self.boll_macd = self.indicators  # compatibility for reports/tests
        self.last_signal_bar: datetime | None = None
        self.last_context = None
        self.last_state = MarketState.UNKNOWN
        self.vix_trend = VixFiveMinuteTrend.NEUTRAL

        self._current_day: date | None = None
        self._day_mode: str | None = None
        self._regime_history: list[MarketState] = []
        self._today_bars: list[Bar] = []
        self._ema_fast: Decimal = ZERO
        self._ema_slow: Decimal = ZERO
        self._ema_fast_prev: Decimal = ZERO
        self._ema_slow_prev: Decimal = ZERO
        self._previous_boll_upper: Decimal = ZERO
        self._previous_boll_lower: Decimal = ZERO
        self._regime_details: dict[str, str] = {}

    def _reset_day(self, trading_day: date) -> None:
        self._current_day = trading_day
        self._day_mode = None
        self._regime_history.clear()
        self._today_bars = []
        self.last_state = MarketState.OBSERVATION

    def set_volatility_context(self, volatility_bars: Sequence[Bar], decision_at: datetime) -> None:
        self.indicators.set_volatility_context(volatility_bars, decision_at)
        self.vix_trend = self.indicators.vix_trend

    def record_entry(self, direction: Direction, entered_at: datetime) -> None:
        self.indicators.record_entry(direction, entered_at)

    def record_profitable_exit(self, direction: Direction, exited_at: datetime) -> None:
        self.indicators.record_profitable_exit(direction, exited_at)

    @staticmethod
    def _rth(bar: Bar) -> bool:
        local = bar.start.astimezone(NY_TZ).time().replace(tzinfo=None)
        return time(9, 30) <= local < time(16, 0)

    def _ema_values(self, closes: Sequence[Decimal]) -> bool:
        if len(closes) < RULES.trend_ema_slow + 1:
            return False
        fast = ema_series(closes, RULES.trend_ema_fast)
        slow = ema_series(closes, RULES.trend_ema_slow)
        self._ema_fast, self._ema_fast_prev = fast[-1], fast[-2]
        self._ema_slow, self._ema_slow_prev = slow[-1], slow[-2]
        return True

    def _raw_regime(self) -> MarketState:
        """Classify the latest completed bar without future information."""
        ctx = self.last_context
        if ctx is None:
            return MarketState.UNKNOWN
        half_width = max(ctx.boll_upper - ctx.boll_middle, Decimal("0.000001"))
        ema_separation = abs(self._ema_fast - self._ema_slow) / half_width
        macd_strength = abs(ctx.macd_hist) / half_width
        band_position = (ctx.current_close - ctx.boll_middle) / half_width

        bullish_votes = sum(
            (
                self._ema_fast > self._ema_slow,
                self._ema_fast > self._ema_fast_prev,
                self._ema_slow >= self._ema_slow_prev,
                ctx.macd_hist > ZERO,
                ctx.macd_hist >= ctx.macd_hist_prev,
                ctx.current_close > ctx.boll_middle,
                ctx.rsi_val >= RULES.regime_trend_call_rsi_min,
            )
        )
        bearish_votes = sum(
            (
                self._ema_fast < self._ema_slow,
                self._ema_fast < self._ema_fast_prev,
                self._ema_slow <= self._ema_slow_prev,
                ctx.macd_hist < ZERO,
                ctx.macd_hist <= ctx.macd_hist_prev,
                ctx.current_close < ctx.boll_middle,
                ctx.rsi_val <= RULES.regime_trend_put_rsi_max,
            )
        )
        trend_common = (
            ema_separation >= RULES.regime_trend_min_ema_separation
            and ctx.boll_middle_crosses <= RULES.regime_trend_max_middle_crosses
        )

        raw = MarketState.UNKNOWN
        if trend_common and bullish_votes >= RULES.regime_trend_min_score:
            raw = MarketState.TREND_UP
        elif trend_common and bearish_votes >= RULES.regime_trend_min_score:
            raw = MarketState.TREND_DOWN
        else:
            range_votes = sum(
                (
                    ema_separation <= RULES.regime_range_max_ema_separation,
                    ctx.boll_middle_crosses >= RULES.regime_range_min_middle_crosses,
                    macd_strength <= RULES.regime_range_max_macd_strength,
                    abs(band_position) <= RULES.regime_range_max_band_position,
                )
            )
            if range_votes >= RULES.regime_range_min_score:
                raw = MarketState.RANGE

        self._regime_details = {
            "raw_regime": raw.value,
            "ema_fast": str(self._ema_fast),
            "ema_slow": str(self._ema_slow),
            "ema_separation_boll": str(ema_separation),
            "macd_strength_boll": str(macd_strength),
            "band_position": str(band_position),
            "bullish_score": str(bullish_votes),
            "bearish_score": str(bearish_votes),
            "boll_middle_crosses": str(ctx.boll_middle_crosses),
        }
        return raw

    def _confirmed_regime(self, raw: MarketState) -> MarketState:
        self._regime_history.append(raw)
        keep = max(RULES.regime_confirmation_bars, 1)
        self._regime_history = self._regime_history[-keep:]
        if len(self._regime_history) == keep and all(
            state is raw for state in self._regime_history
        ):
            self.last_state = raw
        elif self.last_state in {MarketState.OBSERVATION, MarketState.UNKNOWN}:
            self.last_state = MarketState.UNKNOWN
        return self.last_state

    def _signal(self, direction: Direction, strategy: str, spot: Decimal | None) -> Signal:
        ctx = self.last_context
        assert ctx is not None and ctx.bar_end is not None
        indicators = {
            "profile": "regime_adaptive",
            "indicator_timeframe": "1m",
            "confirmed_regime": self.last_state.value,
            "boll_period": str(RULES.timed_boll_period),
            "boll_stddev": str(RULES.timed_boll_stddev),
            "boll_upper": str(ctx.boll_upper),
            "boll_middle": str(ctx.boll_middle),
            "boll_lower": str(ctx.boll_lower),
            "macd": (f"{RULES.timed_macd_fast},{RULES.timed_macd_slow},{RULES.timed_macd_signal}"),
            "macd_hist": str(ctx.macd_hist),
            "macd_hist_prev": str(ctx.macd_hist_prev),
            "rsi": str(ctx.rsi_val),
            "volume_ratio": str(ctx.rvol_val),
            "vix_5m_trend": self.vix_trend.value,
            **self._regime_details,
        }
        return Signal(
            direction=direction,
            bar_end=ctx.bar_end,
            spot=spot or ctx.current_close,
            strategy=strategy,
            market_state=self.last_state,
            indicators=indicators,
        )

    def _trend_signal(self, spot: Decimal | None) -> Signal | None:
        ctx = self.last_context
        assert ctx is not None
        half_width = max(ctx.boll_upper - ctx.boll_middle, Decimal("0.000001"))
        band_position = (ctx.current_close - ctx.boll_middle) / half_width
        minimum_volume = RULES.regime_trend_min_volume_ratio
        
        # Lower threshold for early entry: 0.2 instead of 0.65
        relaxed_band_position = Decimal("0.2")
        
        if (
            self.last_state is MarketState.TREND_UP
            and band_position >= relaxed_band_position
            and ctx.macd_hist > ZERO
            and ctx.macd_hist >= ctx.macd_hist_prev
            and RULES.regime_trend_call_rsi_min <= ctx.rsi_val < RULES.timed_call_rsi_max
            and ctx.rvol_val >= minimum_volume
        ):
            return self._signal(Direction.CALL, "regime_trend_following", spot)
        if (
            self.last_state is MarketState.TREND_DOWN
            and band_position <= -relaxed_band_position
            and ctx.macd_hist < ZERO
            and ctx.macd_hist <= ctx.macd_hist_prev
            and RULES.timed_put_rsi_min < ctx.rsi_val <= RULES.regime_trend_put_rsi_max
            and ctx.rvol_val >= minimum_volume
        ):
            return self._signal(Direction.PUT, "regime_trend_following", spot)
        return None

    def _momentum_signal(self, spot: Decimal | None) -> Signal | None:
        """Pure momentum entry: 3 consecutive rising/falling bars."""
        if len(self._today_bars) < 4:
            return None
        
        ctx = self.last_context
        assert ctx is not None
        
        # Check last 3 bars for consistent direction
        last_3 = self._today_bars[-3:]
        all_rising = all(
            last_3[i].close > last_3[i-1].close for i in range(1, len(last_3))
        )
        all_falling = all(
            last_3[i].close < last_3[i-1].close for i in range(1, len(last_3))
        )
        
        # Require decent volume
        volume_ok = ctx.rvol_val >= RULES.regime_trend_min_volume_ratio * Decimal("0.9")
        
        if all_rising and volume_ok and ctx.rsi_val < RULES.timed_call_rsi_max:
            return self._signal(Direction.CALL, "regime_momentum_3bar", spot)
        
        if all_falling and volume_ok and ctx.rsi_val > RULES.timed_put_rsi_min:
            return self._signal(Direction.PUT, "regime_momentum_3bar", spot)
        
        return None

    def _range_signal(self, spot: Decimal | None) -> Signal | None:
        ctx = self.last_context
        assert ctx is not None
        volume_ok = (
            RULES.regime_range_min_volume_ratio
            <= ctx.rvol_val
            <= RULES.regime_range_max_volume_ratio
        )
        
        # Original: outer band touches
        lower_reentry = ctx.current_close <= ctx.boll_lower or (
            ctx.prev_close <= self._previous_boll_lower and ctx.current_close > ctx.boll_lower
        )
        upper_reentry = ctx.current_close >= ctx.boll_upper or (
            ctx.prev_close >= self._previous_boll_upper and ctx.current_close < ctx.boll_upper
        )
        
        # New: middle band bounces (relaxed entry)
        half_width = max(ctx.boll_upper - ctx.boll_middle, Decimal("0.000001"))
        distance_from_middle = abs(ctx.current_close - ctx.boll_middle) / half_width
        near_middle = distance_from_middle <= Decimal("0.3")  # Within 30% of half-width
        
        # Strong MACD reversal from middle
        macd_turning_up = (
            ctx.macd_hist > ctx.macd_hist_prev
            and ctx.macd_hist > ZERO
            and ctx.current_close < ctx.boll_middle
        )
        macd_turning_down = (
            ctx.macd_hist < ctx.macd_hist_prev
            and ctx.macd_hist < ZERO
            and ctx.current_close > ctx.boll_middle
        )
        
        # Outer band reversions (strict)
        if (
            lower_reentry
            and ctx.rsi_val <= RULES.regime_range_rsi_oversold
            and ctx.macd_hist > ctx.macd_hist_prev
            and volume_ok
        ):
            return self._signal(Direction.CALL, "regime_range_reversion", spot)
        if (
            upper_reentry
            and ctx.rsi_val >= RULES.regime_range_rsi_overbought
            and ctx.macd_hist < ctx.macd_hist_prev
            and volume_ok
        ):
            return self._signal(Direction.PUT, "regime_range_reversion", spot)
        
        # Middle band bounces (relaxed, only if volume supports)
        if near_middle and macd_turning_up and volume_ok:
            return self._signal(Direction.CALL, "regime_range_middle_bounce", spot)
        if near_middle and macd_turning_down and volume_ok:
            return self._signal(Direction.PUT, "regime_range_middle_bounce", spot)
        
        return None

    def evaluate(self, bars_1m: Sequence[Bar], spot: Decimal | None = None) -> Signal | None:
        computed = self.indicators._one_minute_context(bars_1m)
        visible = sorted(
            (bar for bar in bars_1m if bar.complete and self._rth(bar)),
            key=lambda bar: bar.end,
        )
        if not visible:
            return None
        current = visible[-1]
        trading_day = current.end.astimezone(NY_TZ).date()
        if self._current_day != trading_day:
            self._reset_day(trading_day)
        self._today_bars = [
            bar for bar in visible if bar.start.astimezone(NY_TZ).date() == trading_day
        ]
        local_time = current.end.astimezone(NY_TZ).time().replace(tzinfo=None)

        if computed is None:
            self.last_state = (
                MarketState.OBSERVATION
                if local_time <= RULES.phase_collect_end
                else MarketState.UNKNOWN
            )
            return None
        self.last_context, _ = computed
        self.indicators.last_context = self.last_context
        self.indicators._last_today_1m = self._today_bars

        closes = [bar.close for bar in visible[-500:]]
        if not self._ema_values(closes):
            return None
        self._previous_boll_upper, _, self._previous_boll_lower = bollinger_bands(
            closes[:-1], RULES.timed_boll_period, RULES.timed_boll_stddev
        )

        if local_time <= RULES.phase_collect_end:
            self.last_state = MarketState.OBSERVATION
            return None

        raw = self._raw_regime()
        state = self._confirmed_regime(raw)
        self._day_mode = (
            "trend"
            if state in {MarketState.TREND_UP, MarketState.TREND_DOWN}
            else "oscillation"
            if state is MarketState.RANGE
            else None
        )

        if local_time >= RULES.phase_main_end:
            return None
        
        # Try regime-specific signals first
        signal = (
            self._trend_signal(spot)
            if state in {MarketState.TREND_UP, MarketState.TREND_DOWN}
            else self._range_signal(spot)
            if state is MarketState.RANGE
            else None
        )
        
        # If no regime signal and state is still unknown, try momentum
        if signal is None and state is MarketState.UNKNOWN:
            signal = self._momentum_signal(spot)
        
        if signal is not None and signal.bar_end == self.last_signal_bar:
            return None
        if signal is not None:
            self.last_signal_bar = signal.bar_end
        return signal

    def bar_exit_decision(self, position: Position) -> ExitDecision | None:
        """Regime-specific bar exit; price risk and 13:55 close stay in RiskEngine."""
        ctx = self.last_context
        if ctx is None:
            return None
        if position.strategy_name == "regime_range_reversion":
            if position.direction is Direction.CALL and ctx.current_close >= ctx.boll_middle:
                return ExitDecision(ExitReason.BOLLINGER_MIDDLE, position.quantity)
            if position.direction is Direction.PUT and ctx.current_close <= ctx.boll_middle:
                return ExitDecision(ExitReason.BOLLINGER_MIDDLE, position.quantity)
            return self.indicators.bar_exit_decision(position)

        if position.direction is Direction.CALL:
            opposite = self.last_state is MarketState.TREND_DOWN
            ema_broken = ctx.current_close < self._ema_slow
        else:
            opposite = self.last_state is MarketState.TREND_UP
            ema_broken = ctx.current_close > self._ema_slow
        if opposite:
            return ExitDecision(ExitReason.STATE_INVALIDATION, position.quantity)
        if ema_broken:
            return ExitDecision(ExitReason.TREND_EMA_EXIT, position.quantity)
        return None

    @property
    def day_mode(self) -> str | None:
        return self._day_mode


# Backwards-compatible import name used by older integrations.
AdaptiveEngine = HybridEngine
