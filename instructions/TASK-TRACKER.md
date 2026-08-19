# Forward Testing Simulator — Task Tracker

Tracks progress against `instructions/forword-testing.md` (24 steps, 8 phases).

**Last updated:** 2026-08-19 · **Branch:** `arena/01a01ae2-back-test` · **PR:** [#3](https://github.com/yogesh-ing/back-test/pull/3)

---

## Progress

`█████████░░░░░░░░░░░░░░░` **4 / 24 steps complete** (17%)

| Phase | Steps | Status |
|---|---|---|
| 1 · Database | 1–2 | ✅ **Complete** |
| 2 · Core models | 3–6 | 🟡 In progress (2/4) |
| 3 · Execution | 7–9 | ⬜ Not started |
| 4 · Live data | 10–12 | ⬜ Not started |
| 5 · Strategy | 13–14 | ⬜ Not started |
| 6 · Risk | 15–16 | ⬜ Not started |
| 7 · Performance | 17–19 | ⬜ Not started |
| 8 · Orchestration | 20 | ⬜ Not started |
| Bonus | 21–24 | ⬜ Not started |

Build order follows the plan's own recommendation: **1–6 → 10–12 → 7–9 → 20 → rest**.

---

## Steps

Legend: ✅ done · 🟡 in progress · ⬜ not started · ⏭️ deferred

### Phase 1 — Database Design & Setup

| # | Step | Status | Deliverables | Tests |
|---|---|---|---|---|
| 1 | Database Schema Design | ✅ | `db/migrations/001_initial_schema.sql`, `.sqlite.sql`, rollback, `db/verify_schema.sql`, `src/backtest/db/models.py`, `db/alembic/`, `db/DB-IMPLEMENTATION-GUIDE.md` | 44 |
| 2 | Database Connection Manager | ✅ | `src/backtest/db/manager.py`, `src/backtest/db/config.py`, `config/database.yaml`, `db/CONNECTION-MANAGER.md` | 107 |

### Phase 2 — Core Data Models

| # | Step | Status | Deliverables | Tests |
|---|---|---|---|---|
| 3 | Portfolio Model | ✅ | `src/backtest/simulator/portfolio.py`, `position.py` (base), `errors.py`, `money.py` | 130 |
| 4 | Position Model | ✅ | `src/backtest/simulator/lots.py` (LotBook), FIFO/LIFO/average, splits, dividends, `save_to_db` | 77 |
| 5 | Order Model | ⬜ | `src/backtest/simulator/order.py` — lifecycle state machine, 5 order types | — |
| 6 | Fill Model | ⬜ | `src/backtest/simulator/fill.py` — immutable executions, commission models | — |

### Phase 3 — Order Execution Simulation

| # | Step | Status | Deliverables |
|---|---|---|---|
| 7 | Slippage Model | ⬜ | 5 slippage models, `SlippageCalculator` |
| 8 | Commission Calculator | ⬜ | Broker fee models — **adapt to NSE**: STT, stamp duty, SEBI, GST |
| 9 | Order Execution Simulator | ⬜ | `OrderExecutor`, latency, partial fills, rejections |

### Phase 4 — Live Data Integration

| # | Step | Status | Deliverables |
|---|---|---|---|
| 10 | Market Data Handler | ⬜ | Normalisation, bar aggregation — **wire to existing `live/mstock.py`** |
| 11 | Data Quality Validator | ⬜ | Spike/gap detection, OHLC sanity |
| 12 | Time Synchronization Manager | ⬜ | **NSE calendar** (plan says NYSE), IST/UTC, bar alignment |

### Phase 5 — Strategy Integration

| # | Step | Status | Deliverables |
|---|---|---|---|
| 13 | Strategy Adapter | ⬜ | Bridge to existing `strategy/base.py` — **do not duplicate it** |
| 14 | Position Sizing Engine | ⬜ | Fixed / %-of-portfolio / risk-based / ATR / Kelly |

### Phase 6 — Risk Management

| # | Step | Status | Deliverables |
|---|---|---|---|
| 15 | Risk Manager | ⬜ | Pre-trade checks, circuit breakers |
| 16 | Stop Loss & Take Profit Manager | ⬜ | Trailing stops, OCO — **reconcile with `forward/broker.py`** |

### Phase 7 — Performance Tracking

| # | Step | Status | Deliverables |
|---|---|---|---|
| 17 | Performance Calculator | ⬜ | Reuse `engine/metrics.py` where possible |
| 18 | Trade Analyzer | ⬜ | Attribution by exit reason, MAE/MFE |
| 19 | Real-Time Dashboard | ⬜ | Web UI |

### Phase 8 — System Orchestration

| # | Step | Status | Deliverables |
|---|---|---|---|
| 20 | Main Forward Testing Engine | ⬜ | Event loop, state persistence, lifecycle hooks |

### Bonus

| # | Step | Status |
|---|---|---|
| 21 | Alert & Notification System | ⬜ |
| 22 | Backtesting Comparison Tool | ⬜ |
| 23 | Configuration Manager | ⬜ |
| 24 | Testing & CI/CD Setup | ⬜ |

---

## Manual actions required from you

Things the agent cannot do; tracked so nothing is silently skipped.

| # | Action | From | Status |
|---|---|---|---|
| 1 | Apply `db/migrations/001_initial_schema.sql` to your PostgreSQL | Step 1 | ⬜ Pending |
| 2 | Run `db/verify_schema.sql`, confirm all PASS | Step 1 | ⬜ Pending |
| 3 | Set `FORWARD_TEST_DB_URL` in `.env` (never commit it) | Step 1/2 | ⬜ Pending |
| 4 | `pip install -r requirements.txt` (adds SQLAlchemy, Alembic, psycopg2, PyYAML) | Step 1/2 | ⬜ Pending |
| 5 | Review & merge PR #3 | — | ⬜ Pending |

---

## Deviations from the plan document

Recorded so the divergence is deliberate and reviewable, not accidental drift.

| # | Plan says | We do | Why |
|---|---|---|---|
| 1 | SEC / FINRA TAF fees (US) | Generic `exchange_fees` / `regulatory_fees`, INR default | Repo trades NSE via mStock |
| 2 | Column named `timestamp` | Column named `ts` | `timestamp` is a SQL type name; forces quoting, confuses ORM reflection |
| 3 | NYSE calendar, 9:30–16:00 ET | NSE calendar, IST | Same reason as 1 |
| 4 | Broker: Alpaca / IBKR | mStock (already implemented in `live/`) | Existing code |
| 5 | New Strategy base class (Step 13) | Adapt existing `strategy/base.py` | Avoid duplicating a working abstraction |
| 6 | Native SQL `ENUM` types | `VARCHAR` + `CHECK` | Portability to SQLite, required by Step 2 |
| 7 | — | New `simulator/` package for Steps 3–6 | Avoids collision with existing `forward.portfolio.Portfolio` and ORM `db.models.Portfolio` |

---

## Known limitations

Deliberate gaps, recorded so they are not mistaken for oversights.

| # | Limitation | From | Impact |
|---|---|---|---|
| 1 | Tax lots are **not** persisted — the schema has no `lots` table. A FIFO position reloaded from the database collapses to one lot at the stored average price. | Step 4 | Restart mid-run loses FIFO granularity. Workaround: the `to_dict()`/`from_dict()` JSON snapshot **is** lossless; the Step 20 state manager should use it. Revisit if per-lot tax reporting is needed. |
| 2 | `Portfolio.pending_orders` / `filled_orders` are untyped placeholders. | Step 3 | Populated in Step 5 when the Order model lands. |

---

## Architecture as built

```
src/backtest/
├── db/                  # Steps 1–2 ✅
│   ├── models.py        #   ORM: 10 tables
│   ├── manager.py       #   DatabaseManager: pooling, retries, transactions
│   └── config.py        #   Layered config
├── simulator/           # Steps 3–6 🟡  (forward testing domain models)
│   ├── money.py         #   Decimal helpers  ✅
│   ├── errors.py        #   Domain exceptions ✅
│   ├── lots.py          #   LotBook, FIFO/LIFO ✅ Step 4
│   ├── position.py      #   Position         ✅ Steps 3+4
│   ├── portfolio.py     #   Portfolio        ✅
│   ├── order.py         #   Order            ⬜ Step 5
│   └── fill.py          #   Fill             ⬜ Step 6
│
├── data/ engine/ forward/ live/ strategies/ strategy/   # pre-existing
```

**Layering rule:** `simulator/` holds pure in-memory domain logic and must not
import from `engine/` or `forward/`. It talks to the database only through
`db.DatabaseManager`, so every model stays unit-testable with no I/O.

---

## Test counts

| Suite | Tests |
|---|---|
| Pre-existing (backtest engine, mStock) | 25 (+4 skipped — need credentials) |
| `test_db_schema.py` (Step 1) | 44 |
| `test_db_manager.py` (Step 2) | 107 |
| `test_simulator_portfolio.py` (Step 3) | 130 |
| `test_simulator_position.py` (Step 4) | 77 |
| **Total** | **383 passing, 4 skipped** |
