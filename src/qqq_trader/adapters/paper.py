from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from ..domain import (
    AccountSnapshot,
    BrokerOrder,
    Direction,
    OrderRequest,
    OrderSide,
    Position,
)


class PaperBroker:
    """Deterministic in-process broker used by paper mode and integration tests."""

    def __init__(
        self,
        starting_equity: Decimal = Decimal("100000"),
        fee_per_contract: Decimal = Decimal("1.50"),
    ) -> None:
        self.starting_equity = starting_equity
        self.fee_per_contract = fee_per_contract
        self.cash = starting_equity
        self.realized_pnl = Decimal(0)
        self._orders: dict[str, BrokerOrder] = {}
        self._positions: dict[str, Position] = {}

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def preflight(self, account_id: str) -> list[str]:
        return []

    async def account_snapshot(self) -> AccountSnapshot:
        return AccountSnapshot(
            timestamp=datetime.now(timezone.utc),
            equity=self.starting_equity + self.realized_pnl,
            cash_usd=self.cash,
            day_realized_pnl=self.realized_pnl,
        )

    async def submit_limit(self, request: OrderRequest) -> BrokerOrder:
        now = datetime.now(timezone.utc)
        order_id = str(uuid4())
        order = BrokerOrder(
            order_id=order_id,
            intent_id=request.intent_id,
            symbol=request.symbol,
            side=request.side,
            quantity=request.quantity,
            filled_quantity=request.quantity,
            average_price=request.limit_price,
            status="filled",
            submitted_at=now,
        )
        self._orders[order_id] = order
        if request.side is OrderSide.BUY:
            self.cash -= request.limit_price * Decimal(100) * request.quantity
            self._positions[request.symbol] = Position(
                symbol=request.symbol,
                direction=self._direction(request.symbol),
                quantity=request.quantity,
                entry_price=request.limit_price,
                opened_at=now,
                broker_order_id=order_id,
            )
        else:
            position = self._positions[request.symbol]
            sold = min(request.quantity, position.quantity)
            self.cash += request.limit_price * Decimal(100) * sold
            self.realized_pnl += (request.limit_price - position.entry_price) * Decimal(
                100
            ) * sold - self.fee_per_contract * sold
            self.cash -= self.fee_per_contract * sold
            position.quantity -= sold
            if position.quantity == 0:
                del self._positions[request.symbol]
        return order

    async def cancel_order(self, order_id: str) -> None:
        return None

    async def order(self, order_id: str) -> BrokerOrder:
        return self._orders[order_id]

    async def positions(self) -> list[Position]:
        return list(self._positions.values())

    async def open_orders(self) -> list[BrokerOrder]:
        return [
            order for order in self._orders.values() if order.status not in {"filled", "canceled"}
        ]

    async def today_orders(self) -> list[BrokerOrder]:
        return list(self._orders.values())

    @staticmethod
    def _direction(symbol: str) -> Direction:
        core = symbol.split(".")[0]
        return Direction.CALL if "C" in core[-10:] else Direction.PUT
