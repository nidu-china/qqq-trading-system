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
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "qqq-0dte-trader"
    trading_mode: TradingMode = TradingMode.PAPER
    paper_starting_equity: Decimal = Decimal("100000")
    account_id: str = ""
    live_trading_ack: SecretStr = SecretStr("")
    underlying_symbol: str = "QQQ.US"
    database_url: str = "mysql+asyncmy://qqq:qqq@mysql:3306/qqq?charset=utf8mb4"
    data_dir: Path = Path("/data/market")
    report_dir: Path = Path("/data/reports")
    log_dir: Path = Path("logs")

    longbridge_client_id: str = ""
    longbridge_app_key: SecretStr = SecretStr("")
    longbridge_app_secret: SecretStr = SecretStr("")
    longbridge_access_token: SecretStr = SecretStr("")
    longbridge_request_timeout_seconds: Decimal = Decimal("60")

    entry_start: time = time(9, 35)
    entry_end: time = time(14, 0)
    forced_close: time = time(14, 0)
    report_at: time = time(16, 15)
    cooldown_minutes: int = 5
    max_trades_per_day: int = 5

    # Strategy parameters
    orb_min_volume_ratio: Decimal = Decimal("1.5")
    ema_fast_period: int = 9
    ema_slow_period: int = 21
    bollinger_period: int = 20
    bollinger_stddev: Decimal = Decimal("2")
    volume_average_period: int = 20
    min_volume_ratio: Decimal = Decimal("1.0")
    rsi_period: int = 14
    rsi_call_max: Decimal = Decimal("70")
    rsi_put_min: Decimal = Decimal("30")
    bb_width_max: Decimal = Decimal("0.02")
    strike_offset: Decimal = Decimal("2")

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

    max_quote_age_seconds: Decimal = Decimal("2")
    max_spread_ratio: Decimal = Decimal("0.10")
    max_spread_absolute: Decimal = Decimal("0.20")
    min_open_interest: int = 100
    min_option_volume: int = 10

    risk_per_trade: Decimal = Decimal("0.005")
    daily_loss_limit: Decimal = Decimal("0.02")
    stop_loss_pct: Decimal = Decimal("0.25")
    take_profit_1_pct: Decimal = Decimal("0.50")
    take_profit_2_pct: Decimal = Decimal("1.00")
    max_premium_fraction: Decimal = Decimal("0.05")
    max_contracts: int = 10
    fee_per_contract: Decimal = Decimal("1.50")
    slippage_per_contract: Decimal = Decimal("5.00")

    order_timeout_seconds: int = 6
    entry_reprices: int = 2
    max_entry_slippage_pct: Decimal = Decimal("0.02")

    api_host: str = "127.0.0.1"
    api_port: int = 8000
    api_token: SecretStr = SecretStr("")
    log_level: str = "INFO"
    scheduler_poll_seconds: Decimal = Decimal("1")

    @model_validator(mode="after")
    def validate_safety(self) -> Settings:
        fractions = (
            self.risk_per_trade,
            self.daily_loss_limit,
            self.stop_loss_pct,
            self.max_premium_fraction,
        )
        if any(value <= 0 or value >= 1 for value in fractions):
            raise ValueError("risk fractions must be between 0 and 1")
        if self.take_profit_1_pct <= 0 or self.take_profit_2_pct <= self.take_profit_1_pct:
            raise ValueError("take-profit thresholds are invalid")
        if not self.entry_start < self.entry_end <= self.forced_close:
            raise ValueError("trading times must be ordered")
        if (
            min(
                self.bollinger_period,
                self.volume_average_period,
                self.rsi_period,
            )
            < 1
        ):
            raise ValueError("indicator periods must be positive")
        if self.bollinger_period < 2 or self.bollinger_stddev <= 0:
            raise ValueError("Bollinger settings are invalid")
        if self.min_volume_ratio <= 0:
            raise ValueError("minimum volume ratio must be positive")
        if not Decimal(0) < self.rsi_put_min < self.rsi_call_max < Decimal(100):
            raise ValueError("RSI thresholds must satisfy 0 < put < call < 100")
        if self.max_contracts < 1 or self.max_trades_per_day < 1:
            raise ValueError("contract and trade limits must be positive")
        if self.longbridge_request_timeout_seconds <= 0:
            raise ValueError("Longbridge request timeout must be positive")
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
        return self

    def assert_live_authorized(self) -> None:
        if self.trading_mode is not TradingMode.LIVE:
            return
        expected = f"I_UNDERSTAND_LIVE_TRADING:{self.account_id}"
        if not self.account_id or self.live_trading_ack.get_secret_value() != expected:
            raise RuntimeError("live trading acknowledgement does not match account_id")
        if not self.longbridge_client_id:
            raise RuntimeError("LONGBRIDGE_CLIENT_ID is required for live trading")
