"""mStock API client and live data source."""

from __future__ import annotations

import io
import os
from typing import Any

import pandas as pd
import requests

from backtest.data.base import MSTOCK_INTERVAL_MAP, normalize_candles
from backtest.live.auth import get_session_token


class MStockClient:
    """mStock API client."""

    def __init__(self, token: str | None = None):
        self.base_url = os.getenv("MSTOCK_BASE_URL", "https://api.mstock.trade").rstrip("/")
        self.api_key = os.getenv("MSTOCK_API_KEY", "").strip()
        self.token = token or get_session_token()
        self.headers = {"X-Mirae-Version": "1"}
        if self.api_key and self.token:
            self.headers["Authorization"] = f"token {self.api_key}:{self.token}"

    def _resolve_security_token(self, symbol: str, segment: str = "NSE") -> str:
        """Resolve a trading symbol such as NIFTY to the TypeA security token."""
        resp = requests.get(
            f"{self.base_url}/openapi/typea/instruments/scriptmaster",
            headers=self.headers,
            timeout=30,
        )
        resp.raise_for_status()

        csv_text = resp.text
        frame = pd.read_csv(io.StringIO(csv_text), low_memory=False)
        if frame.empty:
            raise ValueError(f"scriptmaster for {segment} was empty")

        lower_frame = frame.rename(columns=lambda c: str(c).strip().lower())
        symbol_key = None
        for candidate in ["tradingsymbol", "symbol", "name", "instrumentname"]:
            if candidate in lower_frame.columns:
                symbol_key = candidate
                break
        token_key = None
        for candidate in ["instrument_token", "token", "securitytoken", "security_token"]:
            if candidate in lower_frame.columns:
                token_key = candidate
                break
        if symbol_key is None or token_key is None:
            raise ValueError(f"scriptmaster response missing symbol/token columns: {list(lower_frame.columns)[:10]}")

        matches = lower_frame[lower_frame[symbol_key].astype(str).str.lower() == symbol.lower()]
        if matches.empty:
            raise ValueError(f"symbol {symbol} not found in scriptmaster")
        return str(matches.iloc[0][token_key])

    def _extract_bars(self, payload: Any) -> list[dict]:
        """Normalize various mStock historical response shapes to a list of bar dicts."""
        if isinstance(payload, list):
            return payload
        if not isinstance(payload, dict):
            return []

        for key in ["data", "candles", "result", "bars", "historical"]:
            if key in payload:
                value = payload[key]
                if isinstance(value, list):
                    return value
                if isinstance(value, dict):
                    nested = self._extract_bars(value)
                    if nested:
                        return nested
        return []

    def get_bars(self, symbol: str, start: str, end: str, interval: str = "day") -> list[dict]:
        """Fetch OHLCV bars from mStock using the TypeA historical route."""
        segment = "NSE"
        security_token = self._resolve_security_token(symbol, segment)
        endpoint = f"{self.base_url}/openapi/typea/instruments/historical/{segment}/{security_token}/{interval}"
        params = {"from": start, "to": end}
        resp = requests.get(endpoint, headers=self.headers, params=params, timeout=30)
        resp.raise_for_status()
        payload = resp.json()
        bars = self._extract_bars(payload)
        if not bars:
            raise ValueError(f"no bars returned for {symbol} from mStock historical endpoint")
        return bars

    def get_latest(self, symbol: str) -> dict:
        """Fetch latest price for a symbol."""
        endpoint = f"{self.base_url}/latest"
        params = {"symbol": symbol}
        resp = requests.get(endpoint, headers=self.headers, params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()


def _candles_to_frame(bars: list[Any]) -> pd.DataFrame:
    """Convert mStock bar list to canonical OHLCV frame."""
    if not bars:
        raise ValueError("no bars returned from mStock")

    data = []
    index_values = []
    for bar in bars:
        if isinstance(bar, dict):
            timestamp = bar.get("t", bar.get("time", bar.get("timestamp")))
            values = {
                "open": bar.get("o", bar.get("open", 0)),
                "high": bar.get("h", bar.get("high", 0)),
                "low": bar.get("l", bar.get("low", 0)),
                "close": bar.get("c", bar.get("close", 0)),
                "volume": bar.get("v", bar.get("volume", 0)),
            }
        elif isinstance(bar, (list, tuple)) and len(bar) >= 6:
            timestamp, open_value, high_value, low_value, close_value, volume = bar[:6]
            values = {
                "open": open_value,
                "high": high_value,
                "low": low_value,
                "close": close_value,
                "volume": volume,
            }
        else:
            raise ValueError(f"unsupported mStock candle format: {bar!r}")

        index_values.append(timestamp)
        data.append({
            "open": float(values["open"]),
            "high": float(values["high"]),
            "low": float(values["low"]),
            "close": float(values["close"]),
            "volume": int(values["volume"]),
        })

    try:
        index = pd.to_datetime(index_values, utc=True).tz_convert(None)
    except Exception as e:
        raise ValueError(f"failed to parse timestamps from mStock: {e}")

    frame = pd.DataFrame(data, index=index)
    return normalize_candles(frame)


class MStockSource:
    """Live market data source via mStock API."""

    def __init__(self, token: str | None = None):
        self.client = MStockClient(token)

    def get_candles(self, symbol: str, start: str, end: str, interval: str = "1day") -> pd.DataFrame:
        """Fetch candles from mStock and return canonical frame.

        ``interval`` is a canonical timeframe (ticket P4.3); it is translated
        to the mStock wire name before the request.
        """
        wire = MSTOCK_INTERVAL_MAP.get(interval, interval)
        bars = self.client.get_bars(symbol, start, end, wire)
        return _candles_to_frame(bars)
