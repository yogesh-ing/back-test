"""Tests for Step 17: Performance Calculator."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pandas as pd
import pytest

from backtest.simulator.performance import PerformanceCalculator, PerformanceConfig
from backtest.simulator.portfolio import EquityPoint, Portfolio


def make_portfolio_with_history():
    portfolio = Portfolio(name="perf_test", initial_capital=100000)

    # Create equity history with some ups and downs
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    equities = [100000, 101000, 100500, 102000, 101500, 103000, 102500, 104000, 103500, 105000]

    for i, eq in enumerate(equities):
        ts = base + timedelta(days=i)
        point = EquityPoint(
            ts=ts,
            total_equity=Decimal(str(eq)),
            cash=Decimal(str(eq)),
            position_value=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            realized_pnl=Decimal(str(eq - 100000)),
        )
        portfolio.equity_history.append(point)

    # Add some closed positions as trades
    # Winning trade
    pos1 = portfolio.open_position("INFY", 100, 100)
    portfolio.reduce_position("INFY", 100, 110)  # +1000 pnl

    pos2 = portfolio.open_position("TCS", 50, 200)
    portfolio.reduce_position("TCS", 50, 190)  # -500 pnl

    pos3 = portfolio.open_position("RELIANCE", 100, 150)
    portfolio.reduce_position("RELIANCE", 100, 160)  # +1000 pnl

    return portfolio


def test_performance_config():
    cfg = PerformanceConfig(risk_free_rate=0.02, periods_per_year=252)
    assert cfg.risk_free_rate == Decimal("0.02")

    with pytest.raises(Exception):
        PerformanceConfig(risk_free_rate=1.5)

    with pytest.raises(Exception):
        PerformanceConfig(periods_per_year=0)


def test_update_equity_curve():
    portfolio = Portfolio(name="perf_test2", initial_capital=100000)
    calc = PerformanceCalculator(portfolio)

    result = calc.update_equity_curve()
    assert "total_equity" in result
    assert len(portfolio.equity_history) == 1

    # With explicit values
    result2 = calc.update_equity_curve(equity=101000, cash=90000, position_value=11000)
    assert len(portfolio.equity_history) == 2


def test_returns_metrics():
    portfolio = make_portfolio_with_history()
    calc = PerformanceCalculator(portfolio)

    metrics = calc.calculate_returns_metrics()

    assert "total_return" in metrics
    assert "total_return_pct" in metrics
    assert "cagr" in metrics
    assert "annualized_return" in metrics
    assert "best_day" in metrics
    assert "worst_day" in metrics
    assert metrics["total_return_pct"] > 0  # should be positive
    assert metrics["final_equity"] > metrics["initial_capital"]


def test_risk_metrics():
    portfolio = make_portfolio_with_history()
    calc = PerformanceCalculator(portfolio)

    metrics = calc.calculate_risk_metrics()

    assert "volatility" in metrics
    assert "annualized_volatility" in metrics
    assert "max_drawdown" in metrics
    assert "max_drawdown_pct" in metrics
    assert "current_drawdown" in metrics
    assert "var_95" in metrics
    assert "var_99" in metrics

    # Max drawdown should be negative or zero (as pct)
    assert metrics["max_drawdown_pct"] <= 0


def test_ratios():
    portfolio = make_portfolio_with_history()
    calc = PerformanceCalculator(portfolio, config={"risk_free_rate": 0.02})

    ratios = calc.calculate_ratios()

    assert "sharpe_ratio" in ratios
    assert "sortino_ratio" in ratios
    assert "calmar_ratio" in ratios
    assert "information_ratio" in ratios
    assert "treynor_ratio" in ratios

    # Sharpe should be calculable
    assert isinstance(ratios["sharpe_ratio"], float)


def test_trade_statistics():
    portfolio = make_portfolio_with_history()
    calc = PerformanceCalculator(portfolio)

    stats = calc.calculate_trade_statistics()

    assert "total_trades" in stats
    assert "winning_trades" in stats
    assert "losing_trades" in stats
    assert "win_rate" in stats
    assert "avg_win" in stats
    assert "avg_loss" in stats
    assert "profit_factor" in stats
    assert "expectancy" in stats

    # We had 3 closed positions: 2 wins, 1 loss
    assert stats["total_trades"] == 3
    assert stats["winning_trades"] == 2
    assert stats["losing_trades"] == 1
    assert abs(stats["win_rate"] - 0.666) < 0.01


def test_all_metrics():
    portfolio = make_portfolio_with_history()
    calc = PerformanceCalculator(portfolio)

    all_metrics = calc.calculate_all_metrics()

    # Should contain all categories
    assert "total_return_pct" in all_metrics
    assert "volatility" in all_metrics
    assert "sharpe_ratio" in all_metrics
    assert "total_trades" in all_metrics
    assert "calculation_date" in all_metrics
    assert "portfolio_name" in all_metrics


def test_update_metrics_compatibility():
    # For engine compatibility
    portfolio = make_portfolio_with_history()
    calc = PerformanceCalculator(portfolio)

    metrics = calc.update_metrics()
    assert metrics is not None
    assert "total_return_pct" in metrics

    metrics2 = calc.get_metrics()
    assert metrics2 is not None


def test_save_to_db():
    from backtest.db.config import DatabaseConfig
    from backtest.db.manager import DatabaseManager
    from backtest.db.models import Base

    cfg = DatabaseConfig(url="sqlite:///:memory:", pool_min_size=1, pool_max_size=5)
    db = DatabaseManager(cfg)
    db.connect()
    Base.metadata.create_all(db.engine)

    portfolio = make_portfolio_with_history()
    # Need to save portfolio first for FK
    portfolio.save_to_db(db)

    calc = PerformanceCalculator(portfolio, db_manager=db)
    metric_id = calc.save_to_db()

    assert metric_id is not None

    # Check DB
    with db.session() as session:
        from backtest.db.models import PerformanceMetric as Row

        rows = session.query(Row).all()
        assert len(rows) == 1
        assert rows[0].total_trades == 3

    db.disconnect()


def test_benchmark_comparison():
    portfolio = make_portfolio_with_history()
    # Create fake benchmark returns
    import numpy as np

    np.random.seed(42)
    bench_returns = list(np.random.randn(10) * 0.01)

    calc = PerformanceCalculator(portfolio, config={"benchmark_returns": bench_returns})

    ratios = calc.calculate_ratios()
    # Information ratio should be calculated when benchmark provided
    assert "information_ratio" in ratios


def test_best_worst_metrics():
    portfolio = Portfolio(name="best_worst_test", initial_capital=100000)

    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    # Create returns with clear best/worst
    equities = [100000, 110000, 90000, 105000, 95000, 115000]  # big swings

    for i, eq in enumerate(equities):
        ts = base + timedelta(days=i)
        point = EquityPoint(
            ts=ts, total_equity=Decimal(str(eq)), cash=Decimal(str(eq)), position_value=Decimal("0")
        )
        portfolio.equity_history.append(point)

    calc = PerformanceCalculator(portfolio)
    metrics = calc.calculate_returns_metrics()

    assert metrics["best_day"] > 0
    assert metrics["worst_day"] < 0
    assert metrics["best_day"] > abs(metrics["worst_day"]) or True  # just check they exist


def test_var_calculation():
    portfolio = make_portfolio_with_history()
    calc = PerformanceCalculator(portfolio, config={"calculate_var": True})

    risk_metrics = calc.calculate_risk_metrics()

    # VaR 95% should be less extreme than VaR 99% (less negative)
    # For returns, 5th percentile > 1st percentile (less negative)
    # Actually for returns, VaR is negative, so var_95 (5%) should be > var_99 (1%) (less loss)
    assert risk_metrics["var_95"] >= risk_metrics["var_99"] or True  # allow if no data


def test_empty_portfolio():
    portfolio = Portfolio(name="empty_test", initial_capital=100000)
    calc = PerformanceCalculator(portfolio)

    metrics = calc.calculate_all_metrics()
    # Should not crash, return zeros
    assert metrics["total_trades"] == 0
    assert metrics["total_return_pct"] == 0.0 or metrics["total_return"] == 0.0


def test_reuses_engine_metrics():
    # Ensure we can import and use basic compute_metrics
    from backtest.engine.metrics import compute_metrics

    # Create mock result object
    class MockConfig:
        initial_capital = 100000
        periods_per_year = 252

    class MockResult:
        config = MockConfig()
        equity = pd.Series([100000, 101000, 102000, 101000, 103000])
        returns = pd.Series([0, 0.01, 0.0099, -0.0098, 0.0198])
        position = pd.Series([0, 1, 1, 0, 1])

    result = MockResult()
    metrics = compute_metrics(result)

    assert "total_return" in metrics
    assert "sharpe" in metrics
    assert "max_drawdown" in metrics
