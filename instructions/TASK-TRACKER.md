# Forward Testing Simulator — Task Tracker

Tracks progress against `instructions/forword-testing.md` (24 steps, 8 phases).

> **Debugging?** See `instructions/ENGINEERING-NOTES.md` — symptom→cause playbook,
> conventions and invariants, and every bug found so far with its root cause.

**Last updated:** 2026-08-23 · **Branch:** `arena/01a02c7a-back-test` · **PR [#3](https://github.com/yogesh-ing/back-test/pull/3) merged** (Steps 1–7) · **PR [#5](https://github.com/yogesh-ing/back-test/pull/5) merged** into `main` (Steps 8–9, pushed manually by the user)

---

## Progress

`████████████████████████` **12 / 24 steps complete** (50%)

| Phase | Steps | Status |
|---|---|---|
| 1 · Database | 1–2 | ✅ **Complete** |
| 2 · Core models | 3–6 | ✅ **Complete** |
| 3 · Execution | 7–9 | ✅ **Complete** |
| 4 · Live data | 10–12 | ✅ **Complete** |
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
| 5 | Order Model | ✅ | `src/backtest/simulator/order.py`, `enums.py` — state machine, 5 order types, triggers, callbacks | 115 |
| 6 | Fill Model | ✅ | `src/backtest/simulator/fill.py`, `commission.py` — immutable fills, 5 commission models, `Portfolio.apply_fill` | 106 |

### Phase 3 — Order Execution Simulation

| # | Step | Status | Deliverables |
|---|---|---|---|
| 7 | Slippage Model | ✅ | `src/backtest/simulator/slippage.py`, `config/slippage.yaml` — 5 models, 4 profiles, tiers, time-of-day · **101 tests** |
| 8 | Commission Calculator | ✅ | `src/backtest/simulator/fees.py`, `config/brokers.yaml` — NSE + US fee stacks, 10 broker presets, monthly volume tiers, FX · **109 tests** |
| 9 | Order Execution Simulator | ✅ | `src/backtest/simulator/execution.py`, `config/execution.yaml` — liquidity caps, queue position, latency, rejections · **99 tests** |

### Phase 4 — Live Data Integration

| # | Step | Status | Deliverables |
|---|---|---|---|
| 10 | Market Data Handler | ✅ | `src/backtest/marketdata/` (`ticks.py` normalization, `bars.py` NSE-anchored aggregation, `feed.py` mStock + mock feeds, `handler.py` hub), `config/marketdata.yaml` — wired to existing `live/mstock.py` · **173 tests** |
| 11 | Data Quality Validator | ✅ | `src/backtest/marketdata/quality.py`, `config/quality.yaml` — z-score spikes, gap/volume anomalies, 3 strictness levels, reject/repair, alerts + regime reset, handler integration · **114 tests** |
| 12 | Time Synchronization Manager | ✅ | `src/backtest/marketdata/timesync.py`, `config/calendar.yaml` — NSE sessions (pre-open/continuous/closing) in IST, NSE 2025–26 + NYSE 2026 holidays, next open/close, trading-day ranges, NTP sync, latency tracking · **96 tests** |

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
| 5 | Review & merge PR #3 | — | ✅ Merged 2026-08-19 (`4e01d65`) |
| 6 | Review & merge the Phase 3 PR (Steps 8–9) | — | ✅ Merged 2026-08-23 as PR [#5](https://github.com/yogesh-ing/back-test/pull/5) (`43abb57`) — user pushed & merged manually |

---

## Deviations from the plan document

Recorded so the divergence is deliberate and reviewable, not accidental drift.

| # | Plan says | We do | Why |
|---|---|---|---|
| 1 | SEC / FINRA TAF fees (US) | **Both** regimes implemented (`IndiaEquityFees` default, `USEquityFees` available) | Repo trades NSE via mStock, but the plan names US fees |
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
| 2 | `Order.status_history`, `triggered` and `extreme_price` are not persisted — no columns in `orders`. | Step 5 | Survives in the `to_dict()` JSON snapshot; Step 20 should use it. |
| 3 | Special trading sessions (Muhurat, half-days, ad-hoc extensions) are not modelled — `config/calendar.yaml` only knows full trading days and full holidays. | Step 12 | 2026-11-08 Muhurat session will show as closed. Acceptable for daily-bar strategies; revisit for intraday live trading. |
| 4 | Holiday calendars require **annual maintenance** — NSE/NYSE publish next year's list each December; add it to `config/calendar.yaml`. | Step 12 | A missing year makes every weekday look like a trading day. |

---

## Architecture as built

```
src/backtest/
├── db/                  # Steps 1–2 ✅
│   ├── models.py        #   ORM: 10 tables
│   ├── manager.py       #   DatabaseManager: pooling, retries, transactions
│   └── config.py        #   Layered config
├── simulator/           # Steps 3–9 ✅  (forward testing domain models)
│   ├── money.py         #   Decimal helpers  ✅
│   ├── errors.py        #   Domain exceptions ✅
│   ├── lots.py          #   LotBook, FIFO/LIFO ✅ Step 4
│   ├── position.py      #   Position         ✅ Steps 3+4
│   ├── portfolio.py     #   Portfolio        ✅
│   ├── enums.py         #   Order enums + FSM  ✅ Step 5
│   ├── order.py         #   Order            ✅ Step 5
│   ├── commission.py    #   5 fee models       ✅ Step 6
│   ├── fill.py          #   Fill (immutable)   ✅ Step 6
│   ├── slippage.py      #   5 slippage models  ✅ Step 7
│   ├── fees.py          #   Full fee stack     ✅ Step 8
│   ├── execution.py     #   OrderExecutor      ✅ Step 9
│
├── marketdata/          # Steps 10–12 ✅  (live data layer)
│   ├── errors.py        #   MarketDataError hierarchy
│   ├── ticks.py         #   Tick/Bar + broker payload normalization
│   ├── bars.py          #   IST boundary alignment, BarAggregator
│   ├── feed.py          #   DataFeed ABC, MStockFeed, MockFeed
│   ├── handler.py       #   MarketDataHandler: buffers, reconnect, cache
│   ├── quality.py       #   DataValidator: spikes, gaps, strictness  ✅ Step 11
│   └── timesync.py      #   TimeManager: NSE calendar, NTP, latency  ✅ Step 12
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
| `test_simulator_order.py` (Step 5) | 115 |
| `test_simulator_fill.py` (Step 6) | 106 |
| `test_simulator_slippage.py` (Step 7) | 101 |
| `test_simulator_fees.py` (Step 8) | 109 |
| `test_simulator_execution.py` (Step 9) | 99 |
| `test_marketdata.py` (Step 10) | 173 |
| `test_marketdata_quality.py` (Step 11) | 114 |
| `test_timesync.py` (Step 12) | 96 |
| **Total** | **1296 passing, 4 skipped** |

