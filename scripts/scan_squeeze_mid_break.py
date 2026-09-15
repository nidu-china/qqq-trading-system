"""Scan Bollinger squeeze + middle-band breakout Call setups on 1-minute QQQ."""

from __future__ import annotations

import json
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

from qqq_trader.market_hours import is_vix_session_bar
from qqq_trader.persistence import ParquetMarketStore

ET = ZoneInfo("America/New_York")
ROOT = Path("data/market/bars")
ENTRY_START = time(9, 40)
ENTRY_END = time(13, 30)
FLAT = time(13, 30)
BB_PERIOD = 20
MAX_HOLD = 20
COOLDOWN = 3
MIN_TRADES = 12


def load_days(symbol: str) -> dict[str, list]:
    base = ROOT / f"symbol={symbol}"
    days = {}
    if not base.exists():
        return days
    for folder in sorted(base.glob("date=*")):
        path = folder / "1m.parquet"
        if not path.exists():
            continue
        bars = [bar for bar in ParquetMarketStore.read_bars(path) if bar.complete]
        if bars:
            days[folder.name.removeprefix("date=")] = sorted(bars, key=lambda b: b.end)
    return days


def ema(values: np.ndarray, period: int) -> np.ndarray:
    out = np.full(len(values), np.nan)
    if len(values) < period:
        return out
    k = 2.0 / (period + 1)
    out[period - 1] = values[:period].mean()
    for i in range(period, len(values)):
        out[i] = (values[i] - out[i - 1]) * k + out[i - 1]
    return out


def macd_hist(values: np.ndarray, fast: int, slow: int, signal: int) -> np.ndarray:
    out = np.full(len(values), np.nan)
    if len(values) < slow + signal - 1:
        return out
    macd = ema(values, fast) - ema(values, slow)
    start = slow - 1
    line = macd[start:]
    if np.isnan(line).any():
        return out
    sig = ema(line, signal)
    out[start + signal - 1 :] = (line - sig)[signal - 1 :]
    return out


def bollinger(close: np.ndarray, period: int = 20, k: float = 2.0):
    n = len(close)
    upper = np.full(n, np.nan)
    mid = np.full(n, np.nan)
    lower = np.full(n, np.nan)
    for i in range(period - 1, n):
        window = close[i - period + 1 : i + 1]
        m = float(window.mean())
        sd = float(window.std(ddof=0))
        mid[i] = m
        upper[i] = m + k * sd
        lower[i] = m - k * sd
    return upper, mid, lower


def rolling_low_rank(width: np.ndarray, lookback: int) -> np.ndarray:
    n = len(width)
    rank = np.full(n, np.nan)
    for i in range(lookback - 1, n):
        window = width[i - lookback + 1 : i + 1]
        if np.isnan(window).any():
            continue
        rank[i] = float(np.mean(window <= width[i]))
    return rank


def local_time(bar) -> time:
    return bar.end.astimezone(ET).time().replace(tzinfo=None)


def prepare(qqq_days, vix_days, lookbacks=(40, 60, 90)):
    prepared = []
    for day, bars in qqq_days.items():
        indicator = [
            b
            for b in bars
            if time(9, 0)
            <= b.start.astimezone(ET).time().replace(tzinfo=None)
            < time(16, 0)
        ]
        if len(indicator) < BB_PERIOD + max(lookbacks):
            continue
        close = np.array([float(b.close) for b in indicator])
        volume = np.array([float(b.volume) for b in indicator], dtype=float)
        upper, mid, lower = bollinger(close)
        width = (upper - lower) / np.where(mid == 0, np.nan, mid)
        ranks = {lb: rolling_low_rank(width, lb) for lb in lookbacks}
        hist = macd_hist(close, 5, 10, 3)
        vol_avg = np.full(len(close), np.nan)
        for i in range(20, len(close)):
            vol_avg[i] = float(np.mean(volume[i - 20 : i]))
        times = [local_time(b) for b in indicator]
        vix_ok = np.ones(len(indicator), dtype=bool)
        if day in vix_days:
            vix_bars = [b for b in vix_days[day] if is_vix_session_bar(b)]
            if len(vix_bars) >= 25:
                vix_h = macd_hist(np.array([float(b.close) for b in vix_bars]), 8, 17, 9)
                j = 0
                last = np.nan
                for i, bar in enumerate(indicator):
                    while j < len(vix_bars) and vix_bars[j].end <= bar.end:
                        if not np.isnan(vix_h[j]):
                            last = vix_h[j]
                        j += 1
                    vix_ok[i] = True if np.isnan(last) else last < 0
        prepared.append(
            {
                "day": day,
                "close": close,
                "upper": upper,
                "mid": mid,
                "width": width,
                "ranks": ranks,
                "hist": hist,
                "volume": volume,
                "vol_avg": vol_avg,
                "times": times,
                "vix_ok": vix_ok,
            }
        )
    return prepared


