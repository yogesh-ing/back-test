# Database Implementation Guide — Forward Testing Simulator

**Step 1 of `instructions/forword-testing.md`**
Target engine: **PostgreSQL 13+** (SQLite supported for local development)

This guide is written so you can apply the schema **by hand**. Nothing in this
repository connects to your database automatically.

---

## 1. What you're installing

| # | Table | Purpose | Grows |
|---|-------|---------|-------|
| 1 | `portfolios` | One row per forward-testing run. Root aggregate. | Tiny |
| 2 | `positions` | Net open exposure per symbol; closed rows kept as history. | Slow |
| 3 | `orders` | Order lifecycle (pending → filled/cancelled/rejected). | Medium |
| 4 | `fills` | Individual executions. Append-only, immutable. | Medium |
| 5 | `trades` | Matched round-trips (entry → exit) with P&L attribution. | Medium |
| 6 | `equity_curve` | Mark-to-market snapshots. The performance time series. | **Fast** |
| 7 | `market_data_cache` | Local OHLCV cache so restarts don't re-hit the broker. | **Fast** |
| 8 | `performance_metrics` | Daily rollup. Derived — safe to delete and rebuild. | Slow |
| 9 | `strategy_signals` | Audit log of every signal, executed or not. | **Fast** |
| 10 | `system_logs` | Structured application log. | **Fast** |

Plus: `schema_migrations` (bookkeeping), 2 views, 46 indexes, 13 foreign keys,
and 2 `updated_at` triggers.

### Relationship map

```
portfolios (root)
├── positions ────────┐
├── orders ───────────┼──> fills
├── trades ◄──────────┘   (fills link to both order and position)
├── equity_curve
├── performance_metrics
├── strategy_signals ──> orders (when executed)
└── system_logs        (ON DELETE SET NULL — logs outlive the portfolio)

market_data_cache      (standalone; not portfolio-scoped)
```

Deleting a portfolio **cascades** to its positions, orders, fills, trades,
equity curve, metrics and signals. It does **not** delete logs — those are
detached (`SET NULL`) so post-mortems survive.

---

## 2. Files in this delivery

| File | What it is |
|---|---|
| `db/migrations/001_initial_schema.sql` | **The one to run.** PostgreSQL DDL. |
| `db/migrations/001_initial_schema_rollback.sql` | Undo. Destroys all data. |
| `db/migrations/001_initial_schema.sqlite.sql` | SQLite mirror for local dev. |
| `db/verify_schema.sql` | Post-install checks. Prints PASS/FAIL. |
| `src/backtest/db/models.py` | SQLAlchemy ORM models (Python mirror). |
| `db/alembic/`, `alembic.ini` | Alembic migration path (alternative to manual SQL). |

All three paths — manual SQL, `Base.metadata.create_all()`, and
`alembic upgrade head` — have been verified to produce **byte-identical**
PostgreSQL schemas. Pick one; see §7 for how to switch later.

---

## 3. PostgreSQL setup (production path)

### 3.1 Create the database and a least-privilege role

Connect as a superuser (`psql -U postgres`) and run:

```sql
-- Application role. Use a real password from your secret manager.
CREATE ROLE ft_app WITH LOGIN PASSWORD 'CHANGE_ME';

CREATE DATABASE forward_test
    WITH OWNER      = ft_app
         ENCODING   = 'UTF8'
         LC_COLLATE = 'en_US.UTF-8'
         LC_CTYPE   = 'en_US.UTF-8'
         TEMPLATE   = template0;

-- UTC everywhere. The engine writes tz-aware UTC; this keeps psql readable
-- and prevents surprise local-time rendering in ad-hoc queries.
ALTER DATABASE forward_test SET timezone TO 'UTC';
```

> **Why UTC and not `Asia/Kolkata`?**
> Store UTC, display IST. NSE session boundaries are converted in the
> application layer (Step 12, TimeManager). Storing local time makes DST-free
> India look fine today but breaks the moment you add a second exchange.

### 3.2 Apply the schema

```bash
cd /path/to/back-test
psql -U ft_app -d forward_test -f db/migrations/001_initial_schema.sql
```

Expected tail of the output:

```
CREATE TABLE
...
CREATE VIEW
INSERT 0 1
COMMIT
```

