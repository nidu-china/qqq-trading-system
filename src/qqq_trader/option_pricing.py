"""Causal 0DTE option pricing helpers used by replay fallback quotes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from math import erf, exp, log, pi, sqrt
from statistics import pstdev

from .config import NY_TZ
from .domain import Bar, Direction

ZERO = Decimal(0)
ONE_CENT = Decimal("0.01")


@dataclass(frozen=True, slots=True)
class OptionValuation:
    price: Decimal
    delta: Decimal
    gamma: Decimal
    theta: Decimal
    vega: Decimal


def remaining_session_years(
    timestamp: datetime,
    minutes_per_year: Decimal,
) -> Decimal:
    """Return remaining regular-session time using the model's IV day count."""
    local = timestamp.astimezone(NY_TZ)
    expiry = local.replace(hour=16, minute=0, second=0, microsecond=0)
    seconds = max(0.0, (expiry - local).total_seconds())
    return Decimal(str(seconds / 60)) / minutes_per_year


def black_scholes_0dte(
    spot: Decimal,
    strike: Decimal,
    timestamp: datetime,
    implied_volatility: Decimal,
    direction: Direction,
    *,
    risk_free_rate: Decimal,
    dividend_yield: Decimal,
    minutes_per_year: Decimal,
) -> OptionValuation:
    """Price a same-day option and return Greeks from the current state."""
    intrinsic = (
        max(ZERO, spot - strike)
        if direction is Direction.CALL
        else max(ZERO, strike - spot)
    )
    years = remaining_session_years(timestamp, minutes_per_year)
    if years <= 0 or implied_volatility <= 0 or spot <= 0 or strike <= 0:
        if direction is Direction.CALL:
            delta = Decimal(1) if spot > strike else ZERO
        else:
            delta = Decimal(-1) if spot < strike else ZERO
        return OptionValuation(intrinsic, delta, ZERO, ZERO, ZERO)

    s = float(spot)
    k = float(strike)
    t = float(years)
    sigma = float(implied_volatility)
    rate = float(risk_free_rate)
    dividend = float(dividend_yield)
    root_t = sqrt(t)
    sigma_root_t = sigma * root_t
    d1 = (log(s / k) + (rate - dividend + 0.5 * sigma * sigma) * t) / sigma_root_t
    d2 = d1 - sigma_root_t
    normal_d1 = 0.5 * (1 + erf(d1 / sqrt(2)))
    normal_d2 = 0.5 * (1 + erf(d2 / sqrt(2)))
    density_d1 = exp(-0.5 * d1 * d1) / sqrt(2 * pi)
    discounted_spot = s * exp(-dividend * t)
    discounted_strike = k * exp(-rate * t)

    if direction is Direction.CALL:
        price = discounted_spot * normal_d1 - discounted_strike * normal_d2
        delta = exp(-dividend * t) * normal_d1
    else:
        price = discounted_strike * (1 - normal_d2) - discounted_spot * (1 - normal_d1)
        delta = exp(-dividend * t) * (normal_d1 - 1)
    gamma = exp(-dividend * t) * density_d1 / (s * sigma_root_t)

    one_session = Decimal(390) / minutes_per_year
    later_years = max(ZERO, years - one_session)
    later_price = _black_scholes_price(
        spot,
        strike,
        later_years,
        implied_volatility,
        direction,
        risk_free_rate,
        dividend_yield,
    )
    bumped_price = _black_scholes_price(
        spot,
        strike,
        years,
        implied_volatility + Decimal("0.01"),
        direction,
        risk_free_rate,
        dividend_yield,
    )
    return OptionValuation(
        max(intrinsic, Decimal(str(price))),
        Decimal(str(delta)),
        Decimal(str(gamma)),
        later_price - Decimal(str(price)),
        bumped_price - Decimal(str(price)),
    )


def _black_scholes_price(
    spot: Decimal,
    strike: Decimal,
    years: Decimal,
    implied_volatility: Decimal,
    direction: Direction,
    risk_free_rate: Decimal,
    dividend_yield: Decimal,
) -> Decimal:
    intrinsic = (
        max(ZERO, spot - strike)
        if direction is Direction.CALL
        else max(ZERO, strike - spot)
    )
    if years <= 0 or implied_volatility <= 0:
        return intrinsic
    s = float(spot)
    k = float(strike)
    t = float(years)
    sigma = float(implied_volatility)
    rate = float(risk_free_rate)
    dividend = float(dividend_yield)
    root_t = sqrt(t)
    d1 = (
        log(s / k) + (rate - dividend + 0.5 * sigma * sigma) * t
    ) / (sigma * root_t)
    d2 = d1 - sigma * root_t
    normal_d1 = 0.5 * (1 + erf(d1 / sqrt(2)))
    normal_d2 = 0.5 * (1 + erf(d2 / sqrt(2)))
    discounted_spot = s * exp(-dividend * t)
    discounted_strike = k * exp(-rate * t)
    if direction is Direction.CALL:
        value = discounted_spot * normal_d1 - discounted_strike * normal_d2
    else:
        value = discounted_strike * (1 - normal_d2) - discounted_spot * (1 - normal_d1)
    return max(intrinsic, Decimal(str(value)))


