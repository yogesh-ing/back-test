"""Data Manager API endpoints.

Endpoints for fetching historical market data from mStock and storing in PostgreSQL.
Runs fetch jobs as background threads with progress tracking.

* POST /api/data/fetch       — Start a fetch job
* GET  /api/data/status       — Get current fetch job status + progress
* POST /api/data/stop         — Stop the current fetch job
* GET  /api/data/inventory    — Show what data is available in DB per symbol
* GET  /api/data/coverage     — Every KNOWN instrument + whether it has bars
                               (PRD backTest-enhance §1.3) — the one endpoint
                               the Backtest, Compare and Optimize symbol
                               pickers read.
"""

from __future__ import annotations

import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from typing import Any

import requests
from flask import Blueprint, jsonify, request
from sqlalchemy import create_engine, text

from backtest.data.base import CANONICAL_TIMEFRAMES, MSTOCK_INTERVAL_MAP, bar_timestamp
from backtest.data.coverage import (
    INDEX_UNIVERSE,
    INSTRUMENT_TYPES,
    NO_DATA_HINT,
    classify_instrument,
    load_equity_universe,
)
from backtest.db.config import get_db_url
from backtest.logging_config import get_logger

data_bp = Blueprint("data_api", __name__)
log = get_logger(__name__)

# -----------------------------------------------------------------------
# Fetch job state (single job at a time)
# -----------------------------------------------------------------------

#: Fetch scope when the picker has nothing ticked — mirrors the Data tab's
#: All/Equity/Index tabs (``DM_TABS`` in data_manager.js). The scope picks
#: the CURATED universe (NIFTY 200 + built-in indices), never the raw
#: 15k-row scriptmaster catalogue: an unticked list used to mean "every
#: NSE/BSE equity" (2026-10-02: a job walked into BSE bond ``001HCCL29``
#: while the UI promised "all NIFTY 200 stocks").
FETCH_SCOPES = ("equity,index", "equity", "index")
DEFAULT_FETCH_SCOPE = "equity,index"

_lock = threading.Lock()
_job: dict[str, Any] = {
    "status": "idle",  # idle | running | done | error
    "symbol": "",  # current symbol being fetched
    "fetched": 0,  # symbols completed
    "total": 0,  # total symbols to fetch
    "bars_total": 0,  # total bars inserted
    "bars_symbol": 0,  # bars for current symbol
    "failed": 0,  # symbols that failed
    "failed_list": [],  # list of (symbol, error) tuples
    "from_date": "",
    "to_date": "",
    "timeframe": "1min",
    "scope": DEFAULT_FETCH_SCOPE,  # what an unticked list resolves to
    "error": None,  # last error message
    "started_at": None,
    "elapsed": "",
    "cancel": False,  # flag to stop the job
    # Chunk-level progress for the symbol being fetched. Without these the
    # bar only ever moved once per symbol (6-8 min of "frozen" progress at
    # 1-minute granularity — it looked hung, 2026-10-02).
    "chunk_done": 0,
    "chunk_total": 0,
    # Chunks lost across the whole job AFTER retries. Before this field the
    # UI reported "done, 0 failed" while mStock 502s silently ate ~half the
    # chunk requests (ALKEM 2026-10-03: 19 of ~41 chunks stored).
    "chunk_errors": 0,
    # Symbols skipped because the requested range is already fully in the DB
    # (coverage-aware gap fill, 2026-10-03). Counted separately so a resume
    # over the same range reads as "N skipped, 0 fetched", not a hung job.
    "skipped": 0,
}

# Single DB-URL authority (ticket P4.3): FORWARD_TEST_DB_URL env >
# config/database.yaml profile — no private env reading or hard-coded URLs.
DB_URL = get_db_url()
MSTOCK_BASE_URL = os.getenv("MSTOCK_BASE_URL", "https://api.mstock.trade").rstrip("/")

# mStock API interval mapping — the shared canonical -> TypeA wire map
# (ticket P4.3: one translation, one place).
_MSTOCK_INTERVAL_MAP = MSTOCK_INTERVAL_MAP

CHUNK_DAYS_MAP = {
    "1day": 800,
    "1min": 2,
    "5min": 10,
    "15min": 30,
    "1hour": 120,
}

# Between-chunk pacing for the historical fetch loop (2026-10-03). mStock's
# gateway 502s a large share of requests fired at the old 0.15 s cadence, but
# the retries behind ``_get_historical_with_retry`` only pay cost on failure —
# so base the loop at 0.5 s and widen to 2.0 s when a symbol hits sustained
# trouble, decaying back once three chunks land clean.
CHUNK_SLEEP_BASE = 0.5
CHUNK_SLEEP_WIDE = 2.0
CHUNK_PACE_UP_AFTER = 3  # consecutive post-retry failures -> widen
CHUNK_PACE_DOWN_AFTER = 3  # consecutive successes -> return to base
# Chunk requests in flight per symbol. The throttle ceiling is the broker
# session, not the CPU: 4 workers × (0.7 s RTT + 0.5 s pacing) ≈ 2.5-3 req/s,
# ~3× the single-threaded loop, while 6-8 workers would just convert idle
# headroom into concurrent 502 storms.
CHUNK_FETCH_WORKERS = 4

#: Circuit-breaker thresholds (see :class:`_CircuitBreaker`): the job aborts
#: only when this many post-retry chunk failures land back-to-back AND the
#: whole streak spans at least ``BREAKER_STALL_SECONDS`` with no clean chunk.
BREAKER_MIN_FAILURES = 8
BREAKER_STALL_SECONDS = 180.0


class FetchCancelled(RuntimeError):
    """Raised inside a chunk worker when Stop was pressed (or the breaker
    tripped) while it was mid-retry — distinguishes "we chose to stop" from
    "mStock failed the chunk", so cancelled windows are not counted as errors."""


class _CircuitBreaker:
    """Job-level detector for "mStock is fully down" (operator report, 2026-10-03).

    Adaptive pacing (``CHUNK_PACE_*``) is built for BAD PATCHES: the gateway
    502s for a while and recovers, so the loop widens its sleep and presses
    on. A full outage looks identical chunk-by-chunk — every request dies
    after its retries — yet pressing on means crawling through all 200 symbols
    collecting errors for days when the user asked it to "fetch for a while,
    then stop". The breaker trips once ``min_failures`` consecutive post-retry
    chunk failures have sustained for ``stall_seconds`` without a single clean
    chunk; one recovered chunk resets the streak, so transient storms never
    abort a run that was about to succeed.
    """

    def __init__(
        self,
        min_failures: int = BREAKER_MIN_FAILURES,
        stall_seconds: float = BREAKER_STALL_SECONDS,
        clock=time.monotonic,
    ) -> None:
        self._lock = threading.Lock()
        self._min_failures = min_failures
        self._stall_seconds = stall_seconds
        self._clock = clock
        self._consec_fails = 0
        self._streak_started = 0.0
        self._failed_at = 0.0
        self._tripped = False

    def record(self, ok: bool) -> None:
        """Fold one post-retry chunk outcome into the streak."""
        with self._lock:
            if ok:
                self._consec_fails = 0
                return
            now = self._clock()
            if self._consec_fails == 0:
                self._streak_started = now
            self._consec_fails += 1
            if (
                not self._tripped
                and self._consec_fails >= self._min_failures
                and now - self._streak_started >= self._stall_seconds
            ):
                self._tripped = True
                self._failed_at = now
                log.error(
                    "[data] mStock circuit breaker TRIPPED: %d consecutive chunk "
                    "failures over %.1f min — the job will stop after this symbol",
                    self._consec_fails,
                    (now - self._streak_started) / 60.0,
                )

    @property
    def tripped(self) -> bool:
        with self._lock:
            return self._tripped

    @property
    def consecutive_failures(self) -> int:
        with self._lock:
            return self._consec_fails

    @property
    def stall_minutes(self) -> float:
        with self._lock:
            end = self._failed_at or self._clock()
            return (end - self._streak_started) / 60.0


# -----------------------------------------------------------------------
# API Endpoints
# -----------------------------------------------------------------------


@data_bp.get("/api/data/status")
def fetch_status() -> tuple:
    """Return current fetch job status."""
    with _lock:
        snapshot = dict(_job)
    return jsonify(snapshot), 200


@data_bp.post("/api/data/stop")
def fetch_stop() -> tuple:
    """Signal the running job to stop. Lands between chunks — within seconds
    in a healthy session; up to one in-flight request (60 s socket timeout)
    when mStock is hanging. Bars collected for the current symbol are stored
    before the job exits."""
    with _lock:
        if _job["status"] != "running":
            return jsonify({"error": "No job running"}), 400
        _job["cancel"] = True
    return jsonify({"status": "stopping"}), 200


@data_bp.post("/api/data/clear")
def fetch_clear() -> tuple:
    """Reset a finished job's state back to idle.

    A completed/cancelled/failed fetch used to leave its progress panel up
    forever — even across page reloads — because the job state itself still
    said ``done``/``error`` and every page load re-showed it. Clearing
    restores the idle defaults so the Data tab can dismiss the panel for
    good. A RUNNING job is refused: stop it first (2026-10-04).
    """
    with _lock:
        if _job["status"] == "running":
            return (
                jsonify({"error": "A fetch job is running. Stop it before clearing."}),
                409,
            )
        _job.update(
            status="idle",
            symbol="",
            fetched=0,
            total=0,
            bars_total=0,
            bars_symbol=0,
            failed=0,
            failed_list=[],
            from_date="",
            to_date="",
            timeframe="1min",
            error=None,
            started_at=None,
            elapsed="",
            cancel=False,
            chunk_done=0,
            chunk_total=0,
            chunk_errors=0,
            skipped=0,
        )
    return jsonify({"status": "idle"}), 200


