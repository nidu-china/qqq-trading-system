from decimal import Decimal

import pytest

from qqq_trader.config import Settings
from qqq_trader.configuration import editable_values, with_editable_values


def test_editable_values_exclude_credentials_and_infrastructure():
    settings = Settings(
        _env_file=None,
        account_id="secret-account",
        longbridge_app_key="secret-key",
        longbridge_app_secret="secret-value",
        longbridge_access_token="secret-token",
    )
    values = editable_values(settings)
    assert "account_id" not in values
    assert "database_url" not in values
    assert "live_trading_ack" not in values
    assert "longbridge_app_key" not in values
    assert "longbridge_app_secret" not in values
    assert "longbridge_access_token" not in values
    assert values["risk_per_trade"] == "0.0025"

    # Verify the field list matches actual settings
    assert "adx_period" in values
    assert "atr_period" in values


def test_online_configuration_runs_cross_field_validation():
    with pytest.raises(ValueError, match="not editable"):
        with_editable_values(Settings(), {"nonexistent_field": 30})

    updated = with_editable_values(Settings(), {"risk_per_trade": "0.01"})
    assert updated.risk_per_trade == Decimal("0.01")


def test_online_configuration_rejects_non_editable_fields():
    with pytest.raises(ValueError, match="not editable"):
        with_editable_values(Settings(), {"trading_mode": "live"})


def test_legacy_paper_signal_only_value_is_ignored():
    updated = with_editable_values(
        Settings(_env_file=None),
        {"paper_signal_only": True, "risk_per_trade": "0.01"},
    )
    assert updated.risk_per_trade == Decimal("0.01")
    assert "paper_signal_only" not in editable_values(updated)


def test_strategy_config_defaults():
    settings = Settings(_env_file=None)
    assert settings.ema_fast_period == 9
    assert settings.ema_slow_period == 20
    assert settings.adx_period == 14
    assert settings.atr_period == 14
    assert settings.macd_fast == 12
    assert settings.macd_slow == 26


def test_indicator_period_validated():
    with pytest.raises(ValueError, match="indicator periods"):
        Settings(_env_file=None, ema_fast_period=1)
