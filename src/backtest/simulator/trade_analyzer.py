"""Trade Analyzer for forward testing (Step 18).

Detailed trade insights, categorization, pattern analysis, quality metrics,
and reporting.

Features
--------
* Trade categorization: by symbol, strategy, time of day, day of week,
  holding period (scalp/day/swing), P/L buckets, exit reason
* Pattern analysis: winning/losing streaks, performance by hour/day,
  best/worst symbols, optimal holding periods
* Quality metrics: execution quality (fill vs mid), slippage, commission %,
  MAE/MFE (Maximum Adverse/Favorable Excursion)
* Reporting: daily/weekly/monthly summaries, trade-by-trade breakdown,
  export to CSV/JSON/Excel, PDF generation placeholder, email placeholder

MAE/MFE
-------
* MAE: Maximum Adverse Excursion – worst price move against position during trade
* MFE: Maximum Favorable Excursion – best price move in favor during trade
* Requires price history during trade holding period

Example
-------
>>> from backtest.simulator.portfolio import Portfolio
>>> from backtest.simulator.trade_analyzer import TradeAnalyzer
>>> portfolio = Portfolio(name="test", initial_capital=100000)
>>> analyzer = TradeAnalyzer(portfolio=portfolio)
>>> report = analyzer.generate_trade_report()
>>> report["total_trades"]
0
"""

from __future__ import annotations

import csv
import json
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from backtest.simulator.money import ZERO, to_decimal

logger = logging.getLogger("backtest.simulator.trade_analyzer")


# ---------------------------------------------------------------------------
# Trade model for analysis
# ---------------------------------------------------------------------------


@dataclass
class AnalyzedTrade:
    """Enriched trade with analysis metrics."""

    trade_id: str
    symbol: str
    strategy_name: Optional[str] = None
    direction: str = "long"
    quantity: Decimal = ZERO
    entry_price: Decimal = ZERO
    exit_price: Decimal = ZERO
    entry_time: Optional[datetime] = None
    exit_time: Optional[datetime] = None
    gross_pnl: Decimal = ZERO
    net_pnl: Decimal = ZERO
    commission_total: Decimal = ZERO
    slippage_total: Decimal = ZERO
    holding_period_minutes: Optional[int] = None
    return_pct: Optional[Decimal] = None
    exit_reason: Optional[str] = None

    # Quality metrics
    mae: Optional[Decimal] = None  # Maximum Adverse Excursion
    mfe: Optional[Decimal] = None  # Maximum Favorable Excursion
    mae_pct: Optional[Decimal] = None
    mfe_pct: Optional[Decimal] = None
    execution_quality_bps: Optional[Decimal] = None
    commission_pct_of_pnl: Optional[Decimal] = None

    # Categorization
    holding_category: str = "unknown"  # scalp, day, swing, position
    pnl_bucket: str = "unknown"
    day_of_week: Optional[str] = None
    hour_of_day: Optional[int] = None

    def __post_init__(self):
        self.symbol = str(self.symbol).strip().upper()
        if self.entry_time and isinstance(self.entry_time, str):
            try:
                self.entry_time = datetime.fromisoformat(self.entry_time)
            except Exception:
                pass
        if self.exit_time and isinstance(self.exit_time, str):
            try:
                self.exit_time = datetime.fromisoformat(self.exit_time)
            except Exception:
                pass

        # Categorize holding period
        if self.holding_period_minutes is not None:
            mins = self.holding_period_minutes
            if mins < 60:
                self.holding_category = "scalp"
            elif mins < 60 * 6:
                self.holding_category = "day"
            elif mins < 60 * 24 * 5:
                self.holding_category = "swing"
            else:
                self.holding_category = "position"

        # PnL bucket
        pnl_float = float(self.net_pnl)
        if pnl_float > 1000:
            self.pnl_bucket = "large_win"
        elif pnl_float > 100:
            self.pnl_bucket = "medium_win"
        elif pnl_float > 0:
            self.pnl_bucket = "small_win"
        elif pnl_float > -100:
            self.pnl_bucket = "small_loss"
        elif pnl_float > -1000:
            self.pnl_bucket = "medium_loss"
        else:
            self.pnl_bucket = "large_loss"

        # Day of week and hour
        if self.entry_time:
            try:
                self.day_of_week = self.entry_time.strftime("%A")
                self.hour_of_day = self.entry_time.hour
            except Exception:
                pass

    def to_dict(self):
        return {
            "trade_id": self.trade_id,
            "symbol": self.symbol,
            "strategy_name": self.strategy_name,
            "direction": self.direction,
            "quantity": str(self.quantity),
            "entry_price": str(self.entry_price),
            "exit_price": str(self.exit_price),
            "entry_time": self.entry_time.isoformat() if self.entry_time else None,
            "exit_time": self.exit_time.isoformat() if self.exit_time else None,
            "gross_pnl": str(self.gross_pnl),
            "net_pnl": str(self.net_pnl),
            "commission_total": str(self.commission_total),
            "holding_period_minutes": self.holding_period_minutes,
            "return_pct": str(self.return_pct) if self.return_pct else None,
            "exit_reason": self.exit_reason,
            "mae": str(self.mae) if self.mae else None,
            "mfe": str(self.mae) if self.mae else None,
            "execution_quality_bps": (
                str(self.execution_quality_bps) if self.execution_quality_bps else None
            ),
            "holding_category": self.holding_category,
            "pnl_bucket": self.pnl_bucket,
            "day_of_week": self.day_of_week,
            "hour_of_day": self.hour_of_day,
        }


