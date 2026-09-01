"""Data Manager API endpoints.

Endpoints for fetching historical market data from mStock and storing in PostgreSQL.
Runs fetch jobs as background threads with progress tracking.

* POST /api/data/fetch       — Start a fetch job
* GET  /api/data/status       — Get current fetch job status + progress
* POST /api/data/stop         — Stop the current fetch job
* GET  /api/data/inventory    — Show what data is available in DB per symbol
"""

from __future__ import annotations

import os
import threading
import time
from datetime import date
from typing import Any

import requests
from flask import Blueprint, jsonify, request
from sqlalchemy import create_engine, text

from backtest.data.base import MSTOCK_INTERVAL_MAP
from backtest.db.config import get_db_url
from backtest.logging_config import get_logger

data_bp = Blueprint("data_api", __name__)
log = get_logger(__name__)

# -----------------------------------------------------------------------
# Fetch job state (single job at a time)
# -----------------------------------------------------------------------
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
    "timeframe": "day",
    "error": None,  # last error message
    "started_at": None,
    "elapsed": "",
    "cancel": False,  # flag to stop the job
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
    """Signal the running job to stop after the current symbol."""
    with _lock:
        if _job["status"] != "running":
            return jsonify({"error": "No job running"}), 400
        _job["cancel"] = True
    return jsonify({"status": "stopping"}), 200


@data_bp.post("/api/data/fetch")
def fetch_start() -> tuple:
    """Start a background fetch job.

    Body: { symbols?: string[], timeframe?: string, from_date?: string, to_date?: string }
    """
    with _lock:
        if _job["status"] == "running":
            return jsonify({"error": "A fetch job is already running. Stop it first."}), 409

    data = request.get_json(silent=True) or {}
    timeframe = data.get("timeframe", "1min")
    from_date = data.get("from_date", "2024-01-01")
    to_date = data.get("to_date", date.today().isoformat())
    symbols = data.get("symbols")  # None = all from instruments table

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

    # Validate auth token exists
    token_file = os.path.join(os.getcwd(), ".mstock_session_token")
    if not os.path.exists(token_file):
        return (
            jsonify({"error": "No auth token. Please authenticate via the Broker button first."}),
            401,
        )

    with open(token_file) as f:
        token = f.read().strip()
    if len(token) < 16:
        return jsonify({"error": "Auth token invalid. Please re-authenticate."}), 401

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
            error=None,
            started_at=time.time(),
            elapsed="",
            cancel=False,
        )

    # Launch background thread
    thread = threading.Thread(
        target=_run_fetch_job,
        args=(token, timeframe, from_date, to_date, symbols),
        daemon=True,
    )
    thread.start()

    return (
        jsonify(
            {
                "status": "started",
                "timeframe": timeframe,
                "from_date": from_date,
                "to_date": to_date,
            }
        ),
        200,
    )


@data_bp.get("/api/data/inventory")
def inventory() -> tuple:
    """Return per-symbol data availability summary."""
    try:
        engine = create_engine(DB_URL, echo=False)
        sql = text(
            """
            SELECT symbol, timeframe, COUNT(*) as bars,
                   MIN(ts) as earliest, MAX(ts) as latest
            FROM market_data_cache
            GROUP BY symbol, timeframe
            ORDER BY symbol, timeframe
        """
        )
        with engine.connect() as conn:
            rows = conn.execute(sql).mappings().all()
        engine.dispose()

        # Group by symbol
        symbols: dict[str, list] = {}
        for r in rows:
            sym = r["symbol"]
            if sym not in symbols:
                symbols[sym] = []
            symbols[sym].append(
                {
                    "timeframe": r["timeframe"],
                    "bars": r["bars"],
                    "earliest": str(r["earliest"])[:10] if r["earliest"] else None,
                    "latest": str(r["latest"])[:10] if r["latest"] else None,
                }
            )

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
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# -----------------------------------------------------------------------
# Background fetch job
# -----------------------------------------------------------------------


