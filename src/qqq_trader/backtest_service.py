from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import asdict
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import uuid4

from .backtest import EventDrivenBacktester, load_option_frames
from .config import NY_TZ, Settings
from .domain import TradingMode
from .indicators import overlay_series
from .market_hours import indicator_session_bars
from .persistence import MySQLJournal, ParquetMarketStore
from .policy import RULES
from .reporting import generate_price_chart
from .risk import ContractSelector, RiskEngine

_LOG = logging.getLogger(__name__)
_HEAVY_RESULT_KEYS = ("price_series", "equity_curve", "chart_svg")


_SETTINGS_SUMMARY_KEYS = (
    "strategy_mode",
    "volatility_filter_enabled", "volatility_symbol",
    "volatility_lookback_days", "volatility_max_staleness_minutes",
    "volatility_vix_macd_rising_block",
    "max_premium_fraction", "max_contracts", "max_trades_per_day",
    "cooldown_minutes", "option_stop_loss_pct",
    "tp1_profit_pct", "tp2_profit_pct", "stale_minutes",
    "phase_collect_start", "phase_collect_end",
    "phase_opening_end", "phase_main_end",
    "timed_opening_last_signal", "timed_opening_flat",
    "timed_boll_period", "timed_boll_stddev",
    "timed_macd_fast", "timed_macd_slow", "timed_macd_signal",
    "timed_rsi_period", "timed_volume_ratio",
    "trend_entry_end",
    "trend_ema_fast", "trend_ema_slow",
    "trend_breakout_confirm_bars", "trend_max_vwap_crosses",
)


def _backtest_settings_summary(settings: Settings) -> dict[str, Any]:
    dumped = settings.model_dump(mode="json")
    return {k: dumped[k] for k in _SETTINGS_SUMMARY_KEYS if k in dumped}


def _load_vix_intraday(vol_root) -> list:
    one_min = vol_root / "1m.parquet"
    if one_min.exists():
        return ParquetMarketStore.read_bars(one_min)
    five_min = vol_root / "5m.parquet"
    if five_min.exists():
        return ParquetMarketStore.read_bars(five_min)
    return []


class BacktestCancelled(Exception):
    pass


