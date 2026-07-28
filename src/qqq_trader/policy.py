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
    timed_main_last_signal: time = time(11, 25)
    timed_max_trades_per_day: int = 5
    timed_opening_size_factor: Decimal = Decimal("1")
    timed_opening_volume_ratio: Decimal = Decimal("2.0")
    timed_opening_range_ratio: Decimal = Decimal("0.75")
    timed_opening_body_ratio: Decimal = Decimal("0.65")
    timed_opening_close_extreme: Decimal = Decimal("0.35")
    timed_slow_ema_period: int = 21

    cooldown_minutes: int = 5
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

    max_premium_fraction: Decimal = Decimal("0.05")
    max_contracts: int = 10
    option_stop_loss_pct: Decimal = Decimal("0.25")
    daily_loss_limit: Decimal = Decimal("0.02")
    tp1_profit_pct: Decimal = Decimal("1.0")
    tp2_profit_pct: Decimal = Decimal("2.5")
    trailing_giveback_pct: Decimal = Decimal("0.30")
    trailing_atr_multiplier: Decimal = Decimal("0.5")
    stale_minutes: int = 30

    fee_per_contract: Decimal = Decimal("1.50")
    slippage_quote: Decimal = Decimal("0.02")
    max_quote_age_seconds: Decimal = Decimal("2")
    max_spread_ratio: Decimal = Decimal("0.10")
    max_spread_absolute: Decimal = Decimal("0.20")
    min_open_interest: int = 100
    min_option_volume: int = 10

    target_delta: Decimal = Decimal("0.45")
    option_candidate_count: int = 5

    synthetic_call_delta: Decimal = Decimal("0.45")
    synthetic_put_delta: Decimal = Decimal("-0.45")
    synthetic_gamma: Decimal = Decimal("0.055")
    synthetic_vega: Decimal = Decimal("0.099")
    synthetic_theta: Decimal = Decimal("-3")
    synthetic_min_price: Decimal = Decimal("0.05")

    def entry_start_for(self, profile: str) -> time:
        return self.timed_opening_start if profile == "timed_trend" else self.entry_start

    def entry_end_for(self, profile: str) -> time:
        return self.entry_end

    def max_trades_for(self, profile: str) -> int:
        return (
            self.timed_max_trades_per_day
            if profile == "timed_trend"
            else self.max_trades_per_day
        )


RULES = StrategyRules()


def rules_from_settings(settings) -> StrategyRules:
    """Build StrategyRules overriding configurable fields from Settings."""
    overrides = {}
    _FIELDS = (
        "max_premium_fraction", "max_contracts", "max_trades_per_day",
        "cooldown_minutes", "daily_loss_limit", "fee_per_contract",
        "slippage_quote", "option_stop_loss_pct", "tp1_profit_pct",
        "tp2_profit_pct", "trailing_atr_multiplier", "stale_minutes",
        "max_spread_ratio", "max_spread_absolute", "min_open_interest",
        "min_option_volume", "target_delta",
        "timed_opening_volume_ratio", "timed_opening_range_ratio",
        "timed_opening_body_ratio", "timed_opening_close_extreme",
        "timed_slow_ema_period",
    )
    for field in _FIELDS:
        value = getattr(settings, field, None)
        if value is not None:
            overrides[field] = value
    if "max_trades_per_day" in overrides:
        overrides["timed_max_trades_per_day"] = overrides["max_trades_per_day"]
    return StrategyRules(**overrides)
