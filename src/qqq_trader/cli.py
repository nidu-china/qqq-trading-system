from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import typer
import uvicorn

from .adapters.longbridge import LongbridgeMarketData, LongbridgeSession
from .api import create_app
from .backtest import BacktestResult, EventDrivenBacktester, load_option_frames_path
from .config import NY_TZ, Settings
from .logging_config import setup_logging
from .persistence import MySQLJournal, ParquetMarketStore
from .reporting import DailyReportData, DailyReportGenerator, TradeSummary
from .risk import ContractSelector, RiskEngine
from .runtime import build_runtime
from .strategy import strategy_from_settings

app = typer.Typer(no_args_is_help=True, help="QQQ 0DTE automated trading system")


def _backtest_metrics(result: BacktestResult) -> dict:
    wins = [trade.pnl for trade in result.trades if trade.pnl > 0]
    losses = [trade.pnl for trade in result.trades if trade.pnl < 0]
    gross_profit = sum(wins, Decimal(0))
    gross_loss = abs(sum(losses, Decimal(0)))
    net_pnl = result.ending_equity - result.starting_equity
    equity = result.starting_equity
    peak = equity
    max_drawdown = Decimal(0)
    for trade in result.trades:
        equity += trade.pnl
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity - peak)
    return {
        "starting_equity": str(result.starting_equity),
        "ending_equity": str(result.ending_equity),
        "net_pnl": str(net_pnl),
        "return_rate": (str(net_pnl / result.starting_equity) if result.starting_equity else "0"),
        "signals": result.signals,
        "trades": len(result.trades),
        "win_rate": (
            str(Decimal(len(wins)) / Decimal(len(result.trades))) if result.trades else "0"
        ),
        "profit_factor": str(gross_profit / gross_loss) if gross_loss else None,
        "max_drawdown": str(max_drawdown),
        "rejected": result.rejected,
        "option_data_complete": result.option_data_complete,
        "volatility_data_complete": result.volatility_data_complete,
        "volatility_regimes": result.volatility_regimes,
    }


@app.command()
def trade() -> None:
    """Run the trading loop and read-only health API."""

    async def main() -> None:
        runtime = await build_runtime()
        log = setup_logging(runtime.settings.log_dir, runtime.settings.log_level)
        log.info("starting %s mode", runtime.settings.trading_mode.value)
        api = create_app(runtime.engine, runtime.journal, runtime.settings)
        server = uvicorn.Server(
            uvicorn.Config(
                api,
                host=runtime.settings.api_host,
                port=runtime.settings.api_port,
                log_level=runtime.settings.log_level.lower(),
            )
        )
        service_task = asyncio.create_task(runtime.service.run())
        server_task = asyncio.create_task(server.serve())
        try:
            done, _ = await asyncio.wait(
                {service_task, server_task}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                if task.exception() is not None:
                    raise task.exception()
            runtime.service.stop()
            server.should_exit = True
            await asyncio.gather(service_task, server_task, return_exceptions=True)
        finally:
            runtime.service.stop()
            if not service_task.done():
                await service_task
            await runtime.close()

    asyncio.run(main())


@app.command()
def backfill(
    start: str = typer.Option(..., help="Start date in YYYY-MM-DD"),
    end: str = typer.Option(..., help="End date in YYYY-MM-DD"),
    symbol: str = typer.Option("QQQ.US"),
    include_volatility: bool = typer.Option(True, "--include-volatility/--no-include-volatility"),
) -> None:
    """Backfill underlying and configured volatility bars."""

    async def main() -> None:
        start_date = date.fromisoformat(start)
        end_date = date.fromisoformat(end)
        settings = Settings()
        log = setup_logging(settings.log_dir, settings.log_level)
        session = LongbridgeSession(settings)
        market = LongbridgeMarketData(session)
        typer.echo(f"loading configuration from {settings.model_config['env_file']}")
        typer.echo("initializing Longbridge market-data client")
        await market.connect()
        try:
            typer.echo(f"requesting {symbol} 1m bars: {start_date} to {end_date}")
            bars = await market.historical_bars(symbol, start_date, end_date, "1m")
            store = ParquetMarketStore(settings.data_dir)
            store.write_bars(bars, "1m")
            from .strategy import BarAggregator

            store.write_bars(BarAggregator.to_five_minutes(bars), "5m")
            typer.echo(f"saved {len(bars)} {symbol} one-minute bars")
            if include_volatility and symbol != settings.volatility_symbol:
                typer.echo(f"requesting {settings.volatility_symbol} 5m and daily bars")
                volatility_5m = await market.historical_bars(
                    settings.volatility_symbol, start_date, end_date, "5m"
                )
                volatility_daily = await market.historical_bars(
                    settings.volatility_symbol, start_date, end_date, "day"
                )
                store.write_bars(volatility_5m, "5m")
                store.write_bars(volatility_daily, "day")
                typer.echo(
                    f"saved {len(volatility_5m)} intraday and "
                    f"{len(volatility_daily)} daily {settings.volatility_symbol} bars"
                )
        finally:
            await market.close()

    asyncio.run(main())


@app.command()
def backtest(
    bars: Path = typer.Option(..., exists=True),
    option_frames: Path | None = typer.Option(None, exists=True),
    volatility_bars: Path | None = typer.Option(None, exists=True),
    volatility_daily_bars: Path | None = typer.Option(None, exists=True),
    starting_equity: str = typer.Option("100000"),
    compare_macd: bool = typer.Option(
        False,
        "--compare-macd",
        help="Compare MACD_BACKTEST_COMBINATIONS with identical market data.",
    ),
) -> None:
    """Replay saved bars and optional captured candidate-option Bid/Ask frames."""
    settings = Settings(trading_mode="replay")
    saved_bars = ParquetMarketStore.read_bars_path(bars, "1m")
    frames = load_option_frames_path(option_frames) if option_frames else {}
    saved_volatility = (
        ParquetMarketStore.read_bars_path(volatility_bars, "5m") if volatility_bars else []
    )
    saved_volatility_daily = (
        ParquetMarketStore.read_bars_path(volatility_daily_bars, "day")
        if volatility_daily_bars
        else []
    )
    combinations = (
        settings.macd_parameter_sets()
        if compare_macd
        else [(settings.macd_fast, settings.macd_slow, settings.macd_signal)]
    )
    runs = []
    for fast, slow, signal in combinations:
        run_settings = Settings.model_validate(
            {
                **settings.model_dump(),
                "macd_fast": fast,
                "macd_slow": slow,
                "macd_signal": signal,
            }
        )
        tester = EventDrivenBacktester(
            run_settings,
            strategy_from_settings(run_settings),
            ContractSelector(run_settings.strike_offset),
            RiskEngine(run_settings),
        )
        result = tester.run(
            saved_bars,
            frames,
            Decimal(starting_equity),
            saved_volatility,
            saved_volatility_daily,
        )
        runs.append(
            {
                "macd": {"fast": fast, "slow": slow, "signal": signal},
                **_backtest_metrics(result),
            }
        )

    warnings = [
        message
        for condition, message in (
            (
                not option_frames,
                "No option Bid/Ask frames supplied; this is not a 0DTE PnL backtest.",
            ),
            (
                settings.volatility_filter_enabled and not volatility_bars,
                "No volatility bars supplied; volatility-gated signals are rejected.",
            ),
        )
        if condition
    ]
    if compare_macd:
        eligible = [
            run
            for run in runs
            if run["trades"] > 0
            and run["option_data_complete"]
            and (not settings.volatility_filter_enabled or run["volatility_data_complete"])
        ]
        ranking = sorted(
            eligible,
            key=lambda run: (Decimal(run["net_pnl"]), Decimal(run["max_drawdown"])),
            reverse=True,
        )
        payload = {
            "mode": "macd_comparison",
            "ranked_by": "net_pnl_then_max_drawdown",
            "best": ranking[0]["macd"] if ranking else None,
            "ranking": ranking,
            "excluded_incomplete_runs": [run for run in runs if run not in eligible],
            "warning": warnings,
        }
    else:
        payload = {**runs[0], "warning": warnings}
    typer.echo(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
        )
    )