class BacktestService:
    def __init__(self, settings: Settings, journal: MySQLJournal) -> None:
        self.settings = settings
        self.journal = journal
        self.jobs: dict[str, dict[str, Any]] = {}
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.worker: asyncio.Task | None = None
        self.cancelled: set[str] = set()
        self._cancel_events: dict[str, threading.Event] = {}

    async def start(self) -> None:
        interrupt = getattr(self.journal, "interrupt_backtest_runs", None)
        if interrupt is not None:
            await interrupt()
        loader = getattr(self.journal, "list_backtest_runs", None)
        if loader is not None:
            try:
                for row in await loader():
                    self.jobs[row.id] = {
                        "id": row.id,
                        "created_at": row.created_at.isoformat(),
                        "updated_at": row.updated_at.isoformat(),
                        "status": row.status,
                        "progress": row.progress,
                        "request": row.request,
                        "result": None,
                        "error": row.error,
                    }
            except Exception:
                _LOG.exception("failed to load backtest history; continuing without it")
        self.worker = asyncio.create_task(self._worker())

    def job_summaries(self) -> list[dict[str, Any]]:
        jobs = sorted(self.jobs.values(), key=lambda item: item["created_at"], reverse=True)
        return [self._summary(job) for job in jobs]

    async def job_detail(self, job_id: str) -> dict[str, Any] | None:
        job = self.jobs.get(job_id)
        if job is None:
            return None
        if job.get("result") is None and job.get("status") == "completed":
            loader = getattr(self.journal, "get_backtest_run", None)
            if loader is not None:
                try:
                    row = await loader(job_id)
                except Exception:
                    _LOG.exception("failed to load backtest result | %s", job_id)
                    row = None
                if row is not None:
                    job["result"] = row.result
                    job["error"] = row.error
        return job

    @staticmethod
    def _summary(job: dict[str, Any]) -> dict[str, Any]:
        result = job.get("result")
        if not isinstance(result, dict):
            return job
        return {
            **job,
            "result": {
                key: value
                for key, value in result.items()
                if key not in _HEAVY_RESULT_KEYS
            },
        }

    async def stop(self) -> None:
        if self.worker is not None:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)

    def availability(self) -> list[dict[str, Any]]:
        root = self.settings.data_dir
        symbol = self.settings.underlying_symbol
        dates: set[str] = set()
        for category, item_symbol in (
            ("bars", symbol),
            ("candidate_option_quotes", symbol),
            ("bars", self.settings.volatility_symbol),
        ):
            directory = root / category / f"symbol={item_symbol}"
            if directory.exists():
                dates.update(
                    path.name.removeprefix("date=")
                    for path in directory.glob("date=*")
                    if path.is_dir()
                )
        result = []
        for value in sorted(dates, reverse=True):
            result.append(
                {
                    "date": value,
                    "bars": (
                        root / "bars" / f"symbol={symbol}" / f"date={value}" / "1m.parquet"
                    ).exists(),
                    "options": (
                        root
                        / "candidate_option_quotes"
                        / f"symbol={symbol}"
                        / f"date={value}"
                        / "data.parquet"
                    ).exists(),
                    "volatility_intraday": (
                        (
                            root
                            / "bars"
                            / f"symbol={self.settings.volatility_symbol}"
                            / f"date={value}"
                            / "1m.parquet"
                        ).exists()
                        or (
                            root
                            / "bars"
                            / f"symbol={self.settings.volatility_symbol}"
                            / f"date={value}"
                            / "5m.parquet"
                        ).exists()
                    ),
                }
            )
        return result

    async def submit(self, request: dict[str, Any]) -> dict[str, Any]:
        start = date.fromisoformat(request["start_date"])
        end = date.fromisoformat(request["end_date"])
        if end < start:
            raise ValueError("end_date must not be earlier than start_date")
        if not request.get("config_version"):
            loader = getattr(self.journal, "active_config", None)
            active = await loader() if loader is not None else None
            if active is not None:
                request["config_version"] = active.id
        job_id = str(uuid4())
        now = datetime.now(timezone.utc).isoformat()
        job = {
            "id": job_id,
            "created_at": now,
            "updated_at": now,
            "status": "queued",
            "progress": 0,
            "request": request,
            "result": None,
            "error": None,
        }
        self.jobs[job_id] = job
        await self._persist(job)
        await self.queue.put(job_id)
        return job

    async def cancel(self, job_id: str) -> dict[str, Any]:
        job = self.jobs[job_id]
        if job["status"] not in {"queued", "running"}:
            return job
        self.cancelled.add(job_id)
        cancel_event = self._cancel_events.get(job_id)
        if cancel_event is not None:
            cancel_event.set()
        job.update(status="cancelled", updated_at=datetime.now(timezone.utc).isoformat())
        await self._persist(job)
        return job

    async def delete(self, job_id: str) -> None:
        self.jobs.pop(job_id, None)
        deleter = getattr(self.journal, "delete_backtest_run", None)
        if deleter is not None:
            await deleter(job_id)

    async def _worker(self) -> None:
        while True:
            job_id = await self.queue.get()
            job = self.jobs[job_id]
            try:
                if job_id in self.cancelled:
                    continue
                job.update(
                    status="running",
                    progress=10,
                    updated_at=datetime.now(timezone.utc).isoformat(),
                )
                await self._persist(job)
                run_request = dict(job["request"])
                version = run_request.get("config_version")
                if version:
                    row = await self.journal.get_config_version(int(version))
                    if row is None:
                        raise ValueError(f"configuration v{version} does not exist")
                    run_request["_config_values"] = row.values
                custom_params = run_request.pop("params", None)
                if custom_params:
                    base = run_request.get("_config_values", {})
                    run_request["_config_values"] = {**base, **custom_params}
                mode = str(run_request.get("strategy_mode") or "hybrid").lower()
                if mode != "hybrid":
                    mode = "hybrid"
                run_request["strategy_mode"] = mode
                run_request["_strategy_mode"] = mode
                cancel_event = threading.Event()
                self._cancel_events[job_id] = cancel_event
                try:
                    result = await asyncio.to_thread(self._run, run_request, cancel_event)
                finally:
                    self._cancel_events.pop(job_id, None)
                if job_id in self.cancelled:
                    continue
                job.update(
                    status="completed",
                    progress=100,
                    result=result,
                    updated_at=datetime.now(timezone.utc).isoformat(),
                )
            except BacktestCancelled:
                pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                job.update(
                    status="failed",
                    error=str(exc),
                    updated_at=datetime.now(timezone.utc).isoformat(),
                )
            finally:
                await self._persist(job)
                self.queue.task_done()

    def _run(self, request: dict[str, Any], cancel_event: threading.Event) -> dict[str, Any]:
        import logging
        log = logging.getLogger(__name__)
        start = date.fromisoformat(request["start_date"])
        end = date.fromisoformat(request["end_date"])
        overrides: dict[str, Any] = request.get("_config_values", {})
        overrides["trading_mode"] = TradingMode.REPLAY
        mode = str(request.get("strategy_mode") or request.get("_strategy_mode") or "hybrid").lower()
        if mode != "hybrid":
            mode = "hybrid"
        overrides["strategy_mode"] = mode
        base = self.settings.model_dump()
        base.update(overrides)
        settings = Settings.model_validate(base)
        log.info("backtest starting | %s to %s | mode=%s", start, end, mode)
        bars = []
        frames = {}
        volatility = []
        current = start
        while current <= end:
            value = current.isoformat()
            bar_path = (
                settings.data_dir
                / "bars"
                / f"symbol={settings.underlying_symbol}"
                / f"date={value}"
                / "1m.parquet"
            )
            option_path = (
                settings.data_dir
                / "candidate_option_quotes"
                / f"symbol={settings.underlying_symbol}"
                / f"date={value}"
                / "data.parquet"
            )
            vol_root = (
                settings.data_dir
                / "bars"
                / f"symbol={settings.volatility_symbol}"
                / f"date={value}"
            )
            if bar_path.exists():
                bars.extend(ParquetMarketStore.read_bars(bar_path))
            if option_path.exists():
                frames.update(load_option_frames(option_path))
            volatility.extend(_load_vix_intraday(vol_root))
            current = date.fromordinal(current.toordinal() + 1)

        if not bars:
            raise ValueError("no QQQ 1-minute bars exist in the selected date range")
        if cancel_event.is_set():
            raise BacktestCancelled()
        tester = EventDrivenBacktester(
            settings,
            None,
            ContractSelector(),
            RiskEngine(settings),
        )
        result = tester.run(
            bars,
            frames,
            Decimal(str(request.get("starting_equity", "10000"))),
            volatility,
            [],
            cancel_check=cancel_event.is_set,
            trade_start=start,
        )
        wins = sum(1 for trade in result.trades if trade.pnl > 0)
        net = result.ending_equity - result.starting_equity
        realized_equity = result.starting_equity
        realized_peak = realized_equity
        realized_max_drawdown = Decimal(0)
        gross_profit = sum((t.pnl for t in result.trades if t.pnl > 0), Decimal(0))
        gross_loss = abs(sum((t.pnl for t in result.trades if t.pnl < 0), Decimal(0)))
        for trade in result.trades:
            realized_equity += trade.pnl
            realized_peak = max(realized_peak, realized_equity)
            realized_max_drawdown = min(
                realized_max_drawdown,
                realized_equity - realized_peak,
            )
        peak = result.starting_equity
        max_drawdown = Decimal(0)
        equity_curve = []
        for point in result.equity_curve:
            peak = max(peak, point.equity)
            max_drawdown = min(max_drawdown, point.equity - peak)
            equity_curve.append(
                {
                    "time": point.timestamp.isoformat(),
                    "equity": str(point.equity),
                    "realized_pnl": str(point.realized_pnl),
                    "unrealized_pnl": str(point.unrealized_pnl),
                    "position_symbol": point.position_symbol,
                }
            )
        chart_svg = generate_price_chart(
            bars,
            [(t.entry_at.isoformat(), t.exit_at.isoformat()) for t in result.trades],
        )
        rules = settings.rules
        price_series: list[dict[str, Any]] = []
        day_bars: list = []
        current_day = None
        for bar in indicator_session_bars(sorted(bars, key=lambda item: item.start)):
            bar_date = bar.start.astimezone(NY_TZ).date()
            if current_day is not None and bar_date != current_day:
                price_series.extend(
                    overlay_series(
                        day_bars,
                        ema_fast=rules.trend_ema_fast,
                        ema_slow=rules.trend_ema_slow,
                        boll_period=rules.timed_boll_period,
                        boll_std=rules.timed_boll_stddev,
                        macd_fast=rules.timed_macd_fast,
                        macd_slow=rules.timed_macd_slow,
                        macd_signal=rules.timed_macd_signal,
                        timestamp="end",
                    )
                )
                day_bars = []
            current_day = bar_date
            day_bars.append(bar)
        if day_bars:
            price_series.extend(
                overlay_series(
                    day_bars,
                    ema_fast=rules.trend_ema_fast,
                    ema_slow=rules.trend_ema_slow,
                    boll_period=rules.timed_boll_period,
                    boll_std=rules.timed_boll_stddev,
                    macd_fast=rules.timed_macd_fast,
                    macd_slow=rules.timed_macd_slow,
                    macd_signal=rules.timed_macd_signal,
                    timestamp="end",
                )
            )
        return {
            "starting_equity": str(result.starting_equity),
            "ending_equity": str(result.ending_equity),
            "net_pnl": str(net),
            "return_rate": str(net / result.starting_equity) if result.starting_equity else "0",
            "signals": result.signals,
            "trade_count": len(result.trades),
            "win_rate": str(Decimal(wins) / Decimal(len(result.trades))) if result.trades else "0",
            "profit_factor": str(gross_profit / gross_loss) if gross_loss else None,
            "max_drawdown": str(max_drawdown),
            "realized_max_drawdown": str(realized_max_drawdown),
            "equity_curve": equity_curve,
            "rejected": result.rejected,
            "option_data_complete": result.option_data_complete,
            "volatility_data_complete": result.volatility_data_complete,
            "volatility_regimes": result.volatility_regimes,
            "signal_records": result.signal_records,
            "trades": [self._trade_payload(trade) for trade in result.trades],
            "chart_svg": chart_svg,
            "price_series": price_series,
            "indicator_timeframe": "1m",
            "indicator_periods": {
                "boll": [
                    RULES.timed_boll_period,
                    str(RULES.timed_boll_stddev),
                ],
                "macd": [
                    RULES.timed_macd_fast,
                    RULES.timed_macd_slow,
                    RULES.timed_macd_signal,
                ],
                "rsi": RULES.timed_rsi_period,
                "volume_lookback": RULES.timed_volume_lookback,
            },
            "settings_used": _backtest_settings_summary(settings),
        }

    @staticmethod
    def _trade_payload(trade) -> dict[str, Any]:
        def convert(value):
            if hasattr(value, "value"):
                return value.value
            if hasattr(value, "isoformat"):
                return value.isoformat()
            if isinstance(value, Decimal):
                return str(value)
            if isinstance(value, dict):
                return {key: convert(item) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [convert(item) for item in value]
            return value

        return convert(asdict(trade))

    async def _persist(self, job: dict[str, Any]) -> None:
        saver = getattr(self.journal, "save_backtest_run", None)
        if saver is None:
            return
        result = job.get("result")
        if isinstance(result, dict):
            result = {key: value for key, value in result.items() if key != "chart_svg"}
        await saver(
            {
                **job,
                "result": result,
                "created_at": datetime.fromisoformat(job["created_at"]),
                "updated_at": datetime.fromisoformat(job["updated_at"]),
            }
        )
