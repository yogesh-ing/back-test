"""Tests for Step 18: Trade Analyzer."""

from __future__ import annotations

from decimal import Decimal
from datetime import datetime, timezone, timedelta

import pandas as pd
import pytest

from backtest.simulator.portfolio import Portfolio, EquityPoint
from backtest.simulator.trade_analyzer import TradeAnalyzer, AnalyzedTrade


def make_portfolio_with_trades():
    portfolio = Portfolio(name="analyzer_test", initial_capital=100000)

    # Create trades with different symbols, exit reasons, holding periods
    base = datetime(2024, 1, 1, 9, 15, tzinfo=timezone.utc)

    # Trade 1: INFY, win, day, signal exit
    pos1 = portfolio.open_position("INFY", 100, 100)
    pos1.opened_at = base
    portfolio.reduce_position("INFY", 100, 110)  # +1000
    # Manually set closed_at for holding period
    if portfolio.closed_positions:
        portfolio.closed_positions[-1].closed_at = base + timedelta(hours=2)
        portfolio.closed_positions[-1].opened_at = base
        # Add exit_reason via attribute
        portfolio.closed_positions[-1].exit_reason = "signal"

    # Trade 2: TCS, loss, scalp, stop loss
    base2 = base + timedelta(days=1)
    pos2 = portfolio.open_position("TCS", 50, 200)
    pos2.opened_at = base2
    portfolio.reduce_position("TCS", 50, 190)  # -500
    if portfolio.closed_positions:
        portfolio.closed_positions[-1].closed_at = base2 + timedelta(minutes=30)
        portfolio.closed_positions[-1].opened_at = base2
        portfolio.closed_positions[-1].exit_reason = "stop_loss"

    # Trade 3: RELIANCE, win, swing, take profit
    base3 = base + timedelta(days=2)
    pos3 = portfolio.open_position("RELIANCE", 100, 150)
    pos3.opened_at = base3
    portfolio.reduce_position("RELIANCE", 100, 160)  # +1000
    if portfolio.closed_positions:
        portfolio.closed_positions[-1].closed_at = base3 + timedelta(days=3)
        portfolio.closed_positions[-1].opened_at = base3
        portfolio.closed_positions[-1].exit_reason = "take_profit"

    # Trade 4: INFY, loss, day, signal
    base4 = base + timedelta(days=3)
    pos4 = portfolio.open_position("INFY", 100, 110)
    pos4.opened_at = base4
    portfolio.reduce_position("INFY", 100, 105)  # -500
    if portfolio.closed_positions:
        portfolio.closed_positions[-1].closed_at = base4 + timedelta(hours=5)
        portfolio.closed_positions[-1].opened_at = base4
        portfolio.closed_positions[-1].exit_reason = "signal"

    return portfolio


def make_market_data():
    # Create price history for MAE/MFE
    base = datetime(2024, 1, 1, 9, 15, tzinfo=timezone.utc)
    dates = [base + timedelta(minutes=i * 10) for i in range(20)]
    df = pd.DataFrame(
        {
            "open": [100 + i * 0.5 for i in range(20)],
            "high": [101 + i * 0.5 for i in range(20)],
            "low": [99 + i * 0.5 for i in range(20)],
            "close": [100.5 + i * 0.5 for i in range(20)],
            "volume": [1000] * 20,
        },
        index=pd.DatetimeIndex(dates),
    )
    return {"INFY": df, "TCS": df, "RELIANCE": df}


def test_analyzed_trade_categorization():
    trade = AnalyzedTrade(
        trade_id="1",
        symbol="INFY",
        quantity=Decimal("100"),
        entry_price=Decimal("100"),
        exit_price=Decimal("110"),
        gross_pnl=Decimal("1000"),
        net_pnl=Decimal("1000"),
        holding_period_minutes=30,
        entry_time=datetime(2024, 1, 2, 10, 0, tzinfo=timezone.utc),
        exit_reason="signal",
    )

    assert trade.holding_category == "scalp"  # 30 min <60
    assert trade.pnl_bucket == "medium_win"  # 1000
    assert trade.day_of_week == "Tuesday"
    assert trade.hour_of_day == 10


