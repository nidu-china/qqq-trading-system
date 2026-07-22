from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from qqq_trader.adapters.paper import PaperBroker
from qqq_trader.config import Settings
from qqq_trader.domain import (
    Bar,
    BrokerOrder,
    Direction,
    OptionContract,
    OrderRequest,
    OrderSide,
    Quote,
    SystemState,
    TradeSignal,
)
from qqq_trader.engine import TradingEngine
from qqq_trader.persistence import MemoryJournal


class FakeMarket:
    def __init__(self, now: datetime):
        self.now = now
        self.option_chain_calls = 0
        self.option_symbol = "QQQ260715C105000.US"
        self.option_quote = Quote(
            self.option_symbol,
            now,
            Decimal("1"),
            Decimal("0.99"),
            Decimal("1.00"),
            100,
            1000,
        )

    async def connect(self):
        pass

    async def close(self):
        pass

    async def subscribe(self, symbols):
        pass

    async def recent_bars(self, symbol, count=500, period="1m"):
        return []

    async def historical_bars(self, symbol, start, end, period="1m"):
        return []

    async def latest_quote(self, symbol):
        if symbol == "QQQ.US":
            return Quote(symbol, self.now, Decimal("103"), volume=1000)
        return self.option_quote

    async def option_chain(self, underlying, expiry):
        self.option_chain_calls += 1
        return [
            OptionContract(
                self.option_symbol,
                underlying,
                expiry,
                Decimal("105"),
                Direction.CALL,
            )
        ]


class PendingPaperBroker(PaperBroker):
    def __init__(self, request: OrderRequest, now: datetime):
        super().__init__()
        self.pending = BrokerOrder(
            order_id="pending-order",
            intent_id=request.intent_id,
            symbol=request.symbol,
            side=request.side,
            quantity=request.quantity,
            filled_quantity=0,
            average_price=None,
            status="pending",
            submitted_at=now,
        )

    async def open_orders(self):
        return [self.pending] if self.pending.status == "pending" else []

    async def cancel_order(self, order_id):
        self.pending = BrokerOrder(
            order_id=self.pending.order_id,
            intent_id=self.pending.intent_id,
            symbol=self.pending.symbol,
            side=self.pending.side,
            quantity=self.pending.quantity,
            filled_quantity=0,
            average_price=None,
            status="canceled",
            submitted_at=self.pending.submitted_at,
        )

    async def order(self, order_id):
        return self.pending


@pytest.mark.asyncio
async def test_engine_opens_and_scales_out(bullish_bars):
    now = bullish_bars[-1].end
    market = FakeMarket(now)
    broker = PaperBroker()
    journal = MemoryJournal()
    settings = Settings(
        trading_mode="paper",
        min_option_volume=10,
        min_open_interest=100,
        volatility_filter_enabled=False,
    )
    engine = TradingEngine(settings, market, broker, journal)
    await engine.start()
    assert engine.state is SystemState.READY

    await engine.on_completed_bars(bullish_bars, now)
    assert engine.state is SystemState.OPEN
    assert engine.position is not None and engine.position.quantity == 10

    market.option_quote = Quote(
        market.option_symbol, now, Decimal("1.5"), Decimal("1.5"), Decimal("1.51"), 200, 1000
    )
    await engine.on_position_quote(market.option_quote, now)
    assert engine.position is not None and engine.position.quantity == 5
    assert engine.position.stop_price == Decimal("1.00")

    market.option_quote = Quote(
        market.option_symbol, now, Decimal("2"), Decimal("2"), Decimal("2.01"), 300, 1000
    )
    await engine.on_position_quote(market.option_quote, now)
    assert engine.position is None
    assert engine.state is SystemState.READY
    assert len(engine.closed_trades) == 2
    assert len(journal.trade_summaries) == 2
    assert [item["signal"].action.value for item in journal.trade_signals] == [
        "buy",
        "sell",
        "sell",
    ]
    assert all(item["status"] == "executed" for item in journal.trade_signals)
    for item in journal.trade_signals:
        signal_step = ("trade_signal", item["signal"].intent_id)
        order_step = ("order_intent", item["signal"].intent_id)
        assert journal.timeline.index(signal_step) < journal.timeline.index(order_step)


