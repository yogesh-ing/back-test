"""WSGI entry point — production boot without the Flask dev server.

Dev (any OS)::

    python -m backtest.web.app --source synthetic

Prod (Linux)::

    gunicorn -c gunicorn.conf.py backtest.web.wsgi:app

Boots exactly what ``run_app`` boots, minus ``app.run``: ``create_app`` from
environment variables, then the portfolio-intelligence evaluator + alert
persistence. Environment honoured (same variables as the dev entry point):

* ``BACKTEST_SOURCE`` — synthetic | csv | mstock | dhan | db (default synthetic)
* ``BACKTEST_LOG_LEVEL`` / ``BACKTEST_LOG_FILE``
* ``BACKTEST_CURRENCY`` / ``FORWARD_REPLAY_SPEED``
* ``PORTFOLIO_INTELLIGENCE`` — 0/false/no/off disables the alert evaluator
* ``ALERT_REFRESH_INTERVAL`` — seconds between alert-rule evaluations

Worker model: see ``gunicorn.conf.py`` — **workers=1, preload off**, because
live trading state (runners, broker session, alert broker, feed registry)
lives in this process.
"""

from __future__ import annotations

import os

from backtest.web.app import create_app, start_portfolio_intelligence


def _portfolio_intelligence_enabled() -> bool:
    value = os.getenv("PORTFOLIO_INTELLIGENCE", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _alert_refresh_interval() -> float:
    try:
        return float(os.getenv("ALERT_REFRESH_INTERVAL", "1.0"))
    except (TypeError, ValueError):
        return 1.0


app = create_app(source=os.getenv("BACKTEST_SOURCE", ""))
app.config["PORTFOLIO_INTELLIGENCE_ENABLED"] = _portfolio_intelligence_enabled()
start_portfolio_intelligence(
    app.config["PORTFOLIO_INTELLIGENCE_ENABLED"], _alert_refresh_interval()
)
