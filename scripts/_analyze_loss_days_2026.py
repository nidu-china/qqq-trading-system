"""Analyze top loss days from July-August 2026 backtest."""
from __future__ import annotations

from datetime import date, time
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from qqq_trader.indicators import ema_series, macd_histogram, vwap
from qqq_trader.persistence import ParquetMarketStore

ET = ZoneInfo("America/New_York")
store_path = Path("data/market/bars")
bars = ParquetMarketStore.read_bars_path(store_path, "1m")
vol_5m = ParquetMarketStore.read_bars_path(store_path, "5m")

qqq_bars = [b for b in bars if b.symbol == "QQQ.US"]

# Top loss days from backtest
LOSS_DAYS = [
    (date(2026, 8, 20), -1675),
    (date(2026, 7, 2), -790),
    (date(2026, 7, 8), -770),
    (date(2026, 7, 10), -722),
    (date(2026, 8, 5), -660),
    (date(2026, 7, 1), -640),
    (date(2026, 8, 12), -620),
    (date(2026, 7, 23), -600),
    (date(2026, 8, 21), -580),
]

for target_day, loss_amt in LOSS_DAYS:
    print(f"\n{'='*90}")
    print(f"  {target_day} | PnL: ${loss_amt}")
    print(f"{'='*90}")

    day_bars = sorted(
        [b for b in qqq_bars
         if b.start.astimezone(ET).date() == target_day
         and time(9, 30) <= b.start.astimezone(ET).time().replace(tzinfo=None) < time(16, 0)],
        key=lambda b: b.start,
    )

    if not day_bars:
        print("  No data available")
        continue

    # Opening Range (9:30-9:39)
    or_bars = [b for b in day_bars
               if b.start.astimezone(ET).time().replace(tzinfo=None) < time(9, 40)]
    or_high = max(b.high for b in or_bars) if or_bars else day_bars[0].high
    or_low = min(b.low for b in or_bars) if or_bars else day_bars[0].low
    or_range = or_high - or_low

    print(f"  Opening Range: High=${or_high:.2f}, Low=${or_low:.2f}, Range=${or_range:.2f}")

    # Price action at 10:00 (classification time)
    bars_to_10 = [b for b in day_bars
                  if b.start.astimezone(ET).time().replace(tzinfo=None) < time(10, 0)]
    if bars_to_10:
        price_10 = bars_to_10[-1].close
        vwap_10 = vwap(bars_to_10)
        
        closes_10 = [b.close for b in bars_to_10]
        ema9_10 = ema_series(closes_10, 9)[-1] if len(closes_10) >= 9 else None
        ema21_10 = ema_series(closes_10, 21)[-1] if len(closes_10) >= 21 else None
        
        print(f"\n  At 10:00 ET (classification):")
        print(f"    Price: ${price_10:.2f}")
        print(f"    VWAP: ${vwap_10:.2f} | Price vs VWAP: {price_10 - vwap_10:+.2f}")
        print(f"    vs OR: ", end="")
        if price_10 > or_high:
            print(f"ABOVE (+${price_10 - or_high:.2f}) -> Trend UP")
        elif price_10 < or_low:
            print(f"BELOW (-${or_low - price_10:.2f}) -> Trend DOWN")
        else:
            print(f"INSIDE -> Oscillation")
        
        if ema9_10 and ema21_10:
            ema_cross = "EMA9 > EMA21 (bullish)" if ema9_10 > ema21_10 else "EMA9 < EMA21 (bearish)"
            print(f"    EMA: {ema_cross}")

    # Day result
    day_high = max(b.high for b in day_bars)
    day_low = min(b.low for b in day_bars)
    day_close = day_bars[-1].close
    day_open = day_bars[0].open
    day_range = day_high - day_low
    
    print(f"\n  Full Day Summary:")
    print(f"    Open: ${day_open:.2f} | Close: ${day_close:.2f} ({day_close - day_open:+.2f})")
    print(f"    High: ${day_high:.2f} | Low: ${day_low:.2f} | Range: ${day_range:.2f}")
    
    # Calculate price action patterns
    lower_half = day_low + day_range * Decimal("0.5")
    if bars_to_10 and price_10 < or_low and day_close > lower_half:
        print(f"  Pattern: False breakdown")
    elif bars_to_10 and price_10 > or_high and day_close < lower_half:
        print(f"  Pattern: False breakout")
    
    # Check VIX behavior
    vix_day = [b for b in vol_5m
               if b.symbol == ".VIX.US"
               and b.start.astimezone(ET).date() == target_day]
    if len(vix_day) >= 2:
        vix_open = vix_day[0].open
        vix_10am_bars = [b for b in vix_day
                         if b.start.astimezone(ET).time().replace(tzinfo=None) < time(10, 0)]
        if vix_10am_bars:
            vix_10 = vix_10am_bars[-1].close
            vix_change = (vix_10 - vix_open) / vix_open * 100
            print(f"  VIX @ 10:00: {vix_open:.2f} -> {vix_10:.2f} ({vix_change:+.2f}%)", end="")
            if vix_change > 3:
                print(f" [High volatility]")
            else:
                print()
    
    # Identify whipsaw pattern
    reversals = 0
    for i in range(1, len(day_bars)):
        if abs(day_bars[i].close - day_bars[i-1].close) > or_range * Decimal("0.3"):
            reversals += 1
    
    if reversals > 50:
        print(f"  [Whipsaw] High choppiness: {reversals} large moves")

print(f"\n{'='*90}")
print("Analysis completed")
print(f"{'='*90}")
