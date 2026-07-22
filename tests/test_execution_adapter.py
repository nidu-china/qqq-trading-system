from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from qqq_trader.adapters.longbridge import (
    LongbridgeBroker,
    LongbridgeMarketData,
    LongbridgeSession,
)
from qqq_trader.config import Settings
from qqq_trader.domain import BrokerOrder, OrderRequest, OrderSide, Quote
from qqq_trader.execution import OrderExecutor
from qqq_trader.persistence import MemoryJournal


class PartialBroker:
    def __init__(self):
        self.requests = []
        self.orders = {}

    async def submit_limit(self, request):
        self.requests.append(request)
        fill = min(2, request.quantity)
        order = BrokerOrder(
            str(len(self.requests)),
            request.intent_id,
            request.symbol,
            request.side,
            request.quantity,
            fill,
            request.limit_price,
            "canceled",
            datetime.now(timezone.utc),
        )
        self.orders[order.order_id] = order
        return order

    async def order(self, order_id):
        return self.orders[order_id]

    async def cancel_order(self, order_id):
        pass


@pytest.mark.asyncio
async def test_repricing_only_submits_unfilled_remainder():
    broker = PartialBroker()
    settings = Settings(entry_reprices=2)
    executor = OrderExecutor(broker, MemoryJournal(), settings)
    now = datetime.now(timezone.utc)

    async def quote_supplier(symbol):
        return Quote(symbol, now, Decimal("1.01"), Decimal("1"), Decimal("1.01"), 100, 100)

    result = await executor.entry(
        OrderRequest("OPT.US", OrderSide.BUY, 4, Decimal("1")), quote_supplier
    )
    assert result is not None and result.filled_quantity == 4
    assert [request.quantity for request in broker.requests] == [4, 2]


class FakeQuoteContext:
    def __init__(self):
        self.candlestick_args = None
        self.subscribe_args = None

    def set_on_quote(self, callback):
        self.quote_callback = callback

    def set_on_depth(self, callback):
        self.depth_callback = callback

    def set_on_candlestick(self, callback):
        self.candlestick_callback = callback

    async def subscribe(self, symbols, subtypes):
        self.subscribe_args = (symbols, subtypes)

    async def subscribe_candlesticks(self, symbol, period, trade_sessions):
        return []

    async def quote(self, symbols):
        return [
            SimpleNamespace(
                symbol=symbols[0],
                timestamp=datetime.now(timezone.utc),
                last_done=Decimal("100"),
                volume=100,
            )
        ]

    async def depth(self, symbol):
        return SimpleNamespace(
            bids=[SimpleNamespace(price=Decimal("99.99"))],
            asks=[SimpleNamespace(price=Decimal("100.01"))],
        )

    async def candlesticks(self, *args):
        self.candlestick_args = args
        return []


class FakeTradeContext:
    def __init__(self):
        self.submit_args = None

    async def submit_order(self, *args):
        self.submit_args = args
        return SimpleNamespace(order_id="123")

    async def order_detail(self, order_id):
        return SimpleNamespace(
            order_id=order_id,
            status=SimpleNamespace(name="Filled"),
            remark=str(self.submit_args[-1]),
            symbol=self.submit_args[0],
            side=SimpleNamespace(name="Buy"),
            quantity=Decimal("1"),
            executed_quantity=Decimal("1"),
            executed_price=Decimal("1.00"),
            submitted_at=datetime.now(timezone.utc),
        )


@pytest.mark.asyncio
async def test_longbridge_adapter_matches_v4_positional_signatures():
    settings = Settings()
    session = LongbridgeSession(settings)
    session.quote = FakeQuoteContext()
    session.trade = FakeTradeContext()
    market = LongbridgeMarketData(session)
    await market.connect()
    await market.subscribe(["QQQ.US"])
    await market.subscribe_candlesticks(["QQQ.US", ".VIX.US"], "1m")
    assert market._candlestick_periods == {"QQQ.US": "1m", ".VIX.US": "1m"}
    await market.recent_bars("QQQ.US", 5, "1m")
    assert len(session.quote.candlestick_args) == 5

    broker = LongbridgeBroker(session, settings)
    request = OrderRequest("QQQ260715C100000.US", OrderSide.BUY, 1, Decimal("1"))
    order = await broker.submit_limit(request)
    assert order.filled_quantity == 1
    assert len(session.trade.submit_args) == 16
    assert session.trade.submit_args[-1] == str(request.intent_id)


@pytest.mark.asyncio
async def test_longbridge_history_request_has_an_application_timeout():
    class SlowHistoryQuote:
        async def history_candlesticks_by_date(self, *args):
            await asyncio.sleep(1)
            return []

    settings = Settings(_env_file=None, longbridge_request_timeout_seconds="0.01")
    session = LongbridgeSession(settings)
    session.quote = SlowHistoryQuote()
    market = LongbridgeMarketData(session)

    with pytest.raises(RuntimeError, match="history request timed out"):
        await market.historical_bars("QQQ.US", date(2026, 7, 15), date(2026, 7, 15), "1m")


@pytest.mark.asyncio
async def test_longbridge_history_is_requested_one_day_at_a_time():
    class RecordingHistoryQuote:
        def __init__(self):
            self.ranges = []

        async def history_candlesticks_by_date(self, symbol, period, adjust, start, end, sessions):
            self.ranges.append((start, end))
            return []

    settings = Settings(_env_file=None)
    session = LongbridgeSession(settings)
    session.quote = RecordingHistoryQuote()
    market = LongbridgeMarketData(session)

    assert not await market.historical_bars("QQQ.US", date(2026, 7, 15), date(2026, 7, 17), "1m")
    assert session.quote.ranges == [
        (date(2026, 7, 15), date(2026, 7, 15)),
        (date(2026, 7, 16), date(2026, 7, 16)),
        (date(2026, 7, 17), date(2026, 7, 17)),
    ]
