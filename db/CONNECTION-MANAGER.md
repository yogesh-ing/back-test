# Database Connection Manager

**Step 2 of `instructions/forword-testing.md`**

`DatabaseManager` wraps a SQLAlchemy engine with the operational behaviour a
long-running trading loop needs: a bounded connection pool, automatic recovery
from a database restart, bounded retries on transient faults, transaction
scoping, and health checks.

Works with **PostgreSQL** (production) and **SQLite** (local development)
behind one API.

---

## Quick start

```python
from backtest.db import DatabaseManager

db = DatabaseManager.from_env()      # config/database.yaml + environment
db.connect()

rows = db.fetch_all(
    "SELECT symbol, quantity FROM positions WHERE portfolio_id = :pid",
    {"pid": portfolio_id},
)

db.disconnect()
```

Or let the context manager handle the lifecycle:

```python
with DatabaseManager.from_env() as db:
    print(db.health_check())
```

---

## Configuration

Three sources, **highest precedence first**:

| Precedence | Source | Use it for |
|---|---|---|
| 1 | keyword arguments to `from_env(...)` | tests, one-off overrides |
| 2 | `FORWARD_TEST_DB_*` environment variables | **secrets** — the URL |
| 3 | `config/database.yaml` (active profile, then `default`) | shape: pool sizes, timeouts, logging |

The split is deliberate: the YAML file is committed and describes *shape*; the
environment supplies *secrets*. The `production` profile has **no `url` key**
precisely so credentials can never be committed.

### Profiles

```bash
export FORWARD_TEST_DB_PROFILE=production
export FORWARD_TEST_DB_URL=postgresql+psycopg2://ft_app:secret@localhost:5432/forward_test
```

| Profile | Backend | Pool | Notes |
|---|---|---|---|
| `development` | SQLite file | 1–5 | Query logging on, 200 ms slow threshold |
| `testing` | SQLite in-memory | 1 | Retries disabled so the suite fails fast |
| `production` | PostgreSQL | 5–20 | URL **must** come from the environment |

### Environment variables

Every YAML key has a matching `FORWARD_TEST_DB_<KEY>` variable:

```bash
FORWARD_TEST_DB_URL=postgresql+psycopg2://user:pass@host:5432/forward_test
FORWARD_TEST_DB_PROFILE=production
FORWARD_TEST_DB_POOL_MIN_SIZE=5
FORWARD_TEST_DB_POOL_MAX_SIZE=20
FORWARD_TEST_DB_LOG_QUERIES=true
FORWARD_TEST_DB_SLOW_QUERY_MS=1000
FORWARD_TEST_DB_CONFIG=/etc/forward_test/database.yaml   # alternate YAML path
```

### Programmatic overrides

```python
db = DatabaseManager.from_env(url="sqlite:///:memory:", profile="testing")
```

Invalid settings raise `ConfigError` at load time, not at first query:

```python
DatabaseManager.from_env(pool_min_size=10, pool_max_size=5)
# ConfigError: pool_max_size (5) must be >= pool_min_size (10)
```

---

## Querying

All helpers take **named bind parameters** (`:name`) plus a dict. Never
f-string values into SQL — bind parameters are what keep this injection-safe.

```python
# Single row, or None
row = db.fetch_one("SELECT * FROM portfolios WHERE name = :n", {"n": "run-1"})

# All rows as dicts
rows = db.fetch_all("SELECT symbol, net_pnl FROM trades WHERE portfolio_id = :p",
                    {"p": pid})

# Single value
n = db.fetch_scalar("SELECT count(*) FROM trades WHERE portfolio_id = :p", {"p": pid})

# DML — returns affected row count
db.execute_query("UPDATE portfolios SET status = :s WHERE portfolio_id = :p",
                 {"s": "paused", "p": pid})

# Bulk insert — uses the driver's executemany path
db.execute_many(
    "INSERT INTO market_data_cache (symbol, timeframe, ts, open, high, low, close) "
    "VALUES (:symbol, :timeframe, :ts, :open, :high, :low, :close)",
    bars,   # list of dicts
)
```

`execute_many` is dramatically faster than looping — measured at 60 bars in
~11 ms against PostgreSQL. Use it whenever you flush a batch.

