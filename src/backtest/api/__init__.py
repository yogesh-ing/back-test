"""REST API blueprints for the unified trading bot platform.

Blueprints:
* ``strategies_bp``  — strategy catalogue + dynamic param schemas
* ``backtest_bp``    — single backtest (``/api/backtest/run``) and
  parallel multi-slot backtest (``/api/backtest/run-many``)
* ``forward_bp``     — forward paper-trading (``/api/forward/start|stop|status``)
* ``broker_auth_bp`` — generic broker authentication
  (``/api/broker/login|verify-totp|status|logout``), auth epic Task 2.1

Mounted by :func:`backtest.web.app.create_app`.
"""

from backtest.api.backtest import backtest_bp
from backtest.api.broker_auth import broker_auth_bp
from backtest.api.data_manager import data_bp
from backtest.api.forward import forward_bp
from backtest.api.strategies import strategies_bp
from backtest.api.symbols import symbols_bp

__all__ = ["strategies_bp", "symbols_bp", "backtest_bp", "forward_bp", "broker_auth_bp", "data_bp"]
