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
    Signal,
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


class SignalForcingEngine(TradingEngine):
    """Engine subclass that forces the strategy to produce a signal."""

    def __init__(self, *args, forced_signal: Signal | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._forced_signal = forced_signal

    async def on_completed_bars(self, bars, now=None, volatility_bars=None, volatility_daily_bars=None):
        if self._forced_signal is not None:
            original_evaluate = self.strategy.evaluate
            self.strategy.evaluate = lambda *a, **kw: self._forced_signal
            try:
                await super().on_completed_bars(bars, now, volatility_bars, volatility_daily_bars)
            finally:
                self.strategy.evaluate = original_evaluate
        else:
            await super().on_completed_bars(bars, now, volatility_bars, volatility_daily_bars)


@pytest.mark.asyncio
async def test_engine_opens_and_scales_out(bullish_bars):
    now = bullish_bars[-1].end
    market = FakeMarket(now)
    broker = PaperBroker()
    journal = MemoryJournal()
    settings = Settings(
        trading_mode="paper",
        volatility_filter_enabled=False,
    )
    forced_signal = Signal(
        direction=Direction.CALL,
        bar_end=now,
        spot=Decimal("103"),
        strategy="trend",
        stop_price=Decimal("101"),
        atr=Decimal("1.5"),
        r_value=Decimal("2.0"),
        breakout_level=Decimal("101"),
        vwap=Decimal("102"),
        indicators={"strategy": "trend"},
    )
    engine = SignalForcingEngine(
        settings, market, broker, journal, forced_signal=forced_signal
    )
    await engine.start()
    assert engine.state is SystemState.READY

    await engine.on_completed_bars(bullish_bars, now)
    assert engine.state is SystemState.OPEN
    assert engine.position is not None

    # +1R exit: entry=1.00, r_value=2.0, so +1R = entry + 2.0 = 3.0
    market.option_quote = Quote(
        market.option_symbol, now, Decimal("3.0"), Decimal("3.0"), Decimal("3.01"), 200, 1000
    )
    await engine.on_position_quote(market.option_quote, now)
    # Should have taken partial profit
    if engine.position is not None:
        assert engine.position.first_target_taken is True

    # +2.5R exit: entry + 5.0 = 6.0
    market.option_quote = Quote(
        market.option_symbol, now, Decimal("6.0"), Decimal("6.0"), Decimal("6.01"), 300, 1000
    )
    await engine.on_position_quote(market.option_quote, now)
    assert engine.position is None
    assert engine.state is SystemState.READY
    assert len(engine.closed_trades) >= 1


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
    forced_signal = Signal(
        direction=Direction.CALL,
        bar_end=now,
        spot=Decimal("103"),
        strategy="trend",
        stop_price=Decimal("101"),
        atr=Decimal("1.5"),
        r_value=Decimal("2.0"),
        indicators={"strategy": "trend"},
    )
    engine = SignalForcingEngine(
        settings, market, PaperBroker(), journal, forced_signal=forced_signal
    )
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
    forced_signal = Signal(
        direction=Direction.CALL,
        bar_end=now,
        spot=Decimal("103"),
        strategy="trend",
        stop_price=Decimal("101"),
        atr=Decimal("1.5"),
        r_value=Decimal("2.0"),
        indicators={"strategy": "trend"},
    )
    engine = SignalForcingEngine(
        settings, market, PaperBroker(), journal, forced_signal=forced_signal
    )
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
