from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from qqq_trader.domain import Bar, Direction
from qqq_trader.strategy import (
    BarAggregator,
    MacdBollingerStrategy,
    bollinger_bands,
    ema,
    macd,
    rsi,
)


def test_indicators(bullish_bars):
    closes = [bar.close for bar in bullish_bars]
    assert ema([Decimal(value) for value in range(1, 11)], 3) > Decimal(8)
    macd_line, signal_line = macd(closes)
    middle, upper, lower = bollinger_bands(closes)
    assert macd_line > signal_line
    assert lower < middle < upper
    assert Decimal(0) < rsi(closes) < Decimal(100)


def test_bullish_breakout_signal(bullish_bars):
    signal = MacdBollingerStrategy().evaluate(bullish_bars)
    assert signal is not None
    assert signal.direction is Direction.CALL
    assert Decimal(signal.indicators["macd"]) > Decimal(signal.indicators["macd_signal"])
    assert Decimal(signal.indicators["volume_ratio"]) > Decimal("1.2")
    assert Decimal(signal.indicators["rsi"]) < Decimal("70")
    assert signal.spot == bullish_bars[-1].close


def test_bearish_breakout_signal(bullish_bars):
    bearish = [
        Bar(
            bar.symbol,
            bar.start,
            bar.end,
            Decimal("200") - bar.open,
            Decimal("200") - bar.low,
            Decimal("200") - bar.high,
            Decimal("200") - bar.close,
            bar.volume,
        )
        for bar in bullish_bars
    ]
    signal = MacdBollingerStrategy().evaluate(bearish)
    assert signal is not None
    assert signal.direction is Direction.PUT
    assert Decimal(signal.indicators["rsi"]) > Decimal("30")


def test_volume_filter_rejects_breakout(bullish_bars):
    last = bullish_bars[-1]
    bullish_bars[-1] = Bar(
        last.symbol,
        last.start,
        last.end,
        last.open,
        last.high,
        last.low,
        last.close,
        1000,
    )
    assert MacdBollingerStrategy().evaluate(bullish_bars) is None


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
    assert MacdBollingerStrategy().evaluate(bullish_bars) is None


def test_aggregate_requires_all_five_minutes():
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
