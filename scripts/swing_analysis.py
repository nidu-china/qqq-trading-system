"""QQQ intraday swing analysis.

For each trading day (09:40-14:00 ET):
  1. Detect all swings using a ZigZag algorithm (reversal threshold = 0.12%)
  2. Select the 5 largest swings by price amplitude
  3. Snapshot key indicators at each swing start bar
  4. Aggregate across all days to find which indicator conditions consistently
     appear at the start of the best up/down swings

Usage:
    python scripts/swing_analysis.py [--days N]
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date, time
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from qqq_trader.indicators import bollinger_bands, ema_series, macd_histogram, rsi, vwap_series
from qqq_trader.persistence import ParquetMarketStore

ET = ZoneInfo("America/New_York")

# --- Configuration ------------------------------------------------------------
REVERSAL_PCT    = Decimal("0.0012")   # 0.12% reversal confirms swing end
MIN_SWING_BARS  = 3                   # minimum bars for a valid swing
TOP_N           = 5                   # top-N swings per day
OR_WINDOW       = (time(9, 30), time(9, 40))
ANALYSIS_WINDOW = (time(9, 40), time(14, 0))

parser = argparse.ArgumentParser(description="QQQ intraday swing analysis")
parser.add_argument("--days",   type=int,  default=0,    help="Limit to last N days (0=all)")
parser.add_argument("--output", type=str,  default=None, help="Save full report to file")
args = parser.parse_args()

import sys, io
_capture = io.StringIO() if args.output else None

class _Tee:
    """Write to both stdout and a capture buffer."""
    def __init__(self, buf): self._buf = buf
    def write(self, s):
        sys.__stdout__.write(s)
        self._buf.write(s)
    def flush(self):
        sys.__stdout__.flush()

if _capture:
    sys.stdout = _Tee(_capture)


# --- Data Loading -------------------------------------------------------------
store_path = Path("data/market/bars")
print("Loading QQQ 1m bars...", flush=True)
all_1m = ParquetMarketStore.read_bars_path(store_path, "1m")
qqq = sorted([b for b in all_1m if b.symbol == "QQQ.US"], key=lambda b: b.start)
print(f"  {len(qqq)} bars loaded", flush=True)


def _et(bar):
    return bar.start.astimezone(ET)

def _t(bar):
    return _et(bar).time().replace(tzinfo=None)

def _d(bar):
    return _et(bar).date()


by_date: dict[date, list] = defaultdict(list)
for b in qqq:
    by_date[_d(b)].append(b)

trading_dates = sorted(by_date.keys())
if args.days > 0:
    trading_dates = trading_dates[-args.days:]

print(f"  Analyzing {len(trading_dates)} days: {trading_dates[0]} to {trading_dates[-1]}\n")


# --- ZigZag Swing Detector ----------------------------------------------------
def find_swings(bars: list) -> list[dict]:
    """Apply ZigZag, return swings sorted by amplitude descending."""
    if len(bars) < MIN_SWING_BARS + 1:
        return []

    direction = None
    ext_i = 0
    ext_p = bars[0].close
    pivots: list[tuple[int, Decimal, str]] = []   # (index, price, 'H'|'L')

    for i, bar in enumerate(bars[1:], 1):
        p = bar.close
        if direction is None:
            if p >= ext_p * (1 + REVERSAL_PCT):
                pivots.append((ext_i, ext_p, 'L'))
                direction, ext_i, ext_p = 'up', i, p
            elif p <= ext_p * (1 - REVERSAL_PCT):
                pivots.append((ext_i, ext_p, 'H'))
                direction, ext_i, ext_p = 'dn', i, p
        elif direction == 'up':
            if p > ext_p:
                ext_i, ext_p = i, p
            elif p <= ext_p * (1 - REVERSAL_PCT):
                pivots.append((ext_i, ext_p, 'H'))
                direction, ext_i, ext_p = 'dn', i, p
        else:   # 'dn'
            if p < ext_p:
                ext_i, ext_p = i, p
            elif p >= ext_p * (1 + REVERSAL_PCT):
                pivots.append((ext_i, ext_p, 'L'))
                direction, ext_i, ext_p = 'up', i, p

    if direction == 'up':
        pivots.append((ext_i, ext_p, 'H'))
    elif direction == 'dn':
        pivots.append((ext_i, ext_p, 'L'))

    swings = []
    for j in range(len(pivots) - 1):
        si, sp, st = pivots[j]
        ei, ep, _  = pivots[j + 1]
        dur = ei - si
        if dur < MIN_SWING_BARS:
            continue
        amp = abs(ep - sp)
        swings.append({
            'dir':         'UP' if st == 'L' else 'DN',
            'start_bar':   bars[si],
            'end_bar':     bars[ei],
            'start_price': sp,
            'end_price':   ep,
            'amp':         amp,
            'amp_pct':     float(amp / sp * 100),
            'dur':         dur,
        })

    swings.sort(key=lambda s: s['amp'], reverse=True)
    return swings


# --- Indicator Snapshot -------------------------------------------------------
def indicator_snap(history: list, or_high: Decimal, or_low: Decimal) -> dict:
    """Return indicator snapshot at the last bar of `history`."""
    closes = [b.close for b in history]
    n = len(closes)

    def safe_macd(fast, slow, sig):
        req = slow + sig - 1
        if n <= req:          # need at least req+1 bars for prev bar calc
            return None, None
        try:
            _, _, h  = macd_histogram(closes,      fast, slow, sig)
            _, _, hp = macd_histogram(closes[:-1], fast, slow, sig)
            return h, hp
        except Exception:
            return None, None

    mf, mfp = safe_macd(5, 10, 3)   # fast MACD  needs >= 13 bars
    ms, msp = safe_macd(8, 17, 9)   # slow MACD  needs >= 26 bars

    e9 = e21 = None
    if n >= 21:
        try:
            e9  = ema_series(closes, 9)[-1]
            e21 = ema_series(closes, 21)[-1]
        except Exception:
            pass

    r14 = None
    if n >= 15:
        try:
            r14 = float(rsi(closes, 14))
        except Exception:
            pass

    bu = bm = bl = bp = None
    if n >= 20:
        try:
            bu, bm, bl = bollinger_bands(closes, 20, Decimal("2"))
            hw = max(bu - bm, Decimal("0.0001"))
            bp = float((closes[-1] - bm) / hw)
        except Exception:
            pass

    rth = [b for b in history if _t(b) >= time(9, 30)]
    vw = vwap_side = None
    if rth:
        try:
            vw = vwap_series(rth)[-1]
            vwap_side = 'above' if closes[-1] > vw else 'below'
        except Exception:
            pass

    rvol = None
    if len(rth) >= 2:
        vols = [b.volume for b in rth]
        avg  = sum(vols[:-1]) / (len(vols) - 1)
        rvol = round(vols[-1] / avg, 2) if avg > 0 else 1.0

    price  = closes[-1]
    or_pos = 'above' if price > or_high else ('below' if price < or_low else 'inside')

    return {
        'close':    float(price),
        'or_pos':   or_pos,
        'ema_bull': (e9 > e21)   if (e9 is not None and e21 is not None) else None,
        'mf':       float(mf)    if mf  is not None else None,
        'mf_pos':   (mf  > 0)    if mf  is not None else None,
        'mf_accel': (mf  > mfp)  if (mf  is not None and mfp is not None) else None,
        'ms':       float(ms)    if ms  is not None else None,
        'ms_pos':   (ms  > 0)    if ms  is not None else None,
        'ms_accel': (ms  > msp)  if (ms  is not None and msp is not None) else None,
        'rsi':      r14,
        'band_pos': bp,
        'vwap_side': vwap_side,
        'rvol':     rvol,
    }


# --- Formatting helpers -------------------------------------------------------
def _b(v):
    """Render bool: Y / N / -"""
    if v is None: return '-'
    return 'Y' if v else 'N'

def _f(v, fmt="+.4f"):
    if v is None: return 'N/A'
    return format(v, fmt)


# --- Main Analysis Loop -------------------------------------------------------
up_snaps:   list[dict] = []
dn_snaps:   list[dict] = []
up_times:   list[str]  = []   # HH:MM of swing starts
dn_times:   list[str]  = []
best_daily: list[dict] = []   # #1 swing per day (for summary table)

W = 115
HDR = (
    f"{'#':>2}  {'Dir':<3}  {'Start':>5}→{'End':<5}  "
    f"{'Amp%':>5}  {'Dur':>3}b  "
    f"{'OR_pos':>7}  {'E9>21':>5}  "
    f"{'MACDf':>8}  {'Fa':>2}  "
    f"{'MACDs':>8}  {'Sa':>2}  "
    f"{'RSI':>5}  {'BandPos':>7}  {'VWAP':>6}  {'Rvol':>5}"
)

print("=" * W)
print(f"{'DAILY TOP-5 SWINGS  (09:40–14:00 ET, ZigZag ≥0.12%)':^{W}}")
print("=" * W)

for day in trading_dates:
    day_bars = sorted(by_date[day], key=lambda b: b.start)

    or_bars = [b for b in day_bars if OR_WINDOW[0] <= _t(b) < OR_WINDOW[1]]
    if not or_bars:
        continue
    or_high = max(b.high for b in or_bars)
    or_low  = min(b.low  for b in or_bars)
    or_rng  = or_high - or_low
    or_pct  = float(or_rng / or_low * 100)

    analysis = [b for b in day_bars if ANALYSIS_WINDOW[0] <= _t(b) < ANALYSIS_WINDOW[1]]
    if len(analysis) < 5:
        continue

    swings = find_swings(analysis)
    if not swings:
        continue

    print(f"\n{'-'*W}")
    print(
        f"  {day}  |  OR ${float(or_low):.2f}–${float(or_high):.2f}"
        f"  (${float(or_rng):.2f}, {or_pct:.3f}%)"
        f"  |  {len(swings)} swings"
    )
    print(HDR)
    print("-" * W)

    for rank, sw in enumerate(swings[:TOP_N], 1):
        sb = sw['start_bar']
        hist = [b for b in day_bars if b.start <= sb.start]
        if not hist:
            continue

        s = indicator_snap(hist, or_high, or_low)
        st_str = _et(sb).strftime("%H:%M")
        en_str = _et(sw['end_bar']).strftime("%H:%M")

        rsi_str  = f"{s['rsi']:5.1f}" if s['rsi'] is not None else "  N/A"
        bp_str   = f"{s['band_pos']:+7.2f}" if s['band_pos'] is not None else "    N/A"
        rvol_str = f"{s['rvol']:5.2f}" if s['rvol'] is not None else "  N/A"

        print(
            f"{rank:>2}  {sw['dir']:<3}  {st_str:>5}→{en_str:<5}  "
            f"{sw['amp_pct']:>4.2f}%  {sw['dur']:>3}b  "
            f"{s['or_pos']:>7}  {_b(s['ema_bull']):>5}  "
            f"{_f(s['mf']):>8}  {_b(s['mf_accel']):>2}  "
            f"{_f(s['ms']):>8}  {_b(s['ms_accel']):>2}  "
            f"{rsi_str}  {bp_str}  "
            f"{(s['vwap_side'] or 'N/A'):>6}  {rvol_str}"
        )

        bucket = (up_snaps, up_times) if sw['dir'] == 'UP' else (dn_snaps, dn_times)
        bucket[0].append(s)
        bucket[1].append(st_str)

        # Collect #1 swing per day for the summary table
        if rank == 1:
            best_daily.append({
                'date':    day,
                'dir':     sw['dir'],
                'start':   st_str,
                'end':     en_str,
                'amp_pct': sw['amp_pct'],
                'dur':     sw['dur'],
                **s,     # merge all indicator fields
            })


# --- Aggregate Statistics -----------------------------------------------------
def _pt(snaps: list[dict], key: str) -> str:
    """% where key is truthy."""
    vals = [s[key] for s in snaps if s[key] is not None]
    if not vals: return "N/A"
    n = sum(1 for v in vals if v)
    return f"{n/len(vals)*100:5.0f}%  ({n}/{len(vals)})"

def _peq(snaps: list[dict], key: str, tgt) -> str:
    vals = [s[key] for s in snaps if s[key] is not None]
    if not vals: return "N/A"
    n = sum(1 for v in vals if v == tgt)
    return f"{n/len(vals)*100:5.0f}%  ({n}/{len(vals)})"

def _avg(snaps: list[dict], key: str) -> str:
    vals = [s[key] for s in snaps if s[key] is not None]
    if not vals: return "N/A"
    return f"{sum(vals)/len(vals):+.3f}  (n={len(vals)})"

def _stats(label: str, snaps: list[dict]) -> None:
    n = len(snaps)
    print(f"\n{'-'*72}")
    print(f"  {label}  [{n} swing starts]")
    print(f"{'-'*72}")
    print(f"  Opening Range position:")
    print(f"    price > OR_HIGH     : {_peq(snaps, 'or_pos', 'above')}")
    print(f"    price inside OR     : {_peq(snaps, 'or_pos', 'inside')}")
    print(f"    price < OR_LOW      : {_peq(snaps, 'or_pos', 'below')}")
    print(f"  EMA(9,21):")
    print(f"    EMA9 > EMA21 [bull] : {_pt(snaps, 'ema_bull')}")
    print(f"  MACD fast (5,10,3):")
    print(f"    histogram > 0       : {_pt(snaps, 'mf_pos')}")
    print(f"    histogram accel     : {_pt(snaps, 'mf_accel')}")
    print(f"    avg histogram       : {_avg(snaps, 'mf')}")
    print(f"  MACD slow (8,17,9):")
    print(f"    histogram > 0       : {_pt(snaps, 'ms_pos')}")
    print(f"    histogram accel     : {_pt(snaps, 'ms_accel')}")
    print(f"    avg histogram       : {_avg(snaps, 'ms')}")
    print(f"  RSI(14):")
    print(f"    average             : {_avg(snaps, 'rsi')}")
    rvals = [s['rsi'] for s in snaps if s['rsi'] is not None]
    if rvals:
        for thr, lbl in [(40, "< 40"), (50, "< 50"), (60, "> 60"), (70, "> 70")]:
            if "<" in lbl:
                c = sum(1 for v in rvals if v < thr)
            else:
                c = sum(1 for v in rvals if v > thr)
            print(f"    RSI {lbl}           : {c/len(rvals)*100:5.0f}%  ({c}/{len(rvals)})")
    print(f"  Bollinger band_position:")
    print(f"    average             : {_avg(snaps, 'band_pos')}")
    bvals = [s['band_pos'] for s in snaps if s['band_pos'] is not None]
    if bvals:
        for thr, lbl in [(-0.65, "< -0.65"), (0, "< 0"), (0.30, "> +0.30"), (0.65, "> +0.65")]:
            if "<" in lbl:
                c = sum(1 for v in bvals if v < thr)
            else:
                c = sum(1 for v in bvals if v > thr)
            print(f"    band_pos {lbl:7s}   : {c/len(bvals)*100:5.0f}%  ({c}/{len(bvals)})")
    print(f"  VWAP:")
    print(f"    price > VWAP        : {_peq(snaps, 'vwap_side', 'above')}")
    print(f"    price < VWAP        : {_peq(snaps, 'vwap_side', 'below')}")
    print(f"  Relative volume  avg : {_avg(snaps, 'rvol')}")

    combos: list[tuple[str, object]] = [
        ("MACDf accel (any sign)",
            lambda s: s['mf_accel'] is True),
        ("MACDf > 0",
            lambda s: s['mf_pos'] is True),
        ("MACDf > 0  AND  accel",
            lambda s: s['mf_pos'] is True and s['mf_accel'] is True),
        ("OR_above  AND  MACDf > 0  AND  accel",
            lambda s: s['or_pos'] == 'above' and s['mf_pos'] is True and s['mf_accel'] is True),
        ("OR_above  AND  MACDf accel",
            lambda s: s['or_pos'] == 'above' and s['mf_accel'] is True),
        ("EMA_bull  AND  MACDf > 0",
            lambda s: s['ema_bull'] is True and s['mf_pos'] is True),
        ("EMA_bull  AND  MACDs > 0",
            lambda s: s['ema_bull'] is True and s['ms_pos'] is True),
        ("OR_above  AND  EMA_bull  AND  MACDs > 0  [CURRENT FILTER]",
            lambda s: s['or_pos'] == 'above' and s['ema_bull'] is True and s['ms_pos'] is True),
        ("VWAP_above  AND  MACDf > 0  AND  accel",
            lambda s: s['vwap_side'] == 'above' and s['mf_pos'] is True and s['mf_accel'] is True),
        ("VWAP_above  AND  MACDf accel",
            lambda s: s['vwap_side'] == 'above' and s['mf_accel'] is True),
    ]
    print(f"\n  Combined signal hit rates:")
    print(f"  {'Signal combination':<52} {'Hit rate':>12}")
    print(f"  {'-'*66}")
    for name, cond in combos:
        hit = sum(1 for s in snaps if cond(s))
        marker = " < current" if "CURRENT" in name else ""
        print(f"  {name:<52} {hit/n*100:5.0f}%  ({hit}/{n}){marker}")


print("\n\n")
print("=" * 80)
print(f"{'AGGREGATE INDICATOR STATISTICS AT SWING STARTS':^80}")
print("=" * 80)
_stats("UPSWING   starts", up_snaps)
_stats("DOWNSWING starts", dn_snaps)


# --- Key Differentiators -----------------------------------------------------
print("\n\n")
print("=" * 80)
print(f"{'KEY DIFFERENTIATORS  (UP vs DOWN swing starts)':^80}")
print("=" * 80)

conds = [
    ("EMA9 > EMA21",           "ema_bull",  True),
    ("MACDf > 0",              "mf_pos",    True),
    ("MACDf accelerating",     "mf_accel",  True),
    ("MACDs > 0",              "ms_pos",    True),
    ("MACDs accelerating",     "ms_accel",  True),
    ("price > VWAP",           "vwap_side", "above"),
    ("price > OR_HIGH",        "or_pos",    "above"),
    ("price < OR_LOW",         "or_pos",    "below"),
    ("price inside OR",        "or_pos",    "inside"),
]

def _frac(snaps, key, tv):
    vals = [s[key] for s in snaps if s[key] is not None]
    if not vals: return 0.0
    return sum(1 for v in vals if v == tv) / len(vals) * 100

print(f"\n  {'Condition':<28}  {'UP%':>8}  {'DN%':>8}  {'Diff':>8}  Bar")
print(f"  {'-'*72}")
for label, key, tv in conds:
    up_p = _frac(up_snaps, key, tv)
    dn_p = _frac(dn_snaps, key, tv)
    diff = up_p - dn_p
    bar  = ("+" * int(abs(diff) / 5)) if diff > 0 else ("-" * int(abs(diff) / 5))
    print(f"  {label:<28}  {up_p:>7.0f}%  {dn_p:>7.0f}%  {diff:>+7.0f}%  {bar}")


# --- Timing Distribution -----------------------------------------------------
print("\n\n")
print("=" * 80)
print(f"{'SWING START TIME DISTRIBUTION (30-min buckets)':^80}")
print("=" * 80)

def _time_dist(times: list[str], label: str) -> None:
    buckets: dict[str, int] = defaultdict(int)
    for t in times:
        h, m = map(int, t.split(":"))
        bm = (m // 30) * 30
        buckets[f"{h:02d}:{bm:02d}"] += 1
    print(f"\n  {label}  (total {len(times)})")
    for k in sorted(buckets):
        c   = buckets[k]
        bar = "#" * c
        pct = c / len(times) * 100 if times else 0
        print(f"    {k}  {bar:<25}  {c:>3}  ({pct:.0f}%)")

_time_dist(up_times, "UPSWING start times")
_time_dist(dn_times, "DOWNSWING start times")

print("\n")
print("=" * 80)
print("Analysis complete.")
print("=" * 80)


# === Best Swing per Day — Summary Table =====================================
print("\n\n")
print("=" * 120)
print(f"{'BEST SWING PER DAY  (#1 by amplitude)':^120}")
print("=" * 120)

TH = (
    f"{'Date':<12}  {'Dir':<3}  {'Start':>5}-{'End':<5}  "
    f"{'Amp%':>5}  {'Dur':>4}b  {'OR_pos':>8}  "
    f"{'EMA9>21':>7}  {'MACDf':>8}  {'Fa':>2}  "
    f"{'MACDs':>8}  {'Sa':>2}  {'RSI':>5}  "
    f"{'BandPos':>7}  {'VWAP':>6}  {'Rvol':>5}"
)
print(TH)
print("-" * 120)

for r in best_daily:
    d     = r['dir']
    or_p  = r.get('or_pos','?')
    eb    = _b(r.get('ema_bull'))
    mf_v  = _f(r.get('mf'))
    mfa   = _b(r.get('mf_accel'))
    ms_v  = _f(r.get('ms'))
    msa   = _b(r.get('ms_accel'))
    rsi_v = f"{r['rsi']:5.1f}" if r.get('rsi') is not None else "  N/A"
    bp_v  = f"{r['band_pos']:+7.2f}" if r.get('band_pos') is not None else "    N/A"
    vwap  = (r.get('vwap_side') or 'N/A')
    rv    = f"{r['rvol']:5.2f}" if r.get('rvol') is not None else "  N/A"
    print(
        f"{str(r['date']):<12}  {d:<3}  {r['start']:>5}-{r['end']:<5}  "
        f"{r['amp_pct']:>4.2f}%  {r['dur']:>4}b  {or_p:>8}  "
        f"{eb:>7}  {mf_v:>8}  {mfa:>2}  "
        f"{ms_v:>8}  {msa:>2}  {rsi_v}  "
        f"{bp_v}  {vwap:>6}  {rv}"
    )


# === Indicator Effectiveness ================================================
print("\n\n")
print("=" * 90)
print(f"{'INDICATOR EFFECTIVENESS  (UP vs DOWN starts, |diff| < 10% = NO EFFECT)':^90}")
print("=" * 90)

all_conds = [
    ("EMA9 > EMA21",          "ema_bull",  True),
    ("EMA9 > EMA21 accel",    "ema_bull",  True),   # same field, kept for symmetry
    ("MACDf > 0",             "mf_pos",    True),
    ("MACDf accelerating",    "mf_accel",  True),
    ("MACDs > 0",             "ms_pos",    True),
    ("MACDs accelerating",    "ms_accel",  True),
    ("price > VWAP",          "vwap_side", "above"),
    ("price < VWAP",          "vwap_side", "below"),
    ("price > OR_HIGH",       "or_pos",    "above"),
    ("price inside OR",       "or_pos",    "inside"),
    ("price < OR_LOW",        "or_pos",    "below"),
]

def _frac2(snaps, key, tv):
    vals = [s[key] for s in snaps if s.get(key) is not None]
    if not vals: return 0.0, 0
    n = sum(1 for v in vals if v == tv)
    return n / len(vals) * 100, len(vals)

print(f"\n  {'Indicator':<30}  {'UP% (n)':>14}  {'DN% (n)':>14}  {'|Diff|':>8}  {'Verdict':>16}")
print(f"  {'-'*88}")

seen_conds = set()
for label, key, tv in all_conds:
    cond_id = (key, str(tv))
    if cond_id in seen_conds:
        continue
    seen_conds.add(cond_id)
    up_p, un = _frac2(up_snaps, key, tv)
    dn_p, dn = _frac2(dn_snaps, key, tv)
    diff = abs(up_p - dn_p)
    verdict = "*** NO EFFECT ***" if diff < 10 else ("USEFUL ^" if up_p > dn_p else "USEFUL v")
    print(f"  {label:<30}  {up_p:>6.0f}% (n={un:<3})  {dn_p:>6.0f}% (n={dn:<3})  {diff:>7.0f}%  {verdict:>16}")

# RSI average comparison
up_rsi = [s['rsi'] for s in up_snaps if s.get('rsi') is not None]
dn_rsi = [s['rsi'] for s in dn_snaps if s.get('rsi') is not None]
if up_rsi and dn_rsi:
    avg_up = sum(up_rsi) / len(up_rsi)
    avg_dn = sum(dn_rsi) / len(dn_rsi)
    diff_rsi = abs(avg_up - avg_dn)
    v_rsi = "*** NO EFFECT ***" if diff_rsi < 5 else ("USEFUL ^" if avg_up > avg_dn else "USEFUL v")
    print(f"  {'avg RSI(14)':<30}  {avg_up:>7.1f}  (n={len(up_rsi):<3})  "
          f"{avg_dn:>7.1f}  (n={len(dn_rsi):<3})  {diff_rsi:>7.1f}   {v_rsi:>16}")

# band_pos average comparison
up_bp = [s['band_pos'] for s in up_snaps if s.get('band_pos') is not None]
dn_bp = [s['band_pos'] for s in dn_snaps if s.get('band_pos') is not None]
if up_bp and dn_bp:
    avg_up = sum(up_bp) / len(up_bp)
    avg_dn = sum(dn_bp) / len(dn_bp)
    diff_bp = abs(avg_up - avg_dn)
    v_bp = "*** NO EFFECT ***" if diff_bp < 0.15 else ("USEFUL ^" if avg_up > avg_dn else "USEFUL v")
    print(f"  {'avg BandPos':<30}  {avg_up:>+7.2f}  (n={len(up_bp):<3})  "
          f"{avg_dn:>+7.2f}  (n={len(dn_bp):<3})  {diff_bp:>7.2f}   {v_bp:>16}")

print(f"\n  Note: 'USEFUL ^' means higher for UP starts  (CALL indicator)")
print(f"        'USEFUL v' means higher for DN starts  (PUT indicator)")
print(f"        '*** NO EFFECT ***' means nearly identical for UP and DN starts")


# === Save output to file ====================================================
if _capture:
    sys.stdout = sys.__stdout__
    out_text = _capture.getvalue()
    Path(args.output).write_text(out_text, encoding="utf-8")
    print(f"\nReport saved to: {args.output}")
