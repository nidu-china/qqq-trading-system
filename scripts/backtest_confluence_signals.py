"""Backtest validation: multi-signal confluence system (A–G).

Runs the full HybridEngine across all available data and breaks down
performance by:
  - Strategy (signal type)
  - Signal confluence score bucket (0-6, 7-9, 10+)
  - Direction (call / put)
  - Session phase (phase2: 09:40-10:00, main: 10:00-12:00)

Usage:
  python scripts/backtest_confluence_signals.py
  python scripts/backtest_confluence_signals.py --start 2026-07-01 --end 2026-08-31
  python scripts/backtest_confluence_signals.py --start 2026-08-18 --end 2026-09-03
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


# ── CLI ──────────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser(description="Confluence signal backtest validator")
parser.add_argument("--start", type=str, default=None, help="Start date YYYY-MM-DD")
parser.add_argument("--end",   type=str, default=None, help="End date YYYY-MM-DD")
args = parser.parse_args()


# ── Load data ─────────────────────────────────────────────────────────────────
store_path = Path("data/market/bars")
all_1m  = ParquetMarketStore.read_bars_path(store_path, "1m")
all_5m  = ParquetMarketStore.read_bars_path(store_path, "5m")
all_day = ParquetMarketStore.read_bars_path(store_path, "day")

qqq_1m = [b for b in all_1m if b.symbol == "QQQ.US"]
vix_5m = [b for b in all_5m if b.symbol == ".VIX.US"]
vix_d  = [b for b in all_day if b.symbol == ".VIX.US"]

all_rth_dates = sorted({
    b.start.astimezone(ET).date()
    for b in qqq_1m
    if time(9, 30) <= b.start.astimezone(ET).time() < time(16, 0)
})

if args.start and args.end:
    from datetime import datetime as _dt
    start_d = _dt.strptime(args.start, "%Y-%m-%d").date()
    end_d   = _dt.strptime(args.end,   "%Y-%m-%d").date()
    target_dates = [d for d in all_rth_dates if start_d <= d <= end_d]
else:
    target_dates = all_rth_dates

if not target_dates:
    raise SystemExit("No trading dates found.")

start_d, end_d = target_dates[0], target_dates[-1]
print(f"\nRunning confluence backtest: {start_d} → {end_d}  ({len(target_dates)} days)\n")

period_bars = [
    b for b in qqq_1m
    if start_d <= b.start.astimezone(ET).date() <= end_d
    and time(9, 0) <= b.start.astimezone(ET).time() < time(16, 0)
]

# ── Run backtest ──────────────────────────────────────────────────────────────
s       = Settings(trading_mode="replay", strategy_mode="hybrid")
tester  = EventDrivenBacktester(s, None, ContractSelector(), RiskEngine(s))
r = tester.run(
    period_bars, {}, Decimal("100000"), vix_5m, vix_d,
    trade_start=start_d,
)


# ── Helper: extract score from signal indicators ──────────────────────────────
def _score_bucket(trade) -> str:
    """Return score bucket string from signal records matched by trade entry."""
    # Try to find the corresponding signal record
    for rec in r.signal_records:
        if rec.get("action") == "buy" and rec.get("status") == "accepted":
            inds = rec.get("indicators", {})
            raw_score = inds.get("signal_score")
            if raw_score is not None:
                score = int(raw_score)
                if score >= 10:
                    return "10+"
                elif score >= 7:
                    return "7-9"
                else:
                    return "0-6"
    return "N/A"


def _phase(trade) -> str:
    t = trade.entry_at.astimezone(ET).time()
    if t < time(10, 0):
        return "phase2 (09:40-10:00)"
    elif t < time(11, 30):
        return "main   (10:00-11:30)"
    else:
        return "late   (11:30+)"


# Build a mapping from trade entry_at → signal record (for score extraction)
signal_by_entry: dict = {}
for rec in r.signal_records:
    if rec.get("action") == "buy" and rec.get("status") == "accepted":
        from datetime import datetime as _dt2
        key = rec["decision_at"]
        signal_by_entry[key] = rec


def _trade_score(trade) -> str:
    key = trade.entry_at.isoformat()
    rec = signal_by_entry.get(key)
    if rec is None:
        return "N/A"
    score = int(rec.get("indicators", {}).get("signal_score", -1))
    if score < 0:
        return "N/A"
    if score >= 10:
        return "10+"
    elif score >= 7:
        return "7-9"
    else:
        return "0-6"


# ── Summary stats helper ──────────────────────────────────────────────────────
def _stats(trades) -> str:
    n     = len(trades)
    wins  = sum(1 for t in trades if t.pnl > 0)
    total = sum(t.pnl for t in trades)
    avg   = total / n if n else Decimal(0)
    wr    = f"{wins / n * 100:.0f}%" if n else "—"
    return f"n={n:3d}  WR={wr:>4}  PnL=${total:>+9,.0f}  avg=${avg:>+6,.0f}/trade"


# ── Table 1: By strategy type ──────────────────────────────────────────────────
print("=" * 72)
print("TABLE 1 — PERFORMANCE BY SIGNAL TYPE")
print("=" * 72)
by_strategy: dict[str, list] = defaultdict(list)
for t in r.trades:
    by_strategy[t.strategy].append(t)

# Sort by total PnL descending
for strat in sorted(by_strategy, key=lambda k: sum(t.pnl for t in by_strategy[k]), reverse=True):
    print(f"  {strat:<40}  {_stats(by_strategy[strat])}")

# ── Table 2: By signal score bucket ───────────────────────────────────────────
print()
print("=" * 72)
print("TABLE 2 — PERFORMANCE BY CONFLUENCE SCORE BUCKET")
print("=" * 72)
by_score: dict[str, list] = defaultdict(list)
for t in r.trades:
    by_score[_trade_score(t)].append(t)

for bucket in ["10+", "7-9", "0-6", "N/A"]:
    trades = by_score.get(bucket, [])
    if trades:
        print(f"  Score {bucket:<4}  {_stats(trades)}")

# ── Table 3: By direction ──────────────────────────────────────────────────────
print()
print("=" * 72)
print("TABLE 3 — PERFORMANCE BY DIRECTION")
print("=" * 72)
calls = [t for t in r.trades if t.direction.value == "call"]
puts  = [t for t in r.trades if t.direction.value == "put"]
print(f"  CALL  {_stats(calls)}")
print(f"  PUT   {_stats(puts)}")

# ── Table 4: New signals (vwap_pullback, trap) ────────────────────────────────
print()
print("=" * 72)
print("TABLE 4 — NEW SIGNAL TYPES (A–G IMPLEMENTATION)")
print("=" * 72)
new_signals = ["vwap_pullback", "trap_false_breakout", "trap_false_breakdown"]
for sig in new_signals:
    trades = by_strategy.get(sig, [])
    if trades:
        print(f"  {sig:<40}  {_stats(trades)}")
    else:
        print(f"  {sig:<40}  (no trades)")

# ── Table 5: Exit reason breakdown ────────────────────────────────────────────
print()
print("=" * 72)
print("TABLE 5 — EXIT REASON BREAKDOWN")
print("=" * 72)
by_exit: dict[str, list] = defaultdict(list)
for t in r.trades:
    by_exit[t.exit_reason].append(t)

for reason in sorted(by_exit, key=lambda k: len(by_exit[k]), reverse=True):
    print(f"  {reason:<30}  {_stats(by_exit[reason])}")

# ── Table 6: Daily summary ────────────────────────────────────────────────────
print()
print("=" * 72)
print("TABLE 6 — DAILY SUMMARY")
print("=" * 72)
print(f"  {'Date':<12}  {'T':>3}  {'W':>3}  {'PnL':>9}  {'Cumul':>10}  Strategies")
print("  " + "-" * 68)
daily: dict[date, dict] = defaultdict(lambda: {"trades": [], "pnl": Decimal(0)})
for t in r.trades:
    d = t.entry_at.astimezone(ET).date()
    daily[d]["trades"].append(t)
    daily[d]["pnl"] += t.pnl

cumul = Decimal(0)
for d in target_dates:
    dd = daily.get(d)
    if dd and dd["trades"]:
        wins = sum(1 for t in dd["trades"] if t.pnl > 0)
        n    = len(dd["trades"])
        cumul += dd["pnl"]
        strats = ", ".join(sorted({t.strategy for t in dd["trades"]}))
        print(f"  {str(d):<12}  {n:>3}  {wins:>3}  ${dd['pnl']:>+7,.0f}  ${cumul:>+8,.0f}  {strats}")
    else:
        print(f"  {str(d):<12}  {'—':>3}  {'—':>3}  {'—':>9}  ${cumul:>+8,.0f}")

# ── Overall summary ───────────────────────────────────────────────────────────
print()
print("=" * 72)
total_pnl  = r.ending_equity - r.starting_equity
total_wins = sum(1 for t in r.trades if t.pnl > 0)
n          = len(r.trades)
wr_str     = f"{total_wins}/{n} ({total_wins / n * 100:.0f}%)" if n else "0/0"
print(f"OVERALL  {n} trades  WR={wr_str}  PnL=${total_pnl:+,.0f}")
print(f"Signals generated: {r.signals}  Rejected: {dict(r.rejected)}")
print("=" * 72)
