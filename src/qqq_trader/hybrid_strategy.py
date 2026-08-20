"""HybridEngine — 自适应双引擎策略 (Dual-Engine Hybrid Strategy).

核心理念：优势互补
  - BOLL/MACD 引擎：擅长快速捕捉动量入场、震荡日 MACD 反转止盈
  - Trend/ORB 引擎：擅长确认方向突破、趋势日 EMA 跟踪持有
  - 组合：两个引擎独立触发入场，10:00 后根据市场性质分流出场

Architecture:
  Phase 1 — Accumulation (9:30 ~ 9:39):
    Both engines warm up indicators and build the Opening Range.
    No signals are emitted.

  Phase 2 — Dual-Signal Entry (9:40 ~ 10:00):
    Two complementary signal sources fire independently:
      A) BOLL/MACD opening signals — volume-driven momentum entries
      B) Trend ORB breakout signals — sustained breakout with EMA/VWAP/MACD
    All positions use unified exit logic before mode is decided:
      - MACD reversal → quick profit capture on direction change
      - EMA slow cross → trend invalidation / stop-loss

  Phase 3 — Day Classification & Adaptive Exit (10:00+):
    Based on accumulated evidence, classify the day:
      - TREND day (sustained breakout, directional EMA, low VWAP crosses):
        HOLD positions with Trend EMA trailing exit. New Trend signals allowed.
      - OSCILLATION day (breakout faded, choppy, mean-reverting):
        Exit on MACD reversal. New BOLL/MACD signals allowed.

    优势互补体现:
      - 趋势日：早期 BOLL/MACD 或 Trend 入场 → 10:00后"拿住" → 吃到大波段
      - 震荡日：早期入场 → MACD 反转快出 → 不会被套
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, time
from decimal import Decimal

from .config import NY_TZ
from .domain import Bar, Direction, ExitDecision, ExitReason, MarketState, Position, Signal
from .indicators import ema_series, macd_histogram, vwap
from .policy import RULES
from .strategy import StrategyEngine
from .trend_strategy import TrendFollowingEngine
from .volatility import VixFiveMinuteTrend

ZERO = Decimal(0)

# ── Day classification parameters ──
OR_CONFIRM_BARS = 3
OR_MIN_EXTENSION_PCT = Decimal("0.20")
OR_MIN_WIDTH_PCT = Decimal("0.0015")  # OR must be >= 0.15% of price to qualify

# ── Phase 2 risk filters ──
VIX_INTRADAY_RISE_THRESHOLD = Decimal("0.03")  # Block CALL if VIX rises > 3% from day open


class HybridEngine:
    """自适应双引擎策略：BOLL/MACD + Trend 优势互补.

    Entry:
      Phase 2 (9:40-10:00): accepts signals from BOTH engines independently.
        A) BOLL/MACD opening signals (volume + direction)
        B) Custom OR breakout (sustained break + EMA + VWAP + MACD alignment)
      Phase 3 (10:00+): only the active mode's engine generates new signals.

    Exit:
      Before mode decision: unified exit (MACD reversal + EMA trailing).
      After mode decision:
        - Trend day → EMA trailing (拿住)
        - Oscillation day → MACD reversal (快出)
    """

    def __init__(self, settings) -> None:
        self.settings = settings
        self.trend = TrendFollowingEngine(settings)
        self.boll_macd = StrategyEngine(settings)

        self._current_day: date | None = None
        self._day_mode: str | None = None  # None | 'trend' | 'oscillation'

        # Custom OR breakout state
        self._or_high: Decimal | None = None
        self._or_low: Decimal | None = None
        self._or_built = False
        self._or_signal_fired = False

        self._today_bars: list[Bar] = []
        self.last_signal_bar: datetime | None = None
        self.last_context = None
        self.last_state = MarketState.UNKNOWN
        self.vix_trend = VixFiveMinuteTrend.NEUTRAL
        self._vix_intraday_change: Decimal = ZERO

    def _reset_day(self, trading_day: date) -> None:
        self._current_day = trading_day
        self._day_mode = None
        self._or_high = None
        self._or_low = None
        self._or_built = False
        self._or_signal_fired = False
        self._today_bars = []

    # ────────────────────────────────────────────────────────────────────
    # Lifecycle hooks (called by backtest/risk engine)
    # ────────────────────────────────────────────────────────────────────

    def set_volatility_context(
        self, volatility_bars: Sequence[Bar], decision_at: datetime
    ) -> None:
        self.trend.set_volatility_context(volatility_bars, decision_at)
        self.boll_macd.set_volatility_context(volatility_bars, decision_at)
        self.vix_trend = self.trend.vix_trend

        decision_date = decision_at.astimezone(NY_TZ).date()
        today_vix = sorted(
            (b for b in volatility_bars
             if b.complete and b.end <= decision_at
             and b.end.astimezone(NY_TZ).date() == decision_date),
            key=lambda b: b.end,
        )
        if today_vix:
            vix_open = today_vix[0].open
            vix_current = today_vix[-1].close
            self._vix_intraday_change = (
                (vix_current - vix_open) / vix_open if vix_open > ZERO else ZERO
            )
        else:
            self._vix_intraday_change = ZERO

    def record_entry(self, direction: Direction, entered_at: datetime) -> None:
        self.trend.record_entry(direction, entered_at)
        self.boll_macd.record_entry(direction, entered_at)

    def record_profitable_exit(
        self, direction: Direction, exited_at: datetime
    ) -> None:
        self.trend.record_profitable_exit(direction, exited_at)
        self.boll_macd.record_profitable_exit(direction, exited_at)

    def _sync_state(self, source) -> None:
        if source.last_context is not None:
            self.last_context = source.last_context
        self.last_state = source.last_state

    # ════════════════════════════════════════════════════════════════════
    # Phase 2B: Custom OR Breakout (EMA + VWAP + MACD confirmation)
    # ════════════════════════════════════════════════════════════════════

    def _build_or(self, today: list[Bar]) -> None:
        """Build Opening Range from phase_collect bars."""
        or_bars = [
            b for b in today
            if b.start.astimezone(NY_TZ).time().replace(tzinfo=None) < RULES.phase_collect_end
        ]
        if not or_bars:
            return
        self._or_high = max(b.high for b in or_bars)
        self._or_low = min(b.low for b in or_bars)
        self._or_built = True

    def _count_consecutive_beyond_or(self, today: list[Bar]) -> tuple[int, int]:
        """Count consecutive recent bars above OR_high / below OR_low."""
        above = 0
        below = 0
        if self._or_high is None or self._or_low is None:
            return 0, 0
        for bar in reversed(today):
            t = bar.start.astimezone(NY_TZ).time().replace(tzinfo=None)
            if t < RULES.phase_collect_end:
                break
            if bar.close > self._or_high:
                above += 1
            else:
                break
        for bar in reversed(today):
            t = bar.start.astimezone(NY_TZ).time().replace(tzinfo=None)
            if t < RULES.phase_collect_end:
                break
            if bar.close < self._or_low:
                below += 1
            else:
                break
        return above, below

    def _or_breakout_signal(
        self, today: list[Bar], current: Bar, spot: Decimal | None
    ) -> Signal | None:
        """Custom OR breakout: consecutive bars + EMA + VWAP + MACD confirmation."""
        if self._or_signal_fired:
            return None
        if self._or_high is None or self._or_low is None:
            return None

        closes = [b.close for b in today]
        if len(closes) < RULES.trend_ema_slow:
            return None

        above_count, below_count = self._count_consecutive_beyond_or(today)

        ema_fast = ema_series(closes, RULES.trend_ema_fast)[-1]
        ema_slow = ema_series(closes, RULES.trend_ema_slow)[-1]
        current_vwap = vwap(today)

        macd_fast = min(RULES.timed_macd_fast, 8)
        macd_slow = min(RULES.timed_macd_slow, 17)
        macd_signal_p = min(RULES.timed_macd_signal, 6)
        required_macd = macd_slow + macd_signal_p - 1
        macd_hist_val = ZERO
        if len(closes) >= required_macd:
            _, _, macd_hist_val = macd_histogram(
                closes, macd_fast, macd_slow, macd_signal_p
            )

        or_range = self._or_high - self._or_low
        if or_range <= ZERO:
            return None

        or_mid = (self._or_high + self._or_low) / 2
        if or_mid > ZERO and or_range / or_mid < OR_MIN_WIDTH_PCT:
            return None

        # CALL: sustained above OR + EMA + VWAP + MACD all aligned
        if (
            above_count >= OR_CONFIRM_BARS
            and ema_fast > ema_slow
            and current.close > current_vwap
            and macd_hist_val > ZERO
        ):
            self._or_signal_fired = True
            return Signal(
                direction=Direction.CALL,
                bar_end=current.end,
                spot=spot or current.close,
                strategy="adaptive_or_breakout",
                market_state=MarketState.TREND_UP,
                indicators={
                    "profile": "adaptive_or",
                    "or_high": str(self._or_high),
                    "or_low": str(self._or_low),
                    "or_range": str(or_range),
                    "ema_fast": str(ema_fast),
                    "ema_slow": str(ema_slow),
                    "macd_hist": str(macd_hist_val),
                    "vwap": str(current_vwap),
                    "confirm_bars": str(above_count),
                    "vix_5m_trend": self.vix_trend.value,
                },
            )

        # PUT: sustained below OR + EMA + VWAP + MACD all aligned
        if (
            below_count >= OR_CONFIRM_BARS
            and ema_fast < ema_slow
            and current.close < current_vwap
            and macd_hist_val < ZERO
        ):
            self._or_signal_fired = True
            return Signal(
                direction=Direction.PUT,
                bar_end=current.end,
                spot=spot or current.close,
                strategy="adaptive_or_breakout",
                market_state=MarketState.TREND_DOWN,
                indicators={
                    "profile": "adaptive_or",
                    "or_high": str(self._or_high),
                    "or_low": str(self._or_low),
                    "or_range": str(or_range),
                    "ema_fast": str(ema_fast),
                    "ema_slow": str(ema_slow),
                    "macd_hist": str(macd_hist_val),
                    "vwap": str(current_vwap),
                    "confirm_bars": str(below_count),
                    "vix_5m_trend": self.vix_trend.value,
                },
            )

        return None

    # ════════════════════════════════════════════════════════════════════
    # Phase 3: Day Classification (at 10:01)
    # ════════════════════════════════════════════════════════════════════

    def _classify_day(self) -> str:
        """Classify day as 'trend' or 'oscillation'.

        Trend evidence (all required):
          - Trend engine confirmed a breakout direction
          - Price still extended beyond OR by > OR_MIN_EXTENSION_PCT
          - Not marked as choppy (excessive VWAP crosses)
        """
        direction = self.trend._breakout_confirmed_direction
        if direction is None:
            return "oscillation"

        ctx = self.trend.last_context
        if ctx is None:
            return "oscillation"

        or_high = self.trend._or_high
        or_low = self.trend._or_low
        if or_high is None or or_low is None:
            return "oscillation"

        or_range = or_high - or_low
        if or_range <= ZERO:
            return "oscillation"

        min_ext = or_range * OR_MIN_EXTENSION_PCT
        if direction is Direction.CALL:
            sustained = ctx.current_close > or_high + min_ext
        else:
            sustained = ctx.current_close < or_low - min_ext

        if not sustained:
            return "oscillation"

        if self.trend._choppy_day:
            return "oscillation"

        return "trend"

    # ════════════════════════════════════════════════════════════════════
    # Main evaluate — signal dispatch
    # ════════════════════════════════════════════════════════════════════

    def evaluate(
        self, bars_1m: Sequence[Bar], spot: Decimal | None = None
    ) -> Signal | None:
        # Always run both engines to keep indicators warm
        trend_signal = self.trend.evaluate(bars_1m, spot)
        boll_signal = self.boll_macd.evaluate(bars_1m, spot)

        visible = [b for b in bars_1m if b.complete]
        if not visible:
            return None
        current = max(visible, key=lambda b: b.end)
        trading_day = current.end.astimezone(NY_TZ).date()
        if self._current_day != trading_day:
            self._reset_day(trading_day)

        current_time = current.end.astimezone(NY_TZ).time().replace(tzinfo=None)

        today = sorted(
            [b for b in visible if b.start.astimezone(NY_TZ).date() == trading_day],
            key=lambda b: b.start,
        )
        self._today_bars = today

        # ── Phase 1: Accumulation (9:30-9:39) — no signals ──
        if current_time < RULES.phase_collect_end:
            self._sync_state(
                self.boll_macd if self.boll_macd.last_context else self.trend
            )
            return None

        # ── No new signals after main window ends ──
        if current_time >= RULES.phase_main_end:
            self._sync_state(
                self.boll_macd if self.boll_macd.last_context else self.trend
            )
            return None

        # ── Phase 3: Mode already decided ──
        if self._day_mode == "trend":
            self._sync_state(self.trend)
            if trend_signal is not None:
                self.last_signal_bar = trend_signal.bar_end
                return trend_signal
            return None

        if self._day_mode == "oscillation":
            self._sync_state(self.boll_macd)
            if boll_signal is not None:
                self.last_signal_bar = boll_signal.bar_end
            return boll_signal

        # ── Phase 2: Dual-Signal Entry (9:40-10:00) ──
        if current_time < RULES.phase_opening_end:
            if not self._or_built:
                self._build_or(today)

            self._sync_state(
                self.boll_macd if self.boll_macd.last_context else self.trend
            )

            # Source A: BOLL/MACD signals (full 9:40-10:00 window)
            # Fill the gap between opening_last_signal and main_start
            effective_boll = boll_signal
            if (
                effective_boll is None
                and RULES.timed_opening_last_signal
                <= current_time
                < RULES.phase_opening_end
                and self.boll_macd.last_context is not None
            ):
                effective_boll = self.boll_macd._opening_signal(
                    self.boll_macd.last_context, spot
                )
                if (
                    effective_boll is not None
                    and effective_boll.bar_end == self.boll_macd.last_signal_bar
                ):
                    effective_boll = None

            if effective_boll is not None:
                # VIX filter: rising > 3% intraday blocks CALL entries
                if (
                    effective_boll.direction is Direction.CALL
                    and self._vix_intraday_change >= VIX_INTRADAY_RISE_THRESHOLD
                ):
                    effective_boll = None

            if effective_boll is not None:
                self.last_signal_bar = effective_boll.bar_end
                self.boll_macd.last_signal_bar = effective_boll.bar_end
                return effective_boll

            # Source B: Custom OR breakout (EMA + VWAP + MACD confirmation)
            if self._or_built:
                or_signal = self._or_breakout_signal(today, current, spot)
                if or_signal is not None:
                    # VIX filter also applies to OR breakout CALL
                    if (
                        or_signal.direction is Direction.CALL
                        and self._vix_intraday_change >= VIX_INTRADAY_RISE_THRESHOLD
                    ):
                        or_signal = None
                if or_signal is not None:
                    self.last_signal_bar = or_signal.bar_end
                    return or_signal

            return None

        # ── Mode Decision (10:01 — final classification) ──
        if self._day_mode is None:
            mode = self._classify_day()
            self._day_mode = mode
        mode = self._day_mode

        if mode == "trend":
            self._sync_state(self.trend)
            if trend_signal is not None:
                self.last_signal_bar = trend_signal.bar_end
                return trend_signal
            # Generate deferred entry if breakout is live but no fresh signal
            signal = self._make_deferred_trend_signal()
            if signal is not None:
                self.last_signal_bar = signal.bar_end
                return signal
            return None
        else:
            self._sync_state(self.boll_macd)
            if boll_signal is not None:
                self.last_signal_bar = boll_signal.bar_end
            return boll_signal

    def _make_deferred_trend_signal(self) -> Signal | None:
        """Generate a Trend entry when mode is decided as 'trend' at 10:01."""
        direction = self.trend._breakout_confirmed_direction
        if direction is None:
            return None
        ctx = self.trend.last_context
        if ctx is None:
            return None
        state = (
            MarketState.TREND_UP
            if direction is Direction.CALL
            else MarketState.TREND_DOWN
        )
        return Signal(
            direction=direction,
            bar_end=ctx.bar_end,
            spot=ctx.current_close,
            strategy="trend_orb_breakout",
            market_state=state,
            indicators={
                "profile": "trend_following",
                "indicator_timeframe": "1m",
                "or_high": str(self.trend._or_high),
                "or_low": str(self.trend._or_low),
                "ema_fast": str(self.trend._today_ema_fast),
                "ema_slow": str(self.trend._today_ema_slow),
                "vix_5m_trend": self.vix_trend.value,
                "adaptive_deferred_entry": "true",
            },
        )

    # ════════════════════════════════════════════════════════════════════
    # Exit logic — adaptive based on day mode
    # ════════════════════════════════════════════════════════════════════

    def bar_exit_decision(self, position: Position) -> ExitDecision | None:
        # Before mode is decided: unified exit for ALL positions
        if self._day_mode is None:
            return self._unified_exit(position)

        # After mode decided: route to day-appropriate exit
        if self._day_mode == "trend":
            # 趋势日 → 拿住，用 Trend EMA trailing exit
            return self.trend.bar_exit_decision(position)

        # 震荡日 → MACD 反转快出
        return self._oscillation_exit(position)

    def _unified_exit(self, position: Position) -> ExitDecision | None:
        """Unified exit for ALL positions before mode decision (9:40-10:00).

        Dual check:
          1. MACD reversal → quick profit capture on direction change
          2. EMA slow cross → trend invalidation
        """
        # Check 1: MACD reversal
        reversal = self._check_macd_reversal(position)
        if reversal is not None:
            return reversal

        # Check 2: EMA trailing
        if not self._today_bars:
            return None
        current = self._today_bars[-1]
        closes = [b.close for b in self._today_bars]
        if len(closes) < RULES.trend_ema_slow:
            return None

        ema_slow = ema_series(closes, RULES.trend_ema_slow)[-1]

        if position.direction is Direction.CALL and current.close < ema_slow:
            return ExitDecision(ExitReason.TREND_EMA_EXIT, position.quantity)
        if position.direction is Direction.PUT and current.close > ema_slow:
            return ExitDecision(ExitReason.TREND_EMA_EXIT, position.quantity)
        return None

    def _oscillation_exit(self, position: Position) -> ExitDecision | None:
        """Exit on oscillation days: MACD reversal, no forced time cutoff.

        Direction correct → hold until reversal signal.
        Direction wrong → risk engine stop-loss handles it.
        """
        ctx = self.boll_macd.last_context
        if ctx is None:
            return None

        # For OR breakout positions on oscillation days: exit if retreated inside OR
        strategy = position.strategy_name or ""
        if strategy in ("adaptive_or_breakout", "trend_orb_breakout"):
            or_high = self._or_high or self.trend._or_high
            or_low = self._or_low or self.trend._or_low
            if or_high is not None and or_low is not None:
                current = self._today_bars[-1] if self._today_bars else None
                if current is not None:
                    if position.direction is Direction.CALL and current.close < or_high:
                        return ExitDecision(ExitReason.OPENING_CUTOFF, position.quantity)
                    if position.direction is Direction.PUT and current.close > or_low:
                        return ExitDecision(ExitReason.OPENING_CUTOFF, position.quantity)

        # MACD reversal detection (shared logic)
        return self._check_macd_reversal(position)

    def _check_macd_reversal(self, position: Position) -> ExitDecision | None:
        """MACD reversal detection (shared by unified and oscillation exits).

        Detects MACD histogram zero-cross with volume confirmation.
        """
        ctx = self.boll_macd.last_context
        if ctx is None:
            return None
        _VOL_STRONG = Decimal("1.20")
        _VOL_BASE = Decimal("1.00")

        if position.direction is Direction.CALL:
            macd_reversed = ctx.macd_hist_prev > ZERO and ctx.macd_hist <= ZERO
            macd_on_reversal_side = ctx.macd_hist <= ZERO
            reversal_candle = ctx.current_close < ctx.current_open
        else:
            macd_reversed = ctx.macd_hist_prev < ZERO and ctx.macd_hist >= ZERO
            macd_on_reversal_side = ctx.macd_hist >= ZERO
            reversal_candle = ctx.current_close > ctx.current_open

        volume_confirmed = (
            ctx.rvol_val >= _VOL_STRONG
            or (ctx.rvol_val >= _VOL_BASE and ctx.rvol_val > ctx.rvol_prev)
        )

        if macd_reversed:
            if reversal_candle and volume_confirmed:
                position.macd_reversal_pending = False
                position.macd_reversal_pending_at = None
                return ExitDecision(ExitReason.DIRECTION_REVERSAL, position.quantity)
            position.macd_reversal_pending = True
            position.macd_reversal_pending_at = ctx.bar_end
        elif position.macd_reversal_pending:
            if not macd_on_reversal_side:
                position.macd_reversal_pending = False
                position.macd_reversal_pending_at = None
            elif (
                ctx.bar_end != position.macd_reversal_pending_at
                and volume_confirmed
            ):
                position.macd_reversal_pending = False
                position.macd_reversal_pending_at = None
                return ExitDecision(ExitReason.DIRECTION_REVERSAL, position.quantity)
        return None

    @property
    def day_mode(self) -> str | None:
        return self._day_mode



# Legacy alias
AdaptiveEngine = HybridEngine
