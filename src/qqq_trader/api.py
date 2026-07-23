from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import CollectorRegistry, Gauge, generate_latest
from pydantic import BaseModel, Field, ValidationError

from .backtest_service import BacktestService
from .config import NY_TZ, Settings
from .configuration import editable_values, with_editable_values
from .domain import SystemState
from .engine import TradingEngine
from .persistence import MySQLJournal


class ConfigUpdate(BaseModel):
    expected_version: int = Field(ge=0)
    values: dict[str, Any]


class BacktestCreate(BaseModel):
    start_date: date
    end_date: date
    starting_equity: Decimal = Field(default=Decimal("100000"), gt=0)
    config_version: int | None = Field(default=None, ge=1)
    params: dict[str, Any] | None = Field(default=None)


def _decimal(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _decimal(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decimal(item) for item in value]
    return value


def _trade(row: Any) -> dict[str, Any]:
    return _decimal(
        {
            "id": row.id,
            "symbol": row.symbol,
            "direction": row.direction,
            "quantity": row.quantity,
            "entry_price": row.entry_price,
            "exit_price": row.exit_price,
            "pnl": row.pnl,
            "fees": row.fees,
            "entry_at": row.entry_at,
            "exit_at": row.exit_at,
            "exit_reason": row.exit_reason,
            "slippage": row.slippage,
            "mae": row.mae,
            "mfe": row.mfe,
        }
    )


def create_app(
    engine: TradingEngine,
    journal: MySQLJournal,
    settings: Settings | None = None,
) -> FastAPI:
    backtests = BacktestService(settings, journal) if settings is not None else None

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if backtests is not None:
            await backtests.start()
        try:
            yield
        finally:
            if backtests is not None:
                await backtests.stop()

    app = FastAPI(
        title="QQQ 0DTE Trader",
        version="0.2.0",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def request_id(request: Request, call_next):
        request.state.request_id = request.headers.get("x-request-id", str(uuid4()))
        response = await call_next(request)
        response.headers["x-request-id"] = request.state.request_id
        return response

    _PUBLIC_PATHS = {"/health/live", "/health/ready", "/metrics"}

    @app.middleware("http")
    async def token_auth(request: Request, call_next):
        token = settings.api_token.get_secret_value() if settings else ""
        if token and request.url.path not in _PUBLIC_PATHS:
            provided = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
            if provided != token:
                return JSONResponse(
                    status_code=401,
                    content={
                        "code": "unauthorized",
                        "message": "missing or invalid API token",
                        "field_errors": [],
                        "request_id": request.state.request_id,
                    },
                )
        return await call_next(request)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "code": f"http_{exc.status_code}",
                "message": str(exc.detail),
                "field_errors": [],
                "request_id": request.state.request_id,
            },
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "code": "validation_error",
                "message": "request validation failed",
                "field_errors": jsonable_encoder(exc.errors()),
                "request_id": request.state.request_id,
            },
        )

    registry = CollectorRegistry()
    ready_gauge = Gauge(
        "qqq_trader_ready", "Whether the engine may manage trades", registry=registry
    )
    pnl_gauge = Gauge("qqq_trader_realized_pnl_usd", "Session realized PnL", registry=registry)
    trades_gauge = Gauge("qqq_trader_trades_total", "Session entries", registry=registry)

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "alive"}

    @app.get("/health/ready")
    async def ready(response: Response) -> dict:
        database_ready = await journal.ping()
        engine_ready = engine.state in {
            SystemState.READY,
            SystemState.ENTRY_PENDING,
            SystemState.OPEN,
            SystemState.EXIT_PENDING,
        }
        if not database_ready or not engine_ready:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "ready": database_ready and engine_ready,
            "database": database_ready,
            **engine.status(),
        }

    @app.get("/status")
    @app.get("/api/v1/status")
    async def engine_status() -> dict:
        return engine.status()

    @app.get("/metrics")
    async def metrics() -> Response:
        ready_gauge.set(
            1
            if engine.state
            in {
                SystemState.READY,
                SystemState.ENTRY_PENDING,
                SystemState.OPEN,
                SystemState.EXIT_PENDING,
            }
            else 0
        )
        pnl_gauge.set(float(engine.realized_pnl))
        trades_gauge.set(engine.trades_today)
        return Response(generate_latest(registry), media_type="text/plain; version=0.0.4")

    @app.get("/api/v1/trades")
    async def trades(
        start_date: date | None = None,
        end_date: date | None = None,
        symbol: str | None = None,
        direction: str | None = Query(default=None, pattern="^(call|put)$"),
        pnl_sign: str | None = Query(default=None, pattern="^(profit|loss|flat)$"),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=20, ge=1, le=200),
    ) -> dict[str, Any]:
        loader = getattr(journal, "list_trades", None)
        if loader is None:
            return {"items": [], "total": 0, "page": page, "page_size": page_size}
        start = (
            datetime.combine(start_date, time.min, NY_TZ).astimezone(timezone.utc)
            if start_date
            else None
        )
        end = (
            datetime.combine(end_date + timedelta(days=1), time.min, NY_TZ).astimezone(timezone.utc)
            if end_date
            else None
        )
        rows, total = await loader(
            start=start,
            end=end,
            symbol=symbol,
            direction=direction,
            pnl_sign=pnl_sign,
            page=page,
            page_size=page_size,
        )
        return {
            "items": [_trade(row) for row in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    @app.get("/api/v1/trades/{trade_id}")
    async def trade_detail(trade_id: int) -> dict[str, Any]:
        loader = getattr(journal, "get_trade", None)
        row = await loader(trade_id) if loader is not None else None
        if row is None:
            raise HTTPException(404, "trade not found")
        return _trade(row)

    @app.get("/api/v1/signals")
    async def decision_signals(
        start_date: date | None = None,
        end_date: date | None = None,
        action: str | None = Query(default=None, pattern="^(buy|sell)$"),
        signal_status: str | None = Query(
            default=None, alias="status", pattern="^(accepted|rejected|executed|failed)$"
        ),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=50, ge=1, le=200),
    ) -> dict[str, Any]:
        loader = getattr(journal, "list_decision_signals", None)
        if loader is None:
            return {"items": [], "total": 0, "page": page, "page_size": page_size}
        start = (
            datetime.combine(start_date, time.min, NY_TZ).astimezone(timezone.utc)
            if start_date
            else None
        )
        end = (
            datetime.combine(end_date + timedelta(days=1), time.min, NY_TZ).astimezone(timezone.utc)
            if end_date
            else None
        )
        rows, total = await loader(
            start=start,
            end=end,
            action=action,
            status=signal_status,
            page=page,
            page_size=page_size,
        )
        return {
            "items": [_decimal(row) for row in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    @app.get("/api/v1/reports")
    async def reports() -> list[dict[str, Any]]:
        if settings is None:
            return []
        result = []
        if settings.report_dir.exists():
            for directory in sorted(settings.report_dir.glob("????-??-??"), reverse=True):
                report = directory / "report.json"
                if report.exists():
                    payload = json.loads(report.read_text(encoding="utf-8"))
                    result.append(
                        {
                            "date": directory.name,
                            "generated_at": payload.get("generated_at"),
                            "metrics": payload.get("metrics", {}),
                        }
                    )
        return result

    @app.get("/api/v1/reports/{trading_date}")
    async def report_detail(trading_date: date) -> dict[str, Any]:
        if settings is None:
            raise HTTPException(404, "report not found")
        path = settings.report_dir / trading_date.isoformat() / "report.json"
        if not path.exists():
            raise HTTPException(404, "report not found")
        return json.loads(path.read_text(encoding="utf-8"))

    @app.get("/api/v1/reports/{trading_date}/chart")
    async def report_chart(trading_date: date) -> FileResponse:
        if settings is None:
            raise HTTPException(404, "chart not found")
        path = settings.report_dir / trading_date.isoformat() / "qqq.svg"
        if not path.exists():
            raise HTTPException(404, "chart not found")
        return FileResponse(path, media_type="image/svg+xml")

    @app.get("/api/v1/config")
    async def config() -> dict[str, Any]:
        active = await journal.active_config() if hasattr(journal, "active_config") else None
        current = active.values if active is not None else editable_values(engine.settings)
        return {
            "version": active.id if active is not None else 0,
            "values": current,
            "engine_version": engine.config_version,
            "position_version": engine.position_config_version,
            "pending_version": engine.pending_config_version,
        }

    @app.put("/api/v1/config")
    async def update_config(command: ConfigUpdate) -> dict[str, Any]:
        if not hasattr(journal, "save_config"):
            raise HTTPException(501, "configuration persistence is unavailable")
        active = await journal.active_config()
        actual_version = active.id if active is not None else 0
        if command.expected_version != actual_version:
            raise HTTPException(409, f"configuration changed; current version is {actual_version}")
        try:
            updated = with_editable_values(engine.settings, command.values)
        except (ValidationError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc
        row = await journal.save_config(editable_values(updated))
        applied = await engine.apply_settings(updated, row.id)
        return {"version": row.id, "values": row.values, "applied": applied, "pending": not applied}

    @app.get("/api/v1/config/versions")
    async def config_versions() -> list[dict[str, Any]]:
        if not hasattr(journal, "config_versions"):
            return []
        return [
            {
                "version": row.id,
                "created_at": row.created_at.isoformat(),
                "active": row.active,
                "values": row.values,
            }
            for row in await journal.config_versions()
        ]

    @app.get("/api/v1/market-data/availability")
    async def availability() -> list[dict[str, Any]]:
        return backtests.availability() if backtests is not None else []

    @app.post("/api/v1/backtests", status_code=202)
    async def create_backtest(command: BacktestCreate) -> dict[str, Any]:
        if backtests is None:
            raise HTTPException(501, "backtest service is unavailable")
        try:
            return await backtests.submit(command.model_dump(mode="json"))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/v1/backtests")
    async def list_backtests() -> list[dict[str, Any]]:
        return (
            sorted(backtests.jobs.values(), key=lambda item: item["created_at"], reverse=True)
            if backtests is not None
            else []
        )

    @app.get("/api/v1/backtests/{job_id}")
    async def backtest_detail(job_id: str) -> dict[str, Any]:
        if backtests is None or job_id not in backtests.jobs:
            raise HTTPException(404, "backtest not found")
        return backtests.jobs[job_id]

    @app.get("/api/v1/backtests/{job_id}/chart")
    async def backtest_chart(job_id: str) -> Response:
        if backtests is None or job_id not in backtests.jobs:
            raise HTTPException(404, "backtest not found")
        job = backtests.jobs[job_id]
        svg = (job.get("result") or {}).get("chart_svg", "")
        if not svg:
            raise HTTPException(404, "chart not available")
        return Response(content=svg, media_type="image/svg+xml")

    @app.delete("/api/v1/backtests/{job_id}")
    async def cancel_or_delete_backtest(job_id: str) -> dict[str, Any]:
        if backtests is None or job_id not in backtests.jobs:
            raise HTTPException(404, "backtest not found")
        job = backtests.jobs[job_id]
        if job["status"] in {"queued", "running"}:
            return await backtests.cancel(job_id)
        await backtests.delete(job_id)
        return {"deleted": True}

    @app.get("/api/v1/events")
    async def events() -> StreamingResponse:
        async def stream():
            previous = None
            while True:
                payload = json.dumps(engine.status(), ensure_ascii=False)
                if payload != previous:
                    yield f"data: {payload}\n\n"
                    previous = payload
                else:
                    yield ": keepalive\n\n"
                await asyncio.sleep(2)

        return StreamingResponse(stream(), media_type="text/event-stream")

    frontend = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if frontend.exists():
        app.mount("/assets", StaticFiles(directory=frontend / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str):
            candidate = frontend / path
            return FileResponse(candidate if candidate.is_file() else frontend / "index.html")

    return app
