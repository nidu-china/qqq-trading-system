"""Signal-to-Swing alignment analysis.

For each trading day:
  1. Run HybridEngine on 1-minute bars (same as live/backtest)
  2. Collect every signal emitted
  3. Run ZigZag to find the top-5 swings by amplitude
  4. For each signal, measure alignment with the nearest same-direction swing:
       - bars_lag     = how many bars after the swing start did the signal fire?
       - pct_captured = how much of the swing amplitude was ALREADY gone at signal time?
       - pct_remaining= amplitude still available after signal entry (potential profit)
  5. Aggregate across all days and report win rates

Usage:
    python scripts/signal_alignment_analysis.py [--days N] [--output FILE]
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date, time
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import sys
sys.path.insert(0, "src")
sys.path.insert(0, "tests")

from qqq_trader.hybrid_strategy import HybridEngine
from qqq_trader.indicators import bollinger_bands, ema_series, macd_histogram, rsi, vwap_series
from qqq_trader.persistence import ParquetMarketStore

try:
    from conftest import make_settings
except Exception:
    def make_settings(**kw):  # type: ignore
        raise RuntimeError("run from the project root: python scripts/signal_alignment_analysis.py")

ET = ZoneInfo("America/New_York")

# ─── Config ──────────────────────────────────────────────────────────────────
REVERSAL_PCT     = Decimal("0.0012")
MIN_SWING_BARS   = 3
TOP_N            = 5
OR_WINDOW        = (time(9, 30), time(9, 40))
ANALYSIS_WINDOW  = (time(9, 40), time(14, 0))
# A signal is "aligned" if it fires within this many bars of the swing start
ALIGN_BAR_WINDOW = 10  # 10 bars = 10 minutes
# Minimum remaining amplitude (% of swing) to be considered "worthwhile"
MIN_REMAINING_PCT = 0.30  # must have ≥ 30% of swing left

parser = argparse.ArgumentParser()
parser.add_argument("--days",   type=int,  default=0)
parser.add_argument("--output", type=str,  default=None)
args = parser.parse_args()

import io as _io
_capture = _io.StringIO() if args.output else None

class _Tee:
    def __init__(self, buf): self._buf = buf
    def write(self, s):
        try:
            sys.__stdout__.write(s)
        except UnicodeEncodeError:
            sys.__stdout__.write(s.encode("ascii", "replace").decode("ascii"))
        self._buf.write(s)
    def flush(self): sys.__stdout__.flush()

if _capture:
    sys.stdout = _Tee(_capture)

# ─── Helpers ─────────────────────────────────────────────────────────────────
def _et(bar):    return bar.start.astimezone(ET)
def _t(bar):     return _et(bar).time().replace(tzinfo=None)
def _d(bar):     return _et(bar).date()


def find_top_n_swings(analysis_bars: list, top_n: int = TOP_N) -> list[dict]:
    if len(analysis_bars) < MIN_SWING_BARS + 1:
        return []
    direction = None
    ext_i, ext_p = 0, analysis_bars[0].close
    pivots: list[tuple[int, Decimal, str]] = []
    for i, bar in enumerate(analysis_bars[1:], 1):
        p = bar.close
        if direction is None:
            if p >= ext_p * (1 + REVERSAL_PCT):
                pivots.append((ext_i, ext_p, "L"))
                direction, ext_i, ext_p = "up", i, p
            elif p <= ext_p * (1 - REVERSAL_PCT):
                pivots.append((ext_i, ext_p, "H"))
                direction, ext_i, ext_p = "dn", i, p
        elif direction == "up":
            if p > ext_p:   ext_i, ext_p = i, p
            elif p <= ext_p * (1 - REVERSAL_PCT):
                pivots.append((ext_i, ext_p, "H"))
                direction, ext_i, ext_p = "dn", i, p
        else:
            if p < ext_p:   ext_i, ext_p = i, p
            elif p >= ext_p * (1 + REVERSAL_PCT):
                pivots.append((ext_i, ext_p, "L"))
                direction, ext_i, ext_p = "up", i, p
    if direction == "up":   pivots.append((ext_i, ext_p, "H"))
    elif direction == "dn": pivots.append((ext_i, ext_p, "L"))

    swings = []
    for j in range(len(pivots) - 1):
        si, sp, st = pivots[j]
        ei, ep, _  = pivots[j + 1]
        dur = ei - si
        if dur < MIN_SWING_BARS: continue
        amp = abs(ep - sp)
        swings.append({
            "dir":        "UP" if st == "L" else "DN",
            "start_idx":  si,
            "end_idx":    ei,
            "start_bar":  analysis_bars[si],
            "end_bar":    analysis_bars[ei],
            "start_price":sp,
            "end_price":  ep,
            "amp":        amp,
            "amp_pct":    float(amp / sp * 100),
            "dur":        dur,
        })
    swings.sort(key=lambda s: s["amp"], reverse=True)
    return swings[:top_n]


# ─── Main analysis ────────────────────────────────────────────────────────────
print("Loading QQQ 1m bars...", flush=True)
store_path = Path("data/market/bars")
all_1m = ParquetMarketStore.read_bars_path(store_path, "1m")
qqq = sorted([b for b in all_1m if b.symbol == "QQQ.US"], key=lambda b: b.start)
print(f"  {len(qqq)} bars loaded", flush=True)

by_date: dict[date, list] = defaultdict(list)
for b in qqq:
    by_date[_d(b)].append(b)

trading_dates = sorted(by_date.keys())
if args.days > 0:
    trading_dates = trading_dates[-args.days:]

print(f"  Analyzing {len(trading_dates)} days: {trading_dates[0]} to {trading_dates[-1]}\n")

settings = make_settings()
engine   = HybridEngine(settings)

# ─── Per-day statistics ───────────────────────────────────────────────────────
W = 130
print("=" * W)
print(f"{'SIGNAL-TO-SWING ALIGNMENT ANALYSIS':^{W}}")
print(f"{'Align window = ±10 bars  |  Min remaining amplitude = 30%':^{W}}")
print("=" * W)

SHDR = (
    f"  {'Time':>5}  {'Dir':<4}  {'Strategy':<28}  "
    f"{'Score':>5}  {'MatchedSwing':>12}  {'Lag':>4}b  "
    f"{'Captured%':>9}  {'Remaining%':>10}  {'Verdict':>12}"
)

# Collect daily rows + stats
rows_all:           list[dict] = []
day_stats:          list[dict] = []

# Global counters
total_signals       = 0
aligned_signals     = 0   # signal fired within ALIGN_BAR_WINDOW of a swing start
worthwhile_signals  = 0   # aligned + ≥ MIN_REMAINING_PCT amplitude left
correct_dir_signals = 0   # signal direction matched swing direction
total_top5_swings   = 0
captured_top5       = 0   # top-5 swings that had at least one aligned signal

# Per-strategy counters: {strategy: {fired, aligned, worthy}}
strat_stats: dict[str, dict] = defaultdict(lambda: {"fired": 0, "aligned": 0, "worthy": 0})

# Missed-swing aggregate stats (for diagnosing what we're missing)
missed_details: list[dict] = []

for day in trading_dates:
    day_bars = sorted(by_date[day], key=lambda b: b.start)

    or_bars = [b for b in day_bars if OR_WINDOW[0] <= _t(b) < OR_WINDOW[1]]
    if not or_bars: continue

    analysis = [b for b in day_bars if ANALYSIS_WINDOW[0] <= _t(b) < ANALYSIS_WINDOW[1]]
    if len(analysis) < 5: continue

    top5 = find_top_n_swings(analysis)
    if not top5: continue

    # Build a bar-index lookup for analysis bars
    bar_by_time: dict[object, int] = {b.start: idx for idx, b in enumerate(analysis)}

    # Run strategy on the full day accumulating bars (as in backtest)
    engine._reset_day(day)
    day_signals: list[dict] = []
    prev_signal_bar = None
    # Snapshot engine context at each analysis bar's end (for missed-swing diagnosis)
    ctx_snapshot: dict[object, object] = {}  # bar.start → last_context

    running: list = []
    for b in day_bars:
        running.append(b)
        if _t(b) < ANALYSIS_WINDOW[0]:
            engine.evaluate(running)
            continue
        if _t(b) >= ANALYSIS_WINDOW[1]:
            break
        sig = engine.evaluate(running)
        # Snapshot context right after evaluate (includes latest indicators)
        if engine.last_context is not None:
            ctx_snapshot[b.start] = engine.last_context
        if sig is None:
            continue
        if sig.bar_end == prev_signal_bar:
            continue
        prev_signal_bar = sig.bar_end

        bar_time_ny = sig.bar_end.astimezone(ET).time().replace(tzinfo=None)
        sig_idx = bar_by_time.get(
            next((ab.start for ab in analysis if ab.end == sig.bar_end), None), None
        )
        score_raw = sig.indicators.get("signal_score", "?")
        day_signals.append({
            "time": bar_time_ny.strftime("%H:%M"),
            "dir":  "UP" if sig.direction.value == "call" else "DN",
            "strategy": sig.strategy,
            "score": score_raw,
            "spot": sig.spot,
            "bar_idx": sig_idx,
        })

    if not day_signals:
        continue

    print(f"\n{'─'*W}")
    print(f"  {day}  |  {len(top5)} top-5 swings  |  {len(day_signals)} signals")
    print(f"  Top-5 swings:  ", end="")
    for sw in top5:
        st = _et(sw["start_bar"]).strftime("%H:%M")
        en = _et(sw["end_bar"]).strftime("%H:%M")
        print(f"[{sw['dir']} {st}→{en} {sw['amp_pct']:.2f}%]  ", end="")
    print()
    print(SHDR)
    print("  " + "─" * (W - 2))

    day_aligned = 0
    day_worthy  = 0
    swings_hit: set[int] = set()

    for dsig in day_signals:
        total_signals += 1
        sig_idx = dsig["bar_idx"]
        strat = dsig["strategy"]
        strat_stats[strat]["fired"] += 1
        if "rank_counts" not in strat_stats[strat]:
            strat_stats[strat]["rank_counts"] = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}

        # Find best matching swing for this signal
        best_swing = None
        best_lag   = None
        best_remaining = None
        best_captured  = None

        for sw_i, sw in enumerate(top5):
            if sw["dir"] != dsig["dir"]:
                continue
            sw_start = sw["start_idx"]
            if sig_idx is None:
                continue
            lag = sig_idx - sw_start
            if lag < 0 or lag > ALIGN_BAR_WINDOW:
                continue
            # How much amplitude remains at signal time
            sig_price = float(analysis[sig_idx].close) if sig_idx < len(analysis) else float(dsig["spot"])
            sp, ep = float(sw["start_price"]), float(sw["end_price"])
            total_amp = abs(ep - sp)
            if total_amp == 0:
                continue
            if sw["dir"] == "UP":
                gone = sig_price - sp
            else:
                gone = sp - sig_price
            gone = max(0.0, gone)
            captured_pct  = gone / total_amp * 100
            remaining_pct = (1 - gone / total_amp) * 100
            if best_lag is None or lag < best_lag:
                best_swing    = sw_i
                best_lag      = lag
                best_captured  = captured_pct
                best_remaining = remaining_pct
                matched_sw     = sw

        # Determine verdict
        if best_lag is None:
            verdict    = "NO_MATCH"
            lag_str    = "  -"
            cap_str    = "    -"
            rem_str    = "    -"
            sw_str     = "       -"
        else:
            aligned_signals += 1
            day_aligned += 1
            swings_hit.add(best_swing)
            sw_time = _et(matched_sw["start_bar"]).strftime("%H:%M")
            sw_str = f"{matched_sw['dir']} {sw_time} #{best_swing+1}"
            lag_str = f"{best_lag:>3}"
            cap_str = f"{best_captured:>8.0f}%"
            rem_str = f"{best_remaining:>9.0f}%"
            if best_remaining >= MIN_REMAINING_PCT * 100:
                verdict = "GOOD"
                worthwhile_signals += 1
                day_worthy += 1
                strat_stats[strat]["worthy"] += 1
            else:
                verdict = "LATE(missed)"
            strat_stats[strat]["aligned"] += 1
            rank = best_swing + 1
            if "rank_counts" not in strat_stats[strat]:
                strat_stats[strat]["rank_counts"] = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
            if rank in strat_stats[strat]["rank_counts"]:
                strat_stats[strat]["rank_counts"][rank] += 1

        print(
            f"  {dsig['time']:>5}  {dsig['dir']:<4}  {dsig['strategy']:<28}  "
            f"  {dsig['score']:>4}  {sw_str:>12}  {lag_str:>4}b  "
            f"{cap_str:>9}  {rem_str:>10}  {verdict:>12}"
        )

    captured_top5 += len(swings_hit)
    total_top5_swings += len(top5)

    # Uncaptured top-5 swings for this day — with indicator snapshot at swing start
    uncaptured = [sw for i, sw in enumerate(top5) if i not in swings_hit]
    if uncaptured:
        print(f"\n  MISSED swings (no aligned signal):")
        for sw in uncaptured:
            st = _et(sw["start_bar"]).strftime("%H:%M")
            en = _et(sw["end_bar"]).strftime("%H:%M")
            # Look up the engine context snapshot at the swing-start bar
            ctx = ctx_snapshot.get(sw["start_bar"].start)
            if ctx is not None:
                vwap = float(ctx.vwap_value) if ctx.vwap_value else 0.0
                price = float(sw["start_price"])
                vwap_rel = "above" if price > vwap else "below"
                rsi   = float(ctx.rsi_val)
                mf    = float(ctx.macd_hist)
                bpos_raw = float(
                    (ctx.current_close - ctx.boll_middle)
                    / max(ctx.boll_upper - ctx.boll_middle, Decimal("0.0001"))
                )
                regime = engine.last_state.value
                print(
                    f"    {sw['dir']} {st}→{en}  amp={sw['amp_pct']:.2f}%  dur={sw['dur']}b"
                    f"  | regime={regime}  VWAP={vwap_rel}"
                    f"  RSI={rsi:.0f}  MACDf={mf:.4f}  BandPos={bpos_raw:+.2f}"
                )
                missed_details.append({
                    "dir":    sw["dir"],
                    "regime": regime,
                    "vwap_rel": vwap_rel,
                    "rsi":    rsi,
                    "macd_f": mf,
                    "band_pos": bpos_raw,
                    "amp_pct": sw["amp_pct"],
                })
            else:
                print(f"    {sw['dir']} {st}→{en}  amp={sw['amp_pct']:.2f}%  dur={sw['dur']}b")

    day_stats.append({
        "date":         day,
        "signals":      len(day_signals),
        "aligned":      day_aligned,
        "worthy":       day_worthy,
        "top5":         len(top5),
        "captured":     len(swings_hit),
    })


# ─── Aggregate summary ────────────────────────────────────────────────────────
print(f"\n\n{'═'*80}")
print(f"{'AGGREGATE SUMMARY':^80}")
print(f"{'═'*80}")
print(f"\n  Days analyzed          : {len(day_stats)}")
print(f"  Total signals fired    : {total_signals}")
print(f"  Signals aligned (±10b) : {aligned_signals}  ({aligned_signals/max(total_signals,1)*100:.0f}% of signals)")
print(f"  Signals 'GOOD' (≥30%   : {worthwhile_signals}  ({worthwhile_signals/max(total_signals,1)*100:.0f}% of signals)")
print(f"  Win rate (good/aligned): {worthwhile_signals/max(aligned_signals,1)*100:.0f}%")
print(f"\n  Top-5 swings total     : {total_top5_swings}")
print(f"  Swings captured        : {captured_top5}  ({captured_top5/max(total_top5_swings,1)*100:.0f}% of top-5)")
print(f"  Swings missed          : {total_top5_swings - captured_top5}  ({(total_top5_swings - captured_top5)/max(total_top5_swings,1)*100:.0f}% of top-5)")

# ─── Per-strategy breakdown ───────────────────────────────────────────────────
print(f"\n\n{'─'*80}")
print(f"  {'STRATEGY BREAKDOWN':^76}")
print(f"  {'─'*76}")
print(f"  {'Strategy':<32}  {'Fired':>6}  {'Aligned':>7}  {'Good':>6}  {'Align%':>7}  {'WinRate':>8}  {'Noise%':>7}  {'Rank Dist'}")
print(f"  {'─'*76}")
for strat, sc in sorted(strat_stats.items(), key=lambda x: -x[1]["fired"]):
    fired   = sc["fired"]
    aligned = sc["aligned"]
    worthy  = sc["worthy"]
    noise   = fired - aligned
    align_p = aligned / max(fired, 1) * 100
    wr_p    = worthy  / max(aligned, 1) * 100
    noise_p = noise   / max(fired, 1) * 100
    rank_counts = sc.get("rank_counts", {})
    rank_str = " ".join(f"#{r}:{rank_counts.get(r,0)}" for r in range(1, 6))
    print(
        f"  {strat:<32}  {fired:>6}  {aligned:>7}  {worthy:>6}"
        f"  {align_p:>6.0f}%  {wr_p:>7.0f}%  {noise_p:>6.0f}%  [{rank_str}]"
    )
print(f"  {'─'*76}")

print(f"\n  Note: run with --output FILE to capture the full per-day detail.")
print()

# ─── Missed-swing diagnostics (aggregate) ────────────────────────────────────
if missed_details:
    from collections import Counter
    print(f"\n\n{'─'*80}")
    print(f"  {'MISSED SWING DIAGNOSTICS (aggregate over all missed top-5)':^76}")
    print(f"  {'─'*76}")

    up_missed   = [m for m in missed_details if m["dir"] == "UP"]
    dn_missed   = [m for m in missed_details if m["dir"] == "DN"]

    def _avg(lst, key):
        vals = [x[key] for x in lst if x[key] is not None]
        return sum(vals) / len(vals) if vals else float("nan")

    def _pct(lst, cond_fn):
        if not lst: return 0.0
        return sum(1 for x in lst if cond_fn(x)) / len(lst) * 100

    for label, grp in [("UP (missed CALL opps)", up_missed), ("DN (missed PUT opps)", dn_missed)]:
        if not grp: continue
        print(f"\n  {label}  ({len(grp)} swings)")
        regime_cnt = Counter(x["regime"] for x in grp)
        vwap_cnt   = Counter(x["vwap_rel"] for x in grp)
        print(f"    Avg RSI       : {_avg(grp,'rsi'):.1f}")
        print(f"    Avg MACDf     : {_avg(grp,'macd_f'):.4f}")
        print(f"    Avg BandPos   : {_avg(grp,'band_pos'):+.2f}")
        print(f"    VWAP below/above : {vwap_cnt.get('below',0)} / {vwap_cnt.get('above',0)}")
        print(f"    Regime breakdown : {dict(regime_cnt)}")
        print(f"    RSI < 45      : {_pct(grp, lambda x: x['rsi'] < 45):.0f}%")
        print(f"    RSI > 60      : {_pct(grp, lambda x: x['rsi'] > 60):.0f}%")
        print(f"    RSI 45-60     : {_pct(grp, lambda x: 45 <= x['rsi'] <= 60):.0f}%")
        print(f"    BandPos < -0.3: {_pct(grp, lambda x: x['band_pos'] < -0.3):.0f}%")
        print(f"    BandPos > +0.3: {_pct(grp, lambda x: x['band_pos'] >  0.3):.0f}%")
        print(f"    MACDf > 0     : {_pct(grp, lambda x: x['macd_f'] > 0):.0f}%")
    print()

# ─── Day-by-day table ─────────────────────────────────────────────────────────
print(f"\n{'─'*80}")
print(f"  {'Date':<12}  {'Signals':>7}  {'Aligned':>7}  {'Good':>7}  {'Top5':>5}  {'Captured':>8}  {'WinRate':>7}")
print(f"  {'─'*72}")
for d in day_stats:
    wr = d["worthy"] / max(d["signals"], 1) * 100
    print(
        f"  {str(d['date']):<12}  {d['signals']:>7}  {d['aligned']:>7}  "
        f"{d['worthy']:>7}  {d['top5']:>5}  {d['captured']:>8}  {wr:>6.0f}%"
    )

print(f"\n{'═'*80}")
print("Analysis complete.")
print(f"{'═'*80}\n")

if _capture:
    sys.stdout = sys.__stdout__
    out_text = _capture.getvalue()
    Path(args.output).write_text(out_text, encoding="utf-8")
    print(f"\nReport saved to: {args.output}")
