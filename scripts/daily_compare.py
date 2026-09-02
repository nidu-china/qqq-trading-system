"""Daily comparison of three strategy modes for July-August 2026."""
from __future__ import annotations

import argparse
import sys
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
parser.add_argument("--all", dest="compare_all", action="store_true", help="Compare all three strategies")
parser.add_argument("--output", type=str, default=None, help="Save report to file (e.g. docs/hybrid_report.txt)")
args = parser.parse_args()

# ── Output redirect ────────────────────────────────────────────────────────────
_out_file = None
if args.output:
    _out_path = Path(args.output)
    _out_path.parent.mkdir(parents=True, exist_ok=True)
    _out_file = open(_out_path, "w", encoding="utf-8")

    class _Tee:
        def __init__(self, *streams):
            self._streams = streams
        def write(self, data):
            for s in self._streams:
                s.write(data)
        def flush(self):
            for s in self._streams:
                s.flush()

    sys.stdout = _Tee(sys.__stdout__, _out_file)

start = datetime.strptime(args.start, "%Y-%m-%d").date()
end = datetime.strptime(args.end, "%Y-%m-%d").date()

ET = ZoneInfo("America/New_York")
store_path = Path("data/market/bars")
bars = ParquetMarketStore.read_bars_path(store_path, "1m")
vol_5m = ParquetMarketStore.read_bars_path(store_path, "5m")
vol_daily = ParquetMarketStore.read_bars_path(store_path, "day")
qqq_bars = [b for b in bars if b.symbol == "QQQ.US"]

# Period bars: premarket (09:00-09:29 ET) + RTH (09:30-16:00 ET)
# reset_daily_context=True resets available to empty at each day start,
# so each day uses only the day's premarket bars for indicator warmup.
period_bars = [
    b for b in qqq_bars
    if start <= b.start.date() <= end
    and time(9, 0) <= b.start.astimezone(ET).time() < time(16, 0)
]
warmup_bars: list = []  # premarket bars are now included in period_bars
VIX = ".VIX.US"
vol = [b for b in vol_5m if b.symbol == VIX and b.start.date() >= date(2026, 5, 1)]
vol_d = [b for b in vol_daily if b.symbol == VIX and b.start.date() >= date(2026, 5, 1)]

rth_bars = [b for b in period_bars if b.start.astimezone(ET).time() >= time(9, 30)]
premarket_count = len(period_bars) - len(rth_bars)
trading_dates = sorted(set(b.start.astimezone(ET).date() for b in rth_bars))
print(f"Period: {trading_dates[0]} to {trading_dates[-1]} ({len(trading_dates)} days)")
print(f"Bars: {len(rth_bars)}, Premarket: {premarket_count}, Vol5m: {len(vol)}, VolD: {len(vol_d)}")
print()

_compare_all = args.compare_all

results: dict[str, dict] = {}
_modes = ["boll", "trend", "hybrid"] if _compare_all else ["hybrid"]
for mode in _modes:
    print(f"Running {mode}...", flush=True)
    s = Settings(trading_mode="replay", strategy_mode=mode)
    tester = EventDrivenBacktester(s, None, ContractSelector(), RiskEngine(s))
    r = tester.run(
        period_bars, {}, Decimal("100000"), vol, vol_d,
        trade_start=start, reset_daily_context=True,
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

if _compare_all:
    # ── Side-by-side comparison table ─────────────────────────────────────────
    col = 22  # width per strategy column
    sep = "-" * (12 + 3 * col + 4)
    print()
    print("=" * (12 + 3 * col + 4))
    print(f"{'THREE-STRATEGY DAILY COMPARISON':^{12 + 3 * col + 4}}")
    print("=" * (12 + 3 * col + 4))
    hdr = f"{'Date':<12}" + "".join(f" | {'BOLL':>{col}}" + "" for _ in ["x"])
    print(
        f"{'Date':<12}"
        f" | {'--- BOLL ---':^{col}}"
        f" | {'--- TREND ---':^{col}}"
        f" | {'--- HYBRID ---':^{col}}"
    )
    print(
        f"{'':12}"
        f" | {'T  PnL  Cum':^{col}}"
        f" | {'T  PnL  Cum':^{col}}"
        f" | {'T  PnL  Cum':^{col}}"
    )
    print(sep)
    cums = {m: Decimal(0) for m in ["boll", "trend", "hybrid"]}
    for d in trading_dates:
        parts = []
        for m in ["boll", "trend", "hybrid"]:
            dd = results[m]["daily"].get(d, {"trades": 0, "pnl": Decimal(0)})
            cums[m] += dd["pnl"]
            if dd["trades"]:
                parts.append(f"{dd['trades']:>2}T ${dd['pnl']:>+7,.0f} ${cums[m]:>+9,.0f}")
            else:
                parts.append(f"{'--':^{col}}")
        print(f"{str(d):<12} | {parts[0]:^{col}} | {parts[1]:^{col}} | {parts[2]:^{col}}")
    print(sep)
    for m in ["boll", "trend", "hybrid"]:
        t = results[m]["total"]
        wr = f"{t['wins']}/{t['trades']} ({t['wins']/t['trades']*100:.0f}%)" if t["trades"] else "N/A"
        rej = sum(t["rejected"].values())
        print(f"{m.upper():<8}: {t['trades']:>3}T  PnL=${t['pnl']:>+9,.0f}  Win={wr}  Rejected={rej}")

else:
    # ── Single hybrid results (original output) ────────────────────────────────
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

    # Print trade details (grouped by day)
    from itertools import groupby
    from operator import attrgetter

    print(f"\n--- HYBRID Trade Details (by Day) ---")
    trades_sorted = sorted(results["hybrid"]["trades"], key=lambda t: t.entry_at)
    for day, day_trades in groupby(trades_sorted, key=lambda t: t.entry_at.astimezone(ET).date()):
        day_list = list(day_trades)
        day_pnl = sum(t.pnl for t in day_list)
        print(f"\n  {str(day)}  ({len(day_list)} trades, PnL=${day_pnl:>+,.0f})")
        for t in day_list:
            d = t.direction.value
            entry = t.entry_at.astimezone(ET).strftime("%H:%M")
            exit_t = t.exit_at.astimezone(ET).strftime("%H:%M")
            strategy = getattr(t, "strategy", "") or ""
            print(f"    {d:4s}  entry={entry}  exit={exit_t}  PnL=${t.pnl:>+7,.0f}  reason={t.exit_reason:<25}  {strategy}")

# ── Close output file ──────────────────────────────────────────────────────────
if _out_file:
    sys.stdout = sys.__stdout__
    _out_file.close()
    print(f"Report saved to: {args.output}")