def implied_volatility_from_mid(
    spot: Decimal,
    strike: Decimal,
    timestamp: datetime,
    option_mid: Decimal,
    direction: Direction,
    *,
    risk_free_rate: Decimal,
    dividend_yield: Decimal,
    minutes_per_year: Decimal,
    floor: Decimal,
    cap: Decimal,
) -> Decimal | None:
    """Infer IV by bisection; return None for inconsistent or expired quotes."""
    years = remaining_session_years(timestamp, minutes_per_year)
    intrinsic = (
        max(ZERO, spot - strike)
        if direction is Direction.CALL
        else max(ZERO, strike - spot)
    )
    if years <= 0 or option_mid <= intrinsic:
        return None
    floor_price = _black_scholes_price(
        spot,
        strike,
        years,
        floor,
        direction,
        risk_free_rate,
        dividend_yield,
    )
    cap_price = _black_scholes_price(
        spot,
        strike,
        years,
        cap,
        direction,
        risk_free_rate,
        dividend_yield,
    )
    if option_mid < floor_price or option_mid > cap_price:
        return None
    low = floor
    high = cap
    for _ in range(60):
        middle = (low + high) / Decimal(2)
        price = _black_scholes_price(
            spot,
            strike,
            years,
            middle,
            direction,
            risk_free_rate,
            dividend_yield,
        )
        if price < option_mid:
            low = middle
        else:
            high = middle
    return (low + high) / Decimal(2)


def historical_daily_volatility(
    bars: list[Bar],
    timestamp: datetime,
    lookback: int = 20,
) -> Decimal | None:
    """Annualized close-to-close volatility using only prior sessions."""
    decision_date = timestamp.astimezone(NY_TZ).date()
    closes: dict[object, tuple[datetime, Decimal]] = {}
    for bar in bars:
        if not bar.complete or bar.end > timestamp:
            continue
        session_date = bar.end.astimezone(NY_TZ).date()
        if session_date >= decision_date:
            continue
        previous = closes.get(session_date)
        if previous is None or bar.end > previous[0]:
            closes[session_date] = (bar.end, bar.close)
    values = [closes[key][1] for key in sorted(closes)][-(lookback + 1) :]
    if len(values) < 6:
        return None
    returns = [
        log(float(current / previous))
        for previous, current in zip(values, values[1:], strict=False)
    ]
    return Decimal(str(pstdev(returns) * sqrt(252)))


def latest_index_volatility(
    bars: list[Bar],
    timestamp: datetime,
    max_age_minutes: int = 15,
) -> Decimal | None:
    """Return the latest causal VIX-style index value as a decimal volatility."""
    visible = [bar for bar in bars if bar.complete and bar.end <= timestamp]
    if not visible:
        return None
    latest = max(visible, key=lambda bar: bar.end)
    age_minutes = (timestamp - latest.end).total_seconds() / 60
    if age_minutes > max_age_minutes or latest.close <= 0:
        return None
    return latest.close / Decimal(100)


def quoted_bid_ask(
    theoretical_price: Decimal,
    *,
    minimum_price: Decimal,
    minimum_spread: Decimal,
    maximum_spread: Decimal,
    spread_ratio: Decimal,
) -> tuple[Decimal, Decimal, Decimal]:
    """Convert a theoretical value to cent-tick Bid/Ask quotes."""
    theoretical = max(minimum_price, theoretical_price)
    desired_spread = min(
        maximum_spread,
        max(minimum_spread, theoretical * spread_ratio),
    )
    spread_ticks = max(
        1,
        int((desired_spread / ONE_CENT).to_integral_value(rounding=ROUND_CEILING)),
    )
    spread = ONE_CENT * spread_ticks
    raw_bid = theoretical - spread / Decimal(2)
    bid = max(
        minimum_price,
        (raw_bid / ONE_CENT).to_integral_value(rounding=ROUND_FLOOR) * ONE_CENT,
    )
    ask = bid + spread
    return theoretical, bid, ask
