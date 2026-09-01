# Full Release Guide — Forward Testing Simulator v1.0 (Windows + PostgreSQL + Real Broker API)

> ⚠️ **HISTORICAL (archived).** References modules deleted in the P1.4/P4.3
> refactor — `dashboard/`, `alerts/`, `analysis/`, `config_manager/`,
> `marketdata/`, `forward/{paper,broker,portfolio,runner,order_ledger,live_engine}.py`.
> Kept for history only; do not use as current documentation.
>
> **Date:** 2026-08-23 · **Branch:** `arena/01a02caa-back-test` · **Tests:** 1175 passing, 4 skipped (need broker creds)
> **Tech Stack:** Python Backend, Windows Direct PostgreSQL, .env for DB + Broker API, Real mStock Credentials, Telegram Preferred

This document combines **Release Notes + 3 Windows Validation Guides** into one scannable file for your manual validation on Windows.

---

## Table of Contents

1. [Release Notes](#release-notes)
2. [Database Migration Sequence Guide](#1-database-migration-sequence-guide)
3. [Windows Postgres Quick-Start Guide](#2-windows-postgres-quick-start-guide)
4. [Manual Testing Checklist](#3-manual-testing-checklist)

---

## Release Notes

### Summary

Full forward testing trading simulator built in 8 phases (24 steps) from database to live dashboard, with bonus alerting, comparison, config management, and CI/CD. All components are **mock-testable** (no credentials needed) with optional live verification via real mStock API and Telegram.

**Tech Stack:** Python 3.9+ Backend, PostgreSQL 13+ (SQLite fallback for dev), SQLAlchemy ORM, Alembic, Pandas/NumPy, Flask + Chart.js Dashboard, Telegram Bot API preferred for alerts (fast, free, reliable vs email delayed, SMS costly).

### Deliverables by Phase

#### Phase 1 — Database Design & Setup (Steps 1–2) ✅
- `db/migrations/001_initial_schema.sql` – 10 tables (portfolios, positions, orders, fills, trades, equity_curve, market_data_cache, performance_metrics, strategy_signals, system_logs) + 2 views (v_open_positions, v_portfolio_summary), 14 FKs, 46+ indexes including 5 critical partial indexes, 2 triggers for updated_at, `gen_random_uuid()` handling for PG 13+ with pgcrypto fallback, idempotent `IF NOT EXISTS`
- `db/migrations/001_initial_schema.sqlite.sql` – SQLite variant with INTEGER PK
- `db/migrations/001_initial_schema_rollback.sql` – reverse dependency order
- `db/verify_schema.sql` – 11 checks printing PASS/FAIL
- `src/backtest/db/models.py` – SQLAlchemy ORM mirror, 10 tables, StrEnum for CHECK constraints
- `src/backtest/db/manager.py` – DatabaseManager with QueuePool 5/20 for PG, NullPool/StaticPool for SQLite, auto-reconnection, retry only transient faults (message-based), transaction context managers, health_check, pool_status
- `config/database.yaml` – 3 profiles development/testing/production

**Tests:** 44 + 107 =151

#### Phase 2 — Core Data Models (Steps 3–6) ✅
- `simulator/money.py` – Decimal helpers, `to_decimal` via `repr()` for float safety, rejects bool, 4dp money, 8dp price
- `simulator/errors.py` – Domain exceptions
- `simulator/lots.py` – LotBook with FIFO/LIFO/AVERAGE, splits, dividends, `to_dict`/`from_dict` lossless
- `simulator/position.py` – Position with signed quantity, market_value signed, unrealized/realized PnL gross of commission
- `simulator/portfolio.py` – Portfolio root aggregate, cash convention long pays / short receives, `calculate_total_equity = cash + position_value`, `can_open_position` → `PositionCheck`, `save_to_db` writes **closed first then open** to satisfy partial unique index
- `simulator/enums.py` – OrderSide, OrderType (5 types), OrderStatus, TimeInForce, VALID_TRANSITIONS FSM
- `simulator/order.py` – Order full lifecycle, 5 order types, trigger sticky, trailing ratchet one-way
- `simulator/fill.py` – Fill immutable, 5 commission models

**Tests:** 130 + 77 + 115 + 106 =428

#### Phase 3 — Order Execution Simulation (Steps 7–9) ✅
- `simulator/slippage.py` – 5 models: Zero, FixedBps, Spread (fallback_bps), VolumeImpact (sqrt law), Volatility (ATR), Hybrid, 4 profiles, tiers, time-of-day, limit cap, signed adverse-positive, max_bps 1000
- `simulator/fees.py` – IndiaEquityFees default (STT 0.1% both sides delivery, 0.025% sell-only intraday), USEquityFees, 10 broker presets, FeeBreakdown → 3 fills columns
- `simulator/execution.py` – OrderExecutor with liquidity caps `max_participation` 10%, queue `touch_fill_probability` 0.5, latency reported not slept, seeded RNG, `enforce_market_hours` off by default, NO_FILL vs REJECTED distinct
- Configs: `slippage.yaml`, `execution.yaml`, `brokers.yaml`

**Tests:** 101 + 109 + 99 =309

#### Phase 4 — Live Data Integration (Steps 10–12) ✅ Mock-Only + Manual
- `live/time_manager.py` – NSE 09:15-15:30 IST, weekend/holiday, pre-market/after-hours, next open/close, trading days, bar alignment, IST/UTC/ET via ZoneInfo, mock time controllable, latency stats
- `live/data_validator.py` – OHLC sanity, price range, bid<=ask, spike Z-score, gap detection, volume anomaly, strictness levels
- `live/market_data_handler.py` – Normalization to standard format, BarBuilder aggregates ticks into 1min/3min/5min/15min/30min/1hr/1day, multi-symbol, reconnection with backoff, bounded buffers, observer pattern, DB cache to MARKET_DATA_CACHE, MockBrokerFeed + MStockBrokerFeed wired to `live/mstock.py`
- Configs: `market_data.yaml`, `data_quality.yaml`, `time_sync.yaml`
- `docs/LOCAL-TESTING-MANUAL.md` – Local testing guide

**Tests:** 18+18+18=54

#### Phase 5 — Strategy Integration (Steps 13–14) ✅
- `forward/strategy_adapter.py` – Bridges `strategy/base.py` (no duplication), Signal model BUY/SELL/HOLD, no lookahead `bar_ts < generated_at`, per-symbol DataFrames, multi-symbol, dry-run, DB logging with FK handling, state persistence
- `simulator/position_sizing.py` – 6 methods: fixed qty/dollar, % portfolio, risk-based `qty=(equity*risk%)/(price*stop%)`, volatility/ATR `qty=risk/(ATR*mult)`, Kelly `f*=p-q/b`, constraints max value/pct, gross exposure, min trade, round lots, 8 profiles
- Config: `position_sizing.yaml`

**Tests:** 20 + 25 =45

#### Phase 6 — Risk Management (Steps 15–16) ✅
- `simulator/risk_manager.py` – Hierarchy order→position→portfolio, order-level restricted/allowed, min/max value, % daily vol, position-level max value/pct, max positions, sector concentration, portfolio-level drawdown, daily loss, leverage, gross exposure, circuit breakers, emergency_stop_all, override, alerts
- `simulator/stop_manager.py` – 6 stop types fixed/percentage/ATR/trailing fixed/trailing %/time, 5 TP types fixed/%/risk-reward/resistance/trailing, breakeven move, scale-out, OCO groups, trailing ratchet one-way
- Configs: `risk.yaml`, `stops.yaml`

**Tests:** 24 + 21 =45

#### Phase 7 — Performance Tracking (Steps 17–19) ✅
- `simulator/performance.py` – Return metrics total %/CAGR/annualized/daily/cumulative/MoM/best-worst day/week/month, risk metrics vol/annualized vol/max DD $/%/DD duration/current DD/VaR 95%/99%, ratios Sharpe/Sortino/Calmar/Information/Treynor, trade stats total/win/loss/win rate/avg win/loss/largest/profit factor/holding/expectancy/consecutive/commission, real-time equity curve, DB persistence
- `simulator/trade_analyzer.py` – AnalyzedTrade enriched, categorize by symbol/strategy/time/day/holding/pnl_bucket/exit_reason, patterns streaks/performance by hour/day/best-worst symbols/optimal holding, quality metrics execution quality bps/slippage/commission %/MAE/MFE, report + export CSV/JSON/Excel
- `dashboard/` – Flask + Chart.js, 0.0.0.0 bind for Arena preview, API endpoints, 7 sections: portfolio overview, open positions with close btn, recent trades green/red, equity line, daily P&L bar, drawdown line, win/loss pie, active orders cancel btn, key metrics, system status, controls start/stop/pause/resume, manual order form, logs, dark/light mode, responsive

**Tests:** 14 + 15 + 15 =44

#### Phase 8 — System Orchestration (Step 20) ✅
- `forward/engine.py` – ForwardTestingEngine with ForwardTestingConfig 7 sections, StateManager atomic JSON save, initialize_system DB+portfolio+strategy+sizer+executor+adapter+data handler+validators/managers, start/pause/resume/stop with graceful save, run_loop live polling, _run_backtest_mode replay, lifecycle hooks with isolation, signal handlers SIGINT/SIGTERM, heartbeat, slow-loop warning, dry-run & backtest modes, CLI
- `config/forward_testing.yaml`, `Dockerfile`, `forward_testing.service`

**Tests:** 14

#### Bonus (Steps 21–24) ✅ 100% Complete

**Step 21 – Alert & Notification System** – `alerts/manager.py` + `config/alerts.yaml`
- 7 channels: **Telegram Bot API preferred** (fast, free, reliable) – user preferred over email delayed and SMS costly, plus Email SMTP TLS, SMS Twilio mock, Slack webhook, Discord webhook, Desktop plyer, Log file
- AlertLevel, AlertType 9 types, ChannelConfig with creds from .env, AlertConfig with routing by level/type, quiet hours IST, rate limiting, templates, history, convenience methods
- 33 tests

**Step 22 – Backtesting Comparison Tool** – `analysis/comparison.py`
- ComparisonAnalyzer loads backtest JSON/CSV/DataFrame and forward DB/portfolio/file, compare_metrics, compare_trades, attribution, bias detection query `bar_ts >= generated_at` should be 0, t-test, PDF report with side-by-side equity curves
- 13 tests

**Step 23 – Configuration Manager** – `config_manager/manager.py` + `config/app.yaml`
- Unified manager for all YAMLs with layered precedence defaults<YAML<env<overrides, dot-path get/set, safe logging redacts secrets, safe save skips secrets, .env support, hot-reload
- 13 tests

**Step 24 – Testing & CI/CD Setup** – `.github/workflows/ci.yml`, `.pre-commit-config.yaml`, `tox.ini`, `tests/unit/`, `integration/`, `e2e/`, `fixtures/`, `benchmarks/`
- ci.yml: lint (black/isort/flake8/pylint/mypy) → test matrix py39/310/311 with pytest-cov 80% → build Docker → deploy-staging → deploy-production manual approval (workflow file requires workflows permission – present locally but needs manual addition via GitHub UI)
- Pre-commit hooks, tox, fixtures (random ticks/bars, corrupted, spike, MockBrokerAPI), benchmarks (performance + load tests 100 symbols/1000 orders/1000 bars)
- **Total: 1175 passing, 4 skipped**

---

## Known Limitations

1. Tax lots not persisted – no lots table – FIFO position reloaded collapses to one lot at average – workaround `to_dict()`/`from_dict()` JSON snapshot lossless – Step 20 state manager should use it
2. Order `status_history`, `triggered`, `extreme_price` not persisted – no columns – survives in JSON snapshot

## Deviations from Plan

1. SEC/FINRA TAF fees (US) → Both regimes implemented (IndiaEquityFees default, USEquityFees available)
2. Column `timestamp` → `ts` – timestamp is SQL type name
3. NYSE calendar → NSE calendar 09:15-15:30 IST
4. Broker Alpaca/IBKR → mStock (already implemented in `live/`)
5. New Strategy base class (Step 13) → Adapt existing `strategy/base.py`
6. Native SQL ENUM → VARCHAR + CHECK – portability to SQLite
7. New `simulator/` package for Steps 3-6 – avoids collision

---

# 1. Database Migration Sequence Guide

> **Role:** Senior PostgreSQL DBA
> **OS:** Windows 10/11, Direct PostgreSQL (not Docker)
> **Engine:** PostgreSQL 13+ (uses `gen_random_uuid()` in core)
> **File:** `db/migrations/001_initial_schema.sql` is the source of truth

## 1.1 Dependency Graph (Why Order Matters)

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

## 1.2 Files in Repo

| File | Purpose | When to Use |
|---|---|---|
| `db/migrations/001_initial_schema.sql` | **PostgreSQL DDL – 10 tables + 2 views + indexes + triggers** – **USE THIS ON WINDOWS** | Production / local Windows Postgres |
| `db/migrations/001_initial_schema.sqlite.sql` | SQLite variant (INTEGER PK for autoincrement) | Dev only, `FORWARD_TEST_DB_URL=sqlite:///...` |
| `db/migrations/001_initial_schema_rollback.sql` | Destroys all forward testing data – reverse order | Only when tearing down |
| `db/verify_schema.sql` | Verification – prints PASS/FAIL for tables, FKs, indexes, views | After migration |
| `db/alembic/versions/20260819_1657_001_initial_forward_testing_schema.py` | Alembic ORM mirror – must stay byte-equivalent to SQL | If you use `alembic upgrade head` instead of manual psql |

**On Windows with direct Postgres, you only need `001_initial_schema.sql` + `verify_schema.sql`.** The file already contains correct dependency order internally.

## 1.3 Strict Execution Sequence (Numbered)

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
- Money columns NUMERIC, never float
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

## 1.4 Common Windows Errors & Fixes

| Error | Cause | Fix |
|---|---|---|
| `relation "portfolios" does not exist` when creating `positions` | Ran file partially, or wrong DB | Ensure you run entire `001_initial_schema.sql` in one `psql -f`, not per table |
| `ForeignKeyViolation: fk_fills_position` | Saving fills before positions – wrong write order in app | Use `Portfolio.save_to_db(include_orders=True)` – it writes portfolios→positions→orders→fills atomically |
| `uq_positions_one_open_per_symbol` violation on 2nd save | Open rows written before closed rows | Already fixed in `portfolio.py` – writes closed first, then flush, inside `no_autoflush` |
| `current transaction is aborted` | Earlier statement failed, rest are noise | Scroll **up** to first error – usually `gen_random_uuid()` missing |
| `could not create pgcrypto extension` | Non-superuser on managed host | Ask superuser to run `CREATE EXTENSION pgcrypto;` or upgrade to PG 13+ |
| `ck_orders_rejection_reason` violation | Rejected without reason | `Order.reject()` requires non-empty reason – DB enforces same |
| `ck_mdc_ohlc` violation | Bad tick persisted | `DataValidator` (Step 11) should reject before DB, but DB also enforces as safety |

## 1.5 Idempotency Guarantee

Every `CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`, `DROP TRIGGER IF EXISTS` – you can re-run `001_initial_schema.sql` safely. The final `INSERT ... ON CONFLICT DO NOTHING` into `schema_migrations` makes it idempotent.

**For local Windows dev, run once, then use `verify_schema.sql` to confirm PASS, then never re-run unless you intentionally want to reset.**

---

# 2. Windows Postgres Quick-Start Guide

> **Roles:** Windows DevOps Specialist + PostgreSQL DBA
> **OS:** Windows 10/11, PostgreSQL installed directly (e.g. from https://www.postgresql.org/download/windows/)
> **Tools:** `psql`, `createdb`, `pg_dump`, PowerShell / cmd
> **App:** Python Backend, `PYTHONPATH=src`

## 2.1 Verify PostgreSQL Installation on Windows

### Check psql is in PATH

**PowerShell:**
```powershell
psql --version
# Expected: psql (PostgreSQL) 15.x or 16.x

# If not found, add to PATH (adjust version):
$env:Path += ";C:\Program Files\PostgreSQL\15\bin"
# Permanent:
[Environment]::SetEnvironmentVariable("Path", $env:Path + ";C:\Program Files\PostgreSQL\15\bin", "User")
```

**cmd:**
```cmd
psql --version
:: If not found:
set PATH=%PATH%;C:\Program Files\PostgreSQL\15\bin
```

### Check service is running

**PowerShell:**
```powershell
Get-Service -Name postgresql*
# Should be Running

# If not:
net start postgresql-x64-15
```

## 2.2 Log Into Local Postgres Server

**Default superuser is `postgres` with password you set during installation.**

**PowerShell / cmd:**
```powershell
# Connect to default 'postgres' database
psql -U postgres -h localhost -p 5432 -d postgres

# You'll be prompted for password
# If you want to avoid prompt, set PGPASSWORD env (PowerShell):
$env:PGPASSWORD="your_postgres_password"
psql -U postgres -h localhost -d postgres

# Inside psql, you should see:
# postgres=#
```

**If peer auth fails:**
```powershell
# Try with -W to force password prompt
psql -U postgres -h 127.0.0.1 -W -d postgres
```

## 2.3 Create New Database `forward_test`

**Inside psql (`postgres=#`):**
```sql
-- Create database
CREATE DATABASE forward_test OWNER postgres;

-- Optional: create app user (recommended, not using superuser for app)
CREATE USER ft_app WITH PASSWORD 'ChangeMe123!';
GRANT ALL PRIVILEGES ON DATABASE forward_test TO ft_app;

-- Exit psql
\q
```

**Or via cmd (one-liner):**
```cmd
:: Create DB via createdb tool
createdb -U postgres -h localhost -O postgres forward_test

:: Or with custom user
psql -U postgres -h localhost -d postgres -c "CREATE USER ft_app WITH PASSWORD 'ChangeMe123!';"
psql -U postgres -h localhost -d postgres -c "CREATE DATABASE forward_test OWNER ft_app;"
psql -U postgres -h localhost -d postgres -c "GRANT ALL PRIVILEGES ON DATABASE forward_test TO ft_app;"
```

**Verify DB exists:**
```powershell
psql -U postgres -h localhost -d postgres -c "\l" | findstr forward_test
```

## 2.4 Execute SQL Scripts in Correct Order

**You are in `C:\Users\YourName\back-test` or wherever you cloned.**

**PowerShell (recommended):**
```powershell
cd C:\Users\YourName\back-test

# Set password for psql to avoid prompts
$env:PGPASSWORD="your_postgres_password"  # or ft_app password if you created ft_app user

# Step 1: Run main migration (one transaction, correct FK order internally)
psql -U postgres -h localhost -d forward_test -f db/migrations/001_initial_schema.sql

# Expected: BEGIN, CREATE TABLE, CREATE INDEX, INSERT, COMMIT – no errors
# If you created ft_app user, use:
psql -U ft_app -h localhost -d forward_test -f db/migrations/001_initial_schema.sql

# Step 2: Verify schema – expect all PASS
psql -U postgres -h localhost -d forward_test -f db/verify_schema.sql

# Output should show:
# table_count 11 | PASS
# view_count 2 | PASS
# fk_count 14 | PASS
# index_count >=46 | PASS
# etc.
```

**cmd:**
```cmd
cd /d C:\Users\YourName\back-test
set PGPASSWORD=your_postgres_password
psql -U postgres -h localhost -d forward_test -f db\migrations\001_initial_schema.sql
psql -U postgres -h localhost -d forward_test -f db\verify_schema.sql
```

**If you use Alembic instead of manual SQL:**
```powershell
# Alembic uses same ORM models – should produce no diff if SQL file already applied
$env:FORWARD_TEST_DB_URL="postgresql+psycopg2://ft_app:ChangeMe123!@localhost:5432/forward_test"
alembic -c alembic.ini upgrade head
# Should say "Running upgrade 001 -> ..."
```

## 2.5 Configure Python `.env` File

**Location:** Repo root, same folder as `.env.example` – **gitignored**, never pushed.

**PowerShell:**
```powershell
Copy-Item .env.example .env
notepad .env
```

**Exact syntax for `.env` (Windows):**

```ini
# =============================================================================
# mStock TypeA API – Real Broker Credentials (from https://api.mstock.trade)
# =============================================================================
MSTOCK_API_KEY=your_api_key_from_mstock_dashboard
MSTOCK_USERNAME=your_client_code_like_AB1234
MSTOCK_PASSWORD=your_mstock_password
MSTOCK_CHECKSUM=W
MSTOCK_AUTH_MODE=otp
# otp = SMS OTP (set MSTOCK_OTP env when prompted) or totp = authenticator app
MSTOCK_BASE_URL=https://api.mstock.trade

# Optional: if you already have valid token, cache it to skip OTP
# The auth module caches token in .mstock_session_token file (also gitignored)
# You can manually create .mstock_session_token with token string

# =============================================================================
# Telegram Alerts – Preferred (fast, free) – Step 21
# =============================================================================
# 1. Chat @BotFather on Telegram → /newbot → get token like 123456:ABC-DEF...
# 2. Send message to your bot, then visit https://api.telegram.org/bot<token>/getUpdates to get chat_id
TELEGRAM_BOT_TOKEN=123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11
TELEGRAM_CHAT_ID=123456789

# Optional: Slack / Discord webhooks
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/T00000000/B00000000/XXXXXXXXXXXXXXXXXXXXXXXX
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/1234567890/ABC-DEF...

# Optional: Email alerts (may be delayed)
ALERT_EMAIL_SMTP_HOST=smtp.gmail.com
ALERT_EMAIL_SMTP_PORT=587
ALERT_EMAIL_USER=your_email@gmail.com
ALERT_EMAIL_PASSWORD=your_app_password_from_env
ALERT_EMAIL_FROM=your_email@gmail.com
ALERT_EMAIL_TO=your_email@gmail.com,other@example.com

# =============================================================================
# PostgreSQL – Windows Direct Installation
# =============================================================================
# Format: postgresql+psycopg2://user:password@host:port/dbname
# If password has special chars like @ or :, URL-encode them: @ -> %40, : -> %3A

# Option A: Using superuser postgres (simplest for local dev)
FORWARD_TEST_DB_URL=postgresql+psycopg2://postgres:your_postgres_password@localhost:5432/forward_test

# Option B: Using app user ft_app (recommended)
# FORWARD_TEST_DB_URL=postgresql+psycopg2://ft_app:ChangeMe123!@localhost:5432/forward_test

# Profile: development (SQLite fallback), testing (in-memory), production (requires real URL)
FORWARD_TEST_DB_PROFILE=development

# Optional: log every SQL query (DEBUG level) – useful for Manual Testing Checklist
FORWARD_TEST_DB_LOG_QUERIES=true
FORWARD_TEST_DB_SLOW_QUERY_MS=200
```

**Critical Windows notes:**
- Use `postgresql+psycopg2://` prefix (SQLAlchemy + psycopg2 driver)
- No spaces around `=`
- If path has spaces, wrap entire file path in quotes when using in PowerShell, but `.env` itself should NOT have quotes around values
- Save as UTF-8, not UTF-16 (Notepad default is okay, but ensure no BOM)

**Verify .env is ignored:**
```powershell
git status
# Should NOT show .env as to-be-committed – if it does, check .gitignore has .env
git check-ignore -v .env
# Expected: .gitignore:.env
```

## 2.6 Spin Up Python Virtual Environment on Windows

**PowerShell:**
```powershell
# In repo root
python --version
# Should be 3.9+ (3.11 recommended)

# Create venv
python -m venv .venv

# Activate
.\.venv\Scripts\Activate.ps1
# If execution policy blocks:
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser

# Upgrade pip
python -m pip install --upgrade pip

# Install deps
pip install -r requirements.txt

# Verify
pip list | findstr -i "pandas sqlalchemy psycopg2"
```

**cmd:**
```cmd
python -m venv .venv
.\.venv\Scripts\activate.bat
pip install -r requirements.txt
```

**Expected packages:** `pandas`, `numpy`, `requests`, `python-dotenv`, `SQLAlchemy`, `alembic`, `psycopg2-binary`, `PyYAML`, `Flask`, `matplotlib`, `pyarrow`, `pytest`

## 2.7 Start the App

### Option A – Run Engine (Main Forward Testing Loop – Step 20)

**PowerShell:**
```powershell
$env:PYTHONPATH="src"
$env:FORWARD_TEST_DB_URL="postgresql+psycopg2://postgres:your_password@localhost:5432/forward_test"

# Dry-run (signals but no trades) – safest first run
python -m backtest.forward.engine --config config/forward_testing.yaml --dry-run --symbols INFY

# Backtest replay mode with mock data (no broker needed)
python -m backtest.forward.engine --backtest --symbols INFY --dry-run

# Live papertrade with real mStock (requires .env with MSTOCK_* creds)
python -m backtest.forward.engine --config config/forward_testing.yaml --symbols INFY TCS

# Or via Python script:
python - << 'PY'
from backtest.forward.engine import ForwardTestingEngine
engine = ForwardTestingEngine(config_file="config/forward_testing.yaml")
engine.initialize_system()
print(engine.get_status())
# engine.start()  # blocks – uncomment when ready
PY
```

### Option B – Run Dashboard (Step 19)

**PowerShell:**
```powershell
$env:PYTHONPATH="src"

# Dashboard with mock portfolio demo (no DB needed)
python -m backtest.dashboard.app --host 0.0.0.0 --port 5000

# Or with real engine data
python - << 'PY'
from backtest.forward.engine import ForwardTestingEngine
from backtest.dashboard.app import run_dashboard

engine = ForwardTestingEngine(config_file="config/forward_testing.yaml")
engine.initialize_system()
run_dashboard(host="0.0.0.0", port=5000, portfolio=engine.portfolio, engine=engine, data_handler=engine.data_handler, performance=engine.performance)
PY
```

Open browser: `http://localhost:5000` – you should see Portfolio Overview, Open Positions, Equity Curve, etc., auto-refresh 5s.

### Option C – Run Tests (Verify Everything Works)

```powershell
$env:PYTHONPATH="src"
pytest tests/ -q -k "not live"
# Expected: 1175 passed, 4 skipped

# With live mStock (requires .env):
pytest tests/test_mstock_live_integration.py -s
```

## 2.8 Quick Reference – All Windows Commands in One Block

**Copy-paste for PowerShell (adjust passwords):**

```powershell
cd C:\Users\YourName\back-test
$env:PGPASSWORD="postgres_password"
psql -U postgres -h localhost -d postgres -c "CREATE DATABASE forward_test OWNER postgres;"
psql -U postgres -h localhost -d forward_test -f db/migrations/001_initial_schema.sql
psql -U postgres -h localhost -d forward_test -f db/verify_schema.sql

Copy-Item .env.example .env
notepad .env
# Fill FORWARD_TEST_DB_URL and MSTOCK_* and TELEGRAM_*

python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:PYTHONPATH="src"
pytest tests/ -q -k "not live"
python -m backtest.forward.engine --dry-run --symbols INFY
python -m backtest.dashboard.app --port 5000
```

**You’re now ready for Manual Testing Checklist.**

---

# 3. Manual Testing Checklist

> **Roles:** Python QA Engineer + Windows DevOps + PostgreSQL DBA
> **Stack:** Python Backend, Windows Direct PostgreSQL, .env for DB + Broker API, Real mStock Credentials
> **Goal:** Verify app reads/writes correctly to local Windows PostgreSQL and communicates with broker API

## Pre-Flight (5 mins)

### 1. Verify .env

```powershell
cd C:\Users\YourName\back-test
cat .env | findstr -v "PASSWORD\|TOKEN\|SECRET"
# Should show FORWARD_TEST_DB_URL, MSTOCK_API_KEY, TELEGRAM_BOT_TOKEN etc.
# Ensure no real secrets printed in logs – check that sensitive values are "***" in safe logs

# Check .env is ignored
git check-ignore -v .env
# Expected: .gitignore:.env
```

**Expected `.env` content (redacted):**
```ini
FORWARD_TEST_DB_URL=postgresql+psycopg2://postgres:****@localhost:5432/forward_test
MSTOCK_API_KEY=****
MSTOCK_USERNAME=AB1234
TELEGRAM_BOT_TOKEN=****
TELEGRAM_CHAT_ID=123456789
```

### 2. Verify DB Connection

```powershell
$env:PGPASSWORD="your_postgres_password"
psql -U postgres -h localhost -d forward_test -c "SELECT count(*) FROM portfolios; SELECT * FROM schema_migrations;"
# Expected: count may be 0 initially, schema_migrations should have version 001
```

### 3. Verify Python Env

```powershell
.\.venv\Scripts\Activate.ps1
$env:PYTHONPATH="src"
python -c "from backtest.db.manager import DatabaseManager; db=DatabaseManager.from_env(); print(db.health_check())"
# Expected: {"healthy": True, "dialect": "postgresql", "latency_ms": <100}
```

---

## Happy Path — 10 Steps

### Step 1 – Create/Load Portfolio (Write to DB)

```powershell
python - << 'PY'
from backtest.db.config import DatabaseConfig
from backtest.db.manager import DatabaseManager
from backtest.db.models import Base
from backtest.simulator.portfolio import Portfolio

cfg = DatabaseConfig(url="postgresql+psycopg2://postgres:your_password@localhost:5432/forward_test")
db = DatabaseManager(cfg)
db.connect()
Base.metadata.create_all(db.engine)

portfolio = Portfolio(name="ManualTestWin", initial_capital=100000)
portfolio.save_to_db(db)
print(f"Portfolio saved: {portfolio.portfolio_id} {portfolio.name}")

# Load back
loaded = Portfolio.load_from_db(db, portfolio.portfolio_id)
print(f"Loaded: {loaded.name} cash={loaded.current_cash}")

db.disconnect()
PY
```

**Verify in psql:**
```powershell
psql -U postgres -h localhost -d forward_test -c "SELECT portfolio_id, name, initial_capital, current_cash, status FROM portfolios WHERE name='ManualTestWin';"
# Expected: 1 row, name ManualTestWin, 100000, active
```

**✅ Pass criteria:** Portfolio row exists, `current_cash` = 100000, no FK errors.

---

### Step 2 – Test Market Data Handler (Mock-Only, No Broker)

```powershell
python - << 'PY'
from backtest.live.market_data_handler import MarketDataHandler

handler = MarketDataHandler(symbols=["INFY"], provider="mock")
handler.connect()
handler.subscribe_symbols(["INFY"])

ticks = []
handler.on_tick_received(lambda t: ticks.append(t))

handler.inject_tick({"symbol":"INFY","bid":1499,"ask":1501,"last":1500,"volume":100,"timestamp":"2024-01-02T09:15:10+05:30"})
handler.inject_tick({"symbol":"INFY","last":1501,"volume":50,"timestamp":"2024-01-02T09:15:20+05:30"})
handler.inject_tick({"symbol":"INFY","last":1502,"volume":20,"timestamp":"2024-01-02T09:16:10+05:30"})

print(f"Ticks received: {len(ticks)}")
print(f"Current quote: {handler.get_current_quote('INFY')}")
print(f"Current bar 1min: {handler.get_current_bar('INFY','1min')}")
print(f"Stats: {handler.get_stats()}")
PY
```

**✅ Pass criteria:** Ticks received =2 (first 2 same minute), 1 bar closed on minute boundary (open 1500 close 1501 vol 150), buffers bounded.

---

### Step 3 – Test Data Validator (Corrupted Data)

```powershell
python - << 'PY'
from backtest.live.data_validator import DataValidator

validator = DataValidator(config={"strictness":"normal"})

# Valid
result = validator.validate_bar({"symbol":"INFY","open":100,"high":101,"low":99,"close":100,"volume":1000})
print(f"Valid bar: {result.valid} {result.code}")

# Invalid OHLC
result = validator.validate_bar({"symbol":"INFY","open":100,"high":98,"low":99,"close":100,"volume":1000})
print(f"Invalid high<low: {result.valid} {result.code} {result.reason}")

# Spike
for p in [100,101,100,101,100,101,100,101,100,101]:
    validator._price_history["INFY"].append(p)
result = validator.check_for_spikes(200, "INFY")
print(f"Spike 200 vs 100 avg: {result.valid} {result.code}")

print(f"Stats: {validator.get_stats()}")
PY
```

**✅ Pass criteria:** Valid passes, invalid fails with code `ohlc_high_low`, spike fails with `price_spike`, stats show failure rate.

---

### Step 4 – Test Time Manager (NSE Calendar)

```powershell
python - << 'PY'
from backtest.live.time_manager import TimeManager
from datetime import datetime
from zoneinfo import ZoneInfo

tm = TimeManager(market="NSE")

# Tuesday 10:00 IST open
dt = datetime(2024, 1, 2, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
print(f"Tue 10:00 open? {tm.is_market_open(dt)}")  # True

# Saturday closed
dt = datetime(2024, 1, 6, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
print(f"Sat 10:00 open? {tm.is_market_open(dt)}")  # False

# Bar alignment 09:17:32 -> 09:15 for 5min
aligned = tm.align_to_timeframe("2024-01-02T09:17:32+05:30", "5min")
print(f"Aligned 09:17:32 to 5min: {aligned}")

# Next open after Tue 16:00 -> Wed 09:15
next_open = tm.get_next_market_open(datetime(2024, 1, 2, 16, 0, tzinfo=ZoneInfo("Asia/Kolkata")))
print(f"Next open after Tue 16:00: {next_open}")
PY
```

**✅ Pass criteria:** Weekday 10:00 True, weekend False, alignment 09:15, next open Wed 09:15.

---

### Step 5 – Test Strategy Adapter (No Lookahead)

```powershell
python - << 'PY'
from backtest.strategy.registry import get_strategy
from backtest.simulator.portfolio import Portfolio
from backtest.forward.strategy_adapter import StrategyAdapter

Sma = get_strategy("sma_crossover")
strat = Sma(fast=2, slow=3)
portfolio = Portfolio(name="AdapterTest", initial_capital=100000)
adapter = StrategyAdapter(strategy=strat, portfolio=portfolio, symbols=["INFY"], min_bars=3)

bars = [
    {"symbol":"INFY","timestamp":"2024-01-01T09:15:00+05:30","open":100,"high":101,"low":99,"close":100,"volume":1000},
    {"symbol":"INFY","timestamp":"2024-01-02T09:15:00+05:30","open":101,"high":102,"low":100,"close":101,"volume":1000},
    {"symbol":"INFY","timestamp":"2024-01-03T09:15:00+05:30","open":102,"high":103,"low":101,"close":102,"volume":1000},
]

for bar in bars:
    sigs = adapter.on_bar_close(bar)
    print(f"Bar {bar['close']} -> signals: {len(sigs)} action={sigs[0].action if sigs else 'none'}")

# Check no lookahead: bar_ts < generated_at
for sig in adapter.signal_history:
    assert sig.bar_ts < sig.generated_at
print(f"No lookahead verified for {len(adapter.signal_history)} signals")

print(f"Orders: {len(adapter.order_history)}")
PY
```

**✅ Pass criteria:** First 2 bars no signals (min_bars), 3rd bar BUY signal, `bar_ts < generated_at` for all, order created.

---

### Step 6 – Test Position Sizing (6 Methods)

```powershell
python - << 'PY'
from backtest.simulator.position_sizing import PositionSizer, SizingConfig, SizingConstraints
from backtest.simulator.portfolio import Portfolio

portfolio = Portfolio(name="SizingTest", initial_capital=100000)

tests = [
    ("fixed_quantity", {"fixed_quantity": 100}, 100),
    ("fixed_dollar", {"fixed_dollar_amount": 10000}, 100),
    ("percentage_portfolio", {"percentage": 0.05}, 50),
    ("risk_based", {"risk_per_trade": 0.01, "stop_loss_pct": 0.02}, 500),
]

for method, params, expected in tests:
    cfg = SizingConfig(method=method, **params)
    sizer = PositionSizer(cfg)
    qty = sizer.calculate_position_size(symbol="INFY", current_price=100, portfolio=portfolio)
    print(f"{method} @100: {qty} expected {expected} {'✅' if int(qty)==expected else '❌'}")

# Kelly
cfg = SizingConfig(method="kelly", win_rate=0.55, avg_win=150, avg_loss=100, kelly_fraction=0.5)
sizer = PositionSizer(cfg)
qty = sizer.calculate_position_size(symbol="INFY", current_price=100, portfolio=portfolio)
print(f"kelly: {qty} expected 125")

# Constraints: max 10% cap
cfg = SizingConfig(method="fixed_quantity", fixed_quantity=1000, constraints=SizingConstraints(max_position_pct=0.1))
sizer = PositionSizer(cfg)
qty = sizer.calculate_position_size(symbol="INFY", current_price=100, portfolio=portfolio)
print(f"Constrained to 10%: {qty} expected 100")
PY
```

**✅ Pass criteria:** All methods return expected qty, constraints cap correctly.

---

### Step 7 – Test Risk Manager (Circuit Breakers)

```powershell
python - << 'PY'
from backtest.simulator.portfolio import Portfolio
from backtest.simulator.risk_manager import RiskManager, RiskConfig
from backtest.simulator.order import Order

portfolio = Portfolio(name="RiskTest", initial_capital=100000)
risk = RiskManager(portfolio, RiskConfig(restricted_symbols={"BAD"}, max_drawdown_pct=0.1))

# Restricted symbol should be rejected
order = Order(symbol="BAD", side="buy", quantity=10, order_type="market")
order.submit()
result = risk.validate_order(order, current_price=100)
print(f"BAD symbol allowed? {result.allowed} code={result.code}")  # False, restricted_symbol

# Valid should pass
order2 = Order(symbol="INFY", side="buy", quantity=10, order_type="market")
order2.submit()
print(f"INFY allowed? {risk.validate_order(order2, current_price=100).allowed}")  # True

# Emergency stop
risk.emergency_stop_all("Manual test emergency")
print(f"Halted? {risk.is_halted()}")  # True
print(f"New order after halt allowed? {risk.validate_order(order2, current_price=100).allowed}")  # False

# Override
risk.config.allow_override = True
risk.config.override_code = "SECRET123"
print(f"Override with wrong code: {risk.override('WRONG')}")  # False
print(f"Override with correct: {risk.override('SECRET123')}")  # True
print(f"Halted after override? {risk.is_halted()}")  # False
PY
```

**✅ Pass criteria:** Restricted rejected, valid passes, halt blocks new orders, override resumes.

---

### Step 8 – Test Stop Manager (Trailing + OCO)

```powershell
python - << 'PY'
from backtest.simulator.portfolio import Portfolio, PortfolioLimits
from backtest.simulator.stop_manager import StopManager

portfolio = Portfolio(name="StopTest", initial_capital=100000, limits=PortfolioLimits(allow_short=True))
pos = portfolio.open_position("INFY", 100, 100)
manager = StopManager(portfolio)

# Add 2% SL and 5% TP as OCO
sl = manager.add_stop_loss(pos, stop_type="percentage", params={"pct":0.02, "oco_group":"exit1"})
tp = manager.add_take_profit(pos, target_type="percentage", params={"pct":0.05, "oco_group":"exit1"})
print(f"SL @ {sl.price} TP @ {tp.price} OCO group {sl.oco_group}")

# Price drops to 97 – SL should trigger, TP cancelled via OCO
hits = manager.check_stops({"INFY": {"close":97,"low":97,"high":101}})
print(f"Hits when price 97: {len(hits)} type={hits[0].stop_type if hits else 'none'}")
print(f"Active stops after SL hit: {len(manager.get_active_stops('INFY'))}")  # 0 due to OCO

# Trailing test
portfolio2 = Portfolio(name="TrailingTest", initial_capital=100000)
pos2 = portfolio2.open_position("INFY", 100, 100)
manager2 = StopManager(portfolio2)
trailing = manager2.add_stop_loss(pos2, stop_type="trailing_fixed", params={"trailing_amount":2})
print(f"Trailing initial @ {trailing.price}")  # 98
updated = manager2.update_trailing_stops({"INFY":105})
print(f"After price 105, trailing -> {trailing.price}")  # 103
updated2 = manager2.update_trailing_stops({"INFY":102})
print(f"After price down to 102, trailing stays @ {trailing.price} (ratchet one-way)")  # still 103
PY
```

**✅ Pass criteria:** SL 98, TP 105, OCO cancels other when one triggers, trailing moves up only (98→103) and stays.

---

### Step 9 – Test Performance & Trade Analyzer (Write to DB)

```powershell
python - << 'PY'
from backtest.db.config import DatabaseConfig
from backtest.db.manager import DatabaseManager
from backtest.db.models import Base
from backtest.simulator.portfolio import Portfolio
from backtest.simulator.performance import PerformanceCalculator
from backtest.simulator.trade_analyzer import TradeAnalyzer

cfg = DatabaseConfig(url="postgresql+psycopg2://postgres:your_password@localhost:5432/forward_test")
db = DatabaseManager(cfg)
db.connect()
Base.metadata.create_all(db.engine)

portfolio = Portfolio(name="PerfTest", initial_capital=100000)
# Simulate some equity history
for eq in [100000, 101000, 100500, 102000]:
    portfolio.current_cash = eq
    portfolio.record_equity()

# Simulate closed trades
pos = portfolio.open_position("INFY", 100, 100)
portfolio.reduce_position("INFY", 100, 110)

calc = PerformanceCalculator(portfolio=portfolio, db_manager=db)
metrics = calc.calculate_all_metrics()
print(f"Total return %: {metrics['total_return_pct']*100:.2f}%")
print(f"Sharpe: {metrics['sharpe_ratio']:.2f}")
print(f"Max DD %: {metrics['max_drawdown_pct']*100:.2f}%")
print(f"Trades: {metrics['total_trades']}")

# Save to DB
metric_id = calc.save_to_db()
print(f"Metrics saved to DB id={metric_id}")

# Trade analyzer
analyzer = TradeAnalyzer(portfolio=portfolio)
report = analyzer.generate_trade_report()
print(f"Trade report: {report['total_trades']} trades, win rate {report['win_rate']*100:.1f}%")

# Export
import tempfile
from pathlib import Path
tmp = Path(tempfile.gettempdir()) / "trades_test.csv"
path = analyzer.export_trades(format="csv", file_path=tmp)
print(f"Trades exported to {path}")

db.disconnect()
PY
```

**Verify in psql:**
```powershell
psql -U postgres -h localhost -d forward_test -c "SELECT portfolio_id, calculation_date, total_trades, win_rate, sharpe_ratio FROM performance_metrics ORDER BY calculated_at DESC LIMIT 1;"
# Expected: 1 row, total_trades 1, win_rate 1.0, sharpe maybe 0
```

**✅ Pass criteria:** Metrics calculated, saved to DB, trade report shows 1 trade, CSV exported.

---

### Step 10 – Test Real Broker API (mStock) + Telegram Alerts

**Requires `.env` with real credentials – see Windows Quick-Start Guide Step 5.**

```powershell
# Test 1: mStock historical fetch (no trading, just data)
python - << 'PY'
from backtest.live.mstock import MStockSource

source = MStockSource()
try:
    df = source.get_candles("INFY", "2024-01-01", "2024-01-05", interval="day")
    print(f"Fetched {len(df)} bars for INFY")
    print(df.head())
    print("mStock connection ✅")
except Exception as e:
    print(f"mStock failed (check .env): {e}")
PY

# Expected: 5 daily bars with open/high/low/close/volume, no errors

# Test 2: Telegram alert (preferred)
python - << 'PY'
from backtest.alerts.manager import AlertManager

manager = AlertManager(config={"min_level":"info","channels":{"log":{"enabled":True},"telegram":{"enabled":True}}})
# Will try Telegram if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env, else just log
record = manager.send_alert(level="info", message="Test from Windows manual checklist", channels=["log","telegram"])
print(f"Alert sent via: {record.success_channels} failed: {record.failed_channels}")
print(f"Message: {record.message}")

# Test convenience methods
record2 = manager.alert_on_trade(type('obj', (), {"symbol":"INFY","side":"BUY","quantity":100,"fill_price":1500,"realized_pnl":500,"reason":"Manual test"})())
print(f"Trade alert: {record2.message}")

# Daily summary
record3 = manager.send_daily_summary(equity=105000, pnl=5000, trades=10, win_rate=60.0)
print(f"Daily summary: {record3.message}")
PY

# Expected: If Telegram configured, you get message on your phone instantly
# If not configured, log channel succeeds and shows "telegram not configured" in failed_channels – still passes
```

**Verify Telegram on phone:** You should receive `🔔 Trade executed: INFY BUY 100 @ 1500 | PnL: 500 | Manual test` or similar.

**✅ Pass criteria:** mStock returns DataFrame with 5 rows, Telegram message arrives on phone (or log shows success if Telegram not configured).

---

### Step 11 – Full Engine End-to-End (Dry-Run)

```powershell
# Dry-run – signals but no real orders – safest final check
$env:PYTHONPATH="src"
python -m backtest.forward.engine --config config/forward_testing.yaml --dry-run --symbols INFY --backtest

# Check state file created
dir state/
cat state/forward_test_state.json | Select-Object -First 20

# Check logs
cat logs/alerts.log | Select-Object -Last 20
```

**Verify in psql (portfolio + signals):**
```powershell
psql -U postgres -h localhost -d forward_test -c "SELECT name, status, current_cash FROM portfolios WHERE name='Forward Test 1';"
psql -U postgres -h localhost -d forward_test -c "SELECT symbol, signal_type, direction, executed, skip_reason FROM strategy_signals ORDER BY generated_at DESC LIMIT 5;"
# Expected: signals with executed=false, skip_reason dry_run or hold, bar_ts < generated_at (no lookahead)
```

**✅ Pass criteria:** Engine starts, heartbeat logs every 60s, state file saved, no crash, signals in DB with `bar_ts < generated_at`.

---

### Step 12 – Dashboard Verification

```powershell
$env:PYTHONPATH="src"
python -m backtest.dashboard.app --port 5000

# Open browser: http://localhost:5000
```

**Checklist in browser:**
- [ ] Portfolio Overview shows equity ₹100k+, cash, pos value, today P&L, total P&L with green/red
- [ ] Key Metrics shows trades today, win rate, Sharpe, max DD, exposure
- [ ] System Status shows Market Data Connected, Strategy active, Health healthy, Loops count, Last Update timestamp
- [ ] Equity Curve line chart updates
- [ ] Daily P&L bar chart green/red
- [ ] Drawdown chart
- [ ] Win/Loss pie
- [ ] Open Positions table shows INFY, TCS with qty, entry, current, unreal P&L, age, Close button
- [ ] Recent Trades table shows last 20 with green/red
- [ ] Active Orders table with Cancel button
- [ ] Manual Order Entry form works – enter INFY BUY 10 MARKET → Submit → shows in Active Orders
- [ ] Toggle Theme (dark/light) works
- [ ] Auto-refresh every 5s (check timestamp updates)

**✅ Pass criteria:** All sections load, charts render, buttons work, no JS console errors.

---

## Final Verification Queries (Copy-Paste for psql)

```powershell
$env:PGPASSWORD="your_password"
psql -U postgres -h localhost -d forward_test -f db/verify_schema.sql

psql -U postgres -h localhost -d forward_test -c "
SELECT 'portfolios' AS tbl, count(*) FROM portfolios
UNION ALL SELECT 'positions', count(*) FROM positions
UNION ALL SELECT 'orders', count(*) FROM orders
UNION ALL SELECT 'fills', count(*) FROM fills
UNION ALL SELECT 'trades', count(*) FROM trades
UNION ALL SELECT 'equity_curve', count(*) FROM equity_curve
UNION ALL SELECT 'strategy_signals', count(*) FROM strategy_signals
UNION ALL SELECT 'performance_metrics', count(*) FROM performance_metrics;
"

psql -U postgres -h localhost -d forward_test -c "
-- Lookahead bias check – should return 0 rows (PASS)
SELECT count(*) AS biased_signals_count,
       CASE WHEN count(*)=0 THEN 'PASS – No lookahead bias' ELSE 'FAIL – Bias detected!' END AS result
FROM strategy_signals
WHERE bar_ts >= generated_at;
"

psql -U postgres -h localhost -d forward_test -c "
-- Recent signals audit
SELECT symbol, signal_type, direction, strength, target_position, executed, skip_reason, generated_at, bar_ts
FROM strategy_signals
ORDER BY generated_at DESC LIMIT 10;
"
```

**All queries should show PASS and reasonable counts.**

---

## Success Criteria – You’re Done When:

- [ ] `verify_schema.sql` shows all PASS (11 tables, 2 views, 14 FKs, >=46 indexes)
- [ ] Portfolio created and saved to Postgres, loaded back correctly
- [ ] Market data handler normalizes mock ticks and aggregates bars correctly (1min boundary)
- [ ] Validator rejects corrupted OHLC and detects spikes
- [ ] Time manager correctly identifies NSE market open/closed and aligns bars
- [ ] Strategy adapter generates signals with `bar_ts < generated_at` (no lookahead) and creates orders
- [ ] Position sizing returns expected qty for all 6 methods and respects constraints
- [ ] Risk manager rejects restricted symbols and halts on drawdown, override works
- [ ] Stop manager triggers SL at 97 (2% below 100) and trailing ratchets one-way
- [ ] Performance calculator calculates total return, Sharpe, max DD, saves to DB
- [ ] Trade analyzer categorizes by symbol/exit reason/holding and calculates MAE/MFE
- [ ] mStock live fetch returns DataFrame (requires .env) and Telegram alert arrives on phone
- [ ] Engine dry-run starts, heartbeats, saves state JSON, signals in DB
- [ ] Dashboard loads at localhost:5000 with all 7 sections and charts

If all checked, your Windows + PostgreSQL + Real Broker API setup is **validated** and ready for live paper trading.

---

## Appendix: .env.example Reference

See `.env.example` for full template. Key sections:

- `MSTOCK_API_KEY`, `MSTOCK_USERNAME`, `MSTOCK_PASSWORD`, `MSTOCK_CHECKSUM`, `MSTOCK_AUTH_MODE`
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (preferred), `SLACK_WEBHOOK_URL`, `DISCORD_WEBHOOK_URL`
- `FORWARD_TEST_DB_URL` – `postgresql+psycopg2://user:pass@localhost:5432/forward_test`
- `FORWARD_TEST_DB_PROFILE` – development/testing/production

All secrets in `.env` (gitignored), never committed. For GitHub Actions, use Secrets.