---

## Transactions

### Context manager (preferred)

Commits on clean exit, rolls back on any exception:

```python
from sqlalchemy import text

with db.transaction() as conn:
    conn.execute(text("UPDATE portfolios SET current_cash = current_cash - :amt"),
                 {"amt": 1500})
    conn.execute(text("INSERT INTO fills (...) VALUES (...)"))
# committed here; if either statement raised, neither applied
```

### ORM session

```python
from backtest.db import Portfolio

with db.session() as s:
    s.add(Portfolio(name="run-1", initial_capital=100000, current_cash=100000))
# committed on exit, rolled back on exception, always closed
```

Sessions use `expire_on_commit=False`, so objects stay readable after the
block ends.

### Explicit API

Provided because the Step 2 spec asks for it. Prefer the context managers —
they cannot leak a connection if you forget to commit.

```python
conn = db.begin_transaction()
try:
    conn.execute(text("INSERT INTO trades (...) VALUES (...)"))
    db.commit()
except Exception:
    db.rollback()
    raise
```

Three behaviours worth knowing:

- **Nesting is refused.** A second `begin_transaction()` on the same thread
  raises `TransactionError` rather than silently flattening, because an inner
  "commit" that actually commits the outer scope is a data-loss bug.
- **Helpers join an open transaction.** `execute_query` and friends
  participate in the thread's transaction rather than opening their own, so a
  later `rollback()` really does undo them.
- **State is thread-local.** One thread's transaction is invisible to another.

---

## Reliability

### Pooling

| Backend | Pool | Why |
|---|---|---|
| PostgreSQL | `QueuePool`, `pool_size=5`, `max_overflow=15` | Hard ceiling of 20 connections |
| SQLite (file) | `NullPool` | SQLite serialises writes; pooling adds contention, not throughput |
| SQLite (`:memory:`) | `StaticPool` | The database *lives inside* one connection — any other pool hands out empty databases |

Verified under load: 30 threads × 15 queries, peak concurrent checkouts 19,
zero errors, ceiling never breached.

### Automatic recovery

`pool_pre_ping` issues a `SELECT 1` before handing out a pooled connection, so
sockets closed by a server restart are detected and replaced rather than
handed to your code.

Combined with retries, a database restart is transparent:

```
[warn] fetch_scalar failed (attempt 1/5): connection to server ... failed — retrying in 0.47s
[warn] fetch_scalar failed (attempt 2/5): connection to server ... failed — retrying in 1.06s
AFTER OUTAGE: rows = 1  (recovered in 1.54s, no exception)
```

This is a real measured run: PostgreSQL was killed mid-query and restarted.
The caller never saw an exception.

`pool_recycle` (default 1800 s) closes connections before any proxy or cloud
load balancer times them out. **Keep it below your infrastructure's idle
timeout** or you will hand out sockets the server already dropped.

### Retries — transient faults only

Retries use exponential backoff (0.5 s → 1 s → 2 s, capped) with jitter, so a
fleet of workers doesn't reconnect in lockstep after an outage.

Crucially, **only genuinely transient faults are retried**:

| Retried | Not retried |
|---|---|
| `server closed the connection` | `duplicate key value` |
| `connection refused` / `connection to server ... failed` | `no such table` |
| `the database system is starting up` | `syntax error` |
| `too many clients` | `column does not exist` |
| `database is locked` (SQLite) | any `IntegrityError` |

This distinction matters more than it looks. `OperationalError` means
"connection died" on PostgreSQL but also "no such table" on SQLite, so the
exception *type* is not enough — the driver message is inspected too. Without
this, a typo'd table name would be retried three times and then reported as a
connection failure, sending you debugging the wrong system entirely.

**Retries are disabled inside an explicit transaction.** Replaying one
statement whose predecessors already applied would corrupt data.

---

## Health checks and monitoring

`health_check()` never raises — a monitoring loop can call it bare:

```python
report = db.health_check()
# {
#   'healthy': True,
#   'dialect': 'postgresql',
#   'latency_ms': 0.57,
#   'pool': {'class': 'QueuePool', 'size': 5, 'checkedin': 5,
#            'checkedout': 0, 'overflow': 0, 'max': 20},
#   'stats': {'queries': 510, 'retries': 2, 'failures': 0, 'slow_queries': 0},
# }
```