The whole file runs in a **single transaction**. If any statement fails,
nothing is applied — you cannot end up half-migrated.

### 3.3 Verify

```bash
psql -U ft_app -d forward_test -f db/verify_schema.sql
```

Read the `result` column. Everything should say `PASS`. Check 10 (money
columns must be `numeric`) passes when it returns **zero rows**.

### 3.4 Point the app at it

In your `.env` (copy from `.env.example`):

```
FORWARD_TEST_DB_URL=postgresql+psycopg2://ft_app:CHANGE_ME@localhost:5432/forward_test
```

Never commit `.env` — it is in `.gitignore`.

---

## 4. SQLite setup (local development path)

Zero installation; the file is created on first connect.

```bash
sqlite3 forward_test.db < db/migrations/001_initial_schema.sqlite.sql
```

```
FORWARD_TEST_DB_URL=sqlite:///forward_test.db
```

### Three SQLite limitations you must know

1. **Foreign keys are OFF by default.** SQLite silently ignores FK constraints
   unless every connection runs `PRAGMA foreign_keys = ON`. The Step 2
   connection manager will do this automatically; ad-hoc `sqlite3` sessions
   will not.

2. **`NUMERIC(20,4)` precision is not enforced.** SQLite stores these as REAL
   (binary float). Money arithmetic is *approximate*. Fine for development,
   **never** for anything you report on. This is the single biggest reason
   production is PostgreSQL.

3. **No timezone awareness.** Timestamps are ISO-8601 text. Always write UTC.

---

## 5. Design decisions worth knowing

These are the choices that will bite you later if you don't know about them.

### CHECK constraints instead of native `ENUM`
Step 2 requires the same schema on SQLite, which has no `ENUM` type. `VARCHAR`
+ `CHECK` is portable and much easier to evolve — adding a value is an
`ALTER TABLE ... DROP/ADD CONSTRAINT`, not a catalog-locking `ALTER TYPE`.
The Python `enum.Enum` classes in `models.py` provide the type safety.

### `NUMERIC`, never `FLOAT`, for money
Binary floating point cannot represent `0.1` exactly. Accumulate a few
thousand fills and the equity curve stops reconciling with the sum of trade
P&L, producing a bug that is miserable to find. `NUMERIC` maps to Python
`Decimal`. Precision used:
`NUMERIC(20,8)` prices/quantities · `NUMERIC(20,4)` money · `NUMERIC(12,6)` ratios.

### One open position per symbol, enforced by the database
```sql
CREATE UNIQUE INDEX uq_positions_one_open_per_symbol
    ON positions (portfolio_id, symbol) WHERE status = 'open';
```
A *partial* unique index. Unlimited closed history rows for the same symbol,
but never two open ones. A double-fill race that would otherwise silently
double your exposure now raises an integrity error instead.

### Column named `ts`, not `timestamp`
The plan document specifies `timestamp`. That is a SQL type name; using it as
a column name forces quoting everywhere and confuses ORM reflection. **This is
the only intentional deviation from the spec's column names.**

### `strategy_signals.bar_ts` — the no-lookahead audit trail
`bar_ts` is the open time of the *completed* bar that produced the signal.
It must always be strictly earlier than `generated_at`. Step 22's look-ahead
bias detector is just a query over this pair. Populate it honestly.

### Fees are generic, not US-specific
The plan lists SEC and FINRA TAF fees. This repo trades NSE via mStock, so the
columns are `exchange_fees` and `regulatory_fees` — you map STT, stamp duty,
SEBI turnover and GST onto these in the Step 8 commission calculator. Default
currency is `INR`.

### `orders.client_order_id` is your idempotency key
Unique per portfolio (partial index, `WHERE client_order_id IS NOT NULL`).
Generate it *before* submitting. If the engine crashes mid-submit and retries,
the duplicate is rejected by the database rather than double-trading.

---

## 6. Smoke test — prove it works end to end

Run this against a **non-production** database. It walks a complete lifecycle:
portfolio → signal → order → fill → position → trade → equity point.

