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
    assert values["volatility_filter_enabled"] is True
    assert "risk_per_trade" not in values
    assert "forced_close" not in values


def test_online_configuration_runs_cross_field_validation():
    with pytest.raises(ValueError, match="not editable"):
        with_editable_values(make_settings(), {"nonexistent_field": 30})

    updated = with_editable_values(make_settings(), {"volatility_filter_enabled": False})
    assert updated.volatility_filter_enabled is False


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


def test_indicator_config_values():
    settings = make_settings()
    assert settings.timed_boll_period == 20
    assert settings.timed_boll_stddev == 2
    assert (settings.timed_macd_fast, settings.timed_macd_slow, settings.timed_macd_signal) == (8, 17, 9)
    assert settings.trend_ema_fast == 9
    assert settings.trend_ema_slow == 21


def test_indicator_period_validated():
    with pytest.raises(ValueError, match="timed_boll_period"):
        make_settings(timed_boll_period=1)


def test_indicator_cross_field_validation():
    with pytest.raises(ValueError, match="timed_macd_fast"):
        make_settings(timed_macd_fast=20, timed_macd_slow=10)
