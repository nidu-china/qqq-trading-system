from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from qqq_trader.config import Settings
from qqq_trader.domain import Bar

_TRADING_DEFAULTS = {
    # 应用与运行模式
    "trading_mode": "paper",
    "strategy_mode": "trend",
    "paper_starting_equity": "10000",
    "account_id": "",
    "underlying_symbol": "QQQ.US",
    "database_url": "mysql+asyncmy://qqq:qqq@localhost:3306/qqq?charset=utf8mb4",
    "data_dir": "/data/market",
    "report_dir": "/data/reports",
    "log_dir": "logs",
    # Longbridge API
    "longbridge_app_key": "",
    "longbridge_app_secret": "",
    "longbridge_access_token": "",
    "longbridge_request_timeout_seconds": "60",
    "report_at": "16:15:00",
    # API 与运行配置
    "api_host": "127.0.0.1",
    "api_port": 8000,
    "api_token": "",
    "log_level": "INFO",
    "scheduler_poll_seconds": "1",
    # VIX 波动率过滤
    "volatility_filter_enabled": True,
    "volatility_symbol": ".VIX.US",
    "volatility_lookback_days": 20,
    "volatility_max_staleness_minutes": 10,
    "volatility_risk_off_percentile": "0.80",
    "volatility_recovery_percentile": "0.65",
    "volatility_rise_5m": "0.02",
    "volatility_rise_15m": "0.03",
    "volatility_fall_5m": "-0.02",
    "volatility_fall_15m": "-0.03",
    "volatility_shock_5m": "0.08",
    "volatility_shock_15m": "0.12",
    # 仓位与风控
    "max_premium_fraction": "0.50",
    "max_contracts": 10,
    "max_trades_per_day": 5,
    "cooldown_minutes": 3,
    "fee_per_contract": "1.50",
    "slippage_quote": "0.02",
    # 离场
    "option_stop_loss_pct": "0.25",
    "tp1_profit_pct": "1.0",
    "tp2_profit_pct": "2.5",
    "stale_minutes": 20,
    # 期权流动性
    "max_spread_ratio": "0.10",
    "max_spread_absolute": "0.20",
    "min_open_interest": 100,
    "min_option_volume": 10,
    "strike_offset": "2",
    # 统一时间窗口
    "phase_collect_start": "09:30:00",
    "phase_collect_end": "09:40:00",
    "phase_opening_end": "10:00:00",
    "phase_main_end": "12:00:00",
    # BOLL/MACD
    "timed_opening_last_signal": "09:45:00",
    "timed_opening_flat": "09:55:00",
    "timed_boll_period": 20,
    "timed_boll_stddev": "2",
    "timed_macd_fast": 8,
    "timed_macd_slow": 17,
    "timed_macd_signal": 9,
    "timed_rsi_period": 14,
    "timed_call_rsi_max": "70",
    "timed_put_rsi_min": "30",
    "timed_volume_lookback": 20,
    "timed_volume_ratio": "1.2",
    "timed_vix_volume_adjustment": "0.10",
    "timed_vix_trend_min_change": "0.005",
    "timed_trend_cross_lookback": 20,
    "timed_trend_max_crosses": 2,
    "timed_continuation_max_band_extension": "1.20",
    "timed_continuation_fresh_macd_volume_multiplier": "1.20",
    "timed_normal_cross2_max_band_extension": "1.20",
    "timed_normal_fresh_macd_volume_multiplier": "1.20",
    "timed_reversal_min_bars": 3,
    "timed_reversal_window": 5,
    # Trend ORB
    "trend_entry_end": "11:30:00",
    "trend_ema_fast": 9,
    "trend_ema_slow": 21,
    "trend_breakout_confirm_bars": 3,
    "trend_max_vwap_crosses": 3,
    "trend_ema_exit_bars": 2,
}


def make_settings(**overrides) -> Settings:
    """Create Settings with all required trading params for tests."""
    return Settings(_env_file=None, **{**_TRADING_DEFAULTS, **overrides})


@pytest.fixture
def bullish_bars() -> list[Bar]:
    """Completed QQQ 1-min bars that trigger the new StrategyEngine.

    50 bars spanning 9:30-10:20 ET (13:30-14:20 UTC).
    Pattern:
    - Bars 0-14 (9:30-9:45): observation phase, range ~100-100.5
    - Bars 15-29 (9:45-10:00): dynamic-structure breakout with volume
    - Bars 30-44 (10:00-10:15): pullback toward VWAP then bullish re-entry
    - Bars 45-49 (10:15-10:20): continuation
    """
    start = datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc)  # 9:30 ET
    bars: list[Bar] = []

    # Phase 1: observation (9:30-9:45) - range bound 99.5-100.5
    for i in range(15):
        base = Decimal("100") + Decimal(str(i * 0.02))
        bars.append(Bar(
            symbol="QQQ.US",
            start=start + timedelta(minutes=i),
            end=start + timedelta(minutes=i + 1),
            open=base,
            high=base + Decimal("0.3"),
            low=base - Decimal("0.2"),
            close=base + Decimal("0.1"),
            volume=1000,
        ))

    # Phase 2: dynamic-structure breakout (9:45-10:00)
    for i in range(15):
        idx = 15 + i
        base = Decimal("100.5") + Decimal(str(i * 0.15))
        bars.append(Bar(
            symbol="QQQ.US",
            start=start + timedelta(minutes=idx),
            end=start + timedelta(minutes=idx + 1),
            open=base,
            high=base + Decimal("0.3"),
            low=base - Decimal("0.1"),
            close=base + Decimal("0.2"),
            volume=2000,
        ))

    # Phase 3: Pullback + re-entry (10:00-10:15)
    pullback_start = Decimal("102.9")
    for i in range(15):
        idx = 30 + i
        if i < 5:
            # Pullback toward VWAP and recent structure
            base = pullback_start - Decimal(str(i * 0.3))
        elif i < 10:
            # Stabilize near support
            base = Decimal("101.5") + Decimal(str((i - 5) * 0.05))
        else:
            # Re-entry with a bullish structure confirmation
            base = Decimal("101.7") + Decimal(str((i - 10) * 0.2))
        bars.append(Bar(
            symbol="QQQ.US",
            start=start + timedelta(minutes=idx),
            end=start + timedelta(minutes=idx + 1),
            open=base - Decimal("0.05"),
            high=base + Decimal("0.2"),
            low=base - Decimal("0.15"),
            close=base + Decimal("0.1"),
            volume=1500,
        ))

    # Phase 4: Continuation (10:15-10:20)
    for i in range(5):
        idx = 45 + i
        base = Decimal("102.5") + Decimal(str(i * 0.1))
        bars.append(Bar(
            symbol="QQQ.US",
            start=start + timedelta(minutes=idx),
            end=start + timedelta(minutes=idx + 1),
            open=base,
            high=base + Decimal("0.2"),
            low=base - Decimal("0.1"),
            close=base + Decimal("0.15"),
            volume=1200,
        ))

    return bars
