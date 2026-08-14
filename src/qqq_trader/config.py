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

    app_name: str = "qqq-0dte-trader"
    trading_mode: TradingMode = TradingMode.PAPER
    strategy_mode: str = "boll_macd"
    paper_starting_equity: Decimal = Decimal("10000")
    account_id: str = ""
    underlying_symbol: str = "QQQ.US"
    database_url: str = "mysql+asyncmy://qqq:qqq@mysql:3306/qqq?charset=utf8mb4"
    data_dir: Path = Path("/data/market")
    report_dir: Path = Path("/data/reports")
    log_dir: Path = Path("logs")

    longbridge_app_key: SecretStr = SecretStr("")
    longbridge_app_secret: SecretStr = SecretStr("")
    longbridge_access_token: SecretStr = SecretStr("")
    longbridge_request_timeout_seconds: Decimal = Decimal("60")

    report_at: time = time(16, 15)

    # The only online-editable strategy values are technical indicators.
    bollinger_period: int = 20
    bollinger_stddev: Decimal = Decimal("2")
    rsi_period: int = 14
    rsi_overbought: Decimal = Decimal("70")
    rsi_oversold: Decimal = Decimal("30")
    ema_fast_period: int = 9
    ema_slow_period: int = 20
    macd_1m_fast: int = 5
    macd_1m_slow: int = 10
    macd_1m_signal: int = 3
    adx_period: int = 14
    atr_period: int = 14

    volatility_filter_enabled: bool = True
    volatility_symbol: str = ".VIX.US"
    volatility_lookback_days: int = 20
    volatility_max_staleness_minutes: int = 10
    volatility_risk_off_percentile: Decimal = Decimal("0.80")
    volatility_recovery_percentile: Decimal = Decimal("0.65")
    volatility_rise_5m: Decimal = Decimal("0.02")
    volatility_rise_15m: Decimal = Decimal("0.03")
    volatility_fall_5m: Decimal = Decimal("-0.02")
    volatility_fall_15m: Decimal = Decimal("-0.03")
    volatility_shock_5m: Decimal = Decimal("0.08")
    volatility_shock_15m: Decimal = Decimal("0.12")

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
    target_delta: Decimal
    strike_offset: Decimal = Decimal("2")

    # 分时趋势策略（必须在 .env 显式配置）
    api_host: str
    api_port: int
    api_token: SecretStr = SecretStr("")
    log_level: str = "INFO"
    scheduler_poll_seconds: Decimal = Decimal("1")

    @model_validator(mode="after")
    def validate_safety(self) -> Settings:
        if self.longbridge_request_timeout_seconds <= 0:
            raise ValueError("Longbridge request timeout must be positive")
        periods = (
            self.bollinger_period,
            self.rsi_period,
            self.ema_fast_period,
            self.ema_slow_period,
            self.macd_1m_fast,
            self.macd_1m_slow,
            self.macd_1m_signal,
            self.adx_period,
            self.atr_period,
        )
        if min(periods) < 2:
            raise ValueError("indicator periods must be >= 2")
        if self.ema_fast_period >= self.ema_slow_period:
            raise ValueError("ema_fast_period must be less than ema_slow_period")
        if self.macd_1m_fast >= self.macd_1m_slow:
            raise ValueError("1-minute MACD fast period must be less than slow period")
        if self.bollinger_stddev <= 0:
            raise ValueError("bollinger_stddev must be positive")
        if not 0 < self.rsi_oversold < self.rsi_overbought < 100:
            raise ValueError("RSI thresholds must satisfy 0 < oversold < overbought < 100")
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
