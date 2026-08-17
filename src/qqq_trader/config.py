from __future__ import annotations

from datetime import time
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .domain import TradingMode

NY_TZ = ZoneInfo("America/New_York")


def _default_env_file() -> Path:
    """Prefer the launch directory, then support an editable-install project root."""
    working_directory = Path.cwd() / ".env"
    if working_directory.is_file():
        return working_directory
    project_root = Path(__file__).resolve().parents[2] / ".env"
    return project_root if project_root.is_file() else working_directory


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_default_env_file(),
        env_file_encoding="utf-8-sig",
        case_sensitive=False,
        extra="ignore",
    )

    # 应用与运行模式（必须在 .env 显式配置）
    trading_mode: TradingMode
    strategy_mode: str
    paper_starting_equity: Decimal
    account_id: str
    underlying_symbol: str
    database_url: str
    data_dir: Path
    report_dir: Path
    log_dir: Path

    # Longbridge API（必须在 .env 显式配置）
    longbridge_app_key: SecretStr
    longbridge_app_secret: SecretStr
    longbridge_access_token: SecretStr
    longbridge_request_timeout_seconds: Decimal

    report_at: time

    # VIX 波动率过滤（必须在 .env 显式配置）
    volatility_filter_enabled: bool
    volatility_symbol: str
    volatility_lookback_days: int
    volatility_max_staleness_minutes: int
    volatility_risk_off_percentile: Decimal
    volatility_recovery_percentile: Decimal
    volatility_rise_5m: Decimal
    volatility_rise_15m: Decimal
    volatility_fall_5m: Decimal
    volatility_fall_15m: Decimal
    volatility_shock_5m: Decimal
    volatility_shock_15m: Decimal

    # 仓位与风控（必须在 .env 显式配置）
    max_premium_fraction: Decimal
    max_contracts: int
    max_trades_per_day: int
    cooldown_minutes: int
    fee_per_contract: Decimal
    slippage_quote: Decimal

    # 离场（必须在 .env 显式配置）
    option_stop_loss_pct: Decimal
    tp1_profit_pct: Decimal
    tp2_profit_pct: Decimal
    stale_minutes: int

    # 期权流动性（必须在 .env 显式配置）
    max_spread_ratio: Decimal
    max_spread_absolute: Decimal
    min_open_interest: int
    min_option_volume: int
    strike_offset: Decimal

    # BOLL/MACD 策略参数（必须在 .env 显式配置，STRATEGY_MODE=boll_macd 专用）
    timed_opening_start: time
    timed_opening_last_signal: time
    timed_opening_flat: time
    timed_main_start: time
    timed_main_last_signal: time
    timed_boll_period: int
    timed_boll_stddev: Decimal
    timed_macd_fast: int
    timed_macd_slow: int
    timed_macd_signal: int
    timed_rsi_period: int
    timed_call_rsi_max: Decimal
    timed_put_rsi_min: Decimal
    timed_volume_lookback: int
    timed_volume_ratio: Decimal
    timed_vix_volume_adjustment: Decimal
    timed_vix_trend_min_change: Decimal
    timed_trend_cross_lookback: int
    timed_trend_max_crosses: int
    timed_continuation_max_band_extension: Decimal
    timed_continuation_fresh_macd_volume_multiplier: Decimal
    timed_normal_cross2_max_band_extension: Decimal
    timed_normal_fresh_macd_volume_multiplier: Decimal
    timed_reversal_min_bars: int
    timed_reversal_window: int

    # Trend ORB 策略参数（必须在 .env 显式配置，STRATEGY_MODE=trend 专用）
    trend_or_start: time
    trend_or_end: time
    trend_entry_end: time
    trend_ema_fast: int
    trend_ema_slow: int
    trend_breakout_confirm_bars: int
    trend_max_vwap_crosses: int
    trend_ema_exit_bars: int

    # API 与运行配置（必须在 .env 显式配置）
    api_host: str
    api_port: int
    api_token: SecretStr
    log_level: str
    scheduler_poll_seconds: Decimal

    @model_validator(mode="after")
    def validate_safety(self) -> Settings:
        if self.longbridge_request_timeout_seconds <= 0:
            raise ValueError("Longbridge request timeout must be positive")
        if self.timed_boll_period < 2:
            raise ValueError("timed_boll_period must be >= 2")
        if self.timed_boll_stddev <= 0:
            raise ValueError("timed_boll_stddev must be positive")
        if self.timed_macd_fast >= self.timed_macd_slow:
            raise ValueError("timed_macd_fast must be less than timed_macd_slow")
        if self.trend_ema_fast >= self.trend_ema_slow:
            raise ValueError("trend_ema_fast must be less than trend_ema_slow")
        percentiles = (
            self.volatility_recovery_percentile,
            self.volatility_risk_off_percentile,
        )
        if any(value <= 0 or value >= 1 for value in percentiles):
            raise ValueError("volatility percentiles must be between 0 and 1")
        if self.volatility_recovery_percentile >= self.volatility_risk_off_percentile:
            raise ValueError("recovery percentile must be below risk-off percentile")
        if self.volatility_lookback_days < 5 or self.volatility_max_staleness_minutes < 1:
            raise ValueError("volatility history and staleness settings are invalid")
        if self.volatility_fall_5m >= 0 or self.volatility_fall_15m >= 0:
            raise ValueError("volatility fall thresholds must be negative")
        if min(self.volatility_rise_5m, self.volatility_rise_15m) <= 0:
            raise ValueError("volatility rise thresholds must be positive")
        if (
            self.volatility_shock_5m <= self.volatility_rise_5m
            or self.volatility_shock_15m <= self.volatility_rise_15m
        ):
            raise ValueError("volatility shock thresholds must exceed rise thresholds")
        if self.max_premium_fraction <= 0 or self.max_premium_fraction > Decimal("0.5"):
            raise ValueError("max_premium_fraction must be between 0 and 0.5")
        if self.max_contracts < 1:
            raise ValueError("max_contracts must be at least 1")
        if self.max_trades_per_day < 1:
            raise ValueError("max_trades_per_day must be at least 1")
        if self.option_stop_loss_pct <= 0 or self.option_stop_loss_pct >= 1:
            raise ValueError("option_stop_loss_pct must be between 0 and 1")
        if self.tp1_profit_pct <= 0:
            raise ValueError("tp1_profit_pct must be positive")
        if self.tp2_profit_pct <= self.tp1_profit_pct:
            raise ValueError("tp2_profit_pct must exceed tp1_profit_pct")
        if self.fee_per_contract < 0:
            raise ValueError("fee_per_contract must be non-negative")
        return self

    @property
    def rules(self):
        from .policy import rules_from_settings
        return rules_from_settings(self)

    def assert_live_authorized(self) -> None:
        if self.trading_mode is not TradingMode.LIVE:
            return
        if not self.account_id:
            raise RuntimeError("ACCOUNT_ID is required for live trading")
        credentials = (
            self.longbridge_app_key.get_secret_value(),
            self.longbridge_app_secret.get_secret_value(),
            self.longbridge_access_token.get_secret_value(),
        )
        if not all(credentials):
            raise RuntimeError(
                "LONGBRIDGE_APP_KEY, LONGBRIDGE_APP_SECRET and "
                "LONGBRIDGE_ACCESS_TOKEN are required for live trading"
            )
