from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from conftest import make_settings

from qqq_trader.adapters.paper import PaperBroker
from qqq_trader.domain import (
    Bar,
    BrokerOrder,
    Direction,
    MarketState,
    OrderRequest,
    OrderSide,
    Position,
    Quote,
    Signal,
    SystemState,
    TradeSignal,
)
from qqq_trader.engine import TradingEngine
from qqq_trader.persistence import MemoryJournal


class UnusedMarket:
    async def connect(self):
        return None

    async def close(self):
        return None

    async def subscribe(self, symbols):
        return None

    async def latest_quote(self, symbol):
        quote = getattr(self, "quote", None)
        if quote is None:
            raise AssertionError("market quote was not expected")
        return quote


class FixedSignalStrategy:
    def __init__(self, signal: Signal) -> None:
        self.signal = signal
        self.last_state = signal.market_state

    def evaluate(self, bars):
        return self.signal

    def bar_exit_decision(self, position):
        return None


def _bar(end: datetime) -> Bar:
    return Bar(
        "QQQ.US",
        end - timedelta(minutes=1),
        end,
        Decimal("500"),
        Decimal("500.1"),
        Decimal("499.9"),
        Decimal("500"),
        1000,
    )


@pytest.mark.asyncio
async def test_engine_rejects_a_signal_older_than_sixty_seconds():
    now = datetime(2026, 7, 15, 14, 47, 1, tzinfo=timezone.utc)
    signal = Signal(
        Direction.CALL,
        now - timedelta(seconds=121),
        Decimal("500"),
        strategy="trend",
        market_state=MarketState.TREND_UP,
        stop_price=Decimal("499"),
        atr=Decimal("1"),
    )
    journal = MemoryJournal()
    engine = TradingEngine(
        make_settings(volatility_filter_enabled=False),
        UnusedMarket(),
        PaperBroker(),
        journal,
    )
    engine.strategy = FixedSignalStrategy(signal)
    engine.state = SystemState.READY
    engine.opening_equity = Decimal("10000")

    await engine.on_completed_bars([_bar(now)], now=now)

    assert journal.signals[-1]["reason"] == "signal_expired"
    assert engine.position is None


@pytest.mark.asyncio
async def test_configuration_is_staged_until_an_open_position_is_flat():
    settings = make_settings()
    engine = TradingEngine(settings, UnusedMarket(), PaperBroker(), MemoryJournal())
    engine.state = SystemState.OPEN
    engine.position = Position(
        "QQQ260715C00500000.US",
        Direction.CALL,
        1,
        Decimal("1"),
        datetime.now(timezone.utc),
    )
    changed = make_settings(timed_boll_stddev="2.5")

    applied = await engine.apply_settings(changed, 2)

    assert not applied
    assert engine.pending_config_version == 2
    assert engine.settings.timed_boll_stddev == Decimal("2")


@pytest.mark.asyncio
async def test_paper_broker_adds_to_position_with_weighted_average_cost():
    broker = PaperBroker()
    symbol = "QQQ260715C00500000.US"
    await broker.submit_limit(
        OrderRequest(symbol, OrderSide.BUY, 2, Decimal("1"))
    )
    await broker.submit_limit(
        OrderRequest(symbol, OrderSide.BUY, 3, Decimal("2"))
    )

    positions = await broker.positions()

    assert len(positions) == 1
    assert positions[0].quantity == 5
    assert positions[0].entry_price == Decimal("1.6")


@pytest.mark.asyncio
async def test_position_strategy_metadata_survives_startup_recovery():
    journal = MemoryJournal()
    broker = PaperBroker()
    request = OrderRequest(
        "QQQ260715C00500000.US",
        OrderSide.BUY,
        2,
        Decimal("1"),
    )
    order = await broker.submit_limit(request)
    await journal.broker_order(order)
    signal = TradeSignal(
        request.intent_id,
        order.submitted_at,
        OrderSide.BUY,
        Direction.CALL,
        request.symbol,
        Decimal("1"),
        2,
        "entry_range",
        {
            "strategy": "range",
            "market_state": "range",
            "spot": "500",
            "highest_bid": "1.80",
            "range_middle_taken": True,
            "first_target_taken": True,
            "stop_price": "1.00",
            "entry_vwap": "500.1",
            "macd_reversal_pending": True,
            "macd_reversal_pending_at": order.submitted_at.isoformat(),
        },
    )
    await journal.trade_signal(signal)
    await journal.trade_signal_status(signal.intent_id, "executed")
    engine = TradingEngine(make_settings(), UnusedMarket(), broker, journal)

    problems = await engine._recover_broker_state(await broker.positions(), [])

    assert not problems
    assert engine.position is not None
    assert engine.position.market_state is MarketState.RANGE
    assert engine.position.highest_bid == Decimal("1.80")
    assert engine.position.range_middle_taken
    assert engine.position.stop_price == Decimal("1.00")
    assert engine.position.macd_reversal_pending
    assert engine.position.macd_reversal_pending_at == order.submitted_at


@pytest.mark.asyncio
async def test_engine_waits_for_completed_bar_before_emergency_stop_exit():
    class RecordingExecutor:
        def __init__(self):
            self.emergency_requests = []

        async def emergency_exit(self, request, quote_supplier):
            self.emergency_requests.append(request)
            return BrokerOrder(
                "market-stop",
                request.intent_id,
                request.symbol,
                request.side,
                request.quantity,
                request.quantity,
                Decimal("0.73"),
                "filled",
                datetime.now(timezone.utc),
            )

        async def exit(self, request, quote_supplier):
            raise AssertionError("hard stop must not use ordinary limit exit")

    now = datetime.now(timezone.utc)
    market = UnusedMarket()
    engine = TradingEngine(
        make_settings(volatility_filter_enabled=False),
        market,
        PaperBroker(),
        MemoryJournal(),
    )
    engine.position = Position(
        "QQQ260715C00500000.US",
        Direction.CALL,
        2,
        Decimal("1.00"),
        now - timedelta(minutes=1),
        stop_price=Decimal("0.75"),
    )
    engine.state = SystemState.OPEN
    executor = RecordingExecutor()
    engine.executor = executor

    quote = Quote(
        engine.position.symbol,
        now,
        Decimal("0.74"),
        Decimal("0.74"),
        Decimal("0.76"),
    )
    market.quote = quote

    await engine.on_position_quote(quote, now)

    assert not executor.emergency_requests
    assert engine.position is not None

    engine.strategy = FixedSignalStrategy(
        Signal(Direction.CALL, now, Decimal("500"), strategy="test")
    )
    await engine.on_completed_bars([_bar(now)], now=now)

    assert len(executor.emergency_requests) == 1
    assert executor.emergency_requests[0].reason == "stop_loss"
    assert engine.position is None
