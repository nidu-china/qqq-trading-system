from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from qqq_trader.api import create_app
from qqq_trader.cli import app
from qqq_trader.domain import SystemState


class FakeEngine:
    state = SystemState.READY
    realized_pnl = Decimal("12.50")
    trades_today = 1

    @staticmethod
    def status():
        return {"state": "ready", "position": None}


class FakeJournal:
    async def ping(self):
        return True


def test_read_only_health_status_and_metrics():
    client = TestClient(create_app(FakeEngine(), FakeJournal()))
    assert client.get("/health/live").status_code == 200
    ready = client.get("/health/ready")
    assert ready.status_code == 200 and ready.json()["ready"] is True
    assert client.get("/status").json()["state"] == "ready"
    assert "qqq_trader_realized_pnl_usd" in client.get("/metrics").text
    missing = client.get("/api/v1/trades/999")
    assert missing.status_code == 404
    assert missing.json()["code"] == "http_404"
    assert missing.headers["x-request-id"]
    assert client.get("/api/v1/signals").json()["items"] == []


def test_cli_exposes_all_operational_commands():
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("trade", "backfill", "backtest", "report", "reconcile"):
        assert command in result.stdout

    backtest_help = CliRunner().invoke(app, ["backtest", "--help"])
    assert backtest_help.exit_code == 0
    assert "--bars" in backtest_help.stdout
