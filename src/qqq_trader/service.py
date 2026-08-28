from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time, timedelta, timezone

from .config import NY_TZ, Settings
from .domain import SystemState
from .engine import TradingEngine
from .indicators import BarAggregator
from .interfaces import VolatilityDataProvider
from .market_hours import regular_session_bars
from .persistence import ParquetMarketStore
from .policy import RULES
from .reporting import DailyReportData, DailyReportGenerator, TradeSummary


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
        self.strategy_warmup_bars = []
        self.volatility_bars_1m = []
        self.volatility_bars_5m = []
        self.volatility_daily_bars = []
        self.option_tick_buffer: list[dict] = []
        self.option_tick_symbol: str | None = None
        self.chain_captured_date = None
        self._last_vix_refresh: datetime | None = None

    async def run(self) -> None:
        self.running = True
        self._log.info("service starting")
        await self.engine.start()
        if self.engine.state is not SystemState.HALTED:
            self._register_realtime_quote_listener()
            await self._warm_strategy_history()
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
        local_time = local.time()

        if (
            local_time >= self.engine.settings.report_at
            and self.reported_date != local.date()
        ):
            await self._generate_report(local)
            self.reported_date = local.date()

        if local_time < time(9, 0) or local_time > time(16, 5):
            return
        if self.last_minute != local.replace(second=0, microsecond=0):
            self.last_minute = local.replace(second=0, microsecond=0)
            recent = await self.engine.market.recent_bars(  # type: ignore[attr-defined]
                self.engine.settings.underlying_symbol, 500, "1m"
            )
            merged = {bar.start: bar for bar in [*self.strategy_warmup_bars, *recent]}
            self.bars_1m = [
                merged[key] for key in sorted(merged) if merged[key].end <= now
            ][-1000:]
            current_day_bars = [
                bar
                for bar in regular_session_bars(self.bars_1m)
                if bar.start.astimezone(NY_TZ).date() == local.date()
            ]
            if current_day_bars:
                self.market_store.replace_bars(current_day_bars, "1m")
            bars_5m = BarAggregator.to_five_minutes(current_day_bars)
            if bars_5m:
                self.market_store.replace_bars(bars_5m, "5m")
            if local.minute % 5 == 0 or self.last_bar_end is None:
                self._log.info(
                    "bars updated | %s 1m=%d 5m=%d | last=%s",
                    self.engine.settings.underlying_symbol,
                    len(self.bars_1m), len(bars_5m),
                    (
                        self.bars_1m[-1].end.astimezone(NY_TZ).strftime("%H:%M")
                        if self.bars_1m
                        else "N/A"
                    ),
                )
            await self._refresh_volatility(now)
            completed_1m = [bar for bar in self.bars_1m if bar.complete]
            if completed_1m and completed_1m[-1].end != self.last_bar_end:
                new_bar = completed_1m[-1]
                bar_age = (now - new_bar.end).total_seconds()
                self._log.info(
                    "new completed bar | %s | bar_end=%s | now=%s | age=%.1fs",
                    new_bar.symbol,
                    new_bar.end.astimezone(NY_TZ).strftime("%H:%M:%S"),
                    now.astimezone(NY_TZ).strftime("%H:%M:%S"),
                    bar_age,
                )
                self.last_bar_end = completed_1m[-1].end
                await self.engine.on_completed_bars(
                    completed_1m,
                    now,
                    self.volatility_bars_1m,
                    self.volatility_daily_bars,
                )
            await self._capture_candidate_options(now, local, completed_1m)
            self._flush_option_ticks(local.date())
            account = await self.engine.broker.account_snapshot()
            await self.engine.journal.risk_snapshot(account, self.engine.state.value == "halted")

        if self.engine.position is not None:
            quote = await self.engine.market.latest_quote(self.engine.position.symbol)
            self._record_option_tick(quote)
            await self.engine.on_position_quote(quote, now)

    def _register_realtime_quote_listener(self) -> None:
        register = getattr(self.engine.market, "add_quote_listener", None)
        if not callable(register):
            return
        register(self._on_realtime_quote)
        self._log.info("registered real-time option stop listener")

    async def _on_realtime_quote(self, quote) -> None:
        position = self.engine.position
        if position is None or quote.symbol != position.symbol:
            return
        self._record_option_tick(quote)
        await self.engine.on_position_quote(quote, datetime.now(timezone.utc))

    def _record_option_tick(self, quote) -> None:
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

    async def _warm_strategy_history(self) -> None:
        now = datetime.now(timezone.utc).astimezone(NY_TZ)
        start = now.date() - timedelta(days=35)
        end = now.date() - timedelta(days=1)
        try:
            self.strategy_warmup_bars = await self.engine.market.historical_bars(
                self.engine.settings.underlying_symbol, start, end, "1m"
            )
        except Exception as exc:
            self.strategy_warmup_bars = []
            await self.engine.journal.event(
                "strategy_warmup_failed",
                str(exc),
                {"symbol": self.engine.settings.underlying_symbol},
            )

    async def _warm_volatility_history(self) -> None:
        now = datetime.now(timezone.utc)
        end = now.astimezone(NY_TZ).date()
        vix_symbol = self.engine.settings.volatility_symbol
        required_days = self.engine.settings.volatility_lookback_days
        
        self._log.info(
            "warming volatility history | %s | required_days=%d",
            vix_symbol, required_days,
        )
        
        # Try to load from local parquet first and determine what's missing
        local_daily_loaded = []
        missing_start = None
        try:
            from pathlib import Path
            store_path = Path(self.engine.settings.data_dir) / "bars"
            daily_bars = self.market_store.__class__.read_bars_path(store_path, "day")
            vix_daily = [
                b for b in daily_bars 
                if b.symbol == vix_symbol and b.start.astimezone(NY_TZ).date() <= end
            ]
            local_daily_loaded = sorted(vix_daily, key=lambda b: b.start)
            
            if local_daily_loaded:
                latest_local_date = local_daily_loaded[-1].start.astimezone(NY_TZ).date()
                self._log.info(
                    "local VIX daily data | %s | count=%d | latest=%s",
                    vix_symbol, len(local_daily_loaded), latest_local_date,
                )
                # Check if we need to fetch missing recent days
                if latest_local_date < end:
                    from datetime import timedelta as td
                    missing_start = latest_local_date + td(days=1)
                    self._log.info(
                        "will fetch missing VIX daily | %s to %s",
                        missing_start, end,
                    )
            else:
                self._log.warning(
                    "no local VIX daily data found | will fetch from API",
                )
                missing_start = end - timedelta(days=max(45, required_days * 2))
        except Exception as exc:
            self._log.warning("failed to load local VIX daily data | %s | will fetch from API", exc)
            missing_start = end - timedelta(days=max(45, required_days * 2))
        
        # Fetch missing data from API
        try:
            # Always fetch intraday from API (needs to be recent)
            intraday_start = end - timedelta(days=5)
            intraday = await self.volatility_provider.historical_bars(
                vix_symbol, intraday_start, end, "5m"
            )
            self.volatility_bars_5m = [bar for bar in intraday if bar.end <= now]
            self.market_store.write_bars(self.volatility_bars_5m, "5m")
            
            # Fetch missing daily data if needed
            if missing_start is not None:
                self._log.info("fetching missing VIX daily | %s to %s", missing_start, end)
                daily = await self.volatility_provider.historical_bars(
                    vix_symbol, missing_start, end, "day"
                )
                # Merge with local data
                self.volatility_daily_bars = local_daily_loaded + daily
                # Write only the new data to avoid overwriting existing files
                self.market_store.write_bars(daily, "day")
                self._log.info(
                    "volatility warmed (incremental) | %s | 5m=%d daily=%d (local=%d + fetched=%d)",
                    vix_symbol, len(intraday), len(self.volatility_daily_bars),
                    len(local_daily_loaded), len(daily),
                )
            else:
                # All daily data is local
                self.volatility_daily_bars = local_daily_loaded
                self._log.info(
                    "volatility warmed (local) | %s | 5m=%d daily=%d",
                    vix_symbol, len(intraday), len(self.volatility_daily_bars),
                )
            
            # Final validation
            if len(self.volatility_daily_bars) < required_days:
                self._log.error(
                    "insufficient VIX daily data after warm | have=%d required=%d",
                    len(self.volatility_daily_bars), required_days,
                )
            
            await self.engine.journal.event(
                "volatility_warmed",
                f"loaded {len(self.volatility_bars_5m)} intraday and {len(self.volatility_daily_bars)} daily bars",
                {
                    "symbol": vix_symbol,
                    "daily_local": len(local_daily_loaded),
                    "daily_fetched": len(self.volatility_daily_bars) - len(local_daily_loaded),
                },
            )
        except Exception as exc:
            self._log.error("volatility warm failed | %s", exc)
            await self.engine.journal.event(
                "volatility_warm_failed",
                str(exc),
                {"symbol": vix_symbol},
            )

    async def _subscribe_realtime_candlesticks(self) -> None:
        subscriber = getattr(self.engine.market, "subscribe_candlesticks", None)
        if subscriber is None:
            return
        symbols = [self.engine.settings.underlying_symbol]
        try:
            await subscriber(symbols, "1m")
            self._log.info("subscribed to real-time 1m candlesticks | %s", symbols)
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
            self._log.warning(
                "candlestick subscription failed | %s | fallback=REST polling | %s",
                symbols, exc,
            )
            await self.engine.journal.event(
                "candlestick_subscription_failed",
                str(exc),
                {"symbols": symbols, "fallback": "recent_bars_polling"},
            )

    async def _refresh_volatility(self, now: datetime) -> None:
        if not self.engine.settings.volatility_filter_enabled:
            return
        if (
            self._last_vix_refresh is not None
            and (now - self._last_vix_refresh).total_seconds() < 60
        ):
            return
        symbol = self.engine.settings.volatility_symbol
        try:
            recent = await self.volatility_provider.recent_bars(symbol, 500, "1m")
            # Only keep regular trading hours (9:30-16:00 ET)
            from datetime import time as time_type
            self.volatility_bars_1m = [
                bar for bar in recent 
                if bar.end <= now
                and time_type(9, 30) <= bar.end.astimezone(NY_TZ).time().replace(tzinfo=None) <= time_type(16, 0)
            ]
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
            latest = self.volatility_bars_1m[-1] if self.volatility_bars_1m else None
            staleness = (
                (now - latest.end).total_seconds() / 60 if latest else float("inf")
            )
            self._log.info(
                "VIX refreshed | %s | 1m=%d 5m=%d | latest=%s | stale=%.1fmin",
                symbol,
                len(self.volatility_bars_1m),
                len(self.volatility_bars_5m),
                latest.end.astimezone(NY_TZ).strftime("%H:%M") if latest else "N/A",
                staleness,
            )
            self._last_vix_refresh = now
        except Exception as exc:
            self._log.warning("VIX refresh failed | %s | %s", symbol, exc)
            self._last_vix_refresh = now
            await self.engine.journal.event(
                "volatility_refresh_failed",
                str(exc),
                {"symbol": symbol},
            )

    async def _capture_candidate_options(self, now, local, bars_5m) -> None:
        local_time = local.time().replace(tzinfo=None)
        if not (
            RULES.phase_collect_end <= local_time <= RULES.forced_close
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

            candidates = sorted(
                contracts, key=lambda contract: abs(contract.strike - spot_quote.last)
            )[: RULES.option_candidate_count * 2]
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
                    "decision_at": row.decision_at.isoformat(),
                    "reason": row.reason,
                    "indicators": row.indicators or {},
                }
                for row in rows["signals"]
                if row.status == "rejected"
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
