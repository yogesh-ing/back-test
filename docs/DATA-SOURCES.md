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
- API limit: 1000 candles per request (auto-chunked)
- Rate-limited by API

### 4. DbSource (`data/db_source.py`) — **NOT YET BUILT**

**Use case:** Fast reads from PostgreSQL, no API calls.

```python
# Planned:
source = DbSource()  # reads from market_data_cache
candles = source.get_candles("RELIANCE", "2024-01-01", "2024-12-31", "day")
```

- Query: `SELECT ts, open, high, low, close, volume FROM market_data_cache WHERE symbol=:sym AND timeframe=:tf AND ts BETWEEN :start AND :end`
- Returns empty DataFrame if symbol not found (no fallback)
- ~0ms response time (TimescaleDB hypertable, indexed)

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
# source = build_source("db")        # -> DbSource() (not built yet)
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
