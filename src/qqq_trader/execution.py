from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from decimal import ROUND_DOWN, Decimal

from .config import Settings
from .domain import BrokerOrder, OrderRequest
from .interfaces import Broker, Journal, QuoteSupplier
from .policy import RULES

FILLED_STATUSES = {"filled", "filled_status", "done"}
TERMINAL_STATUSES = FILLED_STATUSES | {"canceled", "cancelled", "rejected", "expired"}


def tick_price(value: Decimal) -> Decimal:
    tick = Decimal("0.01")
    return value.quantize(tick, rounding=ROUND_DOWN)


class OrderExecutor:
    def __init__(self, broker: Broker, journal: Journal, settings: Settings) -> None:
        self.broker = broker
        self.journal = journal
        self.settings = settings
        self._log = logging.getLogger("qqq_trader.executor")

    async def entry(
        self, request: OrderRequest, quote_supplier: QuoteSupplier
    ) -> BrokerOrder | None:
        initial_limit = request.limit_price
        ceiling = tick_price(initial_limit + RULES.slippage_quote)
        attempts = RULES.entry_reprices + 1
        current = request
        total_filled = 0
        filled_notional = Decimal(0)
        last_order: BrokerOrder | None = None
        for attempt in range(attempts):
            await self.journal.order_intent(current)
            order = await self.broker.submit_limit(current)
            await self.journal.broker_order(order)
            final = await self._wait_terminal(order)
            last_order = final
            if final.filled_quantity and final.average_price is not None:
                total_filled += final.filled_quantity
                filled_notional += final.average_price * final.filled_quantity
            if total_filled >= request.quantity:
                self._log.info(
                    "entry filled | %s | qty=%d | avg=%s",
                    request.symbol, total_filled,
                    filled_notional / Decimal(total_filled),
                )
                return self._aggregate(request, last_order, total_filled, filled_notional)
            if final.status.lower() not in TERMINAL_STATUSES:
                await self.broker.cancel_order(final.order_id)
            if attempt == attempts - 1:
                break
            quote = await quote_supplier(request.symbol)
            if quote.ask is None:
                break
            next_price = min(tick_price(quote.ask), ceiling)
            if next_price <= 0 or next_price > ceiling:
                break
            current = replace(
                request,
                quantity=request.quantity - total_filled,
                limit_price=next_price,
            )
        if total_filled > 0 and last_order is not None:
            self._log.warning(
                "partial entry | %s | filled=%d/%d", request.symbol, total_filled, request.quantity
            )
            await self.journal.event(
                "partial_entry",
                "entry policy ended with a partial fill; managing the filled position",
                {"intent": str(request.intent_id), "quantity": total_filled},
            )
            return self._aggregate(request, last_order, total_filled, filled_notional)
        self._log.warning("entry abandoned | %s | no fill after repricing", request.symbol)
        await self.journal.event(
            "entry_abandoned",
            "entry was not filled inside bounded repricing policy",
            {"intent": str(request.intent_id)},
        )
        return None

    async def exit(
        self,
        request: OrderRequest,
        quote_supplier: QuoteSupplier,
        max_attempts: int = 10,
    ) -> BrokerOrder | None:
        current = request
        total_filled = 0
        filled_notional = Decimal(0)
        last_order: BrokerOrder | None = None
        for _attempt in range(max_attempts):
            await self.journal.order_intent(current)
            order = await self.broker.submit_limit(current)
            await self.journal.broker_order(order)
            final = await self._wait_terminal(order)
            last_order = final
            if final.filled_quantity and final.average_price is not None:
                total_filled += final.filled_quantity
                filled_notional += final.average_price * final.filled_quantity
            if total_filled >= request.quantity:
                self._log.info(
                    "exit filled | %s | qty=%d | avg=%s",
                    request.symbol, total_filled,
                    filled_notional / Decimal(total_filled),
                )
                return self._aggregate(request, last_order, total_filled, filled_notional)
            if final.status.lower() not in TERMINAL_STATUSES:
                await self.broker.cancel_order(final.order_id)
            quote = await quote_supplier(request.symbol)
            if quote.bid is None or quote.bid <= 0:
                await asyncio.sleep(1)
                continue
            current = replace(
                request,
                quantity=request.quantity - total_filled,
                limit_price=tick_price(
                    max(Decimal("0.01"), quote.bid - RULES.slippage_quote)
                ),
            )
        self._log.error(
            "CRITICAL exit failure | %s | unable to fill after %d attempts",
            request.symbol,
            max_attempts,
        )
        await self.journal.event(
            "critical_exit_failure",
            "unable to confirm an exit fill after all retries",
            {"intent": str(request.intent_id), "symbol": request.symbol},
        )
        return None

    async def emergency_exit(
        self,
        request: OrderRequest,
        quote_supplier: QuoteSupplier,
    ) -> BrokerOrder | None:
        """Use a market order for a hard stop, then chase any unfilled remainder."""
        submit_market = getattr(self.broker, "submit_market", None)
        if not callable(submit_market):
            return await self.exit(request, quote_supplier)

        await self.journal.order_intent(request)
        order = await submit_market(request)
        await self.journal.broker_order(order)
        final = await self._wait_terminal(order)
        filled_quantity = final.filled_quantity
        filled_notional = (
            final.average_price * filled_quantity
            if filled_quantity and final.average_price is not None
            else Decimal(0)
        )
        if filled_quantity >= request.quantity and final.average_price is not None:
            self._log.warning(
                "emergency exit filled | %s | qty=%d | avg=%s",
                request.symbol,
                filled_quantity,
                final.average_price,
            )
            return self._aggregate(
                request,
                final,
                filled_quantity,
                filled_notional,
            )
        if final.status.lower() not in TERMINAL_STATUSES:
            await self.broker.cancel_order(final.order_id)

        remaining = request.quantity - filled_quantity
        fallback = await self.exit(
            replace(request, quantity=remaining),
            quote_supplier,
            max_attempts=3,
        )
        if fallback is not None and fallback.average_price is not None:
            filled_quantity += fallback.filled_quantity
            filled_notional += fallback.average_price * fallback.filled_quantity
            final = fallback
        if filled_quantity > 0:
            return self._aggregate(
                request,
                final,
                filled_quantity,
                filled_notional,
            )
        return None

    async def _wait_terminal(self, order: BrokerOrder) -> BrokerOrder:
        if order.status.lower() in TERMINAL_STATUSES:
            return order
        deadline = asyncio.get_running_loop().time() + RULES.order_timeout_seconds
        current = order
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(1.0)
            current = await self.broker.order(order.order_id)
            await self.journal.broker_order(current)
            if current.status.lower() in TERMINAL_STATUSES:
                break
        return current

    @staticmethod
    def _filled(order: BrokerOrder) -> bool:
        return order.filled_quantity >= order.quantity and order.average_price is not None

    @staticmethod
    def _aggregate(
        request: OrderRequest,
        last_order: BrokerOrder,
        total_filled: int,
        filled_notional: Decimal,
    ) -> BrokerOrder:
        return BrokerOrder(
            order_id=last_order.order_id,
            intent_id=request.intent_id,
            symbol=request.symbol,
            side=request.side,
            quantity=total_filled,
            filled_quantity=total_filled,
            average_price=filled_notional / Decimal(total_filled),
            status="filled",
            submitted_at=last_order.submitted_at,
        )
