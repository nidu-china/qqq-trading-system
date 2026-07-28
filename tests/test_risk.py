from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from conftest import make_settings
from qqq_trader.domain import Direction, ExitReason, OptionContract, Position, Quote
from qqq_trader.risk import ContractSelector, RiskEngine

NOW = datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc)


def _quote(symbol: str, delta: str = "0.45") -> Quote:
    return Quote(
        symbol,
        NOW,
        Decimal("1.05"),
        Decimal("1.00"),
        Decimal("1.10"),
        volume=100,
        open_interest=500,
        extra={"delta": delta},
    )


def _position(quantity: int = 4, entry: str = "1.00") -> Position:
    return Position(
        "QQQ260715C00500000.US",
        Direction.CALL,
        quantity,
        Decimal(entry),
        NOW,
    )


def test_contract_selector_uses_five_nearest_and_delta():
    contracts = [
        OptionContract(
            f"C{strike}",
            "QQQ.US",
            date(2026, 7, 15),
            Decimal(str(strike)),
            Direction.CALL,
        )
        for strike in range(497, 504)
    ]
    selector = ContractSelector()
    shortlist = selector.shortlist(contracts, Direction.CALL, Decimal("500.2"))
    assert len(shortlist) == 5
    quotes = {item.symbol: _quote(item.symbol, "0.60") for item in shortlist}
    quotes["C499"] = _quote("C499", "0.46")
    assert selector.select(contracts, Direction.CALL, Decimal("500.2"), quotes).symbol == "C499"


def test_liquidity_and_absolute_quote_slippage_rules():
    risk = RiskEngine(make_settings())
    assert risk.quote_problem(_quote("C500"), NOW) is None
    stale = _quote("C500")
    assert risk.quote_problem(stale, NOW + timedelta(seconds=3)) == "stale_quote"
    wide = Quote("C500", NOW, Decimal("1"), Decimal("0.80"), Decimal("1.20"), 100, 500)
    assert risk.quote_problem(wide, NOW) == "absolute_spread_too_wide"
    # 25% option stop plus $0.02 entry/exit quote slippage and $3 round-trip fee.
    assert risk.planned_loss_per_contract(Decimal("1.00")) == Decimal("30.50")


def test_position_size_obeys_premium_daily_budget_and_contract_cap():
    risk = RiskEngine(make_settings())
    # $10k * 5% premium permits four $1.02 contracts; daily risk also permits four.
    assert risk.position_size(Decimal("10000"), Decimal("1"), Decimal("200")) == 4
    assert risk.position_size(Decimal("100000"), Decimal("0.10"), Decimal("10000")) == 10
    assert risk.position_size(Decimal("10000"), Decimal("1"), Decimal("30")) == 0


def test_option_stop_targets_trailing_stale_midday_and_forced_close():
    risk = RiskEngine(make_settings())
    assert risk.exit_decision(_position(), Decimal("0.75"), NOW).reason is ExitReason.STOP_LOSS

    tp1 = risk.exit_decision(_position(5), Decimal("2.00"), NOW)
    assert tp1 is not None and tp1.reason is ExitReason.TAKE_PROFIT_1 and tp1.quantity == 3
    assert tp1.new_stop == Decimal("1.00")

    assert risk.exit_decision(_position(), Decimal("3.50"), NOW).reason is ExitReason.TAKE_PROFIT_2

    trailing = _position()
    trailing.highest_bid = Decimal("1.40")
    assert risk.exit_decision(trailing, Decimal("1.27"), NOW).reason is ExitReason.TRAILING_STOP

    losing = _position()
    losing.highest_bid = Decimal("1.01")
    assert risk.exit_decision(losing, Decimal("0.99"), NOW) is None

    fees_would_erase_profit = _position()
    fees_would_erase_profit.highest_bid = Decimal("1.04")
    assert risk.exit_decision(fees_would_erase_profit, Decimal("1.02"), NOW) is None

    stale = _position()
    stale.opened_at = NOW - timedelta(minutes=31)
    assert risk.exit_decision(stale, Decimal("0.99"), NOW).reason is ExitReason.STALE_POSITION

    midday = _position(5)
    midday_at = datetime(2026, 7, 15, 15, 30, tzinfo=timezone.utc)  # 11:30 ET
    decision = risk.exit_decision(midday, Decimal("1.01"), midday_at)
    assert decision is not None and decision.reason is ExitReason.MIDDAY_REDUCE
    assert decision.quantity == 3

    forced_at = datetime(2026, 7, 15, 17, 55, tzinfo=timezone.utc)  # 13:55 ET
    assert (
        risk.exit_decision(_position(), Decimal("1.01"), forced_at).reason
        is ExitReason.FORCED_CLOSE
    )


def test_atr_trailing_stop_on_qqq_retracement():
    risk = RiskEngine(make_settings())
    # Call position: QQQ entered at 500, ATR = 0.50
    pos = _position()
    pos.entry_spot = Decimal("500.00")
    pos.entry_atr = Decimal("0.50")
    pos.peak_spot = Decimal("500.00")
    # QQQ rallies to 501.20 → peak_spot updated by exit_decision
    result = risk.exit_decision(pos, Decimal("1.30"), NOW, current_spot=Decimal("501.20"))
    assert result is None
    assert pos.peak_spot == Decimal("501.20")
    # QQQ retraces 0.20 (< 0.5 * ATR = 0.25) → no exit
    result = risk.exit_decision(pos, Decimal("1.20"), NOW, current_spot=Decimal("501.00"))
    assert result is None
    # QQQ retraces 0.30 (>= 0.5 * ATR = 0.25) and bid > entry+fees → trailing stop
    result = risk.exit_decision(pos, Decimal("1.10"), NOW, current_spot=Decimal("500.90"))
    assert result is not None and result.reason is ExitReason.TRAILING_STOP

    # Put position: QQQ entered at 500, ATR = 0.50
    put = Position(
        "QQQ260715P00500000.US", Direction.PUT, 4,
        Decimal("1.00"), NOW,
    )
    put.entry_spot = Decimal("500.00")
    put.entry_atr = Decimal("0.50")
    put.peak_spot = Decimal("500.00")
    put.highest_bid = Decimal("1.00")
    # QQQ drops to 498.80 → peak_spot updated
    risk.exit_decision(put, Decimal("1.30"), NOW, current_spot=Decimal("498.80"))
    assert put.peak_spot == Decimal("498.80")
    # QQQ bounces back 0.30 (>= 0.5*ATR=0.25) and still profitable → trailing stop
    result = risk.exit_decision(put, Decimal("1.10"), NOW, current_spot=Decimal("499.10"))
    assert result is not None and result.reason is ExitReason.TRAILING_STOP


def test_atr_trailing_not_triggered_when_unprofitable():
    """ATR trailing should not fire if bid doesn't cover round-trip fees."""
    risk = RiskEngine(make_settings())
    pos = _position()
    pos.entry_spot = Decimal("500.00")
    pos.entry_atr = Decimal("0.50")
    pos.peak_spot = Decimal("501.00")
    pos.highest_bid = Decimal("1.30")
    # QQQ retraces 0.60 (>= ATR) but bid barely above entry → fees eat profit
    result = risk.exit_decision(pos, Decimal("1.02"), NOW, current_spot=Decimal("500.40"))
    assert result is None


def test_daily_loss_is_fixed_at_two_percent():
    risk = RiskEngine(make_settings())
    assert not risk.daily_loss_breached(Decimal("-199.99"), Decimal("10000"))
    assert risk.daily_loss_breached(Decimal("-200"), Decimal("10000"))
