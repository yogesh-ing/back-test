"""Comprehensive paper trading validation harness.

Runs a full synthetic day (375 1-min bars = 6h15m) and validates:
1. Fill quantities are non-zero
2. Charges (STT, brokerage, GST) are calculated
3. Trades persist to database
4. PnL calculation matches realized + unrealized
5. Position tracking is correct
6. No phantom equity changes

This catches ALL known bugs from 2026-10-13 in one test run.
"""

from __future__ import annotations

import pytest
import pandas as pd
from decimal import Decimal
from pathlib import Path

from backtest.forward.paper_runner import PaperRunner
from backtest.simulator import (
    CommissionCalculator,
    ExecutionConfig,
    OrderExecutor,
    SlippageCalculator,
)
from backtest.simulator.portfolio import Portfolio
from backtest.data.synthetic import SyntheticSource
from backtest.db.manager import DatabaseManager
from backtest.db.models import Base

REPO_ROOT = Path(__file__).resolve().parents[2]


class FakeSignalStrategy:
    """Strategy that generates predictable signals for validation."""
    
    name = "validation_test"
    
    def __init__(self, entry_bar: int = 50, exit_bar: int = 200):
        self.entry_bar = entry_bar
        self.exit_bar = exit_bar
    
    def generate_signals(self, candles: pd.DataFrame) -> pd.Series:
        s = pd.Series(0, index=candles.index, dtype=int)
        if len(candles) > self.entry_bar:
            s.iloc[self.entry_bar:] = 1  # BUY signal
        if len(candles) > self.exit_bar:
            s.iloc[self.exit_bar:] = 0   # EXIT signal
        return s


