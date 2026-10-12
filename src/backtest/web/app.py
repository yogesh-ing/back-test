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
    analytics_bp,
    backtest_bp,
    broker_auth_bp,
    data_bp,
    forward_bp,
    intelligence_bp,
    portfolio_bp,
    reporting_bp,
    settings_bp,
    strategies_bp,
)
from backtest.api.data_guard import data_source_status
from backtest.api.optimize import optimize_bp
from backtest.pine.api import pine_bp
from backtest.api.playbooks import playbooks_bp
from backtest.api.segments import segments_bp
from backtest.api.portfolio import _default_runner_source, list_instances
from backtest.api.symbols import symbols_bp
from backtest.brokers.session_manager import get_session_manager
from backtest.data.source_tags import SOURCE_TAG_VALUES, app_source_tag
from backtest.data.sources_policy import build_policy
from backtest.logging_config import (
    bind_request_id,
    configure_logging,
    current_request_id,
    get_logger,
    reset_request_id,
)
from backtest.simulator.bucket_risk import BUCKET_RISK_LIMITS

logger = get_logger(__name__)


def _default_source_tag() -> str:
    """The source the dropdowns pre-select — derived, never a literal.

    A page that opens on a source its own policy would refuse is a form whose
    first submission is an error, so the selection follows exactly what the
    runner API picks when the client names nothing
    (:func:`backtest.api.portfolio._default_runner_source`), mapped into the
    run-taxonomy vocabulary the selects are built from.
    """
    return app_source_tag(_default_runner_source())


#: Re-exported for /api/config so the UI can show what the active source honours.
try:
    from backtest.api.backtest import SUPPORTED_TIMEFRAMES as _SUPPORTED_TIMEFRAMES