# ---------------------------------------------------------------------------
# TradeAnalyzer
# ---------------------------------------------------------------------------


class TradeAnalyzer:
    """Analyzes trades for insights, patterns, and quality.

    Parameters
    ----------
    portfolio:
        Portfolio with closed_positions or trades
    market_data:
        Optional dict symbol -> DataFrame with OHLCV for MAE/MFE calculation
    """

    def __init__(
        self, portfolio: Any = None, market_data: Optional[Dict[str, pd.DataFrame]] = None
    ):
        self.portfolio = portfolio
        self.market_data = market_data or {}

        logger.info(
            "TradeAnalyzer initialized: portfolio=%s market_data_symbols=%s",
            getattr(portfolio, "name", "?") if portfolio else "None",
            list(self.market_data.keys()),
        )

    # -- core analysis -------------------------------------------------------

    def analyze_trade(
        self, trade: Any, price_history: Optional[pd.DataFrame] = None
    ) -> AnalyzedTrade:
        """Analyze single trade, calculate MAE/MFE and quality metrics.

        Parameters
        ----------
        trade:
            Trade object, dict, or Position with realized PnL
        price_history:
            Optional DataFrame with OHLCV during trade holding period for MAE/MFE

        Returns
        -------
        AnalyzedTrade
        """
        # Normalize trade to dict
        if isinstance(trade, dict):
            data = trade
        else:
            # Try to extract from object (Position or Trade ORM)
            data = {
                "trade_id": getattr(trade, "trade_id", None)
                or getattr(trade, "position_id", None)
                or str(id(trade)),
                "symbol": getattr(trade, "symbol", "UNKNOWN"),
                "strategy_name": getattr(trade, "strategy_name", None),
                "direction": getattr(trade, "direction", None)
                or getattr(trade, "position_type", "long"),
                "quantity": getattr(trade, "quantity", 0),
                "entry_price": getattr(trade, "entry_price", None)
                or getattr(trade, "average_entry_price", 0),
                "exit_price": getattr(trade, "exit_price", None)
                or getattr(trade, "current_price", 0),
                "entry_time": getattr(trade, "entry_time", None)
                or getattr(trade, "opened_at", None),
                "exit_time": getattr(trade, "exit_time", None) or getattr(trade, "closed_at", None),
                "gross_pnl": getattr(trade, "gross_pnl", None) or getattr(trade, "realized_pnl", 0),
                "net_pnl": getattr(trade, "net_pnl", None) or getattr(trade, "realized_pnl", 0),
                "commission_total": getattr(trade, "commission_total", 0),
                "slippage_total": getattr(trade, "slippage_total", 0),
                "holding_period_minutes": getattr(trade, "holding_period_minutes", None),
                "return_percentage": getattr(trade, "return_percentage", None)
                or getattr(trade, "return_pct", None),
                "exit_reason": getattr(trade, "exit_reason", None),
            }

        # Parse values
        trade_id = str(data.get("trade_id", "") or data.get("position_id", "") or "unknown")
        symbol = str(data.get("symbol", "UNKNOWN")).upper()
        quantity = to_decimal(data.get("quantity", 0) or 0, "quantity")
        entry_price = to_decimal(data.get("entry_price", 0) or 0, "entry_price")
        exit_price = to_decimal(data.get("exit_price", 0) or 0, "exit_price")
        gross_pnl = to_decimal(data.get("gross_pnl", 0) or 0, "gross_pnl")
        net_pnl = to_decimal(data.get("net_pnl", 0) or 0, "net_pnl")
        commission = to_decimal(data.get("commission_total", 0) or 0, "commission_total")
        slippage = to_decimal(data.get("slippage_total", 0) or 0, "slippage_total")

        # Times
        entry_time = data.get("entry_time") or data.get("opened_at")
        exit_time = data.get("exit_time") or data.get("closed_at")

        if isinstance(entry_time, str):
            try:
                entry_time = datetime.fromisoformat(entry_time)
            except Exception:
                entry_time = None
        if isinstance(exit_time, str):
            try:
                exit_time = datetime.fromisoformat(exit_time)
            except Exception:
                exit_time = None

        holding_minutes = data.get("holding_period_minutes")
        if holding_minutes is None and entry_time and exit_time:
            try:
                holding_minutes = int((exit_time - entry_time).total_seconds() / 60)
            except Exception:
                holding_minutes = None

        return_pct = data.get("return_percentage") or data.get("return_pct")
        if return_pct is not None:
            try:
                return_pct = to_decimal(return_pct, "return_pct")
            except Exception:
                return_pct = None

        # Quality metrics
        mae = None
        mfe = None
        mae_pct = None
        mfe_pct = None
        exec_quality_bps = None

        # MAE/MFE calculation if price history provided
        hist = price_history
        if hist is None and symbol in self.market_data:
            hist = self.market_data[symbol]

        if hist is not None and not hist.empty and entry_time and exit_time:
            try:
                # Filter history between entry and exit
                if isinstance(hist.index, pd.DatetimeIndex):
                    # Ensure entry/exit are timezone-aware
                    if entry_time.tzinfo is None:
                        entry_time = entry_time.replace(tzinfo=timezone.utc)
                    if exit_time.tzinfo is None:
                        exit_time = exit_time.replace(tzinfo=timezone.utc)

                    mask = (hist.index >= entry_time) & (hist.index <= exit_time)
                    trade_bars = hist.loc[mask]

                    if not trade_bars.empty:
                        # For long: MAE is max adverse (lowest low vs entry),
                        # MFE is highest high vs entry
                        # For short: inverse
                        direction = str(data.get("direction", "long")).lower()
                        is_long = direction == "long"

                        if is_long:
                            # Long: adverse is low below entry, favorable is high above entry
                            lowest = (
                                float(trade_bars["low"].min())
                                if "low" in trade_bars.columns
                                else float(trade_bars["close"].min())
                            )
                            highest = (
                                float(trade_bars["high"].max())
                                if "high" in trade_bars.columns
                                else float(trade_bars["close"].max())
                            )

                            mae_val = min(0, lowest - float(entry_price))  # negative or zero
                            mfe_val = max(0, highest - float(entry_price))  # positive or zero

                            mae = to_decimal(mae_val, "mae")
                            mfe = to_decimal(mfe_val, "mfe")

                            if entry_price != ZERO:
                                mae_pct = mae / entry_price
                                mfe_pct = mfe / entry_price

                        else:
                            # Short: adverse is high above entry, favorable is low below entry
                            lowest = (
                                float(trade_bars["low"].min())
                                if "low" in trade_bars.columns
                                else float(trade_bars["close"].min())
                            )
                            highest = (
                                float(trade_bars["high"].max())
                                if "high" in trade_bars.columns
                                else float(trade_bars["close"].max())
                            )

                            mae_val = min(
                                0, float(entry_price) - highest
                            )  # negative if high above entry
                            mfe_val = max(
                                0, float(entry_price) - lowest
                            )  # positive if low below entry

                            mae = to_decimal(mae_val, "mae")
                            mfe = to_decimal(mfe_val, "mfe")

                            if entry_price != ZERO:
                                mae_pct = mae / entry_price
                                mfe_pct = mfe / entry_price

            except Exception as exc:
                logger.debug("MAE/MFE calculation failed for %s: %s", symbol, exc)

        # Execution quality: fill price vs mid price
        # For simplicity, if we have entry_price and we know mid price at entry time from history
        # Here we approximate as 0 if no data
        if hist is not None and not hist.empty and entry_time:
            try:
                # Find closest bar to entry_time
                if isinstance(hist.index, pd.DatetimeIndex):
                    idx = hist.index.get_indexer([entry_time], method="nearest")[0]
                    if idx >= 0:
                        row = hist.iloc[idx]
                        # Mid price: (high+low)/2 or (bid+ask)/2 or close
                        mid = None
                        if (
                            "bid" in row
                            and "ask" in row
                            and pd.notna(row["bid"])
                            and pd.notna(row["ask"])
                        ):
                            mid = (float(row["bid"]) + float(row["ask"])) / 2
                        elif "high" in row and "low" in row:
                            mid = (float(row["high"]) + float(row["low"])) / 2
                        else:
                            mid = float(row["close"])

                        if mid and mid != 0:
                            # Execution quality bps: (fill - mid)/mid *10000,
                            # positive = adverse for buy
                            fill = float(entry_price)
                            eq_bps = (fill - mid) / mid * 10000
                            exec_quality_bps = to_decimal(eq_bps, "exec_quality_bps")
            except Exception as exc:
                logger.debug("Execution quality calc failed: %s", exc)

        # Commission as % of PnL
        commission_pct = None
        try:
            if net_pnl != ZERO and commission != ZERO:
                # abs commission / abs pnl
                commission_pct = abs(commission) / abs(net_pnl) if abs(net_pnl) != ZERO else None
        except Exception:
            pass

        analyzed = AnalyzedTrade(
            trade_id=trade_id,
            symbol=symbol,
            strategy_name=data.get("strategy_name"),
            direction=str(data.get("direction", "long")).lower(),
            quantity=quantity,
            entry_price=entry_price,
            exit_price=exit_price,
            entry_time=entry_time,
            exit_time=exit_time,
            gross_pnl=gross_pnl,
            net_pnl=net_pnl,
            commission_total=commission,
            slippage_total=slippage,
            holding_period_minutes=holding_minutes,
            return_pct=return_pct,
            exit_reason=data.get("exit_reason"),
            mae=mae,
            mfe=mfe,
            mae_pct=mae_pct,
            mfe_pct=mfe_pct,
            execution_quality_bps=exec_quality_bps,
            commission_pct_of_pnl=commission_pct,
        )

        return analyzed

    def get_trades(self) -> List[Any]:
        """Get trades from portfolio."""
        if self.portfolio is None:
            return []

        # Try closed_positions first
        if hasattr(self.portfolio, "closed_positions") and self.portfolio.closed_positions:
            return list(self.portfolio.closed_positions)

        # Try trades attribute
        if hasattr(self.portfolio, "trades") and self.portfolio.trades:
            return list(self.portfolio.trades)

        return []

    def categorize_trades(
        self, trades_list: Optional[List[Any]] = None
    ) -> Dict[str, Dict[str, List[AnalyzedTrade]]]:
        """Categorize trades by various dimensions.

        Returns dict with keys: by_symbol, by_strategy, by_time_of_day, by_day_of_week,
        by_holding_period, by_pnl_bucket, by_exit_reason
        """
        trades = trades_list or self.get_trades()
        analyzed = [self.analyze_trade(t) for t in trades]

        categorized = {
            "by_symbol": defaultdict(list),
            "by_strategy": defaultdict(list),
            "by_time_of_day": defaultdict(list),
            "by_day_of_week": defaultdict(list),
            "by_holding_period": defaultdict(list),
            "by_pnl_bucket": defaultdict(list),
            "by_exit_reason": defaultdict(list),
        }

        for trade in analyzed:
            categorized["by_symbol"][trade.symbol].append(trade)
            if trade.strategy_name:
                categorized["by_strategy"][trade.strategy_name].append(trade)
            if trade.hour_of_day is not None:
                # Bucket by hour: morning (9-12), afternoon (12-15), etc.
                hour = trade.hour_of_day
                if 9 <= hour < 12:
                    categorized["by_time_of_day"]["morning"].append(trade)
                elif 12 <= hour < 15:
                    categorized["by_time_of_day"]["afternoon"].append(trade)
                elif 15 <= hour < 18:
                    categorized["by_time_of_day"]["evening"].append(trade)
                else:
                    categorized["by_time_of_day"]["other"].append(trade)

            if trade.day_of_week:
                categorized["by_day_of_week"][trade.day_of_week].append(trade)

            categorized["by_holding_period"][trade.holding_category].append(trade)
            categorized["by_pnl_bucket"][trade.pnl_bucket].append(trade)

            if trade.exit_reason:
                categorized["by_exit_reason"][trade.exit_reason].append(trade)
            else:
                categorized["by_exit_reason"]["unknown"].append(trade)

        # Convert defaultdict to regular dict
        return {k: dict(v) for k, v in categorized.items()}

    def find_patterns(self, trades_list: Optional[List[Any]] = None) -> Dict[str, Any]:
        """Find patterns in trades.

        Returns dict with:
        - winning/losing streaks
        - performance by hour of day
        - performance by day of week
        - best/worst performing symbols
        - optimal holding periods
        - entry price vs average price analysis
        """
        trades = trades_list or self.get_trades()
        analyzed = [self.analyze_trade(t) for t in trades]

        if not analyzed:
            return {
                "winning_streak": 0,
                "losing_streak": 0,
                "max_winning_streak": 0,
                "max_losing_streak": 0,
                "performance_by_hour": {},
                "performance_by_day": {},
                "best_worst_symbols": {},
                "optimal_holding": {},
            }

        # Winning/losing streaks
        max_win_streak = 0
        max_loss_streak = 0
        curr_win = 0
        curr_loss = 0

        for trade in analyzed:
            if trade.net_pnl > ZERO:
                curr_win += 1
                curr_loss = 0
                max_win_streak = max(max_win_streak, curr_win)
            elif trade.net_pnl < ZERO:
                curr_loss += 1
                curr_win = 0
                max_loss_streak = max(max_loss_streak, curr_loss)
            else:
                curr_win = 0
                curr_loss = 0

        # Current streak (last trades)
        winning_streak = 0
        losing_streak = 0
        for trade in reversed(analyzed):
            if trade.net_pnl > ZERO and losing_streak == 0:
                winning_streak += 1
            elif trade.net_pnl < ZERO and winning_streak == 0:
                losing_streak += 1
            else:
                break

        # Performance by hour of day
        perf_by_hour = defaultdict(list)
        for trade in analyzed:
            if trade.hour_of_day is not None:
                perf_by_hour[trade.hour_of_day].append(float(trade.net_pnl))

        performance_by_hour = {
            hour: {"avg_pnl": sum(pnls) / len(pnls), "count": len(pnls), "total_pnl": sum(pnls)}
            for hour, pnls in perf_by_hour.items()
        }

        # Performance by day of week
        perf_by_day = defaultdict(list)
        for trade in analyzed:
            if trade.day_of_week:
                perf_by_day[trade.day_of_week].append(float(trade.net_pnl))

        performance_by_day = {
            day: {"avg_pnl": sum(pnls) / len(pnls), "count": len(pnls), "total_pnl": sum(pnls)}
            for day, pnls in perf_by_day.items()
        }

        # Best/worst performing symbols
        perf_by_symbol = defaultdict(list)
        for trade in analyzed:
            perf_by_symbol[trade.symbol].append(float(trade.net_pnl))

        best_worst_symbols = {}
        for symbol, pnls in perf_by_symbol.items():
            best_worst_symbols[symbol] = {
                "total_pnl": sum(pnls),
                "avg_pnl": sum(pnls) / len(pnls) if pnls else 0,
                "count": len(pnls),
                "win_rate": sum(1 for p in pnls if p > 0) / len(pnls) if pnls else 0,
            }

        # Sort by total PnL
        sorted_symbols = sorted(
            best_worst_symbols.items(), key=lambda x: x[1]["total_pnl"], reverse=True
        )
        best_symbol = sorted_symbols[0] if sorted_symbols else None
        worst_symbol = sorted_symbols[-1] if sorted_symbols else None

        # Optimal holding periods
        perf_by_holding = defaultdict(list)
        for trade in analyzed:
            perf_by_holding[trade.holding_category].append(float(trade.net_pnl))

        optimal_holding = {
            cat: {"avg_pnl": sum(pnls) / len(pnls), "count": len(pnls)}
            for cat, pnls in perf_by_holding.items()
        }

        # Entry price vs average price analysis
        # For each symbol, compare entry price to average close during holding?
        # Simplified: just stats on entry prices
        entry_prices = [float(t.entry_price) for t in analyzed if t.entry_price != ZERO]

        entry_analysis = {
            "avg_entry_price": sum(entry_prices) / len(entry_prices) if entry_prices else 0,
            "min_entry": min(entry_prices) if entry_prices else 0,
            "max_entry": max(entry_prices) if entry_prices else 0,
        }

        return {
            "winning_streak": winning_streak,
            "losing_streak": losing_streak,
            "max_winning_streak": max_win_streak,
            "max_losing_streak": max_loss_streak,
            "performance_by_hour": performance_by_hour,
            "performance_by_day": performance_by_day,
            "best_worst_symbols": best_worst_symbols,
            "best_symbol": best_symbol,
            "worst_symbol": worst_symbol,
            "optimal_holding": optimal_holding,
            "entry_analysis": entry_analysis,
        }

    def generate_trade_report(
        self, date_range: Optional[Tuple[datetime, datetime]] = None
    ) -> Dict[str, Any]:
        """Generate comprehensive trade report.

        Parameters
        ----------
        date_range:
            Optional (start, end) tuple to filter trades

        Returns
        -------
        Dict with report
        """
        trades = self.get_trades()

        # Filter by date range if provided
        if date_range:
            start, end = date_range
            filtered = []
            for trade in trades:
                entry_time = getattr(trade, "entry_time", None) or getattr(trade, "opened_at", None)
                if entry_time:
                    if isinstance(entry_time, str):
                        try:
                            entry_time = datetime.fromisoformat(entry_time)
                        except Exception:
                            continue
                    if start <= entry_time <= end:
                        filtered.append(trade)
            trades = filtered

        analyzed = [self.analyze_trade(t) for t in trades]
        categorized = self.categorize_trades(trades)
        patterns = self.find_patterns(trades)

        # Summary stats
        total_pnl = sum(float(t.net_pnl) for t in analyzed)
        total_trades = len(analyzed)
        winning = sum(1 for t in analyzed if t.net_pnl > ZERO)
        losing = sum(1 for t in analyzed if t.net_pnl < ZERO)

        # Daily summary
        daily_summary = defaultdict(list)
        for trade in analyzed:
            if trade.exit_time:
                day = trade.exit_time.date()
                daily_summary[day].append(float(trade.net_pnl))

        daily_report = {
            str(day): {"total_pnl": sum(pnls), "count": len(pnls)}
            for day, pnls in daily_summary.items()
        }

        # Trade-by-trade breakdown
        breakdown = [t.to_dict() for t in analyzed]

        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "date_range": (
                [d.isoformat() if isinstance(d, datetime) else str(d) for d in date_range]
                if date_range
                else None
            ),
            "total_trades": total_trades,
            "winning_trades": winning,
            "losing_trades": losing,
            "win_rate": winning / total_trades if total_trades > 0 else 0,
            "total_pnl": total_pnl,
            "avg_pnl": total_pnl / total_trades if total_trades > 0 else 0,
            "categorized": categorized,
            "patterns": patterns,
            "daily_summary": daily_report,
            "trades": breakdown,
        }

        logger.info(
            "Trade report generated: %s trades, total PnL %.2f, win rate %.1f%%",
            total_trades,
            total_pnl,
            report["win_rate"] * 100,
        )

        return report

    def export_trades(
        self,
        format: str = "csv",
        file_path: Optional[str | Path] = None,
        trades_list: Optional[List[Any]] = None,
    ) -> str:
        """Export trades to file.

        Parameters
        ----------
        format:
            csv, json, or excel
        file_path:
            Path to save to. If None, uses temp file or returns string for json.
        trades_list:
            Optional list of trades to export, else uses portfolio's

        Returns
        -------
        str
            Path to exported file or JSON string
        """
        trades = trades_list or self.get_trades()
        analyzed = [self.analyze_trade(t) for t in trades]

        fmt = str(format).strip().lower()

        if file_path is None:
            if fmt == "json":
                # Return JSON string
                return json.dumps([t.to_dict() for t in analyzed], indent=2)
            else:
                # Default to temp file
                import tempfile

                suffix = ".csv" if fmt == "csv" else ".xlsx" if fmt == "excel" else ".json"
                file_path = (
                    Path(tempfile.gettempdir())
                    / f"trades_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}{suffix}"
                )

        file_path = Path(file_path)
        file_path.parent.mkdir(parents=True, exist_ok=True)

        if fmt == "csv":
            with open(file_path, "w", newline="") as f:
                if not analyzed:
                    f.write("")
                    return str(file_path)

                # Use first trade's dict keys as header
                fieldnames = list(analyzed[0].to_dict().keys())
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                for trade in analyzed:
                    writer.writerow(trade.to_dict())

            logger.info("Trades exported to CSV: %s", file_path)
            return str(file_path)

        elif fmt == "json":
            with open(file_path, "w") as f:
                json.dump([t.to_dict() for t in analyzed], f, indent=2)
            logger.info("Trades exported to JSON: %s", file_path)
            return str(file_path)

        elif fmt in ("excel", "xlsx", "xls"):
            try:
                df = pd.DataFrame([t.to_dict() for t in analyzed])
                df.to_excel(file_path, index=False)
                logger.info("Trades exported to Excel: %s", file_path)
                return str(file_path)
            except Exception as exc:
                logger.warning("Excel export failed, falling back to CSV: %s", exc)
                # Fallback to CSV
                csv_path = file_path.with_suffix(".csv")
                return self.export_trades(format="csv", file_path=csv_path, trades_list=trades)

        else:
            raise ValueError(f"Unsupported export format {format!r}; expected csv, json, excel")

    # -- additional quality metrics ----------------------------------------

    def calculate_execution_quality(
        self, trades_list: Optional[List[Any]] = None
    ) -> Dict[str, Any]:
        """Calculate execution quality metrics."""
        trades = trades_list or self.get_trades()
        analyzed = [self.analyze_trade(t) for t in trades]

        qualities = [
            float(t.execution_quality_bps) for t in analyzed if t.execution_quality_bps is not None
        ]

        if not qualities:
            return {"avg_execution_quality_bps": 0, "count": 0}

        return {
            "avg_execution_quality_bps": sum(qualities) / len(qualities),
            "min_bps": min(qualities),
            "max_bps": max(qualities),
            "count": len(qualities),
        }

    def calculate_slippage_analysis(
        self, trades_list: Optional[List[Any]] = None
    ) -> Dict[str, Any]:
        """Analyze slippage across trades."""
        trades = trades_list or self.get_trades()
        analyzed = [self.analyze_trade(t) for t in trades]

        slippages = [float(t.slippage_total) for t in analyzed if t.slippage_total != ZERO]

        if not slippages:
            return {"total_slippage": 0, "avg_slippage": 0, "count": 0}

        return {
            "total_slippage": sum(slippages),
            "avg_slippage": sum(slippages) / len(slippages),
            "max_slippage": max(slippages),
            "count": len(slippages),
        }

    def __repr__(self):
        return (
            f"<TradeAnalyzer "
            f"portfolio={getattr(self.portfolio, 'name', '?') if self.portfolio else 'None'} "
            f"trades={len(self.get_trades())}>"
        )
