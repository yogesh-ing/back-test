Perfect. I have everything I need. Writing the PRD now.

---

# PRD: Wire Backtest Engine to PostgreSQL (`--source db`)

## Problem Statement

The backtest engine currently runs only on synthetic or CSV data. 467,151 bars of real NIFTY 200 OHLCV data sit in PostgreSQL unused. Users cannot backtest real strategies against real market data from the UI.

**Goal:** Add `--source db` as a fully working data source so the backtest page reads from `market_data_cache` instead of generating fake candles.

---

## Scope

### In Scope
- `DbSource` class implementing the `DataSource` protocol
- SQLAlchemy model for `market_data_cache` (read-only)
- `GET /api/symbols` endpoint returning distinct symbols from DB
- Dynamic symbol dropdown in `backtest.html`
- Wiring `--source db` through `runner.py` and `app.py`
- Proper error handling when symbol is not in DB

### Out of Scope
- Writing/inserting data into `market_data_cache`
- Fallback to mStock API
- Changing the date picker UI
- Compare / Forward Test / Dashboard modes (unchanged)
- Any interval other than `day` (only daily bars exist in DB)

---

## Functional Requirements

| ID | Requirement |
|----|-------------|
| FR-1 | `DbSource.get_candles(symbol, start, end, interval)` queries `market_data_cache` and returns a normalized DataFrame |
| FR-2 | If symbol not found, raise a descriptive error — no silent fallback |
| FR-3 | `GET /api/symbols` returns `["HDFCBANK", "INFY", ...]` sorted alphabetically, filtered by what's actually in the DB |
| FR-4 | Backtest page symbol dropdown populates dynamically when source is `db`, stays static otherwise |
| FR-5 | `--source db` wired through `build_source()` in `runner.py` |
| FR-6 | App startup logs how many symbols and bars are available when source is `db` |

---

## Technical Design

### Data Flow (after this change)

```
CLI: --source db
  → app.py: app.config["BACKTEST_SOURCE"] = "db"
    → api/backtest.py: build_source("db") → DbSource()
      → DbSource.get_candles("RELIANCE", "2024-01-01", "2024-12-31", "day")
        → SELECT ts, open, high, low, close, volume
          FROM market_data_cache
          WHERE symbol = 'RELIANCE'
            AND timeframe = 'day'
            AND ts BETWEEN '2024-01-01' AND '2024-12-31'
          ORDER BY ts ASC
        → normalize_candles(df) → engine
```

### Files Changed

```
src/backtest/
├── data/
│   └── db_source.py          ← NEW
├── db/
│   └── models.py             ← ADD MarketDataCache model
├── web/
│   ├── api/
│   │   ├── backtest.py       ← NO CHANGE (build_source handles it)
│   │   └── symbols.py        ← NEW endpoint
│   ├── app.py                ← REGISTER blueprint + update help text
│   └── templates/
│       └── backtest.html     ← dynamic dropdown when source=db
└── runner.py                 ← ADD db branch in build_source()
```

---

## Task Decomposition

Tasks are ordered by dependency. Each task is independently testable.

---

### Task 1 — Add `MarketDataCache` SQLAlchemy Model
**File:** `src/backtest/db/models.py`
**Effort:** Small
**Depends on:** Nothing

Add a read-only ORM model mapping to the existing `market_data_cache` table. No migrations needed — table already exists.

```python
# What to add to models.py

class MarketDataCache(Base):
    __tablename__ = "market_data_cache"

    data_id    = Column(BigInteger, primary_key=True, autoincrement=True)
    symbol     = Column(String(64),  nullable=False)
    exchange   = Column(String(16),  default="NSE")
    timeframe  = Column(String(8),   nullable=False)
    ts         = Column(DateTime(timezone=True), nullable=False)
    open       = Column(Numeric(20, 8))
    high       = Column(Numeric(20, 8))
    low        = Column(Numeric(20, 8))
    close      = Column(Numeric(20, 8))
    volume     = Column(Numeric(20, 4))
    source     = Column(String(32))
    ingested_at = Column(DateTime(timezone=True))
```

**Acceptance criteria:**
- `from backtest.db.models import MarketDataCache` works without error
- `MarketDataCache.__tablename__ == "market_data_cache"`
- No existing model broken

---

### Task 2 — Build `DbSource` Class
**File:** `src/backtest/data/db_source.py`
**Effort:** Medium
**Depends on:** Task 1

This is the core piece. Implements `DataSource` protocol.

**Full spec:**

