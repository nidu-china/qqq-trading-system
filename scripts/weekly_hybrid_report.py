"""Quick weekly Hybrid mode analysis."""
from datetime import date, time
from decimal import Decimal
from pathlib import Path

from qqq_trader.backtest import EventDrivenBacktester
from qqq_trader.config import NY_TZ, Settings
from qqq_trader.persistence import ParquetMarketStore
from qqq_trader.risk import ContractSelector, RiskEngine

# Load last week data only
store_path = Path("data/market/bars")
all_bars = ParquetMarketStore.read_bars_path(store_path, "1m")
qqq_bars = [b for b in all_bars if b.symbol == "QQQ.US"]

# Last 5 trading days
target_dates = [
    date(2026, 8, 20),
    date(2026, 8, 21),
    date(2026, 8, 24),
    date(2026, 8, 25),
    date(2026, 8, 26),
]

period_bars = [b for b in qqq_bars if b.start.astimezone(NY_TZ).date() in target_dates]
warmup_bars = [b for b in qqq_bars if b.start.astimezone(NY_TZ).date() < min(target_dates)][-2000:]

# VIX data
all_vix = ParquetMarketStore.read_bars_path(store_path, "1m")
vix_1m = [b for b in all_vix if b.symbol == ".VIX.US"]
all_vix_daily = ParquetMarketStore.read_bars_path(store_path, "day")
vix_daily = [b for b in all_vix_daily if b.symbol == ".VIX.US"]

print("=" * 100)
print(f"{'HYBRID MODE BACKTEST - LAST WEEK':^100}")
print("=" * 100)
print(f"\nAnalyzing {len(target_dates)} trading days from {min(target_dates)} to {max(target_dates)}")
print(f"QQQ bars: {len(period_bars)}, VIX 1m bars: {len(vix_1m)}\n")

# Run Hybrid backtest
settings = Settings(trading_mode="replay", strategy_mode="hybrid", volatility_filter_enabled=True)
tester = EventDrivenBacktester(settings, None, ContractSelector(), RiskEngine(settings))
result = tester.run(
    period_bars, {}, Decimal("10000"), vix_1m, vix_daily,
    trade_start=min(target_dates), warmup_bars=warmup_bars
)

print("\n" + "=" * 100)
print(f"{'OVERALL RESULTS':^100}")
print("=" * 100)
print(f"Starting Equity:  ${result.starting_equity:>10,.2f}")
print(f"Ending Equity:    ${result.ending_equity:>10,.2f}")
print(f"Net P&L:          ${result.ending_equity - result.starting_equity:>+10,.2f}")
print(f"Return:           {((result.ending_equity / result.starting_equity - 1) * 100):>+10.2f}%")
print(f"Signals:          {result.signals:>10}")
print(f"Trades:           {len(result.trades):>10}")
if result.trades:
    wins = sum(1 for t in result.trades if t.pnl > 0)
    print(f"Win Rate:         {(wins / len(result.trades) * 100):>10.1f}%")
    total_pnl = sum(t.pnl for t in result.trades)
    print(f"Total P&L:        ${total_pnl:>+10,.2f}")

print(f"\nVIX Regimes:      {dict(result.volatility_regimes)}")
if result.rejected:
    print(f"Rejected Signals: {dict(result.rejected)}")

# Daily breakdown
print("\n" + "=" * 100)
print(f"{'DAILY OPERATIONS':^100}")
print("=" * 100)
print(f"\n{'Date':<12} {'Strategy':<20} {'Direction':<8} {'Entry':<8} {'Exit':<8} "
      f"{'Duration':<10} {'P&L':>10} {'Exit Reason':<25}")
print("-" * 100)

from collections import defaultdict
daily_trades = defaultdict(list)
for trade in result.trades:
    d = trade.entry_at.astimezone(NY_TZ).date()
    daily_trades[d].append(trade)

for trading_date in sorted(target_dates):
    trades = daily_trades.get(trading_date, [])
    if not trades:
        print(f"{trading_date}     No trades")
        continue
    
    for trade in trades:
        entry_time = trade.entry_at.astimezone(NY_TZ).strftime("%H:%M:%S")
        exit_time = trade.exit_at.astimezone(NY_TZ).strftime("%H:%M:%S")
        duration_min = int((trade.exit_at - trade.entry_at).total_seconds() / 60)
        pnl_sign = "+" if trade.pnl >= 0 else ""
        
        print(f"{trading_date:<12} {trade.strategy:<20} {trade.direction.value:<8} "
              f"{entry_time:<8} {exit_time:<8} {duration_min:>7}min "
              f"${pnl_sign}{trade.pnl:>8,.2f} {trade.exit_reason:<25}")
        
        if len(trade.exit_legs) > 1:
            for i, leg in enumerate(trade.exit_legs, 1):
                leg_time = leg.exit_at.astimezone(NY_TZ).strftime("%H:%M:%S")
                leg_pnl_sign = "+" if leg.pnl >= 0 else ""
                print(f"{'':12}   Leg {i}: {leg.quantity} @ {leg_time}  "
                      f"${leg.price:.2f}  ${leg_pnl_sign}{leg.pnl:,.2f}  {leg.reason}")

print("-" * 100)

# Signal analysis
print("\n" + "=" * 100)
print(f"{'SIGNAL DETAILS':^100}")
print("=" * 100)

from collections import Counter
signal_dates = defaultdict(list)
for sig in result.signal_records:
    d = sig["decision_at"][:10]
    signal_dates[d].append(sig)

for trading_date in sorted([str(d) for d in target_dates]):
    signals = signal_dates.get(trading_date, [])
    if not signals:
        continue
    
    print(f"\n{trading_date}")
    print("-" * 100)
    accepted = [s for s in signals if s["status"] == "accepted"]
    rejected = [s for s in signals if s["status"] == "rejected"]
    
    if accepted:
        print(f"  ✅ Accepted signals: {len(accepted)}")
        for sig in accepted:
            time_str = sig["decision_at"][11:19]
            print(f"     {time_str} {sig['action'].upper()} {sig['direction']} @ ${sig.get('price', 'N/A')} | {sig['reason']}")
    
    if rejected:
        print(f"  ❌ Rejected signals: {len(rejected)}")
        reasons = Counter(s["reason"] for s in rejected)
        for reason, count in reasons.most_common():
            print(f"     {reason}: {count}x")

print("\n" + "=" * 100)
