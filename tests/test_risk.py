from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from qqq_trader.config import Settings
from qqq_trader.domain import Direction, ExitReason, OptionContract, Position, Quote
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


def test_quote_problem_checks():
    settings = Settings(trading_mode="replay")
    risk = RiskEngine(settings)
    now = datetime.now(timezone.utc)
    good_quote = Quote("OPT.US", now, Decimal("1"), Decimal("0.98"), Decimal("1.00"), 20, 200)
    assert risk.quote_problem(good_quote, now) is None

    stale_quote = Quote("OPT.US", now - timedelta(seconds=5), Decimal("1"), Decimal("0.98"), Decimal("1.00"), 20, 200)
    assert risk.quote_problem(stale_quote, now) == "stale_quote"

    wide_spread = Quote("OPT.US", now, Decimal("1"), Decimal("0.80"), Decimal("1.20"), 20, 200)
    assert risk.quote_problem(wide_spread, now) == "absolute_spread_too_wide"


def test_position_size_r_based():
    settings = Settings(_env_file=None, trading_mode="replay")
    risk = RiskEngine(settings)
    equity = Decimal("100000")
    entry_price = Decimal("2.00")
    stop_distance_per_contract = Decimal("50")  # $0.50 * 100 shares
    size = risk.position_size(equity, entry_price, stop_distance_per_contract)
    # 1R = 100000 * 0.0025 = $250
    # contracts = 250 / 50 = 5
    assert size == 5


def test_position_size_premium_cap():
    settings = Settings(_env_file=None, trading_mode="replay", max_contracts=10)
    risk = RiskEngine(settings)
    equity = Decimal("10000")
    entry_price = Decimal("5.00")
    stop_distance_per_contract = Decimal("10")  # very small stop -> big size
    size = risk.position_size(equity, entry_price, stop_distance_per_contract)
    # by premium: 10000 * 0.05 / (5 * 100) = 1
    assert size == 1


def test_position_size_zero_when_equity_too_low():
    settings = Settings(_env_file=None, trading_mode="replay", max_contracts=10)
    risk = RiskEngine(settings)
    size = risk.position_size(Decimal("0"), Decimal("1"), Decimal("50"))
    assert size == 0


def test_daily_loss_breached():
    settings = Settings(trading_mode="replay")
    risk = RiskEngine(settings)
    # daily_loss_limit_r = 2, so cumulative_r_loss >= 2 triggers
    assert not risk.daily_loss_breached(Decimal("1.5"))
    assert risk.daily_loss_breached(Decimal("2.0"))
    assert risk.daily_loss_breached(Decimal("3.0"))


def test_exit_stop_loss():
    settings = Settings(trading_mode="replay")
    risk = RiskEngine(settings)
    now = datetime(2026, 7, 15, 15, 0, tzinfo=timezone.utc)  # 11:00 ET
    pos = Position("OPT.US", Direction.CALL, 5, Decimal("2.00"), now, stop_price=Decimal("1.50"))
    decision = risk.exit_decision(pos, Decimal("1.40"), now, r_value=Decimal("0.50"))
    assert decision is not None
    assert decision.reason is ExitReason.STOP_LOSS
    assert decision.quantity == 5


def test_exit_take_profit_1_reduces_half():
    settings = Settings(trading_mode="replay")
    risk = RiskEngine(settings)
    now = datetime(2026, 7, 15, 15, 0, tzinfo=timezone.utc)
    pos = Position("OPT.US", Direction.CALL, 6, Decimal("2.00"), now, stop_price=Decimal("1.50"))
    r_value = Decimal("0.50")  # 1R = $0.50 per contract
    # bid at +1R => entry + 0.50 = 2.50
    decision = risk.exit_decision(pos, Decimal("2.50"), now, r_value=r_value)
    assert decision is not None
    assert decision.reason is ExitReason.TAKE_PROFIT_1
    assert decision.quantity == 3  # (6+1)//2 = 3


def test_exit_take_profit_2_full_exit():
    settings = Settings(trading_mode="replay")
    risk = RiskEngine(settings)
    now = datetime(2026, 7, 15, 15, 0, tzinfo=timezone.utc)
    pos = Position("OPT.US", Direction.CALL, 5, Decimal("2.00"), now,
                   stop_price=Decimal("1.50"), first_target_taken=True)
    r_value = Decimal("0.50")
    # bid at +2.5R => entry + 1.25 = 3.25
    decision = risk.exit_decision(pos, Decimal("3.25"), now, r_value=r_value)
    assert decision is not None
    assert decision.reason is ExitReason.TAKE_PROFIT_2
    assert decision.quantity == 5


def test_exit_stale_position():
    settings = Settings(trading_mode="replay", stale_minutes=20)
    risk = RiskEngine(settings)
    opened_at = datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc)
    now = opened_at + timedelta(minutes=25)
    pos = Position("OPT.US", Direction.CALL, 3, Decimal("2.00"), opened_at,
                   stop_price=Decimal("1.50"), first_target_taken=True)
    r_value = Decimal("0.50")
    # Losing money (bid < entry) after stale_minutes
    decision = risk.exit_decision(pos, Decimal("1.90"), now, r_value=r_value)
    assert decision is not None
    assert decision.reason is ExitReason.STALE_POSITION


def test_exit_forced_close():
    settings = Settings(trading_mode="replay")
    risk = RiskEngine(settings)
    now = datetime(2026, 7, 15, 18, 0, tzinfo=timezone.utc)  # 14:00 ET
    pos = Position("OPT.US", Direction.CALL, 3, Decimal("2.00"),
                   datetime(2026, 7, 15, 15, 0, tzinfo=timezone.utc),
                   stop_price=Decimal("1.50"))
    decision = risk.exit_decision(pos, Decimal("2.20"), now, r_value=Decimal("0.50"))
    assert decision is not None
    assert decision.reason is ExitReason.FORCED_CLOSE


def test_exit_midday_reduce():
    settings = Settings(trading_mode="replay", reduce_at=time(13, 0))
    risk = RiskEngine(settings)
    # reduce_at = 13:00 ET = 17:00 UTC
    now = datetime(2026, 7, 15, 17, 1, tzinfo=timezone.utc)
    pos = Position("OPT.US", Direction.CALL, 4, Decimal("2.00"),
                   datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc),
                   stop_price=Decimal("1.50"))
    decision = risk.exit_decision(pos, Decimal("2.20"), now, r_value=Decimal("0.50"))
    assert decision is not None
    assert decision.reason is ExitReason.MIDDAY_REDUCE
    assert decision.quantity == 2  # (4+1)//2


def test_no_exit_when_healthy():
    settings = Settings(trading_mode="replay")
    risk = RiskEngine(settings)
    now = datetime(2026, 7, 15, 15, 0, tzinfo=timezone.utc)  # 11:00 ET
    pos = Position("OPT.US", Direction.CALL, 5, Decimal("2.00"), now,
                   stop_price=Decimal("1.50"), first_target_taken=True)
    r_value = Decimal("0.50")
    # +0.8R, not stale (just opened), not at stop
    decision = risk.exit_decision(pos, Decimal("2.40"), now, r_value=r_value)
    assert decision is None
