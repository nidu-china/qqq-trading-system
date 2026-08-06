from datetime import datetime, timedelta, timezone
from decimal import Decimal

from conftest import make_settings

from qqq_trader.domain import Bar, Direction, ExitReason, MarketState, Position
from qqq_trader.indicators import (
    BarAggregator,
    MarketContext,
    bollinger_bands,
    macd_histogram,
    rsi,
)
from qqq_trader.policy import RULES
from qqq_trader.strategy import (
    StrategyEngine,
    strategy_from_settings,
)
from qqq_trader.volatility import VixFiveMinuteTrend, vix_five_minute_trend


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


def test_five_minute_aggregator_emits_only_completed_buckets():
    bars = _flat_bars(7)
    aggregated = BarAggregator.to_five_minutes(bars)
    assert len(aggregated) == 1
    assert aggregated[0].start.minute == 30
    assert aggregated[0].end.minute == 35


def test_strategy_factory_and_opening_cutoff():
    settings = make_settings(volatility_filter_enabled=False)
    engine = strategy_from_settings(settings)
    assert isinstance(engine, StrategyEngine)
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
        strategy_name="timed_opening_signal",
    )
    decision = engine.bar_exit_decision(position)
    assert decision is not None
    assert decision.reason is ExitReason.OPENING_CUTOFF


def test_main_entry_window_runs_until_noon():
    assert RULES.timed_main_last_signal == __import__("datetime").time(12, 0)


def test_profitable_exit_resets_crosses_after_five_stable_closes():
    engine = StrategyEngine(make_settings())
    exited_at = datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc)
    sides = [True, False, True, False, True, True, True, True, True]

    assert engine._effective_boll_middle_crosses(sides) == 4
    engine.record_profitable_exit(Direction.CALL, exited_at)
    assert engine._effective_boll_middle_crosses(sides) == 0
    assert engine._last_cross_reset

    engine.record_entry(Direction.CALL, exited_at + timedelta(minutes=6))
    assert engine._effective_boll_middle_crosses(sides) == 4