@data_bp.post("/api/data/fetch")
def fetch_start() -> tuple:
    """Start a background fetch job.

    Body: { symbols?: string[], scope?: 'equity,index'|'equity'|'index',
            timeframe?: string, from_date?: string, to_date?: string }

    ``symbols`` wins when present. With nothing ticked, ``scope`` selects
    the curated universe to fetch (:data:`FETCH_SCOPES`) — the Data tab
    sends its active All/Equity/Index tab.
    """
    with _lock:
        if _job["status"] == "running":
            return jsonify({"error": "A fetch job is already running. Stop it first."}), 409

    data = request.get_json(silent=True) or {}
    timeframe = data.get("timeframe", "1min")
    from_date = data.get("from_date", "2024-01-01")
    to_date = data.get("to_date", date.today().isoformat())
    symbols = data.get("symbols")  # None/empty = the curated universe for `scope`

    scope = data.get("scope") or DEFAULT_FETCH_SCOPE
    if scope not in FETCH_SCOPES:
        return (
            jsonify(
                {
                    "error": (
                        f"Unsupported scope: {scope}. "
                        f"Use one of: {list(FETCH_SCOPES)}"
                    )
                }
            ),
            400,
        )

    if timeframe not in _MSTOCK_INTERVAL_MAP:
        return (
            jsonify(
                {
                    "error": (
                        f"Unsupported timeframe: {timeframe}. "
                        f"Use: {list(_MSTOCK_INTERVAL_MAP.keys())}"
                    )
                }
            ),
            400,
        )

    # Resolve the session token: prefer the live Broker session (the same
    # one the chain/quote paths use), fall back to the persisted token file
    # at the PROJECT ROOT. The old `os.getcwd()` lookup broke whenever the
    # app was launched from src/ (cwd ≠ project root) → phantom
    # "No auth token" despite an active login (fixed 2026-09-22).
    token: str | None = None
    try:
        from backtest.brokers.session_manager import get_session_manager

        token = get_session_manager().get_active_session_token()
    except Exception:  # noqa: BLE001 — fall through to the file
        token = None
    if not token:
        token_file = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            os.pardir,
            ".mstock_session_token",
        )
        token_file = os.path.normpath(token_file)
        if os.path.exists(token_file):
            with open(token_file) as f:
                token = f.read().strip()
    if not token or len(token) < 16:
        return jsonify(
            {"error": "No auth token. Please authenticate via the Broker button first."}
        ), 401

    # Reset job state
    with _lock:
        _job.update(
            status="running",
            symbol="",
            fetched=0,
            total=0,
            bars_total=0,
            bars_symbol=0,
            failed=0,
            failed_list=[],
            from_date=from_date,
            to_date=to_date,
            timeframe=timeframe,
            scope=scope,
            error=None,
            started_at=time.time(),
            elapsed="",
            cancel=False,
            chunk_done=0,
            chunk_total=0,
            chunk_errors=0,
            skipped=0,
        )

    # Launch background thread
    thread = threading.Thread(
        target=_run_fetch_job,
        args=(token, timeframe, from_date, to_date, symbols, scope),
        daemon=True,
    )
    thread.start()

    return (
        jsonify(
            {
                "status": "started",
                "timeframe": timeframe,
                "scope": scope,
                "from_date": from_date,
                "to_date": to_date,
            }
        ),
        200,
    )


@data_bp.get("/api/data/inventory")
def inventory() -> tuple:
    """Per-symbol data availability, served from the CACHED coverage report.

    This endpoint used to run its own ``GROUP BY symbol, timeframe`` scan — the
    same query ``load_bar_coverage`` runs for the coverage report. The Data tab
    loads both at once, so opening one page scanned the whole cache twice to
    render two views of identical data (~1.05s against a 1M-bar cache).

    It now reads the report that is already cached and reshapes it. The row set
    is identical — a symbol appears here exactly when it has bars — because
    this filters the report down to ``data_available`` rows.

    It asks for the UNIVERSE shape (``include_catalogue=False``) because that is
    what the Data tab's symbol list requests, so the two share one cache entry
    and one aggregate. Asking for the curated shape instead would miss that
    entry and re-scan — exactly the waste this rewrite exists to remove.

    Response shape is UNCHANGED (``{symbols: {SYM: [{timeframe, bars,
    earliest, latest}]}, total_symbols, total_bars}``): it is a published
    contract and other tooling may read it, so the payoff for changing it is
    not worth the breakage.
    """
    try:
        report = _coverage_report(include_catalogue=False)
    except Exception as exc:  # noqa: BLE001 — an inventory view must not 500 the tab
        return jsonify({"error": str(exc)}), 500

    symbols: dict[str, list] = {}
    for row in report.instruments:
        if not row.get("data_available"):
            continue  # the old query only ever returned symbols with bars
        stored = row.get("timeframes_stored") or []
        dates = row.get("dates_by_timeframe") or {}
        bars = row.get("bars_by_timeframe") or {}
        symbols[row["symbol"]] = [
            {
                "timeframe": tf,
                "bars": bars.get(tf, 0),
                "earliest": (dates.get(tf) or {}).get("from"),
                "latest": (dates.get(tf) or {}).get("to"),
            }
            for tf in stored
        ]

    return (
        jsonify(
            {
                "symbols": symbols,
                "total_symbols": len(symbols),
                "total_bars": sum(s["bars"] for slist in symbols.values() for s in slist),
            }
        ),
        200,
    )


# -----------------------------------------------------------------------
# Coverage (PRD backTest-enhance §1.3)
# ---------------------------------------------------------------------------

#: Process-level cache of the coverage report. The bar aggregate is one
#: grouped scan over market_data_cache, and three pages mount a picker at
#: once, so without this one page load costs three identical scans. Short TTL
#: because the whole point of the endpoint is that it changes when a fetch
#: job finishes.
_COVERAGE_TTL_SECONDS = 60.0
_coverage_lock = threading.Lock()
_coverage_cache: dict[str, Any] = {"at": 0.0, "report": None}


def _coverage_cache_key(names_only: bool, curate_only: bool, include_catalogue: bool = True) -> str:
    """Which SHAPE of answer a cached report holds.

    Keyed by mode, because the three modes return different things and one
    report must not answer another's request. Serving a names-only report to a
    caller that asked for coverage would tell the Data tab that nothing has
    data; serving a curated-only report to an unfiltered caller would silently
    drop every scriptmaster symbol; serving a full report to the picker would
    work but re-introduce the seconds we just removed.
    """
    if names_only:
        return "names"
    if curate_only:
        return "curated"
    return "full" if include_catalogue else "universe"


def invalidate_coverage_cache() -> None:
    """Drop the cached report (a fetch job just wrote new bars)."""
    with _coverage_lock:
        _coverage_cache.clear()
        _coverage_cache["at"] = 0.0
        _coverage_cache["report"] = None
    # ...and the plain symbol listing, which had NO expiry at all: /api/symbols
    # cached its first answer into symbols._CACHED_SYMBOLS and never cleared it,
    # so the Forward page kept showing the symbol set from boot and a symbol
    # fetched minutes ago stayed invisible until the process restarted. Cleared
    # here because this function is what the fetch job already calls when new
    # bars land — the two caches describe the same fact and must expire together.
    try:
        from backtest.api import symbols as symbols_api

        symbols_api._CACHED_SYMBOLS.clear()
    except Exception as exc:  # noqa: BLE001 — cache housekeeping must never raise
        log.debug("[coverage] could not clear the symbol listing cache: %s", exc)
    # Freshness watches the same MAX(ts::date) the fetch moves — refresh it
    # together so the topbar chip flips right when the job finishes.
    with _freshness_lock:
        _freshness_cache["at"] = 0.0
        _freshness_cache["payload"] = None


