"""REST API blueprints for the unified trading bot platform (PRD Tasks 1.3/1.5/1.6/4.3).

Blueprints:
* ``strategies_bp`` — strategy catalogue + dynamic param schemas
* ``backtest_bp``   — single backtest (``/api/backtest/run``) and
  parallel multi-slot backtest (``/api/backtest/run-many``)
* ``forward_bp``    — forward paper-trading (``/api/forward/start|stop|status``)

Mounted by :func:`backtest.web.app.create_app`.
"""

from backtest.api.backtest import backtest_bp
from backtest.api.forward import forward_bp
from backtest.api.strategies import strategies_bp

__all__ = ["strategies_bp", "backtest_bp", "forward_bp"]