def _continuation_context(**overrides) -> MarketContext:
    values = {
        "boll_upper": Decimal("101.2"),
        "boll_middle": Decimal("100"),
        "boll_middle_prev": Decimal("99.9"),
        "boll_middle_prev2": Decimal("99.8"),
        "boll_lower": Decimal("98.8"),
        "current_close": Decimal("101"),
        "macd_hist": Decimal("0.2"),
        "macd_hist_prev": Decimal("0.1"),
        "rvol_val": Decimal("1.5"),
        "rvol_prev": Decimal("1.3"),
        "rsi_val": Decimal("60"),
        "boll_middle_crosses": 0,
        "bar_end": datetime(2026, 7, 15, 15, 0, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return MarketContext(**values)


def test_continuation_reentry_requires_unextended_price_and_expanding_volume():
    engine = StrategyEngine(make_settings())
    engine._last_cross_reset = True

    signal = engine._entry_signal(
        _continuation_context(),
        "timed_boll_macd_signal",
        None,
    )
    assert signal is not None
    assert signal.strategy == "timed_trend_continuation"

    overextended = _continuation_context(current_close=Decimal("101.45"))
    assert engine._entry_signal(overextended, "timed_boll_macd_signal", None) is None

    fading_volume = _continuation_context(rvol_prev=Decimal("1.6"))
    assert engine._entry_signal(fading_volume, "timed_boll_macd_signal", None) is None


def test_fresh_macd_continuation_requires_twenty_percent_volume_buffer():
    engine = StrategyEngine(make_settings())
    engine._last_cross_reset = True

    weak_cross = _continuation_context(
        macd_hist_prev=Decimal("-0.01"),
        rvol_val=Decimal("1.44"),
    )
    assert engine._entry_signal(weak_cross, "timed_boll_macd_signal", None) is None

    strong_cross = _continuation_context(
        macd_hist_prev=Decimal("-0.01"),
        rvol_val=Decimal("1.45"),
    )
    signal = engine._entry_signal(strong_cross, "timed_boll_macd_signal", None)
    assert signal is not None
    assert signal.direction is Direction.CALL


def test_normal_fresh_macd_filter_requires_expanding_buffered_volume():
    engine = StrategyEngine(
        make_settings(),
        normal_fresh_macd_filter=True,
    )
    weak_cross = _continuation_context(
        macd_hist_prev=Decimal("-0.01"),
        rvol_val=Decimal("1.44"),
        rvol_prev=Decimal("1.3"),
    )
    assert engine._entry_signal(weak_cross, "timed_boll_macd_signal", None) is None

    fading_cross = _continuation_context(
        macd_hist_prev=Decimal("-0.01"),
        rvol_val=Decimal("1.5"),
        rvol_prev=Decimal("1.6"),
    )
    assert engine._entry_signal(fading_cross, "timed_boll_macd_signal", None) is None

    strong_cross = _continuation_context(
        macd_hist_prev=Decimal("-0.01"),
        rvol_val=Decimal("1.45"),
        rvol_prev=Decimal("1.3"),
    )
    signal = engine._entry_signal(strong_cross, "timed_boll_macd_signal", None)
    assert signal is not None
    assert signal.strategy == "timed_boll_macd_signal"


def test_normal_cross2_filter_requires_unextended_price_and_expanding_volume():
    engine = StrategyEngine(make_settings(), normal_cross2_filter=True)
    qualified = _continuation_context(boll_middle_crosses=2)
    assert engine._entry_signal(qualified, "timed_boll_macd_signal", None) is not None

    overextended = _continuation_context(
        boll_middle_crosses=2,
        current_close=Decimal("101.45"),
    )
    assert engine._entry_signal(overextended, "timed_boll_macd_signal", None) is None

    fading_volume = _continuation_context(
        boll_middle_crosses=2,
        rvol_prev=Decimal("1.6"),
    )
    assert engine._entry_signal(fading_volume, "timed_boll_macd_signal", None) is None

    cross1 = _continuation_context(
        boll_middle_crosses=1,
        current_close=Decimal("101.45"),
        rvol_prev=Decimal("1.6"),
    )
    assert engine._entry_signal(cross1, "timed_boll_macd_signal", None) is not None


def test_trend_runner_exits_on_boll_middle_or_macd_reversal():
    engine = StrategyEngine(make_settings())
    position = Position(
        "QQQ260715C00500000.US",
        Direction.CALL,
        2,
        Decimal("1"),
        datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc),
        strategy_name="timed_boll_macd_signal",
        trend_runner=True,
    )
    engine.last_context = MarketContext(
        current_close=Decimal("99.9"),
        boll_middle=Decimal("100"),
        macd_hist=Decimal("0.1"),
        macd_hist_prev=Decimal("0.2"),
        bar_time=__import__("datetime").time(10, 0),
    )
    decision = engine.bar_exit_decision(position)
    assert decision is not None
    assert decision.reason is ExitReason.BOLLINGER_MIDDLE

    engine.last_context.current_close = Decimal("100.1")
    engine.last_context.macd_hist = Decimal("-0.1")
    engine.last_context.macd_hist_prev = Decimal("0.05")
    decision = engine.bar_exit_decision(position)
    assert decision is not None
    assert decision.reason is ExitReason.DIRECTION_REVERSAL


def test_timed_boll_macd_signal_enters_immediately():
    engine = StrategyEngine(
        make_settings(volatility_filter_enabled=False)
    )
    context = MarketContext(
        boll_middle=Decimal("100"),
        boll_middle_prev=Decimal("99.9"),
        boll_middle_prev2=Decimal("99.8"),
        macd_hist=Decimal("0.2"),
        macd_hist_prev=Decimal("0.1"),
        rvol_val=Decimal("1.21"),
        rsi_val=Decimal("69"),
        current_close=Decimal("101"),
        bar_end=datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc),
    )
    signal = engine._entry_signal(
        context, "timed_boll_macd_signal", None
    )
    assert signal is not None
    assert signal.direction is Direction.CALL
    assert signal.strategy == "timed_boll_macd_signal"
    assert signal.market_state is MarketState.TREND_UP


