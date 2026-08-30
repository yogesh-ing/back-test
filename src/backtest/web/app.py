"""Unified Flask application factory.

Binds to ``0.0.0.0`` and accepts the Arena preview host so it can run as a live
preview. The default data source is ``synthetic`` (override via the
``BACKTEST_SOURCE`` config key or ``--source`` flag: synthetic | csv | mstock | db).

Logging: :func:`create_app` installs the project-wide handlers
(:mod:`backtest.logging_config`) and wraps every request with a request id, so
``BACKTEST_LOG_LEVEL=DEBUG`` (or ``--log-level DEBUG``) shows the full
data-source → strategy → engine → adapter path for one HTTP call.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any

from flask import Flask, g, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

from backtest.api import (
    backtest_bp,
    broker_auth_bp,
    data_bp,
    forward_bp,
    portfolio_bp,
    strategies_bp,
)
from backtest.api.symbols import symbols_bp
from backtest.brokers.session_manager import get_session_manager
from backtest.logging_config import (
    bind_request_id,
    configure_logging,
    current_request_id,
    get_logger,
    reset_request_id,
)

logger = get_logger(__name__)

#: Re-exported for /api/config so the UI can show what the active source honours.
try:
    from backtest.api.backtest import SUPPORTED_TIMEFRAMES as _SUPPORTED_TIMEFRAMES
except Exception:  # pragma: no cover - defensive: never break boot on a constant
    _SUPPORTED_TIMEFRAMES = ("1D",)

_HERE = os.path.dirname(os.path.abspath(__file__))
_TEMPLATE_DIR = os.path.join(_HERE, "templates")
_STATIC_DIR = os.path.join(_HERE, "static")


#: Polled by the UI every 1–2 s; their request lines are DEBUG-only so an INFO
#: log stays readable while a forward test or the portfolio grid is running.
QUIET_PATHS = frozenset(
    {
        "/api/forward/status",
        "/api/broker/status",
        "/api/portfolio/summary",
        "/api/portfolio/stream",
        "/health",
    }
)


#: Currency presets. Default is ₹ because this platform trades NSE via mStock;
#: the old UI mixed `$` (Backtest/Compare) with `₹` (Forward) for the same number.
CURRENCY_PRESETS: dict[str, dict[str, str]] = {
    "INR": {"symbol": "₹", "locale": "en-IN"},
    "USD": {"symbol": "$", "locale": "en-US"},
    "EUR": {"symbol": "€", "locale": "de-DE"},
    "GBP": {"symbol": "£", "locale": "en-GB"},
    "JPY": {"symbol": "¥", "locale": "ja-JP"},
    "AUD": {"symbol": "A$", "locale": "en-AU"},
    "SGD": {"symbol": "S$", "locale": "en-SG"},
}

#: Replay clock: bars revealed per second by a running forward session.
DEFAULT_REPLAY_SPEED = 1.0


def _resolve_currency(value: "str | None") -> dict[str, str]:
    """Map ``--currency``/``BACKTEST_CURRENCY`` (a code or a bare symbol) to config."""
    raw = (value or os.getenv("BACKTEST_CURRENCY") or "INR").strip()
    preset = CURRENCY_PRESETS.get(raw.upper())
    if preset:
        return {"code": raw.upper(), **preset}
    if len(raw) <= 3 and not raw.isalpha():      # a symbol: "Rp", "R$", "$"
        return {"code": "CUSTOM", "symbol": raw, "locale": "en-US"}
    logger.warning(
        "unknown currency %r — falling back to INR (known: %s)",
        raw, ", ".join(sorted(CURRENCY_PRESETS)),
    )
    return {"code": "INR", **CURRENCY_PRESETS["INR"]}


def _register_request_logging(app: Flask) -> None:
    """Give every request an id + a start/finish log line, and log failures.

    ``X-Request-Id`` is echoed on every response (and accepted inbound, so a
    curl -H can be grepped in the server log). For ``/api/*`` errors the id is
    also injected into the JSON body, which lets the UI toast quote it.
    """

    @app.before_request
    def _open() -> None:
        g._t0 = time.perf_counter()
        rid, token = bind_request_id(request.headers.get("X-Request-Id"))
        g.request_id = rid
        g._log_token = token
        if request.path.startswith("/api/"):
            quiet = request.path in QUIET_PATHS
            query = f"?{request.query_string.decode()}" if request.query_string else ""
            logger.log(logging.DEBUG if quiet else logging.INFO, "→ %s %s%s",
                       request.method, request.path, query)
            if not quiet:
                logger.debug("→ client=%s agent=%s", request.remote_addr,
                             (request.headers.get("User-Agent") or "-")[:60])

    @app.after_request
    def _close(response):  # noqa: ANN001 - Flask passes the Response
        ms = (time.perf_counter() - getattr(g, "_t0", time.perf_counter())) * 1000
        response.headers["X-Request-Id"] = getattr(g, "request_id", "-")
        if request.path.startswith("/api/") or response.status_code >= 400:
            if response.status_code >= 400:
                level = logging.WARNING
            elif request.path in QUIET_PATHS:
                level = logging.DEBUG
            else:
                level = logging.INFO
            suffix = f" [req={current_request_id()}]" if (
                response.status_code >= 400 and current_request_id()) else ""
            logger.log(level, "← %s %s %s in %.1f ms%s", request.method, request.path,
                       response.status_code, ms, suffix)
        else:
            logger.debug("← %s %s %s in %.1f ms", request.method, request.path,
                         response.status_code, ms)
        if response.status_code >= 400 and request.path.startswith("/api/"):
            try:
                payload = response.get_json(silent=True)
                if isinstance(payload, dict) and "request_id" not in payload:
                    payload["request_id"] = current_request_id()
                    response.set_data(json.dumps(payload))
            except Exception:  # noqa: BLE001 - never let logging break a response
                logger.debug("could not enrich error body", exc_info=True)
        return response

    @app.teardown_request
    def _cleanup(exc: BaseException | None) -> None:
        if exc is not None:
            # The id is inside the message as well as the prefix, so it survives
            # any formatter (journald, pytest's capture, a custom handler).
            logger.error("request blew up: %s %s [req=%s] — %s", request.method, request.path,
                         current_request_id() or "-", exc, exc_info=exc)
        reset_request_id(getattr(g, "_log_token", None))


def _register_error_handlers(app: Flask) -> None:
    """Turn unhandled exceptions into JSON (with a traceback in the log).

    Before this, a 500 from ``/api/*`` produced an HTML Flask traceback page and
    nothing on the console beyond a request line. HTTPExceptions (404, 405, …)
    are passed through untouched so normal routing behaviour is preserved.
    """

    @app.errorhandler(Exception)
    def _unhandled(exc: Exception) -> Any:
        if isinstance(exc, HTTPException):
            return exc
        logger.exception("unhandled error on %s %s [req=%s]", request.method, request.path,
                         current_request_id() or "-")
        if request.path.startswith("/api/"):
            return (
                jsonify(
                    {
                        "error": f"internal error: {exc.__class__.__name__}: {exc}",
                        "request_id": current_request_id(),
                    }
                ),
                500,
            )
        return "Internal error — see server log", 500


def create_app(
    source: str = "synthetic",
    *,
    log_level: str | int | None = None,
    log_file: str | None = None,
    currency: str | None = None,
    replay_speed: float | None = None,
    **overrides: Any,
) -> Flask:
    """Create the unified Flask app.

    Parameters
    ----------
    source:
        Default candle data source for backtest endpoints
        (``synthetic`` | ``csv`` | ``mstock`` | ``db``).
    log_level:
        Logging level for the whole process; ``None`` → ``$BACKTEST_LOG_LEVEL``
        → INFO. ``log_file`` (``None`` → ``$BACKTEST_LOG_FILE``) mirrors output
        to a file.
    currency:
        ISO code (INR, USD, …) or a bare symbol, used by every money display in
        the UI. ``None`` → ``$BACKTEST_CURRENCY`` → INR.
    replay_speed:
        Bars per second a forward-test replay advances at, on the server clock.
        ``0`` freezes it (manual stepping). ``None`` → ``$FORWARD_REPLAY_SPEED``
        → 1.0.
    """
    configure_logging(log_level, log_file)
    app = Flask(
        __name__,
        template_folder=_TEMPLATE_DIR,
        static_folder=_STATIC_DIR,
    )
    app.config["BACKTEST_SOURCE"] = source
    money = _resolve_currency(currency)
    app.config["CURRENCY"] = money["code"]
    app.config["CURRENCY_SYMBOL"] = money["symbol"]
    app.config["CURRENCY_LOCALE"] = money["locale"]
    speed = replay_speed
    if speed is None:
        speed = os.getenv("FORWARD_REPLAY_SPEED", DEFAULT_REPLAY_SPEED)
    try:
        app.config["FORWARD_REPLAY_BARS_PER_SECOND"] = max(0.0, float(speed))
    except (TypeError, ValueError):
        logger.warning("FORWARD_REPLAY_SPEED=%r is not a number — using %s bars/s",
                       speed, DEFAULT_REPLAY_SPEED)
        app.config["FORWARD_REPLAY_BARS_PER_SECOND"] = DEFAULT_REPLAY_SPEED
    app.config.update(overrides)

    @app.context_processor
    def inject_globals() -> dict[str, Any]:
        """Available to every template (base.html tags <body> with these)."""
        return {
            "currency_code": app.config["CURRENCY"],
            "currency_symbol": app.config["CURRENCY_SYMBOL"],
            "currency_locale": app.config["CURRENCY_LOCALE"],
            "replay_speed": app.config["FORWARD_REPLAY_BARS_PER_SECOND"],
        }

    _register_request_logging(app)
    _register_error_handlers(app)

    logger.info(
        "app created: source=%s currency=%s(%s) replay_speed=%s bars/s log_level=%s python=%s",
        source,
        app.config["CURRENCY"],
        app.config["CURRENCY_SYMBOL"],
        app.config["FORWARD_REPLAY_BARS_PER_SECOND"],
        logging.getLevelName(logging.getLogger("backtest").getEffectiveLevel()),
        sys.version.split()[0],
    )

    if source == "db":
        try:
            from backtest.data.db_source import DbSource
            _src = DbSource()
            syms = _src.list_symbols()
            logger.info("[DB] %d symbols available in market_data_cache", len(syms))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[DB] Could not connect to database: %s", exc, exc_info=True)
    elif source in ("synthetic", "csv"):
        logger.warning(
            "[data] source=%s ignores the requested timeframe (daily bars only) — "
            "1D/1H/4H/1W will produce identical candles (see gap G6)",
            source,
        )

    app.register_blueprint(strategies_bp)
    app.register_blueprint(symbols_bp)
    app.register_blueprint(backtest_bp)
    app.register_blueprint(forward_bp)
    app.register_blueprint(broker_auth_bp)
    app.register_blueprint(data_bp)
    app.register_blueprint(portfolio_bp)

    # SSE broadcast cadence for the portfolio command center.
    app.config.setdefault("PORTFOLIO_SSE_INTERVAL", 1.0)

    # Broker session expiry monitor (auth epic Task 2.2): idempotent daemon
    # thread, one per process, polls the active broker session every 5 min.
    get_session_manager().start_monitor()

    # ------------------------------------------------------------------
    # Page routes
    # ------------------------------------------------------------------
    @app.get("/")
    def index() -> Any:
        return render_template("backtest.html", active="backtest", source=app.config.get("BACKTEST_SOURCE", "synthetic"))

    @app.get("/backtest")
    def backtest_page() -> Any:
        return render_template("backtest.html", active="backtest", source=app.config.get("BACKTEST_SOURCE", "synthetic"))

    @app.get("/dashboard")
    def dashboard_page() -> Any:
        return render_template("dashboard.html", active="dashboard")

    @app.get("/compare")
    def compare_page() -> Any:
        return render_template("compare.html", active="compare")

    @app.get("/forward")
    def forward_page() -> Any:
        return render_template("forward.html", active="forward")

    @app.get("/portfolio")
    def portfolio_page() -> Any:
        return render_template("portfolio.html", active="portfolio")

    @app.get("/data")
    def data_page() -> Any:
        return render_template("data_manager.html", active="data")

    @app.get("/api/config")
    def config_view() -> tuple:
        """What the UI needs to know about this deployment (no secrets)."""
        return jsonify({
            "source": app.config["BACKTEST_SOURCE"],
            "currency": {
                "code": app.config["CURRENCY"],
                "symbol": app.config["CURRENCY_SYMBOL"],
                "locale": app.config["CURRENCY_LOCALE"],
            },
            "forward_replay_bars_per_second": app.config["FORWARD_REPLAY_BARS_PER_SECOND"],
            "strategies_supported_timeframes": list(_SUPPORTED_TIMEFRAMES),
            "log_level": logging.getLevelName(
                logging.getLogger("backtest").getEffectiveLevel()
            ),
        }), 200

    @app.get("/health")
    def health() -> tuple:
        return jsonify({"status": "ok", "source": app.config["BACKTEST_SOURCE"]}), 200

    return app


def run_app(
    host: str = "0.0.0.0",
    port: int = 5000,
    source: str = "synthetic",
    debug: bool = False,
    log_level: "str | int | None" = None,
    log_file: "str | None" = None,
    currency: "str | None" = None,
    replay_speed: "float | None" = None,
) -> None:
    """Boot the app and serve it. Unset options fall back to their env vars."""
    app = create_app(source=source, log_level=log_level, log_file=log_file,
                     currency=currency, replay_speed=replay_speed, debug=debug)
    routes = sorted(str(r) for r in app.url_map.iter_rules())
    logger.info("serving %d routes on http://%s:%s — %d api endpoints",
                len(routes), host, port,
                sum(1 for r in routes if r.startswith("/api/")))
    if debug:
        logger.warning("Flask debug mode is ON — reloader disabled, do not use in production")
    logger.debug("routes: %s", ", ".join(routes))
    app.run(host=host, port=port, debug=debug, use_reloader=False)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Unified Trading Bot Platform")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--source", default="synthetic", choices=["synthetic", "csv", "mstock", "db"], help="Data source: synthetic | csv | mstock | db")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument(
        "--log-level",
        default=os.getenv("BACKTEST_LOG_LEVEL", "INFO"),
        help="DEBUG | INFO | WARNING | ERROR (env: BACKTEST_LOG_LEVEL)",
    )
    parser.add_argument(
        "--currency",
        default=os.getenv("BACKTEST_CURRENCY", "INR"),
        help="Money display for every page: INR (default, ₹) | USD | EUR | GBP | "
             "JPY | AUD | SGD, or a bare symbol (env: BACKTEST_CURRENCY)",
    )
    parser.add_argument(
        "--replay-speed",
        type=float,
        default=float(os.getenv("FORWARD_REPLAY_SPEED", "1.0")),
        help="Bars per second a forward-test replay advances at; 0 = manual "
             "(env: FORWARD_REPLAY_SPEED)",
    )
    parser.add_argument(
        "--log-file",
        default=os.getenv("BACKTEST_LOG_FILE"),
        help="also append every log line to this file (env: BACKTEST_LOG_FILE)",
    )
    args = parser.parse_args()
    run_app(
        host=args.host,
        port=args.port,
        source=args.source,
        debug=args.debug,
        log_level=args.log_level,
        log_file=args.log_file,
        currency=args.currency,
        replay_speed=args.replay_speed,
    )


if __name__ == "__main__":
    main()