@pytest.mark.asyncio
async def test_live_mode_requires_exact_acknowledgement():
    now = datetime.now(timezone.utc)
    engine = TradingEngine(
        Settings(trading_mode="live", account_id="abc", longbridge_client_id="client"),
        FakeMarket(now),
        PaperBroker(),
        MemoryJournal(),
    )
    await engine.start()
    assert engine.state is SystemState.HALTED
    assert "acknowledgement" in (engine.last_error or "")


@pytest.mark.asyncio
async def test_startup_adopts_position_matching_persisted_buy_signal(bullish_bars):
    now = bullish_bars[-1].end
    market = FakeMarket(now)
    broker = PaperBroker()
    journal = MemoryJournal()
    request = OrderRequest(
        symbol=market.option_symbol,
        side=OrderSide.BUY,
        quantity=4,
        limit_price=Decimal("1"),
        reason="entry_call",
    )
    await journal.trade_signal(
        TradeSignal(
            intent_id=request.intent_id,
            decision_at=now,
            action=OrderSide.BUY,
            direction=Direction.CALL,
            symbol=request.symbol,
            reference_price=request.limit_price,
            quantity=request.quantity,
            reason=request.reason,
        )
    )
    await broker.submit_limit(request)

    engine = TradingEngine(
        Settings(trading_mode="paper", volatility_filter_enabled=False),
        market,
        broker,
        journal,
    )
    await engine.start()

    assert engine.state is SystemState.OPEN
    assert engine.position is not None
    assert engine.position.symbol == request.symbol
    assert engine.position.quantity == request.quantity
    assert journal.trade_signals[0]["status"] == "executed"
    assert any(event["kind"] == "startup_recovered" for event in journal.events)


@pytest.mark.asyncio
async def test_startup_rejects_same_symbol_position_without_matching_net_fills(bullish_bars):
    now = bullish_bars[-1].end
    market = FakeMarket(now)
    broker = PaperBroker()
    journal = MemoryJournal()
    historical = OrderRequest(
        symbol=market.option_symbol,
        side=OrderSide.BUY,
        quantity=1,
        limit_price=Decimal("1"),
        reason="entry_call",
    )
    await journal.trade_signal(
        TradeSignal(
            intent_id=historical.intent_id,
            decision_at=now - timedelta(minutes=5),
            action=OrderSide.BUY,
            direction=Direction.CALL,
            symbol=historical.symbol,
            reference_price=historical.limit_price,
            quantity=historical.quantity,
            reason=historical.reason,
        )
    )
    await journal.trade_signal_status(historical.intent_id, "executed")
    manual = OrderRequest(
        symbol=market.option_symbol,
        side=OrderSide.BUY,
        quantity=1,
        limit_price=Decimal("1"),
        reason="manual",
    )
    await broker.submit_limit(manual)
    engine = TradingEngine(
        Settings(trading_mode="paper", volatility_filter_enabled=False),
        market,
        broker,
        journal,
    )

    await engine.start()

    assert engine.state is SystemState.HALTED
    assert "unmatched broker position" in (engine.last_error or "")


@pytest.mark.asyncio
async def test_startup_marks_signal_without_order_or_position_failed(bullish_bars):
    now = bullish_bars[-1].end
    market = FakeMarket(now)
    journal = MemoryJournal()
    request = OrderRequest(
        symbol=market.option_symbol,
        side=OrderSide.BUY,
        quantity=1,
        limit_price=Decimal("1"),
        reason="entry_call",
    )
    await journal.trade_signal(
        TradeSignal(
            intent_id=request.intent_id,
            decision_at=now,
            action=OrderSide.BUY,
            direction=Direction.CALL,
            symbol=request.symbol,
            reference_price=request.limit_price,
            quantity=request.quantity,
            reason=request.reason,
        )
    )
    engine = TradingEngine(
        Settings(trading_mode="paper", volatility_filter_enabled=False),
        market,
        PaperBroker(),
        journal,
    )

    await engine.start()

    assert engine.state is SystemState.READY
    assert journal.trade_signals[0]["status"] == "failed"


