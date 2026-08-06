from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class StrategyRules:
    """Non-editable trading rules defined by STRATEGY.md."""

    entry_start: time = time(9, 45)
    entry_end: time = time(11, 30)
    reduce_at: time = time(11, 30)
    forced_close: time = time(13, 55)

    timed_opening_start: time = time(9, 35)
    timed_opening_last_signal: time = time(9, 42)
    timed_opening_flat: time = time(9, 45)
    timed_main_start: time = time(9, 45)
    timed_main_last_signal: time = time(12, 0)
    timed_max_trades_per_day: int = 5
    timed_boll_period: int = 20
    timed_boll_stddev: Decimal = Decimal("2")
    timed_macd_fast: int = 8
    timed_macd_slow: int = 17
    timed_macd_signal: int = 9
    timed_rsi_period: int = 14
    timed_call_rsi_max: Decimal = Decimal("70")
    timed_put_rsi_min: Decimal = Decimal("30")
    timed_volume_lookback: int = 20
    timed_volume_ratio: Decimal = Decimal("1.2")
    timed_vix_volume_adjustment: Decimal = Decimal("0.10")
    timed_vix_trend_min_change: Decimal = Decimal("0.005")
    timed_trend_cross_lookback: int = 20
    timed_trend_max_crosses: int = 2
    timed_continuation_max_band_extension: Decimal = Decimal("1.20")
    timed_continuation_fresh_macd_volume_multiplier: Decimal = Decimal("1.20")
    timed_normal_cross2_max_band_extension: Decimal = Decimal("1.20")
    timed_normal_fresh_macd_volume_multiplier: Decimal = Decimal("1.20")
    timed_reversal_min_bars: int = 3
    timed_reversal_window: int = 5

    cooldown_minutes: int = 3
    max_trades_per_day: int = 5
    signal_ttl_seconds: int = 60
    entry_reprices: int = 2
    order_timeout_seconds: int = 6

    structure_lookback: int = 10
    structure_break_atr: Decimal = Decimal("0.1")
    structure_excursion_atr: Decimal = Decimal("0.2")
    stop_atr_buffer: Decimal = Decimal("0.1")
    max_stop_atr_ratio: Decimal = Decimal("2")
    max_vwap_distance_atr: Decimal = Decimal("3.0")
    prior_level_distance_atr: Decimal = Decimal("0.5")
    range_prior_low_distance_atr: Decimal = Decimal("0.3")
    range_ema_distance_atr: Decimal = Decimal("0.25")
    range_vwap_change_atr: Decimal = Decimal("0.2")
    range_price_span_atr: Decimal = Decimal("4")
    reversal_timeout_minutes: int = 15
    retest_lookback: int = 5
    fast_retest_end: time = time(10, 0)
    retest_min_excursion_atr: Decimal = Decimal("0.2")
    retest_vwap_tolerance_atr: Decimal = Decimal("0.5")
    retest_ema_tolerance_atr: Decimal = Decimal("0.5")
    retest_call_rsi_min: Decimal = Decimal("40")
    retest_put_rsi_max: Decimal = Decimal("60")
    trend_adx_min: Decimal = Decimal("22")
    trend_structure_confirmations: int = 2
    early_ema_tolerance_atr: Decimal = Decimal("0")
    trend_call_rsi_min: Decimal = Decimal("60")
    trend_put_rsi_max: Decimal = Decimal("40")
    require_directional_macd: bool = True
    min_macd_hist_atr: Decimal = Decimal("0.1")
    range_adx_max: Decimal = Decimal("18")
    breakout_volume_ratio: Decimal = Decimal("1.2")

    max_premium_fraction: Decimal = Decimal("0.50")
    max_contracts: int = 10
    option_stop_loss_pct: Decimal = Decimal("0.25")
    tp1_profit_pct: Decimal = Decimal("1.0")
    tp2_profit_pct: Decimal = Decimal("2.5")
    trailing_activation_profit_pct: Decimal = Decimal("0.25")
    trailing_giveback_pct: Decimal = Decimal("0.30")
    stale_minutes: int = 20

    fee_per_contract: Decimal = Decimal("1.50")
    slippage_quote: Decimal = Decimal("0.02")
    max_quote_age_seconds: Decimal = Decimal("2")
    max_spread_ratio: Decimal = Decimal("0.10")
    max_spread_absolute: Decimal = Decimal("0.20")
    min_open_interest: int = 100
    min_option_volume: int = 10

    target_delta: Decimal = Decimal("0.45")
    strike_offset: Decimal = Decimal("2")
    option_candidate_count: int = 5

    synthetic_min_price: Decimal = Decimal("0.01")
    synthetic_iv_floor: Decimal = Decimal("0.08")
    synthetic_iv_cap: Decimal = Decimal("1.50")
    synthetic_default_iv: Decimal = Decimal("0.25")
    synthetic_vix_multiplier: Decimal = Decimal("0.80")
    synthetic_put_iv_skew: Decimal = Decimal("0.01")
    synthetic_observed_iv_max_age_minutes: int = 30
    synthetic_risk_free_rate: Decimal = Decimal("0.04")
    synthetic_dividend_yield: Decimal = Decimal("0.005")
    synthetic_minutes_per_year: Decimal = Decimal("142350")
    synthetic_min_spread: Decimal = Decimal("0.01")
    synthetic_max_spread: Decimal = Decimal("0.05")
    synthetic_spread_ratio: Decimal = Decimal("0.005")

RULES = StrategyRules()


def rules_from_settings(settings) -> StrategyRules:
    """Build StrategyRules overriding configurable fields from Settings."""
    overrides = {}
    _FIELDS = (
        "max_premium_fraction", "max_contracts", "max_trades_per_day",
        "cooldown_minutes", "fee_per_contract",
        "slippage_quote", "option_stop_loss_pct", "tp1_profit_pct",
        "tp2_profit_pct", "stale_minutes",
        "max_spread_ratio", "max_spread_absolute", "min_open_interest",
        "min_option_volume", "target_delta", "strike_offset",
    )
    for field in _FIELDS:
        value = getattr(settings, field, None)
        if value is not None:
            overrides[field] = value
    if "max_trades_per_day" in overrides:
        overrides["timed_max_trades_per_day"] = overrides["max_trades_per_day"]
    return StrategyRules(**overrides)
