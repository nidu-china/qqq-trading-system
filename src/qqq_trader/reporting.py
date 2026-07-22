from __future__ import annotations

import hashlib
import html
import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from .domain import Bar


@dataclass(slots=True)
class TradeSummary:
    symbol: str
    direction: str
    quantity: int
    entry_price: Decimal
    exit_price: Decimal
    pnl: Decimal
    fees: Decimal
    entry_at: str
    exit_at: str
    exit_reason: str
    slippage: Decimal = Decimal(0)
    mae: Decimal = Decimal(0)
    mfe: Decimal = Decimal(0)


@dataclass(slots=True)
class DailyReportData:
    trading_date: date
    opening_equity: Decimal
    closing_equity: Decimal
    trades: list[TradeSummary] = field(default_factory=list)
    rejected_signals: list[dict[str, Any]] = field(default_factory=list)
    system_events: list[dict[str, Any]] = field(default_factory=list)
    comparison_20d: dict[str, Any] = field(default_factory=dict)
    data_quality: dict[str, Any] = field(default_factory=dict)
    underlying_bars: list[Bar] = field(default_factory=list)
    option_backtest_complete: bool = True


class DailyReportGenerator:
    def __init__(self, root: Path) -> None:
        self.root = root

    def generate(self, data: DailyReportData) -> dict[str, Path]:
        directory = self.root / data.trading_date.isoformat()
        directory.mkdir(parents=True, exist_ok=True)
        metrics = self._metrics(data)
        suggestions = self._suggestions(data, metrics)
        payload = self._payload(data, metrics, suggestions)

        json_path = directory / "report.json"
        md_path = directory / "report.md"
        html_path = directory / "report.html"
        svg_path = directory / "qqq.svg"

        self._atomic_text(json_path, json.dumps(payload, ensure_ascii=False, indent=2))
        markdown = self._markdown(data, metrics, suggestions)
        self._atomic_text(md_path, markdown)
        self._atomic_text(svg_path, self._chart(data.underlying_bars, data.trades))
        self._atomic_text(html_path, self._html(data, metrics, suggestions, svg_path.name))
        return {"json": json_path, "markdown": md_path, "html": html_path, "chart": svg_path}

    @staticmethod
    def _metrics(data: DailyReportData) -> dict[str, Any]:
        gross = sum((trade.pnl + trade.fees for trade in data.trades), Decimal(0))
        fees = sum((trade.fees for trade in data.trades), Decimal(0))
        net = sum((trade.pnl for trade in data.trades), Decimal(0))
        wins = [trade for trade in data.trades if trade.pnl > 0]
        losses = [trade for trade in data.trades if trade.pnl < 0]
        profit = sum((trade.pnl for trade in wins), Decimal(0))
        loss = abs(sum((trade.pnl for trade in losses), Decimal(0)))
        equity = data.opening_equity
        peak = equity
        max_drawdown = Decimal(0)
        for trade in sorted(data.trades, key=lambda item: item.exit_at):
            equity += trade.pnl
            peak = max(peak, equity)
            max_drawdown = min(max_drawdown, equity - peak)
        return {
            "gross_pnl": gross,
            "fees": fees,
            "net_pnl": net,
            "trade_count": len(data.trades),
            "win_rate": (
                Decimal(len(wins)) / Decimal(len(data.trades)) if data.trades else Decimal(0)
            ),
            "profit_factor": profit / loss if loss else None,
            "equity_change": data.closing_equity - data.opening_equity,
            "max_mae": min((trade.mae for trade in data.trades), default=Decimal(0)),
            "max_intraday_drawdown": max_drawdown,
            "average_slippage": (
                sum((trade.slippage for trade in data.trades), Decimal(0))
                / Decimal(len(data.trades))
                if data.trades
                else Decimal(0)
            ),
        }

    @staticmethod
    def _suggestions(data: DailyReportData, metrics: dict[str, Any]) -> list[str]:
        result: list[str] = []
        if data.rejected_signals:
            result.append("复核被拒绝信号的主要原因，优先改善行情完整性和成交价差。")
        if metrics["trade_count"] >= 2 and metrics["win_rate"] < Decimal("0.4"):
            result.append("当日胜率偏低；在滚动样本验证前不要放宽突破或流动性门槛。")
        if any(trade.slippage > Decimal("0.05") for trade in data.trades):
            result.append("存在超过0.05美元的滑点，检查追价次数和入场时段流动性。")
        if not data.option_backtest_complete:
            result.append("期权历史报价不完整，本日报不能用于评估真实0DTE期权收益。")
        if not result:
            result.append("没有发现需要立即调整的规则；继续积累样本，禁止自动改动实盘参数。")
        return result

    @staticmethod
    def _payload(data: DailyReportData, metrics: dict[str, Any], suggestions: list[str]) -> dict:
        def serializable(value: Any) -> Any:
            if isinstance(value, Decimal):
                return str(value)
            if isinstance(value, (date, datetime)):
                return value.isoformat()
            if isinstance(value, Bar):
                return {key: serializable(item) for key, item in asdict(value).items()}
            if isinstance(value, dict):
                return {key: serializable(item) for key, item in value.items()}
            if isinstance(value, list):
                return [serializable(item) for item in value]
            return value

        return serializable(
            {
                "trading_date": data.trading_date,
                "generated_at": datetime.now(timezone.utc),
                "opening_equity": data.opening_equity,
                "closing_equity": data.closing_equity,
                "metrics": metrics,
                "trades": [asdict(trade) for trade in data.trades],
                "rejected_signals": data.rejected_signals,
                "system_events": data.system_events,
                "comparison_20d": data.comparison_20d,
                "data_quality": data.data_quality,
                "option_backtest_complete": data.option_backtest_complete,
                "suggestions": suggestions,
            }
        )

    @staticmethod
    def _markdown(data: DailyReportData, metrics: dict[str, Any], suggestions: list[str]) -> str:
        lines = [
            f"# QQQ 0DTE 交易日报 — {data.trading_date.isoformat()}",
            "",
            f"- 净盈亏：{metrics['net_pnl']} USD",
            f"- 手续费：{metrics['fees']} USD",
            f"- 交易次数：{metrics['trade_count']}",
            f"- 胜率：{metrics['win_rate']:.2%}",
            f"- 最大日内回撤：{metrics['max_intraday_drawdown']} USD",
            f"- 平均滑点：{metrics['average_slippage']} USD",
            f"- 期权回测数据完整：{'是' if data.option_backtest_complete else '否'}",
            "",
            "## 交易记录",
            "",
            "| 合约 | 方向 | 数量 | 入场 | 出场 | 净盈亏 | 原因 |",
            "|---|---:|---:|---:|---:|---:|---|",
        ]
        for trade in data.trades:
            lines.append(
                f"| {trade.symbol} | {trade.direction} | {trade.quantity} | "
                f"{trade.entry_price} | {trade.exit_price} | {trade.pnl} | {trade.exit_reason} |"
            )
        lines.extend(["", "## 改进建议", ""])
        lines.extend(f"- {item}" for item in suggestions)
        lines.extend(
            [
                "",
                "## 数据质量与20日对比",
                "",
                f"- 数据质量：`{json.dumps(data.data_quality, ensure_ascii=False)}`",
                f"- 20日对比：`{json.dumps(data.comparison_20d, ensure_ascii=False)}`",
                f"- 被拒绝信号：{len(data.rejected_signals)}",
                f"- 系统事件：{len(data.system_events)}",
            ]
        )
        return "\n".join(lines) + "\n"

    def _html(
        self, data: DailyReportData, metrics: dict[str, Any], suggestions: list[str], chart: str
    ) -> str:
        rows = "".join(
            "<tr>"
            f"<td>{html.escape(trade.symbol)}</td><td>{trade.direction}</td>"
            f"<td>{trade.quantity}</td><td>{trade.entry_price}</td>"
            f"<td>{trade.exit_price}</td><td>{trade.pnl}</td>"
            f"<td>{html.escape(trade.exit_reason)}</td></tr>"
            for trade in data.trades
        )
        tips = "".join(f"<li>{html.escape(item)}</li>" for item in suggestions)
        audit_json = html.escape(
            json.dumps(
                {
                    "data_quality": data.data_quality,
                    "comparison_20d": data.comparison_20d,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>QQQ 日报</title>
<style>body{{font-family:system-ui;max-width:1100px;margin:40px auto;color:#172033}}
table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #d8deea;padding:8px}}
.metrics{{display:flex;gap:24px;flex-wrap:wrap}}.card{{padding:14px;background:#f4f7fb;border-radius:8px}}</style>
</head><body><h1>QQQ 0DTE 交易日报 — {data.trading_date.isoformat()}</h1>
<div class="metrics"><div class="card">净盈亏<br><strong>{metrics["net_pnl"]} USD</strong></div>
<div class="card">胜率<br><strong>{metrics["win_rate"]:.2%}</strong></div>
<div class="card">交易次数<br><strong>{metrics["trade_count"]}</strong></div>
<div class="card">最大回撤<br><strong>{metrics["max_intraday_drawdown"]} USD</strong></div></div>
<h2>QQQ 走势</h2><img src="{chart}" alt="QQQ close chart" style="width:100%">
<h2>交易记录</h2><table><tr><th>合约</th><th>方向</th><th>数量</th><th>入场</th>
<th>出场</th><th>净盈亏</th><th>原因</th></tr>{rows}</table>
<h2>改进建议</h2><ul>{tips}</ul>
<h2>审计摘要</h2><p>被拒绝信号：{len(data.rejected_signals)}；系统事件：{len(data.system_events)}</p>
<pre>{audit_json}</pre>
</body></html>"""

    @staticmethod
    def _chart(bars: list[Bar], trades: list[TradeSummary]) -> str:
        width, height, padding = 1000, 280, 30
        if not bars:
            return (
                f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}"></svg>'
            )
        prices = [float(bar.close) for bar in bars]
        low, high = min(prices), max(prices)
        span = high - low or 1
        points = []
        for index, price in enumerate(prices):
            x = padding + index * (width - padding * 2) / max(1, len(prices) - 1)
            y = height - padding - (price - low) * (height - padding * 2) / span
            points.append(f"{x:.2f},{y:.2f}")
        markers = []
        for trade in trades:
            for kind, timestamp, color in (
                ("entry", trade.entry_at, "#159957"),
                ("exit", trade.exit_at, "#d33f49"),
            ):
                when = datetime.fromisoformat(timestamp)
                nearest_index = min(
                    range(len(bars)),
                    key=lambda index: abs((bars[index].end - when).total_seconds()),
                )
                x = padding + nearest_index * (width - padding * 2) / max(1, len(prices) - 1)
                price = prices[nearest_index]
                y = height - padding - (price - low) * (height - padding * 2) / span
                markers.append(
                    f'<circle cx="{x:.2f}" cy="{y:.2f}" r="5" fill="{color}">'
                    f"<title>{kind}</title></circle>"
                )
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}">'
            '<rect width="100%" height="100%" fill="#f7f9fc"/>'
            f'<polyline fill="none" stroke="#3457d5" stroke-width="2" points="{" ".join(points)}"/>'
            f"{''.join(markers)}"
            f'<text x="30" y="20" font-size="13">High {high:.2f}</text>'
            f'<text x="30" y="270" font-size="13">Low {low:.2f}</text></svg>'
        )

    @staticmethod
    def _atomic_text(path: Path, content: str) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)

    @staticmethod
    def content_hash(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()
