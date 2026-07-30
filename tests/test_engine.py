from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from conftest import make_settings

from qqq_trader.adapters.paper import PaperBroker
from qqq_trader.domain import (
    Bar,
    Direction,
    MarketState,
    OrderRequest,
    OrderSide,
    Position,
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
        now - timedelta(seconds=61),
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
    changed = make_settings(bollinger_stddev="2.5")

    applied = await engine.apply_settings(changed, 2)

    assert not applied
    assert engine.pending_config_version == 2
    assert engine.settings.bollinger_stddev == Decimal("2")


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
            "underlying_stop": "499.5",
            "highest_bid": "1.80",
            "midday_reduced": True,
            "range_middle_taken": True,
            "first_target_taken": True,
            "stop_price": "1.00",
            "entry_vwap": "500.1",
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
    assert engine.position.midday_reduced
    assert engine.position.range_middle_taken
    assert engine.position.stop_price == Decimal("1.00")
