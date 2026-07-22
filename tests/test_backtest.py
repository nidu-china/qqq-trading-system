from __future__ import annotations

from datetime import date, time
from decimal import Decimal

from qqq_trader.backtest import EventDrivenBacktester, OptionFrame
from qqq_trader.config import Settings
from qqq_trader.domain import Bar, Direction, OptionContract, Quote
from qqq_trader.risk import ContractSelector, RiskEngine
from qqq_trader.strategy import MacdBollingerStrategy


def test_full_option_frame_backtest_uses_ask_then_executable_bid(bullish_bars):
    settings = Settings(
        trading_mode="replay", entry_start=time(9, 45), volatility_filter_enabled=False
    )
    entry_at = bullish_bars[-1].end
    contract = OptionContract(
        "QQQ260715C105000.US",
        "QQQ.US",
        date(2026, 7, 15),
        Decimal("105"),
        Direction.CALL,
    )
    entry_quote = Quote(
        contract.symbol,
        entry_at,
        Decimal("1"),
        Decimal("0.99"),
        Decimal("1"),
        100,
        1000,
    )
    prior = bullish_bars[-1]
    exit_bar = Bar(
        prior.symbol,
        prior.end,
        prior.end + (prior.end - prior.start),
        prior.close,
        prior.close + Decimal("0.1"),
        prior.close - Decimal("0.1"),
        prior.close,
        1000,
    )
    exit_quote = Quote(
        contract.symbol,
        exit_bar.end,
        Decimal("1.5"),
        Decimal("1.5"),
        Decimal("1.51"),
        200,
        1000,
    )
    frames = {
        entry_at: OptionFrame(
            entry_at, Decimal("103"), (contract,), {contract.symbol: entry_quote}
        ),
        exit_bar.end: OptionFrame(
            exit_bar.end, Decimal("103"), (contract,), {contract.symbol: exit_quote}
        ),
    }
    result = EventDrivenBacktester(
        settings,
        MacdBollingerStrategy(),
        ContractSelector(),
        RiskEngine(settings),
    ).run([*bullish_bars, exit_bar], frames, Decimal("100000"))
    assert result.option_data_complete
    assert len(result.trades) >= 1
    assert result.trades[0].entry_price == Decimal("1")
    assert result.trades[0].exit_price == Decimal("1.5")
    assert result.ending_equity > result.starting_equity
    if len(result.trades) == 2:
        assert result.trades[1].reason == "backtest_end"
    assert result.signal_records[0]["action"] == "buy"
    assert result.signal_records[0]["status"] == "accepted"
    assert result.signal_records[0]["indicators"]
