"""Opening Range Breakout (ORB) trend-following strategy for 0DTE QQQ options.

Designed for trending days: enters on confirmed ORB with EMA/volume alignment,
rides the trend with EMA-based trailing exit, and stays flat on choppy days
detected by excessive VWAP crosses.
"""

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
from .indicators import MarketContext, ema_series, vwap, vwap_series
from .policy import RULES
from .volatility import VixFiveMinuteTrend, vix_five_minute_trend

ZERO = Decimal(0)


class TrendFollowingEngine:
    """ORB trend strategy: build opening range, wait for breakout, ride trend.

    State machine per trading day:
      OBSERVATION  ->  (OR built)  ->  WAITING_BREAKOUT  ->  TREND_UP / TREND_DOWN
                                   ->  RANGE (choppy day, no trades)

    Key improvements over naive ORB:
      - Sustained breakout: price must stay above/below OR for N consecutive bars
      - VWAP distance: at breakout, close must be meaningfully above VWAP (not hugging)
      - Rolling chop detection: accumulating VWAP crosses can mark choppy mid-session
      - Max 1 signal direction per day to avoid re-entering after false breakout
    """

    def __init__(self, settings) -> None:
        self.settings = settings
        self.last_signal_bar: datetime | None = None
        self.last_context: MarketContext | None = None
        self.last_state = MarketState.UNKNOWN
        self.vix_trend = VixFiveMinuteTrend.NEUTRAL

        self._current_day: date | None = None
        self._or_high: Decimal | None = None
        self._or_low: Decimal | None = None
        self._or_vwap: Decimal | None = None
        self._or_built = False
        self._choppy_day = False
        self._today_1m: list[Bar] = []
        self._today_ema_slow: Decimal = ZERO
        self._today_ema_fast: Decimal = ZERO
        self._breakout_confirmed_direction: Direction | None = None
        self._consecutive_above_or: int = 0
        self._consecutive_below_or: int = 0

    def _reset_day(self, trading_day: date) -> None:
        self._current_day = trading_day
        self._or_high = None
        self._or_low = None
        self._or_vwap = None
        self._or_built = False
        self._choppy_day = False
        self._today_1m = []
        self._today_ema_slow = ZERO
        self._today_ema_fast = ZERO
        self._breakout_confirmed_direction = None
        self._consecutive_above_or = 0
        self._consecutive_below_or = 0

    @staticmethod
    def _rth(bar: Bar) -> bool:
        local_time = bar.start.astimezone(NY_TZ).time().replace(tzinfo=None)
        return time(9, 30) <= local_time < time(16, 0)

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

    def record_entry(self, direction: Direction, entered_at: datetime) -> None:
        pass

    def record_profitable_exit(
        self, direction: Direction, exited_at: datetime
    ) -> None:
        pass

    def _or_bars(self, today: list[Bar]) -> list[Bar]:
        """OR bars: from trend_or_start to trend_or_end, skipping early open bars."""
        return [
            b
            for b in today
            if b.start.astimezone(NY_TZ).time().replace(tzinfo=None)
            >= RULES.trend_or_start
            and b.end.astimezone(NY_TZ).time().replace(tzinfo=None)
            <= RULES.trend_or_end
        ]

    def _build_opening_range(self, today: list[Bar]) -> None:
        or_bars = self._or_bars(today)
        if not or_bars:
            return
        self._or_high = max(b.high for b in or_bars)
        self._or_low = min(b.low for b in or_bars)
        self._or_vwap = vwap(or_bars)
        self._or_built = True

    def _count_vwap_crosses(self, bars: list[Bar]) -> int:
        if len(bars) < 2:
            return 0
        vwap_vals = vwap_series(bars)
        crosses = 0
        prev_side: bool | None = None
        for bar, v in zip(bars, vwap_vals, strict=True):
            side = bar.close >= v
            if prev_side is not None and side != prev_side:
                crosses += 1
            prev_side = side
        return crosses

    def _relative_volume(self, today: list[Bar]) -> Decimal:
        if len(today) < 2:
            return ZERO
        current = today[-1]
        prev_bars = today[:-1]
        avg = Decimal(sum(b.volume for b in prev_bars)) / Decimal(len(prev_bars))
        return Decimal(current.volume) / avg if avg > ZERO else ZERO

    def _build_context(
        self,
        today: list[Bar],
        current: Bar,
        current_time: time,
    ) -> MarketContext:
        closes = [b.close for b in today]
        ema_fast = ZERO
        ema_slow = ZERO
        if len(closes) >= RULES.trend_ema_fast:
            ema_fast = ema_series(closes, RULES.trend_ema_fast)[-1]
        if len(closes) >= RULES.trend_ema_slow:
            ema_slow = ema_series(closes, RULES.trend_ema_slow)[-1]
        self._today_ema_fast = ema_fast
        self._today_ema_slow = ema_slow
        vwap_val = vwap(today) if today else ZERO
        return MarketContext(
            ema9=ema_fast,
            ema20=ema_slow,
            vwap_value=vwap_val,
            current_open=current.open,
            current_high=current.high,
            current_low=current.low,
            current_close=current.close,
            current_volume=current.volume,
            prev_close=today[-2].close if len(today) >= 2 else current.close,
            day_high=max(b.high for b in today),
            day_low=min(b.low for b in today),
            bar_time=current_time,
            bar_end=current.end,
            rvol_val=self._relative_volume(today),
        )

    def _update_breakout_counters(self, today: list[Bar]) -> None:
        """Track consecutive bars above/below OR for sustained breakout check."""
        if self._or_high is None or self._or_low is None:
            return
        current = today[-1]
        if current.close > self._or_high:
            self._consecutive_above_or += 1
            self._consecutive_below_or = 0
        elif current.close < self._or_low:
            self._consecutive_below_or += 1
            self._consecutive_above_or = 0
        else:
            self._consecutive_above_or = 0
            self._consecutive_below_or = 0

    def _breakout_signal(
        self,
        ctx: MarketContext,
        today: list[Bar],
        spot: Decimal | None,
    ) -> Signal | None:
        closes = [b.close for b in today]
        if len(closes) < RULES.trend_ema_fast:
            return None
        if self._or_high is None or self._or_low is None:
            return None

        ema_fast = self._today_ema_fast
        ema_slow = self._today_ema_slow
        or_range = self._or_high - self._or_low

        sustained = RULES.trend_breakout_confirm_bars

        if ema_slow > ZERO:
            ema_bullish = ema_fast > ema_slow
            ema_bearish = ema_fast < ema_slow
        else:
            ema_bullish = ema_fast > ZERO and ctx.current_close > ema_fast
            ema_bearish = ema_fast > ZERO and ctx.current_close < ema_fast

        if (
            self._consecutive_above_or >= sustained
            and ema_bullish
            and ctx.current_close > ctx.vwap_value
            and self._breakout_confirmed_direction is None
        ):
            self._breakout_confirmed_direction = Direction.CALL
            return self._make_signal(
                ctx, Direction.CALL, spot, ema_fast, ema_slow,
                ctx.rvol_val, or_range,
            )

        if (
            self._consecutive_below_or >= sustained
            and ema_bearish
            and ctx.current_close < ctx.vwap_value
            and self._breakout_confirmed_direction is None
        ):
            self._breakout_confirmed_direction = Direction.PUT
            return self._make_signal(
                ctx, Direction.PUT, spot, ema_fast, ema_slow,
                ctx.rvol_val, or_range,
            )

        return None

    def _make_signal(
        self,
        ctx: MarketContext,
        direction: Direction,
        spot: Decimal | None,
        ema_fast: Decimal,
        ema_slow: Decimal,
        volume_ratio: Decimal,
        or_range: Decimal,
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
            strategy="trend_orb_breakout",
            market_state=state,
            indicators={
                "profile": "trend_following",
                "indicator_timeframe": "1m",
                "or_high": str(self._or_high),
                "or_low": str(self._or_low),
                "or_range": str(or_range),
                "or_vwap": str(self._or_vwap),
                "ema_fast": str(ema_fast),
                "ema_fast_period": str(RULES.trend_ema_fast),
                "ema_slow": str(ema_slow),
                "ema_slow_period": str(RULES.trend_ema_slow),
                "volume_ratio": str(volume_ratio),
                "vwap": str(ctx.vwap_value),
                "consecutive_breakout_bars": str(
                    self._consecutive_above_or
                    if direction is Direction.CALL
                    else self._consecutive_below_or
                ),
                "vix_5m_trend": self.vix_trend.value,
            },
        )

    def evaluate(
        self, bars_1m: Sequence[Bar], spot: Decimal | None = None
    ) -> Signal | None:
        visible = sorted(
            (bar for bar in bars_1m if bar.complete and self._rth(bar)),
            key=lambda item: item.end,
        )
        if not visible:
            return None

        current = visible[-1]
        trading_day = current.end.astimezone(NY_TZ).date()
        if self._current_day != trading_day:
            self._reset_day(trading_day)

        today = [
            bar
            for bar in visible
            if bar.start.astimezone(NY_TZ).date() == trading_day
        ]
        self._today_1m = today
        current_time = current.end.astimezone(NY_TZ).time().replace(tzinfo=None)

        ctx = self._build_context(today, current, current_time)
        self.last_context = ctx

        if current_time <= RULES.trend_or_end:
            self._build_opening_range(today)
            ctx.market_state = MarketState.OBSERVATION
            self.last_state = ctx.market_state
            return None

        if not self._or_built:
            self._build_opening_range(today)

        self._update_breakout_counters(today)

        if self._choppy_day:
            ctx.market_state = MarketState.RANGE
            self.last_state = ctx.market_state
            return None

        vwap_crosses = self._count_vwap_crosses(today)
        if vwap_crosses > RULES.trend_max_vwap_crosses:
            self._choppy_day = True
            ctx.market_state = MarketState.RANGE
            self.last_state = ctx.market_state
            return None

        if current_time >= RULES.trend_entry_end:
            ctx.market_state = MarketState.UNKNOWN
            self.last_state = ctx.market_state
            return None

        signal = self._breakout_signal(ctx, today, spot)

        if signal is not None and signal.bar_end == self.last_signal_bar:
            signal = None
        if signal is not None:
            self.last_signal_bar = signal.bar_end
            ctx.market_state = signal.market_state
        self.last_state = ctx.market_state
        return signal

    def bar_exit_decision(self, position: Position) -> ExitDecision | None:
        ctx = self.last_context
        if ctx is None or not self._today_1m:
            return None

        closes = [b.close for b in self._today_1m]
        if len(closes) < RULES.trend_ema_slow:
            return None

        ema_slow_values = ema_series(closes, RULES.trend_ema_slow)

        required_bars = RULES.trend_ema_exit_bars
        if len(ema_slow_values) < required_bars:
            return None
        recent_bars = self._today_1m[-required_bars:]
        recent_ema_slow = ema_slow_values[-required_bars:]

        if position.direction is Direction.CALL:
            ema_crossed = all(
                bar.close < ema_val
                for bar, ema_val in zip(
                    recent_bars, recent_ema_slow, strict=True
                )
            )
        else:
            ema_crossed = all(
                bar.close > ema_val
                for bar, ema_val in zip(
                    recent_bars, recent_ema_slow, strict=True
                )
            )

        if ema_crossed:
            return ExitDecision(ExitReason.TREND_EMA_EXIT, position.quantity)

        if self._or_high is not None and self._or_low is not None:
            or_mid = (self._or_high + self._or_low) / Decimal(2)
            if position.direction is Direction.CALL and ctx.current_close < or_mid:
                return ExitDecision(
                    ExitReason.STATE_INVALIDATION, position.quantity
                )
            if position.direction is Direction.PUT and ctx.current_close > or_mid:
                return ExitDecision(
                    ExitReason.STATE_INVALIDATION, position.quantity
                )

        return None
