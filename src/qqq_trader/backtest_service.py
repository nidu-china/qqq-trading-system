from __future__ import annotations

import asyncio
import threading
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import uuid4

from .backtest import EventDrivenBacktester, load_option_frames
from .config import Settings
from .domain import TradingMode
from .indicators import bollinger_bands, macd_histogram
from .persistence import MySQLJournal, ParquetMarketStore
from .policy import RULES
from .reporting import generate_price_chart
from .risk import ContractSelector, RiskEngine


_SETTINGS_SUMMARY_KEYS = (
    "strategy_mode",
    "volatility_filter_enabled", "volatility_symbol",
    "volatility_lookback_days", "volatility_max_staleness_minutes",
    "volatility_risk_off_percentile", "volatility_recovery_percentile",
    "volatility_rise_5m", "volatility_rise_15m",
    "volatility_fall_5m", "volatility_fall_15m",
    "volatility_shock_5m", "volatility_shock_15m",
    "max_premium_fraction", "max_contracts", "max_trades_per_day",
    "cooldown_minutes", "option_stop_loss_pct",
    "tp1_profit_pct", "tp2_profit_pct", "stale_minutes",
    "timed_opening_start", "timed_opening_last_signal",
    "timed_opening_flat", "timed_main_start", "timed_main_last_signal",
    "timed_boll_period", "timed_boll_stddev",
    "timed_macd_fast", "timed_macd_slow", "timed_macd_signal",
    "timed_rsi_period", "timed_volume_ratio",
    "trend_or_start", "trend_or_end", "trend_entry_end",
    "trend_ema_fast", "trend_ema_slow",
    "trend_breakout_confirm_bars", "trend_max_vwap_crosses",
)


def _backtest_settings_summary(settings: Settings) -> dict[str, Any]:
    dumped = settings.model_dump(mode="json")
    return {k: dumped[k] for k in _SETTINGS_SUMMARY_KEYS if k in dumped}


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
            for row in await loader():
                self.jobs[row.id] = {
                    "id": row.id,
                    "created_at": row.created_at.isoformat(),
                    "updated_at": row.updated_at.isoformat(),
                    "status": row.status,
                    "progress": row.progress,
                    "request": row.request,
                    "result": row.result,
                    "error": row.error,
                }
        self.worker = asyncio.create_task(self._worker())

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
                        root
                        / "bars"
                        / f"symbol={self.settings.volatility_symbol}"
                        / f"date={value}"
                        / "5m.parquet"
                    ).exists(),
                    "volatility_daily": (
                        root
                        / "bars"
                        / f"symbol={self.settings.volatility_symbol}"
                        / f"date={value}"
                        / "day.parquet"
                    ).exists(),
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
                strategy_mode = run_request.pop("strategy_mode", None)
                if strategy_mode:
                    run_request["_strategy_mode"] = strategy_mode
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
        strategy_mode = request.get("_strategy_mode")
        if strategy_mode:
            overrides["strategy_mode"] = strategy_mode
        base = self.settings.model_dump()
        base.update(overrides)
        settings = Settings.model_validate(base)
        log.info("backtest starting | %s to %s", start, end)
        bars = []
        frames = {}
        volatility = []
        volatility_daily = []
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
            if (vol_root / "5m.parquet").exists():
                volatility.extend(ParquetMarketStore.read_bars(vol_root / "5m.parquet"))
            if (vol_root / "day.parquet").exists():
                volatility_daily.extend(ParquetMarketStore.read_bars(vol_root / "day.parquet"))
            current = date.fromordinal(current.toordinal() + 1)

        vol_lookback = timedelta(days=int(settings.volatility_lookback_days * 3))
        vol_start = start - vol_lookback
        vol_cursor = vol_start
        while vol_cursor < start:
            vol_day_root = (
                settings.data_dir
                / "bars"
                / f"symbol={settings.volatility_symbol}"
                / f"date={vol_cursor.isoformat()}"
            )
            if (vol_day_root / "5m.parquet").exists():
                volatility.extend(ParquetMarketStore.read_bars(vol_day_root / "5m.parquet"))
            if (vol_day_root / "day.parquet").exists():
                volatility_daily.extend(ParquetMarketStore.read_bars(vol_day_root / "day.parquet"))
            vol_cursor = date.fromordinal(vol_cursor.toordinal() + 1)
        if not bars:
            raise ValueError("no QQQ 1-minute bars exist in the selected date range")
        warmup_bars = []
        warmup_cursor = start - timedelta(days=35)
        while warmup_cursor < start:
            warmup_path = (
                settings.data_dir
                / "bars"
                / f"symbol={settings.underlying_symbol}"
                / f"date={warmup_cursor.isoformat()}"
                / "1m.parquet"
            )
            if warmup_path.exists():
                warmup_bars.extend(ParquetMarketStore.read_bars(warmup_path))
            warmup_cursor = date.fromordinal(warmup_cursor.toordinal() + 1)
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
            volatility_daily,
            cancel_check=cancel_event.is_set,
            trade_start=start,
            warmup_bars=warmup_bars,
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
        sorted_bars = sorted(bars, key=lambda x: x.start)
        from zoneinfo import ZoneInfo

        et = ZoneInfo("America/New_York")
        all_closes: list[Decimal] = []
        full_series: list[dict[str, Any]] = []
        day_bars: list[Any] = []
        current_day = None
        for b in sorted_bars:
            bar_date = b.start.astimezone(et).date()
            if bar_date != current_day:
                current_day = bar_date
                day_bars = []
            day_bars.append(b)
            all_closes.append(b.close)
            point: dict[str, Any] = {
                "time": b.end.isoformat(),
                "price": float(b.close),
                "volume": b.volume,
            }
            boll_period = RULES.timed_boll_period
            boll_stddev = RULES.timed_boll_stddev
            if len(all_closes) >= boll_period:
                upper, middle, lower = bollinger_bands(
                    all_closes,
                    boll_period,
                    boll_stddev,
                )
                point["bb_upper"] = float(upper)
                point["bb_middle"] = float(middle)
                point["bb_lower"] = float(lower)
            macd_fast_p = RULES.timed_macd_fast
            macd_slow_p = RULES.timed_macd_slow
            macd_sig_p = RULES.timed_macd_signal
            macd_required = macd_slow_p + macd_sig_p - 1
            if len(all_closes) >= macd_required:
                macd_line, signal_line, histogram = macd_histogram(
                    all_closes,
                    macd_fast_p,
                    macd_slow_p,
                    macd_sig_p,
                )
                point["macd"] = float(macd_line)
                point["macd_signal"] = float(signal_line)
                point["macd_hist"] = float(histogram)
            full_series.append(point)
        price_series = full_series
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
        await saver(
            {
                **job,
                "created_at": datetime.fromisoformat(job["created_at"]),
                "updated_at": datetime.fromisoformat(job["updated_at"]),
            }
        )