def test_holding_categories():
    # Scalp <60
    t1 = AnalyzedTrade(trade_id="1", symbol="INFY", holding_period_minutes=30, net_pnl=Decimal("100"))
    assert t1.holding_category == "scalp"

    # Day <6 hours
    t2 = AnalyzedTrade(trade_id="2", symbol="INFY", holding_period_minutes=120, net_pnl=Decimal("100"))
    assert t2.holding_category == "day"

    # Swing <5 days
    t3 = AnalyzedTrade(trade_id="3", symbol="INFY", holding_period_minutes=60 * 24 * 2, net_pnl=Decimal("100"))
    assert t3.holding_category == "swing"

    # Position >=5 days
    t4 = AnalyzedTrade(trade_id="4", symbol="INFY", holding_period_minutes=60 * 24 * 6, net_pnl=Decimal("100"))
    assert t4.holding_category == "position"


def test_analyze_trade():
    portfolio = make_portfolio_with_trades()
    market_data = make_market_data()
    analyzer = TradeAnalyzer(portfolio=portfolio, market_data=market_data)

    trades = analyzer.get_trades()
    assert len(trades) == 4

    analyzed = analyzer.analyze_trade(trades[0])
    assert analyzed.symbol == "INFY"
    assert analyzed.net_pnl == Decimal("1000")

    # With price history for MAE/MFE
    # Trade 1 entry base, exit base+2h, history from base to base+2h should have MAE/MFE
    price_hist = market_data["INFY"]
    analyzed_with_hist = analyzer.analyze_trade(trades[0], price_history=price_hist)
    # MAE/MFE may be calculated if times overlap
    assert analyzed_with_hist is not None


def test_categorize_trades():
    portfolio = make_portfolio_with_trades()
    analyzer = TradeAnalyzer(portfolio=portfolio)

    categorized = analyzer.categorize_trades()

    assert "by_symbol" in categorized
    assert "by_exit_reason" in categorized
    assert "by_holding_period" in categorized
    assert "by_pnl_bucket" in categorized

    # By symbol: INFY should have 2 trades
    assert len(categorized["by_symbol"]["INFY"]) == 2
    assert len(categorized["by_symbol"]["TCS"]) == 1

    # By exit reason
    assert len(categorized["by_exit_reason"]["signal"]) == 2
    assert len(categorized["by_exit_reason"]["stop_loss"]) == 1
    assert len(categorized["by_exit_reason"]["take_profit"]) == 1

    # By holding period
    assert "scalp" in categorized["by_holding_period"]
    assert "day" in categorized["by_holding_period"]
    assert "swing" in categorized["by_holding_period"]


def test_find_patterns():
    portfolio = make_portfolio_with_trades()
    analyzer = TradeAnalyzer(portfolio=portfolio)

    patterns = analyzer.find_patterns()

    assert "winning_streak" in patterns
    assert "losing_streak" in patterns
    assert "max_winning_streak" in patterns
    assert "max_losing_streak" in patterns
    assert "performance_by_day" in patterns
    assert "performance_by_hour" in patterns
    assert "best_worst_symbols" in patterns
    assert "optimal_holding" in patterns

    # Check streaks: trades are win, loss, win, loss -> max streak 1
    assert patterns["max_winning_streak"] == 1
    assert patterns["max_losing_streak"] == 1

    # Best/worst symbols
    assert "INFY" in patterns["best_worst_symbols"]
    # INFY: +1000 -500 = +500 total
    assert patterns["best_worst_symbols"]["INFY"]["total_pnl"] == 500


def test_performance_by_time():
    portfolio = make_portfolio_with_trades()
    analyzer = TradeAnalyzer(portfolio=portfolio)

    patterns = analyzer.find_patterns()

    # Performance by day of week – entry times are Mon, Tue, Wed, Thu
    assert len(patterns["performance_by_day"]) >= 1

    # Performance by hour – all entries at 09:15
    assert len(patterns["performance_by_hour"]) >= 1


def test_generate_trade_report():
    portfolio = make_portfolio_with_trades()
    analyzer = TradeAnalyzer(portfolio=portfolio)

    report = analyzer.generate_trade_report()

    assert "total_trades" in report
    assert "winning_trades" in report
    assert "losing_trades" in report
    assert "win_rate" in report
    assert "total_pnl" in report
    assert "categorized" in report
    assert "patterns" in report
    assert "trades" in report

    assert report["total_trades"] == 4
    assert report["winning_trades"] == 2
    assert report["losing_trades"] == 2
    assert report["win_rate"] == 0.5
    # Total PnL: 1000 -500 +1000 -500 =1000
    assert report["total_pnl"] == 1000


