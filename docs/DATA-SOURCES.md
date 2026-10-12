# Data Sources

## The Contract

Every data source implements `DataSource` from `src/backtest/data/base.py`:

```python
class DataSource(Protocol):
    def get_candles(self, symbol: str, start: str, end: str, interval: str = "day") -> pd.DataFrame:
        ...
```

### Return Format

| Requirement | Value |
|-------------|-------|
| **Columns** | `["open", "high", "low", "close", "volume"]` — exactly 5 |
| **Index** | `DatetimeIndex` (enforced by `normalize_candles()`) |
| **Sorted** | Ascending by date |
| **Deduplicated** | Last occurrence kept |
| **NaN-free** | Rows with NaN close are dropped |
| **Numeric** | All columns cast to float |

### Parameters

| Param | Type | Description |
|-------|------|-------------|
| `symbol` | str | Instrument identifier (e.g. "RELIANCE", "DEMO") |
| `start` | str | Start date as ISO string (e.g. "2024-01-01") |
| `end` | str | End date as ISO string (e.g. "2024-12-31") |
| `interval` | str | "day", "hour", "15minute", etc. |

### normalize_candles()

Located in `data/base.py`. Called automatically by the engine, but sources should also call it for validation:

```python
from backtest.data.base import normalize_candles

def get_candles(self, symbol, start, end, interval="day"):
    df = ...  # your raw data
    return normalize_candles(df)  # validates and cleans
```

## Available Sources

### 1. SyntheticSource (`data/synthetic.py`)

**Use case:** Testing, demos, no API needed.

```python
source = SyntheticSource()
candles = source.get_candles("DEMO", "2024-01-01", "2024-12-31", "day")
```

- Generates random-walk OHLCV using seed based on symbol name
- Same symbol always produces same data (deterministic)
- Creates business-day frequency bars
- Minimum 50 rows required

### 2. CsvSource (`data/csv_source.py`)

**Use case:** Pre-downloaded CSV files.

```python
source = CsvSource(root="data")  # looks for data/{symbol}.csv
candles = source.get_candles("RELIANCE", "2024-01-01", "2024-12-31", "day")
```

- Reads from `{root}/{symbol}.csv`
- Expects columns: `date` (or `datetime`), `open`, `high`, `low`, `close`, `volume`
- Does NOT filter by date range (returns all rows, engine handles slicing)

### 3. MStockSource (`live/mstock.py`)

**Use case:** Real market data from mStock API.

```python
from backtest.live.mstock import MStockSource
source = MStockSource()
candles = source.get_candles("RELIANCE", "2024-01-01", "2024-12-31", "day")
```

- Requires authentication (login + TOTP)
- Token cached in `.mstock_session_token`
- API limit: 1000 candles per request — the Data tab fetch chunks the window
  itself (`api/data_manager.py`) and retries intermittent 502/503/504s; the
  historical endpoint's `to` date is **end-exclusive**, so fetches request one
  day past the wanted window
- Rate-limited by API

### 4. DbSource (`data/db_source.py`)

**Use case:** Fast historical reads from PostgreSQL — the **default backtest source**.

```python
source = DbSource()  # reads from market_data_cache, resolved via get_db_url()
candles = source.get_candles("RELIANCE", "2024-01-01", "2024-12-31", "1day")
```

- Stores **1min** bars, resamples **up** to any requested interval (pandas rules;
  canonical names `1min|5min|15min|1hour|4hour|1day|1week`)
- Reads the finest stored timeframe at or below the request, then resamples
- Returns empty DataFrame if symbol not found (no fallback)
- ~0ms response time (TimescaleDB hypertable, indexed)

### 5. DhanLiveFeed (`data/dhan_live_feed.py`)

**Use case:** Real market data from Dhan HQ — `POST /marketfeed/ohlc` latest
bars (minute-floor IST) + `/charts/intraday` candles. Requires an active Dhan
session (`DHAN_API_KEY`, `DHAN_CLIENT_ID`).

**Runner source tag (2026-10-01):** `DhanLiveFeed` is registered in
`data/source_tags.py` (`SOURCE_TAGS[DhanLiveFeed] = "dhan"`), so `dhan` is a
selectable **Data source** for spawned runners (dropdown + `RunnerConfig.source`
validation) and a valid `portfolios.source` value (CHECK widened by migration
016). Bars stamped `_source="dhan"` fan out only to runners configured with
`source="dhan"` (source-isolated bar routing). Separate from the *execution*
venue: a runner's orders route by `execution_broker`/segment, its bars by
`source` — the two need not match.

### Source registry — how mode and source combine (`data/source_registry.py`)

The single factory deciding where a run's bars come from:

| mode | source choice | What you get |
|---|---|---|
| `backtest` | — (fixed) | `DbSource` (historical DB), optionally wrapped in `AdjustedSource` for corporate-action back-adjustment at read time |
| `live` | — (fixed) | `MStockLiveFeed` (real broker feed) |
| `paper` | `mstock` | live broker data, paper risk |
| `paper` | `synthetic` | generated bars replayed at `replay_speed` bars/second — config-gated, off by default |

Unknown modes / paper runs without a valid choice raise `ConfigError`. The
`synthetic` paper branch is gated by `config/data_sources.yaml` via
`data/sources_policy.py`: when synthetic is disabled the run is refused
(`ConfigError` carrying the policy's refusal) — there is **no fallback** to a
real source. `compare` backtests run through the same `backtest` row above
(`DbSource`), so everything on the DB-backed pages reads `market_data_cache`.

**Options:** backtests and compare-backtests are **equity-only**.
`CHAIN_SOURCES: tuple[str, ...] = ()` in `data/sources_policy.py` is
deliberately empty — the DB holds bars, the broker holds live chains, and
neither is historical option-chain storage. Option expiries always come from
the broker's instrument master (`options/quote_providers.py` reads the chain
returned by `broker.get_option_chain(...)`); they are never computed from a
formula or fetched from the DB.

### Adding a new source (~30 minutes)

1. Create `src/backtest/data/my_source.py` implementing the `DataSource`
   protocol (return `normalize_candles(df)`):
```python
import pandas as pd
from backtest.data.base import normalize_candles

class MySource:
    def get_candles(self, symbol: str, start: str, end: str, interval: str = "day") -> pd.DataFrame:
        df = ...  # fetch your data
        return normalize_candles(df)
```
2. Wire it where your entry path builds sources: `build_source()` in
   `backtest/runner.py` (backtest page) and/or the mode table in
   `data/source_registry.py` (portfolio runners).
3. Update the `--source` help text in `app.py` if it should be CLI-selectable.
4. Run with: `PYTHONPATH=src python -m backtest.web.app --source my_source`

## How Sources Are Selected

### CLI Flag
```bash
python -m backtest.web.app --source synthetic   # or csv, mstock, db
```

### In Code
```python
from backtest.runner import build_source

source = build_source("synthetic")   # -> SyntheticSource()
source = build_source("csv")         # -> CsvSource()
source = build_source("mstock")      # -> MStockSource()
source = build_source("db")          # -> DbSource() (runner.py L65-68)
```

### Web UI
The `--source` flag is set at startup and stored in `app.config["BACKTEST_SOURCE"]`. Every API request uses this source. The user cannot switch sources per-request from the UI.

## Source Selection Flow

```
CLI: --source synthetic
  → app.py: app.config["BACKTEST_SOURCE"] = "synthetic"
    → api/backtest.py: _source() reads config
      → runner.py: build_source("synthetic") returns SyntheticSource()
        → source.get_candles(symbol, from, to, interval)
          → DataFrame with OHLCV
```
