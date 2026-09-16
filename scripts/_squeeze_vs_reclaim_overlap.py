"""At each Jul-Sep squeeze_mid_break entry bar, check if vwap_reclaim_call would fire."""
from __future__ import annotations

import re
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from qqq_trader.config import Settings
from qqq_trader.persistence import ParquetMarketStore
from qqq_trader.policy import init_rules
from qqq_trader.strategy import StrategyEngine, squeeze_mid_break_state

ET = ZoneInfo("America/New_York")
REPORT = Path("reports/backtest_hybrid_jul_aug_sep.txt")
ROW = re.compile(
    r"^\s*\d+\s+(\d{2}/\d{2}\s+\d{2}:\d{2}).*squeeze_mid_break"
)


def load_qqq():
    root = Path("data/market/bars")
    all_1m = ParquetMarketStore.read_bars_path(root, "1m")
    return [b for b in all_1m if b.symbol == "QQQ.US"]


def day_bars(qqq, d: date):
    return sorted(
        [
            b
            for b in qqq
            if b.complete
            and b.start.astimezone(ET).date() == d
            and time(9, 0) <= b.start.astimezone(ET).time().replace(tzinfo=None) < time(16, 0)
        ],
        key=lambda b: b.end,
    )


def parse_squeeze_entries() -> list[datetime]:
    out = []
    for line in REPORT.read_text(encoding="utf-8").splitlines():
        m = ROW.match(line)
        if m:
            out.append(datetime.strptime(f"2026/{m.group(1)}", "%Y/%m/%d %H:%M").replace(tzinfo=ET))
    return out


def reclaim_would_fire(engine: StrategyEngine, bars, end_at: datetime) -> tuple[bool, str]:
    """Mirror _vwap_reclaim_call gates using engine after evaluate context built."""
    available = [b for b in bars if b.end <= end_at]
    sig = engine.evaluate(available)
    if sig and sig.strategy == "squeeze_mid_break":
        # rebuild context at bar without taking squeeze: check reclaim manually
        pass
    ctx = engine.last_context
    if ctx is None:
        return False, "no_context"
    vwap = ctx.vwap_value
    if vwap <= 0:
        return False, "no_vwap"
    today = [b for b in engine._today_bars if b.end <= end_at]
    if len(today) < 3:
        return False, "short_day"
    if engine.last_state.name == "TREND_UP":
        return False, "trend_up_blocked"
    prev = today[-2]
    curr = today[-1]
    if curr.end != end_at:
        return False, "bar_mismatch"
    cross = prev.close < vwap <= curr.close
    if not cross:
        return False, "no_vwap_cross"
    # consecutive below vwap before current (same as strategy)
    count = 0
    for b in reversed(today[:-1]):
        if b.close < vwap:
            count += 1
        else:
            break
    if count < 2:
        return False, f"only_{count}_closes_below_vwap"
    half = max(ctx.boll_upper - ctx.boll_middle, Decimal("0.0001"))
    band_pos = (ctx.current_close - ctx.boll_middle) / half
    if not (curr.close > curr.open):
        return False, "not_bullish"
    if ctx.rsi_val > Decimal("45"):
        return False, f"rsi={ctx.rsi_val}"
    if band_pos > Decimal("0.20"):
        return False, f"band_pos={band_pos:.2f}"
    from qqq_trader.policy import RULES

    if ctx.rvol_val < RULES.regime_range_min_volume_ratio:
        return False, "volume"
    return True, "ok"


def main():
    init_rules(Settings(trading_mode="replay"))
    qqq = load_qqq()
    entries = parse_squeeze_entries()
    engine = StrategyEngine(Settings(trading_mode="replay"))
    hits = 0
    print(f"Squeeze entries in report: {len(entries)}\n")
    for end_at in entries:
        d = end_at.date()
        bars = day_bars(qqq, d)
        # advance engine to end_at
        for b in bars:
            if b.end > end_at:
                break
            engine.evaluate([x for x in bars if x.end <= b.end])
        ok, reason = reclaim_would_fire(engine, bars, end_at)
        if ok:
            hits += 1
        # also note squeeze fire vs armed
        _, fire, _ = squeeze_mid_break_state(engine._indicator_closes)
        print(f"  {end_at.strftime('%m/%d %H:%M')}  reclaim={ok} ({reason})  squeeze_fire={fire}")
    print(f"\nReclaim would match on {hits}/{len(entries)} squeeze entry bars ({hits/len(entries)*100:.0f}%)")


if __name__ == "__main__":
    main()
