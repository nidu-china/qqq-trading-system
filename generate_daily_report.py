#!/usr/bin/env python3
"""Generate a daily QQQ0DTE trading report from local record/log files.

This script is intentionally read-only: it only reads records/, logs/ and CSV files,
then prints a report to stdout for cron/message delivery.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

CN_TZ = timezone(timedelta(hours=8))
ROOT = Path(__file__).resolve().parent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate QQQ0DTE daily trading report")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--date", help="report date, format YYYY-MM-DD")
    group.add_argument("--yesterday", action="store_true", help="use yesterday in Asia/Shanghai timezone")
    return parser.parse_args()


def target_date(args: argparse.Namespace) -> str:
    if args.date:
        datetime.strptime(args.date, "%Y-%m-%d")
        return args.date
    now = datetime.now(CN_TZ)
    day = now.date() - timedelta(days=1 if args.yesterday else 0)
    return day.isoformat()


def load_json(path: Path) -> Any | None:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def short_symbol(symbol: str) -> str:
    """QQQ260526P728000.US -> P728"""
    m = re.search(r"([CP])(\d{6})(?:\.US)?$", str(symbol))
    if not m:
        return str(symbol)
    strike = int(m.group(2)) / 1000
    strike_text = str(int(strike)) if strike.is_integer() else f"{strike:g}"
    return f"{m.group(1)}{strike_text}"


def fmt_num(value: Any, digits: int = 4) -> str:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return str(value)
    s = f"{n:.{digits}f}".rstrip("0").rstrip(".")
    return s if s else "0"


def normalize_trade(raw: dict[str, Any]) -> dict[str, Any]:
    pnl = raw.get("pnl_usd", raw.get("pnl_dollar", raw.get("pnl", 0)))
    pnl_pct = raw.get("pnl_pct", 0)
    try:
        pnl = float(pnl or 0)
    except (TypeError, ValueError):
        pnl = 0.0
    try:
        pnl_pct = float(pnl_pct or 0)
    except (TypeError, ValueError):
        pnl_pct = 0.0

    result = raw.get("result")
    if not result:
        if raw.get("win") is True or pnl > 0:
            result = "win"
        elif raw.get("win") is False or pnl < 0:
            result = "lose"
        else:
            result = "flat"

    contracts = raw.get("contracts")
    if contracts is None:
        qty = raw.get("qty", raw.get("quantity"))
        try:
            qty_f = float(qty or 0)
            contracts = int(qty_f / 100) if qty_f >= 100 and qty_f % 100 == 0 else int(qty_f)
        except (TypeError, ValueError):
            contracts = 0

    opt_symbol = raw.get("opt_symbol", raw.get("symbol", "-"))
    entry = raw.get("entry_opt_price")
    if entry is None:
        entry = raw.get("entry_price", "-") if raw.get("opt_symbol") is None else "N/A"

    return {
        "time": raw.get("time", "--:--"),
        "dir": raw.get("dir", raw.get("direction", "-")),
        "symbol": opt_symbol,
        "short_symbol": short_symbol(str(opt_symbol)),
        "contracts": int(contracts or 0),
        "entry": entry,
        "exit": raw.get("exit_price", "-"),
        "pnl": pnl,
        "pnl_pct": pnl_pct,
        "result": result,
        "reason": raw.get("exit_reason", raw.get("reason", "-")),
        "source": raw.get("_source", raw.get("price_source", "-")),
    }


def load_trades(day: str) -> tuple[list[dict[str, Any]], str | None]:
    record_path = ROOT / "records" / f"{day}.json"
    data = load_json(record_path)
    if data is None:
        return [], f"未找到交易记录文件：{record_path}"
    if isinstance(data, list):
        raw_trades = data
    elif isinstance(data, dict):
        raw_trades = data.get("trades", [])
    else:
        raw_trades = []
    return [normalize_trade(t) for t in raw_trades if isinstance(t, dict)], None


def summarize_orders(day: str) -> Counter[str]:
    path = ROOT / "logs" / f"orders_{day}.log"
    status_counter: Counter[str] = Counter()
    if not path.exists():
        return status_counter
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in lines:
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 6:
            status_counter[parts[5]] += 1
    return status_counter


def market_stats(day: str) -> dict[str, float] | None:
    csv_path = ROOT / "today.csv"
    if not csv_path.exists():
        return None
    rows: list[dict[str, str]] = []
    with csv_path.open("r", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ts = row.get("timestamp", "") or row.get("Datetime", "")
            if ts.startswith(day):
                rows.append(row)
    if not rows:
        return None
    try:
        first = float(rows[0].get("open") or rows[0].get("Open"))
        last = float(rows[-1].get("close") or rows[-1].get("Close"))
        high = max(float(r.get("high") or r.get("High")) for r in rows)
        low = min(float(r.get("low") or r.get("Low")) for r in rows)
        volume = sum(float(r.get("volume") or r.get("Volume") or 0) for r in rows)
    except (KeyError, TypeError, ValueError):
        return {"bars": float(len(rows))}
    pct = (last - first) / first * 100 if first else 0.0
    return {"open": first, "close": last, "high": high, "low": low, "pct": pct, "volume": volume, "bars": float(len(rows))}


def fmt_money(value: float) -> str:
    sign = "+" if value > 0 else ""
    return f"{sign}{value:,.2f}"


def fmt_pct(value: float) -> str:
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.2f}%"


def build_review(trades: list[dict[str, Any]], by_dir: dict[str, float], pnl_total: float, best: dict[str, Any], worst: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    if pnl_total < 0:
        lines.append(f"- 今日亏损 {fmt_money(pnl_total)} USD，核心问题不是单笔爆亏，而是亏损笔数偏多。")
    elif pnl_total > 0:
        lines.append(f"- 今日盈利 {fmt_money(pnl_total)} USD，主要由 {best['short_symbol']} 贡献。")
    else:
        lines.append("- 今日基本打平，交易收益没有覆盖波动成本。")

    losing = [t for t in trades if t["pnl"] < 0]
    winning = [t for t in trades if t["pnl"] > 0]
    if winning and losing:
        loss_sum = sum(t["pnl"] for t in losing)
        win_sum = sum(t["pnl"] for t in winning)
        lines.append(f"- 盈利合计 {fmt_money(win_sum)}，亏损合计 {fmt_money(loss_sum)}，盈利单被多笔亏损覆盖。")

    if by_dir:
        worst_dir = min(by_dir.items(), key=lambda x: x[1])
        lines.append(f"- 主要拖累方向：{worst_dir[0].upper()}，{fmt_money(worst_dir[1])} USD。")

    lines.append(f"- 最大盈利：{best['short_symbol']} {fmt_money(best['pnl'])}；最大亏损：{worst['short_symbol']} {fmt_money(worst['pnl'])}。")
    return lines


def build_report(day: str) -> str:
    trades, warning = load_trades(day)
    order_status = summarize_orders(day)
    generated_at = datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M GMT+8")

    lines: list[str] = []
    lines.append(f"📊 QQQ0DTE 每日交易总结｜{day}")
    lines.append(f"生成时间：{generated_at}")

    if warning:
        lines.append("")
        lines.append(f"⚠️ {warning}")

    if not trades:
        lines.append("")
        lines.append("当日无已归档交易记录。")
        return "\n".join(lines).strip() + "\n"

    total = len(trades)
    wins = sum(1 for t in trades if t["pnl"] > 0 or t["result"] == "win")
    losses = sum(1 for t in trades if t["pnl"] < 0 or t["result"] == "lose")
    flats = total - wins - losses
    pnl_total = sum(t["pnl"] for t in trades)
    win_rate = wins / total * 100 if total else 0.0
    avg_pnl = pnl_total / total if total else 0.0
    best = max(trades, key=lambda t: t["pnl"])
    worst = min(trades, key=lambda t: t["pnl"])
    result_icon = "🟢" if pnl_total > 0 else "🔴" if pnl_total < 0 else "⚪"

    lines.append("")
    lines.append(f"{result_icon} 今日结果：{fmt_money(pnl_total)} USD")
    lines.append(f"交易 {total} 笔｜胜 {wins} 负 {losses} 平 {flats}｜胜率 {win_rate:.1f}%｜单笔均值 {fmt_money(avg_pnl)}")

    stats = market_stats(day)
    if stats:
        lines.append("")
        lines.append("一、市场概况")
        if "open" in stats:
            lines.append(f"QQQ：{stats['open']:.2f} → {stats['close']:.2f}（{fmt_pct(stats['pct'])}）")
            lines.append(f"日内区间：{stats['low']:.2f} ~ {stats['high']:.2f}｜成交量 {stats['volume']:,.0f}")
        else:
            lines.append(f"行情K线：{int(stats['bars'])} 根")

    by_dir: defaultdict[str, float] = defaultdict(float)
    by_dir_count: Counter[str] = Counter()
    for t in trades:
        direction = str(t["dir"])
        by_dir[direction] += t["pnl"]
        by_dir_count[direction] += 1

    lines.append("")
    lines.append("二、交易表现")
    for direction in sorted(by_dir):
        lines.append(f"- {direction.upper()}：{by_dir_count[direction]} 笔，{fmt_money(by_dir[direction])} USD")
    lines.append(f"- 最大盈利：{best['short_symbol']}，{fmt_money(best['pnl'])} USD（{fmt_pct(best['pnl_pct'])}）")
    lines.append(f"- 最大亏损：{worst['short_symbol']}，{fmt_money(worst['pnl'])} USD（{fmt_pct(worst['pnl_pct'])}）")

    lines.append("")
    lines.append("三、交易明细")
    for idx, t in enumerate(sorted(trades, key=lambda x: (str(x["time"]), str(x["symbol"]))), 1):
        pnl_icon = "✅" if t["pnl"] > 0 else "❌" if t["pnl"] < 0 else "➖"
        lines.append(
            f"{idx}. {pnl_icon} {t['short_symbol']}｜{t['contracts']}张｜"
            f"{fmt_num(t['entry'])} → {fmt_num(t['exit'])}｜"
            f"{fmt_money(t['pnl'])}（{fmt_pct(t['pnl_pct'])}）"
        )

    lines.append("")
    lines.append("四、复盘结论")
    lines.extend(build_review(trades, dict(by_dir), pnl_total, best, worst))

    if order_status:
        lines.append("")
        lines.append("五、订单状态")
        status_map = {"submitted": "提交", "filled": "成交", "cancel_failed": "撤单失败", "canceled": "已撤", "rejected": "拒单"}
        parts = [f"{status_map.get(k.lower(), k)} {v}" for k, v in order_status.most_common()]
        lines.append("- " + "｜".join(parts))

    lines.append("")
    lines.append("数据源：records/YYYY-MM-DD.json（broker 成交对账口径）")
    return "\n".join(lines).strip() + "\n"


def main() -> int:
    args = parse_args()
    day = target_date(args)
    print(build_report(day), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