def _coverage_report(
    refresh: bool = False,
    names_only: bool = False,
    curate_only: bool = False,
    include_catalogue: bool = True,
):
    """Build (or reuse) the coverage report. Never raises.

    ``names_only`` returns the static instrument list — symbols and names, no
    question asked about what is stored. It exists because the caller that
    needs only names was paying for the whole answer: on a cache holding 216
    symbols against a 140k-row scriptmaster catalogue, the full report costs
    ~3.4s, of which the catalogue read is ~1.4s and merging its rows into
    216 usable ones is ~1.8s (280k regex calls to fold NFO contract ids back
    to their underlying). A picker that lists names and asks nothing else was
    therefore waiting three seconds for rows it discards.

    ``curate_only`` skips the catalogue read but still reports real coverage:
    a curated row can never come from the catalogue, so the rows are identical
    and only the 1.4s is saved. Callers that filter to ``curated=1`` should set
    it — otherwise they pay for 140k rows they are about to discard.

    ``include_catalogue=False`` is the Data tab's request: the fetchable
    universe (NIFTY 200 + indices, the only symbols that tab can fetch) UNION
    every symbol that has bars, with real coverage on each row. It is the cheap
    shape — one aggregate, no catalogue read, ~0.28s against a 140k-row
    catalogue — and it is deliberately NOT ``curate_only``, because a symbol
    somebody fetched outside the universe must still be listed and shown.

    Availability is not lost by this — it moves to where it can be answered
    with detail: the backtest request, which fails with the dates the symbol
    really holds instead of a greyed-out option with no explanation.

    Degradation ladder: cached report -> database (bars + catalogue) ->
    shipped universe only. A picker with no database still lists the indices
    and the NIFTY 200, every one of them marked as having no data, which is
    the honest answer rather than an empty dropdown.
    """
    now = time.time()
    key = _coverage_cache_key(names_only, curate_only, include_catalogue)
    with _coverage_lock:
        cached = _coverage_cache.get(key)
        fresh = cached is not None and (now - _coverage_cache["at"]) < _COVERAGE_TTL_SECONDS
    if fresh and not refresh:
        return cached

    from backtest.data.coverage import (
        build_coverage,
        load_cached_symbols,
        load_bar_coverage,
        load_catalogue,
        load_equity_universe,
    )

    bars: dict = {}
    catalogue: list = []
    db_available = False
    warnings: list[str] = []
    engine = None
    if names_only:
        # Names and nothing else. The shipped universe is already in memory
        # (config); the DISTINCT symbols in the cache are added so that a
        # symbol someone fetched outside the NIFTY 200 is still pickable —
        # without them, "fetch it, then choose it" breaks, which is the §1.3
        # disappearance bug in new clothes.
        #
        # What is NOT read: the 140k-row instruments catalogue (only needed for
        # nicer names) and the bar aggregate (only needed for counts, dates and
        # serviceable timeframes — answers nobody asked for here). On a 140k
        # catalogue over a 1M-bar cache this is ~130ms against ~4.5s.
        names = load_equity_universe()
        db_available: bool | None = None
        try:
            engine = create_engine(DB_URL, echo=False)
        except Exception as exc:  # noqa: BLE001 — no database at all
            log.info("[coverage] names-only: no database (%s)", exc.__class__.__name__)
        else:
            try:
                cached = load_cached_symbols(engine)
                if cached is not None:
                    names = names + cached
                    # Asked and answered — but only about which names exist,
                    # which is why available_total still reports "unknown".
                    db_available = True
            finally:
                engine.dispose()
        report = build_coverage(
            bars={},
            catalogue=None,
            universe=names,
            # None, not False: nothing here asked whether the database could
            # answer a coverage question, and the picker renders
            # `db_available === false` as "no data source connected" — a claim
            # this path has not earned. Unknown is the truth.
            db_available=db_available,
            coverage_known=False,
        )
        report.catalogue_source = "builtin"
        report.warnings = []
        log.info("[coverage] names-only: %d instruments from the shipped universe",
                 report.total)
        with _coverage_lock:
            _coverage_cache[key] = report
            _coverage_cache["report"] = report
            _coverage_cache["at"] = now
        return report
    try:
        engine = create_engine(DB_URL, echo=False)
    except Exception as exc:  # noqa: BLE001 — no database at all
        warnings.append(f"no database: {exc.__class__.__name__}")
        log.info("[coverage] database unavailable (%s) — serving the shipped universe",
                 exc.__class__.__name__)
    else:
        try:
            bars = load_bar_coverage(engine)
            db_available = True
        except Exception as exc:  # noqa: BLE001 — no cache table is survivable
            warnings.append(f"no cached bars: {exc.__class__.__name__}")
            log.info("[coverage] market_data_cache unavailable: %s", exc.__class__.__name__)
        # A curated-only caller can never see a catalogue row: `curated` is
        # decided from the shipped universe, never from the 140k scriptmaster
        # contracts, and catalogue rows only ADD symbols. Reading it to discard
        # every row costs ~1.4s on a real database, and the Data tab's picker
        # asked for curated rows on every load. The only thing given up is a
        # nicer NAME for a curated symbol the universe listed bare — which the
        # shipped CSVs already carry.
        if include_catalogue and not curate_only:
            catalogue = load_catalogue(engine)
        engine.dispose()

    report = build_coverage(
        bars=bars,
        catalogue=catalogue,
        universe=load_equity_universe(),
        db_available=db_available,
    )
    if catalogue:
        report.catalogue_source = "instruments"
    elif bars:
        report.catalogue_source = "market_data_cache"
    else:
        report.catalogue_source = "builtin"
    if not include_catalogue and not catalogue:
        # Be explicit that this answer covers the universe + cache only, so a
        # client can never read a missing symbol as "it does not exist".
        report.catalogue_source = ("universe" if not bars else "universe+market_data_cache")
    report.warnings = warnings
    log.info(
        "[coverage] %d instruments (%d with data) from %s",
        report.total,
        sum(1 for r in report.instruments if r["data_available"]),
        report.catalogue_source,
    )
    with _coverage_lock:
        _coverage_cache[key] = report
        _coverage_cache["report"] = report
        _coverage_cache["at"] = now
    return report


@data_bp.get("/api/data/coverage")
def coverage() -> tuple:
    """Every known instrument, and whether choosing it would produce bars.

    Query params:
      * ``q``          - substring match on symbol or name
      * ``types``      - comma list of ``equity``/``index``/``futures``/
                         ``options``/``fno`` (the All/Equity/Index/F&O tabs)
      * ``available``  - ``1`` to list only symbols that have bars
      * ``curated``    - ``1`` to list only the built-in universe (NIFTY 200
                         + indices, human-readable names) — the Data tab's
                         fetch picker
      * ``names_only`` - ``1`` for the STATIC instrument list: symbols and
                         names from the shipped universe, with no question
                         asked about stored bars. ``data_available`` is then
                         ``null`` and every row carries ``coverage_known:
                         false``, so a caller must not read the absence of
                         coverage as absence of data. Availability is answered
                         by the backtest request itself, which fails with the
                         dates the symbol does hold.
      * ``limit``      - page size (default 500, ``0`` = no paging)
      * ``offset``     - page offset
      * ``include_catalogue`` - ``0`` to skip the scriptmaster catalogue: the
                         answer is then the fetchable universe UNION every
                         symbol that has bars. The catalogue contributes ~140k
                         contract rows the Data tab cannot fetch by symbol, and
                         reading them costs ~1.4s (plus ~1.8s to merge) for rows
                         that tab discards. Default ``1`` preserves the full
                         report for any existing caller.
      * ``refresh``    - ``1`` to bypass the short-lived cache

    Rows carry ``data_available``, ``bars_count``, ``from_date``,
    ``to_date`` and ``timeframes_available``; a symbol with no bars carries
    ``hint`` telling the user where to get it (PRD §1.3). With ``available``
    set, ``hidden_total`` also reports how many matching symbols were omitted
    for wanting bars, so the picker can prompt "load data first" instead of
    dropping them silently (issues.txt 2026-10-01).
    """
    from backtest.data.coverage import filter_coverage

    types = [t for t in (request.args.get("types") or "").split(",") if t.strip()]
    raw_limit = request.args.get("limit", "500")
    try:
        limit = max(0, int(raw_limit))
    except (TypeError, ValueError):
        return jsonify({"error": f"limit must be a number, got {raw_limit!r}"}), 400
    try:
        offset = max(0, int(request.args.get("offset", "0") or 0))
    except (TypeError, ValueError):
        return jsonify({"error": "offset must be a number"}), 400

    query = request.args.get("q", "")
    names_only = request.args.get("names_only") in ("1", "true", "yes")
    # `available` and `names_only` contradict each other: one says "filter on
    # the bars", the other says "do not look at the bars". The cheaper request
    # wins, and the response below reports the counts as unknown rather than
    # as zero — a caller that asked not to know must not be told "none".
    available_only = not names_only and request.args.get("available") in ("1", "true", "yes")

    curate_only = request.args.get("curated") in ("1", "true", "yes")
    # Absent means "as before" — only an explicit 0 skips the catalogue, so no
    # existing caller changes behaviour by saying nothing.
    include_catalogue = request.args.get("include_catalogue", "1") not in ("0", "false", "no")
    report = _coverage_report(
        refresh=request.args.get("refresh") in ("1", "true", "yes"),
        names_only=names_only,
        curate_only=curate_only,
        include_catalogue=include_catalogue,
    )
    rows, total = filter_coverage(
        report,
        query=query,
        types=types or None,
        available_only=available_only,
        curated_only=curate_only,
        limit=limit or None,
        offset=offset,
    )
    # issues.txt B1 (2026-10-01): a data-only dropdown must announce what it
    # left out — an unlisted symbol otherwise reads as a symbol that does not
    # exist, which is the §1.3 bug in new clothes. `total` is counted before
    # paging, so limit=1 keeps this second pass cheap and still true.
    hidden_total = 0
    if available_only:
        _, matching = filter_coverage(
            report, query=query, types=types or None, available_only=False, limit=1, offset=0
        )
        hidden_total = max(0, matching - total)
    available = (
        None if names_only
        else sum(1 for r in report.instruments if r["data_available"])
    )
    return (
        jsonify(
            {
                "instruments": rows,
                "total": total,
                "returned": len(rows),
                "offset": offset,
                "limit": limit or None,
                "known_total": report.total,
                # None (not 0) when the caller asked for names only: "we did
                # not look" and "there are none" are different answers.
                "available_total": available,
                "hidden_total": hidden_total,
                "db_available": report.db_available,
                "catalogue_source": report.catalogue_source,
                "sources": report.sources,
                "instrument_types": list(INSTRUMENT_TYPES),
                # The canonical timeframe vocabulary, server-ordered (finest
                # first), so a client can present a SET of timeframes in the
                # order the engine speaks instead of declaring its own copy.
                # Same pattern as instrument_types above: the vocabulary lives
                # in backtest.data.base and this endpoint is the surface for it.
                "timeframes": list(CANONICAL_TIMEFRAMES),
                "hint": NO_DATA_HINT,
                "generated_at": report.generated_at,
                "warnings": report.warnings,
            }
        ),
        200,
    )

