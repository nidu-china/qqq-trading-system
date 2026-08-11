"""Compare targeted quality gates for ordinary timed first entries."""

from __future__ import annotations

import argparse
import json
from concurrent.futures import ProcessPoolExecutor
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from qqq_trader.backtest import EventDrivenBacktester, load_option_frames
from qqq_trader.config import NY_TZ, Settings
from qqq_trader.persistence import ParquetMarketStore
from qqq_trader.strategy import StrategyEngine

VARIANTS = {
    "baseline": (False, False),
    "fresh_macd_quality": (True, False),
    "cross2_quality": (False, True),
    "combined_quality": (True, True),
}


def _load_bars(
    root: Path,
    symbol: str,
    start: date,
    end: date,
    timeframe: str,
) -> list:
    bars = []
    current = start
    while current <= end:
        path = (
            root
            / "bars"
            / f"symbol={symbol}"
            / f"date={current.isoformat()}"
            / f"{timeframe}.parquet"
        )
        if path.exists():
            bars.extend(ParquetMarketStore.read_bars(path))
        current += timedelta(days=1)
    return bars


def _max_drawdown(result) -> Decimal:
    peak = result.starting_equity
    drawdown = Decimal(0)
    for point in result.equity_curve:
        peak = max(peak, point.equity)
        drawdown = min(drawdown, point.equity - peak)
    return drawdown


def _trade_metrics(trades: list) -> dict:
    gross_profit = sum((trade.pnl for trade in trades if trade.pnl > 0), Decimal(0))
    gross_loss = abs(sum((trade.pnl for trade in trades if trade.pnl < 0), Decimal(0)))
    wins = sum(trade.pnl > 0 for trade in trades)
    return {
        "trades": len(trades),
        "wins": wins,
        "win_rate": str(Decimal(wins) / Decimal(len(trades))) if trades else "0",
        "net_pnl": str(sum((trade.pnl for trade in trades), Decimal(0))),
        "profit_factor": str(gross_profit / gross_loss) if gross_loss else None,
    }


def _ordinary_categories(result) -> dict:
    accepted = sorted(
        (
            record
            for record in result.signal_records
            if record["action"] == "buy" and record["status"] == "accepted"
        ),
        key=lambda record: record["decision_at"],
    )
    trades = sorted(result.trades, key=lambda trade: trade.entry_at)
    if len(accepted) != len(trades):
        raise RuntimeError(
            f"accepted/trade mismatch: {len(accepted)} accepted, {len(trades)} trades"
        )
    categories = {
        "all_ordinary": [],
        "fresh_macd": [],
        "crosses_equal_2": [],
        "fresh_macd_and_cross2": [],
        "neither": [],
    }
    details = []
    for record, trade in zip(accepted, trades, strict=True):
        if trade.strategy != "timed_boll_macd_signal":
            continue
        indicators = record["indicators"]
        previous_histogram = Decimal(indicators["macd_hist_prev"])
        fresh = (
            trade.direction.value == "call" and previous_histogram <= 0
        ) or (
            trade.direction.value == "put" and previous_histogram >= 0
        )
        cross2 = int(indicators["boll_middle_crosses"]) == 2
        categories["all_ordinary"].append(trade)
        if fresh:
            categories["fresh_macd"].append(trade)
        if cross2:
            categories["crosses_equal_2"].append(trade)
        if fresh and cross2:
            categories["fresh_macd_and_cross2"].append(trade)
        if not fresh and not cross2:
            categories["neither"].append(trade)
        if fresh or cross2:
            details.append(
                {
                    "date": trade.entry_at.astimezone(NY_TZ).date().isoformat(),
                    "entry": trade.entry_at.astimezone(NY_TZ).strftime("%H:%M"),
                    "direction": trade.direction.value,
                    "fresh_macd": fresh,
                    "crosses_equal_2": cross2,
                    "rvol": indicators["volume_ratio"],
                    "previous_rvol": indicators["previous_volume_ratio"],
                    "band_extension": indicators["boll_band_extension"],
                    "pnl": str(trade.pnl),
                    "exit_reason": trade.exit_reason,
                }
            )
    return {
        "metrics": {
            name: _trade_metrics(items) for name, items in categories.items()
        },
        "trades": details,
    }


def _run_variant(arguments: tuple[str, bool, bool, str, str]) -> dict:
    name, fresh_filter, cross2_filter, start_text, end_text = arguments
    start = date.fromisoformat(start_text)
    end = date.fromisoformat(end_text)
    root = Path("data/market")
    settings = Settings(
        data_dir=root,
        volatility_filter_enabled=True,
    )
    bars = _load_bars(root, settings.underlying_symbol, start, end, "1m")
    warmup = _load_bars(
        root,
        settings.underlying_symbol,
        start - timedelta(days=35),
        start - timedelta(days=1),
        "1m",
    )
    volatility = _load_bars(
        root,
        settings.volatility_symbol,
        start - timedelta(days=int(settings.volatility_lookback_days * 3)),
        end,
        "5m",
    )
    volatility_daily = _load_bars(
        root,
        settings.volatility_symbol,
        start - timedelta(days=int(settings.volatility_lookback_days * 3)),
        end,
        "day",
    )
    frames = {}
    current = start
    while current <= end:
        path = (
            root
            / "candidate_option_quotes"
            / f"symbol={settings.underlying_symbol}"
            / f"date={current.isoformat()}"
            / "data.parquet"
        )
        if path.exists():
            frames.update(load_option_frames(path))
        current += timedelta(days=1)
    strategy = StrategyEngine(
        settings,
        normal_fresh_macd_filter=fresh_filter,
        normal_cross2_filter=cross2_filter,
    )
    result = EventDrivenBacktester(settings, strategy=strategy).run(
        bars,
        frames,
        Decimal("10000"),
        volatility,
        volatility_daily,
        trade_start=start,
        warmup_bars=warmup,
    )
    stop_legs = [
        leg
        for trade in result.trades
        for leg in trade.exit_legs
        if leg.reason == "stop_loss"
    ]
    payload = {
        "variant": name,
        "fresh_macd_filter": fresh_filter,
        "cross2_filter": cross2_filter,
        **_trade_metrics(result.trades),
        "max_drawdown_mtm": str(_max_drawdown(result)),
        "stop_count": len(stop_legs),
        "stop_pnl": str(sum((leg.pnl for leg in stop_legs), Decimal(0))),
        "average_stop_penetration_pct": (
            str(
                sum(
                    (leg.stop_penetration_pct or Decimal(0) for leg in stop_legs),
                    Decimal(0),
                )
                / Decimal(len(stop_legs))
            )
            if stop_legs
            else "0"
        ),
    }
    if name == "baseline":
        payload["ordinary_entry_categories"] = _ordinary_categories(result)
    return payload


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2026-07-01")
    parser.add_argument("--end", default="2026-07-31")
    args = parser.parse_args()
    jobs = [
        (name, fresh, cross2, args.start, args.end)
        for name, (fresh, cross2) in VARIANTS.items()
    ]
    with ProcessPoolExecutor(max_workers=len(jobs)) as executor:
        results = list(executor.map(_run_variant, jobs))
    print(json.dumps(results, ensure_ascii=False, indent=2))
