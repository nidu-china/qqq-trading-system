"""Quick Hybrid strategy backtest for a single trading week.

Usage:
  python scripts/weekly_hybrid_report.py              # last 5 trading days in data
  python scripts/weekly_hybrid_report.py --week 2026-08-18   # week starting Mon 08-18
  python scripts/weekly_hybrid_report.py --start 2026-08-18 --end 2026-08-22
  python scripts/weekly_hybrid_report.py --start 2026-07-01 --end 2026-07-31 --daily-reset

Modes:
  default      -- cumulative mode: indicators accumulate across all days (more stable)
  --daily-reset -- each day restarted with yesterday's full RTH (390 bars) +
                   today's premarket (09:00-09:29); simulates live trading where
                   engine restarts daily but loads yesterday as warmup
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from qqq_trader.backtest import EventDrivenBacktester
from qqq_trader.config import NY_TZ, Settings
from qqq_trader.persistence import ParquetMarketStore
from qqq_trader.risk import ContractSelector, RiskEngine

ET = ZoneInfo("America/New_York")

# ── CLI ─────────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser(description="Weekly Hybrid backtest")
parser.add_argument("--week", type=str, default=None,
                    help="Monday of the target week (YYYY-MM-DD). "
                         "Defaults to the last 5 trading days available.")
parser.add_argument("--start", type=str, default=None, help="Explicit start date (YYYY-MM-DD)")
parser.add_argument("--end",   type=str, default=None, help="Explicit end date (YYYY-MM-DD)")
parser.add_argument("--daily-reset", action="store_true",
                    help="Reset indicator state each day using premarket (09:00-09:29) bars as warmup.")
args = parser.parse_args()

# ── Load data ────────────────────────────────────────────────────────────────
store_path = Path("data/market/bars")
all_1m     = ParquetMarketStore.read_bars_path(store_path, "1m")
all_5m     = ParquetMarketStore.read_bars_path(store_path, "5m")
all_day    = ParquetMarketStore.read_bars_path(store_path, "day")

qqq_1m = [b for b in all_1m if b.symbol == "QQQ.US"]
vix_5m = [b for b in all_5m if b.symbol == ".VIX.US"]
vix_d  = [b for b in all_day if b.symbol == ".VIX.US"]

# All available RTH trading dates
all_rth_dates = sorted({
    b.start.astimezone(ET).date()
    for b in qqq_1m
    if time(9, 30) <= b.start.astimezone(ET).time() < time(16, 0)
})

# ── Determine target week ────────────────────────────────────────────────────
if args.start and args.end:
    from datetime import datetime
    start = datetime.strptime(args.start, "%Y-%m-%d").date()
    end   = datetime.strptime(args.end,   "%Y-%m-%d").date()
    target_dates = [d for d in all_rth_dates if start <= d <= end]
elif args.week:
    from datetime import datetime
    monday = datetime.strptime(args.week, "%Y-%m-%d").date()
    friday = monday + timedelta(days=4)
    target_dates = [d for d in all_rth_dates if monday <= d <= friday]
else:
    # Default: last 5 available trading days
    target_dates = all_rth_dates[-5:]

if not target_dates:
    raise SystemExit("No trading dates found for the specified range.")

start, end = target_dates[0], target_dates[-1]

# ── Bars for the period (RTH + pre-market warmup) ───────────────────────────
period_bars = [
    b for b in qqq_1m
    if start <= b.start.astimezone(ET).date() <= end
    and time(9, 30) <= b.start.astimezone(ET).time() < time(16, 0)
]
warmup_bars = [
    b for b in qqq_1m
    if start <= b.start.astimezone(ET).date() <= end
    and time(9, 0) <= b.start.astimezone(ET).time() < time(9, 30)
]

# In daily-reset mode, premarket bars must be in the main bars list so the
# ordered loop processes them before each RTH session starts.
if args.daily_reset:
    run_bars = sorted(period_bars + warmup_bars, key=lambda b: b.end)
else:
    run_bars = period_bars

# ── Run backtest ─────────────────────────────────────────────────────────────
mode_label = "daily-reset (premarket warmup)" if args.daily_reset else "cumulative (cross-day)"
print("=" * 80)
print(f"{'HYBRID WEEKLY BACKTEST':^80}")
print("=" * 80)
print(f"Period : {start}  to  {end}  ({len(target_dates)} days)")
print(f"Bars   : {len(period_bars)} RTH  +  {len(warmup_bars)} premarket  Mode: {mode_label}")
print()

s = Settings(trading_mode="replay", strategy_mode="hybrid")
tester = EventDrivenBacktester(s, None, ContractSelector(), RiskEngine(s))
r = tester.run(
    run_bars, {}, Decimal("100000"), vix_5m, vix_d,
    trade_start=start,
    reset_daily_context=args.daily_reset,
)

# ── Daily summary ─────────────────────────────────────────────────────────────
daily: dict[date, dict] = defaultdict(lambda: {
    "trades": 0, "wins": 0, "pnl": Decimal(0),
})
for t in r.trades:
    d = t.entry_at.astimezone(ET).date()
    daily[d]["trades"] += 1
    daily[d]["pnl"] += t.pnl
    if t.pnl > 0:
        daily[d]["wins"] += 1

header = f"{'Date':<12} | {'Trades':>6} | {'Win':>5} | {'PnL':>10} | {'Cumulative':>12}"
sep    = "-" * len(header)
print(header)
print(sep)
cumulative = Decimal(0)
for d in target_dates:
    dd = daily.get(d)
    if dd and dd["trades"]:
        cumulative += dd["pnl"]
        wr = f"{dd['wins']}/{dd['trades']}"
        print(f"{str(d):<12} | {dd['trades']:>6} | {wr:>5} | ${dd['pnl']:>+8,.0f} | ${cumulative:>+10,.0f}")
    else:
        print(f"{str(d):<12} | {'--':>6} | {'--':>5} | {'--':>10} | ${cumulative:>+10,.0f}")

print(sep)
total_trades = len(r.trades)
total_wins   = sum(1 for t in r.trades if t.pnl > 0)
total_pnl    = r.ending_equity - r.starting_equity
wr_str = f"{total_wins}/{total_trades} ({total_wins/total_trades*100:.0f}%)" if total_trades else "N/A"
print(f"TOTAL: {total_trades}T  PnL=${total_pnl:>+8,.0f}  Win={wr_str}")

# ── Trade details ─────────────────────────────────────────────────────────────
print(f"\n--- Trade Log ---")
for i, t in enumerate(r.trades, 1):
    d = t.entry_at.astimezone(ET).date()
    entry = t.entry_at.astimezone(ET).strftime("%m/%d %H:%M")
    exit_ = t.exit_at.astimezone(ET).strftime("%H:%M")
    strat = (t.strategy or "")[:22]
    print(f"  #{i:2d} {t.direction.value:4s} {entry}->{exit_}  ${t.pnl:>+7,.0f}  "
          f"{t.exit_reason:<20}  {strat}")

# ── Signal summary ────────────────────────────────────────────────────────────
if r.rejected:
    print(f"\nRejected: {dict(r.rejected)}")
