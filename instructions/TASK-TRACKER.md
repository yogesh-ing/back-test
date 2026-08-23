# Forward Testing Simulator — Task Tracker

Tracks progress against `instructions/forword-testing.md` (24 steps, 8 phases).

> **Debugging?** See `instructions/ENGINEERING-NOTES.md` — symptom→cause playbook,
> conventions and invariants, and every bug found so far with its root cause.

**Last updated:** 2026-08-23 · **Branch:** `arena/01a02caa-back-test` · **Steps 10–17, 20 complete (Phase 4, 5, 6, 7 partial, 8 done)**

---

## Progress

`██████████████████████████████████░░` **18 / 24 steps complete** (75%)

| Phase | Steps | Status |
|---|---|---|
| 1 · Database | 1–2 | ✅ **Complete** |
| 2 · Core models | 3–6 | ✅ **Complete** |
| 3 · Execution | 7–9 | ✅ **Complete** |
| 4 · Live data | 10–12 | ✅ **Complete** |
| 5 · Strategy | 13–14 | ✅ **Complete** |
| 6 · Risk | 15–16 | ✅ **Complete** |
| 7 · Performance | 17–19 | 🟡 **In progress** (17 ✅, 18–19 ⬜) |
| 8 · Orchestration | 20 | ✅ **Complete** |
| Bonus | 21–24 | ⬜ Not started |

Build order follows the plan's own recommendation: **1–6 → 10–12 → 7–9 → 20 → rest**.

---

## Steps

Legend: ✅ done · 🟡 in progress · ⬜ not started · ⏭️ deferred

### Phase 1 — Database Design & Setup

| # | Step | Status | Deliverables | Tests |
|---|---|---|---|---|
| 1 | Database Schema Design | ✅ | `db/migrations/001_initial_schema.sql`, `.sqlite.sql`, rollback, `db/verify_schema.sql`, `src/backtest/
├── db/                  # Steps 1–2 ✅
│   ├── models.py        #   ORM: 10 tables
│   ├── manager.py       #   DatabaseManager: pooling, retries, transactions
│   └── config.py        #   Layered config
├── simulator/           # Steps 3–9, 14–15 ✅ (domain models)
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
│   ├── position_sizing.py # PositionSizer, 6 methods, constraints ✅ Step 14
│   ├── risk_manager.py  # RiskManager, RiskConfig, circuit breakers ✅ Step 15
│   ├── stop_manager.py  # StopManager, StopType, OCO, trailing ✅ Step 16
│   └── performance.py   # PerformanceCalculator, return/risk/ratios ✅ Step 17
├── live/                # Steps 10–12 ✅ (live data)
│   ├── market_data_handler.py # MarketDataHandler, BarBuilder, BrokerFeed ✅ Step 10
│   ├── data_validator.py      # DataValidator, ValidationResult ✅ Step 11
│   ├── time_manager.py        # TimeManager, MarketHours ✅ Step 12
│   ├── mstock.py        #   MStockSource (existing, wired) ✅
│   └── auth.py          #   Auth (existing)
├── forward/             # Steps 13–14, 20 ✅ (orchestration)
│   ├── strategy_adapter.py  # StrategyAdapter, Signal, sizers ✅ Step 13
│   ├── engine.py            # ForwardTestingEngine, StateManager ✅ Step 20
│   ├── broker.py        #   SimulatedBroker (legacy)
│   ├── paper.py         #   Walk-forward + live papertrade
│   └── portfolio.py     #   Legacy multi-strategy allocator
├── strategy/            # Strategy abstraction + adapter re-export
│   ├── base.py          #   Strategy base (existing, reused) ✅
│   ├── adapter.py       #   Re-export of forward + simulator sizers ✅ Steps 13–14
│   └── registry.py      #   Strategy registry
├── config/
│   ├── forward_testing.yaml # Main engine config ✅ Step 20
│   ├── position_sizing.yaml # 8 profiles for sizing ✅ Step 14
│   ├── risk.yaml            # Risk limits, 5 profiles ✅ Step 15
│   ├── stops.yaml           # Stop loss & TP, 7 profiles ✅ Step 16
│   ├── performance.yaml     # Performance calc, risk-free, VaR ✅ Step 17
│   ├── market_data.yaml     # Market data handler config ✅ Step 10
│   ├── data_quality.yaml    # Data validator config ✅ Step 11
│   ├── time_sync.yaml       # Time manager config ✅ Step 12
│   ├── slippage.yaml
│   ├── execution.yaml
│   ├── brokers.yaml
│   └── database.yaml
├── docs/
│   └── LOCAL-TESTING-MANUAL.md # Local testing guide ✅ Phase 4
├── Dockerfile           # Container setup ✅ Step 20
├── forward_testing.service # systemd service ✅ Step 20
│
├── data/ engine/ strategies/   # pre-existing
```

**Layering rule:** `simulator/` holds pure in-memory domain logic and must not
import from `engine/` or `forward/`. It talks to the database only through
`db.DatabaseManager`, so every model stays unit-testable with no I/O.
`forward/` and `strategy/` may import from `simulator/` — the adapter bridges
`strategy/base.py` (existing) into `simulator.Portfolio`/`OrderExecutor`.

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
| `test_strategy_adapter.py` (Step 13) | 20 |
| `test_simulator_position_sizing.py` (Step 14) | 25 |
| `test_forward_engine.py` (Step 20) | 14 |
| `test_market_data_handler.py` (Step 10) | 18 |
| `test_data_validator.py` (Step 11) | 18 |
| `test_time_manager.py` (Step 12) | 18 |
| `test_simulator_risk_manager.py` (Step 15) | 24 |
| `test_simulator_stop_manager.py` (Step 16) | 21 |
| `test_simulator_performance.py` (Step 17) | 14 |
| **Total** | **1085 passing, 4 skipped** |

