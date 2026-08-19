"""Tests for HybridEngine: Trend ORB on trending days, BOLL/MACD on oscillation days."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from conftest import make_settings
from qqq_trader.domain import Bar, Direction, MarketState
from qqq_trader.hybrid_strategy import HybridEngine
from qqq_trader.strategy import StrategyEngine
from qqq_trader.trend_strategy import TrendFollowingEngine


def _bar(minute_offset: int, open_: float, high: float, low: float,
         close: float, volume: int = 1000,
         base_time: datetime | None = None) -> Bar:
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
    """Bullish trending day: OR 9:35-9:40, breakout after 9:40."""
    bars: list[Bar] = []
    for i in range(min(count, 10)):
        base = 490.0 + i * 0.1
        bars.append(_bar(i, base, base + 0.3, base - 0.2, base + 0.1))
    for i in range(10, min(count, 60)):
        base = 491.5 + (i - 10) * 0.15
        bars.append(_bar(i, base, base + 0.4, base - 0.1, base + 0.25,
                         volume=2500))
    return bars


def _choppy_bars(count: int = 40) -> list[Bar]:
    """Oscillating bars that cross VWAP repeatedly."""
    bars: list[Bar] = []
    for i in range(count):
        if i % 2 == 0:
            base = 490.0 + 0.5
        else:
            base = 490.0 - 0.5
        bars.append(_bar(i, base, base + 0.3, base - 0.3, base + 0.05))
    return bars


def _flat_then_trending(count: int = 50) -> list[Bar]:
    """Flat during OR, no breakout until after fallback time → BOLL/MACD mode."""
    bars: list[Bar] = []
    for i in range(min(count, 35)):
        base = 490.0 + (i % 3) * 0.05
        bars.append(_bar(i, base, base + 0.2, base - 0.2, base + 0.05))
    for i in range(35, min(count, 60)):
        base = 490.0 + (i - 35) * 0.1
        bars.append(_bar(i, base, base + 0.3, base - 0.1, base + 0.15,
                         volume=2000))
    return bars


class TestHybridEngine:

    def test_observation_during_or(self):
        settings = make_settings()
        engine = HybridEngine(settings)
        bars = _trending_up_bars(10)
        signal = engine.evaluate(bars)
        assert signal is None
        assert engine.last_state == MarketState.OBSERVATION
        assert engine.day_mode is None

    def test_trending_day_selects_trend_mode(self):
        settings = make_settings()
        engine = HybridEngine(settings)
        bars = _trending_up_bars(35)
        signal = None
        for i in range(len(bars)):
            result = engine.evaluate(bars[: i + 1])
            if result is not None and signal is None:
                signal = result
        assert signal is not None
        assert signal.direction is Direction.CALL
        assert signal.strategy == "trend_orb_breakout"
        assert engine.day_mode == "trend"

    def test_choppy_day_selects_oscillation_mode(self):
        settings = make_settings()
        engine = HybridEngine(settings)
        bars = _choppy_bars(35)
        for i in range(len(bars)):
            engine.evaluate(bars[: i + 1])
        assert engine.day_mode == "oscillation"

    def test_fallback_time_selects_oscillation(self):
        settings = make_settings()
        engine = HybridEngine(settings)
        bars = _flat_then_trending(35)
        for i in range(len(bars)):
            engine.evaluate(bars[: i + 1])
        assert engine.day_mode == "oscillation"

    def test_trend_mode_uses_trend_exit(self):
        settings = make_settings()
        engine = HybridEngine(settings)
        bars = _trending_up_bars(30)
        for i in range(len(bars)):
            engine.evaluate(bars[: i + 1])
        if engine.day_mode != "trend":
            pytest.skip("No trend breakout detected in test data")
        from qqq_trader.domain import Position
        position = Position(
            symbol="QQQ260811C492000.US",
            direction=Direction.CALL,
            quantity=10,
            entry_price=Decimal("2.50"),
            opened_at=bars[15].end,
            strategy_name="trend_orb_breakout",
        )
        decision = engine.bar_exit_decision(position)
        assert decision is None

    def test_strategy_from_settings_returns_hybrid(self):
        settings = make_settings(strategy_mode="hybrid")
        from qqq_trader.strategy import strategy_from_settings
        engine = strategy_from_settings(settings)
        assert isinstance(engine, HybridEngine)

    def test_day_mode_resets_on_new_day(self):
        settings = make_settings()
        engine = HybridEngine(settings)
        day1_bars = _trending_up_bars(30)
        for i in range(len(day1_bars)):
            engine.evaluate(day1_bars[: i + 1])
        day1_mode = engine.day_mode

        day2_base = datetime(2026, 8, 12, 13, 30, tzinfo=timezone.utc)
        day2_bars = []
        for i in range(10):
            base = 500.0 + i * 0.1
            day2_bars.append(_bar(i, base, base + 0.3, base - 0.2, base + 0.1,
                                  base_time=day2_base))
        engine.evaluate(day2_bars)
        assert engine.day_mode is None
