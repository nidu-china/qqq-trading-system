"""Daily comparison of three strategy modes for July-August 2026."""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from qqq_trader.backtest import EventDrivenBacktester
from qqq_trader.config import NY_TZ, Settings
from qqq_trader.persistence import ParquetMarketStore
from qqq_trader.risk import ContractSelector, RiskEngine

# Parse command line arguments
parser = argparse.ArgumentParser(description="Backtest Hybrid strategy")
parser.add_argument("--start", type=str, default="2026-07-01", help="Start date (YYYY-MM-DD)")
parser.add_argument("--end", type=str, default="2026-08-27", help="End date (YYYY-MM-DD)")
args = parser.parse_args()

start = datetime.strptime(args.start, "%Y-%m-%d").date()
end = datetime.strptime(args.end, "%Y-%m-%d").date()

ET = ZoneInfo("America/New_York")
store_path = Path("data/market/bars")
bars = ParquetMarketStore.read_bars_path(store_path, "1m")
vol_5m = ParquetMarketStore.read_bars_path(store_path, "5m")
vol_daily = ParquetMarketStore.read_bars_path(store_path, "day")
qqq_bars = [b for b in bars if b.symbol == "QQQ.US"]

# Period bars: regular trading hours (RTH) only
period_bars = [
    b for b in qqq_bars
    if start <= b.start.date() <= end
    and time(9, 30) <= b.start.astimezone(ET).time() < time(16, 0)
]

# Warmup: use 09:00-09:30 pre-market data for each trading day
warmup_bars = [
    b for b in qqq_bars
    if start <= b.start.date() <= end
    and time(9, 0) <= b.start.astimezone(ET).time() < time(9, 30)
]
VIX = ".VIX.US"
vol = [b for b in vol_5m if b.symbol == VIX and b.start.date() >= date(2026, 5, 1)]
vol_d = [b for b in vol_daily if b.symbol == VIX and b.start.date() >= date(2026, 5, 1)]

trading_dates = sorted(set(b.start.astimezone(ET).date() for b in period_bars))
print(f"Period: {trading_dates[0]} to {trading_dates[-1]} ({len(trading_dates)} days)")
print(f"Bars: {len(period_bars)}, Warmup: {len(warmup_bars)}, Vol5m: {len(vol)}, VolD: {len(vol_d)}")
print()

results: dict[str, dict] = {}
for mode in ["hybrid"]:
    print(f"Running {mode}...", flush=True)
    s = Settings(trading_mode="replay", strategy_mode=mode)
    tester = EventDrivenBacktester(s, None, ContractSelector(), RiskEngine(s))
    r = tester.run(
        period_bars, {}, Decimal("100000"), vol, vol_d,
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

# Print daily results
header = f"{'Date':<12} | {'Trades':>6} | {'PnL':>10} | {'Cumulative':>12}"
sep_line = "-" * len(header)
print()
print("=" * len(header))
print(f"{'HYBRID DAILY RESULTS':^{len(header)}}")
print("=" * len(header))
print(header)
print(sep_line)

cumulative = Decimal(0)
for d in trading_dates:
    dd = results["hybrid"]["daily"].get(d, {"trades": 0, "pnl": Decimal(0)})
    trades = dd["trades"]
    pnl = dd["pnl"]
    cumulative += pnl
    if trades > 0:
        print(f"{str(d):<12} | {trades:>6} | ${pnl:>+8} | ${cumulative:>+10}")
    else:
        print(f"{str(d):<12} | {'--':>6} | {'--':>10} | ${cumulative:>+10}")

print(sep_line)
t = results["hybrid"]["total"]
wr = f"{t['wins']}/{t['trades']} ({t['wins']/t['trades']*100:.0f}%)" if t["trades"] else "N/A"
rej = sum(t["rejected"].values())
print(f"TOTAL: {t['trades']}T  PnL=${t['pnl']:>+8}  Win={wr}  Rejected={rej}")

# Print trade details
print(f"\n--- HYBRID Trades ---")
for i, t in enumerate(results["hybrid"]["trades"]):
    d = t.direction.value
    entry = t.entry_at.astimezone(ET).strftime("%m/%d %H:%M")
    exit_t = t.exit_at.astimezone(ET).strftime("%H:%M")
    print(f"  #{i+1:2d}: {d:4s} {entry}->{exit_t} PnL=${t.pnl:>+8} exit={t.exit_reason}")
