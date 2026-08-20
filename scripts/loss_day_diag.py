"""Diagnostic analysis for top loss days in Hybrid strategy."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from qqq_trader.backtest import EventDrivenBacktester
from qqq_trader.config import NY_TZ, Settings
from qqq_trader.indicators import ema_series, macd_histogram, vwap
from qqq_trader.persistence import ParquetMarketStore
from qqq_trader.risk import ContractSelector, RiskEngine

ET = ZoneInfo("America/New_York")
store_path = Path("data/market/bars")
bars = ParquetMarketStore.read_bars_path(store_path, "1m")
vol_5m = ParquetMarketStore.read_bars_path(store_path, "5m")
vol_daily = ParquetMarketStore.read_bars_path(store_path, "day")

qqq_bars = [b for b in bars if b.symbol == "QQQ.US"]

LOSS_DAYS = [date(2026, 8, 12), date(2026, 7, 10), date(2026, 8, 5),
             date(2026, 7, 8), date(2026, 7, 23), date(2026, 7, 20),
             date(2026, 8, 14)]
for target_day in LOSS_DAYS:
    print(f"\n{'='*80}")
    print(f"  ANALYSIS: {target_day}")
    print(f"{'='*80}")

    day_bars = sorted(
        [b for b in qqq_bars
         if b.start.astimezone(ET).date() == target_day
         and time(9, 30) <= b.start.astimezone(ET).time().replace(tzinfo=None) < time(16, 0)],
        key=lambda b: b.start,
    )

    if not day_bars:
        print("  No data")
        continue

    # Opening Range (9:30-9:39)
    or_bars = [b for b in day_bars
               if b.start.astimezone(ET).time().replace(tzinfo=None) < time(9, 40)]
    or_high = max(b.high for b in or_bars)
    or_low = min(b.low for b in or_bars)
    or_range = or_high - or_low
    or_mid = (or_high + or_low) / 2

    print(f"  OR: high={or_high}, low={or_low}, range={or_range}")
    print(f"  Open={day_bars[0].open}, First close={day_bars[0].close}")

    # Show bars from 9:40 to 10:05 with indicators
    print(f"\n  {'Time':<8} {'Open':>8} {'Close':>8} {'Vol':>6} {'vs OR':>8} {'EMA9':>8} {'EMA21':>8} {'MACD_H':>8}")
    print(f"  {'-'*72}")

    phase2_bars = [b for b in day_bars
                   if time(9, 40) <= b.start.astimezone(ET).time().replace(tzinfo=None) <= time(10, 5)]

    for bar in phase2_bars:
        bar_time = bar.end.astimezone(ET).time().replace(tzinfo=None)
        bars_up_to = [b for b in day_bars if b.end <= bar.end]
        closes = [b.close for b in bars_up_to]

        ema9 = ema_series(closes, 9)[-1] if len(closes) >= 9 else Decimal(0)
        ema21 = ema_series(closes, 21)[-1] if len(closes) >= 21 else Decimal(0)

        macd_h = Decimal(0)
        if len(closes) >= 23:
            _, _, macd_h = macd_histogram(closes, 8, 17, 6)

        if bar.close > or_high:
            vs_or = f"+{bar.close - or_high:.2f}"
        elif bar.close < or_low:
            vs_or = f"-{or_low - bar.close:.2f}"
        else:
            vs_or = "inside"

        print(f"  {str(bar_time)[:5]:<8} {bar.open:>8.2f} {bar.close:>8.2f} "
              f"{bar.volume:>6} {vs_or:>8} {ema9:>8.2f} {ema21:>8.2f} {macd_h:>8.4f}")

    # VWAP at key points
    bars_to_10 = [b for b in day_bars
                  if b.start.astimezone(ET).time().replace(tzinfo=None) < time(10, 0)]
    if bars_to_10:
        vwap_10 = vwap(bars_to_10)
        print(f"\n  VWAP at 10:00: {vwap_10:.2f}")
        print(f"  Price at 10:00: {bars_to_10[-1].close:.2f}")
        print(f"  Price vs OR_high: {bars_to_10[-1].close - or_high:+.2f}")
        print(f"  Price vs OR_low: {bars_to_10[-1].close - or_low:+.2f}")

    # Check VIX trend
    vix_bars = [b for b in vol_5m
                if b.symbol == ".VIX.US"
                and b.start.astimezone(ET).date() == target_day
                and b.start.astimezone(ET).time().replace(tzinfo=None) < time(10, 0)]
    if vix_bars:
        vix_open = vix_bars[0].open if vix_bars else None
        vix_last = vix_bars[-1].close if vix_bars else None
        if vix_open and vix_last:
            vix_change = (vix_last - vix_open) / vix_open * 100
            print(f"  VIX (5m): open={vix_open:.2f} → {vix_last:.2f} ({vix_change:+.2f}%)")

    # Overall day result
    day_open = day_bars[0].open
    day_close = day_bars[-1].close
    day_high = max(b.high for b in day_bars)
    day_low = min(b.low for b in day_bars)
    print(f"\n  Day: open={day_open}, close={day_close}, "
          f"high={day_high}, low={day_low}")
    print(f"  Day range: {day_high - day_low:.2f}")
    print(f"  Direction: {'UP' if day_close > day_open else 'DOWN'} "
          f"({day_close - day_open:+.2f})")
