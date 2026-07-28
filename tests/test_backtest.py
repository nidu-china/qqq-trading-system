from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from conftest import make_settings
from qqq_trader.backtest import EventDrivenBacktester, OptionFrame
from qqq_trader.domain import (
    Bar,
    Direction,
    MarketState,
    OptionContract,
    Quote,
    Signal,
)


class OneSignalStrategy:
    def __init__(self) -> None:
        self.sent = False
        self.last_context = None

    def evaluate(self, bars, spot=None):
        current = bars[-1]
        if self.sent or current.end.hour != 13 or current.end.minute != 46:
            return None
        self.sent = True
        return Signal(
            Direction.CALL,
            current.end,
            current.close,
            strategy="trend",
            market_state=MarketState.TREND_UP,
            stop_price=current.close - Decimal("0.5"),
            atr=Decimal("1"),
            vwap=current.close,
            indicators={"size_factor": "1"},
        )

    def bar_exit_decision(self, position):
        return None


def _day_bars() -> list[Bar]:
    start = datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc)
    bars = []
    for index in range(266):  # through the 13:55 ET close event
        price = Decimal("500") + Decimal(index) / Decimal("50")
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
            )
        )
    return bars


def test_synthetic_replay_aggregates_exit_legs_and_forces_close():
    settings = make_settings(volatility_filter_enabled=False)
    replay = EventDrivenBacktester(settings, strategy=OneSignalStrategy())
    result = replay.run(_day_bars(), {}, Decimal("10000"))
    assert result.signals == 1
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.quote_source == "synthetic"
    assert trade.strategy == "trend"
    assert trade.market_state is MarketState.TREND_UP
    assert trade.exit_reason == "forced_close"
    assert trade.exit_at.astimezone(ZoneInfo("America/New_York")).time().hour == 13
    assert sum(leg.quantity for leg in trade.exit_legs) == trade.quantity
    assert trade.fees == Decimal("3.00") * trade.quantity
    assert result.ending_equity == result.starting_equity + trade.pnl


def test_replay_cancel_returns_auditable_partial_result():
    settings = make_settings(volatility_filter_enabled=False)
    replay = EventDrivenBacktester(settings, strategy=OneSignalStrategy())
    calls = 0

    def cancel() -> bool:
        nonlocal calls
        calls += 1
        return calls > 30

    result = replay.run(_day_bars(), {}, Decimal("10000"), cancel_check=cancel)
    assert result.signals == 1
    assert len(result.signal_records) >= 1
    assert result.trades[0].exit_reason == "shutdown"


def test_replay_prefers_real_option_quotes_when_available():
    settings = make_settings(volatility_filter_enabled=False)
    bars = _day_bars()
    contract = OptionContract(
        "QQQ260715C00500000.US",
        "QQQ.US",
        date(2026, 7, 15),
        Decimal("500"),
        Direction.CALL,
    )
    frames = {}
    for bar in bars[15:]:
        quote = Quote(
            contract.symbol,
            bar.end,
            Decimal("1.20"),
            Decimal("1.20"),
            Decimal("1.21"),
            volume=100,
            open_interest=500,
            extra={"delta": "0.45"},
        )
        frames[bar.end] = OptionFrame(
            bar.end,
            bar.close,
            (contract,),
            {contract.symbol: quote},
        )
    replay = EventDrivenBacktester(settings, strategy=OneSignalStrategy())
    result = replay.run(bars, frames, Decimal("10000"))

    assert len(result.trades) == 1
    assert result.trades[0].quote_source == "real"
    assert result.option_data_complete