On failure it returns `{'healthy': False, 'error': '...'}` instead of raising.

```python
db.pool_status()   # pool occupancy only
db.stats           # {'queries', 'retries', 'failures', 'slow_queries'}
```

Query counting is always on (one `perf_counter()` call per statement); only
the *logging* is conditional. These counters feed the Step 20 monitoring hooks.

### Logging

```python
db = DatabaseManager.from_env(log_queries=True, slow_query_ms=500)
```

- `log_queries` — every statement and its duration at `DEBUG`
- `slow_query_ms` — a `WARNING` for anything slower (`0` disables)
- `echo` — SQLAlchemy's own firehose; very noisy, prefer `log_queries`

All output goes to the `backtest.db` logger:

```python
import logging
logging.getLogger("backtest.db").setLevel(logging.DEBUG)
```

---

## Security

**Passwords are masked everywhere.** `safe_url` strips the password, and it is
what `repr()`, `describe()`, `health_check()` and every log line and exception
use:

```python
cfg.url       # 'postgresql+psycopg2://alice:hunter2@db:5432/forward_test'
cfg.safe_url  # 'postgresql+psycopg2://alice:***@db:5432/forward_test'
```

There are explicit tests asserting the password never appears in a connection
error, in `repr()`, or in a health report.

**Injection.** Always pass values as bind parameters:

```python
db.fetch_one("SELECT * FROM portfolios WHERE name = :n", {"n": user_input})  # safe
db.fetch_one(f"SELECT * FROM portfolios WHERE name = '{user_input}'")        # NEVER
```

**PostgreSQL statement timeout** (`statement_timeout_ms`, default 30 s) is set
server-side so one runaway query cannot occupy a pool slot forever.

---

## SQLite specifics

The manager applies these PRAGMAs on every connection:

| PRAGMA | Value | Why |
|---|---|---|
| `foreign_keys` | `ON` | **Off by default in SQLite** — without this every FK in the schema is decorative |
| `journal_mode` | `WAL` | Readers proceed during writes, so a dashboard can poll while the loop writes |
| `synchronous` | `NORMAL` | Sensible durability/speed balance under WAL |
| `busy_timeout` | `connect_timeout × 1000` | Wait for a write lock instead of failing instantly |

Remember SQLite stores `NUMERIC` as float — money arithmetic is approximate.
Development only; see `db/DB-IMPLEMENTATION-GUIDE.md` §4.

---

## Errors

| Exception | Meaning |
|---|---|
| `ConfigError` | Bad or missing configuration; raised at load time |
| `ConnectionError` | Could not connect, or retries exhausted. **Shadows the builtin** — import `DatabaseConnectionError` instead, it is the same class |
| `TransactionError` | Explicit transaction API misused (nesting, commit without begin) |
| `sqlalchemy.exc.IntegrityError` | Constraint violation — passed through unretried |

`ConnectionError` and `TransactionError` both derive from `DatabaseError`.

---

## Shutdown

```python
db.disconnect()
```

Closes every pooled connection. If a transaction is still open on the calling
thread it is **rolled back first**, so a crash during shutdown cannot silently
commit partial work. Safe to call repeatedly. An `atexit` hook disposes the
pool as a last resort, so the process never leaks sockets.

A disconnected manager cannot be reused — construct a new one. This is
deliberate: silently reconnecting a manager someone else shut down hides
lifecycle bugs.

---

## Testing against this manager

```python
@pytest.fixture()
def db():
    m = DatabaseManager.from_env(profile="testing", url="sqlite:///:memory:")
    m.connect()
    Base.metadata.create_all(m.engine)
    yield m
    m.disconnect()
```

The `testing` profile sets `retry_attempts: 1` so failures surface
immediately instead of sleeping through backoff. If you are testing retry
behaviour itself, override it:

```python
DatabaseManager.from_env(profile="testing", url="sqlite:///:memory:",
                         retry_attempts=3, retry_base_delay=0.001)
```

Coverage lives in `tests/test_db_manager.py` — 103 tests spanning config
precedence, pooling, transactions, retry classification, thread safety and
password masking.
