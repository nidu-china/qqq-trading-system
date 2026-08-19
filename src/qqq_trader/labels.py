"""Centralised Chinese display labels for enums, rejection reasons,
and strategy parameter metadata.

The frontend fetches these via ``GET /api/v1/labels`` and
``GET /api/v1/strategy-params`` so it never needs to hardcode
its own translation tables.
"""

from __future__ import annotations

from typing import Any

EXIT_REASON_LABELS: dict[str, str] = {
    "stop_loss": "止损",
    "direction_reversal": "方向反转",
    "take_profit_1": "止盈1",
    "take_profit_2": "止盈2",
    "trailing_stop": "移动止盈",
    "stale_position": "超时平仓",
    "vwap_cross": "VWAP穿越",
    "daily_loss": "日亏损限制",
    "forced_close": "尾盘清仓",
    "shutdown": "系统关闭",
    "structure_stop": "结构止损",
    "state_invalidation": "状态失效",
    "bollinger_middle": "BOLL中轨",
    "bollinger_upper": "BOLL上轨",
    "opening_cutoff": "开盘截止",
    "five_bar_stop": "5根K线止损",
    "trend_ema_exit": "EMA趋势退出",
}

REJECT_LABELS: dict[str, str] = {
    "volatility_unavailable_stale_intraday_data": "VIX 盘中数据过期",
    "volatility_unavailable_missing_intraday_data": "VIX 盘中数据缺失",
    "volatility_unavailable_insufficient_daily_history": "VIX 日线历史不足",
    "volatility_unavailable_insufficient_intraday_history": "VIX 盘中历史不足",
    "volatility_risk_off": "波动率风险偏高",
    "volatility_shock": "波动率剧烈冲击",
    "missing_option_frame": "期权报价缺失",
    "relative_spread_too_wide": "期权价差过大",
    "absolute_spread_too_wide": "期权绝对价差过大",
    "stale_quote": "报价过时",
    "max_trades_per_day": "每日交易次数上限",
    "cooldown": "冷却期内",
    "risk_budget_too_small": "风险预算不足",
    "no_liquid_contract": "无流动性合约",
    "quote_error": "报价异常",
    "option_chain_error": "期权链错误",
    "outside_entry_window": "非入场时间",
    "signal_expired": "信号超时",
}

REGIME_LABELS: dict[str, str] = {
    "normal": "正常",
    "elevated": "偏高",
    "risk_off": "风险关闭",
    "recovery": "恢复期",
    "shock": "剧烈冲击",
    "unavailable": "数据不可用",
}

def all_labels() -> dict[str, dict[str, str]]:
    return {
        "exit_reasons": EXIT_REASON_LABELS,
        "reject_reasons": REJECT_LABELS,
        "regimes": REGIME_LABELS,
    }


from .policy import StrategyRules

_RULES = StrategyRules()


def _time_str(t) -> str:
    return t.strftime("%H:%M:%S")


def _p(key: str, label: str, kind: str = "decimal",
       default: Any = None, **kwargs: Any) -> dict[str, Any]:
    """Build a parameter descriptor, auto-reading default from StrategyRules."""
    if default is None:
        val = getattr(_RULES, key, None)
        if val is not None:
            from datetime import time as _time
            if isinstance(val, _time):
                default = _time_str(val)
            else:
                default = val if isinstance(val, (int, bool)) else str(val)
    d: dict[str, Any] = {"key": key, "label": label, "type": kind}
    if default is not None:
        d["default"] = default
    d.update(kwargs)
    return d


STRATEGY_PARAMS: dict[str, dict[str, Any]] = {
    "shared": {
        "label": "共用参数",
        "params": [
            _p("phase_collect_start", "信号收集开始", "time"),
            _p("phase_collect_end", "信号收集结束", "time"),
            _p("phase_opening_end", "开盘窗口结束", "time"),
            _p("phase_main_end", "主窗口结束", "time"),
            _p("max_premium_fraction", "最大权利金比例", step=0.05, min=0.01, max=0.5),
            _p("max_contracts", "最大合约数", "int", min=1, max=50),
            _p("max_trades_per_day", "每日最大交易次数", "int", min=1, max=20),
            _p("cooldown_minutes", "冷却时间(分)", "int", min=0, max=30),
            _p("option_stop_loss_pct", "期权止损比例", step=0.05, min=0.01, max=0.99),
            _p("tp1_profit_pct", "止盈1阈值", step=0.1, min=0.1),
            _p("tp2_profit_pct", "止盈2阈值", step=0.1, min=0.2),
            _p("stale_minutes", "超时平仓(分)", "int", min=1, max=60),
        ],
    },
    "boll_macd": {
        "label": "BOLL/MACD 策略",
        "params": [
            _p("timed_opening_last_signal", "开盘爆量截止", "time"),
            _p("timed_opening_flat", "开盘强平时间", "time"),
            _p("timed_boll_period", "BOLL 周期", "int", min=2, max=100),
            _p("timed_boll_stddev", "BOLL 标准差", step=0.1, min=0.1, max=5),
            _p("timed_macd_fast", "MACD 快线", "int", min=1, max=30),
            _p("timed_macd_slow", "MACD 慢线", "int", min=2, max=60),
            _p("timed_macd_signal", "MACD 信号线", "int", min=1, max=30),
            _p("timed_rsi_period", "RSI 周期", "int", min=2, max=50),
            _p("timed_call_rsi_max", "Call RSI 上限", min=50, max=100),
            _p("timed_put_rsi_min", "Put RSI 下限", min=0, max=50),
            _p("timed_volume_lookback", "量比回看根数", "int", min=2, max=100),
            _p("timed_volume_ratio", "入场量比门槛", step=0.1, min=0.5, max=5),
            _p("timed_vix_volume_adjustment", "VIX 量比调整", step=0.01),
            _p("timed_vix_trend_min_change", "VIX 趋势最小变化", step=0.001),
            _p("timed_trend_cross_lookback", "中轨穿越回看", "int", min=2, max=100),
            _p("timed_trend_max_crosses", "最大中轨穿越", "int", min=0, max=10),
            _p("timed_continuation_max_band_extension", "延续最大轨距", step=0.05),
            _p("timed_continuation_fresh_macd_volume_multiplier", "延续MACD量比乘数", step=0.05),
            _p("timed_normal_cross2_max_band_extension", "普通cross2最大轨距", step=0.05),
            _p("timed_normal_fresh_macd_volume_multiplier", "普通MACD量比乘数", step=0.05),
            _p("timed_reversal_min_bars", "反转确认根数", "int", min=1, max=10),
            _p("timed_reversal_window", "反转检测窗口", "int", min=2, max=20),
        ],
    },
    "trend": {
        "label": "Trend ORB 策略",
        "params": [
            _p("trend_entry_end", "入场截止", "time"),
            _p("trend_ema_fast", "EMA 快线周期", "int", min=2, max=50),
            _p("trend_ema_slow", "EMA 慢线周期", "int", min=3, max=100),
            _p("trend_breakout_confirm_bars", "突破确认根数", "int", min=1, max=10),
            _p("trend_max_vwap_crosses", "最大VWAP穿越", "int", min=1, max=10),
            _p("trend_ema_exit_bars", "EMA退出确认根数", "int", min=1, max=10),
        ],
    },
}


def strategy_params() -> dict[str, dict[str, Any]]:
    return STRATEGY_PARAMS
