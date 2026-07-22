from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from .domain import (
    AccountSnapshot,
    Bar,
    BrokerOrder,
    OptionContract,
    OrderRequest,
    Position,
    Quote,
    Signal,
    TradeSignal,
)


class MarketDataProvider(Protocol):
    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    async def subscribe(self, symbols: Sequence[str]) -> None: ...

    async def subscribe_candlesticks(self, symbols: Sequence[str], period: str = "1m") -> None: ...

    def realtime_bars(self, symbol: str, count: int = 500) -> list[Bar]: ...

    async def latest_quote(self, symbol: str) -> Quote: ...

    async def recent_bars(self, symbol: str, count: int = 500, period: str = "1m") -> list[Bar]: ...

    async def is_trading_day(self, trading_date: date) -> bool: ...

    async def preflight_options(self, underlying: str, trading_date: date) -> list[str]: ...

    async def option_chain(self, underlying: str, expiry: date) -> list[OptionContract]: ...

    async def historical_bars(
        self, symbol: str, start: date, end: date, period: str = "1m"
    ) -> list[Bar]: ...


class VolatilityDataProvider(Protocol):
    """Replaceable source for VIX now and an external VXN feed later."""

    async def recent_bars(self, symbol: str, count: int = 500, period: str = "1m") -> list[Bar]: ...

    async def historical_bars(
        self, symbol: str, start: date, end: date, period: str = "1m"
    ) -> list[Bar]: ...


class Broker(Protocol):
    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    async def preflight(self, account_id: str) -> list[str]:
        """Return blocking problem descriptions; an empty list means ready."""

    async def account_snapshot(self) -> AccountSnapshot: ...

    async def submit_limit(self, request: OrderRequest) -> BrokerOrder: ...

    async def cancel_order(self, order_id: str) -> None: ...

    async def order(self, order_id: str) -> BrokerOrder: ...

    async def positions(self) -> list[Position]: ...

    async def open_orders(self) -> list[BrokerOrder]: ...

    async def today_orders(self) -> list[BrokerOrder]:
        """Return all terminal orders placed today (filled, cancelled, rejected)."""
        return []


class Journal(Protocol):
    async def event(self, kind: str, message: str, details: dict | None = None) -> None: ...

    async def signal(self, signal: Signal, accepted: bool, reason: str = "") -> None: ...

    async def trade_signal(self, signal: TradeSignal) -> None: ...

    async def trade_signal_status(self, intent_id: UUID, status: str) -> None: ...

    async def trade_signal_by_intent(self, intent_id: UUID) -> TradeSignal | None: ...

    async def trade_signal_for_position(
        self, symbol: str, quantity: int
    ) -> TradeSignal | None: ...

    async def recover_trade_signal_statuses(self) -> dict[str, int]: ...

    async def order_intent(self, request: OrderRequest) -> None: ...

    async def broker_order(self, order: BrokerOrder) -> None: ...

    async def trade_summary(self, summary: dict) -> None: ...

    async def risk_snapshot(self, account: AccountSnapshot, halted: bool) -> None: ...

    async def today_realized_pnl_and_trades(self, trading_date: date) -> tuple[Decimal, int]: ...


QuoteSupplier = Callable[[str], Awaitable[Quote]]


class Clock(Protocol):
    def now(self) -> datetime: ...


class EquityProvider(Protocol):
    async def equity(self) -> Decimal: ...
