from __future__ import annotations

from dataclasses import dataclass

from .adapters.longbridge import LongbridgeBroker, LongbridgeMarketData, LongbridgeSession
from .adapters.paper import PaperBroker
from .config import Settings
from .configuration import with_editable_values
from .domain import TradingMode
from .engine import TradingEngine
from .persistence import MySQLJournal, ParquetMarketStore
from .reporting import DailyReportGenerator
from .service import TradingService


@dataclass(slots=True)
class Runtime:
    settings: Settings
    journal: MySQLJournal
    engine: TradingEngine
    service: TradingService

    async def close(self) -> None:
        await self.journal.close()


async def build_runtime(settings: Settings | None = None) -> Runtime:
    settings = settings or Settings()
    journal = MySQLJournal(settings.database_url)
    await journal.create_schema()
    active_config = await journal.active_config()
    if active_config is not None:
        settings = with_editable_values(settings, active_config.values)
    session = LongbridgeSession(settings)
    market = LongbridgeMarketData(session)
    broker = (
        LongbridgeBroker(session, settings)
        if settings.trading_mode is TradingMode.LIVE
        else PaperBroker(fee_per_contract=settings.fee_per_contract)
    )
    engine = TradingEngine(settings, market, broker, journal)
    if active_config is not None:
        engine.config_version = active_config.id
    service = TradingService(
        settings,
        engine,
        ParquetMarketStore(settings.data_dir),
        DailyReportGenerator(settings.report_dir),
        volatility_provider=market,
    )
    return Runtime(settings, journal, engine, service)