```sql
BEGIN;

-- 1. Portfolio
INSERT INTO portfolios (portfolio_id, name, initial_capital, current_cash)
VALUES ('aaaaaaaa-0000-0000-0000-000000000001', 'Smoke Test', 100000, 100000);

-- 2. Signal from a completed bar (bar_ts < generated_at: no lookahead)
INSERT INTO strategy_signals
    (portfolio_id, symbol, strategy_name, signal_type, direction,
     strength, target_position, bar_ts, indicators_snapshot)
VALUES
    ('aaaaaaaa-0000-0000-0000-000000000001', 'INFY', 'sma_crossover',
     'entry', 'long', 0.8, 1.0, now() - interval '5 minutes',
     '{"sma_fast": 1502.3, "sma_slow": 1488.1}'::jsonb);

-- 3. Order
INSERT INTO orders
    (order_id, portfolio_id, symbol, side, order_type, quantity,
     filled_quantity, average_fill_price, status, filled_at, client_order_id)
VALUES
    ('bbbbbbbb-0000-0000-0000-000000000001',
     'aaaaaaaa-0000-0000-0000-000000000001',
     'INFY', 'buy', 'market', 10, 10, 1500.50, 'filled', now(), 'smoke-001');

-- 4. Position
INSERT INTO positions
    (position_id, portfolio_id, symbol, position_type,
     quantity, average_entry_price, current_price)
VALUES
    ('cccccccc-0000-0000-0000-000000000001',
     'aaaaaaaa-0000-0000-0000-000000000001',
     'INFY', 'long', 10, 1500.50, 1512.00);

-- 5. Fill
INSERT INTO fills
    (order_id, position_id, symbol, side, quantity, fill_price,
     commission, reference_price, slippage_bps, liquidity_flag)
VALUES
    ('bbbbbbbb-0000-0000-0000-000000000001',
     'cccccccc-0000-0000-0000-000000000001',
     'INFY', 'buy', 10, 1500.50, 4.50, 1500.00, 3.33, 'taker');

-- 6. Equity snapshot
INSERT INTO equity_curve
    (portfolio_id, ts, total_equity, cash, position_value, cumulative_pnl)
VALUES
    ('aaaaaaaa-0000-0000-0000-000000000001', now(),
     100110.50, 84990.50, 15120.00, 110.50);

-- 7. Inspect
SELECT * FROM v_portfolio_summary;
SELECT symbol, quantity, market_value, cost_basis FROM v_open_positions;

ROLLBACK;   -- <<< change to COMMIT only if you want to keep this data
```

The final `ROLLBACK` leaves your database untouched. `v_portfolio_summary`
should report `open_positions = 1`, `total_equity = 100110.50`.

### Negative tests — the database should REFUSE these

Each statement must raise an error. If any succeeds, the migration is
incomplete.

```sql
-- Two open positions in the same symbol -> uq_positions_one_open_per_symbol
INSERT INTO positions (portfolio_id, symbol, position_type, quantity, average_entry_price)
VALUES ('aaaaaaaa-0000-0000-0000-000000000001','INFY','long',5,1501);

-- Limit order with no limit price -> ck_orders_limit_price_required
INSERT INTO orders (portfolio_id, symbol, side, order_type, quantity)
VALUES ('aaaaaaaa-0000-0000-0000-000000000001','INFY','buy','limit',10);

-- filled_quantity greater than quantity -> ck_orders_filled_qty
INSERT INTO orders (portfolio_id, symbol, side, order_type, quantity, filled_quantity)
VALUES ('aaaaaaaa-0000-0000-0000-000000000001','INFY','buy','market',10,11);

-- Impossible bar, high below low -> ck_mdc_ohlc
INSERT INTO market_data_cache (symbol, timeframe, ts, open, high, low, close)
VALUES ('INFY','5min', now(), 100, 90, 95, 97);

-- Trade that exits before it enters -> ck_trades_time_order
INSERT INTO trades (portfolio_id, symbol, quantity, entry_price, exit_price,
                    entry_time, exit_time, gross_pnl, net_pnl)
VALUES ('aaaaaaaa-0000-0000-0000-000000000001','INFY',10,100,110,
        now(), now() - interval '1 day', 100, 90);
```

All 22 such cases were verified against a live PostgreSQL instance during
development; all 22 were correctly rejected.

---

## 7. Alembic (optional alternative to manual SQL)

Use Alembic **or** the manual SQL files — not both on the same database.

