"""Analyze market environment on bad August days vs good July days."""
from pathlib import Path
from qqq_trader.persistence import ParquetMarketStore
from zoneinfo import ZoneInfo
from datetime import time, date
from decimal import Decimal

ET = ZoneInfo("America/New_York")
store = Path("data/market/bars")
bars_1m = ParquetMarketStore.read_bars_path(store, "1m")
bars_5m = ParquetMarketStore.read_bars_path(store, "5m")

qqq_1m = [b for b in bars_1m if b.symbol == "QQQ.US"]
vix_5m = [b for b in bars_5m if b.symbol == ".VIX.US"]

bad_days = [
    date(2026, 8, 10),
    date(2026, 8, 12),
    date(2026, 8, 17),
    date(2026, 8, 28),
]
good_days = [
    date(2026, 7, 17),
    date(2026, 7, 27),
    date(2026, 7, 31),
    date(2026, 8, 3),
    date(2026, 8, 19),
]

print("=" * 80)
print(f"{'MARKET ENVIRONMENT ANALYSIS: BAD vs GOOD DAYS':^80}")
print("=" * 80)

def analyze_day(d, label):
    day_qqq = sorted(
        [b for b in qqq_1m if b.start.astimezone(ET).date() == d
         and time(9, 30) <= b.start.astimezone(ET).time() < time(16, 0)],
        key=lambda b: b.start
    )
    day_vix = sorted(
        [b for b in vix_5m if b.start.astimezone(ET).date() == d
         and time(9, 30) <= b.start.astimezone(ET).time() < time(10, 0)],
        key=lambda b: b.start
    )
    if not day_qqq:
        return

    first_30 = day_qqq[:30]
    if not first_30:
        return

    # Opening range (first 10 bars = 10 min)
    or_bars = day_qqq[:10]
    or_high = max(b.high for b in or_bars)
    or_low = min(b.low for b in or_bars)
    or_range = or_high - or_low

    # Directionality: |net move| / sum(|bar moves|)
    net_move = abs(first_30[-1].close - first_30[0].open)
    gross_move = sum(abs(b.close - b.open) for b in first_30)
    directionality = float(net_move / gross_move) if gross_move > 0 else 0

    # First bar ATR proxy (using OR range / 10 bars)
    atr_proxy = float(or_range)

    # VIX at open
    vix_open = float(day_vix[0].open) if day_vix else None
    vix_first30 = [float(b.close) for b in day_vix[:6]] if day_vix else []

    # Reversal count in first 30 bars
    reversals = sum(
        1 for i in range(1, len(first_30))
        if (first_30[i].close - first_30[i-1].close) * (first_30[i-1].close - first_30[i-2].close) < 0
    ) if len(first_30) >= 3 else 0

    print(f"\n{label} {d}:")
    print(f"  OR range: {float(or_range):.3f}  |  Directionality(30m): {directionality:.0%}")
    print(f"  Reversals(30m): {reversals}/28  |  VIX at open: {vix_open}")
    print(f"  VIX first 30min: {vix_first30}")

print("\n--- BAD DAYS (August choppy) ---")
for d in bad_days:
    analyze_day(d, "BAD ")

print("\n--- GOOD DAYS (trending) ---")
for d in good_days:
    analyze_day(d, "GOOD")
