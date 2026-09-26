"""REST API blueprints for the unified trading bot platform.

Blueprints:
* ``strategies_bp``  — strategy catalogue + dynamic param schemas
* ``backtest_bp``    — single backtest (``/api/backtest/run``) and
  parallel multi-slot backtest (``/api/backtest/run-many``)
* ``forward_bp``     — forward paper-trading (``/api/forward/start|stop|status``)
* ``broker_auth_bp`` — generic broker authentication
  (``/api/broker/login|verify-totp|status|logout``), auth epic Task 2.1
* ``portfolio_bp``   — multi-strategy portfolio command center
  (``/api/portfolio/*`` + SSE stream), forward-testing multi-strategy epic
* ``intelligence_bp`` — portfolio Greeks/concentration/correlation, market
  regime and the alert lifecycle (``/api/alerts/*``)

Mounted by :func:`backtest.web.app.create_app`.
"""

from backtest.api.analytics import analytics_bp
from backtest.api.backtest import backtest_bp
from backtest.api.broker_auth import broker_auth_bp
from backtest.api.data_manager import data_bp
from backtest.api.forward import forward_bp
from backtest.api.intelligence import intelligence_bp
from backtest.api.playbooks import playbooks_bp
from backtest.api.portfolio import portfolio_bp
from backtest.api.strategies import strategies_bp
from backtest.api.symbols import symbols_bp

__all__ = [
    "analytics_bp",
    "strategies_bp",
    "symbols_bp",
    "backtest_bp",
    "forward_bp",
    "broker_auth_bp",
    "data_bp",
    "portfolio_bp",
    "intelligence_bp",
    "playbooks_bp",
]
