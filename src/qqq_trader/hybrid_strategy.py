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
        # Trap signal cooldown: prevent consecutive trap fires on the same pivot
        self._last_trap_bar: datetime | None = None
        # Minimum signal score gate: suppress low-quality entries
        # Signals below this score are silently dropped in evaluate()
        self._min_signal_score: int = 4

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
        self._last_trap_bar = None

    def set_volatility_context(self, volatility_bars: Sequence[Bar], decision_at: datetime) -> None:
        self.indicators.set_volatility_context(volatility_bars, decision_at)
        self.vix_trend = self.indicators.vix_trend

    def record_entry(self, direction: Direction, entered_at: datetime) -> None:
        self.indicators.record_entry(direction, entered_at)

    def record_profitable_exit(self, direction: Direction, exited_at: datetime) -> None:
        self.indicators.record_profitable_exit(direction, exited_at)

    def record_stop_loss(self, direction: Direction, strategy: str = "") -> None:
        """Called after a stop-loss exit.

        After 2 stop-losses in the same direction, that direction is blocked
        for the rest of the day.  The lock resets at next day's _reset_day call.
        """
        count = self._stop_loss_count.get(direction, 0) + 1
        self._stop_loss_count[direction] = count
        if count >= 2:
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

    def _is_too_choppy(self, lookback: int = 5, max_reversals: int = 1) -> bool:
        """Return True if recent bars show excessive directional reversals.

        Counts how many times consecutive bar close directions reversed
        (UP→DOWN or DOWN→UP) over the last `lookback` bars. If more than
        `max_reversals` reversals, the market is too choppy for a trend entry.

        Example: closes [A↑B↓C↓D↑E] has 2 reversals (A→B and C→D→E).
        Default threshold: ≥2 reversals in 5 bars → too choppy.
        """
        bars = self._today_bars[-(lookback + 1):]
        if len(bars) < lookback + 1:
            return False
        closes = [b.close for b in bars]
        reversals = 0
        for i in range(1, len(closes) - 1):
            prev_dir = closes[i] - closes[i - 1]
            curr_dir = closes[i + 1] - closes[i]
            if (prev_dir > 0 and curr_dir < 0) or (prev_dir < 0 and curr_dir > 0):
                reversals += 1
        return reversals > max_reversals

    def _is_day_choppy(self, min_bars: int = 10, threshold: float = 0.20) -> bool:
        """Return True if today's session so far shows low directional persistence.

        Computes net / gross move ratio over today's bars:
          net   = |close[-1] - open[0]|       (one-way progress)
          gross = sum(|bar.close - bar.open|)  (total effort)

        Analysis of 43 trading days shows:
          Bad days (high stop-loss rate): ratio 0-16%
          Good days (trending):           ratio 11-39%

        A ratio below `threshold` (default 20%) with ≥ `min_bars` suggests
        the market is oscillating rather than trending → skip momentum entries.
        """
        bars = self._today_bars
        if len(bars) < min_bars:
            return False
        gross = sum(abs(b.close - b.open) for b in bars)
        if gross == 0:
            return False
        net = abs(bars[-1].close - bars[0].open)
        return float(net / gross) < threshold
        return reversals > max_reversals

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

    def _score_signal(self, direction: Direction) -> tuple[int, dict[str, str]]:
        """Compute multi-dimensional signal confluence score (0–11).

        Data-driven from 43-day ZigZag analysis (103 UP + 112 DN swing starts):

        CALL scoring — mean-reversion oversold bounce:
          RSI ≤ 40              0–3  (avg RSI at UP start = 39.1; 56% < 40)
          BandPos ≤ -0.65       0–3  (avg BandPos = -0.67; 62% < -0.65)
          Price < VWAP          0–2  (72% of UP swings start below VWAP)
          MACDf 2-bar accel     0–2  (turning from negative → timing gate)
          Volume ≥ threshold    0–1
          ─────────────────── max 11

        PUT scoring — momentum fade / exhaustion:
          MACDf > 0 AND accel   0–3  (91% MACDf>0; 78% accel at DN start)
          Price > VWAP          0–2  (67% of DN swings start above VWAP)
          BandPos ≥ +0.30       0–2  (78% of DN swings have band_pos>0.30)
          RSI 55–75             0–2  (avg RSI = 59 at DN start)
          Volume ≥ threshold    0–1  (optional quality gate)
          EMA9 > EMA21          0–1  (67% of DN swings have EMA bull)
          ─────────────────── max 11
        """
        ctx = self.last_context
        if ctx is None:
            return 0, {}

        score = 0
        details: dict[str, str] = {}

        half_width = max(ctx.boll_upper - ctx.boll_middle, Decimal("0.0001"))
        band_pos = (ctx.current_close - ctx.boll_middle) / half_width
        vwap = ctx.vwap_value
        macd_accel_2bar = self._macd_fast > self._macd_fast_prev > self._macd_fast_prev2
        details["vwap"] = str(vwap) if vwap > ZERO else "N/A"
        details["band_pos"] = str(band_pos)

        if direction is Direction.CALL:
            # ── Mean-reversion oversold bounce ──────────────────────────────
            rsi_score = 3 if ctx.rsi_val <= Decimal("40") else (2 if ctx.rsi_val <= Decimal("48") else 0)
            score += rsi_score
            details["score_rsi"] = str(rsi_score)

            bp_score = 3 if band_pos <= Decimal("-0.65") else (2 if band_pos <= Decimal("-0.30") else 0)
            score += bp_score
            details["score_band"] = str(bp_score)

            if vwap > ZERO and ctx.current_close < vwap:
                score += 2
                details["score_vwap"] = "2"
            else:
                details["score_vwap"] = "0"

            if macd_accel_2bar:
                score += 2
            details["score_macd_accel"] = "2" if macd_accel_2bar else "0"

            vol_score = 1 if ctx.rvol_val >= RULES.regime_trend_min_volume_ratio else 0
            score += vol_score
            details["score_volume"] = str(vol_score)

        else:  # PUT
            # ── Momentum fade / exhaustion ───────────────────────────────────
            macd_pos_and_accel = self._macd_fast > ZERO and macd_accel_2bar
            macd_score = 3 if macd_pos_and_accel else (1 if self._macd_fast > ZERO else 0)
            score += macd_score
            details["score_macd"] = str(macd_score)

            if vwap > ZERO and ctx.current_close > vwap:
                score += 2
                details["score_vwap"] = "2"
            else:
                details["score_vwap"] = "0"

            bp_score = 2 if band_pos >= Decimal("0.30") else 0
            score += bp_score
            details["score_band"] = str(bp_score)

            rsi_val = ctx.rsi_val
            rsi_score = 2 if Decimal("55") <= rsi_val <= Decimal("75") else (1 if rsi_val > Decimal("50") else 0)
            score += rsi_score
            details["score_rsi"] = str(rsi_score)

            ema_score = 1 if self._ema_fast > self._ema_slow else 0
            score += ema_score
            details["score_ema"] = str(ema_score)

            vol_score = 1 if ctx.rvol_val >= RULES.regime_trend_min_volume_ratio else 0
            score += vol_score
            details["score_volume"] = str(vol_score)

        details["signal_score"] = str(score)
        return score, details

    def _vwap_pullback_signal(self, spot: Decimal | None) -> Signal | None:
        """Data-driven entry signals from ZigZag swing analysis (43 days).

        CALL — oversold bounce BELOW VWAP (mean-reversion):
          72% of UP swing starts are BELOW VWAP (not above!)
          62% have BandPos ≤ -0.65, avg RSI = 39
          → Buy the oversold dip, not the breakout above VWAP.

        PUT — VWAP rejection in downtrend (retest of VWAP from below):
          Requires confirmed EMA downtrend (fast < slow) + bearish bar at VWAP.
          Complements _vwap_macd_fade_signal which is the broader PUT path.
        """
        ctx = self.last_context
        if ctx is None:
            return None
        vwap_val = ctx.vwap_value
        if vwap_val <= ZERO:
            return None
        if len(self._today_bars) < 3:
            return None

        atr_val = ctx.atr_val if ctx.atr_val > ZERO else Decimal("0.30")
        prev_bar = self._today_bars[-2]
        curr_bar = self._today_bars[-1]
        volume_ok = ctx.rvol_val >= RULES.regime_trend_min_volume_ratio

        half_width = max(ctx.boll_upper - ctx.boll_middle, Decimal("0.0001"))
        band_pos = (ctx.current_close - ctx.boll_middle) / half_width

        # Fast MACD (5,10,3) 2-bar acceleration
        macd_accel_2bar = self._macd_fast > self._macd_fast_prev > self._macd_fast_prev2

        # CALL: deeply oversold bounce below VWAP.
        # Use 1-bar MACD turn (not 3-bar) to fire earlier, closer to swing start.
        # RSI ≤ 43 covers 62% of missed UP swings (avg RSI=41); bullish bar + new
        # close high distinguish real bounces from drift-lower continuations.
        macd_turning_up_1bar = self._macd_fast > self._macd_fast_prev
        if (
            ctx.current_close < vwap_val
            and band_pos <= Decimal("-0.60")                       # near lower Bollinger band (avg missed=-0.58)
            and ctx.rsi_val <= Decimal("43")                       # deeply oversold (covers 62% of missed UPs)
            and macd_turning_up_1bar                               # MACD just started turning up
            and curr_bar.close > curr_bar.open                     # bullish candle
            and curr_bar.close > prev_bar.close                    # making new close high
            and volume_ok
            and self.last_state is not MarketState.TREND_DOWN
            and Direction.CALL not in self._direction_blocked
        ):
            return self._signal(Direction.CALL, "vwap_bounce_call", spot)

        # PUT: price tests VWAP from below in downtrend, gets rejected.
        # Require MACD still accelerating negative → rejects entries where the
        # swing momentum has already peaked and MACD is starting to flatten.
        macd_still_falling = self._macd_fast < self._macd_fast_prev
        if (
            self._ema_fast < self._ema_slow                        # EMA downtrend confirmed
            and prev_bar.high >= vwap_val - atr_val * Decimal("0.5")  # prior bar reached VWAP zone
            and ctx.current_close < vwap_val                       # now rejected below VWAP
            and curr_bar.close < curr_bar.open                     # bearish candle
            and self._macd_fast < ZERO                             # MACD still negative
            and macd_still_falling                                 # MACD still accelerating ↓
            and RULES.timed_put_rsi_min < ctx.rsi_val <= Decimal("55")
            and volume_ok
            and self.last_state is not MarketState.TREND_UP
            and Direction.PUT not in self._direction_blocked
        ):
            return self._signal(Direction.PUT, "vwap_pullback", spot)

        return None

    def _vwap_macd_fade_signal(self, spot: Decimal | None) -> Signal | None:
        """Fade the rally: PUT when fast MACD positive and accelerating above VWAP.

        Data-driven (43 days, 112 DN swing starts):
          91% have MACDf > 0 at DN start
          78% have MACDf accelerating
          67% have price > VWAP
          VWAP_above + MACDf_accel combined = 52% hit rate on DN swings
          (vs current _or_reversion which covers only 23%)

        This is the primary broad PUT signal, complementing the OR-anchored
        _or_reversion_signal which requires OR_HIGH break.
        """
        ctx = self.last_context
        if ctx is None:
            return None
        vwap_val = ctx.vwap_value
        if vwap_val <= ZERO:
            return None
        if len(self._today_bars) < 3:
            return None

        # Fast MACD (5,10,3) — 2-bar acceleration from positive territory
        macd_accel_2bar = self._macd_fast > self._macd_fast_prev > self._macd_fast_prev2

        half_width = max(ctx.boll_upper - ctx.boll_middle, Decimal("0.0001"))
        band_pos = (ctx.current_close - ctx.boll_middle) / half_width
        volume_ok = ctx.rvol_val >= RULES.regime_trend_min_volume_ratio

        if (
            ctx.current_close > vwap_val                           # above VWAP (67% of DN starts)
            and self._macd_fast > ZERO                             # MACD positive (91% of DN starts)
            and macd_accel_2bar                                    # still accelerating (78% of DN starts)
            and band_pos >= Decimal("0.50")                        # tightened: near upper band
            and Decimal("65") <= ctx.rsi_val <= Decimal("80")     # overbought (tightened from 55)
            and volume_ok
            and self.last_state is not MarketState.TREND_UP        # not in confirmed strong uptrend
            and Direction.PUT not in self._direction_blocked
        ):
            return self._signal(Direction.PUT, "vwap_macd_fade", spot)

        return None

    def _momentum_exhaustion_signal(self, spot: Decimal | None) -> Signal | None:
        """POST-PEAK exhaustion reversal: PUT when MACD is positive but decelerating.

        Diagnosis of 73 missed DN swing starts:
          74% have MACDf > 0  — but our vwap_macd_fade requires MACDf still ACCELERATING.
          64% have BandPos > +0.30  — market is extended.
          56% are above VWAP.
          Many swing starts happen AFTER the MACD peak (deceleration, not acceleration).

        Signal fires when:
          - Price above VWAP (56–67% of DN starts)
          - MACD positive but decelerating: fast_curr > 0 AND fast_curr < fast_prev
          - BandPos ≥ +0.40 (extended — somewhat higher gate than vwap_macd_fade's +0.30)
          - RSI ≥ 58 (elevated)
          - 15-bar cooldown (shared with vwap_macd_fade to avoid back-to-back entries)
        """
        ctx = self.last_context
        if ctx is None:
            return None
        vwap_val = ctx.vwap_value
        if vwap_val <= ZERO:
            return None
        if len(self._today_bars) < 3:
            return None

        half_width = max(ctx.boll_upper - ctx.boll_middle, Decimal("0.0001"))
        band_pos = (ctx.current_close - ctx.boll_middle) / half_width
        volume_ok = ctx.rvol_val >= RULES.regime_trend_min_volume_ratio

        macd_decel = (
            self._macd_fast > ZERO                  # still positive (not fully reversed)
            and self._macd_fast < self._macd_fast_prev   # but now decelerating from peak
            and self._macd_fast_prev > ZERO         # prev also positive (not spike)
        )
        if (
            ctx.current_close > vwap_val
            and macd_decel
            and band_pos >= Decimal("0.50")         # tightened: avg BandPos=+0.42 at missed DNs
            and ctx.rsi_val >= Decimal("63")        # tightened: avg RSI=56.5 at missed DNs
            and volume_ok
            and self.last_state is not MarketState.TREND_UP
            and Direction.PUT not in self._direction_blocked
        ):
            return self._signal(Direction.PUT, "momentum_exhaustion_put", spot)

        return None

    def _deep_oversold_bounce_call(self, spot: Decimal | None) -> Signal | None:
        """Counter-trend CALL bounce from deeply oversold levels.

        Diagnosis of 85 missed UP swing starts:
          65% have RSI < 45  (deeply oversold)
          72% have BandPos < -0.30  (below BB middle)
          67% below VWAP
          27% start in TREND_DOWN regime — vwap_bounce_call is blocked there

        vwap_bounce_call requires macd_accel_2bar, but at a true swing bottom
        MACD is still decelerating. This signal targets the opposite: deep oversold
        + price makes a lower wick (hammer) or volume spike, signalling exhaustion.

        Conditions (stricter RSI to compensate for relaxed MACD):
          - RSI ≤ 38 (deeply oversold — tighter than vwap_bounce_call's 48)
          - BandPos ≤ -0.50 (near lower Bollinger band)
          - Price below VWAP
          - Current bar closes ABOVE its open (bullish bar — MACD direction not required)
          - Volume OK
          - Shared bounce cooldown (15-bar) with vwap_bounce_call
        """
        ctx = self.last_context
        if ctx is None:
            return None
        vwap_val = ctx.vwap_value
        if vwap_val <= ZERO:
            return None
        if len(self._today_bars) < 3:
            return None

        curr_bar = self._today_bars[-1]
        volume_ok = ctx.rvol_val >= RULES.regime_trend_min_volume_ratio
        half_width = max(ctx.boll_upper - ctx.boll_middle, Decimal("0.0001"))
        band_pos = (ctx.current_close - ctx.boll_middle) / half_width

        if (
            ctx.current_close < vwap_val            # below VWAP (67% of missed UPs)
            and band_pos <= Decimal("-0.60")         # tightened: avg BandPos=-0.49 at missed UPs
            and ctx.rsi_val <= Decimal("35")         # tightened: avg RSI=42 at missed UPs
            and curr_bar.close > curr_bar.open       # bullish bar (exhaustion hint)
            and volume_ok
            and Direction.CALL not in self._direction_blocked
        ):
            return self._signal(Direction.CALL, "deep_oversold_bounce", spot)

        return None

    def _macd_narrowing_call(self, spot: Decimal | None) -> Signal | None:
        """CALL entry when MACD histogram is negative but narrowing (momentum weakening).

        Covers 70% of missed UP swings where MACDf < 0 at swing start.
        The swing bottom typically fires when downward MACD momentum SHRINKS:
          hist < 0  AND  hist > hist_prev  (less negative than before)
          → downward momentum is decelerating → reversal approaching

        Combined with pyramid scaling (30% initial entry), this catches the
        early bounce before MACD crosses zero.

        Conditions:
          - MACDf < 0  (still negative — not a zero-cross, that's vwap_bounce_call)
          - MACDf > MACDf_prev  (narrowing from below)
          - MACDf_prev < 0  (previous bar also negative — confirms narrowing, not a spike)
          - RSI ≤ 48  (oversold; covers the 33% of missed UPs with RSI 40-48)
          - BandPos ≤ -0.30  (below BB middle; 70% of missed UPs)
          - Price below VWAP  (67% of missed UPs)
          - 12-bar cooldown (independent tracker)
        """
        ctx = self.last_context
        if ctx is None:
            return None
        vwap_val = ctx.vwap_value
        if vwap_val <= ZERO:
            return None
        if len(self._today_bars) < 3:
            return None

        volume_ok = ctx.rvol_val >= RULES.regime_trend_min_volume_ratio
        half_width = max(ctx.boll_upper - ctx.boll_middle, Decimal("0.0001"))
        band_pos = (ctx.current_close - ctx.boll_middle) / half_width

        macd_narrowing_from_below = (
            self._macd_fast < ZERO                      # still negative
            and self._macd_fast > self._macd_fast_prev  # but less negative (narrowing)
            and self._macd_fast_prev < ZERO             # prev also negative (not a spike)
        )
        if (
            ctx.current_close < vwap_val
            and macd_narrowing_from_below
            and band_pos <= Decimal("-0.45")         # tightened: avg=-0.49 at missed UPs
            and ctx.rsi_val <= Decimal("42")         # tightened: avg=42.2 at missed UPs
            and volume_ok
            and self.last_state is not MarketState.TREND_DOWN
            and Direction.CALL not in self._direction_blocked
        ):
            return self._signal(Direction.CALL, "macd_narrowing_call", spot)

        return None

    def _macd_narrowing_put(self, spot: Decimal | None) -> Signal | None:
        """PUT entry when MACD histogram is positive but narrowing (momentum weakening).

        Covers 74% of missed DN swings where MACDf > 0 at swing start.
        The swing top typically fires when upward MACD momentum SHRINKS:
          hist > 0  AND  hist < hist_prev  (less positive than before)
          → upward momentum is decelerating → reversal approaching

        This is the symmetric counterpart of _macd_narrowing_call.

        Conditions:
          - MACDf > 0  (still positive)
          - MACDf < MACDf_prev  (narrowing from above)
          - MACDf_prev > 0  (previous bar also positive)
          - RSI ≥ 52  (elevated; covers the 29% of missed DNs with RSI 50-60)
          - BandPos ≥ +0.30  (above BB middle; 64% of missed DNs)
          - Price above VWAP (56% of missed DNs)
          - Not TREND_UP
          - 12-bar cooldown (independent tracker)
        """
        ctx = self.last_context
        if ctx is None:
            return None
        vwap_val = ctx.vwap_value
        if vwap_val <= ZERO:
            return None
        if len(self._today_bars) < 3:
            return None

        volume_ok = ctx.rvol_val >= RULES.regime_trend_min_volume_ratio
        half_width = max(ctx.boll_upper - ctx.boll_middle, Decimal("0.0001"))
        band_pos = (ctx.current_close - ctx.boll_middle) / half_width

        macd_narrowing_from_above = (
            self._macd_fast > ZERO                      # still positive
            and self._macd_fast < self._macd_fast_prev  # but less positive (narrowing)
            and self._macd_fast_prev > ZERO             # prev also positive (not a spike)
        )
        if (
            ctx.current_close > vwap_val
            and macd_narrowing_from_above
            and band_pos >= Decimal("0.45")         # tightened: avg=+0.42 at missed DNs
            and ctx.rsi_val >= Decimal("60")         # tightened: avg=56.5 at missed DNs
            and volume_ok
            and self.last_state is not MarketState.TREND_UP
            and Direction.PUT not in self._direction_blocked
        ):
            return self._signal(Direction.PUT, "macd_narrowing_put", spot)

        return None

    def _trap_signal(self, spot: Decimal | None) -> Signal | None:
        """Detect false breakout (trap) patterns and trade the reversal.

        False breakout above OR_HIGH → PUT:
          prev bar high > OR_HIGH, but volume was thin (rvol_prev < 1.0)
          OR the breakout bar had a large upper wick (>60 % of bar range),
          AND the current bar closes back below OR_HIGH.

        False breakdown below OR_LOW → CALL:
          Symmetric logic on the downside.

        Trap cooldown: after firing, waits ≥ 3 bars before firing again to
        prevent consecutive duplicate signals on the same reversal pivot.
        """
        if self._or_high is None or self._or_low is None:
            return None
        if len(self._today_bars) < 3:
            return None
        ctx = self.last_context
        if ctx is None:
            return None

        # Cooldown: skip if we fired a trap signal within the last 3 bars
        if self._last_trap_bar is not None and ctx.bar_end is not None:
            bars_since = sum(
                1 for b in self._today_bars if b.end > self._last_trap_bar
            )
            if bars_since < 3:
                return None

        prev_bar = self._today_bars[-2]
        curr_bar = self._today_bars[-1]
        macd_curr, macd_prev = self._active_macd()
        bar_range = prev_bar.high - prev_bar.low

        # ── False breakout above OR_HIGH → PUT ──────────────────────────────
        if (
            prev_bar.high > self._or_high                         # prior bar poked above
            and self.last_state is not MarketState.TREND_UP       # not in strong uptrend
            and curr_bar.close < self._or_high                    # price retreated
            and macd_curr <= macd_prev                            # MACD weakening
            and ctx.rsi_val <= Decimal("62")
            and ctx.rvol_val >= RULES.regime_trend_min_volume_ratio
            and Direction.PUT not in self._direction_blocked
        ):
            # Volume or wick must confirm the false breakout
            thin_volume = ctx.rvol_prev < Decimal("1.0")
            upper_wick = (
                bar_range > ZERO
                and (prev_bar.high - prev_bar.close) / bar_range > Decimal("0.55")
            )
            if thin_volume or upper_wick:
                sig = self._signal(Direction.PUT, "trap_false_breakout", spot)
                self._last_trap_bar = sig.bar_end
                return sig

        # ── False breakdown below OR_LOW → CALL ─────────────────────────────
        if (
            prev_bar.low < self._or_low                           # prior bar poked below
            and self.last_state is not MarketState.TREND_DOWN     # not in strong downtrend
            and curr_bar.close > self._or_low                     # price recovered
            and macd_curr >= macd_prev                            # MACD strengthening
            and ctx.rsi_val >= Decimal("38")
            and ctx.rvol_val >= RULES.regime_trend_min_volume_ratio
            and Direction.CALL not in self._direction_blocked
        ):
            thin_volume = ctx.rvol_prev < Decimal("1.0")
            lower_wick = (
                bar_range > ZERO
                and (prev_bar.close - prev_bar.low) / bar_range > Decimal("0.55")
            )
            if thin_volume or lower_wick:
                sig = self._signal(Direction.CALL, "trap_false_breakdown", spot)
                self._last_trap_bar = sig.bar_end
                return sig

        return None

    def _signal(self, direction: Direction, strategy: str, spot: Decimal | None) -> Signal:
        ctx = self.last_context
        assert ctx is not None and ctx.bar_end is not None
        score, score_details = self._score_signal(direction)
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
            **score_details,
        }
        return Signal(
            direction=direction,
            bar_end=ctx.bar_end,
            spot=spot or ctx.current_close,
            strategy=strategy,
            market_state=self.last_state,
            indicators=indicators,
            vwap=ctx.vwap_value if ctx.vwap_value > ZERO else None,
            atr=ctx.atr_val if ctx.atr_val > ZERO else None,
        )

    def pyramid_add_decision(self, position: "Position") -> tuple[int, str] | None:
        """Return (contracts_to_add, stage_name) for pyramid scaling, or None.

        Pyramid schedule (relative to full-size target):
          Stage 0 → 1 : add 40% after spot gains ≥ 0.25 ATR in direction  (total 70%)
          Stage 1 → 2 : add remaining 30% after spot gains ≥ 0.50 ATR      (total 100%)

        Conditions:
          - MACD (fast) must not be reversing against the trade
          - Bar must be before 12:00 ET (avoid theta drag on late adds)
          - pyramid_target_qty must be set on the Position

        Why these thresholds?
          0.25 ATR = ~$0.07 on a typical $0.28 ATR day → confirms initial thesis
          0.50 ATR = ~$0.14 → strong follow-through before committing full size
        """
        from .domain import Position as _Position  # avoid circular at module level
        ctx = self.last_context
        if ctx is None or position.entry_spot is None:
            return None
        if position.pyramid_target_qty <= 0 or position.pyramid_stage >= 2:
            return None

        bar_time = ctx.bar_end.astimezone(NY_TZ).time().replace(tzinfo=None)
        if bar_time >= RULES.phase_main_end:
            return None

        atr = ctx.atr_val if ctx.atr_val > ZERO else Decimal("0.30")
        entry_spot = position.entry_spot
        current = ctx.current_close

        if position.direction is Direction.CALL:
            gained = current - entry_spot
            macd_ok = self._macd_fast >= self._macd_fast_prev
        else:
            gained = entry_spot - current
            macd_ok = self._macd_fast <= self._macd_fast_prev

        if not macd_ok:
            return None

        if position.pyramid_stage == 0:
            threshold = atr * Decimal("0.25")
            if gained >= threshold:
                add_qty = max(1, round(position.pyramid_target_qty * Decimal("0.40")))
                return (add_qty, "pyramid_add_1")

        elif position.pyramid_stage == 1:
            threshold = atr * Decimal("0.50")
            if gained >= threshold:
                already_in = position.quantity
                add_qty = max(1, position.pyramid_target_qty - already_in)
                return (add_qty, "pyramid_add_2")

        return None

    def _trend_signal(self, spot: Decimal | None) -> Signal | None:
        ctx = self.last_context
        assert ctx is not None
        assert ctx.bar_end is not None
        # Chop filter: too many directional reversals in recent bars → skip trend entry
        if self._is_too_choppy():
            return None
        # Day-level chop filter: if session shows very low directional persistence
        # (net/gross < 20%), regime may have been falsely classified as TREND
        if self._is_day_choppy(min_bars=10, threshold=0.20):
            return None
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
        """Pure momentum entry: 3 consecutive rising/falling bars.

        ZigZag analysis (43 days, 215 top-5 swings) shows two opening patterns:
          CALL: oversold flush (RSI~39, price below OR, MACDf negative at start)
          PUT:  overbought push  (RSI~59, MACDf positive at start)
        Both are captured reliably by the 3-bar rule + volume confirmation.

        Note: adding MACD direction gates here was tested but rejected — both
        the Jul-17 CALL (extreme bounce, MACDf turned positive mid-reversal) and
        the Aug-03 CALL (strong opening trend, MACDf positive throughout) would
        have been blocked, losing the strategy's two largest winning sessions.
        """
        if len(self._today_bars) < 4:
            return None

        ctx = self.last_context
        assert ctx is not None

        # Skip on choppy sessions: if today's net/gross move ratio is low,
        # momentum entries are unreliable (8/10, 8/12, 8/17 analysis: 0-16% ratio)
        if self._is_day_choppy(min_bars=10, threshold=0.20):
            return None

        last_3 = self._today_bars[-3:]
        all_rising = all(
            last_3[i].close > last_3[i - 1].close for i in range(1, len(last_3))
        )
        all_falling = all(
            last_3[i].close < last_3[i - 1].close for i in range(1, len(last_3))
        )

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
        # Chop filter: too many reversals → wait for consistent momentum
        if self._is_too_choppy():
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
        """OR-anchored mean reversion for RANGE or UNKNOWN regime.

        ZigZag analysis — 43 trading days Jul–Aug 2026, 215 top-5 swings:
          UP   starts: 55% price < OR_LOW,  avg RSI 39, avg band_pos -0.67
          DOWN starts: 38% price > OR_HIGH, avg RSI 59, avg band_pos +0.62

        Key parameter changes vs. initial version:
          CALL band_pos: -0.30 → -0.65  (matches the actual avg -0.67; the
            original -0.30 admitted too many shallow dips that were noise)
          CALL time gate: signal only fires at ≥10:00 ET to skip the noisy
            09:40-10:00 opening-settle window where OR is freshly formed.
          CALL MACD: 2-bar accel retained — provides a reliable entry-timing
            gate regardless of the absolute histogram level.
          PUT side: unchanged (decel_2bar + RSI≥60 confirmed working in analysis)

        Signal logic:
          CALL  ≥10:00  price < OR_LOW  AND  band_pos ≤ -0.65  AND  RSI ≤ 40
                AND  2-bar MACDf accel
          PUT   any     price > OR_HIGH AND  band_pos ≥ +0.30  AND  RSI ≥ 60
                AND  2-bar MACDf decel
        Exit at Bollinger middle band.
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
        bar_time = ctx.bar_end.astimezone(NY_TZ).time().replace(tzinfo=None)

        # ── CALL: buy the oversold dip below OR_LOW ─────────────────────────
        # ZigZag stats: avg band_pos = -0.67 at UP swing starts; -0.65 gate
        # targets the confirmed deeply-oversold cases and eliminates shallow
        # borderline dips that are more likely to continue lower.
        # ≥10:00 gate: the OR settles in 09:30-09:40; in 09:40-10:00 the market
        # is still in price-discovery mode — OR reversion signals there are
        # statistically unreliable and better handled by _phase2_or_breakout_signal.
        macd_accel_2bar = macd_curr > macd_prev > macd_prev2
        if (
            bar_time >= RULES.phase_opening_end   # ≥10:00 — skip opening settle
            and ctx.current_close < self._or_low
            and band_pos <= Decimal("-0.65")       # avg -0.67; filters shallow dips
            and ctx.rsi_val <= Decimal("40")       # avg RSI = 39 at UP starts
            and macd_accel_2bar                    # 2-bar recovery: timing quality gate
            and volume_ok
        ):
            return self._signal(Direction.CALL, "regime_or_reversion", spot)

        # ── PUT: fade the overbought push above OR_HIGH ─────────────────────
        # ZigZag stats: avg RSI = 59, avg band_pos = +0.62 at DN swing starts.
        # Waiting for MACD to peak and decelerate (2-bar) confirms the rally
        # has exhausted before entering the put — unchanged from original.
        macd_decel_2bar = macd_curr < macd_prev < macd_prev2
        if (
            ctx.current_close > self._or_high
            and band_pos >= Decimal("0.30")        # wide gate — RSI + MACD handle quality
            and ctx.rsi_val >= Decimal("60")       # avg RSI = 59 at DN starts
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

        # After noon, only high-confidence entries survive theta decay:
        # raise the score floor so weak signals don't waste capital in the
        # afternoon when 0DTE options lose value fast.
        original_min_score = self._min_signal_score
        afternoon = time(12, 0)
        if local_time >= afternoon:
            self._min_signal_score = max(self._min_signal_score, 7)

        # ── Signal selection ─────────────────────────────────────────────────
        signal: Signal | None = None

        if state in {MarketState.TREND_UP, MarketState.TREND_DOWN}:
            # Trend regime: Phase 2 uses OR breakout, Phase 3+ uses band_pos ≥ 0.65
            signal = self._trend_signal(spot)
            # VWAP pullback: secondary entry when trend signal doesn't fire
            if signal is None:
                signal = self._vwap_pullback_signal(spot)

        elif state is MarketState.RANGE:
            # Range/oscillation regime: VWAP-based signals.
            # regime_or_reversion removed (4% alignment, 96% noise)
            # vwap_macd_fade/momentum_exhaustion/deep_oversold/macd_narrowing: all <10% alignment, removed
            signal = self._vwap_pullback_signal(spot)

        else:  # MarketState.UNKNOWN
            if local_time < RULES.phase_opening_end:
                # Phase 2 UNKNOWN: OR breakout takes priority over regime signals
                signal = self._phase2_or_breakout_signal(spot)
            if signal is None:
                signal = self._vwap_pullback_signal(spot)
            # Momentum: last resort when no other signal fires
            if signal is None:
                signal = self._momentum_signal(spot)

        # Trap signal: applicable in any regime after primary signals exhausted
        if signal is None:
            signal = self._trap_signal(spot)

        # Drop signals for directions blocked after repeated stop-losses
        if signal is not None and signal.direction in self._direction_blocked:
            self._min_signal_score = original_min_score
            return None

        # Score gate: suppress low-quality signals to reduce false entries.
        # The score is embedded in signal.indicators["signal_score"] by _signal().
        # Structural signals with their own entry conditions are exempt since they
        # have rigorous multi-indicator gates that substitute for the score check.
        # Trend regime signals are exempt (regime confirmation acts as the gate).
        _SCORE_EXEMPT_STRATEGIES = {
            "regime_trend_following", "regime_trend_or_breakout",
            "regime_or_breakout", "regime_momentum_3bar",
            "vwap_pullback",           # EMA downtrend + VWAP rejection gate
            "vwap_bounce_call",        # RSI≤40 + BandPos≤-0.45 + bullish bar + new high gate
        }
        # regime_or_reversion has 94% noise rate — require a stricter score threshold
        _OR_REVERSION_MIN_SCORE = 6
        if signal is not None and signal.strategy not in _SCORE_EXEMPT_STRATEGIES:
            raw_score = int(signal.indicators.get("signal_score", "0"))
            floor = (
                _OR_REVERSION_MIN_SCORE
                if signal.strategy == "regime_or_reversion"
                else self._min_signal_score
            )
            if raw_score < floor:
                self._min_signal_score = original_min_score
                return None

        if signal is not None and signal.bar_end == self.last_signal_bar:
            return None
        if signal is not None:
            self.last_signal_bar = signal.bar_end
        self._min_signal_score = original_min_score
        return signal

    def bar_exit_decision(self, position: Position) -> ExitDecision | None:
        """Regime-specific bar exit; price risk and 13:55 close stay in RiskEngine."""
        ctx = self.last_context
        if ctx is None:
            return None
        # Mean-reversion strategies
        if position.strategy_name in (
            "regime_range_reversion",
            "regime_mean_reversion",
            "regime_or_reversion",
        ):
            # Exit at Bollinger middle band (correct for 0DTE options:
            # time decay is severe, so 2-bar exits at mid-band are appropriate).
            # Post-exit QQQ continuation does not translate to option gains due to theta.
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

        # ATR trailing stop: for trend and pullback strategies, trail by 1 ATR
        # once the trade has moved at least 0.5 ATR in our favor.
        atr_val = ctx.atr_val
        if (
            atr_val > ZERO
            and position.strategy_name in (
                "regime_trend_following",
                "regime_trend_or_breakout",
                "regime_or_breakout",
                "regime_momentum_3bar",
                "vwap_pullback",
            )
            and position.entry_spot is not None
        ):
            bars_since = [b for b in self._today_bars if b.end > position.opened_at]
            if bars_since:
                if position.direction is Direction.CALL:
                    peak = max(b.close for b in bars_since)
                    # Activate only once the trade is comfortably profitable
                    if peak >= position.entry_spot + atr_val * Decimal("0.5"):
                        trail_level = peak - atr_val
                        if ctx.current_close <= trail_level:
                            return ExitDecision(ExitReason.TRAILING_STOP, position.quantity)
                else:
                    trough = min(b.close for b in bars_since)
                    if trough <= position.entry_spot - atr_val * Decimal("0.5"):
                        trail_level = trough + atr_val
                        if ctx.current_close >= trail_level:
                            return ExitDecision(ExitReason.TRAILING_STOP, position.quantity)

        return None

    @property
    def day_mode(self) -> str | None:
        return self._day_mode


# Backwards-compatible import name used by older integrations.
AdaptiveEngine = HybridEngine