def test_timed_signal_filters_exact_volume_threshold_and_overbought_rsi():
    engine = StrategyEngine(
        make_settings()
    )
    context = MarketContext(
        boll_middle=Decimal("100"),
        boll_middle_prev=Decimal("99.9"),
        boll_middle_prev2=Decimal("99.8"),
        macd_hist=Decimal("0.2"),
        macd_hist_prev=Decimal("0.1"),
        rvol_val=Decimal("1.2"),
        rsi_val=Decimal("69"),
        current_close=Decimal("101"),
        bar_end=datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc),
    )
    assert engine._entry_signal(context, "timed_boll_macd_signal", None) is None
    context.rvol_val = Decimal("1.21")
    context.rsi_val = Decimal("70")
    assert engine._entry_signal(context, "timed_boll_macd_signal", None) is None


def test_timed_call_rejects_negative_macd_even_when_it_is_contracting():
    engine = StrategyEngine(
        make_settings()
    )
    context = MarketContext(
        boll_middle=Decimal("100"),
        boll_middle_prev=Decimal("99.9"),
        boll_middle_prev2=Decimal("99.8"),
        macd_hist=Decimal("-0.06"),
        macd_hist_prev=Decimal("-0.1"),
        rvol_val=Decimal("1.21"),
        rsi_val=Decimal("60"),
        current_close=Decimal("101"),
        bar_end=datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc),
    )

    assert engine._entry_signal(context, "timed_boll_macd_signal", None) is None
    context.macd_hist = Decimal("-0.04")
    assert engine._entry_signal(context, "timed_boll_macd_signal", None) is None


def test_timed_call_requires_volume_even_on_boll_reclaim():
    engine = StrategyEngine(
        make_settings()
    )
    context = MarketContext(
        boll_middle=Decimal("100"),
        boll_middle_prev=Decimal("99.9"),
        boll_middle_prev2=Decimal("99.8"),
        prev2_close=Decimal("101"),
        prev_close=Decimal("99"),
        current_close=Decimal("101"),
        macd_hist=Decimal("0.2"),
        macd_hist_prev=Decimal("0.1"),
        rvol_val=Decimal("0.5"),
        rsi_val=Decimal("50"),
        bar_time=__import__("datetime").time(9, 59),
        bar_end=datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc),
    )

    assert engine._entry_signal(context, "timed_boll_macd_signal", None) is None


def test_timed_call_rejects_first_low_volume_boll_cross():
    engine = StrategyEngine(
        make_settings()
    )
    context = MarketContext(
        boll_middle=Decimal("100"),
        boll_middle_prev=Decimal("99.9"),
        boll_middle_prev2=Decimal("99.8"),
        prev2_close=Decimal("99"),
        prev_close=Decimal("99"),
        current_close=Decimal("101"),
        macd_hist=Decimal("0.2"),
        macd_hist_prev=Decimal("0.1"),
        rvol_val=Decimal("0.5"),
        rsi_val=Decimal("50"),
        bar_time=__import__("datetime").time(9, 59),
        bar_end=datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc),
    )

    assert engine._entry_signal(context, "timed_boll_macd_signal", None) is None


def test_timed_call_accepts_all_five_required_conditions():
    engine = StrategyEngine(
        make_settings()
    )
    context = MarketContext(
        boll_middle=Decimal("100"),
        boll_middle_prev=Decimal("99.9"),
        boll_middle_prev2=Decimal("99.8"),
        current_close=Decimal("101"),
        macd_hist=Decimal("0.2"),
        macd_hist_prev=Decimal("0.1"),
        rvol_val=Decimal("2"),
        rsi_val=Decimal("50"),
        bar_time=__import__("datetime").time(10, 30),
        bar_end=datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc),
    )

    signal = engine._entry_signal(context, "timed_boll_macd_signal", None)
    assert signal is not None
    assert signal.direction is Direction.CALL