```python
class DbSource:
    """
    Reads OHLCV candles from market_data_cache (PostgreSQL / TimescaleDB).

    Implements the DataSource protocol:
        get_candles(symbol, start, end, interval) -> pd.DataFrame
    
    DataFrame contract (enforced by normalize_candles):
        - Columns: ["open", "high", "low", "close", "volume"]
        - Index: DatetimeIndex, ascending, deduplicated, NaN-free
    """

    def __init__(self, db_url: str = None):
        """
        db_url: SQLAlchemy connection string.
                Falls back to DATABASE_URL env var.
                Falls back to default local Postgres.
        """

    def get_candles(
        self,
        symbol: str,
        start: str,
        end: str,
        interval: str = "day",
    ) -> pd.DataFrame:
        """
        Queries market_data_cache for symbol+interval in [start, end].

        Raises:
            ValueError: if symbol not found in DB for given interval/range.
                        Message format:
                        "Symbol 'XYZ' not found in database for timeframe 'day'
                         between 2024-01-01 and 2024-12-31.
                         Run fetch_nifty500_historical.py to populate."

        Returns:
            pd.DataFrame with DatetimeIndex and columns
            [open, high, low, close, volume], sorted ascending.
        """

    def list_symbols(self, timeframe: str = "day") -> list[str]:
        """
        Returns sorted list of distinct symbols available in DB
        for the given timeframe. Used by /api/symbols endpoint.
        """
```

**Query to use inside `get_candles`:**
```sql
SELECT ts, open, high, low, close, volume
FROM market_data_cache
WHERE symbol    = :symbol
  AND timeframe = :timeframe
  AND ts BETWEEN :start AND :end
ORDER BY ts ASC
```

**Implementation notes:**
- Use `pd.read_sql()` with the SQLAlchemy engine — do not use the ORM for this query (faster for bulk reads)
- Set `ts` as the DataFrame index immediately after read
- Call `normalize_candles(df)` before returning
- Connection: create engine lazily on first call, reuse after (module-level singleton or instance variable)
- Do not import anything from `backtest.web` — keep data layer clean

**Acceptance criteria:**
- `DbSource().get_candles("RELIANCE", "2024-01-01", "2024-12-31")` returns a DataFrame with 249 ± 5 rows (trading days in 2024)
- Columns are exactly `["open", "high", "low", "close", "volume"]`
- Index is `DatetimeIndex`, ascending, no NaN
- Calling with an unknown symbol raises `ValueError` with the prescribed message
- `DbSource().list_symbols()` returns a list of 201 strings

---

### Task 3 — Register `db` in `build_source()`
**File:** `src/backtest/runner.py`
**Effort:** Trivial
**Depends on:** Task 2

Add one branch. No other logic changes.

```python
# runner.py — inside build_source()

# BEFORE (existing):
def build_source(source_name: str, **kwargs):
    if source_name == "synthetic":
        return SyntheticSource(**kwargs)
    elif source_name == "csv":
        return CsvSource(**kwargs)
    elif source_name == "mstock":
        return MStockSource(**kwargs)
    else:
        raise ValueError(f"Unknown source: {source_name}")

# AFTER (add db branch before the else):
    elif source_name == "db":
        from backtest.data.db_source import DbSource
        return DbSource()
```

**Acceptance criteria:**
- `build_source("db")` returns a `DbSource` instance
- `build_source("synthetic")` still works
- `build_source("unknown")` still raises `ValueError`

---

### Task 4 — Add `GET /api/symbols` Endpoint
**File:** `src/backtest/web/api/symbols.py` (new file)
**Effort:** Small
**Depends on:** Task 2

```python
# Blueprint: symbols_bp
# Route: GET /api/symbols
# Query param: ?timeframe=day (optional, default "day")

# Response (200):
{
  "symbols": ["ADANIENT", "HDFCBANK", "INFY", "RELIANCE", ...],
  "count": 201,
  "timeframe": "day"
}

# Response (500) if DB unreachable:
{
  "error": "Database unavailable",
  "symbols": []
}
```

