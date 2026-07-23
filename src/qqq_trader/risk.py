"""Risk engine: R-based position sizing and exit management.

1R = account equity * risk_per_trade (default 0.25%)
Position size = 1R / stop_distance_per_share
Stop: swing point + 0.1 * ATR
Take profit: +1R reduce half, +2R/2.5R hard target
Stale: exit if < +0.5R after 20 minutes
Daily: stop trading after -2R cumulative
"""
from __future__ import annotations

from datetime import datetime, time
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
    Signal,
)


class ContractSelector:
    def __init__(self, strike_offset: Decimal = Decimal("2")) -> None:
        self.strike_offset = strike_offset

    def select(
        self,
        contracts: "list[OptionContract] | tuple[OptionContract, ...]",
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
    """R-based risk management engine."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def quote_problem(self, quote: Quote, now: datetime) -> str | None:
        """Check if an option quote is usable."""
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

    def compute_r_value(self, equity: Decimal) -> Decimal:
        """Calculate 1R dollar amount from account equity."""
        return equity * self.settings.risk_per_trade

    def position_size(
        self,
        equity: Decimal,
        entry_price: Decimal,
        stop_distance_per_contract: Decimal,
    ) -> int:
        """Calculate position size based on R-risk.

        stop_distance_per_contract: dollar risk per contract (option price move * 100)
        """
        if equity <= 0 or entry_price <= 0 or stop_distance_per_contract <= 0:
            return 0
        one_r = self.compute_r_value(equity)
        by_risk = int((one_r / stop_distance_per_contract).to_integral_value(rounding=ROUND_FLOOR))
        premium_budget = equity * self.settings.max_premium_fraction
        by_premium = int(
            (premium_budget / (entry_price * Decimal(100))).to_integral_value(rounding=ROUND_FLOOR)
        )
        return max(1, min(by_risk, by_premium, self.settings.max_contracts))

    def validate_stop(self, signal: Signal) -> str | None:
        """Reject if stop distance exceeds max_stop_atr_ratio * ATR."""
        if signal.stop_price is None or signal.atr is None or signal.atr <= 0:
            return None
        if signal.r_value is None:
            return None
        max_allowed = signal.atr * self.settings.max_stop_atr_ratio
        if signal.r_value > max_allowed:
            return "stop_too_wide"
        return None

    def daily_loss_breached(self, cumulative_r_loss: Decimal) -> bool:
        """Check if daily R-loss limit is breached."""
        return cumulative_r_loss >= self.settings.daily_loss_limit_r

    def exit_decision(
        self,
        position: Position,
        executable_bid: Decimal,
        now: datetime,
        daily_loss_breached: bool = False,
        r_value: Decimal | None = None,
    ) -> ExitDecision | None:
        """Determine if position should be exited.

        r_value: the 1R distance in option price terms for this position.
        """
        if daily_loss_breached:
            return ExitDecision(ExitReason.DAILY_LOSS, position.quantity)

        from .config import NY_TZ
        local_time = now.astimezone(NY_TZ).time().replace(tzinfo=None)

        # Forced close
        if local_time >= self.settings.forced_close:
            return ExitDecision(ExitReason.FORCED_CLOSE, position.quantity)

        # Midday reduction at reduce_at time
        reduce_at = self.settings.reduce_at
        if (
            local_time >= reduce_at
            and not position.first_target_taken
            and position.quantity > 1
        ):
            quantity = (position.quantity + 1) // 2
            return ExitDecision(ExitReason.MIDDAY_REDUCE, quantity, position.entry_price)

        # Stop loss
        stop = position.stop_price or (position.entry_price * Decimal("0.5"))
        if executable_bid <= stop:
            return ExitDecision(ExitReason.STOP_LOSS, position.quantity)

        # R-based take profits
        if r_value and r_value > 0:
            pnl = executable_bid - position.entry_price
            r_multiple = pnl / r_value

            # +2R or +2.5R: full exit
            if r_multiple >= self.settings.tp2_r:
                return ExitDecision(ExitReason.TAKE_PROFIT_2, position.quantity)

            # +1R: reduce half
            if not position.first_target_taken and r_multiple >= self.settings.tp1_r:
                if position.quantity == 1:
                    return ExitDecision(ExitReason.TAKE_PROFIT_1, 1)
                quantity = (position.quantity + 1) // 2
                return ExitDecision(
                    ExitReason.TAKE_PROFIT_1, quantity, position.entry_price
                )

        # Stale position: losing money after stale_minutes
        if r_value and r_value > 0:
            elapsed_seconds = (now - position.opened_at).total_seconds()
            if elapsed_seconds >= self.settings.stale_minutes * 60:
                pnl = executable_bid - position.entry_price
                if pnl < Decimal(0):
                    return ExitDecision(ExitReason.STALE_POSITION, position.quantity)

        return None