# -----------------------------------------------------------------------
# Background fetch job# -----------------------------------------------------------------------


def _run_fetch_job_inner(
    token: str,
    timeframe: str,
    from_date: str,
    to_date: str,
    symbols: list[str] | None,
    scope: str = DEFAULT_FETCH_SCOPE,
):
    """Background thread: fetch historical data for ticked symbols, or the
    curated universe for ``scope`` when nothing is ticked."""
    engine = create_engine(DB_URL, echo=False)
    api_key = os.getenv("MSTOCK_API_KEY", "")
    mstock_tf = _MSTOCK_INTERVAL_MAP.get(timeframe, timeframe)
    chunk_days = CHUNK_DAYS_MAP.get(timeframe, 800)
    log.info(
        "[data] fetch job starting: timeframe=%s range=%s..%s mstock_tf=%s chunk_days=%d "
        "symbols=%s scope=%s api_key=%s",
        timeframe,
        from_date,
        to_date,
        mstock_tf,
        chunk_days,
        "none ticked" if not symbols else f"{len(symbols)} requested",
        scope,
        "set" if api_key else "MISSING — every request will fail",
    )

    # Load instruments (explicit tick, or the curated scope: catalogue
    # tokens for the NIFTY 200, fixed token map for the indices). This is the
    # one step that used to run OUTSIDE any error handling: a database or
    # catalogue failure here (e.g. Postgres at max_connections) killed the
    # thread silently, leaving the job wedged in "running" with total=0 —
    # which the UI renders as the baffling "0 / 0 symbols" (2026-10-04).
    try:
        instruments, not_found = _load_instruments(engine, symbols, scope)
    except Exception as exc:  # noqa: BLE001 — a wedged job hides the real cause
        log.error(
            "[data] instrument resolution failed — %s: %s. The fetch did NOT "
            "start; retry once the database/catalogue is reachable.",
            exc.__class__.__name__,
            exc,
        )
        with _lock:
            _job["status"] = "error"
            _job["error"] = (
                f"Instrument list could not be loaded ({exc.__class__.__name__}). "
                "The database or instruments catalogue is unreachable — retry "
                "in a moment."
            )
        invalidate_coverage_cache()
        engine.dispose()
        return
    total = len(instruments) + len(not_found)
    if not instruments:
        log.warning(
            "[data] no instruments matched (symbols=%s, scope=%s) — is the "
            "`instruments` table populated? Run scripts/fetch_nifty500_historical.py first",
            ",".join(symbols) if symbols else "none ticked",
            scope,
        )
        # Zero instruments is not a "clean done with 0 stocks" — say why so
        # the UI never celebrates an empty run as success.
        with _lock:
            _job["error"] = (
                "No instruments matched this selection — nothing was fetched. "
                "Tick symbols, or check the scope/tab."
            )
    else:
        log.info("[data] %d instruments to fetch (%d not found)", len(instruments), len(not_found))

    with _lock:
        _job["total"] = total
        for sym in not_found:
            _job["fetched"] += 1
            _job["failed"] += 1
            _job["failed_list"].append(
                (sym, "Symbol not found in the instruments catalogue or the index map")
            )

    # Windows are identical for every symbol (same range + chunk_days), so
    # build the full walk once; each symbol then subtracts its covered days.
    all_windows = _build_windows(from_date, to_date, chunk_days)
    coverage_enabled = _coverage_skip_enabled()
    log.info(
        "[data] coverage-aware skip %s over %d windows/symbol",
        "ON" if coverage_enabled else "OFF (DATA_FETCH_SKIP_COVERED=0)",
        len(all_windows),
    )

    # One probe for the whole job (2026-10-04): a single grouped query returns
    # every symbol's covered days, instead of one round-trip per symbol.
    covered_by_symbol: dict[str, set] = {}
    if coverage_enabled and instruments:
        covered_by_symbol = _probe_covered_days(
            engine,
            [str(i["tradingsymbol"]) for i in instruments],
            timeframe,
            from_date,
            to_date,
        )

    # Holiday closures are symbol-independent, so read them once per job and
    # let the skip treat them like weekends: days that can never hold bars.
    holiday_dates: set = set()
    if coverage_enabled:
        holiday_dates = _load_market_holidays(engine, from_date, to_date)
        log.info(
            "[data] %d market holiday(s) in %s..%s — windows touching only "
            "holidays will not be re-probed",
            len(holiday_dates), from_date, to_date,
        )

    # One breaker per job: it watches post-retry chunk outcomes across ALL
    # symbols, so a gateway that dies mid-run stops the run instead of the
    # run grinding through the remaining 190 symbols.
    breaker = _CircuitBreaker()
    aborted: str | None = None

    for i, inst in enumerate(instruments, 1):
        if breaker.tripped:
            aborted = (
                f"mStock unreachable — {breaker.consecutive_failures} consecutive chunk "
                f"requests failed even after retries over {breaker.stall_minutes:.0f} min; "
                "fetch stopped early. Re-run it when the gateway recovers — already-stored "
                "days are skipped automatically."
            )
            log.error("[data] %s", aborted)
            break
        if _job.get("cancel"):
            log.info("[data] fetch job cancelled after %d/%d symbols", i - 1, total)
            with _lock:
                _job["status"] = "done"
                _job["error"] = "Cancelled by user"
            break

        symbol = inst["tradingsymbol"]
        sec_token = str(inst["instrument_token"])
        inst_exchange = inst.get("exchange", "NSE")

        with _lock:
            _job["symbol"] = symbol
            _job["bars_symbol"] = 0
            _job["chunk_done"] = 0
            _job["chunk_total"] = 0

        def _on_chunk(done: int, tot: int) -> None:
            with _lock:
                _job["chunk_done"] = done
                _job["chunk_total"] = tot

        # Coverage-aware skip: within the user's requested [from, to], drop the
        # windows whose days already sit in the DB. A fully-covered symbol costs
        # zero API requests; scattered 502 holes get gap-filled, not re-walked.
        needed_windows = all_windows
        if coverage_enabled:
            covered = covered_by_symbol.get(str(symbol).strip().upper(), set())
            needed_windows = _windows_needing_fetch(all_windows, covered,
                                                    holiday_dates)
            if not needed_windows:
                log.info(
                    "[data] %s: %s..%s already complete (%d window(s) fully "
                    "covered) — skipping, 0 requests",
                    symbol, from_date, to_date, len(all_windows),
                )
                with _lock:
                    _job["fetched"] += 1
                    _job["skipped"] += 1
                continue
            if len(needed_windows) < len(all_windows):
                log.info(
                    "[data] %s: gap-fill %d of %d windows (%d days already stored)",
                    symbol, len(needed_windows), len(all_windows), len(covered),
                )

        try:
            bars, chunk_errors = _fetch_bars_chunked(
                api_key,
                token,
                sec_token,
                from_date,
                to_date,
                inst_exchange,
                mstock_tf,
                chunk_days,
                should_cancel=lambda: bool(_job.get("cancel")) or breaker.tripped,
                on_progress=_on_chunk,
                windows=needed_windows,
                breaker=breaker,
            )
            if chunk_errors:
                # Partial coverage: some chunks died even after retries. The
                # symbol still counts as fetched, but the UI must not claim a
                # clean run — surface the lost-chunk total in job status.
                with _lock:
                    _job["chunk_errors"] += chunk_errors
                log.warning(
                    "[data] %s: %d chunk(s) failed even after retries — partial "
                    "coverage for %s..%s (%s)",
                    symbol,
                    chunk_errors,
                    from_date,
                    to_date,
                    mstock_tf,
                )
            if not bars:
                log.warning(
                    "[data] %s: API returned no bars for %s..%s (%s) — nothing stored",
                    symbol,
                    from_date,
                    to_date,
                    mstock_tf,
                )
                if chunk_errors:
                    # Every request for this symbol failed (expired broker
                    # session looks exactly like this) — count it FAILED so
                    # the UI stops reporting silent zero-bar "successes".
                    err = f"{chunk_errors} chunk request(s) failed, 0 bars stored (broker session?)"
                    log.warning("[data] %s: %s", symbol, err)
                    with _lock:
                        _job["failed"] += 1
                        _job["failed_list"].append((symbol, err[:160]))
                        _job["fetched"] += 1
                else:
                    with _lock:
                        _job["fetched"] += 1
                continue

            inserted = _persist_bars(engine, bars, symbol, inst_exchange, timeframe)
            log.info(
                "[data] %s: %d bars fetched, %d inserted (%d/%d done)",
                symbol,
                len(bars),
                inserted,
                i,
                total,
            )

            with _lock:
                _job["fetched"] += 1
                _job["bars_total"] += inserted
                _job["bars_symbol"] = inserted

        except Exception as exc:  # noqa: BLE001 — a bad symbol must not kill the job
            log.warning(
                "[data] %s failed (%d/%d): %s: %s", symbol, i, total, exc.__class__.__name__, exc
            )
            log.debug("[data] %s traceback", symbol, exc_info=True)
            with _lock:
                _job["failed"] += 1
                _job["failed_list"].append((symbol, str(exc)[:120]))
                _job["fetched"] += 1

        # Update elapsed
        elapsed_s = time.time() - (_job["started_at"] or time.time())
        with _lock:
            _job["elapsed"] = f"{int(elapsed_s // 60)}m {int(elapsed_s % 60)}s"

        time.sleep(0.3)  # rate limit

    with _lock:
        _job["status"] = "error" if aborted else "done"
        if aborted:
            _job["error"] = aborted
        _job["symbol"] = ""
        log.info(
            "[data] fetch job finished: %d/%d symbols ok (%d skipped as already "
            "covered), %d failed, %d bars inserted, %d chunk errors",
            _job["fetched"] - _job["failed"],
            total,
            _job["skipped"],
            _job["failed"],
            _job["bars_total"],
            _job["chunk_errors"],
        )

    # The bars just written are exactly what /api/data/coverage reports, so the
    # next page load must not serve the pre-fetch answer for a minute.
    invalidate_coverage_cache()
    engine.dispose()


