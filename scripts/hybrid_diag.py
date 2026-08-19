"""Diagnose Hybrid mode selection failures in early July 2026.

Analyzes each day's OR characteristics, breakout quality, and compares
Hybrid's mode choice against what would have been optimal.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from qqq_trader.backtest import EventDrivenBacktester
from qqq_trader.config import NY_TZ, Settings
from qqq_trader.indicators import ema_series, vwap, vwap_series
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

# ── Per-day OR analysis ──
print("=" * 100)
print(f"{'HYBRID MODE SELECTION DIAGNOSIS':^100}")
print("=" * 100)
print(f"\n{'Date':<12} {'Mode':>9} {'OR Range':>10} {'OR%':>6} {'BrkDir':>7} {'BrkTime':>8} "
      f"{'Duration':>9} {'Exit':>15} {'PnL':>9} {'Optimal':>9} {'OptPnL':>9}")
print("-" * 100)

# Run both strategies separately to know optimal
trend_results = {}
boll_results = {}

for mode, result_dict in [("trend", trend_results), ("boll_macd", boll_results)]:
    s = Settings(trading_mode="replay", strategy_mode=mode)
    tester = EventDrivenBacktester(s, None, ContractSelector(), RiskEngine(s))
    r = tester.run(period_bars, {}, Decimal("10000"), vol, vol_d,
                   trade_start=start, warmup_bars=warmup_bars)
    for t in r.trades:
        d = t.entry_at.astimezone(ET).date()
        if d not in result_dict:
            result_dict[d] = Decimal(0)
        result_dict[d] += t.pnl

# Run hybrid
s = Settings(trading_mode="replay", strategy_mode="hybrid")
tester = EventDrivenBacktester(s, None, ContractSelector(), RiskEngine(s))
r = tester.run(period_bars, {}, Decimal("10000"), vol, vol_d,
               trade_start=start, warmup_bars=warmup_bars)

hybrid_daily_pnl: dict[date, Decimal] = defaultdict(lambda: Decimal(0))
hybrid_daily_trades: dict[date, list] = defaultdict(list)
for t in r.trades:
    d = t.entry_at.astimezone(ET).date()
    hybrid_daily_pnl[d] += t.pnl
    hybrid_daily_trades[d].append(t)

# Analyze OR for each day
for d in trading_dates:
    day_bars = sorted(
        [b for b in period_bars if b.start.astimezone(ET).date() == d],
        key=lambda b: b.start,
    )
    if not day_bars:
        continue

    or_bars = [
        b for b in day_bars
        if time(9, 30) <= b.start.astimezone(ET).time().replace(tzinfo=None) < time(9, 40)
    ]

    or_high = max(b.high for b in or_bars) if or_bars else Decimal(0)
    or_low = min(b.low for b in or_bars) if or_bars else Decimal(0)
    or_range = or_high - or_low
    mid_price = (or_high + or_low) / 2
    or_pct = (or_range / mid_price * 100) if mid_price > 0 else Decimal(0)

    # Count VWAP crosses in first 30 min after OR
    post_or = [
        b for b in day_bars
        if time(9, 40) <= b.start.astimezone(ET).time().replace(tzinfo=None) < time(10, 10)
    ]
    vwap_crosses = 0
    if len(post_or) >= 2:
        vwap_vals = vwap_series(day_bars[:len(or_bars) + len(post_or)])
        vwap_vals = vwap_vals[len(or_bars):]
        prev_side = None
        for i, (bar, v) in enumerate(zip(post_or, vwap_vals)):
            side = bar.close >= v
            if prev_side is not None and side != prev_side:
                vwap_crosses += 1
            prev_side = side

    # Get hybrid trades for this day
    trades = hybrid_daily_trades.get(d, [])
    pnl = hybrid_daily_pnl.get(d, Decimal(0))

    # Determine actual mode used
    if trades:
        first_trade = trades[0]
        strat = first_trade.strategy or ""
        mode_used = "trend" if strat.startswith("trend_") else "boll"
        direction = first_trade.direction.value
        entry_time = first_trade.entry_at.astimezone(ET).strftime("%H:%M")
        duration_min = int((first_trade.exit_at - first_trade.entry_at).total_seconds() / 60)
        exit_reason = first_trade.exit_reason
    else:
        mode_used = "none"
        direction = "--"
        entry_time = "--"
        duration_min = 0
        exit_reason = "--"

    # Determine optimal mode
    trend_pnl = trend_results.get(d, Decimal(0))
    boll_pnl = boll_results.get(d, Decimal(0))
    if trend_pnl >= boll_pnl and trend_pnl > 0:
        optimal = "trend"
        opt_pnl = trend_pnl
    elif boll_pnl > trend_pnl and boll_pnl > 0:
        optimal = "boll"
        opt_pnl = boll_pnl
    elif trend_pnl >= boll_pnl:
        optimal = "trend" if trend_pnl >= 0 else "flat"
        opt_pnl = max(trend_pnl, Decimal(0))
    else:
        optimal = "boll" if boll_pnl >= 0 else "flat"
        opt_pnl = max(boll_pnl, Decimal(0))

    print(f"{str(d):<12} {mode_used:>9} {or_range:>10.2f} {or_pct:>5.2f}% "
          f"{direction:>7} {entry_time:>8} {duration_min:>6}min "
          f"{str(exit_reason)[:15]:>15} ${pnl:>+8} {optimal:>9} ${opt_pnl:>+8}")

print("-" * 100)

# Summary statistics
print("\n\n")
print("=" * 80)
print(f"{'MODE SELECTION ACCURACY':^80}")
print("=" * 80)

correct = 0
wrong = 0
wrong_cost = Decimal(0)
for d in trading_dates:
    trades = hybrid_daily_trades.get(d, [])
    if not trades:
        continue
    strat = trades[0].strategy or ""
    mode_used = "trend" if strat.startswith("trend_") else "boll"
    pnl = hybrid_daily_pnl.get(d, Decimal(0))
    
    trend_pnl = trend_results.get(d, Decimal(0))
    boll_pnl = boll_results.get(d, Decimal(0))
    
    # Wrong if the other mode would have been profitable and better
    other_pnl = boll_pnl if mode_used == "trend" else trend_pnl
    if pnl < 0 and other_pnl > pnl:
        wrong += 1
        wrong_cost += (pnl - other_pnl)
    else:
        correct += 1

total = correct + wrong
print(f"Correct mode: {correct}/{total} ({correct/total*100:.0f}%)")
print(f"Wrong mode:   {wrong}/{total} ({wrong/total*100:.0f}%)")
print(f"Cost of wrong choices: ${wrong_cost:+.0f}")

# OR Range analysis
print("\n\n")
print("=" * 80)
print(f"{'OR RANGE vs OUTCOME':^80}")
print("=" * 80)
print(f"\n{'OR Range':>10} {'Days':>5} {'Avg PnL':>10} {'Win%':>6} {'Trend Used':>11}")

# Bucket by OR range
or_buckets: dict[str, list] = defaultdict(list)
for d in trading_dates:
    day_bars = sorted(
        [b for b in period_bars if b.start.astimezone(ET).date() == d],
        key=lambda b: b.start,
    )
    or_bars = [
        b for b in day_bars
        if time(9, 30) <= b.start.astimezone(ET).time().replace(tzinfo=None) < time(9, 40)
    ]
    if not or_bars:
        continue
    or_high = max(b.high for b in or_bars)
    or_low = min(b.low for b in or_bars)
    or_range = or_high - or_low
    mid = (or_high + or_low) / 2
    or_pct = float(or_range / mid * 100) if mid > 0 else 0.0

    pnl = hybrid_daily_pnl.get(d, Decimal(0))
    trades = hybrid_daily_trades.get(d, [])
    mode_used = "trend" if trades and (trades[0].strategy or "").startswith("trend_") else "boll"
    
    if or_pct < 0.15:
        bucket = "< 0.15%"
    elif or_pct < 0.30:
        bucket = "0.15-0.30%"
    elif or_pct < 0.50:
        bucket = "0.30-0.50%"
    else:
        bucket = "> 0.50%"
    or_buckets[bucket].append((pnl, mode_used))

for bucket in ["< 0.15%", "0.15-0.30%", "0.30-0.50%", "> 0.50%"]:
    entries = or_buckets.get(bucket, [])
    if not entries:
        continue
    avg_pnl = sum(p for p, _ in entries) / len(entries)
    win_pct = sum(1 for p, _ in entries if p > 0) / len(entries) * 100
    trend_pct = sum(1 for _, m in entries if m == "trend") / len(entries) * 100
    print(f"{bucket:>10} {len(entries):>5} ${avg_pnl:>+9.0f} {win_pct:>5.0f}% {trend_pct:>10.0f}%")

# Stop loss analysis for Trend mode
print("\n\n")
print("=" * 80)
print(f"{'TREND MODE STOP LOSS ANALYSIS':^80}")
print("=" * 80)
print(f"\n{'Date':<12} {'Entry':>6} {'Exit':>6} {'Mins':>5} {'PnL':>9} {'OR Range':>10} {'OR%':>7}")

for d in trading_dates:
    trades = hybrid_daily_trades.get(d, [])
    for t in trades:
        if not (t.strategy or "").startswith("trend_"):
            continue
        if t.exit_reason != "stop_loss":
            continue
        entry_time = t.entry_at.astimezone(ET).strftime("%H:%M")
        exit_time = t.exit_at.astimezone(ET).strftime("%H:%M")
        duration = int((t.exit_at - t.entry_at).total_seconds() / 60)
        
        day_bars = sorted(
            [b for b in period_bars if b.start.astimezone(ET).date() == d],
            key=lambda b: b.start,
        )
        or_bars = [
            b for b in day_bars
            if time(9, 30) <= b.start.astimezone(ET).time().replace(tzinfo=None) < time(9, 40)
        ]
        or_high = max(b.high for b in or_bars) if or_bars else Decimal(0)
        or_low = min(b.low for b in or_bars) if or_bars else Decimal(0)
        or_range = or_high - or_low
        mid = (or_high + or_low) / 2
        or_pct = or_range / mid * 100 if mid > 0 else Decimal(0)
        
        print(f"{str(d):<12} {entry_time:>6} {exit_time:>6} {duration:>5} "
              f"${t.pnl:>+8} {or_range:>10.2f} {or_pct:>6.3f}%")