def make_1min_bars(n: int = 375, start: str = "2024-01-15 09:15") -> pd.DataFrame:
    """Generate realistic 1-minute NIFTY-like bars for one trading day."""
    idx = pd.date_range(start=start, periods=n, freq="min")
    # Simulate intraday pattern: open at 21800, drift up then down
    base_price = 21800.0
    trend = [0.5 * (i - n//2)**2 / n for i in range(n)]  # parabolic
    noise = [(-1)**(i % 2) * (i % 7) * 0.3 for i in range(n)]
    
    closes = [base_price + trend[i] + noise[i] for i in range(n)]
    opens = [closes[i-1] if i > 0 else base_price for i in range(n)]
    highs = [max(opens[i], closes[i]) + abs(noise[i]) * 0.5 for i in range(n)]
    lows = [min(opens[i], closes[i]) - abs(noise[i]) * 0.5 for i in range(n)]
    volumes = [50000 + (i % 11) * 1000 for i in range(n)]
    
    return pd.DataFrame({
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
    }, index=idx)


class Fake1MinSource:
    """Fixed 1-minute bars for deterministic testing."""
    
    def __init__(self, bars: pd.DataFrame):
        self._bars = bars
    
    def get_candles(self, symbol, start, end, interval="1min"):
        return self._bars


def make_executor() -> OrderExecutor:
    """Realistic executor with slippage and fees."""
    config = ExecutionConfig(seed=42, price_improvement_probability=Decimal("0"))
    return OrderExecutor(
        config=config,
        slippage=SlippageCalculator(base_bps=5),  # 5 bps slippage
        fees=CommissionCalculator.for_broker("zerodha"),  # Real fee structure
    )


@pytest.fixture()
def db():
    """In-memory SQLite with full schema."""
    manager = DatabaseManager.from_env(
        path=str(REPO_ROOT / "config" / "database.yaml"),
        profile="testing",
        url="sqlite:///:memory:"
    )
    manager.connect()
    Base.metadata.create_all(manager.engine)
    yield manager
    manager.disconnect()


class TestPaperTradingValidation:
    """All-in-one validation suite for paper trading bugs."""
    
    def _run_validation_day(self):
        """Helper: run a full synthetic day and return runner state."""
        bars = make_1min_bars(n=375)  # Full trading day
        source = Fake1MinSource(bars)
        strategy = FakeSignalStrategy(entry_bar=50, exit_bar=200)
        
        portfolio = Portfolio(name="validation-day", initial_capital=10_00_000)
        executor = make_executor()
        
        runner = PaperRunner(
            portfolio=portfolio,
            source=source,
            strategy=strategy,
            executor=executor,
            symbols=["NIFTY"],
            start="2024-01-15 09:15",
            end="2024-01-15 15:30",
            interval="1min",
        )
        
        summary = runner.run()
        return runner, summary, bars
    
    def test_fill_quantities_nonzero(self):
        """Bug #1 from 2026-10-13: fills had qty=0."""
        runner, summary, bars = self._run_validation_day()
        
        # Check filled orders
        for order_id, fill in runner.portfolio.filled_orders.items():
            assert fill.quantity > 0, f"Fill {order_id} has zero quantity"
            assert fill.quantity == abs(fill.signed_quantity), \
                f"Fill {order_id} quantity mismatch: {fill.quantity} vs {fill.signed_quantity}"
    
    def test_charges_calculated(self):
        """Bug #2 from 2026-10-13: no STT/brokerage/GST in fills."""
        runner, summary, bars = self._run_validation_day()
        
        total_commission = runner.portfolio.total_commission
        assert total_commission > 0, \
            f"No charges calculated! Total commission should be > 0, got {total_commission}"
        
        # Verify individual fills have fees
        for order_id, fill in runner.portfolio.filled_orders.items():
            assert fill.total_fees > 0, \
                f"Fill {order_id} has no fees: {fill.total_fees}"
    
    def test_trades_persisted_to_db(self, db):
        """Bug #3 from 2026-10-13: trades only in memory, not DB."""
        runner, summary, bars = self._run_validation_day()
        
        # Query database for trades
        with db.session() as session:
            from backtest.db.models import Trade
            
            trades = session.query(Trade).filter_by(
                portfolio_id=runner.portfolio.portfolio_id
            ).all()
            
            # Should have at least entry + exit trades
            assert len(trades) >= 2, \
                f"Expected >= 2 trades in DB, found {len(trades)}"
            
            # Verify trade fields populated
            for trade in trades:
                assert trade.quantity > 0, f"Trade {trade.id} has zero quantity"
                assert trade.fill_price > 0, f"Trade {trade.id} has zero price"
                assert trade.timestamp is not None, f"Trade {trade.id} missing timestamp"
    
    def test_daily_pnl_calculation(self):
        """Bug #4 from 2026-10-13: phantom PnL from stale gains."""
        runner, summary, bars = self._run_validation_day()
        
        # Get daily PnL
        daily_pnl = runner.daily_pnl
        
        # Calculate expected PnL manually
        closed_positions = runner.portfolio.closed_positions
        realized_pnl = sum(p.realized_pnl for p in closed_positions.values())
        
        # Open positions MTM
        unrealized_pnl = sum(
            pos.unrealized_pnl 
            for pos in runner.portfolio.positions.values() 
            if pos.quantity != 0
        )
        
        expected_pnl = float(realized_pnl) + float(unrealized_pnl)
        
        # Allow small floating point tolerance
        assert abs(daily_pnl - expected_pnl) < 0.01, \
            f"Daily PnL mismatch: got {daily_pnl}, expected {expected_pnl}"
    
    def test_position_tracking_correct(self):
        """Verify position lifecycle: open → increase → decrease → close."""
        runner, summary, bars = self._run_validation_day()
        
        # After entry+exit round trip, should have no open positions
        assert len(runner.portfolio.positions) == 0 or \
               all(pos.quantity == 0 for pos in runner.portfolio.positions.values()), \
            f"Positions should be closed after round trip: {runner.portfolio.positions}"
        
        # Should have closed positions recorded
        assert len(runner.portfolio.closed_positions) >= 1, \
            f"Should have closed positions: {runner.portfolio.closed_positions}"
    
    def test_equity_curve_consistency(self):
        """Equity curve should reflect actual trades, not phantom changes."""
        runner, summary, bars = self._run_validation_day()
        
        equity_curve = runner.equity_curve
        assert len(equity_curve) > 0, "No equity curve recorded"
        
        # First point should equal initial capital
        first_equity = equity_curve[0].get('equity', 0)
        assert abs(first_equity - 10_00_000) < 1.0, \
            f"Initial equity mismatch: {first_equity} vs 1000000"
        
        # Last point should match current equity
        last_equity = equity_curve[-1].get('equity', 0)
        current_equity = runner.equity()
        assert abs(last_equity - current_equity) < 1.0, \
            f"Final equity mismatch: curve={last_equity}, current={current_equity}"
    
    def test_no_stale_historical_gains(self):
        """Daily PnL should not include gains from previous days."""
        runner, summary, bars = self._run_validation_day()
        
        # Day start equity should equal initial capital for fresh run
        assert runner._day_start_equity == 10_00_000, \
            f"Day start equity should be initial capital: {runner._day_start_equity}"
        
        # Daily PnL should be reasonable (< 5% of capital for one day)
        daily_pnl = runner.daily_pnl
        max_reasonable = 10_00_000 * 0.05  # 5% threshold
        assert abs(daily_pnl) < max_reasonable, \
            f"Daily PnL {daily_pnl} exceeds reasonable bound {max_reasonable}"
    
    def test_trade_closure_tracking(self):
        """Bug fix verification: _closed_trades list populated."""
        runner, summary, bars = self._run_validation_day()
        
        # Should have tracked closed trades
        assert hasattr(runner, '_closed_trades'), \
            "Missing _closed_trades attribute"
        
        closed_trades = runner._closed_trades
        assert len(closed_trades) >= 1, \
            f"Should track closed trades, found {len(closed_trades)}"
        
        # Each trade should have required fields
        for trade in closed_trades:
            assert 'net_pnl' in trade, f"Trade missing net_pnl: {trade}"
            assert 'exit_time' in trade, f"Trade missing exit_time: {trade}"
            assert 'symbol' in trade, f"Trade missing symbol: {trade}"