def test_timed_call_requires_two_consecutive_rising_boll_middles():
    engine = StrategyEngine(make_settings())
    context = MarketContext(
        boll_middle=Decimal("100"),
        boll_middle_prev=Decimal("99.9"),
        boll_middle_prev2=Decimal("100.1"),
        current_close=Decimal("101"),
        macd_hist=Decimal("0.2"),
        macd_hist_prev=Decimal("0.1"),
        rvol_val=Decimal("2"),
        rsi_val=Decimal("50"),
        boll_middle_crosses=0,
        bar_end=datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc),
    )

    assert engine._entry_signal(context, "timed_boll_macd_signal", None) is None


def test_timed_put_requires_falling_boll_middle_and_at_most_two_crosses():
    engine = StrategyEngine(make_settings())
    context = MarketContext(
        boll_middle=Decimal("100"),
        boll_middle_prev=Decimal("100.1"),
        boll_middle_prev2=Decimal("100.2"),
        current_close=Decimal("99"),
        macd_hist=Decimal("-0.2"),
        macd_hist_prev=Decimal("-0.1"),
        rvol_val=Decimal("2"),
        rsi_val=Decimal("50"),
        boll_middle_crosses=2,
        bar_end=datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc),
    )

    signal = engine._entry_signal(context, "timed_boll_macd_signal", None)
    assert signal is not None
    assert signal.direction is Direction.PUT

    context.boll_middle_crosses = 3
    assert engine._entry_signal(context, "timed_boll_macd_signal", None) is None


def test_vix_trend_adjusts_call_and_put_thresholds_symmetrically():
    engine = StrategyEngine(make_settings())

    engine.vix_trend = VixFiveMinuteTrend.FALLING
    assert engine._entry_thresholds(Direction.CALL) == (
        Decimal("1.08"),
        Decimal("70"),
        2,
    )
    assert engine._entry_thresholds(Direction.PUT) == (
        Decimal("1.32"),
        Decimal("30"),
        2,
    )

    engine.vix_trend = VixFiveMinuteTrend.RISING
    assert engine._entry_thresholds(Direction.CALL) == (
        Decimal("1.32"),
        Decimal("70"),
        2,
    )
    assert engine._entry_thresholds(Direction.PUT) == (
        Decimal("1.08"),
        Decimal("30"),
        2,
    )


def test_falling_vix_allows_call_with_relaxed_volume_threshold():
    engine = StrategyEngine(make_settings())
    context = MarketContext(
        boll_middle=Decimal("100"),
        boll_middle_prev=Decimal("99.9"),
        boll_middle_prev2=Decimal("99.8"),
        current_close=Decimal("101"),
        macd_hist=Decimal("0.2"),
        macd_hist_prev=Decimal("0.1"),
        rvol_val=Decimal("1.09"),
        rsi_val=Decimal("69"),
        boll_middle_crosses=2,
        bar_end=datetime(2026, 8, 3, 13, 56, tzinfo=timezone.utc),
    )

    assert engine._entry_signal(context, "timed_boll_macd_signal", None) is None
    engine.vix_trend = VixFiveMinuteTrend.FALLING
    signal = engine._entry_signal(context, "timed_boll_macd_signal", None)
    assert signal is not None
    assert signal.direction is Direction.CALL
    assert signal.indicators["vix_5m_trend"] == "falling"
    assert signal.indicators["volume_ratio_threshold"] == "1.080"


def test_vix_five_minute_trend_uses_only_completed_visible_bars():
    decision_at = datetime(2026, 8, 3, 14, 0, tzinfo=timezone.utc)

    def vix_bar(minutes: int, opened: str, closed: str) -> Bar:
        end = decision_at + timedelta(minutes=minutes)
        open_value = Decimal(opened)
        close_value = Decimal(closed)
        return Bar(
            ".VIX.US",
            end - timedelta(minutes=5),
            end,
            open_value,
            max(open_value, close_value),
            min(open_value, close_value),
            close_value,
            0,
        )

    bars = [
        vix_bar(-10, "16.30", "16.25"),
        vix_bar(-5, "16.24", "16.14"),
        vix_bar(5, "16.10", "16.50"),
    ]

    assert vix_five_minute_trend(bars, decision_at, 10) is VixFiveMinuteTrend.FALLING

    bars[1] = vix_bar(-5, "16.25", "16.20")
    assert vix_five_minute_trend(bars, decision_at, 10) is VixFiveMinuteTrend.NEUTRAL


