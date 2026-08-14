"""Hybrid strategy: Trend ORB on trending days, BOLL/MACD on oscillation days.

Day classification flow (OR builds in background, never blocks signals):
  1. Before OR end (9:40): BOLL/MACD opening signals pass through normally.
     OR range is built in the background by the Trend engine.
  2. After OR end, before fallback (9:40-10:00): Wait for ORB breakout.
     - Breakout confirmed → commit to Trend mode for the rest of the day.
     - Choppy detected (VWAP crosses) → switch to BOLL/MACD mode.
     - During this period, BOLL/MACD signals still pass through.
  3. At fallback time (10:00): No breakout → commit to BOLL/MACD mode.

Exit logic follows whichever strategy produced the position's signal.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal

from .config import NY_TZ
from .domain import Bar, Direction, ExitDecision, MarketState, Position, Signal
from .policy import RULES
from .strategy import StrategyEngine
from .trend_strategy import TrendFollowingEngine
from .volatility import VixFiveMinuteTrend


class HybridEngine:
    """Dispatches between TrendFollowingEngine and StrategyEngine per day.

    Before the day mode is decided, BOLL/MACD signals pass through so
    opening-window profits are not lost.  Once a trend breakout is confirmed,
    the engine switches to Trend mode and only emits Trend signals.
    """

    def __init__(self, settings) -> None:
        self.settings = settings
        self.trend = TrendFollowingEngine(settings)
        self.boll_macd = StrategyEngine(settings)

        self._current_day: date | None = None
        self._day_mode: str | None = None  # None | 'trend' | 'boll_macd'

        self.last_signal_bar: datetime | None = None
        self.last_context = None
        self.last_state = MarketState.UNKNOWN
        self.vix_trend = VixFiveMinuteTrend.NEUTRAL

    def _reset_day(self, trading_day: date) -> None:
        self._current_day = trading_day
        self._day_mode = None

    def set_volatility_context(
        self, volatility_bars: Sequence[Bar], decision_at: datetime
    ) -> None:
        self.trend.set_volatility_context(volatility_bars, decision_at)
        self.boll_macd.set_volatility_context(volatility_bars, decision_at)
        self.vix_trend = self.trend.vix_trend

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

    def evaluate(
        self, bars_1m: Sequence[Bar], spot: Decimal | None = None
    ) -> Signal | None:
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

        if self._day_mode == "trend":
            self._sync_state(self.trend)
            if trend_signal is not None:
                self.last_signal_bar = trend_signal.bar_end
            return trend_signal

        if self._day_mode == "boll_macd":
            self._sync_state(self.boll_macd)
            if boll_signal is not None:
                self.last_signal_bar = boll_signal.bar_end
            return boll_signal

        if trend_signal is not None:
            self._day_mode = "trend"
            self._sync_state(self.trend)
            self.last_signal_bar = trend_signal.bar_end
            return trend_signal

        if self.trend._choppy_day or current_time >= RULES.hybrid_fallback_time:
            self._day_mode = "boll_macd"
            self._sync_state(self.boll_macd)
            if boll_signal is not None:
                self.last_signal_bar = boll_signal.bar_end
            return boll_signal

        if boll_signal is not None:
            self._sync_state(self.boll_macd)
            self.last_signal_bar = boll_signal.bar_end
            return boll_signal

        self._sync_state(self.boll_macd if self.boll_macd.last_context else self.trend)
        return None

    def bar_exit_decision(self, position: Position) -> ExitDecision | None:
        if (position.strategy_name or "").startswith("trend_"):
            return self.trend.bar_exit_decision(position)
        return self.boll_macd.bar_exit_decision(position)

    @property
    def day_mode(self) -> str | None:
        return self._day_mode
