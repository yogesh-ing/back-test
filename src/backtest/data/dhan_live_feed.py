"""Live Dhan market-data feed (2026-09-25) — mirror of MStockLiveFeed.

Implements the duck-type the feed bus consumes — ``latest_bar(symbol) ->
dict | None`` and ``get_candles(...)`` — against the DhanHQ v2 REST API
(endpoints per ``reference-code/DhanHQ-py-main``):

* quotes:      ``POST /v2/marketfeed/ohlc`` (today's running OHLC + LTP)
* intraday:    ``POST /v2/charts/intraday`` (1-minute candles, last 5 days)

Authentication is the Dhan access token (from the Dhan login flow via the
broker session manager) plus the client id, sent as
``access-token`` / ``client-id`` headers — same as DhanHTTP in the SDK.

Index → security-id mapping: Dhan uses the NSE security IDs (NIFTY 11957,
BANKNIFTY 49081 after the 2024 index renumbering — verified against the SDK
docs' examples); override via the ``DHAN_INDEX_SECURITY_IDS`` env var
(JSON) when Dhan renumbers again.

Credentials are lazy (resolved at query time from the broker session
manager + ``DHAN_API_KEY`` env placeholder), so building an instance
without an active session is safe — the registry does exactly that.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd
import requests

from backtest.data.base import normalize_candles

logger = logging.getLogger("backtest.data.dhan_live_feed")

__all__ = ["DhanLiveFeed", "DHAN_INDEX_SECURITY_IDS", "DhanAPIError"]

API_BASE_URL = "https://api.dhan.co/v2"

#: NSE index security IDs (Dhan convention). NIFTY 11957 / BANKNIFTY 49081
#: per DhanHQ-conventions.csv; override via DHAN_INDEX_SECURITY_IDS env JSON.
DHAN_INDEX_SECURITY_IDS: dict[str, str] = {
    "NIFTY": "11957",
    "NIFTY50": "11957",
    "NIFTY 50": "11957",
    "BANKNIFTY": "49081",
    "NIFTY BANK": "49081",
    "NIFTYBANK": "49081",
}

#: NSE segment for index/derivatives quotes (Dhan exchange-segment codes).
IDX_SEGMENT = "IDX_I"

#: Retry posture mirrors the mStock feed (one retry after a short backoff).
_RETRY_BACKOFF_S = 1.0


class DhanAPIError(RuntimeError):
    """A Dhan data-API call failed after retries (user-facing message)."""


def _ist_naive(now_utc: datetime | None = None) -> datetime:
    return (now_utc or datetime.now(timezone.utc)) + timedelta(hours=5, minutes=30)


class DhanLiveFeed:
    """Live OHLCV feed from Dhan (duck-types MStockLiveFeed).

    Parameters
    ----------
    client:
        Optional injectable client with ``get_ohlc(symbol) -> dict | None``
        for tests; ``None`` talks to the API directly with lazy credentials.
    base_url:
        API base URL override (defaults to ``DHAN_BASE_URL`` env / v2 host).
    """

    def __init__(self, client: Any = None, base_url: str | None = None, **kwargs: Any) -> None:
        self.client = client
        self._base_url_override = base_url
        self._security_ids: dict[str, str] = {}

    # -- credentials (lazy) --------------------------------------------------

    def _credentials(self) -> tuple[str, str]:
        """``(access_token, client_id)`` — resolved at query time.

        The access token comes from the active broker session (Dhan login
        via the session manager); the client id from ``DHAN_CLIENT_ID`` env
        or the token call's parked value. Never the other way around.
        """
        from backtest.brokers.session_manager import get_session_manager

        mgr = get_session_manager()
        broker = mgr.get_active_broker()
        if getattr(broker, "broker_name", "") != "dhan":
            raise DhanAPIError(
                "active broker session is not Dhan — log in via the Dhan flow first"
            )
        token = mgr.get_active_session_token()
        if not token:
            raise DhanAPIError("no active Dhan session — log in (client ID + PIN + TOTP)")
        client_id = os.getenv("DHAN_CLIENT_ID", "").strip()
        if not client_id:
            raise DhanAPIError("DHAN_CLIENT_ID is not configured in .env")
        return token, client_id

    def _base_url(self) -> str:
        url = self._base_url_override or os.getenv("DHAN_BASE_URL", API_BASE_URL)
        return str(url).rstrip("/")

    def _security_id_for(self, symbol: str) -> str:
        key = str(symbol).strip().upper()
        if key not in self._security_ids:
            ids = dict(DHAN_INDEX_SECURITY_IDS)
            raw = os.getenv("DHAN_INDEX_SECURITY_IDS", "").strip()
            if raw:
                try:
                    import json as _json

                    ids.update({str(k).upper(): str(v) for k, v in _json.loads(raw).items()})
                except ValueError:
                    logger.warning("DHAN_INDEX_SECURITY_IDS is not valid JSON — ignored")
            if key not in ids:
                raise DhanAPIError(
                    f"no Dhan security id for {key!r} — set DHAN_INDEX_SECURITY_IDS"
                )
            self._security_ids[key] = ids[key]
        return self._security_ids[key]

    # -- wire calls ----------------------------------------------------------

    def _post(self, path: str, payload: dict[str, Any]) -> Any:
        token, client_id = self._credentials()
        headers = {
            "access-token": token,
            "client-id": client_id,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        url = f"{self._base_url()}{path}"
        last_exc: Exception | None = None
        for attempt in range(2):
            try:
                resp = requests.post(
                    url, json={**payload, "dhanClientId": client_id},
                    headers=headers, timeout=15,
                )
                if resp.status_code in (401, 403):
                    raise DhanAPIError("Dhan session rejected the request — re-login")
                if resp.status_code == 429:
                    raise DhanAPIError("Dhan rate limit hit — slow down the poll")
                resp.raise_for_status()
                return resp.json()
            except DhanAPIError:
                raise
            except Exception as exc:  # noqa: BLE001 — one retry on transient errors
                last_exc = exc
                if attempt == 0:
                    time.sleep(_RETRY_BACKOFF_S)
        raise DhanAPIError(f"Dhan request failed: {last_exc}")

    def get_ohlc(self, symbol: str) -> dict[str, Any] | None:
        """Today's running OHLC + LTP for ``symbol`` via ``/marketfeed/ohlc``."""
        if self.client is not None:
            getter = getattr(self.client, "get_ohlc", None)
            if callable(getter):
                return getter(symbol)
        security_id = self._security_id_for(symbol)
        payload = self._post(
            "/marketfeed/ohlc", {IDX_SEGMENT: [security_id]}
        )
        # Response shape: {"data": {"IDX_I": {"11957": {"ohlc": {...},
        # "last_price": ...}}}, "status": "success"} (per SDK MarketFeed).
        data = (payload.get("data") or {}).get(IDX_SEGMENT) or {}
        entry = data.get(security_id) or {}
        ohlc = entry.get("ohlc") or {}
        last = entry.get("last_price")
        if not ohlc or last is None:
            return None
        return {"ohlc": ohlc, "last_price": float(last)}

    # -- feed-bus duck-type ---------------------------------------------------

    def latest_bar(self, symbol: str) -> dict | None:
        """Latest bar for ``symbol`` (same shape as MStockLiveFeed.latest_bar).

        Built from the today-session ``/marketfeed/ohlc``: OHLC is the
        session's running aggregate, ``ts`` is the minute floor of now (IST).
        """
        try:
            quote = self.get_ohlc(symbol)
        except DhanAPIError as exc:
            logger.warning("dhan feed: ohlc(%s) failed: %s", symbol, exc)
            return None
        if not quote:
            return None
        ohlc = quote["ohlc"]
        last = quote["last_price"]
        ts = _ist_naive().replace(second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
        return {
            "ts": ts,
            "open": float(ohlc.get("open") or last),
            "high": float(ohlc.get("high") or last),
            "low": float(ohlc.get("low") or last),
            "close": float(last),
            "volume": float(ohlc.get("volume") or 0),
        }

    def get_candles(
        self, symbol: str, start: str, end: str, interval: str = "1min"
    ) -> pd.DataFrame:
        """Intraday candles via ``/charts/intraday`` (last 5 trading days)."""
        if self.client is not None and hasattr(self.client, "get_candles"):
            frame = self.client.get_candles(symbol, start, end, interval)
            if frame is not None and not frame.empty:
                return frame
        try:
            security_id = self._security_id_for(symbol)
        except DhanAPIError as exc:
            logger.warning("dhan feed: candles unavailable for %s: %s", symbol, exc)
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        try:
            payload = self._post(
                "/charts/intraday",
                {
                    "securityId": security_id,
                    "exchangeSegment": IDX_SEGMENT,
                    "instrument": "INDEX",
                    "interval": 1,
                    "oi": False,
                    "fromDate": str(start)[:10],
                    "toDate": str(end)[:10],
                },
            )
        except DhanAPIError as exc:
            logger.warning("dhan feed: candles(%s) failed: %s", symbol, exc)
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        rows = payload.get("data") or {}
        opens = rows.get("open") or []
        highs = rows.get("high") or []
        lows = rows.get("low") or []
        closes = rows.get("close") or []
        volumes = rows.get("volume") or []
        stamps = rows.get("timestamp") or []
        bars = []
        for i, close in enumerate(closes):
            if i >= len(stamps):
                break
            ts = datetime.utcfromtimestamp(float(stamps[i])) + timedelta(  # noqa: DTZ006
                hours=5, minutes=30
            )
            bars.append(
                {
                    "ts": ts,
                    "open": float(opens[i]) if i < len(opens) else float(close),
                    "high": float(highs[i]) if i < len(highs) else float(close),
                    "low": float(lows[i]) if i < len(lows) else float(close),
                    "close": float(close),
                    "volume": float(volumes[i]) if i < len(volumes) else 0.0,
                }
            )
        if not bars:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        frame = pd.DataFrame(bars).set_index("ts").sort_index()
        return normalize_candles(frame)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<DhanLiveFeed client={'yes' if self.client is not None else 'http'}>"