def test_opening_put_uses_price_and_volume_without_macd_or_rsi_filters():
    engine = StrategyEngine(make_settings())
    context = MarketContext(
        boll_middle=Decimal("100"),
        current_open=Decimal("99.5"),
        current_close=Decimal("99"),
        prev_close=Decimal("99.4"),
        macd_hist=Decimal("0.2"),
        macd_hist_prev=Decimal("0.1"),
        rvol_val=Decimal("1.21"),
        rsi_val=Decimal("10"),
        bar_end=datetime(2026, 7, 15, 13, 35, tzinfo=timezone.utc),
    )

    signal = engine._opening_signal(context, None)
    assert signal is not None
    assert signal.direction is Direction.PUT
    assert signal.strategy == "timed_opening_signal"


def test_opening_call_requires_bullish_price_trend_and_strict_volume():
    engine = StrategyEngine(make_settings())
    context = MarketContext(
        boll_middle=Decimal("100"),
        current_open=Decimal("100.5"),
        current_close=Decimal("101"),
        prev_close=Decimal("100.6"),
        macd_hist=Decimal("-0.2"),
        macd_hist_prev=Decimal("-0.1"),
        rvol_val=Decimal("1.2"),
        rsi_val=Decimal("90"),
        bar_end=datetime(2026, 7, 15, 13, 35, tzinfo=timezone.utc),
    )

    assert engine._opening_signal(context, None) is None
    context.rvol_val = Decimal("1.21")
    signal = engine._opening_signal(context, None)
    assert signal is not None
    assert signal.direction is Direction.CALL


def test_current_boll_breakthrough_is_not_counted_as_prior_chop():
    sides = [False, True, False, True, False]
    assert StrategyEngine._prior_boll_middle_crosses(sides) == 3


def test_timed_call_low_volume_reclaim_rejects_range_market():
    engine = StrategyEngine(
        make_settings()
    )
    context = MarketContext(
        boll_middle=Decimal("100"),
        boll_middle_prev=Decimal("100"),
        boll_middle_prev2=Decimal("100"),
        prev2_close=Decimal("101"),
        prev_close=Decimal("99"),
        current_close=Decimal("101"),
        macd_hist=Decimal("0.2"),
        macd_hist_prev=Decimal("0.1"),
        rvol_val=Decimal("0.5"),
        rsi_val=Decimal("50"),
        boll_middle_crosses=4,
        bar_time=__import__("datetime").time(10, 30),
        bar_end=datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc),
    )

    assert engine._entry_signal(context, "timed_boll_macd_signal", None) is None


def test_timed_put_also_rejects_range_market():
    engine = StrategyEngine(
        make_settings()
    )
    context = MarketContext(
        boll_middle=Decimal("100"),
        current_close=Decimal("99"),
        macd_hist=Decimal("-0.2"),
        macd_hist_prev=Decimal("-0.1"),
        rvol_val=Decimal("2"),
        rsi_val=Decimal("50"),
        boll_middle_crosses=4,
        bar_time=__import__("datetime").time(10, 30),
        bar_end=datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc),
    )

    assert engine._entry_signal(context, "timed_boll_macd_signal", None) is None


def test_timed_strategy_uses_one_minute_boll_and_macd_context():
    bars = _flat_bars(27)
    engine = StrategyEngine(
        make_settings(volatility_filter_enabled=False)
    )

    computed = engine._one_minute_context(bars)

    assert computed is not None
    context, today = computed
    assert context.bar_end == bars[-1].end
    assert len(today) == len(bars)
    closes = [bar.close for bar in bars]
    assert (context.boll_upper, context.boll_middle, context.boll_lower) == (
        bollinger_bands(closes, 20, Decimal("2"))
    )
    assert context.macd_hist == macd_histogram(closes, 8, 17, 9)[2]


