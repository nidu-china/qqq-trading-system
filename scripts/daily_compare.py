"""Daily comparison of three strategy modes for July-August 2026."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from qqq_trader.backtest import EventDrivenBacktester
from qqq_trader.config import NY_TZ, Settings
from qqq_trader.persistence import ParquetMarketStore
from qqq_trader.risk import ContractSelector, RiskEngine

ET = ZoneInfo("America/New_York")
store_path = Path("data/market/bars")
bars = ParquetMarketStore.read_bars_path(store_path, "1m")
vol_5m = ParquetMarketStore.read_bars_path(store_path, "5m")
vol_daily = ParquetMarketStore.read_bars_path(store_path, "day")

start = date(2026, 7, 1)
qqq_bars = [b for b in bars if b.symbol == "QQQ.US"]
period_bars = [b for b in qqq_bars if b.start.date() >= start]
warmup_bars = [b for b in qqq_bars if b.start.date() < start]
vol = [b for b in vol_5m if b.start.date() >= date(2026, 5, 1)]
vol_d = [b for b in vol_daily if b.start.date() >= date(2026, 5, 1)]

trading_dates = sorted(set(b.start.astimezone(ET).date() for b in period_bars))
print(f"Period: {trading_dates[0]} to {trading_dates[-1]} ({len(trading_dates)} days)")
print(f"Bars: {len(period_bars)}, Warmup: {len(warmup_bars)}, Vol5m: {len(vol)}, VolD: {len(vol_d)}")
print()

results: dict[str, dict] = {}
for mode in ["trend", "boll_macd", "hybrid"]:
    print(f"Running {mode}...", flush=True)
    s = Settings(trading_mode="replay", strategy_mode=mode)
    tester = EventDrivenBacktester(s, None, ContractSelector(), RiskEngine(s))
    r = tester.run(
        period_bars, {}, Decimal("10000"), vol, vol_d,
        trade_start=start, warmup_bars=warmup_bars,
    )

    daily: dict[date, dict] = defaultdict(lambda: {
        "signals": 0, "trades": 0, "wins": 0, "losses": 0,
        "pnl": Decimal(0), "rejected": defaultdict(int),
    })

    for rec in r.signal_records:
        dt_str = rec.get("decision_at", "")
        if not dt_str:
            continue
        from datetime import datetime
        dt = datetime.fromisoformat(dt_str).astimezone(ET).date()
        status = rec.get("status", "")
        if status in ("accepted",):
            daily[dt]["signals"] += 1
        elif status == "rejected":
            reason = rec.get("reject_reason", "unknown")
            daily[dt]["rejected"][reason] += 1

    for t in r.trades:
        d = t.entry_at.astimezone(ET).date()
        daily[d]["trades"] += 1
        daily[d]["pnl"] += t.pnl
        if t.pnl > 0:
            daily[d]["wins"] += 1
        elif t.pnl < 0:
            daily[d]["losses"] += 1

    results[mode] = {
        "daily": dict(daily),
        "total": {
            "signals": r.signals,
            "trades": len(r.trades),
            "wins": sum(1 for t in r.trades if t.pnl > 0),
            "losses": sum(1 for t in r.trades if t.pnl < 0),
            "pnl": r.ending_equity - r.starting_equity,
            "rejected": dict(r.rejected),
            "regimes": dict(r.volatility_regimes),
        },
        "trades": r.trades,
    }

# Print daily comparison table
header = f"{'Date':<12} | {'Trend ORB':>20} | {'BOLL/MACD':>20} | {'Hybrid':>20}"
sep_line = "-" * len(header)
print()
print("=" * len(header))
print(f"{'DAILY RESULTS':^{len(header)}}")
print("=" * len(header))
print(header)
print(sep_line)

cumulative = {m: Decimal(0) for m in ["trend", "boll_macd", "hybrid"]}

for d in trading_dates:
    parts = []
    for mode in ["trend", "boll_macd", "hybrid"]:
        dd = results[mode]["daily"].get(d, {"trades": 0, "pnl": Decimal(0)})
        trades = dd["trades"]
        pnl = dd["pnl"]
        cumulative[mode] += pnl
        if trades > 0:
            parts.append(f"{trades}T ${pnl:>+8}")
        else:
            parts.append(f"{'--':>12}")
    print(f"{str(d):<12} | {parts[0]:>20} | {parts[1]:>20} | {parts[2]:>20}")

print(sep_line)
print(f"{'TOTAL':<12} | ", end="")
for i, mode in enumerate(["trend", "boll_macd", "hybrid"]):
    t = results[mode]["total"]
    text = f"{t['trades']}T ${t['pnl']:>+8}"
    end_char = " | " if i < 2 else "\n"
    print(f"{text:>20}", end=end_char)

print(f"{'WIN RATE':<12} | ", end="")
for i, mode in enumerate(["trend", "boll_macd", "hybrid"]):
    t = results[mode]["total"]
    wr = f"{t['wins']}/{t['trades']} ({t['wins']/t['trades']*100:.0f}%)" if t["trades"] else "N/A"
    end_char = " | " if i < 2 else "\n"
    print(f"{wr:>20}", end=end_char)

print(f"{'REJECTED':<12} | ", end="")
for i, mode in enumerate(["trend", "boll_macd", "hybrid"]):
    t = results[mode]["total"]
    rej = sum(t["rejected"].values())
    end_char = " | " if i < 2 else "\n"
    print(f"{rej:>20}", end=end_char)

# Print trade details
for mode in ["trend", "boll_macd", "hybrid"]:
    print(f"\n--- {mode.upper()} Trades ---")
    for i, t in enumerate(results[mode]["trades"]):
        d = t.direction.value
        entry = t.entry_at.astimezone(ET).strftime("%m/%d %H:%M")
        exit_t = t.exit_at.astimezone(ET).strftime("%H:%M")
        print(f"  #{i+1:2d}: {d:4s} {entry}->{exit_t} PnL=${t.pnl:>+8} exit={t.exit_reason}")
