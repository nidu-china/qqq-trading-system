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
            # Some records store shares-like quantity=contracts*100, some store contracts directly.
            contracts = int(qty_f / 100) if qty_f >= 100 and qty_f % 100 == 0 else int(qty_f)
        except (TypeError, ValueError):
            contracts = 0

    opt_symbol = raw.get("opt_symbol", raw.get("symbol", "-"))
    entry = raw.get("entry_opt_price")
    # If an option symbol exists but entry_opt_price is missing, entry_price is usually
    # the underlying QQQ price, not the option fill price. Do not show it as option entry.
    if entry is None:
        entry = raw.get("entry_price", "-") if raw.get("opt_symbol") is None else "N/A"

    return {
        "time": raw.get("time", "--:--"),
        "dir": raw.get("dir", raw.get("direction", "-")),
        "symbol": opt_symbol,
        "contracts": contracts,
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


def summarize_orders(day: str) -> tuple[Counter[str], list[str]]:
    path = ROOT / "logs" / f"orders_{day}.log"
    status_counter: Counter[str] = Counter()
    tail: list[str] = []
    if not path.exists():
        return status_counter, tail
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    tail = lines[-8:]
    for line in lines:
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 6:
            status_counter[parts[5]] += 1
    return status_counter, tail


def market_brief(day: str) -> str | None:
    # today.csv is overwritten by live trading; only use it when it appears to match target date.
    csv_path = ROOT / "today.csv"
    if not csv_path.exists():
        return None
    rows: list[dict[str, str]] = []
    with csv_path.open("r", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ts = row.get("timestamp", "")
            if ts.startswith(day):
                rows.append(row)
    if not rows:
        return None
    try:
        first = float(rows[0]["open"])
        last = float(rows[-1]["close"])
        high = max(float(r["high"]) for r in rows)
        low = min(float(r["low"]) for r in rows)
        volume = sum(float(r.get("volume") or 0) for r in rows)
    except (KeyError, TypeError, ValueError):
        return f"行情K线：{len(rows)} 根"
    pct = (last - first) / first * 100 if first else 0.0
    return f"QQQ 日内：开 {first:.2f} / 收 {last:.2f} / 高 {high:.2f} / 低 {low:.2f} / 涨跌 {pct:+.2f}% / 量 {volume:,.0f}"


def fmt_money(value: float) -> str:
    sign = "+" if value > 0 else ""
    return f"{sign}{value:,.2f}"


def fmt_pct(value: float) -> str:
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.2f}%"


def build_report(day: str) -> str:
    trades, warning = load_trades(day)
    order_status, order_tail = summarize_orders(day)
    generated_at = datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S GMT+8")

    lines: list[str] = []
    lines.append(f"QQQ0DTE 每日交易总结｜{day}")
    lines.append(f"生成时间：{generated_at}")

    brief = market_brief(day)
    if brief:
        lines.append(brief)

    if warning:
        lines.append("")
        lines.append(f"⚠️ {warning}")

    if not trades:
        lines.append("")
        lines.append("当日无已归档交易记录。")
    else:
        total = len(trades)
        wins = sum(1 for t in trades if t["pnl"] > 0 or t["result"] == "win")
        losses = sum(1 for t in trades if t["pnl"] < 0 or t["result"] == "lose")
        flats = total - wins - losses
        pnl_total = sum(t["pnl"] for t in trades)
        win_rate = wins / total * 100 if total else 0.0
        avg_pnl = pnl_total / total if total else 0.0
        best = max(trades, key=lambda t: t["pnl"])
        worst = min(trades, key=lambda t: t["pnl"])

        lines.append("")
        lines.append("核心结果")
        lines.append(f"- 交易数：{total} 笔")
        lines.append(f"- 胜 / 负 / 平：{wins} / {losses} / {flats}")
        lines.append(f"- 胜率：{win_rate:.1f}%")
        lines.append(f"- 总盈亏：{fmt_money(pnl_total)} USD")
        lines.append(f"- 单笔平均：{fmt_money(avg_pnl)} USD")
        lines.append(f"- 最好单笔：{best['dir']} {best['symbol']}，{fmt_money(best['pnl'])} USD（{fmt_pct(best['pnl_pct'])}）")
        lines.append(f"- 最差单笔：{worst['dir']} {worst['symbol']}，{fmt_money(worst['pnl'])} USD（{fmt_pct(worst['pnl_pct'])}）")

        by_dir: defaultdict[str, float] = defaultdict(float)
        by_reason: defaultdict[str, float] = defaultdict(float)
        reason_count: Counter[str] = Counter()
        for t in trades:
            by_dir[str(t["dir"])] += t["pnl"]
            reason = str(t["reason"])
            reason_count[reason] += 1
            by_reason[reason] += t["pnl"]

        lines.append("")
        lines.append("方向统计")
        for direction, pnl in sorted(by_dir.items()):
            count = sum(1 for t in trades if str(t["dir"]) == direction)
            lines.append(f"- {direction}：{count} 笔，{fmt_money(pnl)} USD")

        lines.append("")
        lines.append("平仓/归因统计")
        for reason, count in reason_count.most_common():
            lines.append(f"- {reason}：{count} 笔，{fmt_money(by_reason[reason])} USD")

        lines.append("")
        lines.append("交易明细")
        for idx, t in enumerate(sorted(trades, key=lambda x: str(x["time"])), 1):
            lines.append(
                f"{idx}. {t['time']} {t['dir']} {t['symbol']} "
                f"{t['contracts']}张｜入 {t['entry']} / 出 {t['exit']}｜"
                f"{fmt_money(t['pnl'])} USD（{fmt_pct(t['pnl_pct'])}）｜{t['reason']}"
            )

    if order_status:
        status_text = "，".join(f"{k}:{v}" for k, v in order_status.most_common())
        lines.append("")
        lines.append(f"订单日志状态计数：{status_text}")
    elif order_tail:
        lines.append("")
        lines.append("订单日志存在，但未解析到状态计数。")

    if order_tail:
        lines.append("")
        lines.append("订单日志最后记录")
        for line in order_tail:
            # Keep order IDs but do not expose account-level secrets; these are operational identifiers.
            lines.append(f"- {line}")

    return "\n".join(lines).strip() + "\n"


def main() -> int:
    args = parse_args()
    day = target_date(args)
    print(build_report(day), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