def _run_fetch_job(
    token: str,
    timeframe: str,
    from_date: str,
    to_date: str,
    symbols: list[str] | None,
    scope: str,
) -> None:
    """Thread target: never let an unhandled error wedge the job state.

    Any exception escaping the worker used to leave ``_job`` stuck in
    "running" forever (total=0 → "0 / 0 symbols", Start blocked with 409),
    because only the per-symbol loop had error handling. This wrapper is the
    last-resort fence: the job lands in "error" with the real message, so the
    UI shows why and the Start button comes back (2026-10-04).
    """
    try:
        _run_fetch_job_inner(token, timeframe, from_date, to_date, symbols, scope)
    except Exception as exc:  # noqa: BLE001 — a wedged job hides the real cause
        log.error(
            "[data] fetch job crashed unexpectedly — %s: %s",
            exc.__class__.__name__,
            exc,
            exc_info=True,
        )
        with _lock:
            _job["status"] = "error"
            _job["error"] = (
                f"Fetch job crashed ({exc.__class__.__name__}) — see the server "
                "log for details. The job was stopped; retry when ready."
            )


def _index_row(symbol: str) -> dict | None:
    """Catalogue row for an index symbol, or ``None`` when it is unknown.

    Indexes are NOT in the scriptmaster ``instruments`` table — they resolve
    through the fixed token map the live feed already uses
    (:mod:`backtest.data.mstock_live_feed`), verified live against the TypeA
    historical endpoint (2026-09-18). Aliases ("NIFTY BANK") are normalised to
    the canonical universe symbol ("BANKNIFTY") so the bars land under the
    same key the coverage report and backtest pages read.
    """
    from backtest.data.coverage import INDEX_UNIVERSE
    from backtest.data.mstock_live_feed import INDEX_SECURITY_TOKENS

    sym = str(symbol).strip().upper()
    token = INDEX_SECURITY_TOKENS.get(sym) or INDEX_SECURITY_TOKENS.get(sym.replace(" ", ""))
    if token is None:
        return None
    canonical = {sym.upper(): s for s, sym in INDEX_UNIVERSE}
    canonical.update({s: s for s, _ in INDEX_UNIVERSE})
    sym = canonical.get(sym, sym)
    exchange = "BSE" if "SENSEX" in sym else "NSE"
    return {
        "tradingsymbol": sym,
        "instrument_token": token,
        "name": sym,
        "exchange": exchange,
    }


def _catalogue_rows(engine, syms: list[str]) -> list[dict]:
    """Resolve trading symbols to fetchable rows in the scriptmaster
    catalogue — at most ONE row per symbol, NSE preferred.

    The catalogue carries both an NSE ``EQ`` and a BSE ``Equity`` row for
    many names. Without the dedupe a fetch pulled both: the same symbol was
    requested twice and the second pass rewrote its bars under the other
    exchange — the exact shape of the NSE+BSE duplicate-row poison already
    documented in ``market_data_cache`` (2026-10-02: an equity-scope fetch
    showed 318 rows for the 200-symbol universe).
    """
    if not syms:
        return []
    placeholders = ", ".join([f":s{i}" for i in range(len(syms))])
    sql = text(
        f"SELECT tradingsymbol, instrument_token, name, exchange "
        f"FROM instruments "
        f"WHERE ((exchange = 'NSE' AND instrument_type = 'EQ') OR "
        f"       (exchange = 'BSE' AND instrument_type = 'Equity')) "
        f"AND UPPER(tradingsymbol) IN ({placeholders}) "
        f"ORDER BY tradingsymbol"
    )
    params = {f"s{i}": s.upper() for i, s in enumerate(syms)}
    with engine.connect() as conn:
        rows = conn.execute(sql, params).mappings().all()

    one_per_symbol: dict[str, dict] = {}
    for r in rows:
        row = dict(r)
        sym = str(row["tradingsymbol"]).strip().upper()
        kept = one_per_symbol.get(sym)
        if kept is None or (kept["exchange"] != "NSE" and row["exchange"] == "NSE"):
            one_per_symbol[sym] = row
    return list(one_per_symbol.values())


def _load_instruments(
    engine, symbols: list[str] | None, scope: str = DEFAULT_FETCH_SCOPE
) -> tuple[list[dict], list[str]]:
    """Resolve what a fetch job should pull into ``(rows, not_found)``.

    Explicitly ticked symbols always win: index names resolve through the
    fixed token map (they are absent from the equity-only ``instruments``
    table), everything else through the catalogue, and a symbol neither
    knows is reported in ``not_found`` — counted, not silently dropped.

    With nothing ticked, ``scope`` selects the CURATED universe the Data
    tab's picker actually shows (``coverage?curated=1``): ``equity`` → the
    shipped NIFTY 200 list, ``index`` → the built-in index universe,
    ``equity,index`` → both. It is deliberately NOT the full scriptmaster
    catalogue any more: "no selection" meaning all ~15k NSE+BSE rows sent a
    job into BSE bond symbols while the UI promised the NIFTY 200
    (2026-10-02 incident, ``001HCCL29``).
    """
    if symbols:
        index_rows: list[dict] = []
        equity_syms: list[str] = []
        not_found: list[str] = []
        for s in symbols:
            sym = str(s).strip().upper()
            if not sym:
                continue
            row = _index_row(sym)
            if row is not None:
                # Token map wins: it also carries the alias spellings
                # ("NIFTY BANK") that classify_instrument resolves as equity.
                index_rows.append(row)
            elif classify_instrument(sym) == "index":
                not_found.append(sym)  # an index the map does not have a token for
            else:
                equity_syms.append(sym)

        rows = _catalogue_rows(engine, equity_syms)
        matched = {str(r["tradingsymbol"]).strip().upper() for r in rows}
        not_found.extend(s for s in equity_syms if s not in matched)
        rows.extend(index_rows)
        return rows, not_found

    parts = scope.split(",")
    rows = []
    not_found = []
    if "equity" in parts:
        universe_syms = [str(r["symbol"]).strip().upper() for r in load_equity_universe()]
        found = _catalogue_rows(engine, universe_syms)
        matched = {str(r["tradingsymbol"]).strip().upper() for r in found}
        rows.extend(found)
        not_found.extend(s for s in universe_syms if s not in matched)
        bse_only = [r["tradingsymbol"] for r in found if r["exchange"] != "NSE"]
        if bse_only:
            # Known catalogue gap: most NIFTY 200 names have no NSE row with
            # instrument_type='EQ' (they carry segment codes instead), so
            # they resolve to their BSE row. Bars land exchange=BSE — fine
            # for reading, wrong if a symbol-level NSE series is expected.
            log.info(
                "[data] equity scope: %d of %d universe symbols resolved via BSE only "
                "(no NSE 'EQ' catalogue row), e.g. %s",
                len(bse_only),
                len(universe_syms),
                ", ".join(sorted(bse_only)[:5]),
            )
    if "index" in parts:
        rows.extend(filter(None, (_index_row(sym) for sym, _ in INDEX_UNIVERSE)))
    return rows, not_found


def _get_historical_with_retry(
    url, headers, params, attempts=4, base_sleep=1.5, should_cancel=None
):
    """GET the TypeA historical endpoint, retrying mStock's intermittent 502s.

    The gateway answers 502 for a large share of requests fired at the chunk
    loop's ~6 req/s cadence (observed 2026-10-03: ALKEM fetch lost ~22 of 41
    chunks; manual re-probes seconds later returned the bars fine). A short
    backoff almost always succeeds on the next attempt. Raises the last error
    if every attempt fails so the caller counts the chunk as an error.

    ``should_cancel`` is checked before every attempt so a Stop (or a tripped
    circuit breaker) lands *between* backoffs instead of burning the rest of
    the ladder: worst case a worker now holds on for the one request already
    in flight (60 s socket timeout) rather than 4 × 60 s + backoffs. Raises
    :class:`FetchCancelled` — which the chunk loop does NOT count as an error.
    """
    last_error = None
    for attempt in range(1, attempts + 1):
        if should_cancel is not None and should_cancel():
            raise FetchCancelled("historical fetch cancelled mid-retry")
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=60)
        except requests.RequestException as exc:
            last_error = exc  # transport error — worth retrying
        else:
            if resp.status_code in (502, 503, 504):
                last_error = requests.HTTPError(f"HTTP {resp.status_code}", response=resp)
            else:
                resp.raise_for_status()  # 4xx (auth etc.) will not fix itself
                return resp
        if attempt < attempts:
            time.sleep(base_sleep * attempt)
    raise last_error


