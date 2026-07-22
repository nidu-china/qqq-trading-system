from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import ROUND_FLOOR, Decimal

from .config import Settings
from .domain import (
    AccountSnapshot,
    Direction,
    ExitDecision,
    ExitReason,
    OptionContract,
    Position,
    Quote,
)


class ContractSelector:
    def __init__(self, strike_offset: Decimal = Decimal("2")) -> None:
        self.strike_offset = strike_offset

    def select(
        self,
        contracts: Sequence[OptionContract],
        direction: Direction,
        spot: Decimal,
    ) -> OptionContract | None:
        eligible = [contract for contract in contracts if contract.right is direction]
        if not eligible:
            return None
        target = (
            spot + self.strike_offset if direction is Direction.CALL else spot - self.strike_offset
        )

        def ranking(contract: OptionContract) -> tuple[Decimal, Decimal]:
            tie_break = -contract.strike if direction is Direction.CALL else contract.strike
            return abs(contract.strike - target), tie_break

        return min(eligible, key=ranking)


class RiskEngine:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def quote_problem(self, quote: Quote, now: datetime) -> str | None:
        age = Decimal(str((now - quote.timestamp).total_seconds()))
        if age < 0 or age > self.settings.max_quote_age_seconds:
            return "stale_quote"
        if quote.bid is None or quote.ask is None or quote.bid <= 0 or quote.ask <= 0:
            return "missing_bid_ask"
        if quote.ask < quote.bid:
            return "crossed_market"
        mid = quote.mid
        spread = quote.spread
        assert mid is not None and spread is not None
        if spread > self.settings.max_spread_absolute:
            return "absolute_spread_too_wide"
        if mid <= 0 or spread / mid > self.settings.max_spread_ratio:
            return "relative_spread_too_wide"
        if quote.open_interest < self.settings.min_open_interest:
            return "insufficient_open_interest"
        if quote.volume < self.settings.min_option_volume:
            return "insufficient_volume"
        return None

    def position_size(self, account: AccountSnapshot, entry_price: Decimal) -> int:
        if account.equity <= 0 or entry_price <= 0:
            return 0
        risk_budget = account.equity * self.settings.risk_per_trade
        per_contract_risk = (
            entry_price * Decimal(100) * self.settings.stop_loss_pct
            + self.settings.fee_per_contract
            + self.settings.slippage_per_contract
        )
        by_risk = int((risk_budget / per_contract_risk).to_integral_value(rounding=ROUND_FLOOR))
        premium_budget = account.equity * self.settings.max_premium_fraction
        by_premium = int(
            (premium_budget / (entry_price * Decimal(100))).to_integral_value(rounding=ROUND_FLOOR)
        )
        return max(0, min(by_risk, by_premium, self.settings.max_contracts))

    def daily_loss_breached(self, account: AccountSnapshot, opening_equity: Decimal) -> bool:
        if opening_equity <= 0:
            return True
        return account.day_pnl <= -(opening_equity * self.settings.daily_loss_limit)

    def exit_decision(
        self,
        position: Position,
        executable_bid: Decimal,
        now: datetime,
        daily_loss_breached: bool = False,
    ) -> ExitDecision | None:
        if daily_loss_breached:
            return ExitDecision(ExitReason.DAILY_LOSS, position.quantity)
        from .config import NY_TZ

        local_time = now.astimezone(NY_TZ).time().replace(tzinfo=None)
        if local_time >= self.settings.forced_close:
            return ExitDecision(ExitReason.FORCED_CLOSE, position.quantity)

        stop = position.stop_price or (
            position.entry_price * (Decimal(1) - self.settings.stop_loss_pct)
        )
        if executable_bid <= stop:
            return ExitDecision(ExitReason.STOP_LOSS, position.quantity)

        second_target = position.entry_price * (Decimal(1) + self.settings.take_profit_2_pct)
        if executable_bid >= second_target:
            return ExitDecision(ExitReason.TAKE_PROFIT_2, position.quantity)

        first_target = position.entry_price * (Decimal(1) + self.settings.take_profit_1_pct)
        if not position.first_target_taken and executable_bid >= first_target:
            if position.quantity == 1:
                return ExitDecision(ExitReason.TAKE_PROFIT_1, 1)
            quantity = (position.quantity + 1) // 2
            return ExitDecision(ExitReason.TAKE_PROFIT_1, quantity, position.entry_price)
        return None
