"""Live mStock market-data feed (ticket P3.4).

Replaces the P1.2 stub and folds in the 60-second mStock polling loop that
lived in the old (now-deleted) live forward engine module (P3.4). That
engine's strategy / paper-trade / DB parts belonged to the forward engine
(P1.x re-homed them into ``forward/engine.py`` + the simulator portfolio);
what survives here is the DATA: real-time bars from the mStock TypeA API.

Two interfaces
--------------
* :meth:`MStockLiveFeed.iter_bars` — a generator yielding real-time bars
  roughly every ``poll_interval_s`` seconds (default 60). Duplicate bars
  (same timestamp as the last yielded one) are skipped so a retry can
  never double-feed a bar; the market-hours gate idles instead of
  hammering the API while the exchange is closed.
* :meth:`MStockLiveFeed.get_candles` — the ``DataSource`` contract:
  recent bars for a window as a normalized OHLCV frame (the
  ``SourceRegistry`` hands this class out for ``mode='live'`` and
  ``mode='paper', source='mstock'``).

Credentials are lazy: an explicit ``mstock_client`` (anything with
``get_latest_bar(symbol)`` — and optionally ``get_candles``) is preferred
and keeps the feed fully unit-testable without credentials; otherwise
auth happens at query time via ``backtest.live.auth.get_session_token``
plus the ``MSTOCK_API_KEY`` environment variable. Building an instance
without credentials is therefore safe (the registry does exactly that).
"""

from __future__ import annotations

import logging
import os
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterator

import pandas as pd
import requests

from backtest.data.base import MSTOCK_INTERVAL_MAP, normalize_candles

logger = logging.getLogger("backtest.data.mstock_live_feed")

__all__ = [
    "MStockLiveFeed",
    "MARKET_OPEN_HOUR",
    "MARKET_OPEN_MINUTE",
    "MARKET_CLOSE_HOUR",
    "MARKET_CLOSE_MINUTE",
]

# NSE market hours (IST = UTC+5:30)
MARKET_OPEN_HOUR = 9
MARKET_OPEN_MINUTE = 15
MARKET_CLOSE_HOUR = 15
MARKET_CLOSE_MINUTE = 30

#: Canonical timeframe names → mStock TypeA historical-interval names
#: (ticket P4.3) — the shared wire-translation map from :mod:`backtest.data.base`.
_INTERVAL_MAP = MSTOCK_INTERVAL_MAP


def _market_open(now_utc: datetime | None = None) -> bool:
    """Whether the NSE market is currently open (IST business hours)."""
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    ist = now_utc + timedelta(hours=5, minutes=30)
    if ist.weekday() >= 5:
        return False
    market_open = ist.replace(
        hour=MARKET_OPEN_HOUR, minute=MARKET_OPEN_MINUTE, second=0, microsecond=0
    )
    market_close = ist.replace(
        hour=MARKET_CLOSE_HOUR, minute=MARKET_CLOSE_MINUTE, second=0, microsecond=0
    )
    return market_open <= ist <= market_close


def _typea_headers(api_key: str, token: str) -> dict[str, str]:
    return {"X-Mirae-Version": "1", "Authorization": f"token {api_key}:{token}"}


def _candle_row_to_bar(row: Any) -> dict | None:
    """One ``[ts, open, high, low, close, volume]`` API row → bar dict."""
    if isinstance(row, list) and len(row) >= 6:
        return {
            "ts": row[0],
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": int(row[5]),
        }
    if isinstance(row, dict):
        try:
            return {
                "ts": row.get("ts") or row.get("timestamp"),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": int(row.get("volume", 0)),
            }
        except (KeyError, TypeError, ValueError):
            return None
    return None