def simulate(days, params, naive=False):
    lookback = params["lookback"]
    pct = params["pct"]
    min_bars = params["min_bars"]
    expand = params["expand"]
    max_bp = params["max_bp"]
    use_macd = params["macd"]
    use_vix = params["vix"]
    use_vol = params["vol"]
    trades = []
    for day in days:
        close = day["close"]
        mid = day["mid"]
        upper = day["upper"]
        width = day["width"]
        rank = day["ranks"][lookback]
        squeeze = rank <= pct
        streak = np.zeros(len(close), dtype=int)
        for i in range(len(close)):
            if squeeze[i]:
                streak[i] = streak[i - 1] + 1 if i else 1
        in_pos = False
        entry_i = -1
        cooldown_until = -1
        need_reset = False
        for i in range(len(close)):
            t = day["times"][i]
            if in_pos:
                hold = i - entry_i
                if (
                    (not np.isnan(mid[i]) and close[i] < mid[i])
                    or hold >= MAX_HOLD
                    or t >= FLAT
                    or i == len(close) - 1
                ):
                    entry = close[entry_i]
                    exit_px = close[i]
                    path = close[entry_i : i + 1]
                    trades.append(
                        {
                            "day": day["day"],
                            "hold": hold,
                            "pnl": float(exit_px - entry),
                            "mfe": float(path.max() - entry),
                            "mae": float(path.min() - entry),
                            "win": bool(exit_px > entry),
                        }
                    )
                    in_pos = False
                    cooldown_until = i + COOLDOWN
                    need_reset = True
                continue
            if t < ENTRY_START or t >= ENTRY_END or i <= cooldown_until or i < 1:
                continue
            if np.isnan(mid[i]) or np.isnan(mid[i - 1]):
                continue
            cross = close[i - 1] <= mid[i - 1] and close[i] > mid[i]
            if naive:
                fire = cross
            else:
                if need_reset:
                    if not squeeze[i]:
                        need_reset = False
                    continue
                armed = streak[i - 1] >= min_bars
                if params.get("expand_from_coil"):
                    start = max(0, i - min_bars)
                    coil = width[start:i]
                    coil = coil[~np.isnan(coil)] if coil.size else coil
                    expanding = (
                        coil.size > 0
                        and not np.isnan(width[i])
                        and width[i] >= float(np.min(coil)) * expand
                    )
                else:
                    expanding = (
                        not np.isnan(width[i])
                        and not np.isnan(width[i - 1])
                        and width[i] >= width[i - 1] * expand
                    )
                half = max(upper[i] - mid[i], 1e-9)
                band_pos = (close[i] - mid[i]) / half
                fire = armed and expanding and cross and band_pos <= max_bp
                if use_macd:
                    fire = fire and (not np.isnan(day["hist"][i])) and day["hist"][i] >= 0
                if use_vol:
                    fire = (
                        fire
                        and day["vol_avg"][i] > 0
                        and day["volume"][i] >= day["vol_avg"][i]
                    )
                if use_vix:
                    fire = fire and bool(day["vix_ok"][i])
            if fire:
                in_pos = True
                entry_i = i
    return trades


def summarize(trades):
    n = len(trades)
    empty = {
        "n": 0,
        "wins": 0,
        "win_rate": None,
        "mean_pts": None,
        "median_pts": None,
        "pf": None,
        "mean_mfe": None,
        "mean_mae": None,
        "mean_hold": None,
        "days": 0,
    }
    if n == 0:
        return empty
    pnls = np.array([t["pnl"] for t in trades])
    wins = pnls[pnls > 0]
    losses = pnls[pnls <= 0]
    gp = float(wins.sum()) if len(wins) else 0.0
    gl = float(-losses.sum()) if len(losses) else 0.0
    return {
        "n": n,
        "wins": int((pnls > 0).sum()),
        "win_rate": float((pnls > 0).mean()),
        "mean_pts": float(pnls.mean()),
        "median_pts": float(np.median(pnls)),
        "pf": (gp / gl) if gl > 0 else (None if gp == 0 else 99.0),
        "mean_mfe": float(np.mean([t["mfe"] for t in trades])),
        "mean_mae": float(np.mean([t["mae"] for t in trades])),
        "mean_hold": float(np.mean([t["hold"] for t in trades])),
        "days": len({t["day"] for t in trades}),
    }