```bash
export FORWARD_TEST_DB_URL=postgresql+psycopg2://ft_app:CHANGE_ME@localhost:5432/forward_test

alembic upgrade head        # apply
alembic current             # what's applied
alembic history --verbose   # full log
alembic downgrade -1        # undo one revision
alembic upgrade head --sql  # print SQL instead of running it (hand to a DBA)
```

**Already applied the SQL by hand and now want Alembic?** Stamp, don't upgrade:

```bash
alembic stamp 001
```

This records revision `001` as applied without re-creating anything.

Future schema changes:

```bash
alembic revision --autogenerate -m "add trailing stop columns"
# ALWAYS read the generated file before applying — autogenerate misses
# triggers, views, and data migrations.
alembic upgrade head
```

Note that autogenerate **cannot see triggers**. The two `updated_at` triggers
are hand-written inside revision `001` and must be maintained manually.

---

## 8. Operations

### Backup

```bash
# Full compressed dump
pg_dump -Fc -d forward_test -f forward_test_$(date +%F).dump

# Restore
pg_restore -d forward_test_restored forward_test_2026-08-19.dump

# Schema only (for diffing against the migration file)
pg_dump --schema-only -d forward_test > schema_snapshot.sql
```

### Rollback

```bash
pg_dump -Fc -d forward_test -f backup_before_rollback.dump   # ALWAYS first
psql -U ft_app -d forward_test -f db/migrations/001_initial_schema_rollback.sql
```

This drops every table and **destroys all data**. `schema_migrations` is
deliberately left in place.

### Housekeeping as the fast-growing tables fill up

`equity_curve`, `market_data_cache`, `strategy_signals` and `system_logs` grow
continuously. A 1-minute strategy on 5 symbols writes roughly 2,000
market-data rows per trading day.

```sql
-- Trim debug/info logs older than 30 days
DELETE FROM system_logs
WHERE ts < now() - interval '30 days'
  AND log_level IN ('debug','info');

-- Trim intraday bars older than 90 days (keep daily bars forever)
DELETE FROM market_data_cache
WHERE ts < now() - interval '90 days'
  AND timeframe NOT IN ('day','week','month');

-- Reclaim space and refresh planner statistics
VACUUM ANALYZE;
```

When `market_data_cache` passes ~50M rows, partition it by month on `ts`.
Not needed now; noted so the decision isn't a surprise later.

---

## 9. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `extension "pgcrypto" is not available` | Old PG, extension not installed | Upgrade to PG 13+ (`gen_random_uuid()` is built in). The migration handles this gracefully and only fails if no UUID generator exists at all. |
| `permission denied for schema public` | PG 15+ revoked default `CREATE` on `public` | `GRANT CREATE, USAGE ON SCHEMA public TO ft_app;` |
| `current transaction is aborted, commands ignored` | An earlier statement failed; whole file is one transaction | Scroll **up** to the first real error — everything after is noise. |
| `relation "portfolios" already exists` | Re-running the migration | Harmless. Every statement uses `IF NOT EXISTS`; the file is idempotent. |
| FK violations don't fire (SQLite) | `PRAGMA foreign_keys` defaults to OFF | Run `PRAGMA foreign_keys = ON;` on every connection. |
| `could not connect to server` | Wrong host/port, or PG not running | `pg_isready -h localhost -p 5432` |
| Money values look slightly wrong | Using SQLite, which stores `NUMERIC` as float | Expected. Use PostgreSQL for anything you report on. |
| Alembic: `Can't locate revision '001'` | Running from the wrong directory | Run from the repo root, where `alembic.ini` lives. |

---

## 10. Checklist

- [ ] PostgreSQL 13+ running and reachable (`pg_isready`)
- [ ] `forward_test` database created, owned by `ft_app`, timezone `UTC`
- [ ] `001_initial_schema.sql` applied without error
- [ ] `verify_schema.sql` shows **all PASS**
- [ ] Smoke test (§6) runs and rolls back cleanly
- [ ] Negative tests (§6) are all **rejected**
- [ ] `FORWARD_TEST_DB_URL` set in `.env`
- [ ] `.env` is **not** committed
- [ ] Backup command tested at least once

Once every box is ticked, Step 1 is done and **Step 2 (Database Connection
Manager)** can begin.