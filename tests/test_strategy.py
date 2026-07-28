from datetime import datetime, timedelta, timezone
from decimal import Decimal

from conftest import make_settings

from qqq_trader.domain import Bar, Direction, ExitReason, MarketState, Position
from qqq_trader.strategy import (
    BarAggregator,
    MarketContext,
    MarketStateClassifier,
    StrategyEngine,
    bollinger_bands,
    ema_series,
    rsi,
    strategy_from_settings,
)
from qqq_trader.timed_strategy import TimedTrendStrategy


def _flat_bars(count: int, *, complete: bool = True) -> list[Bar]:
    start = datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc)
    bars = []
    for index in range(count):
        price = Decimal("500") + Decimal(index % 2) / Decimal("100")
        bars.append(
            Bar(
                "QQQ.US",
                start + timedelta(minutes=index),
                start + timedelta(minutes=index + 1),
                price,
                price + Decimal("0.05"),
                price - Decimal("0.05"),
                price,
                1000,
                complete=complete,
            )
        )
    return bars


def test_bollinger_20_2_and_rsi_overbought_oversold():
    values = [Decimal(index) for index in range(1, 21)]
    upper, middle, lower = bollinger_bands(values, 20, Decimal("2"))
    assert middle == Decimal("10.5")
    assert upper > middle > lower
    assert rsi([Decimal(index) for index in range(1, 17)], 14) == Decimal("100")
    assert rsi([Decimal(20 - index) for index in range(16)], 14) == Decimal("0")


def test_dynamic_structure_uses_completed_bars_only():
    bars = _flat_bars(16)
    incomplete = Bar(
        "QQQ.US",
        bars[-1].end,
        bars[-1].end + timedelta(minutes=1),
        Decimal("500"),
        Decimal("999"),
        Decimal("1"),
        Decimal("500"),
        1000,
        complete=False,
    )
    engine = StrategyEngine(make_settings(volatility_filter_enabled=False))
    computed = engine._context([*bars, incomplete])
    assert computed is not None
    context, today = computed
    assert len(today) == 16
    assert context.structure_high < Decimal("501")
    assert context.structure_low > Decimal("499")


def test_observation_window_and_duplicate_bar_do_not_signal():
    engine = StrategyEngine(make_settings(volatility_filter_enabled=False))
    assert engine.evaluate(_flat_bars(10)) is None
    assert engine.last_state is MarketState.OBSERVATION
    # Re-evaluating exactly the same completed history remains deterministic.
    assert engine.evaluate(_flat_bars(10)) is None


def test_five_minute_aggregator_emits_only_completed_buckets():
    bars = _flat_bars(7)
    aggregated = BarAggregator.to_five_minutes(bars)
    assert len(aggregated) == 1
    assert aggregated[0].start.minute == 30
    assert aggregated[0].end.minute == 35


def test_range_classifier_requires_four_confirmations():
    context = MarketContext(
        vwap_value=Decimal("500"),
        vwap_slope_val=Decimal("0.01"),
        ema9=Decimal("500"),
        ema20=Decimal("500.01"),
        adx_val=Decimal("15"),
        atr_val=Decimal("1"),
        prev_day_low=Decimal("495"),
        current_close=Decimal("500"),
        bar_time=datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc)
        .astimezone()
        .time()
        .replace(tzinfo=None),
    )
    # Explicit ET time avoids depending on the host timezone.
    context.bar_time = datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc).astimezone(
        __import__("zoneinfo").ZoneInfo("America/New_York")
    ).time().replace(tzinfo=None)
    bars = _flat_bars(25)
    assert MarketStateClassifier().classify(context, bars) is MarketState.RANGE


def test_trend_signal_can_fire_on_any_completed_one_minute_bar():
    start = datetime(2026, 7, 15, 13, 44, tzinfo=timezone.utc)
    bars = [
        Bar(
            "QQQ.US",
            start + timedelta(minutes=index),
            start + timedelta(minutes=index + 1),
            Decimal("100") + Decimal(index),
            Decimal("100.8") + Decimal(index),
            Decimal("99.8") + Decimal(index),
            Decimal("100.5") + Decimal(index),
            1000 if index < 2 else 2000,
        )
        for index in range(3)
    ]
    context = MarketContext(
        vwap_value=Decimal("101.5"),
        vwap_slope_val=Decimal("0.1"),
        ema9=Decimal("101.5"),
        ema20=Decimal("101"),
        macd_hist=Decimal("0.2"),
        macd_hist_prev=Decimal("0.1"),
        adx_val=Decimal("25"),
        atr_val=Decimal("1"),
        current_close=Decimal("102.5"),
        current_high=Decimal("102.8"),
        current_low=Decimal("101.8"),
        current_volume=2000,
        rsi_val=Decimal("60"),
        bar_end=bars[-1].end,
    )
    engine = StrategyEngine(make_settings(volatility_filter_enabled=False))

    signal = engine._evaluate_trend(context, bars, None)

    assert signal is not None
    assert signal.direction is Direction.CALL
    # 09:47 ET is not a five-minute boundary.
    assert signal.bar_end.minute == 47


