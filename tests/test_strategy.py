"""Tests for StrategyEngine regime classification and factory."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from conftest import make_settings
from qqq_trader.domain import Bar, Direction, ExitReason, MarketState, Position
from qqq_trader.indicators import MarketContext
from qqq_trader.strategy import StrategyEngine, squeeze_mid_break_state, strategy_from_settings


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
    """Bullish trending day: OR 9:30-9:35, breakout after 9:35."""
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


class TestStrategyEngine:

    def test_observation_during_or(self):
        settings = make_settings()
        engine = StrategyEngine(settings)
        bars = _trending_up_bars(5)
        signal = engine.evaluate(bars)
        assert signal is None
        assert engine.last_state == MarketState.OBSERVATION
        assert engine.day_mode is None

    def test_trending_day_selects_trend_mode(self):
        settings = make_settings()
        engine = StrategyEngine(settings)
        bars = _trending_up_bars(35)
        for i in range(len(bars)):
            engine.evaluate(bars[: i + 1])
        assert engine.last_state is MarketState.TREND_UP
        assert engine.day_mode == "trend"

    def test_choppy_day_selects_oscillation_mode(self):
        settings = make_settings()
        engine = StrategyEngine(settings)
        bars = _choppy_bars(35)
        for i in range(len(bars)):
            engine.evaluate(bars[: i + 1])
        assert engine.day_mode == "oscillation"

    def test_fallback_time_selects_oscillation(self):
        settings = make_settings()
        engine = StrategyEngine(settings)
        bars = _flat_then_trending(35)
        for i in range(len(bars)):
            engine.evaluate(bars[: i + 1])
        assert engine.day_mode == "oscillation"

    def test_trend_mode_uses_trend_exit(self):
        settings = make_settings()
        engine = StrategyEngine(settings)
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

    def test_vwap_reclaim_only_on_cross_bar(self):
        engine = StrategyEngine(make_settings())
        base = datetime(2026, 9, 14, 13, 30, tzinfo=timezone.utc)
        below = [_bar(i, 702.8, 703.0, 702.6, 702.9, base_time=base) for i in range(10)]
        cross = _bar(10, 703.1, 703.6, 703.0, 703.45, volume=2000, base_time=base)
        hold = _bar(11, 703.5, 704.0, 703.4, 703.82, volume=2000, base_time=base)
        engine._today_bars = [*below, cross]
        assert engine._is_vwap_reclaim(Decimal("703.22"))
        assert engine._consecutive_closes_below_vwap(Decimal("703.22")) >= 2
        engine._today_bars = [*below, cross, hold]
        assert not engine._is_vwap_reclaim(Decimal("703.22"))

    def test_strategy_from_settings_returns_strategy_engine(self):
        settings = make_settings(strategy_mode="hybrid")
        engine = strategy_from_settings(settings)
        assert isinstance(engine, StrategyEngine)

    def test_day_mode_resets_on_new_day(self):
        settings = make_settings()
        engine = StrategyEngine(settings)
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

    def test_indicators_ignore_prior_day_bars(self):
        settings = make_settings()
        engine = StrategyEngine(settings)
        prior_base = datetime(2026, 8, 10, 13, 30, tzinfo=timezone.utc)
        prior = [
            _bar(i, 10.0, 10.1, 9.9, 10.0, volume=1000, base_time=prior_base)
            for i in range(80)
        ]
        today = _choppy_bars(40)
        engine.evaluate(prior + today)
        assert engine._indicator_closes == [bar.close for bar in today]


def _osc(count: int, amplitude: Decimal, center: Decimal = Decimal("100")) -> list[Decimal]:
    return [
        center + (amplitude if i % 2 == 0 else -amplitude) for i in range(count)
    ]


def _squeeze_series() -> list[Decimal]:
    """Wide history, tight coil, then a close just above the middle."""
    return (
        _osc(20, Decimal("2"))
        + _osc(90, Decimal("2"))
        + _osc(19, Decimal("0.08"))
        + [Decimal("99.92"), Decimal("100.08")]
    )


class TestSqueezeMidBreak:
    def test_session_lookback_adapts_before_ninety_bars(self):
        closes = _osc(20, Decimal("3")) + _osc(20, Decimal("0.05"))
        _armed, _fire, squeezed = squeeze_mid_break_state(closes, lookback=90)
        assert squeezed

    def test_fires_after_coil_and_mid_cross(self):
        armed, fire, _ = squeeze_mid_break_state(
            _squeeze_series(),
            expand=Decimal("1.00"),
            max_coil_width=Decimal("1"),
        )
        assert armed
        assert fire

    def test_fires_when_mid_cross_comes_after_coil_expands(self):
        closes = (
            _osc(25, Decimal("0.4"))
            + _osc(10, Decimal("0.03"))
            + [
                Decimal("99.50"),
                Decimal("99.20"),
                Decimal("99.45"),
                Decimal("99.15"),
                Decimal("99.40"),
                Decimal("100.03"),
            ]
        )
        armed, fire, squeezed = squeeze_mid_break_state(
            closes, lookback=90, max_coil_width=Decimal("1")
        )
        assert not squeezed
        assert armed
        assert fire

    def test_absolute_coil_width_cap_blocks_loose_squeeze(self):
        armed, fire, _ = squeeze_mid_break_state(
            _squeeze_series(),
            expand=Decimal("1.00"),
            max_coil_width=Decimal("0.0029"),
        )
        assert armed
        assert not fire

    def test_does_not_fire_after_hold_expires(self):
        closes = (
            _osc(25, Decimal("0.4"))
            + _osc(10, Decimal("0.03"))
            + [
                Decimal("99.50"),
                Decimal("99.20"),
                Decimal("99.45"),
                Decimal("99.15"),
                Decimal("99.40"),
            ]
            + [Decimal("99.8") if i % 2 == 0 else Decimal("97.5") for i in range(24)]
            + [Decimal("100.05")]
        )
        armed, fire, _ = squeeze_mid_break_state(closes, lookback=90, hold=20)
        assert not armed
        assert not fire

    def test_does_not_fire_without_prior_squeeze(self):
        closes = _osc(80, Decimal("2")) + [Decimal("104")]
        armed, fire, _ = squeeze_mid_break_state(closes)
        assert not armed
        assert not fire

    def test_expand_gate_blocks_when_width_does_not_grow(self):
        _armed, fire, _ = squeeze_mid_break_state(
            _squeeze_series(),
            expand=Decimal("1.20"),
        )
        assert not fire

    def test_one_fire_per_coil_until_squeeze_ends(self):
        settings = make_settings()
        engine = StrategyEngine(settings)
        engine.last_state = MarketState.RANGE
        engine.last_context = MarketContext(
            bar_end=datetime(2026, 8, 11, 15, 0, tzinfo=timezone.utc),
            current_close=Decimal("100.08"),
        )
        engine._indicator_closes = _squeeze_series()
        engine._squeeze_need_reset = True
        assert not engine._refresh_squeeze_state()
        engine._indicator_closes = _osc(120, Decimal("2"))
        assert not engine._refresh_squeeze_state()
        assert not engine._squeeze_need_reset
        engine.last_state = MarketState.RANGE
        signal = engine._squeeze_mid_break_signal(Decimal("100.08"))
        assert signal is not None
        assert signal.strategy == "squeeze_mid_break"
        assert signal.direction is Direction.CALL

    def test_exit_when_close_back_below_middle(self):
        settings = make_settings()
        engine = StrategyEngine(settings)
        bars = _choppy_bars(40)
        for i in range(len(bars)):
            engine.evaluate(bars[: i + 1])
        ctx = engine.last_context
        assert ctx is not None
        position = Position(
            symbol="QQQ260811C492000.US",
            direction=Direction.CALL,
            quantity=10,
            entry_price=Decimal("2.50"),
            opened_at=bars[-1].end,
            strategy_name="squeeze_mid_break",
        )
        engine.last_context = replace(
            ctx, current_close=ctx.boll_middle - Decimal("0.05")
        )
        decision = engine.bar_exit_decision(position)
        assert decision is not None
        assert decision.reason is ExitReason.BOLLINGER_MIDDLE