def _set_timed_reversal_context(
    engine: StrategyEngine,
    bars: list[Bar],
    boll_values: list[Decimal],
    macd_values: list[Decimal],
) -> None:
    engine.last_context = MarketContext(
        bar_time=__import__("datetime").time(10, 5)
    )
    engine._last_today_1m = bars
    engine._last_boll_middle_by_end = {
        bar.end: value for bar, value in zip(bars, boll_values, strict=True)
    }
    engine._last_macd_hist_by_end = {
        bar.end: value for bar, value in zip(bars, macd_values, strict=True)
    }


def test_timed_call_reversal_can_confirm_on_third_bar():
    engine = StrategyEngine(
        make_settings()
    )
    start = datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc)
    bars = [
        Bar(
            "QQQ.US",
            start + timedelta(minutes=index),
            start + timedelta(minutes=index + 1),
            Decimal("101") - Decimal(index) / Decimal("10"),
            Decimal("101.1") - Decimal(index) / Decimal("10"),
            Decimal("100.4") - Decimal(index) / Decimal("10"),
            Decimal("100.5") - Decimal(index) / Decimal("10"),
            1000,
        )
        for index in range(3)
    ]
    _set_timed_reversal_context(
        engine,
        bars,
        [Decimal("101"), Decimal("100.8"), Decimal("100.6")],
        [Decimal("0.2"), Decimal("0.05"), Decimal("-0.1")],
    )
    position = Position(
        "QQQ260715C00500000.US",
        Direction.CALL,
        3,
        Decimal("1"),
        start - timedelta(seconds=1),
        strategy_name="timed_boll_macd_signal",
    )
    decision = engine.bar_exit_decision(position)
    assert decision is not None
    assert decision.reason is ExitReason.DIRECTION_REVERSAL


def test_timed_reversal_requires_boll_and_macd_confirmation():
    engine = StrategyEngine(
        make_settings()
    )
    start = datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc)
    bars = [
        Bar(
            "QQQ.US",
            start + timedelta(minutes=index),
            start + timedelta(minutes=index + 1),
            Decimal("101"),
            Decimal("101.1"),
            Decimal("100.4"),
            Decimal("100.5"),
            1000,
        )
        for index in range(5)
    ]
    position = Position(
        "QQQ260715C00500000.US",
        Direction.CALL,
        3,
        Decimal("1"),
        start - timedelta(seconds=1),
        strategy_name="timed_boll_macd_signal",
    )
    _set_timed_reversal_context(
        engine,
        bars,
        [Decimal("101")] * 5,
        [
            Decimal("0.4"),
            Decimal("0.3"),
            Decimal("0.2"),
            Decimal("0.1"),
            Decimal("0.05"),
        ],
    )
    assert engine.bar_exit_decision(position) is None


def test_timed_put_reversal_is_symmetric():
    engine = StrategyEngine(
        make_settings()
    )
    start = datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc)
    bars = [
        Bar(
            "QQQ.US",
            start + timedelta(minutes=index),
            start + timedelta(minutes=index + 1),
            Decimal("100") + Decimal(index) / Decimal("10"),
            Decimal("100.6") + Decimal(index) / Decimal("10"),
            Decimal("99.9") + Decimal(index) / Decimal("10"),
            Decimal("100.5") + Decimal(index) / Decimal("10"),
            1000,
        )
        for index in range(3)
    ]
    _set_timed_reversal_context(
        engine,
        bars,
        [Decimal("100"), Decimal("100.2"), Decimal("100.4")],
        [Decimal("-0.2"), Decimal("-0.05"), Decimal("0.1")],
    )
    position = Position(
        "QQQ260715P00500000.US",
        Direction.PUT,
        3,
        Decimal("1"),
        start - timedelta(seconds=1),
        strategy_name="timed_boll_macd_signal",
    )
    decision = engine.bar_exit_decision(position)
    assert decision is not None
    assert decision.reason is ExitReason.DIRECTION_REVERSAL
