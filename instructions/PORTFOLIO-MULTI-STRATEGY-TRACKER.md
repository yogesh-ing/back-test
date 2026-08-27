# Portfolio Command Center — Task Tracker

Tracks progress against `instructions/forword-testing-multi-stratergy.md`
(Multi-Strategy Multi-Instrument Forward Testing Engine).

**Last updated:** 2026-08-27 · **Branch:** `arena/01a043b7-back-test`

## Adaptation notes (PRD path → actual repo layout)

The repo nests code under `src/backtest/` (see `TASK-TRACKER.md` deviations).

| PRD path | Actual path |
|---|---|
| `forward/runner.py` | `src/backtest/forward/runner.py` |
| `data/universe.py` | `src/backtest/data/universe.py` |
| `forward/portfolio_manager.py` | `src/backtest/forward/portfolio_manager.py` |
| `forward/risk_supervisor.py` | `src/backtest/forward/risk_supervisor.py` |
| `forward/order_ledger.py` | `src/backtest/forward/order_ledger.py` |
| `dashboard/routes/portfolio_routes.py` | `src/backtest/api/portfolio.py` (Flask blueprint) |
| `dashboard/templates/portfolio.html` | `src/backtest/web/templates/portfolio.html` |
| `dashboard/static/js/portfolio.js` | `src/backtest/web/static/js/portfolio.js` |
| `tests/benchmark_portfolio.py` | `benchmarks/benchmark_portfolio.py` |

V1 is **paper-trading / in-memory** (no broker credentials needed): orders flow
through the OrderLedger into a `PaperBroker` that fills at bar-close price, so
the tagging/routing layer is fully exercised and testable. A synthetic feed
(`forward/feed.py`) drives live ticks for the demo; mStock live wiring is the
same V2 item already noted in the main tracker.

## Phases — ALL COMPLETE ✅ (2026-08-27)

| Phase | Focus | Tasks | Status |
|---|---|---|---|
| 1 | StrategyRunner isolated container | 1.1, 1.2 | ✅ |
| 2 | Symbol universe / pool engine | 2.1, 2.2 | ✅ |
| 3 | PortfolioManager + risk guard | 3.1, 3.2 | ✅ |
| 4 | Order tagging ledger + broker router | 4.1 | ✅ |
| 5 | REST API + SSE stream | 5.1, 5.2 | ✅ |
| 6 | Portfolio Command Center UI | 6.1, 6.2, 6.3 | ✅ |
| 7 | Benchmark + circuit breaker tests | 7.1, 7.2 | ✅ |

### Task list

- [x] 1.1 `StrategyRunner` data model + state engine (`forward/runner.py`)
- [x] 1.2 Instance PnL & order isolation (local cash/positions/trades, win rate, drawdown)
- [x] 2.1 Universe registry (`data/universe.py`): NIFTY_50, TOP_10/20_CRYPTO pools
- [x] 2.2 Multi-symbol scanning + signal ranking + max pool positions (once/tick scan)
- [x] 3.1 `PortfolioManager` lifecycle + `get_portfolio_summary()`
- [x] 3.2 `RiskSupervisor`: daily loss + max drawdown breakers, correlation warning
- [x] 4.1 `OrderLedger` client-order-id tagging + fill routing (`PRT-{instance}-{ts}-{seq}`)
- [x] 5.1 REST routes: summary, create, control, bulk control, emergency stop, deep dive, universes
- [x] 5.2 SSE `/api/portfolio/stream` (1s JSON snapshots)
- [x] 6.1 Header metric cards + global toolbar
- [x] 6.2 Dense 50+ instance matrix (live status dots, type badges, search, filter, sort, row actions)
- [x] 6.3 Deep-dive slide-over (instance equity curve, positions, signal reason log, param inspector, universe chips)
- [x] 7.1 `benchmarks/benchmark_portfolio.py` — 50 runners, 1,287 fills, 0 contamination, 130 MB RSS
- [x] 7.2 `tests/test_circuit_breakers.py` — halt < 500 ms (measured ~15 ms), flatten all

### Verification (acceptance workflow)

All 5 PRD acceptance steps verified against the live app:
1. `/portfolio` opens with an empty matrix → 2. three instances spawned (RSI/BTC/1H ₹10L,
   Swing/NIFTY50/1D ₹25L, MACD/ETH/15m ₹8L) all show 🟢 RUNNING, total capital **₹43,00,000**
   → 3. deep-dive on the pool shows **50 universe symbols**, basket positions, and its own
   equity curve → 4. `POST /api/portfolio/test/breach` flips the matrix to
   **🔴 CIRCUIT_BREAKER_HALT** and fires the banner.

### Benchmark results (Task 7.1)

```
50 runners (30 single + 20 pool), 230 symbols
steady-state tick      311 ms   (budget 1,000 ms)
warmup (30 ticks)       5.6 s
total fills           1,287  (1,200 mock + tick fills)
cross-contamination       0
RSS                   130 MB total  (2.6 MB/runner; budget 800 MB / <15 MB per instance)
```

### Tests

* `tests/test_portfolio_engine.py` — 30 tests (runner isolation, universe, pool
  ranking, manager lifecycle, supervisor rules, circuit-breaker integration)
* `tests/test_api_portfolio.py` — 17 tests (REST + SSE + page render)
* `tests/test_circuit_breakers.py` — 5 stress tests (< 500 ms halt, flatten/pause modes)
* **52 new tests, all passing.** No regressions in the pre-existing 1,730+ suite
  (the only failures/errors are the documented pre-existing baseline drift in
  `test_api_forward.py` / `test_e2e_workflow.py` and mStock-credential broker tests).

### Key implementation notes / deviations

| # | PRD says | Built | Why |
|---|---|---|---|
| 1 | Pool scan per bar event | Pool basket scan runs **once per tick** (`on_tick_end`) | Per-symbol-event rescans made an n-symbol pool O(n²) per tick; one scan per tick is O(n) and matches "evaluate on bar close" semantics. |
| 2 | mStock live broker | `PaperBroker` fills at bar-close behind the same `OrderLedger` | V1 needs no credentials; the tag→route→`on_fill` path is identical for a live gateway (swap the broker, keep `ledger.apply_fill`). |
| 3 | Daily-loss default ₹10k / DD 10% | Defaults loosened (₹250k / 25%) | The demo synthetic walk is volatile; tight defaults self-halted on warmup. Production limits are injected via `GlobalRiskConfig`; the `/test/breach` demo tightens them deterministically. |
| 4 | Order id `PRT-{instance}-{timestamp_ms}` | `PRT-{instance8}-{ts_ms}-{monotonic seq}` | Timestamp alone can collide under fast benchmark loops; the seq guarantees uniqueness. |