# ---------------------------------------------------------------------------
# Coverage-aware gap filling (2026-10-03)
# ---------------------------------------------------------------------------
#
# A re-run over the same range used to re-walk every chunk of every symbol,
# so the 200-symbol 2022→2026 fetch took ~27h no matter how much was already
# stored — and a symbol like ABCAPITAL (a few 502 holes in otherwise-complete
# history) was re-fetched whole. These helpers let the fetch job cross-check
# the user's requested [from, to] against ``market_data_cache`` day by day and
# pull ONLY the windows that still contain a missing day — a fully-covered
# symbol costs zero API requests. Disable with DATA_FETCH_SKIP_COVERED=0.

#: Full-session bar count per canonical timeframe, used purely to decide when a
#: stored day is "complete enough" to trust. A day holding fewer than
#: ``COVERAGE_FRACTION`` of these is treated as missing so a resume gap-fills
#: partial days (e.g. a window that half-succeeded) rather than trusting holes.
_FULL_DAY_BARS = {
    "1min": 375,  # 09:15–15:30 IST
    "5min": 75,
    "15min": 25,
    "1hour": 6,  # 375 / 60, floored
    "1day": 1,
}
#: Completeness fraction for a stored day to count as "fetched". Raised from
#: 0.7 (2026-10-03) to 0.9 (2026-10-04): at 0.7 a day could be missing up to
#: ~110 of its 375 minutes and still be trusted — a multi-hour in-between hole
#: that silently distorts every backtest on that day. At 0.9 any hole larger
#: than ~37 minutes (1min bars) re-fetches the window. Override with
#: DATA_FETCH_DAY_COMPLETENESS.
COVERAGE_FRACTION = float(os.getenv("DATA_FETCH_DAY_COMPLETENESS", "0.9"))


def _coverage_skip_enabled() -> bool:
    """Coverage-aware skip is on by default; ``DATA_FETCH_SKIP_COVERED=0`` off."""
    return os.getenv("DATA_FETCH_SKIP_COVERED", "1").strip() != "0"


def _coverage_threshold(timeframe: str) -> int:
    """Minimum bars a day needs to count as already-fetched for ``timeframe``."""
    expected = _FULL_DAY_BARS.get(timeframe, 1)
    return max(1, math.ceil(expected * COVERAGE_FRACTION))


def _build_windows(from_date: str, to_date: str, chunk_days: int) -> list:
    """Split the INCLUSIVE range ``[from_date, to_date]`` into ``chunk_days``-wide
    (start, end) datetimes — the windows the chunk loop fetches, decided before
    the loop runs so coverage-aware skipping knows what still needs fetching.
    """
    from datetime import datetime, timedelta

    start = datetime.strptime(from_date, "%Y-%m-%d")
    end = datetime.strptime(to_date, "%Y-%m-%d")
    windows = []
    chunk_start = start
    # `<=`, not `<`: to_date is inclusive (the API defaults it to today, and a
    # from==to one-day fetch must yield one window, not zero). `<` silently
    # dropped the last day whenever it landed exactly on a chunk boundary.
    while chunk_start <= end:
        chunk_end = min(chunk_start + timedelta(days=chunk_days), end)
        windows.append((chunk_start, chunk_end))
        chunk_start = chunk_end + timedelta(days=1)
    return windows


def _day_expr(engine, column: str = "ts") -> str:
    """SQL that reduces a timestamp column to a day, for the engine's dialect.

    ``date_trunc('day', ts)::date`` / ``ts::date`` are PostgreSQL cast syntax
    with no SQLite equivalent; on SQLite the whole probe raises, is swallowed by
    its error handler, and returns ``{}`` — which means *every* symbol is
    re-fetched in full on every resume, silently disabling the coverage skip on
    the default dev profile (config/database.yaml → sqlite, 13 sqlite
    migrations ship). ``date(ts)`` is SQLite's equivalent and yields the same
    ``YYYY-MM-DD`` text.

    An engine whose dialect cannot be read (a stub in a test, a proxy object) is
    treated as the deployment target rather than raising: this is called while
    building the probe's SQL, outside its ``except``, so raising here would take
    down a fetch job that is otherwise perfectly able to run.
    """
    name = getattr(getattr(engine, "dialect", None), "name", None)
    if name == "sqlite":
        return f"date({column})"
    return f"date_trunc('day', {column})::date"


def _probe_covered_days(
    engine, symbols: list[str], timeframe: str, from_date: str, to_date: str
) -> dict[str, set]:
    """One query for the WHOLE job: which days already hold enough bars.

    Replaces the per-symbol probe (a round-trip per symbol — 200 queries for
    the curated universe). Two correctness upgrades over the old probe
    (2026-10-04):

    * ``COUNT(DISTINCT ts)`` — duplicate/re-written rows can no longer inflate
      coverage into skipping a day that is actually thin.
    * The raised :data:`COVERAGE_FRACTION` (0.9) means a stored day with an
      in-between hole of missing hours no longer counts as fetched: it is
      re-fetched instead of silently feeding gappy bars into backtests.

    Exchange-agnostic: the cache's ``exchange`` column is unreliable (one
    symbol's bars land under BSE or NSE depending on which catalogue row the
    fetch resolved), and for backtest reads bars are bars. Filtering by
    exchange used to blind the probe into re-fetching an already-complete
    symbol wholesale.

    Returns ``{}`` on ANY error — every symbol then gets a full fetch rather
    than being wrongly skipped because a probe failed.
    """
    if not symbols:
        return {}
    threshold = _coverage_threshold(timeframe)
    bind = {f"s{i}": s for i, s in enumerate(dict.fromkeys(symbols))}
    in_clause = ", ".join(f":{k}" for k in bind)
    covered: dict[str, set] = {}
    try:
        # Built inside the try: a dialect this cannot express must degrade to a
        # full fetch like any other probe failure, not take the job down.
        day = _day_expr(engine)
        sql = text(
            f"SELECT symbol, {day} AS d, COUNT(DISTINCT ts) AS n "
            "FROM market_data_cache "
            f"WHERE timeframe = :timeframe AND symbol IN ({in_clause}) "
            f"AND {day} >= :from_date AND {day} <= :to_date "
            "GROUP BY symbol, d"
        )
        with engine.connect() as conn:
            rows = conn.execute(
                sql,
                {
                    **bind,
                    "timeframe": timeframe,
                    "from_date": from_date,
                    "to_date": to_date,
                },
            ).all()
    except Exception as exc:  # noqa: BLE001 — probing must never break a fetch
        log.warning(
            "[data] coverage probe failed (%s symbols, %s, %s..%s): %s — fetching full ranges",
            len(bind), timeframe, from_date, to_date, exc,
        )
        return covered
    for symbol, d, n in rows:
        if n >= threshold:
            # PostgreSQL hands back a datetime, SQLite the TEXT 'YYYY-MM-DD';
            # _windows_needing_fetch compares against ``date`` objects, so a raw
            # string here would match nothing and disable the skip entirely.
            day = _as_session_date(d)
            if day is not None:
                covered.setdefault(str(symbol).strip().upper(), set()).add(day)
    return covered


def _load_market_holidays(engine, from_date: str, to_date: str) -> set:
    """Exchange-closure dates inside the requested range, from ``market_holidays``.

    Seeded from the NSE holiday-master API (2022–2026 as of 2026-10-04, see
    ``tools/seed_market_holidays.py``). A weekday here can never hold session
    bars, so the coverage skip must not count it as "missing" — without this,
    every window touching a holiday is re-probed on every resume forever
    (~12–16 dates/symbol/run; the 2025-10-21 Diwali Muhurat special session is
    listed too, and its ~61 stored bars are all that day will ever have).

    Returns ``{}`` on ANY error (missing table, bad column) — the skip then
    behaves like before and merely re-probes holiday windows.
    """
    sql = text(
        "SELECT holiday_date FROM market_holidays "
        "WHERE holiday_date >= :from_date AND holiday_date <= :to_date "
        "AND is_trading_holiday IS NOT FALSE"
    )
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                sql, {"from_date": from_date, "to_date": to_date}
            ).all()
    except Exception as exc:  # noqa: BLE001 — calendar read must never break a fetch
        log.warning(
            "[data] holiday calendar read failed (%s..%s): %s — "
            "holiday windows will be re-probed (old behaviour)",
            from_date, to_date, exc,
        )
        return set()
    return {r[0].date() if hasattr(r[0], "date") else r[0] for r in rows}


