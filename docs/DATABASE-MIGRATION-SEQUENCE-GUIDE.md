# Database Migration Sequence Guide — Windows Direct PostgreSQL

> **Role:** Senior PostgreSQL DBA
> **OS:** Windows 10/11, Direct PostgreSQL (not Docker)
> **Engine:** PostgreSQL 13+ (uses `gen_random_uuid()` in core)
> **File:** `db/migrations/001_initial_schema.sql` is the source of truth

---

## 1. Dependency Graph (Why Order Matters)

Foreign keys enforce this order. Violating it gives `relation does not exist` or `ForeignKeyViolation`.

```
portfolios (ROOT – no FK)
  │
  ├── positions (FK: portfolio_id → portfolios)
  │     │
  │     ├── orders (FK: portfolio_id → portfolios, position_id → positions)
  │     │     │
  │     │     ├── fills (FK: order_id → orders, position_id → positions)
  │     │     │     │
  │     │     └── trades (FK: portfolio_id, position_id, entry_order_id → orders, exit_order_id → orders)
  │     │
  │     ├── equity_curve (FK: portfolio_id → portfolios) + UNIQUE(portfolio_id, ts)
  │     ├── performance_metrics (FK: portfolio_id → portfolios) + UNIQUE(portfolio_id, date)
  │     └── strategy_signals (FK: portfolio_id → portfolios, order_id → orders)
  │           │
  │           └── system_logs (FK: portfolio_id → portfolios ON DELETE SET NULL – logs outlive portfolio)
  │
  ├── market_data_cache (NO FK – independent, but has UNIQUE(symbol, exchange, timeframe, ts))
  └── schema_migrations (bookkeeping, no FK)

Views (depend on tables, created last):
  ├── v_open_positions (positions JOIN portfolios WHERE status='open')
  └── v_portfolio_summary (portfolios + LATERAL equity_curve + counts from positions/trades)
```

**Rule:** `portfolios → positions → orders → fills → trades → equity_curve → market_data_cache → performance_metrics → strategy_signals → system_logs → views`

---

## 2. Files in Repo

| File | Purpose | When to Use |
|---|---|---|
| `db/migrations/001_initial_schema.sql` | **PostgreSQL DDL – 10 tables + 2 views + indexes + triggers** – **USE THIS ON WINDOWS** | Production / local Windows Postgres |
| `db/migrations/001_initial_schema.sqlite.sql` | SQLite variant (INTEGER PK for autoincrement) | Dev only, `FORWARD_TEST_DB_URL=sqlite:///...` |
| `db/migrations/001_initial_schema_rollback.sql` | Destroys all forward testing data – reverse order | Only when tearing down |
| `db/verify_schema.sql` | Verification – prints PASS/FAIL for tables, FKs, indexes, views | After migration |
| `db/alembic/versions/20260819_1657_001_initial_forward_testing_schema.py` | Alembic ORM mirror – must stay byte-equivalent to SQL | If you use `alembic upgrade head` instead of manual psql |

**On Windows with direct Postgres, you only need `001_initial_schema.sql` + `verify_schema.sql`.** The file already contains correct dependency order internally.

---

## 3. Strict Execution Sequence (Numbered)

### Step 0 – Safety (Optional but Recommended)

```sql
-- Backup before any destructive action
-- In cmd: pg_dump -Fc -d forward_test -f backup_before_migration.dump
```

### Step 1 – Ensure UUID Generator Exists

The migration file starts with a `DO $$` block that checks `gen_random_uuid()`:

```sql
-- If to_regproc('gen_random_uuid') IS NULL → tries CREATE EXTENSION pgcrypto
-- If still NULL → RAISES EXCEPTION with clear message
```

- **PostgreSQL 13+:** `gen_random_uuid()` is built-in – no action needed
- **PostgreSQL 11/12:** Needs superuser to run `CREATE EXTENSION pgcrypto;` once
- **Managed hosts (RDS/Supabase):** If you see `Could not create pgcrypto extension`, ask superuser to run it, then re-run file

### Step 2 – Run the Main Migration (One Transaction)

The file is wrapped in `BEGIN; ... COMMIT;` – either all 10 tables land or none.

**Tables created in order inside the file:**