def _extract_candles(payload: Any) -> list[Any]:
    """Pull the candle list out of either known mStock response shape."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        data = payload.get("data", payload)
        if isinstance(data, dict):
            return data.get("candles", []) or []
        if isinstance(data, list):
            return data
    return []


def _fetch_bars(
    base_url: str,
    token: str,
    api_key: str,
    security_token: str,
    start: str,
    end: str,
    segment: str = "NSE",
    interval: str = "minute",
) -> list[dict]:
    """Fetch bars for ``[start, end]`` from the TypeA historical endpoint.

    Returns ``[]`` (with a logged error) when the API is unreachable — a
    feed hiccup must never take the poll loop down.
    """
    url = f"{base_url}/openapi/typea/instruments/historical/{segment}/{security_token}/{interval}"
    params = {"from": start, "to": end}
    try:
        resp = requests.get(
            url, headers=_typea_headers(api_key, token), params=params, timeout=15
        )
        resp.raise_for_status()
        rows = _extract_candles(resp.json())
    except Exception as exc:  # noqa: BLE001 — feed hiccups are logged, not fatal
        logger.error("Failed to fetch bars for %s: %s", security_token, exc)
        return []
    bars = [_candle_row_to_bar(row) for row in rows]
    return [b for b in bars if b is not None]


def _resolve_security_token(base_url: str, api_key: str, token: str, symbol: str) -> str:
    """Resolve a symbol to its mStock security token via scriptmaster."""
    resp = requests.get(
        f"{base_url}/openapi/typea/instruments/scriptmaster",
        headers=_typea_headers(api_key, token),
        timeout=30,
    )
    resp.raise_for_status()
    import io

    frame = pd.read_csv(io.StringIO(resp.text), low_memory=False)
    if frame.empty:
        raise ValueError("scriptmaster is empty")

    lower = frame.rename(columns=lambda c: str(c).strip().lower())
    symbol_key = next(
        (c for c in ("tradingsymbol", "symbol", "name") if c in lower.columns), None
    )
    token_key = next(
        (c for c in ("instrument_token", "token", "securitytoken") if c in lower.columns), None
    )
    if not symbol_key or not token_key:
        raise ValueError(f"scriptmaster missing columns: {list(lower.columns)[:10]}")

    matches = lower[lower[symbol_key].astype(str).str.lower() == str(symbol).lower()]
    if matches.empty:
        raise ValueError(f"symbol {symbol} not in scriptmaster")
    return str(matches.iloc[0][token_key])


class MStockLiveFeed:
    """Live OHLCV feed from the mStock broker (ticket P3.4).

    Parameters
    ----------
    mstock_client:
        Optional duck-typed client with ``get_latest_bar(symbol) -> dict |
        None`` (and optionally ``get_candles(symbol, start, end,
        interval) -> pd.DataFrame``). Tests and adapters inject one; with
        ``None`` the feed talks to the API directly with lazy credentials.
    poll_interval_s:
        Seconds between polls for :meth:`iter_bars` (default 60, the
        interval the old live engine used).
    segment:
        mStock segment for the historical endpoint (default ``NSE``).
    base_url:
        API base URL override; defaults to ``MSTOCK_BASE_URL`` / the
        standard mStock host at query time.
    """

    def __init__(
        self,
        mstock_client: Any = None,
        poll_interval_s: float = 60,
        segment: str = "NSE",
        base_url: str | None = None,
        **kwargs: Any,
    ) -> None:
        self.client = mstock_client
        self.interval = float(poll_interval_s)
        self.segment = str(segment or "NSE")
        self._base_url_override = base_url
        # Kept for SourceRegistry compatibility (the P1.2 stub stored any
        # extra kwargs here).
        self._config: dict[str, Any] = dict(kwargs)
        self._security_tokens: dict[str, str] = {}

    # -- credentials (lazy) ------------------------------------------------

    def _credentials(self) -> tuple[str, str]:
        """Session token + API key, resolved at query time (never at build)."""
        from backtest.live.auth import get_session_token

        api_key = os.getenv("MSTOCK_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError(
                "MSTOCK_API_KEY is not configured — set it (or inject mstock_client) "
                "before querying the live feed"
            )
        return get_session_token(), api_key

    def _base_url(self) -> str:
        url = self._base_url_override or os.getenv("MSTOCK_BASE_URL", "https://api.mstock.trade")
        return str(url).rstrip("/")

    def _security_token_for(self, symbol: str) -> str:
        key = str(symbol).strip().upper()
        if key not in self._security_tokens:
            token, api_key = self._credentials()
            self._security_tokens[key] = _resolve_security_token(
                self._base_url(), api_key, token, key
            )
        return self._security_tokens[key]

    # -- bar access ----------------------------------------------------------

    def latest_bar(self, symbol: str) -> dict | None:
        """The latest bar for ``symbol`` (``None`` when none is available)."""
        if self.client is not None:
            return self.client.get_latest_bar(symbol)
        token, api_key = self._credentials()
        today = date.today().strftime("%Y-%m-%d")
        start = (date.today() - timedelta(days=3)).strftime("%Y-%m-%d")
        bars = _fetch_bars(
            self._base_url(), token, api_key,
            self._security_token_for(symbol), start, today, self.segment, "minute",
        )
        return bars[-1] if bars else None

    def iter_bars(
        self,
        symbol: str,
        max_bars: int | None = None,
        stop_check: Callable[[], bool] | None = None,
        market_gate: bool = True,
        sleep: Callable[[float], None] = time.sleep,
    ) -> Iterator[dict]:
        """Yield real-time bars, one poll every ``self.interval`` seconds.

        * A bar is yielded only when its timestamp differs from the last
          yielded one — a retried poll never double-feeds a bar.
        * While ``market_gate`` is on and the exchange is closed, the loop
          sleeps without calling the API.
        * ``stop_check`` (a zero-arg callable) is consulted before each
          poll — a ``threading.Event``-style stop hook.
        * ``max_bars`` bounds the number of yielded bars (testing / warmup).
        """
        yielded = 0
        last_ts: Any = None
        while True:
            if stop_check is not None and stop_check():
                break
            if market_gate and not _market_open():
                sleep(self.interval)
                continue
            bar = self.latest_bar(symbol)
            if bar is not None and bar.get("ts") != last_ts:
                last_ts = bar.get("ts")
                yield bar
                yielded += 1
            if max_bars is not None and yielded >= max_bars:
                break
            sleep(self.interval)

    # -- DataSource contract --------------------------------------------------

    def get_candles(
        self, symbol: str, start: str, end: str, interval: str = "1day"
    ) -> pd.DataFrame:
        """Recent bars for the window as a normalized OHLCV frame.

        A client providing ``get_candles`` wins (adapters may already have
        the history); otherwise the feed fetches from the TypeA historical
        endpoint. A live feed is fundamentally real-time — a window far in
        the past returns whatever the API still serves, and an empty frame
        (rather than a raise) when nothing is available.
        """
        if self.client is not None and hasattr(self.client, "get_candles"):
            frame = self.client.get_candles(symbol, start, end, interval)
            if frame is not None and not frame.empty:
                return frame
        try:
            token, api_key = self._credentials()
        except RuntimeError as exc:
            logger.warning("live feed unavailable for %s: %s", symbol, exc)
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        key = str(interval).strip().lower()
        if key not in _INTERVAL_MAP:
            raise ValueError(
                f"interval {interval!r} not supported by the mStock live feed "
                f"(supported: {', '.join(_INTERVAL_MAP)})"
            )
        bars = _fetch_bars(
            self._base_url(),
            token,
            api_key,
            self._security_token_for(symbol),
            str(start),
            str(end),
            self.segment,
            _INTERVAL_MAP[key],
        )
        if not bars:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        frame = pd.DataFrame(bars)
        frame["ts"] = pd.to_datetime(frame["ts"])
        frame = frame.set_index("ts").sort_index()
        return normalize_candles(frame)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"<MStockLiveFeed client={'yes' if self.client is not None else 'http'} "
            f"interval={self.interval:.0f}s segment={self.segment}>"
        )
