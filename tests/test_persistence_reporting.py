from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

from qqq_trader.persistence import ParquetMarketStore
from qqq_trader.reporting import DailyReportData, DailyReportGenerator, TradeSummary


def test_parquet_bars_are_idempotent(tmp_path, bullish_bars):
    store = ParquetMarketStore(tmp_path)
    path = store.write_bars(bullish_bars, "5m")
    assert path is not None
    store.write_bars(bullish_bars, "5m")
    loaded = store.read_bars(path)
    assert len(loaded) == len(bullish_bars)
    manifest = json.loads(path.with_suffix(".manifest.json").read_text(encoding="utf-8"))
    assert manifest["rows"] == len(bullish_bars)


def test_generates_three_report_formats_and_chart(tmp_path, bullish_bars):
    trade = TradeSummary(
        "QQQ260715C105000.US",
        "call",
        1,
        Decimal("1"),
        Decimal("1.5"),
        Decimal("48.5"),
        Decimal("1.5"),
        "2026-07-15T14:00:00+00:00",
        "2026-07-15T14:10:00+00:00",
        "take_profit_1",
    )
    paths = DailyReportGenerator(tmp_path).generate(
        DailyReportData(
            date(2026, 7, 15),
            Decimal("100000"),
            Decimal("100048.5"),
            [trade],
            underlying_bars=bullish_bars,
        )
    )
    assert all(path.exists() for path in paths.values())
    payload = json.loads(paths["json"].read_text(encoding="utf-8"))
    assert payload["metrics"]["net_pnl"] == "48.5"
    assert "禁止自动" in paths["html"].read_text(encoding="utf-8")
