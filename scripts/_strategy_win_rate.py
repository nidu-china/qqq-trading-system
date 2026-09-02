"""Per-strategy win rate breakdown for Jul-Aug 2026."""
import sys
sys.path.insert(0, "src")
from collections import defaultdict
from datetime import date, time
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
from qqq_trader.backtest import EventDrivenBacktester
from qqq_trader.config import Settings
from qqq_trader.persistence import ParquetMarketStore
from qqq_trader.risk import ContractSelector, RiskEngine

store_path = Path("data/market/bars")
all_1m = ParquetMarketStore.read_bars_path(store_path, "1m")
all_5m = ParquetMarketStore.read_bars_path(store_path, "5m")
all_day = ParquetMarketStore.read_bars_path(store_path, "day")
qqq_1m = sorted([b for b in all_1m if b.symbol == "QQQ.US"], key=lambda b: b.start)
vix_5m = [b for b in all_5m if b.symbol == ".VIX.US"]
vix_d  = [b for b in all_day if b.symbol == ".VIX.US"]

START, END = date(2026, 7, 1), date(2026, 8, 28)
period = [b for b in qqq_1m
          if START <= b.start.astimezone(ET).date() <= END
          and time(9, 30) <= b.start.astimezone(ET).time() < time(16, 0)]
warmup = [b for b in qqq_1m
          if START <= b.start.astimezone(ET).date() <= END
          and time(9, 0) <= b.start.astimezone(ET).time() < time(9, 30)]

s = Settings(trading_mode="replay", strategy_mode="hybrid")
tester = EventDrivenBacktester(s, None, ContractSelector(), RiskEngine(s))
print("Running Jul-Aug backtest...", flush=True)
result = tester.run(period, {}, Decimal("100000"), vix_5m, vix_d,
                    trade_start=START, warmup_bars=warmup)

total_pnl = result.ending_equity - result.starting_equity
print(f"Total: {len(result.trades)} trades  PnL=${float(total_pnl):+,.0f}")
print()

stats = defaultdict(lambda: {"wins": 0, "total": 0, "pnl": Decimal(0),
                              "stop_losses": 0, "exits": defaultdict(int)})
for t in result.trades:
    k = t.strategy or "unknown"
    stats[k]["total"] += 1
    stats[k]["pnl"] += t.pnl
    stats[k]["exits"][t.exit_reason] += 1
    if t.pnl > 0:
        stats[k]["wins"] += 1
    if "stop_loss" in t.exit_reason:
        stats[k]["stop_losses"] += 1

print(f"{'Strategy':<35} {'N':>4} {'Win%':>6} {'SL%':>5} {'AvgPnL':>8} {'TotalPnL':>10}")
print("-" * 72)
for k, v in sorted(stats.items(), key=lambda x: x[1]["total"], reverse=True):
    wr  = v["wins"]  / v["total"] * 100 if v["total"] else 0
    slr = v["stop_losses"] / v["total"] * 100 if v["total"] else 0
    avg = float(v["pnl"]) / v["total"] if v["total"] else 0
    print(f"{k:<35} {v['total']:>4}  {wr:>4.0f}%  {slr:>4.0f}%  ${avg:>+7.0f}  ${float(v['pnl']):>+9,.0f}")

print()
print("Exit reason breakdown per strategy:")
for k, v in sorted(stats.items(), key=lambda x: x[1]["total"], reverse=True):
    exits_str = "  ".join(f"{r}:{c}" for r, c in sorted(v["exits"].items(), key=lambda x: -x[1]))
    print(f"  {k:<35}: {exits_str}")
