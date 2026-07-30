import pytest
from conftest import make_settings

from qqq_trader.configuration import editable_values, with_editable_values


def test_editable_values_exclude_credentials_and_infrastructure():
    settings = make_settings(
        account_id="secret-account",
        longbridge_app_key="secret-key",
        longbridge_app_secret="secret-value",
        longbridge_access_token="secret-token",
    )
    values = editable_values(settings)
    assert "account_id" not in values
    assert "database_url" not in values
    assert "longbridge_app_key" not in values
    assert "longbridge_app_secret" not in values
    assert "longbridge_access_token" not in values
    assert values["bollinger_period"] == 20
    assert values["bollinger_stddev"] == "2"
    assert "adx_period" in values
    assert "atr_period" in values
    assert "risk_per_trade" not in values
    assert "entry_start" not in values


def test_online_configuration_runs_cross_field_validation():
    with pytest.raises(ValueError, match="not editable"):
        with_editable_values(make_settings(), {"nonexistent_field": 30})

    updated = with_editable_values(make_settings(), {"bollinger_stddev": "2.5"})
    assert str(updated.bollinger_stddev) == "2.5"


def test_online_configuration_rejects_non_editable_fields():
    with pytest.raises(ValueError, match="not editable"):
        with_editable_values(make_settings(), {"trading_mode": "live"})


def test_live_authorization_requires_complete_longbridge_api_credentials():
    settings = make_settings(
        trading_mode="live",
        account_id="account",
        longbridge_app_key="key",
        longbridge_app_secret="secret",
        longbridge_access_token="token",
    )
    settings.assert_live_authorized()

    missing_token = make_settings(
        trading_mode="live",
        account_id="account",
        longbridge_app_key="key",
        longbridge_app_secret="secret",
        longbridge_access_token="",
    )
    with pytest.raises(RuntimeError, match="LONGBRIDGE_ACCESS_TOKEN"):
        missing_token.assert_live_authorized()


def test_legacy_fields_are_silently_ignored():
    updated = with_editable_values(
        make_settings(),
        {"paper_signal_only": True, "risk_per_trade": "0.01", "macd_fast": 12},
    )
    assert "paper_signal_only" not in editable_values(updated)
    assert "risk_per_trade" not in editable_values(updated)


def test_indicator_config_defaults():
    settings = make_settings()
    assert settings.ema_fast_period == 9
    assert settings.ema_slow_period == 20
    assert settings.adx_period == 14
    assert settings.atr_period == 14
    assert settings.bollinger_period == 20
    assert settings.bollinger_stddev == 2
    assert settings.rsi_overbought == 70
    assert settings.rsi_oversold == 30
    assert (settings.macd_1m_fast, settings.macd_1m_slow, settings.macd_1m_signal) == (5, 10, 3)
    assert not hasattr(settings, "macd_5m_fast")


def test_indicator_period_validated():
    with pytest.raises(ValueError, match="indicator periods"):
        make_settings(ema_fast_period=1)


def test_indicator_cross_field_validation():
    with pytest.raises(ValueError, match="RSI thresholds"):
        make_settings(rsi_oversold=80, rsi_overbought=70)
    with pytest.raises(ValueError, match="1-minute MACD"):
        make_settings(macd_1m_fast=12, macd_1m_slow=10)
