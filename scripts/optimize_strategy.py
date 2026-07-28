"""Small walk-forward comparison for fixed STRATEGY.md rule candidates."""

from __future__ import annotations

import json
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from qqq_trader.backtest import EventDrivenBacktester
from qqq_trader.config import NY_TZ, Settings
from qqq_trader.persistence import ParquetMarketStore
from qqq_trader.policy import RULES

CANDIDATES = {
    "strict_1m": {
        "max_vwap_distance_atr": Decimal("1.5"),
        "trend_structure_confirmations": 2,
        "early_ema_tolerance_atr": Decimal("0"),
    },
    "wider_vwap": {
        "max_vwap_distance_atr": Decimal("3.0"),
        "trend_structure_confirmations": 2,
        "early_ema_tolerance_atr": Decimal("0"),
    },
    "early_confirmation": {
        "max_vwap_distance_atr": Decimal("1.5"),
        "trend_structure_confirmations": 1,
        "early_ema_tolerance_atr": Decimal("0.25"),
    },
    "balanced_fast": {
        "max_vwap_distance_atr": Decimal("3.0"),
        "trend_structure_confirmations": 1,
        "early_ema_tolerance_atr": Decimal("0.25"),
    },
    "momentum_filtered": {
        "max_vwap_distance_atr": Decimal("3.0"),
        "trend_structure_confirmations": 2,
        "early_ema_tolerance_atr": Decimal("0"),
        "trend_call_rsi_min": Decimal("60"),
        "trend_put_rsi_max": Decimal("40"),
        "require_directional_macd": True,
    },
    "momentum_strong": {
        "max_vwap_distance_atr": Decimal("3.0"),
        "trend_structure_confirmations": 2,
        "early_ema_tolerance_atr": Decimal("0"),
        "trend_call_rsi_min": Decimal("60"),
        "trend_put_rsi_max": Decimal("40"),
        "require_directional_macd": True,
        "min_macd_hist_atr": Decimal("0.1"),
    },
}


def _load_bars(root: Path, symbol: str, start: date, end: date, timeframe: str) -> list:
    bars = []
    cursor = start
    while cursor <= end:
        path = root / "bars" / f"symbol={symbol}" / f"date={cursor}" / f"{timeframe}.parquet"
        if path.exists():
            bars.extend(ParquetMarketStore.read_bars(path))
        cursor += timedelta(days=1)
    return bars


def _metrics(trades: list) -> dict:
    ordered = sorted(trades, key=lambda trade: trade.exit_at)
    pnl = sum((trade.pnl for trade in ordered), Decimal(0))
    wins = sum(trade.pnl > 0 for trade in ordered)
    gross_profit = sum((trade.pnl for trade in ordered if trade.pnl > 0), Decimal(0))
    gross_loss = abs(sum((trade.pnl for trade in ordered if trade.pnl < 0), Decimal(0)))
    equity = peak = Decimal(0)
    drawdown = Decimal(0)
    for trade in ordered:
        equity += trade.pnl
        peak = max(peak, equity)
        drawdown = min(drawdown, equity - peak)
    return {
        "trades": len(ordered),
        "net_pnl": str(pnl),
        "win_rate": str(Decimal(wins) / Decimal(len(ordered))) if ordered else "0",
        "profit_factor": str(gross_profit / gross_loss) if gross_loss else None,
        "max_drawdown": str(drawdown),
    }


def _trade_details(trades: list) -> list[dict]:
    return [
        {
            "date": trade.entry_at.astimezone(NY_TZ).date().isoformat(),
            "entry": trade.entry_at.astimezone(NY_TZ).strftime("%H:%M"),
            "exit": trade.exit_at.astimezone(NY_TZ).strftime("%H:%M"),
            "direction": trade.direction.value,
            "strategy": trade.strategy,
            "pnl": str(trade.pnl),
            "reason": trade.exit_reason,
        }
        for trade in trades
    ]


def run_candidate(item: tuple[str, dict]) -> dict:
    name, changes = item
    changes = dict(changes)
    volatility_enabled = bool(changes.pop("_volatility_filter_enabled", True))
    rules = replace(RULES, **changes)
    import qqq_trader.backtest as backtest_module
    import qqq_trader.risk as risk_module
    import qqq_trader.strategy as strategy_module

    backtest_module.RULES = rules
    risk_module.RULES = rules
    strategy_module.RULES = rules

    root = Path("data/market")
    start = date(2026, 7, 1)
    end = date(2026, 7, 23)
    split = date(2026, 7, 15)
    settings = Settings(
        _env_file=None,
        data_dir=root,
        volatility_filter_enabled=volatility_enabled,
    )
    qqq = _load_bars(root, settings.underlying_symbol, start, end, "1m")
    volatility = _load_bars(
        root,
        settings.volatility_symbol,
        date(2026, 5, 1),
        end,
        "5m",
    )
    volatility_daily = _load_bars(
        root,
        settings.volatility_symbol,
        date(2026, 5, 1),
        end,
        "day",
    )
    result = EventDrivenBacktester(settings).run(
        qqq,
        {},
        Decimal("10000"),
        volatility,
        volatility_daily,
        trade_start=start,
    )
    train = [
        trade for trade in result.trades if trade.entry_at.astimezone(NY_TZ).date() < split
    ]
    validation = [
        trade for trade in result.trades if trade.entry_at.astimezone(NY_TZ).date() >= split
    ]
    accepted_validation = []
    for record in result.signal_records:
        timestamp = record.get("decision_at")
        if record.get("action") != "buy" or record.get("status") != "accepted" or not timestamp:
            continue
        local = datetime.fromisoformat(timestamp).astimezone(NY_TZ)
        if local.date() >= split:
            accepted_validation.append(
                {
                    "date": local.date().isoformat(),
                    "time": local.strftime("%H:%M"),
                    "direction": record.get("direction"),
                    "indicators": record.get("indicators", {}),
                }
            )
    return {
        "candidate": name,
        "rules": {key: str(value) for key, value in changes.items()},
        "train": _metrics(train),
        "validation": _metrics(validation),
        "validation_trades": _trade_details(validation),
        "validation_signals": accepted_validation,
        "full": _metrics(result.trades),
        "signals": result.signals,
        "rejected": result.rejected,
    }


if __name__ == "__main__":
    with ProcessPoolExecutor(max_workers=len(CANDIDATES)) as executor:
        results = list(executor.map(run_candidate, CANDIDATES.items()))
    print(json.dumps(results, ensure_ascii=False, indent=2))