@pytest.mark.asyncio
async def test_startup_cancels_matching_open_order_and_marks_signal_failed(bullish_bars):
    now = bullish_bars[-1].end
    market = FakeMarket(now)
    journal = MemoryJournal()
    request = OrderRequest(
        symbol=market.option_symbol,
        side=OrderSide.BUY,
        quantity=2,
        limit_price=Decimal("1"),
        reason="entry_call",
    )
    await journal.trade_signal(
        TradeSignal(
            intent_id=request.intent_id,
            decision_at=now,
            action=OrderSide.BUY,
            direction=Direction.CALL,
            symbol=request.symbol,
            reference_price=request.limit_price,
            quantity=request.quantity,
            reason=request.reason,
        )
    )
    broker = PendingPaperBroker(request, now)
    engine = TradingEngine(
        Settings(trading_mode="paper", volatility_filter_enabled=False),
        market,
        broker,
        journal,
    )

    await engine.start()

    assert engine.state is SystemState.READY
    assert broker.pending.status == "canceled"
    assert journal.trade_signals[0]["status"] == "failed"


@pytest.mark.asyncio
async def test_risk_off_volatility_blocks_call_signal(bullish_bars):
    now = bullish_bars[-1].end
    market = FakeMarket(now)
    journal = MemoryJournal()
    settings = Settings(trading_mode="paper")
    engine = TradingEngine(settings, market, PaperBroker(), journal)
    await engine.start()

    daily = []
    for index in range(20):
        start = now - timedelta(days=30 - index)
        value = Decimal(10 + index)
        daily.append(
            Bar(
                ".VIX.US",
                start,
                start + timedelta(days=1),
                value,
                value,
                value,
                value,
                0,
            )
        )
    intraday = []
    for minutes, value in ((15, "33"), (5, "34"), (0, "35")):
        end = now - timedelta(minutes=minutes)
        price = Decimal(value)
        intraday.append(
            Bar(
                ".VIX.US",
                end - timedelta(minutes=5),
                end,
                price,
                price,
                price,
                price,
                0,
            )
        )

    await engine.on_completed_bars(bullish_bars, now, intraday, daily)
    assert engine.state is SystemState.READY
    assert engine.position is None
    assert journal.signals[-1]["reason"] == "volatility_risk_off"


@pytest.mark.asyncio
async def test_paper_publishes_buy_signal_before_order_and_executes(bullish_bars):
    now = bullish_bars[-1].end
    market = FakeMarket(now)
    journal = MemoryJournal()
    settings = Settings(
        trading_mode="paper",
        volatility_filter_enabled=False,
    )
    engine = TradingEngine(settings, market, PaperBroker(), journal)
    await engine.start()

    await engine.on_completed_bars(bullish_bars, now)

    assert engine.state is SystemState.OPEN
    assert engine.position is not None
    assert market.option_chain_calls == 1
    assert journal.intents and journal.orders
    assert journal.signals[-1]["accepted"] is True
    trade_signal = journal.trade_signals[-1]
    assert trade_signal["signal"].action.value == "buy"
    assert trade_signal["status"] == "executed"
    assert journal.timeline[0][0] == "trade_signal"
    assert journal.timeline[1][0] == "order_intent"
    assert journal.timeline[0][1] == journal.timeline[1][1]
    event = next(item for item in journal.events if item["kind"] == "buy_signal")
    assert event["details"]["direction"] == "call"


@pytest.mark.asyncio
async def test_configuration_update_is_deferred_until_existing_position_closes(bullish_bars):
    now = bullish_bars[-1].end
    market = FakeMarket(now)
    journal = MemoryJournal()
    initial = Settings(
        trading_mode="paper",
        volatility_filter_enabled=False,
    )
    engine = TradingEngine(initial, market, PaperBroker(), journal)
    await engine.start()
    assert await engine.apply_settings(initial, 1) is True

    await engine.on_completed_bars(bullish_bars, now)
    assert engine.position is not None
    assert engine.position_config_version == 1

    updated = initial.model_copy(update={"stop_loss_pct": Decimal("0.40")})
    assert await engine.apply_settings(updated, 2) is False
    assert engine.config_version == 1
    assert engine.pending_config_version == 2
    assert engine.settings.stop_loss_pct == Decimal("0.25")

    market.option_quote = Quote(
        market.option_symbol,
        now,
        Decimal("2"),
        Decimal("2"),
        Decimal("2.01"),
        300,
        1000,
    )
    await engine.on_position_quote(market.option_quote, now)
    await engine.on_position_quote(market.option_quote, now)

    assert engine.position is None
    assert engine.config_version == 2
    assert engine.pending_config_version is None
    assert engine.settings.stop_loss_pct == Decimal("0.40")
