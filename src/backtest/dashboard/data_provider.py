"""Dashboard Data Provider – backend logic for dashboard (Step 19).

Gathers data from Portfolio, PerformanceCalculator, TradeAnalyzer,
OrderExecutor, and ForwardTestingEngine into JSON-serializable dicts
for the web UI.

This module is pure backend logic (no Flask/Streamlit dependency) so it
can be unit tested without a web server.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, date, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional

from backtest.simulator.money import ZERO

logger = logging.getLogger("backtest.dashboard.data_provider")


class DashboardDataProvider:
    """Provides data for dashboard sections.

    Parameters
    ----------
    portfolio:
        Simulator Portfolio
    performance:
        PerformanceCalculator or similar with get_metrics()
    trade_analyzer:
        TradeAnalyzer with generate_trade_report()
    engine:
        ForwardTestingEngine with get_status()
    data_handler:
        MarketDataHandler with is_connected(), get_stats()
    """

    def __init__(
        self,
        portfolio: Any = None,
        performance: Any = None,
        trade_analyzer: Any = None,
        engine: Any = None,
        data_handler: Any = None,
    ):
        self.portfolio = portfolio
        self.performance = performance
        self.trade_analyzer = trade_analyzer
        self.engine = engine
        self.data_handler = data_handler

    # -- portfolio overview ------------------------------------------------

    def get_portfolio_overview(self) -> Dict[str, Any]:
        """Portfolio overview: equity, cash, position value, P&L."""
        if self.portfolio is None:
            return {
                "total_equity": 0,
                "cash": 0,
                "position_value": 0,
                "today_pnl": 0,
                "today_pnl_pct": 0,
                "total_pnl": 0,
                "total_pnl_pct": 0,
                "initial_capital": 0,
            }

        try:
            total_equity = float(self.portfolio.calculate_total_equity() if hasattr(self.portfolio, "calculate_total_equity") else 0)
            cash = float(getattr(self.portfolio, "current_cash", 0) or 0)
            position_value = float(self.portfolio.calculate_position_value() if hasattr(self.portfolio, "calculate_position_value") else 0)
            initial = float(getattr(self.portfolio, "initial_capital", 1) or 1)

            total_pnl = total_equity - initial
            total_pnl_pct = (total_equity / initial - 1) * 100 if initial != 0 else 0

            # Today's P&L from equity_history or performance
            today_pnl = 0
            today_pnl_pct = 0
            try:
                if hasattr(self.portfolio, "equity_history") and self.portfolio.equity_history:
                    # Find today's first equity point
                    today = date.today()
                    today_points = [p for p in self.portfolio.equity_history if p.ts.date() == today]
                    if today_points:
                        first_today = float(today_points[0].total_equity)
                        today_pnl = total_equity - first_today
                        today_pnl_pct = (total_equity / first_today - 1) * 100 if first_today != 0 else 0
                    else:
                        # Use last point vs previous
                        if len(self.portfolio.equity_history) >= 2:
                            prev = float(self.portfolio.equity_history[-2].total_equity)
                            today_pnl = total_equity - prev
                            today_pnl_pct = (total_equity / prev - 1) * 100 if prev != 0 else 0
            except Exception as exc:
                logger.debug("Today PnL calc failed: %s", exc)

            return {
                "total_equity": round(total_equity, 2),
                "cash": round(cash, 2),
                "position_value": round(position_value, 2),
                "today_pnl": round(today_pnl, 2),
                "today_pnl_pct": round(today_pnl_pct, 2),
                "total_pnl": round(total_pnl, 2),
                "total_pnl_pct": round(total_pnl_pct, 2),
                "initial_capital": round(initial, 2),
                "status": getattr(self.portfolio, "status", "active"),
            }

        except Exception as exc:
            logger.warning("Failed to get portfolio overview: %s", exc)
            return {"total_equity": 0, "cash": 0, "position_value": 0, "error": str(exc)}

    # -- open positions ----------------------------------------------------

    def get_open_positions(self) -> List[Dict[str, Any]]:
        """Open positions table."""
        if self.portfolio is None or not hasattr(self.portfolio, "positions"):
            return []

        positions = []

        try:
            for symbol, pos in self.portfolio.positions.items():
                try:
                    qty = float(getattr(pos, "quantity", 0) or 0)
                    entry_price = float(getattr(pos, "average_entry_price", 0) or 0)
                    current_price = float(getattr(pos, "current_price", entry_price) or entry_price)
                    unrealized = float(getattr(pos, "unrealized_pnl", 0) or 0)
                    market_value = float(getattr(pos, "market_value", 0) or 0)

                    unrealized_pct = (unrealized / (abs(qty) * entry_price) * 100) if qty != 0 and entry_price != 0 else 0

                    # Position age
                    age_minutes = None
                    age_str = "unknown"
                    try:
                        opened = getattr(pos, "opened_at", None)
                        if opened:
                            if isinstance(opened, str):
                                opened = datetime.fromisoformat(opened)
                            if opened.tzinfo is None:
                                opened = opened.replace(tzinfo=timezone.utc)
                            delta = datetime.now(timezone.utc) - opened
                            age_minutes = int(delta.total_seconds() / 60)
                            if age_minutes < 60:
                                age_str = f"{age_minutes}m"
                            elif age_minutes < 60 * 24:
                                age_str = f"{age_minutes // 60}h {age_minutes % 60}m"
                            else:
                                age_str = f"{age_minutes // (60*24)}d"
                    except Exception:
                        pass

                    positions.append(
                        {
                            "symbol": symbol,
                            "quantity": qty,
                            "position_type": getattr(pos, "position_type", "long" if qty > 0 else "short"),
                            "entry_price": round(entry_price, 2),
                            "current_price": round(current_price, 2),
                            "market_value": round(market_value, 2),
                            "unrealized_pnl": round(unrealized, 2),
                            "unrealized_pnl_pct": round(unrealized_pct, 2),
                            "age_minutes": age_minutes,
                            "age": age_str,
                            "position_id": getattr(pos, "position_id", ""),
                        }
                    )
                except Exception as exc:
                    logger.debug("Failed to process position %s: %s", symbol, exc)
                    continue

            # Sort by unrealized PnL descending
            positions.sort(key=lambda x: x["unrealized_pnl"], reverse=True)

            return positions

        except Exception as exc:
            logger.warning("Failed to get open positions: %s", exc)
            return []

    # -- recent trades -----------------------------------------------------

    def get_recent_trades(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Recent trades table (last N trades)."""
        if self.portfolio is None:
            return []

        try:
            # Try trade_analyzer first
            if self.trade_analyzer and hasattr(self.trade_analyzer, "get_trades"):
                trades = self.trade_analyzer.get_trades()
                analyzed = [self.trade_analyzer.analyze_trade(t) for t in trades[-limit:]]
                # Sort by exit time descending
                analyzed_sorted = sorted(analyzed, key=lambda x: x.exit_time or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
                result = []
                for trade in analyzed_sorted[:limit]:
                    result.append(
                        {
                            "trade_id": trade.trade_id,
                            "symbol": trade.symbol,
                            "direction": trade.direction,
                            "quantity": float(trade.quantity),
                            "entry_price": float(trade.entry_price),
                            "exit_price": float(trade.exit_price),
                            "gross_pnl": float(trade.gross_pnl),
                            "net_pnl": float(trade.net_pnl),
                            "return_pct": float(trade.return_pct) if trade.return_pct else 0,
                            "holding_period_minutes": trade.holding_period_minutes,
                            "holding_category": trade.holding_category,
                            "exit_reason": trade.exit_reason,
                            "entry_time": trade.entry_time.isoformat() if trade.entry_time else None,
                            "exit_time": trade.exit_time.isoformat() if trade.exit_time else None,
                            "is_winner": trade.net_pnl > ZERO,
                        }
                    )
                return result

            # Fallback to portfolio.closed_positions
            if hasattr(self.portfolio, "closed_positions") and self.portfolio.closed_positions:
                closed = self.portfolio.closed_positions[-limit:]
                result = []
                for pos in reversed(closed):
                    try:
                        pnl = float(getattr(pos, "realized_pnl", 0) or 0)
                        result.append(
                            {
                                "symbol": getattr(pos, "symbol", "UNKNOWN"),
                                "quantity": float(getattr(pos, "quantity", 0) or 0),
                                "entry_price": float(getattr(pos, "average_entry_price", 0) or 0),
                                "exit_price": float(getattr(pos, "current_price", 0) or 0),
                                "net_pnl": pnl,
                                "is_winner": pnl > 0,
                                "exit_time": getattr(pos, "closed_at", None).isoformat() if getattr(pos, "closed_at", None) else None,
                            }
                        )
                    except Exception:
                        continue
                return result

            return []

        except Exception as exc:
            logger.warning("Failed to get recent trades: %s", exc)
            return []

    # -- performance charts ------------------------------------------------

    def get_equity_curve(self, limit: int = 100) -> Dict[str, List[Any]]:
        """Equity curve data for line chart."""
        if self.portfolio is None or not hasattr(self.portfolio, "equity_history"):
            return {"timestamps": [], "equity": [], "cash": [], "position_value": []}

        try:
            history = self.portfolio.equity_history[-limit:]

            timestamps = []
            equity = []
            cash = []
            position_value = []

            for point in history:
                try:
                    timestamps.append(point.ts.isoformat())
                    equity.append(float(point.total_equity))
                    cash.append(float(point.cash))
                    position_value.append(float(point.position_value))
                except Exception:
                    continue

            return {
                "timestamps": timestamps,
                "equity": equity,
                "cash": cash,
                "position_value": position_value,
            }

        except Exception as exc:
            logger.warning("Failed to get equity curve: %s", exc)
            return {"timestamps": [], "equity": []}

    def get_daily_pnl(self, limit: int = 30) -> Dict[str, List[Any]]:
        """Daily P&L bar chart data."""
        if self.portfolio is None or not hasattr(self.portfolio, "equity_history"):
            return {"dates": [], "pnl": []}

        try:
            # Group equity by day and calculate daily PnL
            from collections import defaultdict

            daily_equity = defaultdict(list)

            for point in self.portfolio.equity_history:
                try:
                    day = point.ts.date()
                    daily_equity[day].append(float(point.total_equity))
                except Exception:
                    continue

            dates = []
            pnls = []

            sorted_days = sorted(daily_equity.keys())[-limit:]

            prev_close = None
            for day in sorted_days:
                equities = daily_equity[day]
                if not equities:
                    continue
                day_close = equities[-1]
                if prev_close is not None:
                    pnl = day_close - prev_close
                else:
                    # First day vs initial capital
                    initial = float(getattr(self.portfolio, "initial_capital", day_close) or day_close)
                    pnl = day_close - initial

                dates.append(str(day))
                pnls.append(round(pnl, 2))
                prev_close = day_close

            return {"dates": dates, "pnl": pnls}

        except Exception as exc:
            logger.warning("Failed to get daily PnL: %s", exc)
            return {"dates": [], "pnl": []}

    def get_drawdown_chart(self, limit: int = 100) -> Dict[str, List[Any]]:
        """Drawdown chart data."""
        if self.portfolio is None or not hasattr(self.portfolio, "equity_history"):
            return {"timestamps": [], "drawdown_pct": [], "drawdown": []}

        try:
            history = self.portfolio.equity_history[-limit:]

            timestamps = []
            drawdown_pct = []
            drawdown_abs = []

            peak = 0

            for point in history:
                try:
                    equity = float(point.total_equity)
                    if equity > peak:
                        peak = equity

                    dd_abs = peak - equity
                    dd_pct = (dd_abs / peak * 100) if peak != 0 else 0

                    timestamps.append(point.ts.isoformat())
                    drawdown_pct.append(round(dd_pct, 2))
                    drawdown_abs.append(round(dd_abs, 2))
                except Exception:
                    continue

            return {"timestamps": timestamps, "drawdown_pct": drawdown_pct, "drawdown": drawdown_abs}

        except Exception as exc:
            logger.warning("Failed to get drawdown chart: %s", exc)
            return {"timestamps": [], "drawdown_pct": []}

    def get_win_loss_ratio(self) -> Dict[str, Any]:
        """Win/loss ratio pie chart data."""
        if self.portfolio is None:
            return {"winning": 0, "losing": 0, "win_rate": 0}

        try:
            if self.trade_analyzer and hasattr(self.trade_analyzer, "get_trades"):
                trades = self.trade_analyzer.get_trades()
                winning = sum(1 for t in trades if float(getattr(t, "realized_pnl", 0) or 0) > 0)
                losing = sum(1 for t in trades if float(getattr(t, "realized_pnl", 0) or 0) < 0)
                total = winning + losing
                win_rate = winning / total * 100 if total > 0 else 0

                return {"winning": winning, "losing": losing, "win_rate": round(win_rate, 1), "total": total}

            if hasattr(self.portfolio, "closed_positions"):
                winning = sum(1 for p in self.portfolio.closed_positions if float(getattr(p, "realized_pnl", 0) or 0) > 0)
                losing = sum(1 for p in self.portfolio.closed_positions if float(getattr(p, "realized_pnl", 0) or 0) < 0)
                total = winning + losing
                win_rate = winning / total * 100 if total > 0 else 0
                return {"winning": winning, "losing": losing, "win_rate": round(win_rate, 1), "total": total}

            return {"winning": 0, "losing": 0, "win_rate": 0, "total": 0}

        except Exception as exc:
            logger.warning("Failed to get win/loss ratio: %s", exc)
            return {"winning": 0, "losing": 0, "win_rate": 0}

    # -- active orders -----------------------------------------------------

    def get_active_orders(self) -> List[Dict[str, Any]]:
        """Active (pending) orders."""
        if self.portfolio is None or not hasattr(self.portfolio, "pending_orders"):
            return []

        try:
            orders = []

            for order in self.portfolio.pending_orders:
                try:
                    orders.append(
                        {
                            "order_id": getattr(order, "order_id", ""),
                            "symbol": getattr(order, "symbol", "UNKNOWN"),
                            "side": str(getattr(order, "side", "unknown")),
                            "order_type": str(getattr(order, "order_type", "unknown")),
                            "quantity": float(getattr(order, "quantity", 0) or 0),
                            "filled_quantity": float(getattr(order, "filled_quantity", 0) or 0),
                            "remaining_quantity": float(getattr(order, "remaining_quantity", 0) or 0),
                            "limit_price": float(getattr(order, "limit_price", 0) or 0) if getattr(order, "limit_price", None) else None,
                            "status": str(getattr(order, "status", "unknown")),
                            "submitted_at": getattr(order, "submitted_at", None).isoformat() if getattr(order, "submitted_at", None) else None,
                            "strategy_name": getattr(order, "strategy_name", None),
                        }
                    )
                except Exception as exc:
                    logger.debug("Failed to process order %s: %s", getattr(order, "order_id", "?"), exc)
                    continue

            return orders

        except Exception as exc:
            logger.warning("Failed to get active orders: %s", exc)
            return []

    # -- key metrics -------------------------------------------------------

    def get_key_metrics(self) -> Dict[str, Any]:
        """Key metrics panel: trades today, win rate, Sharpe, drawdown, exposure."""
        metrics = {
            "total_trades_today": 0,
            "win_rate": 0,
            "sharpe_ratio": 0,
            "max_drawdown": 0,
            "max_drawdown_pct": 0,
            "current_exposure": 0,
            "current_exposure_pct": 0,
        }

        try:
            # Performance metrics
            if self.performance and hasattr(self.performance, "get_metrics"):
                perf = self.performance.get_metrics()
                metrics["sharpe_ratio"] = round(float(perf.get("sharpe_ratio", 0) or 0), 2)
                metrics["max_drawdown"] = round(float(perf.get("max_drawdown", 0) or 0), 2)
                metrics["max_drawdown_pct"] = round(float(perf.get("max_drawdown_pct", 0) or perf.get("max_drawdown_percentage", 0) or 0) * 100, 2)
                metrics["win_rate"] = round(float(perf.get("win_rate", 0) or 0) * 100, 1)
                metrics["total_trades_today"] = perf.get("total_trades", 0) or 0

            # Exposure from portfolio
            if self.portfolio and hasattr(self.portfolio, "get_current_exposure"):
                exposure = self.portfolio.get_current_exposure()
                metrics["current_exposure"] = round(float(exposure.get("gross_exposure", 0) or 0), 2)
                metrics["current_exposure_pct"] = round(float(exposure.get("gross_exposure_pct", 0) or 0) * 100, 2)

            # Trades today from trade analyzer or closed_positions
            if self.portfolio and hasattr(self.portfolio, "closed_positions"):
                today = date.today()
                today_trades = 0
                for pos in self.portfolio.closed_positions:
                    try:
                        closed_at = getattr(pos, "closed_at", None)
                        if closed_at:
                            if isinstance(closed_at, str):
                                closed_at = datetime.fromisoformat(closed_at)
                            if closed_at.date() == today:
                                today_trades += 1
                    except Exception:
                        continue
                metrics["total_trades_today"] = today_trades

            return metrics

        except Exception as exc:
            logger.warning("Failed to get key metrics: %s", exc)
            return metrics

    # -- system status -----------------------------------------------------

    def get_system_status(self) -> Dict[str, Any]:
        """System status: data connection, strategy status, last update, health."""
        status = {
            "market_data_connected": False,
            "strategy_status": "unknown",
            "last_data_update": None,
            "system_health": "unknown",
            "loop_count": 0,
            "error_count": 0,
            "is_halted": False,
        }

        try:
            if self.data_handler:
                if hasattr(self.data_handler, "is_connected"):
                    status["market_data_connected"] = self.data_handler.is_connected()
                if hasattr(self.data_handler, "get_stats"):
                    stats = self.data_handler.get_stats()
                    status["market_data_stats"] = stats

            if self.portfolio:
                status["strategy_status"] = getattr(self.portfolio, "status", "active")

            if self.engine:
                if hasattr(self.engine, "get_status"):
                    engine_status = self.engine.get_status()
                    status["loop_count"] = engine_status.get("loop_count", 0)
                    status["error_count"] = engine_status.get("error_count", 0)
                    status["is_halted"] = engine_status.get("is_halted", False) if "is_halted" in engine_status else False
                    # Last update from equity history
                    if self.portfolio and hasattr(self.portfolio, "equity_history") and self.portfolio.equity_history:
                        last_point = self.portfolio.equity_history[-1]
                        status["last_data_update"] = last_point.ts.isoformat() if hasattr(last_point, "ts") else None

            # System health
            if status["error_count"] == 0:
                status["system_health"] = "healthy"
            elif status["error_count"] < 3:
                status["system_health"] = "warning"
            else:
                status["system_health"] = "critical"

            if status["is_halted"]:
                status["system_health"] = "halted"

            return status

        except Exception as exc:
            logger.warning("Failed to get system status: %s", exc)
            return status

    # -- combined ----------------------------------------------------------

    def get_all_dashboard_data(self) -> Dict[str, Any]:
        """Get all dashboard data in one call."""
        return {
            "portfolio_overview": self.get_portfolio_overview(),
            "open_positions": self.get_open_positions(),
            "recent_trades": self.get_recent_trades(),
            "equity_curve": self.get_equity_curve(),
            "daily_pnl": self.get_daily_pnl(),
            "drawdown_chart": self.get_drawdown_chart(),
            "win_loss_ratio": self.get_win_loss_ratio(),
            "active_orders": self.get_active_orders(),
            "key_metrics": self.get_key_metrics(),
            "system_status": self.get_system_status(),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
