"""Tests for Step 22: Backtesting Comparison Tool."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pandas as pd
import pytest

from backtest.analysis.comparison import ComparisonAnalyzer, ComparisonConfig
from backtest.simulator.portfolio import Portfolio
from backtest.db.config import DatabaseConfig
from backtest.db.manager import DatabaseManager
from backtest.db.models import Base


def make_backtest_results():
    # Simple backtest: equity from 100k to 110k, 5 trades
    equity = [100000, 101000, 102000, 103000, 104000, 105000, 106000, 107000, 108000, 109000, 110000]
    trades = [
        {"symbol": "INFY", "net_pnl": 1000, "pnl": 1000},
        {"symbol": "INFY", "net_pnl": -500, "pnl": -500},
        {"symbol": "TCS", "net_pnl": 1500, "pnl": 1500},
        {"symbol": "TCS", "net_pnl": 500, "pnl": 500},
        {"symbol": "RELIANCE", "net_pnl": 2000, "pnl": 2000},
    ]

    return {"equity": equity, "trades": trades, "metrics": {"total_return": 0.10, "sharpe_ratio": 1.5, "win_rate": 0.8, "total_trades": 5, "max_drawdown": -0.02}}


def make_forward_portfolio():
    portfolio = Portfolio(name="forward_test", initial_capital=100000)

    # Equity with friction: 100k to 108k (2k less due to slippage/commission)
    from datetime import datetime, timezone, timedelta
    from decimal import Decimal
    from backtest.simulator.portfolio import EquityPoint

    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    equities = [100000, 100800, 101500, 102200, 103000, 104000, 105000, 106000, 107000, 108000]

    for i, eq in enumerate(equities):
        ts = base + timedelta(days=i)
        point = EquityPoint(ts=ts, total_equity=Decimal(str(eq)), cash=Decimal(str(eq)), position_value=Decimal("0"))
        portfolio.equity_history.append(point)

    # Closed positions with commission/slippage
    pos1 = portfolio.open_position("INFY", 100, 100)
    portfolio.reduce_position("INFY", 100, 109)  # +900 after commission?
    pos2 = portfolio.open_position("TCS", 50, 200)
    portfolio.reduce_position("TCS", 50, 195)  # -250

    return portfolio


def test_comparison_config():
    cfg = ComparisonConfig(risk_free_rate=0.02, periods_per_year=252)
    assert cfg.risk_free_rate == 0.02


def test_load_backtest_results_dict():
    analyzer = ComparisonAnalyzer()
    data = make_backtest_results()

    metrics = analyzer.load_backtest_results(data)

    assert "total_return" in metrics
    assert analyzer._backtest_equity is not None
    assert len(analyzer._backtest_trades) == 5


def test_load_backtest_results_df():
    analyzer = ComparisonAnalyzer()

    df = pd.DataFrame({"total_equity": [100000, 101000, 102000, 103000, 104000, 105000]})
    metrics = analyzer.load_backtest_results(df)

    assert "total_return" in metrics
    assert analyzer._backtest_equity is not None


def test_load_backtest_results_json_file():
    analyzer = ComparisonAnalyzer()
    data = make_backtest_results()

    with tempfile.TemporaryDirectory() as tmpdir:
        json_path = Path(tmpdir) / "backtest.json"
        json_path.write_text(json.dumps(data))

        metrics = analyzer.load_backtest_results(str(json_path))
        assert "total_return" in metrics


def test_load_forward_results_portfolio():
    analyzer = ComparisonAnalyzer()
    portfolio = make_forward_portfolio()

    metrics = analyzer.load_forward_test_results(portfolio=portfolio)

    assert "total_return" in metrics
    assert analyzer._forward_equity is not None


def test_compare_metrics():
    analyzer = ComparisonAnalyzer()
    analyzer.load_backtest_results(make_backtest_results())
    analyzer.load_forward_test_results(portfolio=make_forward_portfolio())

    diffs = analyzer.compare_metrics()

    assert "return_difference" in diffs
    assert "sharpe_ratio_difference" in diffs
    assert "trade_count_difference" in diffs
    assert "total_return_backtest" in diffs
    assert "total_return_forward" in diffs

    # Forward should underperform backtest in this example (108k vs 110k)
    assert diffs["return_difference"] < 0


def test_compare_trades():
    analyzer = ComparisonAnalyzer()
    analyzer.load_backtest_results(make_backtest_results())
    analyzer.load_forward_test_results(portfolio=make_forward_portfolio())

    comparison = analyzer.compare_trades()

    assert "backtest_trade_count" in comparison
    assert "forward_trade_count" in comparison
    assert "pnl_difference" in comparison

    assert comparison["backtest_trade_count"] == 5
    assert comparison["forward_trade_count"] == 2


def test_attribution():
    analyzer = ComparisonAnalyzer()
    analyzer.load_backtest_results(make_backtest_results())
    portfolio = make_forward_portfolio()
    # Add commission to forward metrics for attribution
    portfolio.total_commission = 500
    analyzer.load_forward_test_results(portfolio=portfolio)
    # Manually set slippage and commission for test
    analyzer._forward_metrics["total_slippage"] = 1500
    analyzer._forward_metrics["total_commission"] = 500

    attribution = analyzer.calculate_attribution()

    assert "total_return_difference" in attribution
    assert "slippage_cost" in attribution
    assert "commission_cost" in attribution
    assert "total_friction" in attribution
    assert attribution["total_friction"] == 2000
    assert "underperformed" in attribution


def test_lookahead_bias_detection_no_db():
    analyzer = ComparisonAnalyzer(db_manager=None)

    bias = analyzer.detect_lookahead_bias()
    assert bias["checked"] is False
    assert "db_manager required" in bias["message"]


def test_lookahead_bias_detection_with_db():
    cfg = DatabaseConfig(url="sqlite:///:memory:", pool_min_size=1, pool_max_size=5)
    db = DatabaseManager(cfg)
    db.connect()
    Base.metadata.create_all(db.engine)

    # Create portfolio and signals
    portfolio = Portfolio(name="bias_test", initial_capital=100000)
    portfolio.save_to_db(db)

    from backtest.db.models import StrategySignal as SignalRow
    from datetime import datetime, timezone, timedelta

    base = datetime(2024, 1, 2, 10, 0, tzinfo=timezone.utc)

    with db.session() as session:
        # Valid signal: bar_ts < generated_at
        valid_signal = SignalRow(
            portfolio_id=portfolio.portfolio_id,
            symbol="INFY",
            signal_type="entry",
            direction="long",
            bar_ts=base,
            generated_at=base + timedelta(seconds=10),
            executed=True,
        )
        session.add(valid_signal)

        # Biased signal: bar_ts >= generated_at
        biased_signal = SignalRow(
            portfolio_id=portfolio.portfolio_id,
            symbol="INFY",
            signal_type="entry",
            direction="long",
            bar_ts=base + timedelta(seconds=20),
            generated_at=base + timedelta(seconds=10),
            executed=True,
        )
        session.add(biased_signal)

    analyzer = ComparisonAnalyzer(db_manager=db)
    bias = analyzer.detect_lookahead_bias()

    assert bias["checked"] is True
    assert bias["has_bias"] is True
    assert bias["biased_count"] == 1
    assert bias["total_count"] == 2

    db.disconnect()


def test_generate_report_json():
    analyzer = ComparisonAnalyzer()
    analyzer.load_backtest_results(make_backtest_results())
    analyzer.load_forward_test_results(portfolio=make_forward_portfolio())

    with tempfile.TemporaryDirectory() as tmpdir:
        json_path = Path(tmpdir) / "comparison.json"
        result_path = analyzer.generate_comparison_report(file_path=json_path)

        assert Path(result_path).exists()
        data = json.loads(Path(result_path).read_text())
        assert "differences" in data
        assert "attribution" in data
        assert "recommendations" in data


def test_generate_report_no_file():
    analyzer = ComparisonAnalyzer()
    analyzer.load_backtest_results(make_backtest_results())
    analyzer.load_forward_test_results(portfolio=make_forward_portfolio())

    json_str = analyzer.generate_comparison_report(file_path=None)
    assert "differences" in json_str
    assert "INFY" in json_str or "total_return" in json_str


def test_recommendations():
    analyzer = ComparisonAnalyzer()
    analyzer.load_backtest_results(make_backtest_results())
    portfolio = make_forward_portfolio()
    analyzer.load_forward_test_results(portfolio=portfolio)
    analyzer._forward_metrics["total_slippage"] = 1500
    analyzer._forward_metrics["total_commission"] = 500

    diffs = analyzer.compare_metrics()
    attribution = analyzer.calculate_attribution()
    bias = {"has_bias": False}

    recs = analyzer._generate_recommendations(diffs, attribution, bias)

    assert len(recs) >= 1
    assert any("underperformed" in r.lower() or "friction" in r.lower() or "no major" in r.lower() for r in recs)

    # With bias
    bias_with = {"has_bias": True, "biased_count": 5}
    recs2 = analyzer._generate_recommendations(diffs, attribution, bias_with)
    assert any("bias" in r.lower() for r in recs2)


def test_statistical_tests_no_scipy():
    analyzer = ComparisonAnalyzer()

    # Without equity curves, should return message
    result = analyzer.statistical_significance_tests()
    assert "message" in result

    # With equity curves but no scipy, should handle gracefully
    analyzer.load_backtest_results(make_backtest_results())
    analyzer.load_forward_test_results(portfolio=make_forward_portfolio())

    result = analyzer.statistical_significance_tests()
    # May have scipy or not, but should not crash
    assert isinstance(result, dict)