def _run_fetch_job(
    token: str, timeframe: str, from_date: str, to_date: str, symbols: list[str] | None
):
    """Background thread: fetch historical data for all/some symbols."""
    engine = create_engine(DB_URL, echo=False)
    api_key = os.getenv("MSTOCK_API_KEY", "")
    mstock_tf = _MSTOCK_INTERVAL_MAP.get(timeframe, timeframe)
    chunk_days = CHUNK_DAYS_MAP.get(timeframe, 800)
    log.info(
        "[data] fetch job starting: timeframe=%s range=%s..%s mstock_tf=%s chunk_days=%d "
        "symbols=%s api_key=%s",
        timeframe,
        from_date,
        to_date,
        mstock_tf,
        chunk_days,
        "all" if not symbols else f"{len(symbols)} requested",
        "set" if api_key else "MISSING — every request will fail",
    )

    # Load instruments
    instruments = _load_instruments(engine, symbols)
    total = len(instruments)
    if not total:
        log.warning(
            "[data] no instruments matched (symbols=%s) — is the `instruments` table "
            "populated? Run scripts/fetch_nifty500_historical.py first",
            ",".join(symbols) if symbols else "NSE/BSE equities",
        )
    else:
        log.info("[data] %d instruments to fetch", total)

    with _lock:
        _job["total"] = total

    for i, inst in enumerate(instruments, 1):
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

        try:
            bars = _fetch_bars_chunked(
                api_key, token, sec_token, from_date, to_date, inst_exchange, mstock_tf, chunk_days
            )
            if not bars:
                log.warning(
                    "[data] %s: API returned no bars for %s..%s (%s) — nothing stored",
                    symbol,
                    from_date,
                    to_date,
                    mstock_tf,
                )
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
        _job["status"] = "done"
        _job["symbol"] = ""
        log.info(
            "[data] fetch job finished: %d/%d symbols ok, %d failed, %d bars inserted",
            _job["fetched"] - _job["failed"],
            total,
            _job["failed"],
            _job["bars_total"],
        )

    engine.dispose()


def _load_instruments(engine, symbols: list[str] | None) -> list[dict]:
    """Load instruments from DB. If symbols list provided, filter to those."""
    if symbols:
        placeholders = ", ".join([f":s{i}" for i in range(len(symbols))])
        sql = text(
            f"SELECT tradingsymbol, instrument_token, name, exchange "
            f"FROM instruments "
            f"WHERE ((exchange = 'NSE' AND instrument_type = 'EQ') OR "
            f"       (exchange = 'BSE' AND instrument_type = 'Equity')) "
            f"AND UPPER(tradingsymbol) IN ({placeholders}) "
            f"ORDER BY tradingsymbol"
        )
        params = {f"s{i}": s.upper() for i, s in enumerate(symbols)}
    else:
        sql = text(
            "SELECT tradingsymbol, instrument_token, name, exchange "
            "FROM instruments "
            "WHERE (exchange = 'NSE' AND instrument_type = 'EQ') OR "
            "      (exchange = 'BSE' AND instrument_type = 'Equity') "
            "ORDER BY tradingsymbol"
        )
        params = {}

    with engine.connect() as conn:
        rows = conn.execute(sql, params).mappings().all()
    return [dict(r) for r in rows]


def _fetch_bars_chunked(
    api_key: str,
    token: str,
    sec_token: str,
    from_date: str,
    to_date: str,
    segment: str,
    mstock_tf: str,
    chunk_days: int,
) -> list[dict]:
    """Fetch OHLCV bars from mStock, chunked by date range."""
    from datetime import datetime, timedelta

    headers = {"X-Mirae-Version": "1", "Authorization": f"token {api_key}:{token}"}
    url = (
        f"{MSTOCK_BASE_URL}/openapi/typea/instruments/historical/{segment}/{sec_token}/{mstock_tf}"
    )

    start = datetime.strptime(from_date, "%Y-%m-%d")
    end = datetime.strptime(to_date, "%Y-%m-%d")
    all_bars = []
    chunk_start = start

    while chunk_start < end:
        chunk_end = min(chunk_start + timedelta(days=chunk_days), end)
        params = {"from": chunk_start.strftime("%Y-%m-%d"), "to": chunk_end.strftime("%Y-%m-%d")}
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=60)
            resp.raise_for_status()
            payload = resp.json()
            bars = _extract_bars(payload)
            all_bars.extend(bars)
        except Exception as exc:  # noqa: BLE001 — skip bad chunks, but say so
            log.warning(
                "[data] chunk %s..%s failed (%s: %s) — those bars are missing",
                chunk_start,
                chunk_end,
                exc.__class__.__name__,
                exc,
            )
        chunk_start = chunk_end + timedelta(days=1)
        time.sleep(0.15)

    return all_bars


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
    import pandas as pd

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
                ts_raw = bar.get("t", bar.get("time", bar.get("timestamp")))
                ts = pd.Timestamp(ts_raw)
                if ts.tzinfo is not None:
                    ts = ts.tz_convert("UTC").tz_localize(None)
                o, h, l, c = (
                    float(bar.get("o", bar.get("open", 0))),
                    float(bar.get("h", bar.get("high", 0))),
                    float(bar.get("l", bar.get("low", 0))),
                    float(bar.get("c", bar.get("close", 0))),
                )
                v = int(bar.get("v", bar.get("volume", 0)))
            elif isinstance(bar, (list, tuple)) and len(bar) >= 6:
                ts = pd.Timestamp(bar[0])
                if ts.tzinfo is not None:
                    ts = ts.tz_convert("UTC").tz_localize(None)
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
                    "ts": ts.to_pydatetime(),
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
