from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Integer,
    MetaData,
    Numeric,
    String,
    Text,
    Uuid,
    func,
    select,
)
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .domain import (
    AccountSnapshot,
    Bar,
    BrokerOrder,
    Direction,
    OrderRequest,
    OrderSide,
    Signal,
    TradeSignal,
)

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class EventRow(Base):
    __tablename__ = "system_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    kind: Mapped[str] = mapped_column(String(64), index=True)
    message: Mapped[str] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class TradeSignalRow(Base):
    __tablename__ = "trade_signals"
    intent_id: Mapped[Any] = mapped_column(Uuid, primary_key=True)
    decision_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    action: Mapped[str] = mapped_column(String(8), index=True)
    direction: Mapped[str] = mapped_column(String(8))
    symbol: Mapped[str] = mapped_column(String(64), index=True)
    reference_price: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    quantity: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), index=True, default="accepted")
    reason: Mapped[str] = mapped_column(String(64), default="")
    indicators: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class OrderIntentRow(Base):
    __tablename__ = "order_intents"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    intent_id: Mapped[Any] = mapped_column(Uuid, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    symbol: Mapped[str] = mapped_column(String(64), index=True)
    side: Mapped[str] = mapped_column(String(8))
    quantity: Mapped[int] = mapped_column(Integer)
    limit_price: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    reason: Mapped[str] = mapped_column(String(64), default="")


class BrokerOrderRow(Base):
    __tablename__ = "broker_orders"
    order_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    intent_id: Mapped[Any] = mapped_column(Uuid, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    symbol: Mapped[str] = mapped_column(String(64), index=True)
    side: Mapped[str] = mapped_column(String(8))
    quantity: Mapped[int] = mapped_column(Integer)
    filled_quantity: Mapped[int] = mapped_column(Integer)
    average_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ExecutionRow(Base):
    __tablename__ = "executions"
    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    order_id: Mapped[str] = mapped_column(String(64), index=True)
    intent_id: Mapped[Any] = mapped_column(Uuid, index=True)
    symbol: Mapped[str] = mapped_column(String(64), index=True)
    side: Mapped[str] = mapped_column(String(8))
    cumulative_quantity: Mapped[int] = mapped_column(Integer)
    price: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class PositionRow(Base):
    __tablename__ = "positions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(64), index=True)
    direction: Mapped[str] = mapped_column(String(8))
    quantity: Mapped[int] = mapped_column(Integer)
    entry_price: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    realized_pnl: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)


class RiskSnapshotRow(Base):
    __tablename__ = "risk_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    equity: Mapped[Decimal] = mapped_column(Numeric(20, 4))
    cash_usd: Mapped[Decimal] = mapped_column(Numeric(20, 4))
    day_realized_pnl: Mapped[Decimal] = mapped_column(Numeric(20, 4))
    day_unrealized_pnl: Mapped[Decimal] = mapped_column(Numeric(20, 4))
    halted: Mapped[bool] = mapped_column(Boolean, default=False)


class TradeSummaryRow(Base):
    __tablename__ = "trade_summaries"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(64), index=True)
    direction: Mapped[str] = mapped_column(String(8))
    quantity: Mapped[int] = mapped_column(Integer)
    entry_price: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    exit_price: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    pnl: Mapped[Decimal] = mapped_column(Numeric(20, 4))
    fees: Mapped[Decimal] = mapped_column(Numeric(20, 4))
    entry_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    exit_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    exit_reason: Mapped[str] = mapped_column(String(64))
    slippage: Mapped[Decimal] = mapped_column(Numeric(18, 6), default=Decimal(0))
    mae: Mapped[Decimal] = mapped_column(Numeric(18, 6), default=Decimal(0))
    mfe: Mapped[Decimal] = mapped_column(Numeric(18, 6), default=Decimal(0))


class ReportRunRow(Base):
    __tablename__ = "report_runs"
    trading_date: Mapped[date] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    output_path: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))


class ConfigVersionRow(Base):
    __tablename__ = "config_versions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )
    values: Mapped[dict[str, Any]] = mapped_column(JSON)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)


