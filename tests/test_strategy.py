from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from qqq_trader.config import Settings
from qqq_trader.domain import Bar, Direction
from qqq_trader.strategy import (
    BarAggregator,
    MarketStateClassifier,
    StrategyEngine,
    adx,
    atr,
    atr_series,
    ema,
    ema_series,
    macd_histogram,
    macd_histogram_series,
    rvol,
    strategy_from_settings,
    vwap,
    vwap_slope,
)


def test_ema_calculation():
    values = [Decimal(str(i)) for i in range(1, 11)]
    result = ema(values, 3)
    assert result > Decimal(8)


def test_ema_series_length():
    values = [Decimal(str(i)) for i in range(1, 21)]
    series = ema_series(values, 5)
    assert len(series) == 16  # 20 - 5 + 1


def test_macd_histogram():
    # Use non-linear data to ensure non-zero histogram
    values = [Decimal("100") + Decimal(str(i * 0.5 + (i % 3) * 0.2)) for i in range(40)]
    line, signal, hist = macd_histogram(values, 12, 26, 9)
    assert line > 0  # Uptrend data should have positive MACD line


def test_macd_histogram_series():
    values = [Decimal("100") + Decimal(str(i * 0.5)) for i in range(40)]
    series = macd_histogram_series(values, 12, 26, 9)
    assert len(series) > 0


def test_vwap_calculation():
    start = datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc)
    bars = [
        Bar("QQQ.US", start + timedelta(minutes=i), start + timedelta(minutes=i + 1),
            Decimal("100"), Decimal("101"), Decimal("99"), Decimal("100"),
            1000, Decimal("100000"))
        for i in range(5)
    ]
    result = vwap(bars)
    assert result == Decimal("100")


def test_vwap_slope():
    start = datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc)
    bars = [
        Bar("QQQ.US", start + timedelta(minutes=i * 5), start + timedelta(minutes=i * 5 + 5),
            Decimal(str(100 + i)), Decimal(str(101 + i)), Decimal(str(99 + i)),
            Decimal(str(100 + i)),
            1000, Decimal("100000"))
        for i in range(6)
    ]
    slope = vwap_slope(bars, lookback=3)
    assert slope > 0


def test_atr_calculation():
    start = datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc)
    bars = [
        Bar("QQQ.US", start + timedelta(minutes=i * 5), start + timedelta(minutes=i * 5 + 5),
            Decimal("100"), Decimal(str(100 + (i % 3))), Decimal(str(99 - (i % 2))),
            Decimal("100"),
            1000, Decimal("100000"))
        for i in range(20)
    ]
    result = atr(bars, 14)
    assert result > 0


def test_atr_series_length():
    start = datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc)
    bars = [
        Bar("QQQ.US", start + timedelta(minutes=i * 5), start + timedelta(minutes=i * 5 + 5),
            Decimal("100"), Decimal(str(100 + (i % 3))), Decimal(str(99 - (i % 2))),
            Decimal("100"),
            1000, Decimal("100000"))
        for i in range(20)
    ]
    series = atr_series(bars, 14)
    assert len(series) >= 1


def test_adx_calculation():
    start = datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc)
    bars = [
        Bar("QQQ.US", start + timedelta(minutes=i * 5), start + timedelta(minutes=i * 5 + 5),
            Decimal(str(100 + i * 0.5)),
            Decimal(str(101 + i * 0.5)),
            Decimal(str(99 + i * 0.5)),
            Decimal(str(100.5 + i * 0.5)),
            1000, Decimal("100000"))
        for i in range(30)
    ]
    result = adx(bars, 14)
    assert result >= 0


def test_rvol_calculation():
    assert rvol(2000, [1000, 1000, 1000]) == Decimal("2.0")
    assert rvol(0, [1000, 1000]) == Decimal("1.0")
    assert rvol(1000, []) == Decimal("1.0")


def test_bar_aggregator_five_minutes():
    start = datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc)
    bars = [
        Bar(
            "QQQ.US",
            start + timedelta(minutes=index),
            start + timedelta(minutes=index + 1),
            Decimal("100"),
            Decimal("101"),
            Decimal("99"),
            Decimal("100.5"),
            10,
            Decimal("1000"),
        )
        for index in range(6)
    ]
    result = BarAggregator.to_five_minutes(bars)
    assert len(result) == 1
    assert result[0].volume == 50
    assert result[0].start == start
    assert result[0].end == start + timedelta(minutes=5)


def test_bar_aggregator_requires_all_five():
    start = datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc)
    bars = [
        Bar(
            "QQQ.US",
            start + timedelta(minutes=index),
            start + timedelta(minutes=index + 1),
            Decimal("100"),
            Decimal("101"),
            Decimal("99"),
            Decimal("100.5"),
            10,
            Decimal("1000"),
        )
        for index in range(4)
    ]
    result = BarAggregator.to_five_minutes(bars)
    assert len(result) == 0


def test_strategy_from_settings_returns_engine():
    settings = Settings(trading_mode="replay")
    engine = strategy_from_settings(settings)
    assert isinstance(engine, StrategyEngine)


def test_strategy_ignores_incomplete_last_bar(bullish_bars):
    last = bullish_bars[-1]
    bullish_bars[-1] = Bar(
        symbol=last.symbol,
        start=last.start,
        end=last.end,
        open=last.open,
        high=last.high,
        low=last.low,
        close=last.close,
        volume=last.volume,
        complete=False,
    )
    settings = Settings(trading_mode="replay")
    engine = strategy_from_settings(settings)
    assert engine.evaluate(bullish_bars) is None


def test_strategy_no_signal_before_entry_start():
    """Strategy returns None before 9:45 ET."""
    start = datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc)  # 9:30 ET
    bars = [
        Bar("QQQ.US", start + timedelta(minutes=i), start + timedelta(minutes=i + 1),
            Decimal("100"), Decimal("101"), Decimal("99"), Decimal("100"),
            1000, Decimal("100000"))
        for i in range(14)  # ends at 9:44, no 5-min completion at 9:45
    ]
    settings = Settings(trading_mode="replay")
    engine = strategy_from_settings(settings)
    assert engine.evaluate(bars) is None


def test_market_state_classifier_detects_range():
    """Classifier returns RANGE when conditions are met."""
    from qqq_trader.strategy import MarketContext, MarketState
    settings = Settings(trading_mode="replay")
    classifier = MarketStateClassifier(settings)
    ctx = MarketContext(
        orh=Decimal("101"),
        orl=Decimal("99"),
        vwap_value=Decimal("100"),
        vwap_slope_val=Decimal("0.01"),
        ema9=Decimal("100.01"),
        ema20=Decimal("100.00"),
        adx_val=Decimal("15"),
        macd_hist=Decimal("0.01"),
        macd_hist_prev=Decimal("0.01"),
    )
    start = datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc)
    bars = [
        Bar("QQQ.US", start + timedelta(minutes=i * 5), start + timedelta(minutes=i * 5 + 5),
            Decimal("100"), Decimal("100.3"), Decimal("99.7"), Decimal("100"),
            1000)
        for i in range(6)
    ]
    state = classifier.classify(ctx, bars)
    assert state == MarketState.RANGE
