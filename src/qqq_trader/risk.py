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
    """Shortlist near-ATM contracts and prefer a liquid Delta near ±0.45."""

    def shortlist(
        self,
        contracts: list[OptionContract] | tuple[OptionContract, ...],
        direction: Direction,
        spot: Decimal,
    ) -> list[OptionContract]:
        return sorted(
            (contract for contract in contracts if contract.right is direction),
            key=lambda item: abs(item.strike - spot),
        )[: RULES.option_candidate_count]

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
            ranked: list[tuple[Decimal, Decimal, OptionContract]] = []
            for contract in shortlist:
                quote = quotes.get(contract.symbol)
                if quote is None:
                    continue
                try:
                    delta = abs(Decimal(str(quote.extra.get("delta", ""))))
                except Exception:
                    continue
                ranked.append(
                    (abs(delta - RULES.target_delta), abs(contract.strike - spot), contract)
                )
            if ranked:
                return min(ranked, key=lambda item: (item[0], item[1]))[2]
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

    def planned_loss_per_contract(self, entry_price: Decimal) -> Decimal:
        rules = self.rules
        estimated_entry = entry_price + rules.slippage_quote
        estimated_stop = max(
            Decimal("0.01"),
            estimated_entry * (Decimal(1) - rules.option_stop_loss_pct)
            - rules.slippage_quote,
        )
        return (
            (estimated_entry - estimated_stop) * Decimal(100)
            + rules.fee_per_contract * Decimal(2)
        )

    def position_size(
        self,
        equity: Decimal,
        entry_price: Decimal,
        remaining_daily_loss: Decimal,
        size_factor: Decimal = Decimal("1"),
    ) -> int:
        rules = self.rules
        if equity <= 0 or entry_price <= 0 or remaining_daily_loss <= 0 or size_factor <= 0:
            return 0
        estimated_entry = entry_price + rules.slippage_quote
        premium_cost = estimated_entry * Decimal(100) + rules.fee_per_contract
        planned_loss = self.planned_loss_per_contract(entry_price)
        by_premium = int(
            (equity * rules.max_premium_fraction / premium_cost).to_integral_value(
                rounding=ROUND_FLOOR
            )
        )
        by_daily_risk = int(
            (remaining_daily_loss / planned_loss).to_integral_value(rounding=ROUND_FLOOR)
        )
        normal = min(by_premium, by_daily_risk, rules.max_contracts)
        return max(
            0,
            int((Decimal(normal) * size_factor).to_integral_value(rounding=ROUND_FLOOR)),
        )

    def daily_loss_breached(self, day_pnl: Decimal, opening_equity: Decimal) -> bool:
        return opening_equity > 0 and day_pnl <= -(opening_equity * self.rules.daily_loss_limit)

    def exit_decision(
        self,
        position: Position,
        executable_bid: Decimal,
        now: datetime,
        daily_loss_breached: bool = False,
        current_spot: Decimal | None = None,
    ) -> ExitDecision | None:
        rules = self.rules
        if daily_loss_breached:
            return ExitDecision(ExitReason.DAILY_LOSS, position.quantity)
        from .config import NY_TZ

        local_time = now.astimezone(NY_TZ).time().replace(tzinfo=None)
        if local_time >= rules.forced_close:
            return ExitDecision(ExitReason.FORCED_CLOSE, position.quantity)
        position.highest_bid = max(position.highest_bid or position.entry_price, executable_bid)
        if current_spot is not None and position.peak_spot is not None:
            if position.direction is Direction.CALL:
                position.peak_spot = max(position.peak_spot, current_spot)
            else:
                position.peak_spot = min(position.peak_spot, current_spot)
        option_stop = position.stop_price or (
            position.entry_price * (Decimal(1) - rules.option_stop_loss_pct)
        )
        if executable_bid <= option_stop:
            return ExitDecision(ExitReason.STOP_LOSS, position.quantity)
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
        entry_atr = position.entry_atr
        if (
            current_spot is not None
            and position.peak_spot is not None
            and entry_atr is not None
            and entry_atr > 0
        ):
            trail_distance = rules.trailing_atr_multiplier * entry_atr
            if position.direction is Direction.CALL:
                retracement = position.peak_spot - current_spot
            else:
                retracement = current_spot - position.peak_spot
            minimum_profitable_bid = (
                position.entry_price
                + rules.fee_per_contract * Decimal(2) / Decimal(100)
            )
            if retracement >= trail_distance and executable_bid > minimum_profitable_bid:
                return ExitDecision(ExitReason.TRAILING_STOP, position.quantity)
        elif position.highest_bid > position.entry_price:
            trailing_price = position.entry_price + (
                Decimal(1) - rules.trailing_giveback_pct
            ) * (position.highest_bid - position.entry_price)
            if position.first_target_taken:
                trailing_price = max(trailing_price, position.entry_price)
            minimum_profitable_bid = (
                position.entry_price
                + rules.fee_per_contract * Decimal(2) / Decimal(100)
            )
            if minimum_profitable_bid < executable_bid <= trailing_price:
                return ExitDecision(ExitReason.TRAILING_STOP, position.quantity)
        if (now - position.opened_at).total_seconds() >= rules.stale_minutes * 60:
            if executable_bid < position.entry_price:
                return ExitDecision(ExitReason.STALE_POSITION, position.quantity)
        if (
            local_time >= rules.reduce_at
            and not position.midday_reduced
            and not (position.strategy_name or "").startswith("timed_")
        ):
            position.midday_reduced = True
            if position.quantity > 1:
                return ExitDecision(
                    ExitReason.MIDDAY_REDUCE,
                    (position.quantity + 1) // 2,
                    position.entry_price,
                )
        return None
