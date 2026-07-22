from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from qqq_trader.config import Settings
from qqq_trader.domain import AccountSnapshot, Direction, OptionContract, Position, Quote
from qqq_trader.risk import ContractSelector, RiskEngine


def contract(strike: str, right: Direction) -> OptionContract:
    marker = "C" if right is Direction.CALL else "P"
    return OptionContract(
        f"QQQ260715{marker}{int(Decimal(strike) * 1000):06d}.US",
        "QQQ.US",
        date(2026, 7, 15),
        Decimal(strike),
        right,
    )


def test_selector_uses_directional_tie_break():
    contracts = [
        contract("101", Direction.CALL),
        contract("102", Direction.CALL),
        contract("103", Direction.CALL),
        contract("98", Direction.PUT),
        contract("99", Direction.PUT),
    ]
    selector = ContractSelector(Decimal("2"))
    assert selector.select(contracts, Direction.CALL, Decimal("100.5")).strike == Decimal("103")
    assert selector.select(contracts, Direction.PUT, Decimal("100.5")).strike == Decimal("98")


def test_liquidity_and_position_size():
    settings = Settings(min_open_interest=100, min_option_volume=10)
    risk = RiskEngine(settings)
    now = datetime.now(timezone.utc)
    quote = Quote("OPT.US", now, Decimal("1"), Decimal("0.98"), Decimal("1.00"), 20, 200)
    assert risk.quote_problem(quote, now) is None
    account = AccountSnapshot(now, Decimal("100000"), Decimal("100000"))
    assert risk.position_size(account, Decimal("1")) == settings.max_contracts


def test_position_size_skips_unsafe_minimum():
    settings = Settings(max_contracts=10)
    account = AccountSnapshot(datetime.now(timezone.utc), Decimal("1000"), Decimal("1000"))
    assert RiskEngine(settings).position_size(account, Decimal("5")) == 0


def test_partial_and_single_contract_exits():
    settings = Settings()
    risk = RiskEngine(settings)
    now = datetime(2026, 7, 15, 15, 0, tzinfo=timezone.utc)
    multi = Position("OPT.US", Direction.CALL, 5, Decimal("1"), now)
    decision = risk.exit_decision(multi, Decimal("1.50"), now)
    assert decision is not None and decision.quantity == 3
    assert decision.new_stop == Decimal("1")

    single = Position("OPT.US", Direction.CALL, 1, Decimal("1"), now)
    decision = risk.exit_decision(single, Decimal("1.50"), now)
    assert decision is not None and decision.quantity == 1


def test_daily_loss_limit_includes_unrealized():
    settings = Settings()
    account = AccountSnapshot(
        datetime.now(timezone.utc),
        Decimal("100000"),
        Decimal("50000"),
        Decimal("-1000"),
        Decimal("-1000"),
    )
    assert RiskEngine(settings).daily_loss_breached(account, Decimal("100000"))
