"""Opt-in live mStock integration test.

Run explicitly with RUN_MSTOCK_LIVE_TEST=1. Requires a fresh authenticator TOTP.
"""

from __future__ import annotations

import io
import os
from pathlib import Path

import pandas as pd
import pytest
import requests

from backtest.live.auth import get_auth_code, login, verify_totp
from backtest.live.mstock import MStockClient, _candles_to_frame


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_MSTOCK_LIVE_TEST") != "1",
    reason="set RUN_MSTOCK_LIVE_TEST=1 to run against mStock",
)


def _find_column(frame: pd.DataFrame, candidates: tuple[str, ...]) -> str | None:
    columns = {str(column).strip().lower(): column for column in frame.columns}
    for candidate in candidates:
        if candidate in columns:
            return columns[candidate]
    return None


def _resolve_nifty_index(frame: pd.DataFrame) -> str:
    """Resolve NIFTY index token from provider instrument master, never by guess."""
    token_column = _find_column(
        frame,
        (
            "instrument_token",
            "token",
            "securitytoken",
            "security_token",
            "securityidcode",
            "securityid",
            "scripcode",
            "security_id",
        ),
    )
    if token_column is None:
        raise AssertionError(f"instrument master has no security-token column: {list(frame.columns)}")

    text_frame = frame.fillna("").astype(str)
    searchable_columns = [
        column
        for column in frame.columns
        if str(column).strip().lower()
        in {"symbol", "tradingsymbol", "name", "instrumentname", "description", "underlying"}
    ]
    if not searchable_columns:
        searchable_columns = list(frame.columns)

    nifty_mask = text_frame[searchable_columns].apply(
        lambda column: column.str.contains("NIFTY", case=False, regex=False)
    ).any(axis=1)
    derivative_mask = text_frame[searchable_columns].apply(
        lambda column: column.str.contains("FUT|OPT| CE| PE", case=False, regex=True)
    ).any(axis=1)
    non_index_mask = text_frame[searchable_columns].apply(
        lambda column: column.str.contains("ETF|BEES", case=False, regex=True)
    ).any(axis=1)
    index_mask = text_frame[searchable_columns].apply(
        lambda column: column.str.contains("NIFTY ?50|INDEX", case=False, regex=True)
    ).any(axis=1)
    candidates = frame[nifty_mask & index_mask & ~derivative_mask & ~non_index_mask]
    if candidates.empty:
        candidates = frame[nifty_mask & ~derivative_mask & ~non_index_mask]
    if candidates.empty:
        candidates = frame[nifty_mask]
    if candidates.empty:
        raise AssertionError("instrument master contains no NIFTY instrument")

    token = str(candidates.iloc[0][token_column]).strip()
    if not token or token.lower() == "nan":
        raise AssertionError("NIFTY index row has empty security token")
    return token


def test_live_nifty_index_history_to_csv():
    """Authenticate, resolve NIFTY from scriptmaster, fetch daily history, save CSV."""
    login_response = login()
    assert login_response.get("status") == "success", login_response

    code = get_auth_code()
    session = verify_totp(code)
    access_token = session["token"]
    api_key = os.environ["MSTOCK_API_KEY"].strip()
    base_url = os.getenv("MSTOCK_BASE_URL", "https://api.mstock.trade").rstrip("/")
    headers = {
        "X-Mirae-Version": "1",
        "Authorization": f"token {api_key}:{access_token}",
    }

    instruments_response = requests.get(
        f"{base_url}/openapi/typea/instruments/scriptmaster",
        headers=headers,
        timeout=30,
    )
    instruments_response.raise_for_status()
    instruments = pd.read_csv(io.StringIO(instruments_response.text), low_memory=False)
    nifty_token = _resolve_nifty_index(instruments)

    client = MStockClient(token=access_token)
    history_response = requests.get(
        f"{base_url}/openapi/typea/instruments/historical/NSE/{nifty_token}/day",
        headers=client.headers,
        params={"from": "2024-01-01", "to": "2024-06-30"},
        timeout=30,
    )
    history_response.raise_for_status()
    bars = client._extract_bars(history_response.json())
    assert bars, history_response.text[:500]

    candles = _candles_to_frame(bars)
    assert not candles.empty
    assert list(candles.columns) == ["open", "high", "low", "close", "volume"]

    output_path = Path("data/live/nifty_index_2024_h1.csv")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    candles.to_csv(output_path)
    print(f"saved={output_path} rows={len(candles)} token={nifty_token}")
