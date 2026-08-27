"""Unified web application for the trading bot platform.

PRD §3 — a single Flask app hosting the Dashboard / Backtest / Compare /
Forward pages, all sharing the strategy registry and data sources.
"""

# Lazy imports: app and run_app are accessed directly via
# ``python -m backtest.web.app``, so we must not eagerly import them
# here -- doing so causes a RuntimeWarning on module execution.


def __getattr__(name: str):
    if name in ("create_app", "run_app"):
        from backtest.web.app import create_app, run_app

        globals()["create_app"] = create_app
        globals()["run_app"] = run_app
        return globals()[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["create_app", "run_app"]
