"""Analyze strategy signal quality for July 1-22 and suggest optimizations."""
from datetime import date, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo
from qqq_trader.config import Settings
from qqq_trader.persistence import ParquetMarketStore
from qqq_trader.strategy import (
    strategy_from_settings, vwap, ema_series,
)

ET = ZoneInfo("America/New_York")
settings = Settings(trading_mode="replay", volatility_filter_enabled=False)
router = strategy_from_settings(settings)

start = date(2026, 7, 1)
end = date(2026, 7, 22)

all_signals = []

for d_offset in range((end - start).days + 1):
    d = start + timedelta(days=d_offset)
    path = settings.data_dir / "bars" / f"symbol=QQQ.US" / f"date={d.isoformat()}" / "1m.parquet"
    if not path.exists():
        continue
    bars = sorted(ParquetMarketStore.read_bars(path), key=lambda b: b.start)
    if not bars:
        continue

    day_open = float(bars[0].open)
    day_close = float(bars[-1].close)
    day_high = max(float(b.high) for b in bars)
    day_low = min(float(b.low) for b in bars)
    day_direction = "UP" if day_close > day_open else "DOWN"
    day_range = day_high - day_low

    signals_today = []
    for i in range(20, len(bars)):
        window = bars[:i + 1]
        signal = router.evaluate(window)
        if signal:
            bar = bars[i]
            t_et = bar.end.astimezone(ET)
            strat = signal.indicators.get("strategy", "?")

            future_bars = bars[i+1:i+31]
            if future_bars:
                future_high = max(float(b.high) for b in future_bars)
                future_low = min(float(b.low) for b in future_bars)
                future_close_30 = float(future_bars[-1].close)
                entry_price = float(signal.spot)

                if signal.direction.value == "call":
                    max_profit = future_high - entry_price
                    max_loss = entry_price - future_low
                    pnl_30m = future_close_30 - entry_price
                else:
                    max_profit = entry_price - future_low
                    max_loss = future_high - entry_price
                    pnl_30m = entry_price - future_close_30

                quality = "GOOD" if max_profit > max_loss * 1.5 else ("OK" if max_profit > max_loss else "BAD")
            else:
                max_profit = max_loss = pnl_30m = 0
                quality = "N/A"

            sig_info = {
                "date": d, "time_et": t_et.strftime("%H:%M"),
                "strategy": strat, "direction": signal.direction.value,
                "price": float(signal.spot), "quality": quality,
                "max_profit": max_profit, "max_loss": max_loss, "pnl_30m": pnl_30m,
                "day_direction": day_direction, "day_range": day_range,
                "indicators": signal.indicators,
            }
            signals_today.append(sig_info)
            all_signals.append(sig_info)

    if signals_today:
        print(f"\n{'='*100}")
        print(f"  {d} | O={day_open:.2f} H={day_high:.2f} L={day_low:.2f} C={day_close:.2f} | {day_direction} ({day_close-day_open:+.2f}) Range={day_range:.2f}")
        print(f"{'='*100}")
        for s in signals_today:
            ind = s["indicators"]
            detail = ""
            if s["strategy"] == "orb":
                detail = f"ORH={ind.get('orb_high','?')[:7]} ORL={ind.get('orb_low','?')[:7]} VolR={ind.get('volume_ratio','?')[:5]}"
            elif s["strategy"] == "ema_trend":
                detail = f"EMA9={ind.get('ema9','?')[:7]} EMA21={ind.get('ema21','?')[:7]} Spread={ind.get('ema_spread','?')[:8]}"
            print(f"  {s['time_et']} | {s['direction']:>4} | {s['strategy']:<14} | ${s['price']:.2f} | "
                  f"P={s['max_profit']:+.2f} L={s['max_loss']:.2f} 30m={s['pnl_30m']:+.2f} [{s['quality']}] {detail}")

# ======================== SUMMARY ========================
print(f"\n\n{'='*100}")
print("SIGNAL QUALITY ANALYSIS")
print(f"{'='*100}")
print(f"\nTotal signals: {len(all_signals)}")

by_strategy = {}
for s in all_signals:
    by_strategy.setdefault(s["strategy"], []).append(s)

for strat, sigs in sorted(by_strategy.items()):
    good = sum(1 for s in sigs if s["quality"] == "GOOD")
    ok = sum(1 for s in sigs if s["quality"] == "OK")
    bad = sum(1 for s in sigs if s["quality"] == "BAD")
    avg_profit = sum(s["max_profit"] for s in sigs) / len(sigs)
    avg_loss = sum(s["max_loss"] for s in sigs) / len(sigs)
    avg_pnl = sum(s["pnl_30m"] for s in sigs) / len(sigs)
    calls = sum(1 for s in sigs if s["direction"] == "call")
    puts = sum(1 for s in sigs if s["direction"] == "put")

    print(f"\n  [{strat}] ({len(sigs)} signals: {calls} CALL, {puts} PUT)")
    print(f"    Quality: GOOD={good} OK={ok} BAD={bad}  |  Win rate: {(good+ok)/len(sigs)*100:.0f}%")
    print(f"    Avg max profit: {avg_profit:+.2f}  Avg max loss: {avg_loss:.2f}  Avg 30m PnL: {avg_pnl:+.2f}")

