from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from ..config import Settings
from ..domain import (
    AccountSnapshot,
    Bar,
    BrokerOrder,
    Direction,
    OptionContract,
    OrderRequest,
    Position,
    Quote,
)
from ..domain import (
    OrderSide as DomainOrderSide,
)


def _value(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _decimal(value: Any, default: str = "0") -> Decimal:
    return Decimal(str(default if value is None else value))


def _timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo:
            return value
        return value.astimezone(timezone.utc)
    if value is None:
        return datetime.now(timezone.utc)
    return datetime.fromtimestamp(float(value), tz=timezone.utc)


def _status(value: Any) -> str:
    name = getattr(value, "name", None)
    text = name or str(value)
    return text.rsplit(".", 1)[-1].lower()


class LongbridgeSession:
    """Owns authenticated SDK contexts shared by quote and trade adapters."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.quote: Any = None
        self.trade: Any = None
        self._config: Any = None

    async def connect(self) -> None:
        if self.quote is not None:
            return
        from longbridge.openapi import AsyncQuoteContext, AsyncTradeContext, Config, OAuthBuilder

        log = logging.getLogger("qqq_trader.longbridge")
        if self.settings.longbridge_client_id:
            oauth = await OAuthBuilder(self.settings.longbridge_client_id).build_async(
                lambda url: log.info("Authorize Longbridge OAuth: %s", url)
            )
            self._config = Config.from_oauth(oauth)
        else:
            app_key = self.settings.longbridge_app_key.get_secret_value()
            app_secret = self.settings.longbridge_app_secret.get_secret_value()
            access_token = self.settings.longbridge_access_token.get_secret_value()
            if not all((app_key, app_secret, access_token)):
                raise RuntimeError(
                    "Longbridge credentials are missing: configure OAuth with "
                    "LONGBRIDGE_CLIENT_ID or set LONGBRIDGE_APP_KEY, "
                    "LONGBRIDGE_APP_SECRET and LONGBRIDGE_ACCESS_TOKEN in .env"
                )
            self._config = Config.from_apikey(app_key, app_secret, access_token)
        self.quote = AsyncQuoteContext.create(self._config)
        self.trade = AsyncTradeContext.create(self._config)

    async def close(self) -> None:
        for context in (self.quote, self.trade):
            close = getattr(context, "close", None)
            if close:
                result = close()
                if hasattr(result, "__await__"):
                    await result
        self.quote = None
        self.trade = None


class LongbridgeMarketData:
    def __init__(self, session: LongbridgeSession) -> None:
        self.session = session
        self._log = logging.getLogger("qqq_trader.longbridge.market")
        self._subscribed: set[str] = set()
        self._quote_pushes: dict[str, dict[str, Any]] = {}
        self._depth_pushes: dict[str, Any] = {}
        self._candlestick_bars: dict[str, dict[datetime, Bar]] = {}
        self._candlestick_periods: dict[str, str] = {}

    async def connect(self) -> None:
        await self.session.connect()
        self.session.quote.set_on_quote(self._on_quote)
        self.session.quote.set_on_depth(self._on_depth)
        self.session.quote.set_on_candlestick(self._on_candlestick)

    async def close(self) -> None:
        await self.session.close()

    async def subscribe(self, symbols) -> None:
        from longbridge.openapi import SubType

        pending = [symbol for symbol in symbols if symbol not in self._subscribed]
        if pending:
            await self.session.quote.subscribe(pending, [SubType.Quote, SubType.Depth])
            self._subscribed.update(pending)
            self._log.info("subscribed quotes | %s", ", ".join(pending))

    async def subscribe_candlesticks(self, symbols, period: str = "1m") -> None:
        from longbridge.openapi import Period, TradeSessions

        timeout = float(self.session.settings.longbridge_request_timeout_seconds)
        for symbol in symbols:
            if self._candlestick_periods.get(symbol) == period:
                continue
            try:
                async with asyncio.timeout(timeout):
                    rows = await self.session.quote.subscribe_candlesticks(
                        symbol,
                        self._period(Period, period),
                        TradeSessions.All,
                    )
            except TimeoutError as exc:
                raise RuntimeError(
                    f"Longbridge candlestick subscription timed out after {timeout:g}s: "
                    f"symbol={symbol}, period={period}"
                ) from exc
            self._candlestick_periods[symbol] = period
            for row in rows:
                self._store_candlestick(self._bar(symbol, row, period))

    def realtime_bars(self, symbol: str, count: int = 500) -> list[Bar]:
        bars = self._candlestick_bars.get(symbol, {})
        return [bars[timestamp] for timestamp in sorted(bars)[-count:]]

    async def latest_quote(self, symbol: str) -> Quote:
        greeks: dict[str, str] = {}
        pushed = self._quote_pushes.get(symbol)
        pushed_depth = self._depth_pushes.get(symbol)
        if (
            pushed is not None
            and _decimal(_value(pushed, "last_done")) > 0
            and not self._looks_like_option(symbol)
        ):
            return self._from_push(symbol, pushed, pushed_depth)
        if self._looks_like_option(symbol):
            rows = await self.session.quote.option_quote([symbol])
            try:
                from longbridge.openapi import CalcIndex

                indexes = [
                    CalcIndex.Delta,
                    CalcIndex.Gamma,
                    CalcIndex.Theta,
                    CalcIndex.Vega,
                    CalcIndex.Rho,
                ]
                calculated = await self.session.quote.calc_indexes([symbol], indexes)
                if calculated:
                    for name in ("delta", "gamma", "theta", "vega", "rho"):
                        greeks[name] = str(_value(calculated[0], name, ""))
            except Exception:
                pass
        else:
            rows = await self.session.quote.quote([symbol])
        if not rows:
            raise RuntimeError(f"no quote returned for {symbol}")
        row = rows[0]
        bid = ask = None
        try:
            depth = pushed_depth or await self.session.quote.depth(symbol)
            bids = _value(depth, "bids", []) or []
            asks = _value(depth, "asks", []) or []
            if bids:
                bid = _decimal(_value(bids[0], "price"))
            if asks:
                ask = _decimal(_value(asks[0], "price"))
        except Exception:
            pass
        quote_timestamp = _timestamp(_value(row, "timestamp"))
        last = _decimal(_value(row, "last_done"))
        volume = int(_value(row, "volume", 0) or 0)
        if pushed is not None:
            quote_timestamp = _timestamp(_value(pushed, "timestamp"))
            last = _decimal(_value(pushed, "last_done"), str(last))
            volume = int(_value(pushed, "volume", volume) or volume)
        return Quote(
            symbol=symbol,
            timestamp=quote_timestamp,
            last=last,
            bid=bid,
            ask=ask,
            volume=volume,
            open_interest=int(_value(row, "open_interest", 0) or 0),
            extra={
                "iv": str(_value(row, "implied_volatility", "")),
                **greeks,
            },
        )

    async def is_trading_day(self, trading_date: date) -> bool:
        from longbridge.openapi import Market

        response = await self.session.quote.trading_days(Market.US, trading_date, trading_date)
        return trading_date in (_value(response, "trading_days", []) or [])

    async def preflight_options(self, underlying: str, trading_date: date) -> list[str]:
        problems: list[str] = []
        try:
            expiries = await self.session.quote.option_chain_expiry_date_list(underlying)
            if trading_date not in expiries:
                return ["no same-day option expiry is available"]
            contracts = await self.option_chain(underlying, trading_date)
            if not contracts:
                return ["same-day option chain is empty"]
            spot = await self.latest_quote(underlying)
            nearest = min(contracts, key=lambda item: abs(item.strike - spot.last))
            rows = await self.session.quote.option_quote([nearest.symbol])
            if not rows:
                problems.append("OPRA option quote returned no data")
        except Exception as exc:
            problems.append(f"OpenAPI OPRA/option preflight failed: {exc}")
        return problems

    def _on_quote(self, symbol: str, event: Any) -> None:
        previous = self._quote_pushes.get(symbol, {})
        last_done = _decimal(_value(event, "last_done"))
        self._quote_pushes[symbol] = {
            "last_done": last_done if last_done > 0 else previous.get("last_done", Decimal(0)),
            "timestamp": _value(event, "timestamp", previous.get("timestamp")),
            "volume": _value(event, "volume", previous.get("volume", 0)),
        }

    def _on_depth(self, symbol: str, event: Any) -> None:
        self._depth_pushes[symbol] = event

    def _on_candlestick(self, symbol: str, event: Any) -> None:
        period = self._candlestick_periods.get(symbol, "1m")
        row = _value(event, "candlestick")
        if row is None:
            return
        self._store_candlestick(
            self._bar(
                symbol,
                row,
                period,
                complete=bool(_value(event, "is_confirmed", False)),
            )
        )

    def _store_candlestick(self, bar: Bar) -> None:
        bars = self._candlestick_bars.setdefault(bar.symbol, {})
        bars[bar.start] = bar
        while len(bars) > 1000:
            del bars[min(bars)]

    @staticmethod
    def _from_push(symbol: str, event: Any, depth: Any) -> Quote:
        bids = _value(depth, "bids", []) or []
        asks = _value(depth, "asks", []) or []
        return Quote(
            symbol=symbol,
            timestamp=_timestamp(_value(event, "timestamp")),
            last=_decimal(_value(event, "last_done")),
            bid=_decimal(_value(bids[0], "price")) if bids else None,
            ask=_decimal(_value(asks[0], "price")) if asks else None,
            volume=int(_value(event, "volume", 0) or 0),
        )

    async def option_chain(self, underlying: str, expiry: date) -> list[OptionContract]:
        rows = await self.session.quote.option_chain_info_by_date(underlying, expiry)
        result: list[OptionContract] = []
        for row in rows:
            strike = _decimal(_value(row, "strike_price", _value(row, "price")))
            call_symbol = _value(row, "call_symbol")
            put_symbol = _value(row, "put_symbol")
            if call_symbol:
                result.append(
                    OptionContract(call_symbol, underlying, expiry, strike, Direction.CALL)
                )
            if put_symbol:
                result.append(OptionContract(put_symbol, underlying, expiry, strike, Direction.PUT))
        return result

    async def recent_bars(self, symbol: str, count: int = 500, period: str = "1m") -> list[Bar]:
        from longbridge.openapi import AdjustType, Period, TradeSessions

        realtime = self.realtime_bars(symbol, count)
        if realtime:
            return realtime
        period_value = self._period(Period, period)
        rows = await self.session.quote.candlesticks(
            symbol,
            period_value,
            count,
            AdjustType.NoAdjust,
            TradeSessions.All,
        )
        return [self._bar(symbol, row, period) for row in rows]

    async def historical_bars(
        self, symbol: str, start: date, end: date, period: str = "1m"
    ) -> list[Bar]:
        from longbridge.openapi import AdjustType, Period, TradeSessions

        if end < start:
            raise ValueError("historical bar end date must not precede start date")
        timeout = float(self.session.settings.longbridge_request_timeout_seconds)
        bars: dict[datetime, Bar] = {}
        current = start
        while current <= end:
            try:
                async with asyncio.timeout(timeout):
                    rows = await self.session.quote.history_candlesticks_by_date(
                        symbol,
                        self._period(Period, period),
                        AdjustType.NoAdjust,
                        current,
                        current,
                        TradeSessions.Intraday,
                    )
            except TimeoutError as exc:
                raise RuntimeError(
                    f"Longbridge history request timed out after {timeout:g}s: "
                    f"symbol={symbol}, period={period}, date={current}"
                ) from exc
            for row in rows:
                bar = self._bar(symbol, row, period)
                bars[bar.start] = bar
            current += timedelta(days=1)
        self._log.info(
            "historical bars loaded | %s %s | %s to %s | %d bars",
            symbol, period, start, end, len(bars),
        )
        return [bars[timestamp] for timestamp in sorted(bars)]

    @staticmethod
    def _period(enum: Any, period: str) -> Any:
        mapping = {
            "1m": "Min_1",
            "5m": "Min_5",
            "15m": "Min_15",
            "30m": "Min_30",
            "60m": "Min_60",
            "day": "Day",
        }
        return getattr(enum, mapping[period])

    @staticmethod
    def _bar(symbol: str, row: Any, period: str, complete: bool = True) -> Bar:
        start = _timestamp(_value(row, "timestamp"))
        if period == "1m":
            minutes = 1
        elif period.endswith("m"):
            minutes = int(period.removesuffix("m"))
        else:
            minutes = 1440
        return Bar(
            symbol=symbol,
            start=start,
            end=start + timedelta(minutes=minutes),
            open=_decimal(_value(row, "open")),
            high=_decimal(_value(row, "high")),
            low=_decimal(_value(row, "low")),
            close=_decimal(_value(row, "close")),
            volume=int(_value(row, "volume", 0)),
            turnover=_decimal(_value(row, "turnover")),
            complete=complete,
        )

    @staticmethod
    def _looks_like_option(symbol: str) -> bool:
        core = symbol.split(".")[0]
        return len(core) > 10 and ("C" in core[-10:] or "P" in core[-10:])


class LongbridgeBroker:
    def __init__(self, session: LongbridgeSession, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self._log = logging.getLogger("qqq_trader.longbridge.broker")
        self._intent_by_order: dict[str, UUID] = {}

    async def connect(self) -> None:
        await self.session.connect()

    async def close(self) -> None:
        return None

    async def preflight(self, account_id: str) -> list[str]:
        problems: list[str] = []
        try:
            account = await self.account_snapshot()
            if account.cash_usd <= 0:
                problems.append("no positive USD cash balance")
            if account.margin_call:
                problems.append("account is under margin call")
        except Exception as exc:
            problems.append(f"account query failed: {exc}")
        return problems

    async def account_snapshot(self) -> AccountSnapshot:
        rows = await self.session.trade.account_balance("USD")
        if not rows:
            raise RuntimeError("Longbridge returned no USD account balance")
        row = rows[0]
        cash = _decimal(_value(row, "total_cash"))
        equity = _decimal(_value(row, "net_assets", _value(row, "total_cash")))
        self._log.debug("account snapshot | equity=%.2f | cash=%.2f", equity, cash)
        return AccountSnapshot(
            timestamp=datetime.now(timezone.utc),
            equity=equity,
            cash_usd=cash,
            risk_level=int(_value(row, "risk_level")) if _value(row, "risk_level") else None,
            margin_call=bool(_value(row, "margin_call", False)),
        )

    async def submit_limit(self, request: OrderRequest) -> BrokerOrder:
        from longbridge.openapi import OrderSide, OrderType, TimeInForceType

        self._log.info(
            "submit order | %s %s | %s | qty=%d | limit=%s",
            request.side.value, request.symbol, request.reason,
            request.quantity, request.limit_price,
        )
        side = OrderSide.Buy if request.side is DomainOrderSide.BUY else OrderSide.Sell
        response = await self.session.trade.submit_order(
            request.symbol,
            OrderType.LO,
            side,
            Decimal(request.quantity),
            TimeInForceType.Day,
            request.limit_price,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            str(request.intent_id),
        )
        order_id = str(_value(response, "order_id"))
        self._intent_by_order[order_id] = request.intent_id
        self._log.info("order submitted | order_id=%s", order_id)
        return await self.order(order_id)

    async def cancel_order(self, order_id: str) -> None:
        self._log.info("cancel order | order_id=%s", order_id)
        await self.session.trade.cancel_order(order_id)

    async def order(self, order_id: str) -> BrokerOrder:
        row = await self.session.trade.order_detail(order_id)
        return self._broker_order(row, self._intent_by_order.get(order_id))

    async def positions(self) -> list[Position]:
        response = await self.session.trade.stock_positions()
        channels = _value(response, "channels", _value(response, "list", [])) or []
        positions: list[Position] = []
        for channel in channels:
            rows = _value(channel, "positions", []) or []
            for row in rows:
                quantity = int(_decimal(_value(row, "quantity")))
                if quantity <= 0:
                    continue
                symbol = str(_value(row, "symbol"))
                positions.append(
                    Position(
                        symbol=symbol,
                        direction=(
                            Direction.CALL if "C" in symbol.split(".")[0][-10:] else Direction.PUT
                        ),
                        quantity=quantity,
                        entry_price=_decimal(_value(row, "cost_price")),
                        opened_at=datetime.now(timezone.utc),
                    )
                )
        return positions

    async def open_orders(self) -> list[BrokerOrder]:
        rows = await self.session.trade.today_orders()
        result = []
        for row in rows:
            order = self._broker_order(row, None)
            terminal = {
                "filled",
                "filled_status",
                "done",
                "canceled",
                "cancelled",
                "rejected",
                "expired",
            }
            if order.status not in terminal:
                result.append(order)
        return result

    def _broker_order(self, row: Any, intent_id: UUID | None) -> BrokerOrder:
        order_id = str(_value(row, "order_id"))
        remark = _value(row, "remark", "")
        if intent_id is None:
            try:
                intent_id = UUID(str(remark))
            except (ValueError, TypeError):
                intent_id = UUID(int=0)
        side_text = _status(_value(row, "side"))
        return BrokerOrder(
            order_id=order_id,
            intent_id=intent_id,
            symbol=str(_value(row, "symbol")),
            side=DomainOrderSide.BUY if "buy" in side_text else DomainOrderSide.SELL,
            quantity=int(_decimal(_value(row, "quantity"))),
            filled_quantity=int(_decimal(_value(row, "executed_quantity"))),
            average_price=(
                _decimal(_value(row, "executed_price")) if _value(row, "executed_price") else None
            ),
            status=_status(_value(row, "status")),
            submitted_at=_timestamp(_value(row, "submitted_at")),
        )
