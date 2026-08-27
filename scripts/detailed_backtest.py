"""Run backtest and display detailed daily operations."""
from datetime import date
from decimal import Decimal
from pathlib import Path

from qqq_trader.backtest import EventDrivenBacktester, load_option_frames_path
from qqq_trader.config import Settings
from qqq_trader.persistence import ParquetMarketStore
from qqq_trader.risk import ContractSelector, RiskEngine


def main():
    settings = Settings(
        _env_file=Path(__file__).parent.parent / ".env",
        strategy_mode="hybrid",
        volatility_filter_enabled=True,
    )
    
    data_dir = settings.data_dir
    bars_path = data_dir / "bars"
    
    print("Loading market data...")
    qqq_bars = ParquetMarketStore.read_bars_path(bars_path, "1m")
    vix_bars_1m = ParquetMarketStore.read_bars_path(bars_path, "1m", symbol=".VIX.US")
    vix_bars_daily = ParquetMarketStore.read_bars_path(bars_path, "day", symbol=".VIX.US")
    
    print(f"Loaded {len(qqq_bars)} QQQ bars, {len(vix_bars_1m)} VIX 1m bars")
    
    tester = EventDrivenBacktester(
        settings,
        None,
        ContractSelector(),
        RiskEngine(settings),
    )
    
    print("\nRunning backtest...")
    result = tester.run(
        qqq_bars,
        {},
        Decimal("10000"),
        vix_bars_1m,
        vix_bars_daily,
    )
    
    print("\n" + "=" * 80)
    print("BACKTEST SUMMARY")
    print("=" * 80)
    print(f"Starting Equity: ${result.starting_equity:,.2f}")
    print(f"Ending Equity:   ${result.ending_equity:,.2f}")
    print(f"Net P&L:         ${result.ending_equity - result.starting_equity:,.2f}")
    print(f"Return Rate:     {((result.ending_equity / result.starting_equity - 1) * 100):.2f}%")
    print(f"Signals:         {result.signals}")
    print(f"Trades:          {len(result.trades)}")
    if result.trades:
        wins = sum(1 for t in result.trades if t.pnl > 0)
        print(f"Win Rate:        {(wins / len(result.trades) * 100):.1f}%")
    
    print(f"\nVIX Regimes: {result.volatility_regimes}")
    if result.rejected:
        print(f"Rejected Signals: {result.rejected}")
    
    print("\n" + "=" * 80)
    print("DAILY OPERATIONS")
    print("=" * 80)
    
    # Group operations by date
    daily_ops = {}
    for signal in result.signal_records:
        dt = signal["decision_at"][:10]
        if dt not in daily_ops:
            daily_ops[dt] = {"signals": [], "trades": []}
        daily_ops[dt]["signals"].append(signal)
    
    for trade in result.trades:
        dt = str(trade.entry_at)[:10]
        if dt not in daily_ops:
            daily_ops[dt] = {"signals": [], "trades": []}
        daily_ops[dt]["trades"].append(trade)
    
    for trading_date in sorted(daily_ops.keys()):
        ops = daily_ops[trading_date]
        print(f"\n{trading_date}")
        print("-" * 80)
        
        for signal in ops["signals"]:
            status = signal["status"]
            action = signal["action"]
            direction = signal["direction"]
            symbol = signal.get("symbol", "N/A")
            price = signal.get("price", "N/A")
            reason = signal["reason"]
            
            if status == "accepted":
                print(f"  ✅ {action.upper()} {direction} {symbol} @ ${price} | {reason}")
            elif status == "rejected":
                print(f"  ❌ {action.upper()} {direction} REJECTED | {reason}")
        
        for trade in ops["trades"]:
            pnl_sign = "+" if trade.pnl >= 0 else ""
            print(f"  💰 CLOSED {trade.direction.value} {trade.symbol}")
            print(f"     Entry: ${trade.entry_price:.2f} @ {str(trade.entry_at)[11:19]}")
            print(f"     Exit:  ${trade.exit_price:.2f} @ {str(trade.exit_at)[11:19]}")
            print(f"     P&L:   {pnl_sign}${trade.pnl:.2f} | {trade.exit_reason}")
            if trade.exit_legs:
                print(f"     Legs:  {len(trade.exit_legs)} exits")


if __name__ == "__main__":
    main()
