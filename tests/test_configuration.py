from decimal import Decimal

import pytest

from qqq_trader.config import Settings
from qqq_trader.configuration import editable_values, with_editable_values


def test_editable_values_exclude_credentials_and_infrastructure():
    settings = Settings(
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
    assert values["risk_per_trade"] == "0.005"


def test_online_configuration_runs_cross_field_validation():
    with pytest.raises(ValueError, match="MACD"):
        with_editable_values(Settings(), {"macd_fast": 30, "macd_slow": 20})

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


def test_macd_defaults_and_backtest_combinations():
    settings = Settings(_env_file=None)
    assert (settings.macd_fast, settings.macd_slow, settings.macd_signal) == (5, 10, 3)
    assert settings.macd_parameter_sets() == [(8, 17, 9), (6, 13, 5), (5, 10, 3)]


def test_macd_backtest_combinations_are_validated():
    with pytest.raises(ValueError, match="MACD"):
        Settings(_env_file=None, macd_backtest_combinations="5,3,2")
