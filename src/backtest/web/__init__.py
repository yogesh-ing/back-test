"""Unified web application for the trading bot platform.

PRD §3 — a single Flask app hosting the Dashboard / Backtest / Compare /
Forward pages, all sharing the strategy registry and data sources.

For now (Epic 1) it mounts the REST API blueprints and a health check; the page
routes and templates land in Epic 2-4.
"""

from backtest.web.app import create_app, run_app

__all__ = ["create_app", "run_app"]
