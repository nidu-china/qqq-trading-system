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
    settings = make_settings(
        volatility_filter_enabled=False,
        stale_minutes=400,
    )
    replay = EventDrivenBacktester(settings, strategy=OneSignalStrategy())
    result = replay.run(_day_bars(), {}, Decimal("10000"))
    assert result.signals == 1
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.quote_source == "synthetic"
    assert trade.pricing_model == "black_scholes_0dte"
    assert trade.entry_iv is not None
    assert trade.iv_source == "default"
    assert trade.entry_spread is not None
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
    assert result.trades[0].modeled_quote_bars == 0
    assert result.option_data_complete
    entry_point = next(
        point for point in result.equity_curve if point.timestamp == bars[15].end
    )
    assert entry_point.realized_pnl == 0
    assert entry_point.unrealized_pnl == Decimal("-40.00")
    assert entry_point.equity == Decimal("9960.00")


def test_replay_records_stop_trigger_fill_and_penetration():
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
    for index, bar in enumerate(bars[15:]):
        bid = Decimal("0.80") if index == 1 else Decimal("1.20")
        quote = Quote(
            contract.symbol,
            bar.end,
            bid,
            bid,
            bid + Decimal("0.01"),
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

    result = EventDrivenBacktester(
        settings,
        strategy=OneSignalStrategy(),
    ).run(bars, frames, Decimal("10000"))

    leg = result.trades[0].exit_legs[0]
    assert leg.reason == "stop_loss"
    assert leg.stop_price == Decimal("0.9075")
    assert leg.trigger_bid == Decimal("0.80")
    assert leg.fill_bid == Decimal("0.80")
    assert leg.stop_penetration == Decimal("0.1075")
    assert leg.stop_penetration_pct == Decimal("0.1075") / Decimal("1.21")


def test_replay_models_missing_minutes_then_returns_to_real_quotes():
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
            extra={"delta": "0.45", "iv": "0.25"},
        )
        frames[bar.end] = OptionFrame(
            bar.end,
            bar.close,
            (contract,),
            {contract.symbol: quote},
        )
    del frames[bars[16].end]

    replay = EventDrivenBacktester(settings, strategy=OneSignalStrategy())
    result = replay.run(bars, frames, Decimal("10000"))

    assert len(result.trades) == 1
    assert result.trades[0].quote_source == "real"
    assert result.trades[0].pricing_model == "black_scholes_0dte"
    assert result.trades[0].modeled_quote_bars == 1
    assert not result.option_data_complete
