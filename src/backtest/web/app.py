"""Unified Flask application factory.

Binds to ``0.0.0.0`` and accepts the Arena preview host so it can run as a live
preview. The default data source is ``synthetic`` (override via the
``BACKTEST_SOURCE`` config key or ``--source`` flag: synthetic | csv | mstock).
"""

from __future__ import annotations

import logging
import os
from typing import Any

from flask import Flask, jsonify, render_template

from backtest.api import backtest_bp, forward_bp, strategies_bp

logger = logging.getLogger("backtest.web.app")

_HERE = os.path.dirname(os.path.abspath(__file__))
_TEMPLATE_DIR = os.path.join(_HERE, "templates")
_STATIC_DIR = os.path.join(_HERE, "static")


def create_app(source: str = "synthetic", **overrides: Any) -> Flask:
    """Create the unified Flask app.

    Parameters
    ----------
    source:
        Default candle data source for backtest endpoints
        (``synthetic`` | ``csv`` | ``mstock``).
    """
    app = Flask(
        __name__,
        template_folder=_TEMPLATE_DIR,
        static_folder=_STATIC_DIR,
    )
    app.config["BACKTEST_SOURCE"] = source
    app.config.update(overrides)

    app.register_blueprint(strategies_bp)
    app.register_blueprint(backtest_bp)
    app.register_blueprint(forward_bp)

    # ------------------------------------------------------------------
    # Page routes
    # ------------------------------------------------------------------
    @app.get("/")
    def index() -> Any:
        return render_template("backtest.html", active="backtest")

    @app.get("/backtest")
    def backtest_page() -> Any:
        return render_template("backtest.html", active="backtest")

    @app.get("/dashboard")
    def dashboard_page() -> Any:
        return render_template("dashboard.html", active="dashboard")

    @app.get("/compare")
    def compare_page() -> Any:
        return render_template("compare.html", active="compare")

    @app.get("/forward")
    def forward_page() -> Any:
        return render_template("forward.html", active="forward")

    @app.get("/health")
    def health() -> tuple:
        return jsonify({"status": "ok", "source": app.config["BACKTEST_SOURCE"]}), 200

    return app


def run_app(
    host: str = "0.0.0.0",
    port: int = 5000,
    source: str = "synthetic",
    debug: bool = False,
) -> None:
    app = create_app(source=source)
    logger.warning(
        "Starting unified app at http://%s:%s (preview: https://%s-{sandboxId}.e2b.app)",
        host,
        port,
        port,
    )
    app.run(host=host, port=port, debug=debug, use_reloader=False)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Unified Trading Bot Platform")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--source", default="synthetic", help="synthetic | csv | mstock")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    run_app(host=args.host, port=args.port, source=args.source, debug=args.debug)


if __name__ == "__main__":
    main()
