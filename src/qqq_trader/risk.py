"""Contract selection, position sizing and exit management."""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_FLOOR, Decimal

from .config import Settings
from .domain import (
    Direction,
    ExitDecision,
    ExitReason,
    OptionContract,
    Position,
    Quote,
)
from .policy import RULES


class ContractSelector:
    """Select 0DTE contracts within spot ± strike_offset."""

    def shortlist(
        self,
        contracts: list[OptionContract] | tuple[OptionContract, ...],
        direction: Direction,
        spot: Decimal,
    ) -> list[OptionContract]:
        offset = RULES.strike_offset
        lower = spot - offset
        upper = spot + offset
        candidates = [
            c for c in contracts
            if c.right is direction and lower <= c.strike <= upper
        ]
        return sorted(candidates, key=lambda c: abs(c.strike - spot))[
            : RULES.option_candidate_count
        ]

    def select(
        self,
        contracts: list[OptionContract] | tuple[OptionContract, ...],
        direction: Direction,
        spot: Decimal,
        quotes: dict[str, Quote] | None = None,
    ) -> OptionContract | None:
        shortlist = self.shortlist(contracts, direction, spot)
        if not shortlist:
            return None
        if quotes:
            priced: list[tuple[Decimal, Decimal, OptionContract]] = []
            for contract in shortlist:
                quote = quotes.get(contract.symbol)
                if quote is None or quote.ask is None or quote.ask <= 0:
                    continue
                priced.append(
                    (quote.ask, abs(contract.strike - spot), contract)
                )
            if priced:
                return min(priced, key=lambda item: (item[0], item[1]))[2]
        return min(shortlist, key=lambda item: abs(item.strike - spot))


class RiskEngine:
    """Fixed STRATEGY.md risk rules shared by live, paper and replay."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.rules = settings.rules

    def quote_problem(self, quote: Quote, now: datetime) -> str | None:
        rules = self.rules
        age = Decimal(str((now - quote.timestamp).total_seconds()))
        if age < 0 or age > rules.max_quote_age_seconds:
            return "stale_quote"
        if quote.bid is None or quote.ask is None or quote.bid <= 0 or quote.ask <= 0:
            return "missing_bid_ask"
        if quote.ask < quote.bid:
            return "crossed_market"
        mid = quote.mid
        spread = quote.spread
        assert mid is not None and spread is not None
        if spread > rules.max_spread_absolute:
            return "absolute_spread_too_wide"
        if mid <= 0 or spread / mid > rules.max_spread_ratio:
            return "relative_spread_too_wide"
        if quote.open_interest < rules.min_open_interest:
            return "insufficient_open_interest"
        if quote.volume < rules.min_option_volume:
            return "insufficient_volume"
        return None

    def position_size(
        self,
        equity: Decimal,
        entry_price: Decimal,
        size_factor: Decimal = Decimal("1"),
    ) -> int:
        rules = self.rules
        if equity <= 0 or entry_price <= 0 or size_factor <= 0:
            return 0
        estimated_entry = entry_price + rules.slippage_quote
        premium_cost = estimated_entry * Decimal(100) + rules.fee_per_contract
        by_premium = int(
            (equity * rules.max_premium_fraction / premium_cost).to_integral_value(
                rounding=ROUND_FLOOR
            )
        )
        normal = min(by_premium, rules.max_contracts)
        return max(
            0,
            int((Decimal(normal) * size_factor).to_integral_value(rounding=ROUND_FLOOR)),
        )

    def exit_decision(
        self,
        position: Position,
        executable_bid: Decimal,
        now: datetime,
        *,
        allow_stop_loss: bool = True,
    ) -> ExitDecision | None:
        rules = self.rules
        from .config import NY_TZ

        local_time = now.astimezone(NY_TZ).time().replace(tzinfo=None)
        if local_time >= rules.forced_close:
            return ExitDecision(ExitReason.FORCED_CLOSE, position.quantity)
        position.highest_bid = max(position.highest_bid or position.entry_price, executable_bid)
        option_stop = position.stop_price or (
            position.entry_price * (Decimal(1) - rules.option_stop_loss_pct)
        )
        if allow_stop_loss and executable_bid <= option_stop:
            return ExitDecision(ExitReason.STOP_LOSS, position.quantity)
        if position.trend_runner:
            return None
        profit_pct = (executable_bid - position.entry_price) / position.entry_price
        if profit_pct >= rules.tp2_profit_pct:
            return ExitDecision(ExitReason.TAKE_PROFIT_2, position.quantity)
        if not position.first_target_taken and profit_pct >= rules.tp1_profit_pct:
            quantity = (
                position.quantity
                if position.quantity == 1
                else (position.quantity + 1) // 2
            )
            return ExitDecision(ExitReason.TAKE_PROFIT_1, quantity, position.entry_price)
        maximum_profit_pct = (
            position.highest_bid - position.entry_price
        ) / position.entry_price
        if maximum_profit_pct >= rules.trailing_activation_profit_pct:
            trailing_price = position.entry_price + (
                Decimal(1) - rules.trailing_giveback_pct
            ) * (position.highest_bid - position.entry_price)
            if executable_bid <= trailing_price:
                if position.quantity == 1:
                    position.trend_runner = True
                    return None
                return ExitDecision(
                    ExitReason.TRAILING_STOP,
                    (position.quantity + 1) // 2,
                )
        if (now - position.opened_at).total_seconds() >= rules.stale_minutes * 60:
            if executable_bid < position.entry_price:
                return ExitDecision(ExitReason.STALE_POSITION, position.quantity)
        return None
