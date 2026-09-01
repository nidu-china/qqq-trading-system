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
from .indicators import bollinger_bands, ema_series, macd_histogram
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
        # Dual MACD: fast for range, slow for trend
        self._macd_fast: Decimal = ZERO       # MACD(5,10,3) for range
        self._macd_fast_prev: Decimal = ZERO
        self._macd_fast_prev2: Decimal = ZERO  # two bars ago (2-bar acceleration check)
        self._macd_slow: Decimal = ZERO       # MACD(8,17,9) for trend
        self._macd_slow_prev: Decimal = ZERO
        # Opening Range (09:30-09:40 ET) — updated each bar, stable by 09:40
        self._or_high: Decimal | None = None
        self._or_low: Decimal | None = None

        # Direction lock after repeated stop-losses in the same direction
        self._stop_loss_count: dict[Direction, int] = {}
        self._direction_blocked: set[Direction] = set()

    def _reset_day(self, trading_day: date) -> None:
        self._current_day = trading_day
        self._day_mode = None
        self._regime_history.clear()
        self._today_bars = []
        self.last_state = MarketState.OBSERVATION
        self._or_high = None
        self._or_low = None
        self._macd_fast_prev2 = ZERO
        self._stop_loss_count.clear()
        self._direction_blocked.clear()

    def set_volatility_context(self, volatility_bars: Sequence[Bar], decision_at: datetime) -> None:
        self.indicators.set_volatility_context(volatility_bars, decision_at)
        self.vix_trend = self.indicators.vix_trend

    def record_entry(self, direction: Direction, entered_at: datetime) -> None:
        self.indicators.record_entry(direction, entered_at)

    def record_profitable_exit(self, direction: Direction, exited_at: datetime) -> None:
        self.indicators.record_profitable_exit(direction, exited_at)

    def record_stop_loss(self, direction: Direction) -> None:
        """Called after a stop-loss exit.

        After the first stop-loss in a direction, that direction is blocked for
        the rest of the day.  A single stop-loss already signals that the
        regime classification is unreliable for this direction; continuing to
        enter is just bleeding equity.  The lock resets at next day's
        _reset_day call.
        """
        self._direction_blocked.add(direction)

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

    def _dual_macd_values(self, closes: Sequence[Decimal]) -> bool:
        """Calculate both fast MACD(5,10,3) and slow MACD(8,17,9)."""
        # Fast MACD for range (5,10,3) — also track 2-bar history for quality filter
        if len(closes) >= 10 + 3:
            _, _, fast_hist  = macd_histogram(closes,      5, 10, 3)
            _, _, fast_prev  = macd_histogram(closes[:-1], 5, 10, 3)
            _, _, fast_prev2 = macd_histogram(closes[:-2], 5, 10, 3)
            self._macd_fast       = fast_hist
            self._macd_fast_prev  = fast_prev
            self._macd_fast_prev2 = fast_prev2
        else:
            return False
        
        # Slow MACD for trend (8,17,9) - default
        if len(closes) >= 17 + 9:
            _, _, slow_hist = macd_histogram(closes, 8, 17, 9)
            _, _, slow_prev = macd_histogram(closes[:-1], 8, 17, 9)
            self._macd_slow = slow_hist
            self._macd_slow_prev = slow_prev
        else:
            return False
        
        return True

    def _active_macd(self) -> tuple[Decimal, Decimal]:
        """Return (current, previous) MACD based on regime."""
        if self.last_state is MarketState.RANGE:
            return (self._macd_fast, self._macd_fast_prev)
        else:
            return (self._macd_slow, self._macd_slow_prev)

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
        assert ctx.bar_end is not None
        half_width = max(ctx.boll_upper - ctx.boll_middle, Decimal("0.000001"))
        band_position = (ctx.current_close - ctx.boll_middle) / half_width
        minimum_volume = RULES.regime_trend_min_volume_ratio
        macd_curr, macd_prev = self._active_macd()

        bar_time = ctx.bar_end.astimezone(NY_TZ).time().replace(tzinfo=None)
        in_phase2 = bar_time < RULES.phase_opening_end

        if in_phase2 and self._or_high is not None and self._or_low is not None:
            # Phase 2 (09:40-10:00): OR breakout entry — no band_position requirement
            if (
                self.last_state is MarketState.TREND_UP
                and ctx.current_close > self._or_high
                and macd_curr >= macd_prev
                and RULES.regime_trend_call_rsi_min <= ctx.rsi_val < RULES.timed_call_rsi_max
                and ctx.rvol_val >= minimum_volume
            ):
                return self._signal(Direction.CALL, "regime_trend_or_breakout", spot)
            if (
                self.last_state is MarketState.TREND_DOWN
                and ctx.current_close < self._or_low
                and macd_curr <= macd_prev
                and RULES.timed_put_rsi_min < ctx.rsi_val <= RULES.regime_trend_put_rsi_max
                and ctx.rvol_val >= minimum_volume
            ):
                return self._signal(Direction.PUT, "regime_trend_or_breakout", spot)
        else:
            # Phase 3+ (10:00+): band_position confirmation + OR gate
            # OR gate: only enter if price has already broken out of the Opening Range.
            # This prevents regime lock-in entries when the market is still inside OR.
            required_band_position = Decimal("0.65")
            above_or = self._or_high is None or ctx.current_close > self._or_high
            below_or = self._or_low is None or ctx.current_close < self._or_low
            if (
                self.last_state is MarketState.TREND_UP
                and above_or
                and band_position >= required_band_position
                and macd_curr > ZERO
                and macd_curr >= macd_prev
                and RULES.regime_trend_call_rsi_min <= ctx.rsi_val < RULES.timed_call_rsi_max
                and ctx.rvol_val >= minimum_volume
            ):
                return self._signal(Direction.CALL, "regime_trend_following", spot)
            if (
                self.last_state is MarketState.TREND_DOWN
                and below_or
                and band_position <= -required_band_position
                and macd_curr < ZERO
                and macd_curr <= macd_prev
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

    def _phase2_or_breakout_signal(self, spot: Decimal | None) -> Signal | None:
        """Phase 2 (09:40-10:00) OR breakout in UNKNOWN regime.

        Fires before regime confirmation (3-bar delay).  Uses fast MACD(5,10,3)
        for early sensitivity.  ZigZag analysis: OR breakouts account for 16%
        of upswings / 19% of downswings — worth capturing early.
        """
        if self._or_high is None or self._or_low is None:
            return None
        ctx = self.last_context
        assert ctx is not None
        macd_curr = self._macd_fast
        macd_prev_val = self._macd_fast_prev
        volume_ok = ctx.rvol_val >= RULES.regime_trend_min_volume_ratio
        if (
            ctx.current_close > self._or_high
            and macd_curr >= macd_prev_val
            and ctx.rsi_val < RULES.timed_call_rsi_max
            and volume_ok
        ):
            return self._signal(Direction.CALL, "regime_or_breakout", spot)
        if (
            ctx.current_close < self._or_low
            and macd_curr <= macd_prev_val
            and ctx.rsi_val > RULES.timed_put_rsi_min
            and volume_ok
        ):
            return self._signal(Direction.PUT, "regime_or_breakout", spot)
        return None

    def _range_signal(self, spot: Decimal | None) -> Signal | None:
        ctx = self.last_context
        assert ctx is not None
        volume_ok = (
            RULES.regime_range_min_volume_ratio
            <= ctx.rvol_val
            <= RULES.regime_range_max_volume_ratio
        )
        # Use fast MACD(5,10,3) for range — more sensitive
        macd_curr, macd_prev = self._active_macd()

        # Outer-band reversions only (strict) — inner-band reversion handled by
        # _or_reversion_signal which adds the OR-position gate for quality control
        lower_reentry = ctx.current_close <= ctx.boll_lower or (
            ctx.prev_close <= self._previous_boll_lower and ctx.current_close > ctx.boll_lower
        )
        upper_reentry = ctx.current_close >= ctx.boll_upper or (
            ctx.prev_close >= self._previous_boll_upper and ctx.current_close < ctx.boll_upper
        )
        if (
            lower_reentry
            and ctx.rsi_val <= RULES.regime_range_rsi_oversold
            and macd_curr > macd_prev
            and volume_ok
        ):
            return self._signal(Direction.CALL, "regime_range_reversion", spot)
        if (
            upper_reentry
            and ctx.rsi_val >= RULES.regime_range_rsi_overbought
            and macd_curr < macd_prev
            and volume_ok
        ):
            return self._signal(Direction.PUT, "regime_range_reversion", spot)
        return None

    def _or_reversion_signal(self, spot: Decimal | None) -> Signal | None:
        """OR-anchored mean reversion for Phase 3+ (after 10:00 ET).

        ZigZag analysis — 43 trading days Jul–Aug 2026, 215 top-5 swings:
          - 55% of upswing   starts: price < OR_LOW  (avg RSI 39, band_pos -0.86)
          - 38% of downswing starts: price > OR_HIGH (avg RSI 59, band_pos +0.78)

        Uses the OR as a daily fair-value reference: excursions beyond OR
        boundaries tend to revert.  More specific than raw band reversion
        because it requires BOTH OR position AND Bollinger band confirmation.

        Signal logic (fires in RANGE or UNKNOWN regime, Phase 3+ only):
          CALL  price < OR_LOW  AND  band_pos < -0.30  AND  RSI <= 40  AND  2-bar MACDf accel
          PUT   price > OR_HIGH AND  band_pos > +0.30  AND  RSI >= 60  AND  2-bar MACDf decel
        Exit at Bollinger middle band (same as regime_range_reversion).
        """
        if self._or_high is None or self._or_low is None:
            return None
        ctx = self.last_context
        assert ctx is not None

        half_width = max(ctx.boll_upper - ctx.boll_middle, Decimal("0.000001"))
        band_pos = (ctx.current_close - ctx.boll_middle) / half_width
        macd_curr  = self._macd_fast
        macd_prev  = self._macd_fast_prev
        macd_prev2 = self._macd_fast_prev2
        volume_ok = ctx.rvol_val >= RULES.regime_range_min_volume_ratio

        # ── CALL: buy the oversold dip below OR_LOW ─────────────────────────
        # ZigZag stats: 55% of upswings start with price < OR_LOW,
        # avg band_pos = -0.67, avg RSI = 39, MACD negative but accelerating.
        # Use -0.30 threshold (captures the full range including avg -0.67);
        # OR position + RSI<=40 + 2-bar MACD provide the quality gate.
        macd_accel_2bar = macd_curr > macd_prev > macd_prev2
        if (
            ctx.current_close < self._or_low
            and band_pos <= Decimal("-0.30")   # consistent with OR gate + RSI filter
            and ctx.rsi_val <= Decimal("40")   # avg RSI = 39 at upswing starts
            and macd_accel_2bar                # 2 bars of consecutive improvement
            and volume_ok
        ):
            return self._signal(Direction.CALL, "regime_or_reversion", spot)

        # ── PUT: fade the overbought push above OR_HIGH ─────────────────────
        # ZigZag stats: 38% of downswings start with price > OR_HIGH,
        # avg band_pos = +0.62, avg RSI = 59, MACD positive but decelerating.
        # band_pos >= 0.30 captures 78% of above-OR-HIGH downswings;
        # RSI >= 60 provides the quality filter (avg 59 in analysis).
        macd_decel_2bar = macd_curr < macd_prev < macd_prev2
        if (
            ctx.current_close > self._or_high
            and band_pos >= Decimal("0.30")    # RSI + MACD handle quality
            and ctx.rsi_val >= Decimal("60")   # avg RSI = 59 at DN starts
            and macd_decel_2bar
            and volume_ok
        ):
            return self._signal(Direction.PUT, "regime_or_reversion", spot)

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
        # Opening Range: highest high / lowest low of the 09:30-09:40 window
        or_collect_bars = [
            b for b in self._today_bars
            if b.start.astimezone(NY_TZ).time().replace(tzinfo=None) < time(9, 40)
        ]
        if or_collect_bars:
            self._or_high = max(b.high for b in or_collect_bars)
            self._or_low = min(b.low for b in or_collect_bars)
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
        if not self._dual_macd_values(closes):
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

        # ── Signal selection ─────────────────────────────────────────────────
        signal: Signal | None = None

        if state in {MarketState.TREND_UP, MarketState.TREND_DOWN}:
            # Trend regime: Phase 2 uses OR breakout, Phase 3+ uses band_pos ≥ 0.65
            signal = self._trend_signal(spot)

        elif state is MarketState.RANGE:
            # Range/oscillation regime: outer-band + middle-band reversion
            signal = self._range_signal(spot)
            # OR-anchored reversion as secondary: price below OR_LOW or above OR_HIGH
            if signal is None:
                signal = self._or_reversion_signal(spot)

        else:  # MarketState.UNKNOWN
            if local_time < RULES.phase_opening_end:
                # Phase 2 UNKNOWN: OR breakout takes priority over regime signals
                signal = self._phase2_or_breakout_signal(spot)
            # Fall through to range + OR-reversion regardless of phase
            if signal is None:
                signal = self._range_signal(spot)
            if signal is None:
                signal = self._or_reversion_signal(spot)
            # Momentum: last resort when no other signal fires
            if signal is None:
                signal = self._momentum_signal(spot)
        
        # Drop signals for directions blocked after repeated stop-losses
        if signal is not None and signal.direction in self._direction_blocked:
            return None

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
        # Mean-reversion strategies: exit when price returns to Bollinger middle band
        if position.strategy_name in (
            "regime_range_reversion",
            "regime_mean_reversion",
            "regime_or_reversion",
        ):
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