except Exception:  # pragma: no cover - defensive: never break boot on a constant
    _SUPPORTED_TIMEFRAMES = ("1day",)

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
    if len(raw) <= 3 and not raw.isalpha():  # a symbol: "Rp", "R$", "$"
        return {"code": "CUSTOM", "symbol": raw, "locale": "en-US"}
    logger.warning(
        "unknown currency %r — falling back to INR (known: %s)",
        raw,
        ", ".join(sorted(CURRENCY_PRESETS)),
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
            logger.log(
                logging.DEBUG if quiet else logging.INFO,
                "→ %s %s%s",
                request.method,
                request.path,
                query,
            )
            if not quiet:
                logger.debug(
                    "→ client=%s agent=%s",
                    request.remote_addr,
                    (request.headers.get("User-Agent") or "-")[:60],
                )

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
            suffix = (
                f" [req={current_request_id()}]"
                if (response.status_code >= 400 and current_request_id())
                else ""
            )
            logger.log(
                level,
                "← %s %s %s in %.1f ms%s",
                request.method,
                request.path,
                response.status_code,
                ms,
                suffix,
            )
        else:
            logger.debug(
                "← %s %s %s in %.1f ms", request.method, request.path, response.status_code, ms
            )
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
            logger.error(
                "request blew up: %s %s [req=%s] — %s",
                request.method,
                request.path,
                current_request_id() or "-",
                exc,
                exc_info=exc,
            )
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
        logger.exception(
            "unhandled error on %s %s [req=%s]",
            request.method,
            request.path,
            current_request_id() or "-",
        )
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


def _portfolio_buckets() -> dict[str, dict[str, Any]]:
    """Per-bucket (paper/live) aggregates for the /portfolio landing page.

    Each bucket lists its own instances only, so the landing shows a
    combined summary without one bucket leaking into the other (P4.1).

    Ticket #10: the bucket keys come from the canonical ``BUCKET_RISK_LIMITS``
    map (the same source the templates render their mode selects from) — the
    vocabulary is never re-declared here.
    """
    buckets: dict[str, dict[str, Any]] = {}
    for mode in BUCKET_RISK_LIMITS:
        rows = list_instances(mode)
        buckets[mode] = {
            "count": len(rows),
            "allocated": round(sum(r.get("allocated_capital", 0.0) for r in rows), 2),
            "equity": round(sum(r.get("equity", 0.0) for r in rows), 2),
            "daily_pnl": round(sum(r.get("daily_pnl", 0.0) for r in rows), 2),
            "realized_pnl": round(sum(r.get("realized_pnl", 0.0) for r in rows), 2),
        }
    return buckets


def _warn_if_source_disabled(policy: Any, source: str) -> None:
    """Say it at startup, not at the first failed request.

    A deployment that boots onto a disabled source is misconfigured; the app
    still serves pages and the refusal explains itself, but nobody should have
    to run a backtest to discover it.
    """
    if not policy.is_enabled(source):
        logger.error(
            "[data-policy] source=%s is DISABLED — backtests and optimizations will be "
            "refused with 409. %s",
            source,
            policy.refusal_for(source),
        )


#: Preference order for the automatic fallback. ``db`` first (real, certifiable,
#: already on disk), then the broker feeds, then CSV. Synthetic is deliberately
#: NOT in this list: an operator who disabled it did so on purpose, and silently
#: serving generated candles as a "fallback" would defeat the whole policy.
_FALLBACK_PREFERENCE = ("db", "mstock", "dhan", "csv")


def resolve_source(policy: Any, requested: str) -> str:
    """The source the app will actually run on.

    PRD change of direction (2026-09-30): a disabled configured source used to
    block the Backtest and Optimize tabs behind a ⛔ banner. That punished the
    ordinary case — synthetic disabled on purpose, real sources enabled — for
    nothing. When the requested source is disabled the app now falls back to
    the best *enabled* one (``_FALLBACK_PREFERENCE`` order), logs the
    substitution loudly, and the UI's source badge simply says what is in use.

    Only an explicit request for a specific source keeps its refusal meaning:
    if every fallback candidate is also disabled, the requested source is kept
    and the 409 path still guards it — the app refuses to run rather than
    pretending a source is fine.

    An empty request (no ``$BACKTEST_SOURCE``) is not a request for synthetic;
    it means "whatever the config enables", resolved in the same order.
    """
    requested = str(requested or "").strip().lower()
    if policy.is_enabled(requested):
        return requested
    if not requested:
        # Nothing was asked for: take the first source the config enables.
        # Synthetic leads because it is the historical default and the test
        # profile's choice — and it can only be chosen by a deployment that
        # sets ``synthetic: enabled: true``, so production, where the flag is
        # off, lands on db/mstock exactly as before.
        for candidate in ("synthetic",) + _FALLBACK_PREFERENCE:
            if policy.is_enabled(candidate):
                logger.info(
                    "[data-policy] no source requested — running on source=%s "
                    "(enabled sources: %s)",
                    candidate,
                    ", ".join(policy.enabled_names()),
                )
                return candidate
        logger.error(
            "[data-policy] no source requested and NO source is enabled — run "
            "endpoints will refuse with 409."
        )
        return ""
    for candidate in _FALLBACK_PREFERENCE:
        if candidate != requested and policy.is_enabled(candidate):
            logger.warning(
                "[data-policy] requested source=%s is disabled — falling back to "
                "source=%s (enabled sources: %s)",
                requested,
                candidate,
                ", ".join(policy.enabled_names()),
            )
            return candidate
    logger.error(
        "[data-policy] source=%s is disabled and NO enabled fallback exists "
        "(enabled: %s) — run endpoints will refuse with 409.",
        requested,
        ", ".join(policy.enabled_names()) or "none",
    )
    return requested


def create_app(
    source: str = "",
    *,
    log_level: str | int | None = None,
    log_file: str | None = None,
    currency: str | None = None,
    replay_speed: float | None = None,
    data_source_policy: Any | None = None,
    **overrides: Any,
) -> Flask:
    """Create the unified Flask app.

    Parameters
    ----------
    source:
        Candle data source for backtest endpoints (``db`` | ``csv`` | ``mstock``
        | ``synthetic``). Empty — the default — asks the policy to pick, which
        is what keeps ``$BACKTEST_SOURCE`` out of the picture as a hidden
        synthetic switch.
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
    if source == "mock_broker":
        # Gap-PRD P5: literal `--source mock_broker` — zero-credential
        # dry-run. Registers the auto-authenticating MockBroker (orders
        # logged, never submitted) and runs the DATA pipeline as synthetic,
        # so every downstream source tag stays a known value.
        from backtest.brokers.session_manager import enable_mock_broker

        enable_mock_broker()
        source = "synthetic"
        logger.warning(
            "[mock-broker] dry-run venue enabled — no credentials required; "
            "orders are LOGGED, never submitted; data source runs synthetic"
        )
    app = Flask(
        __name__,
        template_folder=_TEMPLATE_DIR,
        static_folder=_STATIC_DIR,
    )
    # Resolved once here so a test can substitute a policy without touching the
    # module-level cache, and so the refusal message is identical everywhere.
    # ``data_source_policy`` exists because the resolution two lines down has to
    # use THE SAME policy the request guard will use. A caller that set
    # ``app.config["DATA_SOURCE_POLICY"]`` after this function returned got an
    # app that had already resolved its source against the on-disk profile — so
    # ``BACKTEST_SOURCE`` and the policy disagreed about what the app runs on,
    # and a guard checking the resolved name refused a source the injected
    # policy happily allowed (or, worse, allowed one it did not describe).
    app.config["DATA_SOURCE_POLICY"] = (
        data_source_policy if data_source_policy is not None else build_policy()
    )
    # A disabled configured source falls back to the best enabled one rather
    # than blocking both tabs (see resolve_source). The REQUESTED source is
    # remembered under BACKTEST_SOURCE_REQUESTED so provenance labels can
    # distinguish "asked for synthetic" from "running db instead".
    app.config["BACKTEST_SOURCE_REQUESTED"] = source
    app.config["BACKTEST_SOURCE"] = resolve_source(app.config["DATA_SOURCE_POLICY"], source)
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
        logger.warning(
            "FORWARD_REPLAY_SPEED=%r is not a number — using %s bars/s", speed, DEFAULT_REPLAY_SPEED
        )
        app.config["FORWARD_REPLAY_BARS_PER_SECOND"] = DEFAULT_REPLAY_SPEED
    app.config.update(overrides)

    @app.context_processor
    def inject_globals() -> dict[str, Any]:
        """Available to every template (base.html tags <body> with these).

        Ticket #10: the taxonomy vocabulary is injected here from the
        backend-owned constants, so templates render their mode/source
        choices from the canonical set instead of re-declaring it.
        """
        try:
            from backtest.live.market_status import get_market_day_state

            m_state = get_market_day_state()
        except Exception:  # noqa: BLE001
            m_state = {
                "day_state": "TRADING_DAY",
                "session_state": "OPEN",
                "holiday_name": None,
                "next_open_ts": None,
                "gap_warning": None,
                "is_open": True,
            }

        try:
            from backtest.api.data_manager import get_data_freshness

            d_fresh = get_data_freshness()
        except Exception:  # noqa: BLE001 — the chip degrades, the page must not
            d_fresh = {
                "db_available": False, "fetching": False, "level": "unknown",
                "label": "DATA ?", "title": "Freshness check unavailable",
                "per_timeframe": {}, "target_day": None,
            }

        return {
            "currency_code": app.config["CURRENCY"],
            "currency_symbol": app.config["CURRENCY_SYMBOL"],
            "currency_locale": app.config["CURRENCY_LOCALE"],
            "replay_speed": app.config["FORWARD_REPLAY_BARS_PER_SECOND"],
            "bucket_modes": sorted(BUCKET_RISK_LIMITS),
            "source_tags": sorted(SOURCE_TAG_VALUES),
            "default_source_tag": _default_source_tag(),
            "market_status": m_state,
            "data_freshness": d_fresh,
        }

    _register_request_logging(app)
    _register_error_handlers(app)

    logger.info(
        "app created: source=%s currency=%s(%s) replay_speed=%s bars/s log_level=%s python=%s",
        app.config["BACKTEST_SOURCE"],
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
    app.register_blueprint(intelligence_bp)
    app.register_blueprint(playbooks_bp)
    app.register_blueprint(analytics_bp)
    app.register_blueprint(pine_bp)
    app.register_blueprint(optimize_bp)
    app.register_blueprint(segments_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(reporting_bp)

    # SSE broadcast cadence for the portfolio command center.
    app.config.setdefault("PORTFOLIO_SSE_INTERVAL", 1.0)
    # Portfolio Intelligence: the alert widget renders on every page unless
    # the app was started with --disable-portfolio-intelligence.
    app.config.setdefault("PORTFOLIO_INTELLIGENCE_ENABLED", True)

    # U6.3 / D7: drop-in strategy plugins (plugins/strategies/*.py). Import-
    # clean + conformance-passing files register here; broken files are logged
    # and skipped — never crash the app.
    try:
        from backtest.plugins import discover_plugins

        loaded = discover_plugins()
        if loaded:
            logger.info("plugin strategies loaded: %s", ", ".join(loaded))
    except Exception:  # noqa: BLE001 — plugin loading must never break startup
        logger.exception("strategy plugin discovery failed — continuing without plugins")

    # Broker session expiry monitor (auth epic Task 2.2): idempotent daemon
    # thread, one per process, polls the active broker session every 5 min.
    get_session_manager().start_monitor()

    # PnL-vs-spot end-of-day exporter (2026-09-24): server-side daemon that
    # fires at 15:30 IST even with no browser open (owner is away before
    # close), writing charts/<date>/{PNG,CSV} per live option runner.
    try:
        from backtest.forward.watch_export import MarketCloseExporter

        _watch_exporter = MarketCloseExporter()
        _watch_exporter.start()
        app.config["WATCH_EXPORTER"] = _watch_exporter
    except Exception:  # noqa: BLE001 — the exporter must never break boot
        logger.exception("market-close watch exporter failed to start")

    # ------------------------------------------------------------------
    # Page routes
    # ------------------------------------------------------------------
    @app.get("/")
    def index() -> Any:
        return render_template(
            "backtest.html",
            active="backtest",
            source=app.config["BACKTEST_SOURCE"],
            data_sources=data_source_status(),
        )

    @app.get("/backtest")
    def backtest_page() -> Any:
        return render_template(
            "backtest.html",
            active="backtest",
            source=app.config["BACKTEST_SOURCE"],
            data_sources=data_source_status(),
        )

    @app.get("/api/data-sources")
    def data_sources() -> Any:
        """Which sources this deployment may run on, and why not the others."""
        return jsonify(data_source_status())

    @app.get("/compare")
    def compare_page() -> Any:
        return render_template("compare.html", active="compare")

    @app.get("/analytics")
    def analytics_page() -> Any:
        strategy_id = request.args.get("strategy") or ""
        return render_template("analytics.html", active="analytics", selected_strategy=strategy_id)

    @app.get("/optimize")
    def optimize_page() -> Any:
        """Parameter optimization: setup form + run history."""
        return render_template(
            "optimize.html",
            active="optimize",
            selected_strategy=request.args.get("strategy") or "",
            source=app.config["BACKTEST_SOURCE"],
            data_sources=data_source_status(),
        )

    @app.get("/optimize/runs/<run_id>")
    @app.get("/optimize/runs/<run_id>/progress")
    @app.get("/optimize/runs/<run_id>/results")
    def optimize_run_page(run_id: str) -> Any:
        """One run: live progress while running, results dashboard after."""
        return render_template("optimize_run.html", active="optimize", run_id=run_id)

    @app.get("/strategy-builder")
    def strategy_builder_page() -> Any:
        """Pine Script → Python strategy converter UI."""
        return render_template("strategy_builder.html", active="strategy_builder")

    @app.get("/forward")
    def forward_page() -> Any:
        return render_template("forward.html", active="forward")

    @app.get("/portfolio")
    def portfolio_page() -> Any:
        """Landing: combined summary of the paper + live buckets (P4.1)."""
        return render_template("portfolio.html", active="portfolio", buckets=_portfolio_buckets())

    @app.get("/portfolio/paper")
    def portfolio_paper_page() -> Any:
        """Paper bucket only — live instances never render here (P4.1)."""
        return render_template(
            "portfolio_paper.html",
            active="portfolio",
            mode="paper",
            instances=list_instances("paper"),
        )

    @app.get("/portfolio/live")
    def portfolio_live_page() -> Any:
        """Live bucket only — paper instances never render here (P4.1)."""
        return render_template(
            "portfolio_live.html",
            active="portfolio",
            mode="live",
            instances=list_instances("live"),
        )

    @app.get("/risk")
    def risk_page() -> Any:
        return render_template("risk.html", active="risk")

    @app.get("/settings")
    def settings_page() -> Any:
        return render_template("settings.html", active="settings")

    @app.get("/data")
    def data_page() -> Any:
        return render_template("data_manager.html", active="data")

    @app.get("/reporting")
    def reporting_page() -> Any:
        """Consolidated P&L / tax report (PRD-002)."""
        return render_template("reporting.html", active="reporting")

    # Options trading dashboard (Phase 7) — page + JSON API
    from backtest.web.options_api import register_options_routes
    register_options_routes(app)

    @app.get("/api/config")
    def config_view() -> tuple:
        """What the UI needs to know about this deployment (no secrets)."""
        return (
            jsonify(
                {
                    "source": app.config["BACKTEST_SOURCE"],
                    "currency": {
                        "code": app.config["CURRENCY"],
                        "symbol": app.config["CURRENCY_SYMBOL"],
                        "locale": app.config["CURRENCY_LOCALE"],
                    },
                    "forward_replay_bars_per_second": app.config["FORWARD_REPLAY_BARS_PER_SECOND"],
                    "strategies_supported_timeframes": list(_SUPPORTED_TIMEFRAMES),
                    # Ticket #10 — the single vocabulary the UI must render its
                    # mode/source choices from (backend-owned, never re-declared).
                    "taxonomy": {
                        "modes": sorted(BUCKET_RISK_LIMITS),
                        "sources": sorted(SOURCE_TAG_VALUES),
                    },
                    "log_level": logging.getLevelName(
                        logging.getLogger("backtest").getEffectiveLevel()
                    ),
                }
            ),
            200,
        )

    @app.get("/health")
    def health() -> tuple:
        return jsonify({"status": "ok", "source": app.config["BACKTEST_SOURCE"]}), 200

    return app


def start_portfolio_intelligence(enabled: bool = True, interval: "float | None" = None) -> None:
    """Start the alert evaluator thread + alert/history persistence.

    Also starts the outbound notifier (Telegram/email) — it subscribes to the
    alert broker directly and is deliberately started *before* the
    intelligence toggle below: breaker-trip alerts are safety messages and
    must leave the process even when the intelligence layer is disabled.

    Tests never call this (they drive ``evaluate()`` directly). Fail-soft: a
    broken intelligence layer logs and leaves trading untouched.
    """
    try:
        from backtest.alerts.notifier import start_alert_notifier

        start_alert_notifier()
    except Exception:  # noqa: BLE001 — notifications are best-effort
        logger.exception("[alerts] outbound notifier failed to start")
    try:
        from backtest.forward.portfolio_manager import get_portfolio_manager

        intel = getattr(get_portfolio_manager(), "intelligence", None)
        if intel is None:
            logger.warning("[intelligence] unavailable — alerts disabled")
            return
        intel.enabled = bool(enabled)
        if not enabled:
            logger.info("[intelligence] disabled (--disable-portfolio-intelligence)")
            return
        try:
            from backtest.db import DatabaseManager
            from backtest.intelligence.persistence import IntelligencePersister

            db = DatabaseManager.from_env()
            is_sqlite = str(db.config.url or "").startswith("sqlite")
            intel.attach_persister(IntelligencePersister(db, ensure_schema=is_sqlite))
        except Exception:  # noqa: BLE001 — history is best-effort
            logger.info("[intelligence] alert persistence disabled (no database)", exc_info=True)
        try:
            from backtest.options.chain_snapshots import add_snapshot_listener

            add_snapshot_listener(
                lambda underlying, rows, ts: intel.ingest_chain(
                    underlying, rows, ts=ts, source="chain_snapshot"
                )
            )
        except Exception:  # noqa: BLE001
            logger.debug("[intelligence] chain snapshot hook unavailable", exc_info=True)
        intel.start(interval)
    except Exception:  # noqa: BLE001
        logger.exception("[intelligence] failed to start — trading unaffected")


def run_app(
    host: str = "0.0.0.0",
    port: int = 5000,
    source: str = "",
    debug: bool = False,
    log_level: "str | int | None" = None,
    log_file: "str | None" = None,
    currency: "str | None" = None,
    replay_speed: "float | None" = None,
    enable_portfolio_intelligence: bool = True,
    alert_refresh_interval: "float | None" = None,
) -> None:
    """Boot the app and serve it. Unset options fall back to their env vars."""
    app = create_app(
        source=source,
        log_level=log_level,
        log_file=log_file,
        currency=currency,
        replay_speed=replay_speed,
        debug=debug,
    )
    app.config["PORTFOLIO_INTELLIGENCE_ENABLED"] = bool(enable_portfolio_intelligence)
    start_portfolio_intelligence(enable_portfolio_intelligence, alert_refresh_interval)
    routes = sorted(str(r) for r in app.url_map.iter_rules())
    logger.info(
        "serving %d routes on http://%s:%s — %d api endpoints",
        len(routes),
        host,
        port,
        sum(1 for r in routes if r.startswith("/api/")),
    )
    if debug:
        logger.warning("Flask debug mode is ON — reloader disabled, do not use in production")
    logger.debug("routes: %s", ", ".join(routes))
    app.run(host=host, port=port, debug=debug, use_reloader=False)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Unified Trading Bot Platform")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument(
        "--source",
        default=os.getenv("BACKTEST_SOURCE", ""),
        choices=["", "synthetic", "csv", "mstock", "dhan", "db", "mock_broker"],
        help=(
            "Data source: synthetic | csv | mstock | dhan | db | mock_broker. "
            "Empty (the default) lets config/data_sources.yaml choose, so no "
            "source hides in a CLI default; a disabled choice falls back per "
            "the data policy. (mock_broker = synthetic data + zero-credential "
            "dry-run broker, Gap-PRD P5)"
        ),
    )
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
    parser.add_argument(
        "--enable-portfolio-intelligence",
        dest="portfolio_intelligence",
        action="store_true",
        default=os.getenv("PORTFOLIO_INTELLIGENCE", "1").strip().lower()
        not in {"0", "false", "no", "off"},
        help="portfolio Greeks/regime analytics + alerts (default ON; env: PORTFOLIO_INTELLIGENCE)",
    )
    parser.add_argument(
        "--disable-portfolio-intelligence",
        dest="portfolio_intelligence",
        action="store_false",
        help="turn the alert evaluator and alert widget off",
    )
    parser.add_argument(
        "--alert-refresh-interval",
        type=float,
        default=float(os.getenv("ALERT_REFRESH_INTERVAL", "1.0")),
        help="seconds between alert-rule evaluations (env: ALERT_REFRESH_INTERVAL)",
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
        enable_portfolio_intelligence=args.portfolio_intelligence,
        alert_refresh_interval=args.alert_refresh_interval,
    )


if __name__ == "__main__":
    main()
