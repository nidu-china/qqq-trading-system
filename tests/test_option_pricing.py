from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from qqq_trader.backtest import EventDrivenBacktester, OptionFrame, SyntheticOption
from qqq_trader.domain import Bar, Direction, OptionContract, Quote
from qqq_trader.option_pricing import (
    black_scholes_0dte,
    historical_daily_volatility,
    implied_volatility_from_mid,
    latest_index_volatility,
    quoted_bid_ask,
)
from qqq_trader.policy import RULES

NY = ZoneInfo("America/New_York")


def _valuation(
    timestamp: datetime,
    direction: Direction = Direction.CALL,
    spot: Decimal = Decimal("675"),
    strike: Decimal = Decimal("675"),
    iv: Decimal = Decimal("0.25"),
):
    return black_scholes_0dte(
        spot,
        strike,
        timestamp,
        iv,
        direction,
        risk_free_rate=RULES.synthetic_risk_free_rate,
        dividend_yield=RULES.synthetic_dividend_yield,
        minutes_per_year=RULES.synthetic_minutes_per_year,
    )


def test_0dte_price_decays_and_greeks_have_expected_signs():
    morning = datetime(2026, 7, 28, 10, 0, tzinfo=NY)
    afternoon = morning.replace(hour=14)

    call_morning = _valuation(morning)
    call_afternoon = _valuation(afternoon)
    put_morning = _valuation(morning, Direction.PUT)

    assert call_morning.price > call_afternoon.price
    assert call_morning.delta > 0
    assert put_morning.delta < 0
    assert call_morning.gamma > 0
    assert call_morning.theta < 0
    assert call_morning.vega > 0


def test_implied_volatility_round_trip():
    timestamp = datetime(2026, 7, 28, 10, 30, tzinfo=NY)
    expected = Decimal("0.32")
    price = _valuation(timestamp, iv=expected).price

    actual = implied_volatility_from_mid(
        Decimal("675"),
        Decimal("675"),
        timestamp,
        price,
        Direction.CALL,
        risk_free_rate=RULES.synthetic_risk_free_rate,
        dividend_yield=RULES.synthetic_dividend_yield,
        minutes_per_year=RULES.synthetic_minutes_per_year,
        floor=RULES.synthetic_iv_floor,
        cap=RULES.synthetic_iv_cap,
    )

    assert actual is not None
    assert abs(actual - expected) < Decimal("0.000001")


def test_synthetic_repricing_is_path_independent():
    contract = OptionContract(
        "QQQ260728C00675000.US",
        "QQQ.US",
        date(2026, 7, 28),
        Decimal("675"),
        Direction.CALL,
    )
    target = datetime(2026, 7, 28, 10, 30, tzinfo=NY)
    direct = SyntheticOption(contract, Decimal("0.25"), "test")
    travelled = SyntheticOption(contract, Decimal("0.25"), "test")

    travelled.quote(Decimal("670"), target - timedelta(minutes=10))
    direct_quote = direct.quote(Decimal("675"), target)
    travelled_quote = travelled.quote(Decimal("675"), target)

    assert direct_quote == travelled_quote


def test_iv_sources_ignore_future_market_data():
    timestamp = datetime(2026, 7, 28, 10, 0, tzinfo=NY)
    contract = OptionContract(
        "QQQ260728C00675000.US",
        "QQQ.US",
        date(2026, 7, 28),
        Decimal("675"),
        Direction.CALL,
    )

    def frame(at: datetime, iv: str) -> OptionFrame:
        quote = Quote(
            contract.symbol,
            at,
            Decimal("2"),
            extra={"iv": iv},
        )
        return OptionFrame(at, Decimal("675"), (contract,), {contract.symbol: quote})

    frames = {
        timestamp - timedelta(minutes=1): frame(
            timestamp - timedelta(minutes=1), "0.24"
        ),
        timestamp + timedelta(minutes=1): frame(
            timestamp + timedelta(minutes=1), "0.80"
        ),
    }
    not_yet_visible = frame(timestamp + timedelta(seconds=1), "0.70")
    frames[timestamp] = OptionFrame(
        timestamp,
        not_yet_visible.spot,
        not_yet_visible.contracts,
        not_yet_visible.quotes,
    )
    assert (
        EventDrivenBacktester._observed_option_iv(
            frames, timestamp, Direction.CALL
        )
        == Decimal("0.24")
    )

    past_vix = Bar(
        "VIX.US",
        timestamp - timedelta(minutes=6),
        timestamp - timedelta(minutes=1),
        Decimal("20"),
        Decimal("20"),
        Decimal("20"),
        Decimal("20"),
        1,
    )
    future_vix = Bar(
        "VIX.US",
        timestamp,
        timestamp + timedelta(minutes=5),
        Decimal("80"),
        Decimal("80"),
        Decimal("80"),
        Decimal("80"),
        1,
    )
    assert latest_index_volatility([past_vix, future_vix], timestamp) == Decimal(
        "0.2"
    )


def test_historical_volatility_excludes_decision_day():
    decision = datetime(2026, 7, 28, 10, 0, tzinfo=NY)
    bars = []
    for index, close in enumerate(("100", "101", "99", "102", "100", "103")):
        end = decision - timedelta(days=6 - index)
        bars.append(
            Bar(
                "QQQ.US",
                end - timedelta(minutes=1),
                end,
                Decimal(close),
                Decimal(close),
                Decimal(close),
                Decimal(close),
                1,
            )
        )
    baseline = historical_daily_volatility(bars, decision)
    same_day = Bar(
        "QQQ.US",
        decision - timedelta(minutes=1),
        decision,
        Decimal("200"),
        Decimal("200"),
        Decimal("200"),
        Decimal("200"),
        1,
    )
    assert baseline is not None
    assert historical_daily_volatility([*bars, same_day], decision) == baseline


def test_synthetic_bid_ask_uses_cent_ticks():
    theoretical, bid, ask = quoted_bid_ask(
        Decimal("2.347"),
        minimum_price=Decimal("0.01"),
        minimum_spread=Decimal("0.01"),
        maximum_spread=RULES.synthetic_max_spread,
        spread_ratio=RULES.synthetic_spread_ratio,
    )

    assert theoretical == Decimal("2.347")
    assert bid == Decimal("2.33")
    assert ask == Decimal("2.35")
    assert bid.as_tuple().exponent == -2
    assert ask.as_tuple().exponent == -2
