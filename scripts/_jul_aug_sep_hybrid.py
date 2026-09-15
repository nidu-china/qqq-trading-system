"""Jul–Sep Hybrid replay with current rules. Writes a report for comparison."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from qqq_trader.backtest import EventDrivenBacktester
from qqq_trader.config import Settings
from qqq_trader.persistence import ParquetMarketStore
from qqq_trader.risk import ContractSelector, RiskEngine

ET = ZoneInfo("America/New_York")
OUT = Path("reports/backtest_hybrid_jul_aug_sep.txt")


def _load():
    root = Path("data/market/bars")
    all_1m = ParquetMarketStore.read_bars_path(root, "1m")
    qqq = [b for b in all_1m if b.symbol == "QQQ.US"]
    vix = [b for b in all_1m if b.symbol == ".VIX.US"]
    return qqq, vix


def _filter(bars, start: date, end: date, open_t: time, close_t: time):
    return [
        b
        for b in bars
        if start <= b.start.astimezone(ET).date() <= end
        and open_t <= b.start.astimezone(ET).time().replace(tzinfo=None) < close_t
    ]


def _run(qqq, vix, start: date, end: date):
    bars = _filter(qqq, start, end, time(9, 0), time(16, 0))
    vol = _filter(vix, start, end, time(4, 0), time(16, 0))
    settings = Settings(trading_mode="replay", strategy_mode="hybrid")
    tester = EventDrivenBacktester(settings, None, ContractSelector(), RiskEngine(settings))
    result = tester.run(bars, {}, Decimal("100000"), vol, [], trade_start=start)
    sessions = sorted(
        {b.start.astimezone(ET).date() for b in bars if b.start.astimezone(ET).time().replace(tzinfo=None) >= time(9, 30)}
    )
    return result, sessions, bars, vol


def _wr(trades):
    if not trades:
        return "0/0 (—)"
    wins = sum(1 for t in trades if t.pnl > 0)
    return f"{wins}/{len(trades)} ({wins / len(trades) * 100:.1f}%)"


def _pnl(trades):
    return sum((t.pnl for t in trades), Decimal(0))


def _month_block(label, start, end, result):
    trades = [
        t
        for t in result.trades
        if start <= t.entry_at.astimezone(ET).date() <= end
    ]
    lines = [
        f"================================================================================================",
        f"{label}",
        f"================================================================================================",
        "",
        f"--- hybrid ---",
        f"  PnL ${ _pnl(trades):+,.0f}   WR {_wr(trades)}   signals={result.signals}  rejected={dict(result.rejected)}",
        f"    #  entry ET      exit ET       dir   strategy                      qty      in     out  exit                     pnl",
    ]
    for i, t in enumerate(trades, 1):
        entry = t.entry_at.astimezone(ET).strftime("%m/%d %H:%M")
        exit_ = t.exit_at.astimezone(ET).strftime("%m/%d %H:%M")
        strat = (t.strategy or "")[:28]
        lines.append(
            f"   {i:2d}  {entry}   {exit_}   {t.direction.value.upper():4s}  {strat:<28}  "
            f"{t.quantity:3d}   {t.entry_price:5.2f}   {t.exit_price:5.2f}  {t.exit_reason:<20}  "
            f"{t.pnl:+6.0f}"
        )
    by_setup: dict[str, list] = defaultdict(list)
    for t in trades:
        by_setup[t.strategy or "?"].append(t)
    lines.append("")
    lines.append("  --- by setup ---")
    for name, group in sorted(by_setup.items(), key=lambda kv: -abs(_pnl(kv[1]))):
        lines.append(
            f"    {name:<28} n={len(group):2d}  wr={_wr(group):<16} pnl=${_pnl(group):+,.0f}"
        )
    lines.append("")
    return lines, trades


def main():
    print("Loading bars...")
    qqq, vix = _load()
    months = [
        ("2026-07", date(2026, 7, 1), date(2026, 7, 31)),
        ("2026-08", date(2026, 8, 1), date(2026, 8, 31)),
        ("2026-09", date(2026, 9, 1), date(2026, 9, 14)),
    ]
    out_lines = [
        "QQQ 0DTE Hybrid  |  start equity $100,000  |  synthetic 0DTE quotes",
        "Periods: Jul 1-31, Aug 1-31, Sep 1-14 2026 (Labor Day 09-07 closed)",
        "Current rules: session indicators 09:00 only (no prior day), OR 09:30-09:35, "
        "squeeze hold 20 bars + coil min width <= 0.29%, VIX 1m MACD gate "
        f"(rising_block={Settings(trading_mode='replay').volatility_vix_macd_rising_block})",
        f"Sizing: MAX_PREMIUM_FRACTION={Settings(trading_mode='replay').max_premium_fraction}  MAX_CONTRACTS={Settings(trading_mode='replay').max_contracts}",
        "",
    ]
    all_trades = []
    summary_rows = []
    for name, start, end in months:
        print(f"Running {name}...")
        result, sessions, bars, vol = _run(qqq, vix, start, end)
        label = f"{name}  ({len(sessions)} sessions, {start.isoformat()} → {end.isoformat()})"
        block, trades = _month_block(label, start, end, result)
        out_lines.extend(block)
        all_trades.extend(trades)
        summary_rows.append(
            (
                name,
                len(trades),
                _wr(trades),
                _pnl(trades),
                result.signals,
                dict(result.rejected),
                dict(result.volatility_regimes),
            )
        )
        print(
            f"  {name}: n={len(trades)} wr={_wr(trades)} pnl=${_pnl(trades):+,.0f} "
            f"signals={result.signals} rejected={dict(result.rejected)}"
        )

    out_lines.append("SUMMARY")
    out_lines.append("================================================================================================")
    out_lines.append("  period    strategy       n            WR         PnL")
    for name, n, wr, pnl, *_ in summary_rows:
        out_lines.append(f"  {name}   hybrid       {n:3d}   {wr:<16}  $ {pnl:>+8,.0f}")
    total_n = len(all_trades)
    out_lines.append(
        f"  TOTAL     hybrid       {total_n:3d}   {_wr(all_trades):<16}  $ {_pnl(all_trades):>+8,.0f}"
    )
    out_lines.append("")
    by_setup: dict[str, list] = defaultdict(list)
    for t in all_trades:
        by_setup[t.strategy or "?"].append(t)
    out_lines.append("ALL MONTHS by setup")
    for name, group in sorted(by_setup.items(), key=lambda kv: -abs(_pnl(kv[1]))):
        out_lines.append(
            f"  {name:<28} n={len(group):3d}  wr={_wr(group):<16} pnl=${_pnl(group):+,.0f}"
        )
    squeeze = by_setup.get("squeeze_mid_break", [])
    out_lines.append("")
    out_lines.append(f"squeeze_mid_break trades: {len(squeeze)}")
    for t in squeeze:
        out_lines.append(
            f"  {t.entry_at.astimezone(ET).strftime('%m/%d %H:%M')}  "
            f"{t.direction.value}  qty={t.quantity}  in={t.entry_price}  out={t.exit_price}  "
            f"{t.exit_reason}  pnl=${t.pnl:+,.0f}"
        )
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()