def _windows_needing_fetch(windows: list, covered_days: set,
                           holidays=()) -> list:
    """Keep only the windows whose span includes a *missing trading day*.

    Only Mon–Fri days can hold session bars, so a weekday is the sole thing
    that counts as "should have data." Saturdays/Sundays have none by
    definition and must not mark a window missing — without the weekday filter
    every window touching a weekend would be re-fetched, and a fully-covered
    symbol would never be skipped (verified live 2026-10-03: AB-CAPITAL kept
    all 578 windows because the weekend days never appear in the DB).

    ``holidays`` (from :func:`_load_market_holidays`) extends the "cannot hold
    bars" set to weekday exchange closures, so a fully-fetched symbol's resume
    drops its windows on the FIRST pass instead of re-probing ~14 holiday
    windows per symbol per run.

    ``covered_days`` empty → every window (a genuine full fetch). All weekdays
    covered → empty list (caller skips the symbol with zero requests). A window
    touching even one missing weekday is re-fetched whole; the upsert in
    :func:`_persist_bars` discards the days inside it that already exist.
    """
    from datetime import timedelta

    if not covered_days:
        return list(windows)
    needed = []
    for c_start, c_end in windows:
        d = c_start
        missing = False
        while d <= c_end:
            if (d.weekday() < 5 and d.date() not in covered_days
                    and d.date() not in holidays):
                missing = True
                break
            d += timedelta(days=1)
        if missing:
            needed.append((c_start, c_end))
    return needed


# ---------------------------------------------------------------------------
# Data-freshness chip (2026-10-04) — tail-staleness of completed sessions
# ---------------------------------------------------------------------------

#: Minute-of-day (IST) by which the last session's bars are expected to have
#: settled in mStock's historical endpoint. Flagging at 15:31 would nag about
#: a session still incomplete; the 0.9 coverage probe would re-fetch it anyway
#: — the chip waits until that cannot be an artifact.
DATA_SETTLE_MIN_IST = 16 * 60 + 15  # 16:15 IST

_IST = timezone(timedelta(hours=5, minutes=30))
_freshness_lock = threading.Lock()
_freshness_cache: dict[str, Any] = {"at": 0.0, "payload": None, "updating": False}


def _is_session_day(d, holidays) -> bool:
    """A day that should have produced bars: Mon–Fri and not an exchange closure."""
    return d.weekday() < 5 and d not in holidays


def _last_settled_session(now_ist, holidays) -> date:
    """Most recent session whose bars are due in the DB as of ``now_ist``.

    Today counts only past :data:`DATA_SETTLE_MIN_IST`; otherwise the target
    walks back over weekends and ``market_holidays`` closures (45-day cap for
    pathological calendars — callers see an over-strict target, never a lie
    that data is current).
    """
    if (_is_session_day(now_ist.date(), holidays)
            and now_ist.hour * 60 + now_ist.minute >= DATA_SETTLE_MIN_IST):
        return now_ist.date()
    d = now_ist.date() - timedelta(days=1)
    for _ in range(45):
        if _is_session_day(d, holidays):
            return d
        d -= timedelta(days=1)
    return d


def _as_session_date(value) -> "date | None":
    """Normalise a ``MAX(ts)`` value from any dialect to a plain date.

    PostgreSQL hands back a ``datetime``; SQLite hands back the column's TEXT
    (``"2026-09-30 15:28:00"``). The freshness walk compares this against a
    ``date``, so both must land on the same type — a raw string used to make
    every comparison silently unequal.
    """
    if value is None:
        return None
    if hasattr(value, "date") and not isinstance(value, str):
        return value.date()
    text_value = str(value).strip()
    if not text_value:
        return None
    try:
        return datetime.fromisoformat(text_value.replace("Z", "+00:00")).date()
    except ValueError:
        log.warning("[data] unparsable ts in market_data_cache: %r", text_value[:40])
        return None


def _count_missing_sessions(last_day, target: date, holidays) -> int:
    """Trading sessions in ``(last_day, target]` — tail staleness only.

    Assumes everything up to ``last_day`` is covered; holes *inside* that
    stretch are the coverage probe's job (it re-fetches their windows), while
    the chip answers the one question a global widget can ask cheaply: "is
    the newest session in the database?"
    """
    if last_day is None or last_day >= target:
        return 0
    n, d = 0, last_day + timedelta(days=1)
    while d <= target:
        if _is_session_day(d, holidays):
            n += 1
        d += timedelta(days=1)
    return n


def _compute_freshness() -> dict:
    """Per-timeframe staleness vs the last completed session. Never raises."""
    with _lock:
        fetching = _job["status"] == "running"
    out: dict[str, Any] = {
        "db_available": False, "fetching": fetching, "level": "unknown",
        "label": "DATA ?", "title": "Freshness unknown — database unreachable",
        "per_timeframe": {}, "target_day": None,
    }
    engine = None
    try:
        engine = create_engine(DB_URL, echo=False)
        now_ist = datetime.now(_IST)
        with engine.connect() as conn:
            # ``ts::date`` is PostgreSQL cast syntax. On SQLite it raises
            # "unrecognized token: ':'", the whole block is swallowed by the
            # except below, and the chip reports "database unreachable" — a
            # false diagnosis on the DEFAULT dev profile (config/database.yaml
            # → sqlite:///forward_test.db, and 13 sqlite migrations ship).
            # MAX(ts) is portable; the value is normalised to a date below.
            rows = conn.execute(text(
                "SELECT timeframe, MAX(ts) FROM market_data_cache "
                "GROUP BY timeframe"
            )).all()
        # Holiday window only needs to cover the walkback; 60 days reaches
        # any realistic gap (longest NSE stretch is under 10).
        holidays = _load_market_holidays(
            engine,
            (now_ist.date() - timedelta(days=60)).isoformat(),
            now_ist.date().isoformat(),
        )
        target = _last_settled_session(now_ist, holidays)
        out["target_day"] = target.isoformat()
        per, worst = {}, 0
        for tf, last in rows:
            last_d = _as_session_date(last)
            missing = _count_missing_sessions(last_d, target, holidays)
            per[str(tf)] = {
                "last_day": last_d.isoformat() if last_d else None,
                "missing": missing,
            }
            worst = max(worst, missing)
        out["per_timeframe"] = per
        out["db_available"] = True
        if fetching:
            out["level"] = "fetching"
            out["label"] = "DATA FETCHING"
            out["title"] = "A fetch is running — the chip updates when it finishes."
        elif not per:
            out["level"] = "red"
            out["label"] = "NO CACHED DATA"
            out["title"] = "market_data_cache is empty — fetch the curated universe."
        else:
            level = "current" if worst == 0 else ("amber" if worst == 1 else "red")
            details = " · ".join(
                f"{tf}: {v['last_day'] or 'no data'}"
                + (f" ({v['missing']} behind)" if v["missing"] else "")
                for tf, v in sorted(per.items())
            )
            out["level"] = level
            out["label"] = ("DATA CURRENT" if level == "current"
                            else f"DATA {worst} SESSION{'S' if worst > 1 else ''} BEHIND")
            out["title"] = (
                f"Last completed session: {target.isoformat()}. {details}."
                + ("" if level == "current" else " Click to fetch on the Data tab.")
            )
    except Exception as exc:  # noqa: BLE001 — a chip must never break a page render
        log.warning("[data] freshness check failed: %s", exc)
    finally:
        if engine is not None:
            engine.dispose()
    return out


def _spawn_freshness_refresh() -> None:
    """Recompute the freshness payload off-thread; at most one runner."""
    def _run() -> None:
        try:
            payload = _compute_freshness()
            with _freshness_lock:
                _freshness_cache["payload"] = payload
                _freshness_cache["at"] = time.time()
        except Exception as exc:  # noqa: BLE001 — next render retries anyway
            log.warning("[data] background freshness refresh failed: %s", exc)
        finally:
            with _freshness_lock:
                _freshness_cache["updating"] = False

    with _freshness_lock:
        if _freshness_cache["updating"]:
            return
        _freshness_cache["updating"] = True
    threading.Thread(target=_run, daemon=True, name="data-freshness-refresh").start()


#: The grouped MAX over the hypertable costs ~14s until migration 019
#: (``ix_mdc_tf_ts``) lands — and it can only be built while fetches are
#: idle (Timescale rejects CONCURRENTLY). So the render path NEVER computes
#: inline: it serves the cached payload and refreshes in the background
#: (stale-while-revalidate). A gray "CHECKING" chip beats a 14s page.
_FRESHNESS_TTL_SECONDS = 300.0


def get_data_freshness() -> dict:
    """Cached freshness payload for the topbar chip; never blocks a render."""
    with _freshness_lock:
        payload = _freshness_cache["payload"]
        expired = payload is None or (
            time.time() - _freshness_cache["at"] >= _FRESHNESS_TTL_SECONDS
        )
    if expired:
        _spawn_freshness_refresh()
    if payload is not None:
        return payload  # stale but truthful-ish; refresh is in flight
    return {
        "db_available": False, "fetching": False, "level": "unknown",
        "label": "DATA CHECKING", "title": "First freshness check running — updates shortly.",
        "per_timeframe": {}, "target_day": None,
    }


@data_bp.route("/api/data/freshness", methods=["GET"])
def api_data_freshness():
    """JSON the topbar chip re-renders from (initially it is server-rendered)."""
    return jsonify(get_data_freshness())


