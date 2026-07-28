from __future__ import annotations

from typing import Any

from .config import Settings

EDITABLE_FIELDS = (
    "bollinger_period",
    "bollinger_stddev",
    "rsi_period",
    "rsi_overbought",
    "rsi_oversold",
    "ema_fast_period",
    "ema_slow_period",
    "macd_1m_fast",
    "macd_1m_slow",
    "macd_1m_signal",
    "adx_period",
    "atr_period",
    "volatility_filter_enabled",
    "volatility_symbol",
    "volatility_lookback_days",
    "volatility_max_staleness_minutes",
    "volatility_risk_off_percentile",
    "volatility_recovery_percentile",
    "volatility_rise_5m",
    "volatility_rise_15m",
    "volatility_fall_5m",
    "volatility_fall_15m",
    "volatility_shock_5m",
    "volatility_shock_15m",
)
LEGACY_IGNORED_FIELDS = {
    "entry_start",
    "entry_end",
    "forced_close",
    "cooldown_minutes",
    "max_trades_per_day",
    "macd_fast",
    "macd_slow",
    "macd_signal",
    "macd_5m_fast",
    "macd_5m_slow",
    "macd_5m_signal",
    "rvol_lookback_days",
    "or_width_lookback_days",
    "or_width_low_pct",
    "or_width_high_pct",
    "trend_rvol_min",
    "trend_adx_min",
    "range_adx_max",
    "chase_atr_factor",
    "strike_offset",
    "max_quote_age_seconds",
    "max_spread_ratio",
    "max_spread_absolute",
    "min_open_interest",
    "min_option_volume",
    "risk_per_trade",
    "daily_loss_limit_r",
    "atr_stop_buffer",
    "max_stop_atr_ratio",
    "tp1_r",
    "tp2_r",
    "stale_minutes",
    "reduce_at",
    "max_premium_fraction",
    "max_contracts",
    "fee_per_contract",
    "slippage_per_contract",
    "order_timeout_seconds",
    "entry_reprices",
    "max_entry_slippage_pct",
    "paper_signal_only",
    "macd_backtest_combinations",
    "score_threshold",
    "consecutive_bars",
    "orb_min_volume_ratio",
    "volume_average_period",
    "min_volume_ratio",
    "rsi_call_max",
    "rsi_put_min",
    "bb_width_max",
    "daily_loss_limit",
    "stop_loss_pct",
    "take_profit_1_pct",
    "take_profit_2_pct",
    "strategy_type",
    "dynamic_score_threshold",
    "dynamic_score_margin",
    "dynamic_score_confirmation_bars",
    "dynamic_direction_watch_minutes",
}


def editable_values(settings: Settings) -> dict[str, Any]:
    dumped = settings.model_dump(mode="json")
    return {field: dumped[field] for field in EDITABLE_FIELDS}


def with_editable_values(settings: Settings, values: dict[str, Any]) -> Settings:
    values = {key: value for key, value in values.items() if key not in LEGACY_IGNORED_FIELDS}
    unknown = sorted(set(values) - set(EDITABLE_FIELDS))
    if unknown:
        raise ValueError(f"fields are not editable: {', '.join(unknown)}")
    payload = settings.model_dump()
    payload.update(values)
    return Settings.model_validate(payload)
