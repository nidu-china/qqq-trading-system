from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal

from .config import NY_TZ, Settings
from .domain import SystemState
from .engine import TradingEngine
from .interfaces import VolatilityDataProvider
from .persistence import ParquetMarketStore
from .reporting import DailyReportData, DailyReportGenerator, TradeSummary
from .strategy import BarAggregator


class TradingService:
    def __init__(
        self,
        settings: Settings,
        engine: TradingEngine,
        market_store: ParquetMarketStore,
        report_generator: DailyReportGenerator,
        volatility_provider: VolatilityDataProvider | None = None,
    ) -> None:
        self.engine = engine
        self.market_store = market_store
        self.report_generator = report_generator
        self.volatility_provider = volatility_provider or engine.market
        self._log = logging.getLogger("qqq_trader.service")
        self.running = False
        self.last_bar_end = None
        self.last_minute = None
        self.reported_date = None
        self.bars_1m = []
        self.volatility_bars_1m = []
        self.volatility_bars_5m = []
        self.volatility_daily_bars = []
        self.option_tick_buffer: list[dict] = []
        self.option_tick_symbol: str | None = None
        self.chain_captured_date = None

    async def run(self) -> None:
        self.running = True
        self._log.info("service starting")
        await self.engine.start()
        if self.engine.state is not SystemState.HALTED:
            await self._subscribe_realtime_candlesticks()
        if self.engine.settings.volatility_filter_enabled:
            await self._warm_volatility_history()
        try:
            while self.running:
                if self.engine.state is not SystemState.HALTED:
                    await self.step()
                await asyncio.sleep(float(self.engine.settings.scheduler_poll_seconds))
        finally:
            local_date = datetime.now(timezone.utc).astimezone(NY_TZ).date()
            self._flush_option_ticks(local_date)
            await self.engine.shutdown()

    async def step(self, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        local = now.astimezone(NY_TZ)
        if local.weekday() >= 5:
            return
        if self.last_minute != local.replace(second=0, microsecond=0):
            self.last_minute = local.replace(second=0, microsecond=0)
            recent = await self.engine.market.recent_bars(  # type: ignore[attr-defined]
                self.engine.settings.underlying_symbol, 500, "1m"
            )
            self.bars_1m = [bar for bar in recent if bar.end <= now]
            self.market_store.write_bars(self.bars_1m, "1m")
            bars_5m = BarAggregator.to_five_minutes(self.bars_1m)
            self.market_store.write_bars(bars_5m, "5m")
            await self._refresh_volatility(now)
            completed_1m = [bar for bar in self.bars_1m if bar.complete]
            if completed_1m and completed_1m[-1].end != self.last_bar_end:
                self.last_bar_end = completed_1m[-1].end
                await self.engine.on_completed_bars(
                    completed_1m,
                    now,
                    self.volatility_bars_5m,
                    self.volatility_daily_bars,
                )
            await self._capture_candidate_options(now, local, completed_1m)
            self._flush_option_ticks(local.date())
            account = await self.engine.broker.account_snapshot()
            await self.engine.journal.risk_snapshot(account, self.engine.state.value == "halted")

        if self.engine.position is not None:
            quote = await self.engine.market.latest_quote(self.engine.position.symbol)
            self.option_tick_symbol = quote.symbol
            self.option_tick_buffer.append(
                {
                    "timestamp": quote.timestamp,
                    "last": str(quote.last),
                    "bid": str(quote.bid) if quote.bid is not None else None,
                    "ask": str(quote.ask) if quote.ask is not None else None,
                    "volume": quote.volume,
                    "open_interest": quote.open_interest,
                    **quote.extra,
                }
            )
            await self.engine.on_position_quote(quote, now)

        if (
            local.time().replace(tzinfo=None) >= self.engine.settings.report_at
            and self.reported_date != local.date()
        ):
            await self._generate_report(local)
            self.reported_date = local.date()

    async def _warm_volatility_history(self) -> None:
        now = datetime.now(timezone.utc)
        end = now.astimezone(NY_TZ).date()
        start = end - timedelta(days=max(45, self.engine.settings.volatility_lookback_days * 2))
        try:
            intraday, daily = await asyncio.gather(
                self.volatility_provider.historical_bars(
                    self.engine.settings.volatility_symbol, start, end, "5m"
                ),
                self.volatility_provider.historical_bars(
                    self.engine.settings.volatility_symbol, start, end, "day"
                ),
            )
            self.volatility_bars_5m = [bar for bar in intraday if bar.end <= now]
            self.volatility_daily_bars = daily
            self.market_store.write_bars(self.volatility_bars_5m, "5m")
            self.market_store.write_bars(self.volatility_daily_bars, "day")
            await self.engine.journal.event(
                "volatility_warmed",
                f"loaded {len(intraday)} intraday and {len(daily)} daily bars",
                {"symbol": self.engine.settings.volatility_symbol},
            )
        except Exception as exc:
            await self.engine.journal.event(
                "volatility_warm_failed",
                str(exc),
                {"symbol": self.engine.settings.volatility_symbol},
            )

    async def _subscribe_realtime_candlesticks(self) -> None:
        subscriber = getattr(self.engine.market, "subscribe_candlesticks", None)
        if subscriber is None:
            return
        symbols = [self.engine.settings.underlying_symbol]
        if self.engine.settings.volatility_filter_enabled:
            symbols.append(self.engine.settings.volatility_symbol)
        try:
            await subscriber(symbols, "1m")
            await self.engine.journal.event(
                "candlesticks_subscribed",
                "subscribed to real-time one-minute candlesticks",
                {"symbols": symbols},
            )
            self._log.info(
                "%s MODE | REAL-TIME 1M CANDLES | %s",
                self.engine.settings.trading_mode.value.upper(),
                ", ".join(symbols),
            )
        except Exception as exc:
            await self.engine.journal.event(
                "candlestick_subscription_failed",
                str(exc),
                {"symbols": symbols, "fallback": "recent_bars_polling"},
            )

    async def _refresh_volatility(self, now: datetime) -> None:
        if not self.engine.settings.volatility_filter_enabled:
            return
        try:
            recent = await self.volatility_provider.recent_bars(
                self.engine.settings.volatility_symbol, 500, "1m"
            )
            self.volatility_bars_1m = [bar for bar in recent if bar.end <= now]
            self.market_store.write_bars(self.volatility_bars_1m, "1m")
            derived = BarAggregator.to_five_minutes(self.volatility_bars_1m)
            merged = {bar.start: bar for bar in [*self.volatility_bars_5m, *derived]}
            cutoff = now - timedelta(
                days=max(45, self.engine.settings.volatility_lookback_days * 2)
            )
            self.volatility_bars_5m = [
                merged[key] for key in sorted(merged) if merged[key].end >= cutoff
            ]
            self.market_store.write_bars(derived, "5m")
        except Exception as exc:
            await self.engine.journal.event(
                "volatility_refresh_failed",
                str(exc),
                {"symbol": self.engine.settings.volatility_symbol},
            )

    async def _capture_candidate_options(self, now, local, bars_5m) -> None:
        local_time = local.time().replace(tzinfo=None)
        if not (
            self.engine.settings.entry_start <= local_time <= self.engine.settings.forced_close
        ):
            return
        try:
            spot_quote = await self.engine.market.latest_quote(
                self.engine.settings.underlying_symbol
            )
            contracts = await self.engine.market.option_chain(
                self.engine.settings.underlying_symbol, local.date()
            )
            if self.chain_captured_date != local.date():
                self.market_store.write_records(
                    "option_chain",
                    self.engine.settings.underlying_symbol,
                    local.date(),
                    [
                        {
                            "symbol": contract.symbol,
                            "underlying": contract.underlying,
                            "expiry": contract.expiry.isoformat(),
                            "strike": str(contract.strike),
                            "direction": contract.right.value,
                        }
                        for contract in contracts
                    ],
                )
                self.chain_captured_date = local.date()

            candidates = []
            for contract in contracts:
                target = (
                    spot_quote.last + self.engine.settings.strike_offset
                    if contract.right.value == "call"
                    else spot_quote.last - self.engine.settings.strike_offset
                )
                if abs(contract.strike - target) <= Decimal("2"):
                    candidates.append(contract)
            snapshots = await asyncio.gather(
                *(self.engine.market.latest_quote(contract.symbol) for contract in candidates),
                return_exceptions=True,
            )
            records = []
            bar_end = bars_5m[-1].end if bars_5m else now
            for contract, snapshot in zip(candidates, snapshots, strict=True):
                if isinstance(snapshot, Exception):
                    continue
                records.append(
                    {
                        "captured_at": snapshot.timestamp,
                        "bar_end": bar_end,
                        "spot": str(spot_quote.last),
                        "symbol": contract.symbol,
                        "underlying": contract.underlying,
                        "expiry": contract.expiry.isoformat(),
                        "strike": str(contract.strike),
                        "direction": contract.right.value,
                        "last": str(snapshot.last),
                        "bid": str(snapshot.bid) if snapshot.bid is not None else None,
                        "ask": str(snapshot.ask) if snapshot.ask is not None else None,
                        "volume": snapshot.volume,
                        "open_interest": snapshot.open_interest,
                        **snapshot.extra,
                    }
                )
            self.market_store.write_records(
                "candidate_option_quotes",
                self.engine.settings.underlying_symbol,
                local.date(),
                records,
            )
        except Exception as exc:
            self._log.warning("option capture failed: %s", exc)
            await self.engine.journal.event(
                "option_capture_failed", str(exc), {"at": now.isoformat()}
            )

    def _flush_option_ticks(self, trading_date) -> None:
        if not self.option_tick_buffer or not self.option_tick_symbol:
            return
        self.market_store.write_records(
            "option_quotes",
            self.option_tick_symbol,
            trading_date,
            self.option_tick_buffer,
        )
        self.option_tick_buffer = []

    async def _generate_report(self, local: datetime) -> None:
        self._log.info("generating daily report for %s", local.date())
        account = await self.engine.broker.account_snapshot()
        trades = list(self.engine.closed_trades)
        rejected: list[dict] = []
        events: list[dict] = []
        comparison: dict = {}
        report_rows = getattr(self.engine.journal, "report_rows", None)
        if report_rows is not None:
            local_start = datetime.combine(local.date(), time.min, NY_TZ)
            local_end = local_start + timedelta(days=1)
            rows = await report_rows(
                local_start.astimezone(timezone.utc), local_end.astimezone(timezone.utc)
            )
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
                {
                    "at": row.created_at.isoformat(),
                    "kind": row.kind,
                    "message": row.message,
                    "details": row.details,
                }
                for row in rows["events"]
            ]
        performance = getattr(self.engine.journal, "performance_20d", None)
        if performance is not None:
            comparison = await performance(datetime.combine(local.date(), time.min, NY_TZ))
        ordered = sorted({bar.start for bar in self.bars_1m})
        gaps = sum(
            max(0, int((right - left).total_seconds() // 60) - 1)
            for left, right in zip(ordered, ordered[1:], strict=False)
        )
        self.report_generator.generate(
            DailyReportData(
                trading_date=local.date(),
                opening_equity=self.engine.opening_equity,
                closing_equity=account.equity,
                trades=trades,
                rejected_signals=rejected,
                system_events=events,
                comparison_20d=comparison,
                data_quality={
                    "one_minute_bars": len(self.bars_1m),
                    "missing_minutes_between_observations": gaps,
                },
                underlying_bars=list(self.bars_1m),
            )
        )

    def stop(self) -> None:
        self.running = False