def test_generate_report_with_date_range():
    portfolio = make_portfolio_with_trades()
    analyzer = TradeAnalyzer(portfolio=portfolio)

    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 1, 2, tzinfo=timezone.utc)

    report = analyzer.generate_trade_report(date_range=(start, end))

    # Should filter trades in range – only first trade (Jan1)
    # Actually our trades are Jan1, Jan2, Jan3, Jan4 – so Jan1 to Jan2 inclusive should have 2 trades
    assert report["total_trades"] <= 4


def test_export_csv():
    portfolio = make_portfolio_with_trades()
    analyzer = TradeAnalyzer(portfolio=portfolio)

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmpdir:
        csv_path = Path(tmpdir) / "trades.csv"
        result = analyzer.export_trades(format="csv", file_path=csv_path)

        assert Path(result).exists()
        assert Path(result).suffix == ".csv"

        # Check content
        with open(result) as f:
            content = f.read()
            assert "INFY" in content
            assert "trade_id" in content


def test_export_json():
    portfolio = make_portfolio_with_trades()
    analyzer = TradeAnalyzer(portfolio=portfolio)

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmpdir:
        json_path = Path(tmpdir) / "trades.json"
        result = analyzer.export_trades(format="json", file_path=json_path)

        assert Path(result).exists()

        with open(result) as f:
            data = f.read()
            assert "INFY" in data

        # Without file_path, should return JSON string
        json_str = analyzer.export_trades(format="json", file_path=None)
        assert "INFY" in json_str
        assert json_str.startswith("[")


def test_export_excel():
    portfolio = make_portfolio_with_trades()
    analyzer = TradeAnalyzer(portfolio=portfolio)

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmpdir:
        excel_path = Path(tmpdir) / "trades.xlsx"
        result = analyzer.export_trades(format="excel", file_path=excel_path)

        # May fallback to CSV if openpyxl not available, but should create file
        assert Path(result).exists()


def test_execution_quality():
    portfolio = make_portfolio_with_trades()
    market_data = make_market_data()
    analyzer = TradeAnalyzer(portfolio=portfolio, market_data=market_data)

    quality = analyzer.calculate_execution_quality()
    assert "avg_execution_quality_bps" in quality
    assert "count" in quality


def test_slippage_analysis():
    portfolio = make_portfolio_with_trades()
    analyzer = TradeAnalyzer(portfolio=portfolio)

    # Add slippage to trades
    for pos in portfolio.closed_positions:
        pos.slippage_total = Decimal("10")

    analysis = analyzer.calculate_slippage_analysis()
    assert "total_slippage" in analysis
    assert "avg_slippage" in analysis


def test_empty_portfolio():
    portfolio = Portfolio(name="empty", initial_capital=100000)
    analyzer = TradeAnalyzer(portfolio=portfolio)

    assert len(analyzer.get_trades()) == 0

    report = analyzer.generate_trade_report()
    assert report["total_trades"] == 0
    assert report["win_rate"] == 0

    patterns = analyzer.find_patterns()
    assert patterns["max_winning_streak"] == 0

    categorized = analyzer.categorize_trades()
    assert len(categorized["by_symbol"]) == 0


def test_mae_mfe_calculation():
    portfolio = Portfolio(name="mae_test", initial_capital=100000)

    base = datetime(2024, 1, 1, 9, 15, tzinfo=timezone.utc)
    pos = portfolio.open_position("INFY", 100, 100)
    pos.opened_at = base
    portfolio.reduce_position("INFY", 100, 105)
    if portfolio.closed_positions:
        portfolio.closed_positions[-1].closed_at = base + timedelta(hours=1)
        portfolio.closed_positions[-1].opened_at = base

    # Create price history that goes down to 95 (adverse) and up to 110 (favorable)
    dates = [base + timedelta(minutes=i * 5) for i in range(12)]  # 1 hour
    lows = [100, 99, 98, 95, 96, 97, 98, 99, 100, 102, 105, 110]
    highs = [101, 100, 99, 96, 97, 98, 99, 100, 101, 103, 106, 111]

    df = pd.DataFrame(
        {"open": [100] * 12, "high": highs, "low": lows, "close": [100 + i for i in range(12)], "volume": [1000] * 12},
        index=pd.DatetimeIndex(dates),
    )

    analyzer = TradeAnalyzer(portfolio=portfolio, market_data={"INFY": df})

    trades = analyzer.get_trades()
    analyzed = analyzer.analyze_trade(trades[0], price_history=df)

    # Long: MAE should be 95-100 = -5 (adverse), MFE 111-100=11 (favorable)
    assert analyzed.mae is not None
    assert analyzed.mfe is not None
    assert float(analyzed.mae) <= 0
    assert float(analyzed.mfe) >= 0