**Logic:**
- Only active when `app.config["BACKTEST_SOURCE"] == "db"` — if source is `synthetic` or `csv`, return `{"symbols": [], "count": 0}` immediately (no DB call)
- Use `DbSource().list_symbols(timeframe)` — don't duplicate the query
- Cache the result in `flask.g` or module-level for the process lifetime (symbols list won't change during a run)

**Acceptance criteria:**
- `GET /api/symbols` returns 200 with a list of 201 symbols when source is `db`
- `GET /api/symbols` returns `{"symbols": [], "count": 0}` when source is `synthetic`
- Response time < 200ms (uses cached list after first call)

---

### Task 5 — Register `symbols_bp` in `app.py`
**File:** `src/backtest/web/app.py`
**Effort:** Trivial
**Depends on:** Task 4

Two changes:

```python
# 1. Register the blueprint
from backtest.web.api.symbols import symbols_bp
app.register_blueprint(symbols_bp)

# 2. Update --source help text
parser.add_argument(
    "--source",
    choices=["synthetic", "csv", "mstock", "db"],   # add "db"
    default="synthetic",
    help="Data source: synthetic | csv | mstock | db"
)
```

**3. Add startup log when source is `db`:**
```python
# After app.config["BACKTEST_SOURCE"] is set:
if args.source == "db":
    try:
        from backtest.data.db_source import DbSource
        _src = DbSource()
        syms = _src.list_symbols()
        app.logger.info(f"[DB] {len(syms)} symbols available in market_data_cache")
    except Exception as e:
        app.logger.warning(f"[DB] Could not connect to database: {e}")
```

**Acceptance criteria:**
- App starts with `--source db` without error
- Startup log shows symbol count
- `--source fake` still raises argparse error
- Other sources unaffected

---

### Task 6 — Dynamic Symbol Dropdown in `backtest.html`
**File:** `src/backtest/web/templates/backtest.html`
**Effort:** Small
**Depends on:** Task 4, Task 5

The template needs to know whether the current source is `db` to decide which behaviour to use. Pass it from the route:

```python
# In the Flask route that renders backtest.html:
return render_template(
    "backtest.html",
    source=app.config.get("BACKTEST_SOURCE", "synthetic")
)
```

**Template changes:**

```html
<!-- Symbol select — keep existing options as fallback -->
<select id="symbol">
  <option>RELIANCE</option>   <!-- shown while loading / for non-db sources -->
  <option>HDFCBANK</option>
  <option>INFY</option>
  <option>TCS</option>
</select>

<!-- Status line below dropdown, hidden by default -->
<small id="symbol-status" class="text-muted" style="display:none"></small>
```

```javascript
// Add to existing page JS — runs on DOMContentLoaded

const SOURCE = "{{ source }}";   // injected by Flask

if (SOURCE === "db") {
  const sel = document.getElementById("symbol");
  const status = document.getElementById("symbol-status");

  // Show loading state
  sel.innerHTML = '<option disabled selected>Loading symbols…</option>';
  status.style.display = "inline";
  status.textContent = "Fetching available symbols from database…";

  fetch("/api/symbols")
    .then(r => r.json())
    .then(data => {
      if (!data.symbols || data.symbols.length === 0) {
        sel.innerHTML = '<option disabled selected>No symbols found</option>';
        status.textContent = "No data in database.";
        return;
      }
      // Populate dropdown
      sel.innerHTML = data.symbols
        .map(s => `<option value="${s}">${s}</option>`)
        .join("");
      // Default selection: RELIANCE if present
      if (data.symbols.includes("RELIANCE")) {
        sel.value = "RELIANCE";
      }
      status.textContent = `${data.count} symbols available`;
    })
    .catch(() => {
      sel.innerHTML = '<option disabled selected>Error loading symbols</option>';
      status.textContent = "Could not reach database.";
    });
}
// If SOURCE !== "db": leave the static dropdown exactly as-is
```

**Acceptance criteria:**
- With `--source db`: dropdown populates with 201 symbols on page load
- With `--source synthetic`: dropdown shows original static options, no fetch call made
- If `/api/symbols` fails: dropdown shows "Error loading symbols", page still usable
- Default selected symbol is RELIANCE when available

---

## Error Handling Summary

| Scenario | Behaviour |
|---|---|
| Symbol not in DB | `ValueError` with message telling user to run `fetch_nifty500_historical.py` |
| DB unreachable at query time | Exception propagates to API layer → 500 response with error message |
| DB unreachable at startup | Warning log, app starts anyway (user sees error on first backtest run) |
| Symbol dropdown fetch fails | JS shows "Error loading symbols", user can still type — form still submits |
| interval != "day" requested | `ValueError`: "Only 'day' interval available in database. Requested: '1min'" |

---

## Testing Checkpoints

After each task you can verify independently:

```bash
# Task 1 — model import
python -c "from backtest.db.models import MarketDataCache; print('OK')"

# Task 2 — DbSource query
PYTHONPATH=src python -c "
from backtest.data.db_source import DbSource
df = DbSource().get_candles('RELIANCE', '2024-01-01', '2024-12-31')
print(df.shape, df.columns.tolist(), df.index.dtype)
"

# Task 3 — runner wiring
PYTHONPATH=src python -c "
from backtest.runner import build_source
src = build_source('db')
print(type(src).__name__)
"

# Task 4+5 — symbols endpoint
curl http://localhost:5000/api/symbols | python -m json.tool

# Task 6 — visual check
# Open browser → backtest page → observe dropdown populates
```

---

## Execution Order

```
Task 1 (model)
    └── Task 2 (DbSource)
            ├── Task 3 (runner) ← can ship independently
            └── Task 4 (endpoint)
                    └── Task 5 (app.py)
                            └── Task 6 (UI)
```

Tasks 3 and 4 can be built in parallel once Task 2 is done. Everything else is sequential.

---

## Definition of Done

- [ ] `PYTHONPATH=src python -m backtest.web.app --source db` starts without error
- [ ] Backtest page loads with 201 symbols in dropdown
- [ ] Running backtest on `RELIANCE` / `2024-01-01` / `2024-12-31` / `SMA Crossover` returns real trade results
- [ ] Running with unknown symbol shows error message in UI (not a crash)
- [ ] `--source synthetic` still works identically to before
- [ ] No new dependencies added (uses `sqlalchemy` and `psycopg2` already in requirements)