def test_trend_rejects_neutral_rsi_or_weak_directional_macd():
    start = datetime(2026, 7, 15, 13, 44, tzinfo=timezone.utc)
    bars = [
        Bar(
            "QQQ.US",
            start + timedelta(minutes=index),
            start + timedelta(minutes=index + 1),
            Decimal("100") + Decimal(index),
            Decimal("100.8") + Decimal(index),
            Decimal("99.8") + Decimal(index),
            Decimal("100.5") + Decimal(index),
            2000,
        )
        for index in range(3)
    ]
    context = MarketContext(
        vwap_value=Decimal("101.5"),
        vwap_slope_val=Decimal("0.1"),
        ema9=Decimal("101.5"),
        ema9_prev=Decimal("101.4"),
        ema20=Decimal("101"),
        macd_hist=Decimal("0.05"),
        macd_hist_prev=Decimal("0.04"),
        adx_val=Decimal("25"),
        atr_val=Decimal("1"),
        current_close=Decimal("102.5"),
        current_high=Decimal("102.8"),
        current_low=Decimal("101.8"),
        current_volume=2000,
        rsi_val=Decimal("50"),
        bar_end=bars[-1].end,
    )
    engine = StrategyEngine(make_settings(volatility_filter_enabled=False))

    assert engine._evaluate_trend(context, bars, None) is None


def test_trend_retest_uses_stabilized_dynamic_low_without_opening_range():
    start = datetime(2026, 7, 15, 14, 40, tzinfo=timezone.utc)
    prices = [
        ("103.0", "103.2", "102.8", "103.0"),
        ("102.9", "103.0", "102.6", "102.8"),
        ("102.7", "102.8", "102.2", "102.5"),
        ("102.5", "102.8", "102.4", "102.6"),
        ("102.6", "102.7", "102.3", "102.4"),
        ("102.5", "103.2", "102.5", "103.1"),
    ]
    bars = [
        Bar(
            "QQQ.US",
            start + timedelta(minutes=index),
            start + timedelta(minutes=index + 1),
            *(Decimal(value) for value in values),
            1500,
        )
        for index, values in enumerate(prices)
    ]
    context = MarketContext(
        vwap_value=Decimal("103"),
        ema9=Decimal("102.8"),
        ema20=Decimal("103"),
        macd_hist=Decimal("0.01"),
        macd_hist_prev=Decimal("-0.02"),
        atr_val=Decimal("1"),
        day_high=Decimal("104"),
        day_low=Decimal("100"),
        current_open=bars[-1].open,
        current_high=bars[-1].high,
        current_low=bars[-1].low,
        current_close=bars[-1].close,
        current_volume=bars[-1].volume,
        rsi_val=Decimal("45"),
        bar_time=bars[-1].end.astimezone(
            __import__("zoneinfo").ZoneInfo("America/New_York")
        ).time().replace(tzinfo=None),
        bar_end=bars[-1].end,
    )
    engine = StrategyEngine(make_settings(volatility_filter_enabled=False))

    signal = engine._evaluate_retest(context, bars, None)

    assert signal is not None
    assert signal.direction is Direction.CALL
    assert signal.market_state is MarketState.TREND_RETEST_UP
    assert signal.stop_price == Decimal("102.1")
    assert "orh" not in signal.indicators
    assert "orl" not in signal.indicators


def test_timed_strategy_factory_and_opening_cutoff():
    settings = make_settings(
        strategy_profile="timed_trend",
        volatility_filter_enabled=False,
    )
    engine = strategy_from_settings(settings)
    assert isinstance(engine, TimedTrendStrategy)
    engine.last_context = MarketContext(
        current_close=Decimal("100"),
        bar_time=__import__("datetime").time(9, 45),
    )
    position = Position(
        "QQQ260715P00500000.US",
        Direction.PUT,
        1,
        Decimal("1"),
        datetime(2026, 7, 15, 13, 35, tzinfo=timezone.utc),
        strategy_name="timed_opening_scalp",
    )
    decision = engine.bar_exit_decision(position)
    assert decision is not None
    assert decision.reason is ExitReason.OPENING_CUTOFF


def test_timed_trend_signal_enters_immediately_without_pullback():
    engine = TimedTrendStrategy(
        make_settings(
            strategy_profile="timed_trend",
            volatility_filter_enabled=False,
        )
    )
    start = datetime(2026, 7, 15, 13, 45, tzinfo=timezone.utc)
    bars = [
        Bar(
            "QQQ.US",
            start + timedelta(minutes=index),
            start + timedelta(minutes=index + 1),
            Decimal("101.3") if index == 3 else Decimal("100.8"),
            Decimal("101.5"),
            Decimal("101.2") if index == 3 else Decimal("100.7"),
            Decimal("101.4"),
            1000,
        )
        for index in range(4)
    ]
    context = MarketContext(
        vwap_value=Decimal("100.8"),
        vwap_slope_val=Decimal("0.2"),
        ema9=Decimal("101"),
        ema20=Decimal("100.5"),
        atr_val=Decimal("1"),
        current_open=bars[-1].open,
        current_high=bars[-1].high,
        current_low=bars[-1].low,
        current_close=bars[-1].close,
        current_volume=bars[-1].volume,
        bar_end=bars[-1].end,
    )
    signal = engine._trend_signal(
        context,
        bars,
        Decimal("100.8"),
        Decimal("100.4"),
        None,
    )
    assert signal is not None
    assert signal.direction is Direction.CALL
    assert signal.strategy == "timed_trend_signal"
    assert signal.market_state is MarketState.TREND_UP


def test_timed_strategy_uses_one_minute_ema21_context():
    bars = _flat_bars(27)
    engine = TimedTrendStrategy(
        make_settings(
            strategy_profile="timed_trend",
            volatility_filter_enabled=False,
        )
    )

    computed = engine._one_minute_context(bars)

    assert computed is not None
    context, today, _, _ = computed
    assert context.bar_end == bars[-1].end
    assert len(today) == len(bars)
    assert context.ema20 == ema_series(
        [bar.close for bar in bars],
        21,
    )[-1]
