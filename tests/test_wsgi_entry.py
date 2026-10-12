"""WSGI entry point (gunicorn) — the production boot path.

The entry must build the same app the dev server (``run_app``) builds:
env-driven ``create_app`` + the portfolio-intelligence evaluator. Intelligence
is disabled here (``PORTFOLIO_INTELLIGENCE=0``) so booting the entry point in a
test never attaches the alert persister to a real database.
"""

from __future__ import annotations

import importlib
import sys

from flask import Flask


def test_wsgi_entry_boots_and_serves(monkeypatch):
    monkeypatch.setenv("BACKTEST_SOURCE", "synthetic")
    monkeypatch.setenv("PORTFOLIO_INTELLIGENCE", "0")
    sys.modules.pop("backtest.web.wsgi", None)  # fresh import → fresh boot

    mod = importlib.import_module("backtest.web.wsgi")

    assert isinstance(mod.app, Flask)
    assert mod.app.config["PORTFOLIO_INTELLIGENCE_ENABLED"] is False
    assert mod.app.test_client().get("/health").status_code == 200
