"""Tests for the Opening Range Breakout trend-following strategy."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from conftest import make_settings
from qqq_trader.domain import Bar, Direction, ExitReason, Position
from qqq_trader.trend_strategy import TrendFollowingEngine


def _bar(minute_offset: int, open_: float, high: float, low: float,
         close: float, volume: int = 1000,
         base_time: datetime | None = None) -> Bar:
    """Create a 1-min QQQ bar at 9:30 ET + minute_offset."""
    base = base_time or datetime(2026, 8, 11, 13, 30, tzinfo=timezone.utc)
    return Bar(
        symbol="QQQ.US",
        start=base + timedelta(minutes=minute_offset),
        end=base + timedelta(minutes=minute_offset + 1),
        open=Decimal(str(open_)),
        high=Decimal(str(high)),
        low=Decimal(str(low)),
        close=Decimal(str(close)),
        volume=volume,
    )


def _trending_up_bars(count: int = 40) -> list[Bar]:
    """Generate bars for a bullish trending day.

    9:30-9:40: OR builds in 490-491 range (steady, OR uses bars from 9:35)
    9:40+: price breaks above OR high and trends up consistently
    """
    bars: list[Bar] = []
    for i in range(min(count, 10)):
        base = 490.0 + i * 0.1
        bars.append(_bar(i, base, base + 0.3, base - 0.2, base + 0.1))

    for i in range(10, min(count, 60)):
        base = 491.5 + (i - 10) * 0.15
        bars.append(_bar(i, base, base + 0.4, base - 0.1, base + 0.25,
                         volume=2500))
    return bars


def _choppy_bars(count: int = 30) -> list[Bar]:
    """Generate bars that oscillate around VWAP to trigger choppy-day filter."""
    bars: list[Bar] = []
    for i in range(count):
        if i % 2 == 0:
            base = 490.0 + 0.5
        else:
            base = 490.0 - 0.5
        bars.append(_bar(i, base, base + 0.3, base - 0.3, base + 0.05))
    return bars


def _trending_down_bars(count: int = 40) -> list[Bar]:
    """Generate bars for a bearish trending day."""
    bars: list[Bar] = []
    for i in range(min(count, 10)):
        base = 500.0 - i * 0.1
        bars.append(_bar(i, base, base + 0.2, base - 0.3, base - 0.1))

    for i in range(10, min(count, 60)):
        base = 498.5 - (i - 10) * 0.15
        bars.append(_bar(i, base, base + 0.1, base - 0.4, base - 0.25,
                         volume=2500))
    return bars


class TestTrendFollowingEngine:

    def test_no_signal_during_opening_range(self):
        settings = make_settings()
        engine = TrendFollowingEngine(settings)
        bars = _trending_up_bars(10)  # offsets 0-9, last bar ends at 9:40 ET
        signal = engine.evaluate(bars)
        assert signal is None
        assert engine.last_state.value == "observation"

    def test_no_signal_on_choppy_day(self):
        settings = make_settings()
        engine = TrendFollowingEngine(settings)
        bars = _choppy_bars(25)
        for i in range(len(bars)):
            signal = engine.evaluate(bars[: i + 1])
        assert signal is None
        assert engine._choppy_day is True

    def test_call_signal_on_upward_breakout(self):
        settings = make_settings()
        engine = TrendFollowingEngine(settings)
        bars = _trending_up_bars(35)
        signal = None
        for i in range(len(bars)):
            result = engine.evaluate(bars[: i + 1])
            if result is not None:
                signal = result
                break
        assert signal is not None
        assert signal.direction is Direction.CALL
        assert signal.strategy == "trend_orb_breakout"
        assert "or_high" in signal.indicators
        assert "ema_fast" in signal.indicators

    def test_put_signal_on_downward_breakout(self):
        settings = make_settings()
        engine = TrendFollowingEngine(settings)
        bars = _trending_down_bars(35)
        signal = None
        for i in range(len(bars)):
            result = engine.evaluate(bars[: i + 1])
            if result is not None:
                signal = result
                break
        assert signal is not None
        assert signal.direction is Direction.PUT
        assert signal.strategy == "trend_orb_breakout"

    def test_dedup_one_signal_per_bar(self):
        settings = make_settings()
        engine = TrendFollowingEngine(settings)
        bars = _trending_up_bars(35)
        signals = []
        for i in range(len(bars)):
            result = engine.evaluate(bars[: i + 1])
            if result is not None:
                signals.append(result)
        bar_ends = [s.bar_end for s in signals]
        assert len(bar_ends) == len(set(bar_ends))

    def test_ema_exit_triggers_on_cross(self):
        settings = make_settings()
        engine = TrendFollowingEngine(settings)
        bars = _trending_up_bars(30)

        for i in range(len(bars)):
            engine.evaluate(bars[: i + 1])

        reversal_base = bars[-1].close
        for j in range(5):
            drop = reversal_base - Decimal(str(j * 0.8))
            bars.append(_bar(
                30 + j, float(drop), float(drop + Decimal("0.1")),
                float(drop - Decimal("0.5")), float(drop - Decimal("0.3")),
                volume=2000,
            ))
            engine.evaluate(bars)

        position = Position(
            symbol="QQQ260811C492000.US",
            direction=Direction.CALL,
            quantity=10,
            entry_price=Decimal("2.50"),
            opened_at=bars[22].end,
            strategy_name="trend_orb_breakout",
        )
        decision = engine.bar_exit_decision(position)
        assert decision is not None
        assert decision.reason in {ExitReason.TREND_EMA_EXIT, ExitReason.STATE_INVALIDATION}

    def test_or_reentry_triggers_state_invalidation(self):
        settings = make_settings()
        engine = TrendFollowingEngine(settings)

        bars = _trending_up_bars(25)
        for i in range(len(bars)):
            engine.evaluate(bars[: i + 1])

        or_mid = (engine._or_high + engine._or_low) / 2
        crash_close = float(or_mid - Decimal("1"))
        bars.append(_bar(
            25, crash_close + 0.3, crash_close + 0.5, crash_close - 0.1,
            crash_close, volume=3000,
        ))
        engine.evaluate(bars)

        position = Position(
            symbol="QQQ260811C492000.US",
            direction=Direction.CALL,
            quantity=10,
            entry_price=Decimal("2.50"),
            opened_at=bars[22].end,
            strategy_name="trend_orb_breakout",
        )
        decision = engine.bar_exit_decision(position)
        assert decision is not None
        assert decision.reason is ExitReason.STATE_INVALIDATION

    def test_no_exit_when_trend_intact(self):
        settings = make_settings()
        engine = TrendFollowingEngine(settings)
        bars = _trending_up_bars(30)
        for i in range(len(bars)):
            engine.evaluate(bars[: i + 1])

        position = Position(
            symbol="QQQ260811C492000.US",
            direction=Direction.CALL,
            quantity=10,
            entry_price=Decimal("2.50"),
            opened_at=bars[22].end,
            strategy_name="trend_orb_breakout",
        )
        decision = engine.bar_exit_decision(position)
        assert decision is None

    def test_strategy_from_settings_returns_trend_engine(self):
        settings = make_settings(strategy_mode="trend")
        from qqq_trader.strategy import strategy_from_settings
        engine = strategy_from_settings(settings)
        assert isinstance(engine, TrendFollowingEngine)

    def test_strategy_from_settings_returns_boll_macd_by_default(self):
        settings = make_settings()
        from qqq_trader.strategy import strategy_from_settings, StrategyEngine
        engine = strategy_from_settings(settings)
        assert isinstance(engine, StrategyEngine)

    def test_no_signal_after_entry_end(self):
        settings = make_settings()
        engine = TrendFollowingEngine(settings)
        base = datetime(2026, 8, 11, 13, 30, tzinfo=timezone.utc)
        bars: list[Bar] = []
        for i in range(130):
            if i < 20:
                p = 490.0 + i * 0.1
            else:
                p = 492.0 + (i - 20) * 0.15
            bars.append(_bar(i, p, p + 0.4, p - 0.1, p + 0.2,
                             volume=2500, base_time=base))

        signal = None
        for i in range(len(bars)):
            result = engine.evaluate(bars[: i + 1])
            last_bar = bars[i]
            local = last_bar.end.astimezone(
                __import__("zoneinfo").ZoneInfo("America/New_York")
            ).time()
            if result is not None and local >= __import__("datetime").time(11, 30):
                signal = result
        assert signal is None

    def test_midday_reduce_skipped_for_trend_strategy(self):
        """RiskEngine should skip midday reduce for trend_ prefixed strategies."""
        from qqq_trader.risk import RiskEngine

        settings = make_settings()
        risk = RiskEngine(settings)
        position = Position(
            symbol="QQQ260811C492000.US",
            direction=Direction.CALL,
            quantity=10,
            entry_price=Decimal("2.50"),
            opened_at=datetime(2026, 8, 11, 13, 50, tzinfo=timezone.utc),
            strategy_name="trend_orb_breakout",
        )
        now = datetime(2026, 8, 11, 15, 35, tzinfo=timezone.utc)
        decision = risk.exit_decision(position, Decimal("2.60"), now)
        assert decision is None or decision.reason is not ExitReason.MIDDAY_REDUCE