# Analyze patterns in BAD signals
print(f"\n\n--- PATTERN ANALYSIS: BAD SIGNALS ---")
bad_signals = [s for s in all_signals if s["quality"] == "BAD"]
if bad_signals:
    # Direction alignment analysis
    aligned = sum(1 for s in bad_signals if 
                  (s["direction"] == "call" and s["day_direction"] == "UP") or
                  (s["direction"] == "put" and s["day_direction"] == "DOWN"))
    counter = len(bad_signals) - aligned
    print(f"  Direction aligned with day trend: {aligned}/{len(bad_signals)} ({aligned/len(bad_signals)*100:.0f}%)")
    print(f"  Counter-trend signals (BAD):      {counter}/{len(bad_signals)} ({counter/len(bad_signals)*100:.0f}%)")

    # Time distribution
    early = sum(1 for s in bad_signals if s["time_et"] < "10:15")
    mid = sum(1 for s in bad_signals if "10:15" <= s["time_et"] < "11:00")
    late = sum(1 for s in bad_signals if s["time_et"] >= "11:00")
    print(f"  Time distribution: Early(<10:15)={early}  Mid(10:15-11:00)={mid}  Late(>11:00)={late}")

    # Consecutive signals issue
    print(f"\n  BAD signals per day:")
    by_day = {}
    for s in bad_signals:
        by_day.setdefault(str(s["date"]), []).append(s)
    for day, sigs in sorted(by_day.items()):
        times = " → ".join(s["time_et"] for s in sigs)
        print(f"    {day}: {len(sigs)} BAD  [{times}]")

# Analyze GOOD signals
print(f"\n\n--- PATTERN ANALYSIS: GOOD SIGNALS ---")
good_signals = [s for s in all_signals if s["quality"] == "GOOD"]
if good_signals:
    aligned = sum(1 for s in good_signals if 
                  (s["direction"] == "call" and s["day_direction"] == "UP") or
                  (s["direction"] == "put" and s["day_direction"] == "DOWN"))
    print(f"  Direction aligned with day trend: {aligned}/{len(good_signals)} ({aligned/len(good_signals)*100:.0f}%)")
    print(f"\n  GOOD signal details:")
    for s in good_signals:
        print(f"    {s['date']} {s['time_et']} | {s['direction']:>4} | {s['strategy']} | "
              f"P={s['max_profit']:+.2f} L={s['max_loss']:.2f} | Day={s['day_direction']} Range={s['day_range']:.1f}")

# Optimization suggestions
print(f"\n\n{'='*100}")
print("OPTIMIZATION SUGGESTIONS")
print(f"{'='*100}")
total = len(all_signals)
bad_count = len(bad_signals)
good_count = len(good_signals)
ok_count = sum(1 for s in all_signals if s["quality"] == "OK")

print(f"""
Current Performance:
  Total signals: {total}
  GOOD: {good_count} ({good_count/total*100:.0f}%)  OK: {ok_count} ({ok_count/total*100:.0f}%)  BAD: {bad_count} ({bad_count/total*100:.0f}%)
  Overall win rate (GOOD+OK): {(good_count+ok_count)/total*100:.0f}%
""")

# Check for repeated signals problem
consecutive_days = {}
for s in all_signals:
    key = (str(s["date"]), s["direction"])
    consecutive_days.setdefault(key, []).append(s["time_et"])

repeat_count = sum(1 for times in consecutive_days.values() if len(times) > 2)
print(f"  Issue 1: Repeated same-direction signals ({repeat_count} day-direction combos with 3+ signals)")
print(f"  → Suggestion: Add cooldown (15-20 min) between same-direction signals")

counter_bad = sum(1 for s in bad_signals if 
    (s["direction"] == "call" and s["day_direction"] == "DOWN") or
    (s["direction"] == "put" and s["day_direction"] == "UP"))
print(f"\n  Issue 2: Counter-trend BAD signals: {counter_bad}/{bad_count}")
print(f"  → Suggestion: Require stronger EMA spread (>0.001) to filter weak trends")

early_bad = sum(1 for s in bad_signals if s["time_et"] < "10:15")
print(f"\n  Issue 3: Early BAD signals (before 10:15): {early_bad}/{bad_count}")
print(f"  → Suggestion: Delay EMA strategy start to 10:15 (allow trend to establish)")