def pack(label, params, trades):
    return {"label": label, "params": params, **summarize(trades)}


def main():
    qqq = load_days("QQQ.US")
    vix = load_days(".VIX.US")
    print(f"loaded QQQ days={len(qqq)} VIX days={len(vix)}")
    days = prepare(qqq, vix)
    print(f"prepared days={len(days)}")

    naive = pack(
        "naive_mid_cross",
        {"naive": True},
        simulate(
            days,
            {
                "lookback": 60,
                "pct": 0.2,
                "min_bars": 5,
                "expand": 1.00,
                "max_bp": 1.0,
                "macd": False,
                "vix": False,
                "vol": False,
            },
            naive=True,
        ),
    )
    core = []
    for lookback in (40, 60, 90):
        for pct in (0.15, 0.20, 0.25, 0.30, 0.40):
            for min_bars in (3, 5, 8):
                params = {
                    "lookback": lookback,
                    "pct": pct,
                    "min_bars": min_bars,
                    "expand": 1.00,
                    "max_bp": 1.00,
                    "macd": False,
                    "vix": False,
                    "vol": False,
                    "expand_from_coil": True,
                }
                core.append(
                    pack(
                        f"lb{lookback}_p{int(pct*100)}_n{min_bars}",
                        params,
                        simulate(days, params),
                    )
                )

    ranked = [r for r in core if r["n"] >= MIN_TRADES]
    ranked.sort(key=lambda r: ((r["win_rate"] or 0), (r["mean_pts"] or 0)), reverse=True)
    seed = ranked[0]["params"] if ranked else core[0]["params"]

    refinements = []
    for expand in (1.00, 1.05, 1.10, 1.20):
        for max_bp in (0.70, 1.00, 1.50):
            for macd in (False, True):
                for vix_on in (False, True):
                    for vol in (False, True):
                        params = {
                            **seed,
                            "expand": expand,
                            "max_bp": max_bp,
                            "macd": macd,
                            "vix": vix_on,
                            "vol": vol,
                            "expand_from_coil": True,
                        }
                        refinements.append(
                            pack(
                                f"x{expand:.2f}_bp{max_bp:.2f}"
                                f"{'_macd' if macd else ''}"
                                f"{'_vix' if vix_on else ''}"
                                f"{'_vol' if vol else ''}",
                                params,
                                simulate(days, params),
                            )
                        )

    payload = {
        "sample": {
            "qqq_days": len(qqq),
            "prepared_days": len(days),
            "vix_days": len(vix),
            "start": min(qqq) if qqq else None,
            "end": max(qqq) if qqq else None,
            "exit": "spot close < BOLL middle, or 20 minutes, or 13:30 ET",
            "entry_window": "09:40-13:30 ET",
            "bb": "BOLL(20,2) on 1-minute QQQ, including 09:00 if present",
            "win": "exit spot > entry spot",
            "min_trades": MIN_TRADES,
        },
        "naive": naive,
        "core": core,
        "refinements": refinements,
        "seed": seed,
    }
    out = Path("reports/squeeze_mid_break_scan.json")
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"wrote {out}")

    def dump(title, items, limit=10):
        ok = [r for r in items if r["n"] >= MIN_TRADES]
        ok.sort(key=lambda r: ((r["win_rate"] or 0), r["n"]), reverse=True)
        print(f"\n== {title} ==")
        for r in ok[:limit]:
            print(
                f"  {r['label']:32} n={r['n']:3} wr={r['win_rate']:.1%} "
                f"mean={r['mean_pts']:+.3f} mfe={r['mean_mfe']:+.3f} "
                f"mae={r['mean_mae']:+.3f} hold={r['mean_hold']:.1f} pf={r['pf']}"
            )

    print(
        f"\n== naive == n={naive['n']} wr={naive['win_rate']:.1%} "
        f"mean={naive['mean_pts']:+.3f} pf={naive['pf']}"
    )
    dump("core: squeeze lookback/pct/min_bars (x1.05 bp0.70)", core)
    dump("refinements on best core seed", refinements)


if __name__ == "__main__":
    main()
