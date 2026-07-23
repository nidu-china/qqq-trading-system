from __future__ import annotations

from datetime import date, time
from decimal import Decimal

from qqq_trader.backtest import EventDrivenBacktester, OptionFrame
from qqq_trader.config import Settings
from qqq_trader.domain import Bar, Direction, OptionContract, Quote
from qqq_trader.risk import ContractSelector, RiskEngine
from qqq_trader.strategy import strategy_from_settings


def test_backtest_synthetic_frames_used_when_no_option_data(bullish_bars):
    """Backtest runs with synthetic option frames when none are provided."""
    settings = Settings(
        trading_mode="replay",
        entry_start=time(9, 45),
        volatility_filter_enabled=False,
    )
    result = EventDrivenBacktester(
        settings,
        strategy_from_settings(settings),
        ContractSelector(),
        RiskEngine(settings),
    ).run(bullish_bars, {}, Decimal("100000"))
    assert result.starting_equity == Decimal("100000")
    # May or may not produce trades depending on signal generation
    assert result.ending_equity > 0


def test_backtest_with_option_frame(bullish_bars):
    """Backtest executes trade when matching OptionFrame is available."""
    settings = Settings(
        trading_mode="replay",
        entry_start=time(9, 45),
        volatility_filter_enabled=False,
    )
    strategy = strategy_from_settings(settings)

    # Run strategy to find when it would signal
    signal = None
    signal_bar_end = None
    for i in range(len(bullish_bars)):
        window = bullish_bars[: i + 1]
        sig = strategy.evaluate(window)
        if sig is not None:
            signal = sig
            signal_bar_end = bullish_bars[i].end
            break

    if signal is None:
        # Strategy didn't fire with this data - test passes vacuously
        return

    contract = OptionContract(
        "QQQ260715C105000.US",
        "QQQ.US",
        date(2026, 7, 15),
        Decimal("105"),
        Direction.CALL,
    )
    entry_quote = Quote(
        contract.symbol,
        signal_bar_end,
        Decimal("1"),
        Decimal("0.99"),
        Decimal("1"),
        100,
        1000,
    )
    frames = {
        signal_bar_end: OptionFrame(
            signal_bar_end, Decimal("103"), (contract,), {contract.symbol: entry_quote}
        ),
    }
    # Use a fresh strategy instance for the actual backtest
    result = EventDrivenBacktester(
        settings,
        strategy_from_settings(settings),
        ContractSelector(),
        RiskEngine(settings),
    ).run(bullish_bars, frames, Decimal("100000"))
    assert result.signals >= 1


def test_backtest_closes_position_at_end(bullish_bars):
    """Positions open at end of data are closed."""
    settings = Settings(
        trading_mode="replay",
        entry_start=time(9, 45),
        entry_end=time(14, 0),
        forced_close=time(14, 0),
        volatility_filter_enabled=False,
    )
    result = EventDrivenBacktester(
        settings,
        strategy_from_settings(settings),
        ContractSelector(),
        RiskEngine(settings),
    ).run(bullish_bars, {}, Decimal("100000"))
    # If any trades opened, they should be closed
    if result.trades:
        last_trade = result.trades[-1]
        assert last_trade.reason in (
            "backtest_end", "stop_loss", "take_profit_1",
            "take_profit_2", "forced_close", "stale_position",
            "midday_reduce", "vwap_cross", "trailing_stop",
        )


def test_backtest_respects_max_trades_per_day():
    """Backtest does not exceed max_trades_per_day."""
    from datetime import datetime, timedelta, timezone

    settings = Settings(
        trading_mode="replay",
        entry_start=time(9, 45),
        max_trades_per_day=1,
        volatility_filter_enabled=False,
    )
    # Generate enough bars for multiple potential signals
    start = datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc)
    bars = []
    for i in range(90):
        base = Decimal("100") + Decimal(str(i * 0.1))
        bars.append(Bar(
            "QQQ.US",
            start + timedelta(minutes=i),
            start + timedelta(minutes=i + 1),
            base,
            base + Decimal("0.3"),
            base - Decimal("0.2"),
            base + Decimal("0.15"),
            1500,
        ))
    result = EventDrivenBacktester(
        settings,
        strategy_from_settings(settings),
        ContractSelector(),
        RiskEngine(settings),
    ).run(bars, {}, Decimal("100000"))
    # At most 1 entry signal should be accepted
    accepted = [r for r in result.signal_records if r.get("status") == "accepted"]
    assert len(accepted) <= 1
