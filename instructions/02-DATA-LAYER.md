# Card 02 — Data layer (`data/`)

**Prerequisite:** Cards 00–01. **Builds:** all data sources returning the
canonical candle frame (Card 00 rule 8).

## `data/base.py`
- `CANDLE_COLUMNS = ["open","high","low","close","volume"]`.
- `DataSource` — runtime-checkable `typing.Protocol` with
  `get_candles(symbol, start, end, interval="day") -> pd.DataFrame`.
- `normalize_candles(df)`: copy; lowercase/strip columns; raise `ValueError` if a
  required column is missing or the frame is empty; require `DatetimeIndex`;
  coerce columns numeric; keep only canonical columns; drop duplicate index (keep
  last); sort ascending; drop NaN-close rows; raise if empty after cleaning.

## `data/synthetic.py`
- `SyntheticSource.get_candles(...)`: **deterministic** — seed a NumPy RNG from a
  stable hash of `symbol` so repeats are identical. Geometric random walk over
  business days between `start` and `end`; derive OHLCV; return
  `normalize_candles(...)`. Must yield > 50 rows for multi-year ranges.

## `data/csv_source.py`
- `CsvSource(root="data")`. `get_candles` reads `<root>/<SYMBOL>.csv`
  (case-insensitive headers incl. `date`), parses dates → index, returns
  `normalize_candles(...)`.

## `data/mstock_client.py` — read-only REST client (NO order methods)
- `BASE_URL="https://api.mstock.trade"`; `ROUTES`:
  - login `/openapi/typea/connect/login`
  - session_token `/openapi/typea/session/token`
  - verify_totp `/openapi/typea/session/verifytotp`
  - scriptmaster `/openapi/typea/instruments/scriptmaster`
  - historical `/openapi/typea/instruments/historical/{exchange}/{instrument_token}/{interval}`
- `@dataclass MStockConfig(api_key, username, password, checksum="W",
  base_url=BASE_URL, timeout=15)`.
- `MStockClient(config)`:
  - `_headers`: always `{"X-Mirae-Version":"1"}`; add
    `Authorization: token {api_key}:{access_token}` **only when a token exists**
    (never during login).
  - `_request(method, route, *, params, data, form)`: build URL; `form` ⇒
    `Content-Type: application/x-www-form-urlencoded`; `raise_for_status()`.
  - `login()`: POST form `{"Username":username,"Password":password}` (capitalized).
  - `generate_session(otp)`: POST `{api_key, request_token=otp, checksum}` →
    extract+store+return `access_token` (top-level or under `data`); raise if
    absent. (SMS OTP accounts.)
  - `verify_totp(totp)`: POST `{api_key, totp}` → same token handling.
    (Authenticator TOTP accounts.)
  - `set_access_token`, `get_instruments()` (GET scriptmaster CSV → `read_csv`),
    `get_historical_candles(exchange, token, interval, frm, to)` (GET with query
    `{from, to}` → JSON).
  - Helpers `_safe_json`, `_dig(payload, key)` (find at top or under `data`).

## `data/mstock_source.py` — DataSource adapter (auth + caching)
- `MStockSource(config=None, cache_dir="data-cache", exchange="NSE",
  otp_provider=None, auth_mode=None, code=None)`:
  - `_config_from_env()`: load `.env`; require API_KEY/USERNAME/PASSWORD.
  - `auth_mode` from arg or `MSTOCK_AUTH_MODE` (default `otp`).
  - 2nd-factor code order: `code` arg → `MSTOCK_TOTP`/`MSTOCK_OTP` env →
    interactive prompt (text differs otp vs totp).
  - `_ensure_auth()`: reuse cached `.mstock_token.json` if present; else
    `login()` → get code → `verify_totp` (totp) or `generate_session` (otp) →
    cache token.
  - `_load_instruments()`: cache scriptmaster → `data-cache/scriptmaster.parquet`
    (lowercased cols).
  - `resolve_token(symbol)`: symbol col ∈ `tradingsymbol|symbol|name`, token col
    ∈ `instrument_token|token|exchange_token`; case-insensitive match; raise if
    missing.
  - `get_candles(...)`: parquet cache key
    `<SYMBOL>_<interval>_<start>_<end>.parquet`; else auth, resolve token, call
    history with **plain dates**, `_candles_to_frame` → `normalize_candles` →
    cache.
  - `_candles_to_frame(raw)`: dig `candles|data|result` (and nested `.candles`);
    support **list-of-lists** `[ts,o,h,l,c,v]` and **list-of-dicts** (rename
    `timestamp/time/datetime→date`, `o/h/l/c→open/high/low/close`,
    `v/vol→volume`); datetime index.

> **DEC-001 caveat:** real historical JSON shape may vary; `_candles_to_frame`
> is the ONLY place to adjust — downstream is unaffected.

**Verify:**
```
$env:PYTHONPATH="src"; python -c "from backtest.data.synthetic import SyntheticSource as S; d=S().get_candles('DEMO','2022-01-01','2022-06-01'); print(list(d.columns), len(d)>50)"
```