class BacktestRunRow(Base):
    __tablename__ = "backtest_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(String(24), index=True)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    request: Mapped[dict[str, Any]] = mapped_column(JSON)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class MySQLJournal:
    def __init__(self, database_url: str) -> None:
        self.engine: AsyncEngine = create_async_engine(database_url, pool_pre_ping=True)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def create_schema(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def close(self) -> None:
        await self.engine.dispose()

    async def ping(self) -> bool:
        try:
            async with self.engine.connect() as connection:
                await connection.exec_driver_sql("SELECT 1")
            return True
        except Exception:
            return False

    async def event(self, kind: str, message: str, details: dict | None = None) -> None:
        async with self.sessions() as session, session.begin():
            session.add(EventRow(kind=kind, message=message, details=details or {}))

    async def signal(self, signal: Signal, accepted: bool, reason: str = "") -> None:
        if accepted:
            return
        from uuid import uuid4 as _uuid4

        async with self.sessions() as session, session.begin():
            await session.merge(
                TradeSignalRow(
                    intent_id=_uuid4(),
                    decision_at=signal.bar_end,
                    action="buy",
                    direction=signal.direction.value,
                    symbol="",
                    reference_price=signal.spot,
                    quantity=0,
                    status="rejected",
                    reason=reason,
                    indicators=signal.indicators or {},
                )
            )

    async def trade_signal(self, signal: TradeSignal) -> None:
        async with self.sessions() as session, session.begin():
            await session.merge(
                TradeSignalRow(
                    intent_id=signal.intent_id,
                    decision_at=signal.decision_at,
                    action=signal.action.value,
                    direction=signal.direction.value,
                    symbol=signal.symbol,
                    reference_price=signal.reference_price,
                    quantity=signal.quantity,
                    status="accepted",
                    reason=signal.reason,
                    indicators=signal.indicators,
                )
            )

    async def trade_signal_status(self, intent_id: UUID, status: str) -> None:
        if status not in {"accepted", "executed", "failed"}:
            raise ValueError(f"invalid trade signal status: {status}")
        async with self.sessions() as session, session.begin():
            row = await session.get(TradeSignalRow, intent_id)
            if row is not None:
                row.status = status

    async def trade_signal_by_intent(self, intent_id: UUID) -> TradeSignal | None:
        async with self.sessions() as session:
            row = await session.get(TradeSignalRow, intent_id)
            return self._trade_signal(row) if row is not None else None

    async def trade_signal_for_position(
        self, symbol: str, quantity: int
    ) -> TradeSignal | None:
        async with self.sessions() as session:
            row = await session.scalar(
                select(TradeSignalRow)
                .where(
                    TradeSignalRow.symbol == symbol,
                    TradeSignalRow.action == OrderSide.BUY.value,
                    TradeSignalRow.status.in_(("accepted", "executed")),
                )
                .order_by(TradeSignalRow.decision_at.desc())
                .limit(1)
            )
            if row is None:
                return None
            orders = list(
                (
                    await session.scalars(
                        select(BrokerOrderRow).where(BrokerOrderRow.symbol == symbol)
                    )
                ).all()
            )
            net_filled = sum(
                order.filled_quantity
                if order.side == OrderSide.BUY.value
                else -order.filled_quantity
                for order in orders
            )
            persisted_match = net_filled == quantity
            crash_window_match = (
                net_filled == 0 and row.status == "accepted" and quantity <= row.quantity
            )
            return self._trade_signal(row) if persisted_match or crash_window_match else None

    async def recover_trade_signal_statuses(self) -> dict[str, int]:
        recovered = {"executed": 0, "failed": 0, "summaries_rebuilt": 0}
        async with self.sessions() as session, session.begin():
            rows = list(
                (
                    await session.scalars(
                        select(TradeSignalRow).where(TradeSignalRow.status == "accepted")
                    )
                ).all()
            )
            for row in rows:
                orders = list(
                    (
                        await session.scalars(
                            select(BrokerOrderRow).where(
                                BrokerOrderRow.intent_id == row.intent_id
                            )
                        )
                    ).all()
                )
                filled_quantity = sum(order.filled_quantity for order in orders)
                status = "executed" if filled_quantity >= row.quantity else "failed"
                row.status = status
                recovered[status] += 1

            buy_executed = list(
                (
                    await session.scalars(
                        select(TradeSignalRow).where(
                            TradeSignalRow.status == "executed",
                            TradeSignalRow.action == "buy",
                        )
                    )
                ).all()
            )
            for buy_signal in buy_executed:
                existing_summary = await session.scalar(
                    select(func.count())
                    .select_from(TradeSummaryRow)
                    .where(
                        TradeSummaryRow.symbol == buy_signal.symbol,
                        TradeSummaryRow.entry_at >= buy_signal.decision_at - timedelta(seconds=30),
                        TradeSummaryRow.entry_at <= buy_signal.decision_at + timedelta(minutes=5),
                    )
                )
                if existing_summary and existing_summary > 0:
                    continue
                sell_signals = list(
                    (
                        await session.scalars(
                            select(TradeSignalRow).where(
                                TradeSignalRow.status == "executed",
                                TradeSignalRow.action == "sell",
                                TradeSignalRow.symbol == buy_signal.symbol,
                                TradeSignalRow.decision_at > buy_signal.decision_at,
                            ).order_by(TradeSignalRow.decision_at)
                        )
                    ).all()
                )
                if not sell_signals:
                    continue
                sell_signal = sell_signals[0]
                buy_orders = list(
                    (
                        await session.scalars(
                            select(BrokerOrderRow).where(
                                BrokerOrderRow.intent_id == buy_signal.intent_id,
                                BrokerOrderRow.filled_quantity > 0,
                            )
                        )
                    ).all()
                )
                sell_orders = list(
                    (
                        await session.scalars(
                            select(BrokerOrderRow).where(
                                BrokerOrderRow.intent_id == sell_signal.intent_id,
                                BrokerOrderRow.filled_quantity > 0,
                            )
                        )
                    ).all()
                )
                if not buy_orders or not sell_orders:
                    continue
                entry_price = buy_orders[0].average_price or buy_signal.reference_price
                exit_price = sell_orders[0].average_price or sell_signal.reference_price
                quantity = min(buy_orders[0].filled_quantity, sell_orders[0].filled_quantity)
                from .config import Settings

                fee = Settings().fee_per_contract
                pnl = (exit_price - entry_price) * Decimal(100) * quantity - fee * quantity
                session.add(
                    TradeSummaryRow(
                        symbol=buy_signal.symbol,
                        direction=buy_signal.direction,
                        quantity=quantity,
                        entry_price=entry_price,
                        exit_price=exit_price,
                        pnl=pnl,
                        fees=fee * quantity,
                        entry_at=buy_signal.decision_at,
                        exit_at=sell_signal.decision_at,
                        exit_reason="recovered",
                        slippage=Decimal(0),
                        mae=Decimal(0),
                        mfe=Decimal(0),
                    )
                )
                recovered["summaries_rebuilt"] += 1
        return recovered

    @staticmethod
    def _trade_signal(row: TradeSignalRow) -> TradeSignal:
        return TradeSignal(
            intent_id=row.intent_id,
            decision_at=row.decision_at,
            action=OrderSide(row.action),
            direction=Direction(row.direction),
            symbol=row.symbol,
            reference_price=row.reference_price,
            quantity=row.quantity,
            reason=row.reason,
            indicators=row.indicators or {},
        )

    async def order_intent(self, request: OrderRequest) -> None:
        async with self.sessions() as session, session.begin():
            session.add(
                OrderIntentRow(
                    intent_id=request.intent_id,
                    symbol=request.symbol,
                    side=request.side.value,
                    quantity=request.quantity,
                    limit_price=request.limit_price,
                    reason=request.reason,
                )
            )

    async def broker_order(self, order: BrokerOrder) -> None:
        async with self.sessions() as session, session.begin():
            await session.merge(
                BrokerOrderRow(
                    order_id=order.order_id,
                    intent_id=order.intent_id,
                    symbol=order.symbol,
                    side=order.side.value,
                    quantity=order.quantity,
                    filled_quantity=order.filled_quantity,
                    average_price=order.average_price,
                    status=order.status,
                    submitted_at=order.submitted_at,
                    updated_at=datetime.now(timezone.utc),
                )
            )
            if order.filled_quantity > 0 and order.average_price is not None:
                await session.merge(
                    ExecutionRow(
                        id=f"{order.order_id}:{order.filled_quantity}",
                        order_id=order.order_id,
                        intent_id=order.intent_id,
                        symbol=order.symbol,
                        side=order.side.value,
                        cumulative_quantity=order.filled_quantity,
                        price=order.average_price,
                        recorded_at=datetime.now(timezone.utc),
                    )
                )

    async def trade_summary(self, summary: dict) -> None:
        async with self.sessions() as session, session.begin():
            session.add(TradeSummaryRow(**summary))

    async def risk_snapshot(self, account: AccountSnapshot, halted: bool) -> None:
        async with self.sessions() as session, session.begin():
            session.add(
                RiskSnapshotRow(
                    created_at=account.timestamp,
                    equity=account.equity,
                    cash_usd=account.cash_usd,
                    day_realized_pnl=account.day_realized_pnl,
                    day_unrealized_pnl=account.day_unrealized_pnl,
                    halted=halted,
                )
            )

    async def today_realized_pnl_and_trades(self, trading_date: date) -> tuple[Decimal, int]:
        from .config import NY_TZ

        local_start = datetime.combine(trading_date, datetime.min.time(), NY_TZ)
        local_end = local_start + timedelta(days=1)
        start_utc = local_start.astimezone(timezone.utc)
        end_utc = local_end.astimezone(timezone.utc)
        async with self.sessions() as session:
            rows = list(
                (
                    await session.scalars(
                        select(TradeSummaryRow).where(
                            TradeSummaryRow.exit_at >= start_utc,
                            TradeSummaryRow.exit_at < end_utc,
                        )
                    )
                ).all()
            )
        realized = sum((row.pnl for row in rows), Decimal(0))
        return realized, len(rows)

    async def report_rows(self, start: datetime, end: datetime) -> dict[str, list[Any]]:
        async with self.sessions() as session:
            trades = list(
                (
                    await session.scalars(
                        select(TradeSummaryRow)
                        .where(TradeSummaryRow.exit_at >= start, TradeSummaryRow.exit_at < end)
                        .order_by(TradeSummaryRow.exit_at)
                    )
                ).all()
            )
            events = list(
                (
                    await session.scalars(
                        select(EventRow)
                        .where(EventRow.created_at >= start, EventRow.created_at < end)
                        .order_by(EventRow.created_at)
                    )
                ).all()
            )
            signals = list(
                (
                    await session.scalars(
                        select(TradeSignalRow)
                        .where(TradeSignalRow.decision_at >= start, TradeSignalRow.decision_at < end)
                        .order_by(TradeSignalRow.decision_at)
                    )
                ).all()
            )
            risks = list(
                (
                    await session.scalars(
                        select(RiskSnapshotRow)
                        .where(
                            RiskSnapshotRow.created_at >= start,
                            RiskSnapshotRow.created_at < end,
                        )
                        .order_by(RiskSnapshotRow.created_at)
                    )
                ).all()
            )
            return {"trades": trades, "events": events, "signals": signals, "risks": risks}

    async def performance_20d(self, before: datetime) -> dict[str, Any]:
        async with self.sessions() as session:
            rows = list(
                (
                    await session.scalars(
                        select(TradeSummaryRow)
                        .where(TradeSummaryRow.exit_at < before)
                        .order_by(TradeSummaryRow.exit_at.desc())
                        .limit(500)
                    )
                ).all()
            )
        selected = []
        days = []
        for row in rows:
            trading_day = row.exit_at.date()
            if trading_day not in days:
                days.append(trading_day)
            if len(days) > 20:
                break
            selected.append(row)
        wins = sum(1 for row in selected if row.pnl > 0)
        return {
            "trading_days": len(days),
            "trades": len(selected),
            "net_pnl": str(sum((row.pnl for row in selected), Decimal(0))),
            "win_rate": str(Decimal(wins) / Decimal(len(selected))) if selected else "0",
            "average_slippage": str(
                sum((row.slippage for row in selected), Decimal(0)) / Decimal(len(selected))
            )
            if selected
            else "0",
        }

    async def list_trades(
        self,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        symbol: str | None = None,
        direction: str | None = None,
        pnl_sign: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[TradeSummaryRow], int]:
        filters = []
        if start is not None:
            filters.append(TradeSummaryRow.exit_at >= start)
        if end is not None:
            filters.append(TradeSummaryRow.exit_at < end)
        if symbol:
            filters.append(TradeSummaryRow.symbol.like(f"%{symbol}%"))
        if direction:
            filters.append(TradeSummaryRow.direction == direction)
        if pnl_sign == "profit":
            filters.append(TradeSummaryRow.pnl > 0)
        elif pnl_sign == "loss":
            filters.append(TradeSummaryRow.pnl < 0)
        elif pnl_sign == "flat":
            filters.append(TradeSummaryRow.pnl == 0)
        async with self.sessions() as session:
            total = await session.scalar(
                select(func.count()).select_from(TradeSummaryRow).where(*filters)
            )
            rows = list(
                (
                    await session.scalars(
                        select(TradeSummaryRow)
                        .where(*filters)
                        .order_by(TradeSummaryRow.exit_at.desc())
                        .offset((page - 1) * page_size)
                        .limit(page_size)
                    )
                ).all()
            )
        return rows, int(total or 0)

    async def get_trade(self, trade_id: int) -> TradeSummaryRow | None:
        async with self.sessions() as session:
            return await session.get(TradeSummaryRow, trade_id)

    async def list_decision_signals(
        self,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        action: str | None = None,
        status: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[list[dict[str, Any]], int]:
        filters = []
        if start is not None:
            filters.append(TradeSignalRow.decision_at >= start)
        if end is not None:
            filters.append(TradeSignalRow.decision_at < end)
        if action:
            filters.append(TradeSignalRow.action == action)
        if status:
            filters.append(TradeSignalRow.status == status)

        offset = (page - 1) * page_size
        async with self.sessions() as session:
            total = int(
                await session.scalar(
                    select(func.count()).select_from(TradeSignalRow).where(*filters)
                )
                or 0
            )
            rows = list(
                (
                    await session.scalars(
                        select(TradeSignalRow)
                        .where(*filters)
                        .order_by(TradeSignalRow.decision_at.desc())
                        .offset(offset)
                        .limit(page_size)
                    )
                ).all()
            )
            items = [
                {
                    "id": f"{row.action}:{row.intent_id}",
                    "action": row.action,
                    "decision_at": row.decision_at,
                    "direction": row.direction,
                    "symbol": row.symbol or None,
                    "price": row.reference_price,
                    "quantity": row.quantity if row.quantity > 0 else None,
                    "status": row.status,
                    "reason": row.reason,
                    "indicators": row.indicators or {},
                }
                for row in rows
            ]
        return items, total

    async def active_config(self) -> ConfigVersionRow | None:
        async with self.sessions() as session:
            return await session.scalar(
                select(ConfigVersionRow)
                .where(ConfigVersionRow.active.is_(True))
                .order_by(ConfigVersionRow.id.desc())
                .limit(1)
            )

    async def save_config(self, values: dict[str, Any]) -> ConfigVersionRow:
        async with self.sessions() as session, session.begin():
            rows = list(
                (
                    await session.scalars(
                        select(ConfigVersionRow).where(ConfigVersionRow.active.is_(True))
                    )
                ).all()
            )
            for row in rows:
                row.active = False
            created = ConfigVersionRow(values=values, active=True)
            session.add(created)
            await session.flush()
            await session.refresh(created)
            return created

    async def config_versions(self, limit: int = 50) -> list[ConfigVersionRow]:
        async with self.sessions() as session:
            return list(
                (
                    await session.scalars(
                        select(ConfigVersionRow).order_by(ConfigVersionRow.id.desc()).limit(limit)
                    )
                ).all()
            )

    async def get_config_version(self, version: int) -> ConfigVersionRow | None:
        async with self.sessions() as session:
            return await session.get(ConfigVersionRow, version)

    async def save_backtest_run(self, payload: dict[str, Any]) -> None:
        async with self.sessions() as session, session.begin():
            await session.merge(BacktestRunRow(**payload))

    async def list_backtest_runs(self, limit: int = 50) -> list[BacktestRunRow]:
        async with self.sessions() as session:
            return list(
                (
                    await session.scalars(
                        select(BacktestRunRow)
                        .order_by(BacktestRunRow.created_at.desc())
                        .limit(limit)
                    )
                ).all()
            )

    async def delete_backtest_run(self, job_id: str) -> None:
        async with self.sessions() as session, session.begin():
            row = await session.get(BacktestRunRow, job_id)
            if row is not None:
                await session.delete(row)

    async def interrupt_backtest_runs(self) -> None:
        async with self.sessions() as session, session.begin():
            rows = list(
                (
                    await session.scalars(
                        select(BacktestRunRow).where(
                            BacktestRunRow.status.in_(("queued", "running"))
                        )
                    )
                ).all()
            )
            for row in rows:
                row.status = "interrupted"
                row.error = "service restarted before completion"
                row.updated_at = datetime.now(timezone.utc)


class MemoryJournal:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.signals: list[dict[str, Any]] = []
        self.trade_signals: list[dict[str, Any]] = []
        self.timeline: list[tuple[str, Any]] = []
        self.intents: list[OrderRequest] = []
        self.orders: list[BrokerOrder] = []
        self.trade_summaries: list[dict[str, Any]] = []
        self.risk_snapshots: list[dict[str, Any]] = []

    async def event(self, kind: str, message: str, details: dict | None = None) -> None:
        self.events.append({"kind": kind, "message": message, "details": details or {}})

    async def signal(self, signal: Signal, accepted: bool, reason: str = "") -> None:
        self.signals.append({"signal": signal, "accepted": accepted, "reason": reason})

    async def trade_signal(self, signal: TradeSignal) -> None:
        self.trade_signals.append({"signal": signal, "status": "accepted"})
        self.timeline.append(("trade_signal", signal.intent_id))

    async def trade_signal_status(self, intent_id: UUID, status: str) -> None:
        for item in self.trade_signals:
            if item["signal"].intent_id == intent_id:
                item["status"] = status
                return

    async def trade_signal_by_intent(self, intent_id: UUID) -> TradeSignal | None:
        for item in self.trade_signals:
            if item["signal"].intent_id == intent_id:
                return item["signal"]
        return None

    async def trade_signal_for_position(
        self, symbol: str, quantity: int
    ) -> TradeSignal | None:
        candidates = [
            item
            for item in self.trade_signals
            if item["signal"].symbol == symbol
            and item["signal"].action is OrderSide.BUY
            and item["status"] in {"accepted", "executed"}
        ]
        if not candidates:
            return None
        candidate = max(candidates, key=lambda item: item["signal"].decision_at)
        latest_orders = {order.order_id: order for order in self.orders}
        net_filled = sum(
            (
                order.filled_quantity
                if order.side is OrderSide.BUY
                else -order.filled_quantity
            )
            for order in latest_orders.values()
            if order.symbol == symbol
        )
        persisted_match = net_filled == quantity
        crash_window_match = (
            net_filled == 0
            and candidate["status"] == "accepted"
            and quantity <= candidate["signal"].quantity
        )
        return candidate["signal"] if persisted_match or crash_window_match else None

    async def recover_trade_signal_statuses(self) -> dict[str, int]:
        recovered = {"executed": 0, "failed": 0}
        latest_orders = {order.order_id: order for order in self.orders}
        for item in self.trade_signals:
            if item["status"] != "accepted":
                continue
            signal = item["signal"]
            filled = sum(
                order.filled_quantity
                for order in latest_orders.values()
                if order.intent_id == signal.intent_id
            )
            status = "executed" if filled >= signal.quantity else "failed"
            item["status"] = status
            recovered[status] += 1
        return recovered

    async def order_intent(self, request: OrderRequest) -> None:
        self.intents.append(request)
        self.timeline.append(("order_intent", request.intent_id))

    async def broker_order(self, order: BrokerOrder) -> None:
        self.orders.append(order)

    async def trade_summary(self, summary: dict) -> None:
        self.trade_summaries.append(summary)

    async def risk_snapshot(self, account: AccountSnapshot, halted: bool) -> None:
        self.risk_snapshots.append({"account": account, "halted": halted})

    async def today_realized_pnl_and_trades(self, trading_date: date) -> tuple[Decimal, int]:
        from .config import NY_TZ

        local_start = datetime.combine(trading_date, datetime.min.time(), NY_TZ)
        local_end = local_start + timedelta(days=1)
        start_utc = local_start.astimezone(timezone.utc)
        end_utc = local_end.astimezone(timezone.utc)
        matching = [
            s for s in self.trade_summaries
            if start_utc <= s.get("exit_at", datetime.min.replace(tzinfo=timezone.utc)) < end_utc
        ]
        realized = sum((s.get("pnl", Decimal(0)) for s in matching), Decimal(0))
        return realized, len(matching)


class ParquetMarketStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def write_bars(self, bars: list[Bar], timeframe: str) -> Path | None:
        if not bars:
            return None
        by_day: dict[tuple[str, date], list[Bar]] = {}
        for bar in bars:
            by_day.setdefault((bar.symbol, bar.start.date()), []).append(bar)
        last_path: Path | None = None
        for (symbol, trading_date), day_bars in by_day.items():
            directory = self.root / "bars" / f"symbol={symbol}" / f"date={trading_date.isoformat()}"
            path = directory / f"{timeframe}.parquet"
            existing = self.read_bars(path) if path.exists() else []
            merged = {bar.start: bar for bar in [*existing, *day_bars]}
            ordered = [merged[key] for key in sorted(merged)]
            records = [self._bar_record(bar) for bar in ordered]
            self._atomic_parquet(path, pa.Table.from_pylist(records))
            self._write_manifest(path, records)
            last_path = path
        return last_path

    def write_records(
        self, category: str, symbol: str, trading_date: date, records: list[dict[str, Any]]
    ) -> Path | None:
        if not records:
            return None
        directory = self.root / category / f"symbol={symbol}" / f"date={trading_date.isoformat()}"
        path = directory / "data.parquet"
        existing = pq.ParquetFile(path).read().to_pylist() if path.exists() else []
        combined = [*existing, *records]
        if combined and "timestamp" in combined[0]:
            deduplicated = {
                f"{row.get('symbol', symbol)}:{row['timestamp']}": row for row in combined
            }
            combined = [deduplicated[key] for key in sorted(deduplicated)]
        elif combined and "captured_at" in combined[0]:
            deduplicated = {
                f"{row.get('symbol', symbol)}:{row['captured_at']}": row for row in combined
            }
            combined = [deduplicated[key] for key in sorted(deduplicated)]
        elif combined and "symbol" in combined[0]:
            deduplicated = {str(row["symbol"]): row for row in combined}
            combined = [deduplicated[key] for key in sorted(deduplicated)]
        self._atomic_parquet(path, pa.Table.from_pylist(combined))
        self._write_manifest(path, combined)
        return path

    @staticmethod
    def read_bars(path: Path) -> list[Bar]:
        table = pq.ParquetFile(path).read()
        result: list[Bar] = []
        for row in table.to_pylist():
            result.append(
                Bar(
                    symbol=row["symbol"],
                    start=row["start"],
                    end=row["end"],
                    open=Decimal(row["open"]),
                    high=Decimal(row["high"]),
                    low=Decimal(row["low"]),
                    close=Decimal(row["close"]),
                    volume=row["volume"],
                    turnover=Decimal(row["turnover"]),
                    complete=row["complete"],
                )
            )
        return result

    @classmethod
    def read_bars_path(cls, path: Path, timeframe: str | None = None) -> list[Bar]:
        """Read one Parquet file or recursively merge partitioned bar files."""
        if path.is_file():
            return cls.read_bars(path)
        pattern = f"{timeframe}.parquet" if timeframe else "*.parquet"
        merged: dict[tuple[str, datetime], Bar] = {}
        for candidate in sorted(path.rglob(pattern)):
            for bar in cls.read_bars(candidate):
                merged[(bar.symbol, bar.start)] = bar
        return [merged[key] for key in sorted(merged, key=lambda item: (item[1], item[0]))]

    @staticmethod
    def _bar_record(bar: Bar) -> dict[str, Any]:
        record = asdict(bar)
        for key in ("open", "high", "low", "close", "turnover"):
            record[key] = str(record[key])
        return record

    @staticmethod
    def _atomic_parquet(path: Path, table: pa.Table) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        pq.write_table(table, temporary, compression="zstd")
        temporary.replace(path)

    @staticmethod
    def _write_manifest(path: Path, records: list[dict[str, Any]]) -> None:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        manifest = {
            "path": path.name,
            "rows": len(records),
            "sha256": digest,
            "written_at": datetime.now(timezone.utc).isoformat(),
        }
        temporary = path.with_suffix(".manifest.json.tmp")
        final = path.with_suffix(".manifest.json")
        temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(final)
