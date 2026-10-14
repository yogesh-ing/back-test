"""Live Upstox market-data feed (2026-10-13) — mirror of DhanLiveFeed.

Implements the duck-type the feed bus consumes — ``latest_bar(symbol) -> dict
| None`` and ``get_candles(...)`` — against the Upstox v2/v3 REST API
(endpoints per ``reference-code/upstox-python-master`` + the v3 docs):

* quotes:      ``GET /v2/market-quote/quotes?instrument_key=...``
               (day running OHLC + LTP + volume)
* candles:     ``GET /v3/historical-candle/intraday/{key}/minutes/1`` for
               today, ``GET /v3/historical-candle/{key}/minutes/1/{to}/{from}``
               for earlier days (row shape ``[ts, o, h, l, c, volume, oi]``)

Authentication is the Upstox access token from the Upstox login flow (OAuth)
via the broker session manager, sent as ``Authorization: Bearer`` +
``Api-Version`` — the same headers ``upstox_client`` sets.

Instrument keys: Upstox addresses instruments by key (``NSE_INDEX|Nifty 50``,
``NSE_EQ|INE002A01018``), so symbols are resolved through
:func:`backtest.brokers.upstox.resolve_instrument_key` (seeded index keys →
``UPSTOX_INSTRUMENT_KEYS`` env override → the instrument master).

Credentials are lazy (resolved at query time from the broker session
manager), so building an instance without an active session is safe — the
registry does exactly that.
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

logger = logging.getLogger("backtest.data.upstox_live_feed")

__all__ = ["UpstoxLiveFeed", "UpstoxAPIError"]

API_BASE_URL = "https://api.upstox.com/v2"
API_BASE_URL_V3 = "https://api.upstox.com/v3"

#: Retry posture mirrors the mStock/Dhan feeds (one retry after a backoff).
_RETRY_BACKOFF_S = 1.0

_EMPTY_COLUMNS = ["open", "high", "low", "close", "volume"]


class UpstoxAPIError(RuntimeError):
    """An Upstox data-API call failed after retries (user-facing message)."""


def _ist_naive(now_utc: datetime | None = None) -> datetime:
    return (now_utc or datetime.now(timezone.utc)) + timedelta(hours=5, minutes=30)


class UpstoxLiveFeed:
    """Live OHLCV feed from Upstox (duck-types MStockLiveFeed).

    Parameters
    ----------
    client:
        Optional injectable client with ``get_ohlc(symbol)`` /
        ``get_candles(...)`` for tests; ``None`` talks to the API directly
        with lazy credentials.
    base_url:
        v2 API base URL override (defaults to ``UPSTOX_API_BASE_URL`` env /
        the Upstox host).
    """

    def __init__(self, client: Any = None, base_url: str | None = None, **kwargs: Any) -> None:
        self.client = client
        self._base_url_override = base_url

    # -- credentials (lazy) --------------------------------------------------

    def _credentials(self) -> str:
        """The Upstox access token — resolved at query time.

        Asks the session manager for THIS broker's session (not the
        UI-active one), so an mStock-active UI still feeds source=upstox
        runners as long as the Upstox session itself is valid.
        """
        from backtest.brokers.session_manager import get_session_manager

        mgr = get_session_manager()
        token = mgr.get_active_session_token("upstox")
        if not token:
            raise UpstoxAPIError(
                "no active Upstox session — log in (API key + API secret, then the auth code)"
            )
        return token

    def _base_url(self) -> str:
        url = self._base_url_override or os.getenv("UPSTOX_API_BASE_URL", API_BASE_URL)
        return str(url).rstrip("/")

    @staticmethod
    def _base_url_v3() -> str:
        return os.getenv("UPSTOX_API_BASE_URL_V3", API_BASE_URL_V3).rstrip("/")

    def _instrument_key_for(self, symbol: str) -> str:
        from backtest.brokers.upstox import resolve_instrument_key

        try:
            return resolve_instrument_key(symbol)
        except Exception as exc:  # noqa: BLE001 — surfaced as a feed warning
            raise UpstoxAPIError(str(exc)) from exc

    # -- wire calls ----------------------------------------------------------

    def _request(
        self,
        url: str,
        api_version: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        token = self._credentials()
        headers = {
            "Authorization": f"Bearer {token}",
            "Api-Version": api_version,
            "Accept": "application/json",
        }
        last_exc: Exception | None = None
        for attempt in range(2):
            try:
                resp = requests.get(url, params=params, headers=headers, timeout=15)
                if resp.status_code in (401, 403):
                    raise UpstoxAPIError("Upstox session rejected the request — re-login")
                if resp.status_code == 429:
                    raise UpstoxAPIError("Upstox rate limit hit — slow down the poll")
                resp.raise_for_status()
                return resp.json()
            except UpstoxAPIError:
                raise
            except Exception as exc:  # noqa: BLE001 — one retry on transient errors
                last_exc = exc
                if attempt == 0:
                    time.sleep(_RETRY_BACKOFF_S)
        raise UpstoxAPIError(f"Upstox request failed: {last_exc}")

    def get_ohlc(self, symbol: str) -> dict[str, Any] | None:
        """Day running OHLC + LTP + volume for ``symbol`` (``/market-quote/quotes``)."""
        if self.client is not None:
            getter = getattr(self.client, "get_ohlc", None)
            if callable(getter):
                return getter(symbol)
        key = self._instrument_key_for(symbol)
        payload = self._request(
            f"{self._base_url()}/market-quote/quotes",
            "2.0",
            params={"instrument_key": key},
        )
        data = payload.get("data") if isinstance(payload, dict) else None
        entry = None
        if isinstance(data, dict):
            entry = data.get(key)
            if entry is None and len(data) == 1:
                entry = next(iter(data.values()))
        if not isinstance(entry, dict):
            return None
        ohlc = entry.get("ohlc") or {}
        last = entry.get("last_price")
        if last is None:
            return None
        volume = entry.get("volume") or entry.get("total_volume") or 0
        return {
            "ohlc": {
                "open": ohlc.get("open"),
                "high": ohlc.get("high"),
                "low": ohlc.get("low"),
                "close": ohlc.get("close"),
                "volume": volume,
            },
            "last_price": float(last),
            "timestamp": entry.get("timestamp"),
        }

    # -- feed-bus duck-type ---------------------------------------------------

    def latest_bar(self, symbol: str) -> dict | None:
        """Latest bar for ``symbol`` (same shape as MStockLiveFeed.latest_bar).

        Built from the day-session ``/market-quote/quotes``: OHLC is the
        session's running aggregate, ``ts`` is the quote timestamp (or the
        minute floor of now, IST) — same convention as the Dhan feed.
        """
        try:
            quote = self.get_ohlc(symbol)
        except UpstoxAPIError as exc:
            logger.warning("upstox feed: ohlc(%s) failed: %s", symbol, exc)
            return None
        if not quote:
            return None
        ohlc = quote["ohlc"]
        last = quote["last_price"]
        ts = None
        raw_ts = quote.get("timestamp")
        if isinstance(raw_ts, (int, float)) and raw_ts > 1_000_000_000:
            unit = "ms" if raw_ts > 1_000_000_000_000 else "s"
            ts = pd.to_datetime(raw_ts, unit=unit, utc=True).tz_convert(
                "Asia/Kolkata"
            ).tz_localize(None)
        elif isinstance(raw_ts, str) and raw_ts.strip():
            try:
                parsed = pd.to_datetime(raw_ts)
                if parsed.tzinfo is not None:
                    parsed = parsed.tz_convert("Asia/Kolkata").tz_localize(None)
                ts = parsed
            except ValueError:
                ts = None
        if ts is None:
            ts = _ist_naive()
        ts = ts.replace(second=0, microsecond=0)
        return {
            "ts": ts.strftime("%Y-%m-%d %H:%M:%S"),
            "open": float(ohlc.get("open") or last),
            "high": float(ohlc.get("high") or last),
            "low": float(ohlc.get("low") or last),
            "close": float(last),
            "volume": float(ohlc.get("volume") or 0),
        }

    def get_candles(
        self, symbol: str, start: str, end: str, interval: str = "1min"
    ) -> pd.DataFrame:
        """Intraday candles for ``[start, end]`` (v3 intraday + historical)."""
        if self.client is not None and hasattr(self.client, "get_candles"):
            frame = self.client.get_candles(symbol, start, end, interval)
            if frame is not None and not frame.empty:
                return frame
        try:
            key = self._instrument_key_for(symbol)
        except UpstoxAPIError as exc:
            logger.warning("upstox feed: candles unavailable for %s: %s", symbol, exc)
            return pd.DataFrame(columns=_EMPTY_COLUMNS)

        unit, step = _unit_interval(interval)
        today = datetime.now().date()
        start_day = _parse_day(start, default=today)
        end_day = _parse_day(end, default=today)

        rows: list[list[Any]] = []
        try:
            # Days before today → the dated historical endpoint.
            if start_day < today:
                to_day = min(end_day, today - timedelta(days=1))
                rows.extend(
                    self._fetch_historical(key, unit, step, to_day, start_day)
                )
            # Today (when the range reaches it) → the intraday endpoint,
            # which is the only one Upstox fills with the running session.
            if end_day >= today:
                rows.extend(self._fetch_intraday(key, unit, step))
        except UpstoxAPIError as exc:
            logger.warning("upstox feed: candles(%s) failed: %s", symbol, exc)
            return pd.DataFrame(columns=_EMPTY_COLUMNS)

        bars = _rows_to_bars(rows)
        if not bars:
            return pd.DataFrame(columns=_EMPTY_COLUMNS)
        frame = pd.DataFrame(bars).set_index("ts").sort_index()
        frame = frame[~frame.index.duplicated(keep="last")]
        # Window filter — the endpoints answer in days, callers ask in ranges.
        try:
            lo = pd.to_datetime(str(start))
            hi = pd.to_datetime(str(end)) + timedelta(days=1) - timedelta(seconds=1)
            frame = frame.loc[(frame.index >= lo) & (frame.index <= hi)]
        except (ValueError, TypeError):
            pass
        if frame.empty:
            return pd.DataFrame(columns=_EMPTY_COLUMNS)
        return normalize_candles(frame)

    # -- endpoint helpers ------------------------------------------------------

    def _fetch_intraday(self, key: str, unit: str, step: int) -> list[list[Any]]:
        payload = self._request(
            f"{self._base_url_v3()}/historical-candle/intraday/{_encode(key)}/{unit}/{step}",
            "3.0",
        )
        return _candles_from(payload)

    def _fetch_historical(
        self, key: str, unit: str, step: int, to_day: Any, from_day: Any
    ) -> list[list[Any]]:
        payload = self._request(
            f"{self._base_url_v3()}/historical-candle/{_encode(key)}/{unit}/{step}"
            f"/{to_day.isoformat()}/{from_day.isoformat()}",
            "3.0",
        )
        return _candles_from(payload)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<UpstoxLiveFeed client={'yes' if self.client is not None else 'http'}>"


def _encode(key: str) -> str:
    """URL-encode an instrument key (``NSE_EQ|INE002A01018`` keeps readable parts)."""
    from urllib.parse import quote

    return quote(key, safe="|_")


def _candles_from(payload: Any) -> list[list[Any]]:
    data = payload.get("data") if isinstance(payload, dict) else None
    candles = data.get("candles") if isinstance(data, dict) else None
    if not isinstance(candles, list):
        return []
    return [row for row in candles if isinstance(row, (list, tuple)) and len(row) >= 6]


def _rows_to_bars(rows: list[list[Any]]) -> list[dict[str, Any]]:
    """``[ts, o, h, l, c, volume, oi]`` rows → naive-IST bar dicts."""
    bars: list[dict[str, Any]] = []
    for row in rows:
        try:
            ts = pd.to_datetime(row[0])
            if ts.tzinfo is not None:
                # Upstox stamps +05:30 — keep the IST wall clock, drop tz
                # (the feed bus and every other source are naive-IST).
                ts = ts.tz_convert("Asia/Kolkata").tz_localize(None)
            bars.append(
                {
                    "ts": ts.to_pydatetime(),
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": float(row[5] or 0),
                }
            )
        except (TypeError, ValueError, IndexError):
            continue
    return bars


def _parse_day(value: str, default: Any) -> Any:
    try:
        return pd.to_datetime(str(value)).date()
    except (TypeError, ValueError):
        return default


def _unit_interval(interval: str) -> tuple[str, int]:
    """``"1min"`` → ``("minutes", 1)``; unknown → 1-minute bars."""
    raw = str(interval or "1min").strip().lower()
    for suffix, unit in (("minute", "minutes"), ("min", "minutes"), ("hour", "hours"), ("day", "days")):
        if raw.endswith(suffix):
            head = raw[: -len(suffix)] or "1"
            break
        if raw.endswith(suffix + "s"):
            head = raw[: -(len(suffix) + 1)] or "1"
            break
    else:
        head, unit = "1", "minutes"
    try:
        step = max(1, int(head))
    except ValueError:
        step = 1
    if unit == "days":
        step = 1
    return unit, step
