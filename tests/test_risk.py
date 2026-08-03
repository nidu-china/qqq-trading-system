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


def test_contract_selector_uses_strike_offset_and_cheapest():
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
    shortlist = selector.shortlist(contracts, Direction.CALL, Decimal("500"))
    assert {c.symbol for c in shortlist} == {"C498", "C499", "C500", "C501", "C502"}
    quotes = {item.symbol: _quote(item.symbol) for item in shortlist}
    cheap = Quote("C502", NOW, Decimal("0.50"), Decimal("0.45"), Decimal("0.55"), 100, 500)
    quotes["C502"] = cheap
    chosen = selector.select(contracts, Direction.CALL, Decimal("500"), quotes)
    assert chosen.symbol == "C502"


def test_liquidity_and_absolute_quote_slippage_rules():
    risk = RiskEngine(make_settings())
    assert risk.quote_problem(_quote("C500"), NOW) is None
    stale = _quote("C500")
    assert risk.quote_problem(stale, NOW + timedelta(seconds=3)) == "stale_quote"
    wide = Quote("C500", NOW, Decimal("1"), Decimal("0.80"), Decimal("1.20"), 100, 500)
    assert risk.quote_problem(wide, NOW) == "absolute_spread_too_wide"
def test_position_size_obeys_premium_budget_and_contract_cap():
    risk = RiskEngine(make_settings())
    assert risk.position_size(Decimal("10000"), Decimal("1")) == 10
    assert risk.position_size(Decimal("100000"), Decimal("0.10")) == 10
    assert risk.position_size(Decimal("1000"), Decimal("1")) == 4
    assert risk.position_size(
        Decimal("10000"), Decimal("1"), size_factor=Decimal("0.5")
    ) == 5


def test_option_stop_targets_trailing_stale_midday_and_forced_close():
    risk = RiskEngine(make_settings())
    assert risk.exit_decision(_position(), Decimal("0.75"), NOW).reason is ExitReason.STOP_LOSS
    timed = _position()
    timed.strategy_name = "timed_boll_macd_signal"
    assert risk.exit_decision(timed, Decimal("0.75"), NOW).reason is ExitReason.STOP_LOSS

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
    stale.opened_at = NOW - timedelta(minutes=21)
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