1. `schema_migrations` – bookkeeping (version, description, applied_at)
2. `set_updated_at()` – trigger function for `updated_at`
3. `portfolios` – root aggregate, UNIQUE(name), CHECK status active/paused/stopped, CHECK initial_capital>0, trigger `trg_portfolios_updated_at`, index `ix_portfolios_status`
4. `positions` – FK to portfolios CASCADE, CHECK status open/closed, CHECK position_type long/short, CHECK qty sign vs type, partial unique index `uq_positions_one_open_per_symbol WHERE status='open'` (critical!), indexes on portfolio_status, symbol, opened_at
5. `orders` – FK to portfolios CASCADE, FK to positions SET NULL, CHECK side buy/sell, CHECK order_type market/limit/stop/stop_limit/trailing_stop, CHECK TIF day/gtc/ioc/fok, CHECK qty>0, CHECK filled qty, CHECK limit/stop/trailing required per type, CHECK rejection reason required when rejected, CHECK filled consistency, unique partial index `uq_orders_client_order_id WHERE client_order_id IS NOT NULL`, indexes on portfolio_status, symbol_submitted, position, working (pending/partial)
6. `fills` – FK to orders CASCADE, FK to positions SET NULL, CHECK side, CHECK liquidity_flag maker/taker, CHECK qty>0, price>0, fees non-negative, indexes on order, position, filled_at, symbol
7. `trades` – FK to portfolios CASCADE, FK to positions SET NULL, FK to entry/exit orders SET NULL, CHECK direction long/short, CHECK qty>0, CHECK exit_time>=entry_time, CHECK exit_reason in 8 values, indexes on portfolio_exit, symbol, strategy, net_pnl
8. `equity_curve` – BIGSERIAL PK, FK to portfolios CASCADE, UNIQUE(portfolio_id, ts) for idempotent writer, index on portfolio_ts DESC
9. `market_data_cache` – BIGSERIAL PK, NO FK, CHECK timeframe in 10 values, CHECK OHLC high>=low and high>=open/close and low<=open/close, CHECK prices>0, volume>=0, bid<=ask, UNIQUE(symbol, exchange, timeframe, ts), indexes on symbol_tf_ts, ts
10. `performance_metrics` – BIGSERIAL PK, FK to portfolios CASCADE, CHECK counts, CHECK win_rate 0..1, UNIQUE(portfolio_id, calculation_date), index on portfolio_date
11. `strategy_signals` – BIGSERIAL PK, FK to portfolios CASCADE, FK to orders SET NULL, CHECK signal_type entry/exit, CHECK direction long/short/flat, CHECK strength 0..1, CHECK target_position -1..1, indexes on portfolio_gen, symbol, unexecuted WHERE executed=false, GIN index on indicators_snapshot JSONB
12. `system_logs` – BIGSERIAL PK, FK to portfolios SET NULL (logs outlive portfolio), CHECK log_level debug/info/warning/error/critical, indexes on ts, portfolio_ts, level_ts, component, partial index ix_logs_errors WHERE level in (error,critical)
13. Views:
    - `v_open_positions` – `positions WHERE status='open'` JOIN portfolios
    - `v_portfolio_summary` – portfolios + LATERAL latest equity_curve + counts

14. `INSERT INTO schema_migrations (version='001', description='Initial...') ON CONFLICT DO NOTHING`

### Step 3 – Verify

Run `db/verify_schema.sql` – expect all PASS:

- Tables: 11 (10 + schema_migrations)
- Views: 2
- FKs: 14 (positions 1 + orders 2 + fills 2 + trades 4 + equity_curve 1 + performance_metrics 1 + strategy_signals 2 + system_logs 1 =14)
- Indexes: >=46
- Critical partial indexes: `uq_positions_one_open_per_symbol`, `uq_orders_client_order_id`, `ix_orders_working`, `ix_signals_unexecuted`, `ix_logs_errors`
- Triggers: 2 (portfolios and orders updated_at)
- `gen_random_uuid()` available
- Money columns NUMERIC, not float
- Migration recorded in schema_migrations

### Step 4 – Rollback (Only If Needed)

If you need to destroy and re-create:

```sql
-- Order is REVERSE of creation – leaf tables first
DROP VIEW IF EXISTS v_portfolio_summary;
DROP VIEW IF EXISTS v_open_positions;
DROP TABLE IF EXISTS system_logs;
DROP TABLE IF EXISTS strategy_signals;
DROP TABLE IF EXISTS performance_metrics;
DROP TABLE IF EXISTS market_data_cache;
DROP TABLE IF EXISTS equity_curve;
DROP TABLE IF EXISTS trades;
DROP TABLE IF EXISTS fills;
DROP TABLE IF EXISTS orders;
DROP TABLE IF EXISTS positions;
DROP TABLE IF EXISTS portfolios;
DROP FUNCTION IF EXISTS set_updated_at();
DELETE FROM schema_migrations WHERE version='001';
```

File `001_initial_schema_rollback.sql` already does this in correct reverse order.

---

## 4. Common Windows Errors & Fixes

| Error | Cause | Fix |
|---|---|---|
| `relation "portfolios" does not exist` when creating `positions` | Ran file partially, or wrong DB | Ensure you run entire `001_initial_schema.sql` in one `psql -f`, not per table |
| `ForeignKeyViolation: fk_fills_position` | Saving fills before positions – wrong write order in app | Use `Portfolio.save_to_db(include_orders=True)` – it writes portfolios→positions→orders→fills atomically |
| `uq_positions_one_open_per_symbol` violation on 2nd save | Open rows written before closed rows | Already fixed in `portfolio.py` – writes closed first, then flush, inside `no_autoflush` |
| `current transaction is aborted` | Earlier statement failed, rest are noise | Scroll **up** to first error – usually `gen_random_uuid()` missing |
| `could not create pgcrypto extension` | Non-superuser on managed host | Ask superuser to run `CREATE EXTENSION pgcrypto;` or upgrade to PG 13+ |
| `ck_orders_rejection_reason` violation | Rejected without reason | `Order.reject()` requires non-empty reason – DB enforces same |
| `ck_mdc_ohlc` violation | Bad tick persisted | `DataValidator` (Step 11) should reject before DB, but DB also enforces as safety |

---

## 5. Idempotency Guarantee

Every `CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`, `DROP TRIGGER IF EXISTS` – you can re-run `001_initial_schema.sql` safely. The final `INSERT ... ON CONFLICT DO NOTHING` into `schema_migrations` makes it idempotent.

**For local Windows dev, run once, then use `verify_schema.sql` to confirm PASS, then never re-run unless you intentionally want to reset.**

