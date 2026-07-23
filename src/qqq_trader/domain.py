from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

ZERO = Decimal("0")


class Direction(StrEnum):
    CALL = "call"
    PUT = "put"


class OrderSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class TradingMode(StrEnum):
    PAPER = "paper"
    LIVE = "live"
    REPLAY = "replay"


class SystemState(StrEnum):
    STARTING = "starting"
    READY = "ready"
    ENTRY_PENDING = "entry_pending"
    OPEN = "open"
    EXIT_PENDING = "exit_pending"
    HALTED = "halted"


class ExitReason(StrEnum):
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT_1 = "take_profit_1"
    TAKE_PROFIT_2 = "take_profit_2"
    TRAILING_STOP = "trailing_stop"
    STALE_POSITION = "stale_position"
    MIDDAY_REDUCE = "midday_reduce"
    VWAP_CROSS = "vwap_cross"
    DAILY_LOSS = "daily_loss"
    FORCED_CLOSE = "forced_close"
    SHUTDOWN = "shutdown"


class MarketState(StrEnum):
    OBSERVATION = "observation"
    TREND = "trend"
    REVERSAL = "reversal"
    RANGE = "range"


def _aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")


@dataclass(frozen=True, slots=True)
class Bar:
    symbol: str
    start: datetime
    end: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    turnover: Decimal = ZERO
    complete: bool = True

    def __post_init__(self) -> None:
        _aware(self.start)
        _aware(self.end)
        if self.end <= self.start:
            raise ValueError("bar end must be after start")
        if min(self.open, self.high, self.low, self.close) <= ZERO:
            raise ValueError("bar prices must be positive")
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise ValueError("invalid OHLC range")
        if self.volume < 0:
            raise ValueError("volume cannot be negative")


@dataclass(frozen=True, slots=True)
class Quote:
    symbol: str
    timestamp: datetime
    last: Decimal
    bid: Decimal | None = None
    ask: Decimal | None = None
    volume: int = 0
    open_interest: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _aware(self.timestamp)
        if self.last < ZERO:
            raise ValueError("last price cannot be negative")

    @property
    def mid(self) -> Decimal | None:
        if self.bid is None or self.ask is None or self.bid <= ZERO or self.ask <= ZERO:
            return None
        return (self.bid + self.ask) / Decimal(2)

    @property
    def spread(self) -> Decimal | None:
        if self.bid is None or self.ask is None:
            return None
        return self.ask - self.bid


@dataclass(frozen=True, slots=True)
class OptionContract:
    symbol: str
    underlying: str
    expiry: date
    strike: Decimal
    right: Direction

    def __post_init__(self) -> None:
        if self.strike <= ZERO:
            raise ValueError("strike must be positive")


@dataclass(frozen=True, slots=True)
class Signal:
    direction: Direction
    bar_end: datetime
    spot: Decimal
    strategy: str = ""
    stop_price: Decimal | None = None
    atr: Decimal | None = None
    r_value: Decimal | None = None
    breakout_level: Decimal | None = None
    vwap: Decimal | None = None
    indicators: dict[str, str] = field(default_factory=dict)
    id: UUID = field(default_factory=uuid4)
    # Legacy compatibility fields
    ema_fast: Decimal = ZERO
    ema_slow: Decimal = ZERO


@dataclass(frozen=True, slots=True)
class TradeSignal:
    intent_id: UUID
    decision_at: datetime
    action: OrderSide
    direction: Direction
    symbol: str
    reference_price: Decimal
    quantity: int
    reason: str
    indicators: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _aware(self.decision_at)
        if self.reference_price <= ZERO or self.quantity <= 0:
            raise ValueError("trade signal price and quantity must be positive")


@dataclass(slots=True)
class Position:
    symbol: str
    direction: Direction
    quantity: int
    entry_price: Decimal
    opened_at: datetime
    initial_quantity: int | None = None
    first_target_taken: bool = False
    stop_price: Decimal | None = None
    broker_order_id: str | None = None
    strategy_name: str | None = None

    def __post_init__(self) -> None:
        _aware(self.opened_at)
        if self.quantity <= 0 or self.entry_price <= ZERO:
            raise ValueError("position quantity and entry price must be positive")
        if self.initial_quantity is None:
            self.initial_quantity = self.quantity


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    timestamp: datetime
    equity: Decimal
    cash_usd: Decimal
    day_realized_pnl: Decimal = ZERO
    day_unrealized_pnl: Decimal = ZERO
    risk_level: int | None = None
    margin_call: bool = False

    @property
    def day_pnl(self) -> Decimal:
        return self.day_realized_pnl + self.day_unrealized_pnl


@dataclass(frozen=True, slots=True)
class OrderRequest:
    symbol: str
    side: OrderSide
    quantity: int
    limit_price: Decimal
    intent_id: UUID = field(default_factory=uuid4)
    reason: str = ""


@dataclass(frozen=True, slots=True)
class BrokerOrder:
    order_id: str
    intent_id: UUID
    symbol: str
    side: OrderSide
    quantity: int
    filled_quantity: int
    average_price: Decimal | None
    status: str
    submitted_at: datetime


@dataclass(frozen=True, slots=True)
class ExitDecision:
    reason: ExitReason
    quantity: int
    new_stop: Decimal | None = None