@app.command()
def report(
    trading_date: str = typer.Option(..., help="Trading date in YYYY-MM-DD"),
) -> None:
    """Regenerate a daily report from MySQL records and stored bars."""

    async def main() -> None:
        report_date = date.fromisoformat(trading_date)
        settings = Settings()
        journal = MySQLJournal(settings.database_url)
        local_start = datetime.combine(report_date, time.min, NY_TZ)
        local_end = local_start + timedelta(days=1)
        rows = await journal.report_rows(
            local_start.astimezone(timezone.utc), local_end.astimezone(timezone.utc)
        )
        risks = rows["risks"]
        opening = risks[0].equity if risks else Decimal(0)
        closing = risks[-1].equity if risks else opening
        trades = [
            TradeSummary(
                symbol=row.symbol,
                direction=row.direction,
                quantity=row.quantity,
                entry_price=row.entry_price,
                exit_price=row.exit_price,
                pnl=row.pnl,
                fees=row.fees,
                entry_at=row.entry_at.isoformat(),
                exit_at=row.exit_at.isoformat(),
                exit_reason=row.exit_reason,
                slippage=row.slippage,
                mae=row.mae,
                mfe=row.mfe,
            )
            for row in rows["trades"]
        ]
        rejected = [
            {
                "bar_end": row.bar_end.isoformat(),
                "reason": row.reason,
                "indicators": row.indicators or {},
            }
            for row in rows["signals"]
            if not row.accepted
        ]
        events = [
            {"at": row.created_at.isoformat(), "kind": row.kind, "message": row.message}
            for row in rows["events"]
        ]
        bar_path = (
            settings.data_dir
            / "bars"
            / f"symbol={settings.underlying_symbol}"
            / f"date={report_date.isoformat()}"
            / "1m.parquet"
        )
        saved_bars = ParquetMarketStore.read_bars(bar_path) if bar_path.exists() else []
        paths = DailyReportGenerator(settings.report_dir).generate(
            DailyReportData(
                trading_date=report_date,
                opening_equity=opening,
                closing_equity=closing,
                trades=trades,
                rejected_signals=rejected,
                system_events=events,
                underlying_bars=saved_bars,
            )
        )
        await journal.close()
        typer.echo(str(paths["html"]))

    asyncio.run(main())


@app.command()
def reconcile() -> None:
    """Read broker state and report whether the engine can safely become ready."""

    async def main() -> None:
        runtime = await build_runtime()
        await runtime.engine.start()
        ok = await runtime.engine.reconcile()
        typer.echo(
            json.dumps({"safe": ok, **runtime.engine.status()}, ensure_ascii=False, indent=2)
        )
        await runtime.engine.broker.close()
        await runtime.engine.market.close()
        await runtime.close()
        if not ok:
            raise typer.Exit(code=2)

    asyncio.run(main())


if __name__ == "__main__":
    app()
