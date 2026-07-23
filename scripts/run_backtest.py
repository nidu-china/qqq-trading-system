"""Run backtest for July and output detailed trade analysis."""
from datetime import date, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo
from qqq_trader.config import Settings
from qqq_trader.persistence import ParquetMarketStore
from qqq_trader.strategy import strategy_from_settings
from qqq_trader.backtest import EventDrivenBacktester
from qqq_trader.risk import ContractSelector, RiskEngine

ET = ZoneInfo("America/New_York")
settings = Settings(trading_mode="replay", volatility_filter_enabled=False)
store = ParquetMarketStore(settings.data_dir)

start = date(2026, 7, 1)
end = date(2026, 7, 22)

bars = []
for d in range((end - start).days + 1):
    day = start + timedelta(days=d)
    path = settings.data_dir / "bars" / f"symbol=QQQ.US" / f"date={day.isoformat()}" / "1m.parquet"
    if path.exists():
        day_bars = ParquetMarketStore.read_bars(path)
        bars.extend(day_bars)
        print(f"  {day} : {len(day_bars)} bars")
    else:
        print(f"  {day} : no data")

vix_bars_5m = []
vix_bars_daily = []
for d in range((end - start).days + 1):
    day = start + timedelta(days=d)
    p5 = settings.data_dir / "bars" / f"symbol={settings.volatility_symbol}" / f"date={day.isoformat()}" / "5m.parquet"
    pd = settings.data_dir / "bars" / f"symbol={settings.volatility_symbol}" / f"date={day.isoformat()}" / "day.parquet"
    if p5.exists():
        vix_bars_5m.extend(ParquetMarketStore.read_bars(p5))
    if pd.exists():
        vix_bars_daily.extend(ParquetMarketStore.read_bars_path(pd, "day"))

print(f"\nQQQ 1m bars: {len(bars)}")
print(f"VIX 5m bars: {len(vix_bars_5m)}")
print(f"VIX daily bars: {len(vix_bars_daily)}")

strategy = strategy_from_settings(settings)
tester = EventDrivenBacktester(
    settings, strategy,
    ContractSelector(settings.strike_offset),
    RiskEngine(settings),
)
result = tester.run(bars, {}, Decimal("100000"), vix_bars_5m, vix_bars_daily)

pnl = result.ending_equity - result.starting_equity
pnl_pct = pnl / result.starting_equity * 100

print(f"\n{'='*90}")
print(f"BACKTEST RESULTS: {start} to {end}")
print(f"{'='*90}")
print(f"Starting equity: ${result.starting_equity:,.2f}")
print(f"Ending equity:   ${result.ending_equity:,.2f}")
print(f"PnL:             ${pnl:+,.2f} ({pnl_pct:+.2f}%)")
print(f"Trades: {len(result.trades)}")
print(f"Signals accepted: {result.signals}")

# Win/loss analysis
wins = [t for t in result.trades if t.pnl > 0]
losses = [t for t in result.trades if t.pnl <= 0]
if result.trades:
    print(f"Win/Loss: {len(wins)}W / {len(losses)}L  ({len(wins)/len(result.trades)*100:.0f}% win rate)")
    total_win = sum(t.pnl for t in wins)
    total_loss = sum(t.pnl for t in losses)
    print(f"Total Win: ${total_win:+,.2f}  Total Loss: ${total_loss:+,.2f}")
    if total_loss != 0:
        print(f"Profit Factor: {abs(total_win/total_loss):.2f}")

# Detailed signal records with buy/sell time pairs
print(f"\n{'='*90}")
print(f"DETAILED TRADE LOG (Buy/Sell with ET times)")
print(f"{'='*90}")
print(f"{'Date':<12} {'Entry ET':<10} {'Exit ET':<10} {'Dir':<5} {'Strategy':<14} {'Entry Reason':<14} {'Exit Reason':<16} {'Spot':<8}")
print(f"{'-'*90}")

# Group signal records into entry/exit pairs
entries = []
for rec in result.signal_records:
    t = rec.get("decision_at", "")
    if not t:
        continue
    from datetime import datetime
    if isinstance(t, str):
        try:
            dt = datetime.fromisoformat(t)
        except (ValueError, TypeError):
            continue
    else:
        dt = t
    dt_et = dt.astimezone(ET)
    
    indicators = rec.get("indicators", {})
    strat = indicators.get("strategy", "?")
    
    if rec["status"] == "accepted":
        entries.append({
            "date": dt_et.strftime("%Y-%m-%d"),
            "entry_time": dt_et.strftime("%H:%M"),
            "direction": rec["direction"],
            "strategy": strat,
            "entry_reason": rec.get("reason", "-"),
            "spot": indicators.get("vwap", "?")[:7] if "vwap" in indicators else "?",
            "exit_time": None,
            "exit_reason": None,
        })
    elif rec["status"] == "executed" and entries:
        for entry in reversed(entries):
            if entry["exit_time"] is None:
                entry["exit_time"] = dt_et.strftime("%H:%M")
                entry["exit_reason"] = rec.get("reason", "-")
                break

for e in entries:
    exit_t = e["exit_time"] or "?"
    exit_r = e["exit_reason"] or "?"
    print(f"  {e['date']:<10} {e['entry_time']:<10} {exit_t:<10} {e['direction']:<5} {e['strategy']:<14} {e['entry_reason']:<14} {exit_r:<16}")

# Rejection summary
print(f"\n--- Rejected Signals ({sum(result.rejected.values())}) ---")
for reason, count in sorted(result.rejected.items(), key=lambda x: -x[1]):
    print(f"  {reason}: {count}")

# Per-day summary
print(f"\n--- Per-Day Signal Summary ---")
day_signals = {}
for rec in result.signal_records:
    t = rec.get("decision_at", "")
    if not t:
        continue
    from datetime import datetime
    if isinstance(t, str):
        try:
            dt = datetime.fromisoformat(t)
        except (ValueError, TypeError):
            continue
    else:
        dt = t
    day_key = dt.astimezone(ET).strftime("%Y-%m-%d")
    day_signals.setdefault(day_key, {"accepted": 0, "rejected": 0, "executed": 0})
    day_signals[day_key][rec["status"]] = day_signals[day_key].get(rec["status"], 0) + 1

for day, counts in sorted(day_signals.items()):
    print(f"  {day}: accepted={counts.get('accepted',0)} executed={counts.get('executed',0)} rejected={counts.get('rejected',0)}")