def _fetch_bars_chunked(
    api_key: str,
    token: str,
    sec_token: str,
    from_date: str,
    to_date: str,
    segment: str,
    mstock_tf: str,
    chunk_days: int,
    should_cancel=None,
    on_progress=None,
    workers: int = CHUNK_FETCH_WORKERS,
    windows: list | None = None,
    breaker: "_CircuitBreaker | None" = None,
) -> tuple[list[dict], int]:
    """Fetch OHLCV bars from mStock, chunked by date range.

    Returns ``(bars, chunk_errors)`` — the error count lets the caller mark a
    symbol as FAILED when every chunk request errored (e.g. an expired broker
    session returning 401 for all of them) instead of reporting a silent
    zero-bar "success".

    ``should_cancel`` is checked before every chunk so a Stop request lands
    within seconds even for a wide date range at 1-minute granularity
    (the per-symbol cancel check alone left the user watching a single
    symbol churn through hundreds of chunks).

    ``on_progress(done, total)`` is called after every completed chunk so the
    UI can draw a moving bar inside a multi-minute symbol.

    Chunks run ``workers`` threads deep (default ``CHUNK_FETCH_WORKERS``) —
    the loop is pure network waiting, so serialising it left 3/4 of the
    session's usable throughput unused. Pacing is shared state under
    ``pace_lock``: a burst of ``CHUNK_PACE_UP_AFTER`` consecutive failures
    widens the sleep for ALL workers (bad mStock hours self-throttle), and
    ``CHUNK_PACE_DOWN_AFTER`` clean chunks decay one worker back to base
    cadence at a time. ``workers=1`` makes the loop fully sequential, which
    the pacing-rhythm tests rely on.

    ``windows`` — when given, exactly these ``(start, end)`` windows are
    fetched instead of the full ``[from_date, to_date]`` walk. The caller uses
    it to drop windows whose days are already in the DB (coverage-aware gap
    fill); omitted, the whole range is built so existing callers are unchanged.

    ``breaker`` — the job's :class:`_CircuitBreaker`. Every post-retry chunk
    outcome is recorded on it, and once it has tripped the remaining windows
    short-circuit as *cancelled* (no request is fired), so a full mStock
    outage ends the walk in seconds instead of days.
    """
    headers = {"X-Mirae-Version": "1", "Authorization": f"token {api_key}:{token}"}
    url = (
        f"{MSTOCK_BASE_URL}/openapi/typea/instruments/historical/{segment}/{sec_token}/{mstock_tf}"
    )

    if windows is None:
        windows = _build_windows(from_date, to_date, chunk_days)
    total_chunks = max(1, len(windows))
    if on_progress is not None:
        on_progress(0, total_chunks)

    all_bars: list[dict] = []
    chunk_errors = 0
    chunk_done = 0
    pace_state = {"pace": CHUNK_SLEEP_BASE, "consec_fails": 0, "consec_ok": 0}
    pace_lock = threading.Lock()

    def _next_pace(ok: bool) -> float:
        """Fold one chunk outcome into the shared pacing; return the sleep."""
        with pace_lock:
            if ok:
                pace_state["consec_fails"] = 0
                pace_state["consec_ok"] += 1
                if (
                    pace_state["consec_ok"] >= CHUNK_PACE_DOWN_AFTER
                    and pace_state["pace"] > CHUNK_SLEEP_BASE
                ):
                    pace_state["pace"] = CHUNK_SLEEP_BASE
                    pace_state["consec_ok"] = 0
                    log.info(
                        "[data] mStock responding cleanly again — pacing back to %.1fs",
                        pace_state["pace"],
                    )
            else:
                pace_state["consec_ok"] = 0
                pace_state["consec_fails"] += 1
                if (
                    pace_state["consec_fails"] >= CHUNK_PACE_UP_AFTER
                    and pace_state["pace"] < CHUNK_SLEEP_WIDE
                ):
                    pace_state["pace"] = CHUNK_SLEEP_WIDE
                    log.warning(
                        "[data] %d consecutive chunk failures — widening pace to "
                        "%.1fs for the rest of this symbol",
                        pace_state["consec_fails"],
                        pace_state["pace"],
                    )
            return pace_state["pace"]

    def _fetch_window(window):
        c_start, c_end = window
        if should_cancel is not None and should_cancel():
            return ("cancelled", None, None)
        if breaker is not None and breaker.tripped:
            return ("cancelled", None, None)
        # The windows are inclusive, but mStock's `to` date is END-EXCLUSIVE
        # (verified live 2026-10-06: from=to returns zero bars; Sep 30..Oct 2
        # returns Sep 30 + Oct 1). Ask one day past the window to collect it.
        params = {
            "from": c_start.strftime("%Y-%m-%d"),
            "to": (c_end + timedelta(days=1)).strftime("%Y-%m-%d"),
        }
        try:
            resp = _get_historical_with_retry(url, headers, params, should_cancel=should_cancel)
            bars = _extract_bars(resp.json())
            pace = _next_pace(True)
            if breaker is not None:
                breaker.record(True)
            result = ("ok", bars, None)
        except FetchCancelled:
            # Stop pressed (or breaker tripped) mid-retry-ladder — the window
            # was abandoned by choice, not by mStock: count it as neither a
            # success nor an error, and do not feed the breaker streak.
            return ("cancelled", None, None)
        except Exception as exc:  # noqa: BLE001 — skip bad chunks, but say so
            log.warning(
                "[data] chunk %s..%s failed (%s: %s) — those bars are missing",
                c_start,
                c_end,
                exc.__class__.__name__,
                exc,
            )
            pace = _next_pace(False)
            if breaker is not None:
                breaker.record(False)
            result = ("fail", None, exc)
        time.sleep(pace)
        return result

    cancelled = False
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(_fetch_window, w) for w in windows]
        for fut in as_completed(futures):
            kind, bars, _exc = fut.result()
            if kind == "cancelled":
                cancelled = True
                continue
            chunk_done += 1
            if kind == "ok":
                all_bars.extend(bars or [])
            else:
                chunk_errors += 1
            if on_progress is not None:
                on_progress(chunk_done, total_chunks)

    if cancelled:
        log.info(
            "[data] chunk loop cancelled — returning %d bars collected so far",
            len(all_bars),
        )
    return all_bars, chunk_errors


def _extract_bars(payload) -> list[dict]:
    """Extract bar list from mStock response."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ["data", "candles", "result", "bars", "historical"]:
            if key in payload:
                value = payload[key]
                if isinstance(value, list):
                    return value
                if isinstance(value, dict):
                    for k2 in ["candles", "data", "result", "bars"]:
                        if k2 in value and isinstance(value[k2], list):
                            return value[k2]
    return []


def _persist_bars(engine, bars: list[dict], symbol: str, exchange: str, timeframe: str) -> int:
    """Upsert OHLCV bars into market_data_cache."""
    if not bars:
        return 0

    sql = text(
        """
        INSERT INTO market_data_cache
            (symbol, exchange, timeframe, ts, open, high, low, close, volume, source, ingested_at)
        VALUES
            (:symbol, :exchange, :timeframe, :ts, :open, :high, :low, :close, :volume,"""
        """ :source, now())
        ON CONFLICT (symbol, exchange, timeframe, ts) DO UPDATE
            SET open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
                close = EXCLUDED.close, volume = EXCLUDED.volume, ingested_at = now()
    """
    )

    rows = []
    skipped: list[str] = []
    for bar in bars:
        try:
            if isinstance(bar, dict):
                ts = bar_timestamp(bar.get("t", bar.get("time", bar.get("timestamp"))))
                o, h, l, c = (
                    float(bar.get("o", bar.get("open", 0))),
                    float(bar.get("h", bar.get("high", 0))),
                    float(bar.get("l", bar.get("low", 0))),
                    float(bar.get("c", bar.get("close", 0))),
                )
                v = int(bar.get("v", bar.get("volume", 0)))
            elif isinstance(bar, (list, tuple)) and len(bar) >= 6:
                ts = bar_timestamp(bar[0])
                o, h, l, c = float(bar[1]), float(bar[2]), float(bar[3]), float(bar[4])
                v = int(bar[5])
            else:
                continue

            if o <= 0 or h <= 0 or l <= 0 or c <= 0:
                skipped.append(f"{ts}: non-positive price")
                continue
            if h < l or h < o or h < c or l > o or l > c:
                skipped.append(f"{ts}: OHLC inconsistent (o={o} h={h} l={l} c={c})")
                continue

            rows.append(
                {
                    "symbol": symbol,
                    "exchange": exchange,
                    "timeframe": timeframe,
                    "ts": ts,
                    "open": o,
                    "high": h,
                    "low": l,
                    "close": c,
                    "volume": v,
                    "source": "mstock",
                }
            )
        except Exception as exc:  # noqa: BLE001 — one malformed bar must not lose the rest
            skipped.append(f"{bar!r:.60}: {exc.__class__.__name__}: {exc}")
            continue

    if skipped:
        log.warning(
            "[data] %s: dropped %d/%d bars while parsing (e.g. %s)",
            symbol,
            len(skipped),
            len(bars),
            skipped[0],
        )
        log.debug("[data] %s full drop list: %s", symbol, "; ".join(skipped[:50]))

    if not rows:
        log.warning(
            "[data] %s: %d bars fetched but none were usable — nothing to insert", symbol, len(bars)
        )
        return 0

    total = 0
    chunk_size = 500
    with engine.connect() as conn:
        for i in range(0, len(rows), chunk_size):
            chunk = rows[i : i + chunk_size]
            conn.execute(sql, chunk)
            total += len(chunk)
        conn.commit()

    return total
