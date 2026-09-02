# Rebuild Instruction Cards — index

A model can rebuild the entire `backtest` system (backtesting + comparison +
forward testing) from these cards **without copying source**. Each card is
self-contained; build them **in order** and pass the checks before moving on.

## Read this first
- **Language:** Python ≥ 3.10. **Run convention:** `PYTHONPATH=src`, entry
  `python -m backtest <cmd>`.
- **Golden rules (apply to every card):**
  1. Keep the invariants in **Card 00** exact — they are safety-critical
     (this system trades against a live market).
  2. After each card, run its **Verify** step. Do not proceed on failure.
  3. **Never weaken a test** to make it pass — fix the code.
  4. Only code strategies/new files as specified; don't alter shared infra to
     change results.

## Cards (build order)
| # | Card | Builds |
|---|------|--------|
| 00 | `00-INVARIANTS.md` | The non-negotiable behavioral contract |
| 01 | `01-SKELETON-ENV.md` | File tree, deps, packaging, `.env`, run setup |
| 02 | `02-DATA-LAYER.md` | `data/`: base contract, synthetic, csv, mStock |
| 03 | `03-STRATEGY-SYSTEM.md` | `strategy/` plug-in system + 4 built-ins |
| 04 | `04-ENGINE-CLI.md` | engine, metrics, plotting, runner, CLI |
| 05 | `05-FORWARD-TESTING.md` | `forward/` paper trading + portfolio management |
| 06 | `06-ACCEPTANCE-TESTS.md` | The 22 tests + final gate (pins behavior) |
| 07 | `07-LIVE-BRINGUP.md` | mStock live bring-up + build-order checklist |
| 08 | `08-ALLOWED-BUILDING-BLOCKS.md` | Allowed APIs/primitives (curb hallucinations) |

## Dependency flow
`00 → 01 → 02 → 03 → 04` gives a working **backtest + compare** on synthetic
data (verify with Card 06 tests 1–19). Then `05` adds **forward testing**
(tests 20–22). `07` is live-market bring-up, only where market access exists.

> Cards 00 and 06 are the two you must not cut corners on: the invariants and
> the acceptance tests are what make a rebuilt system trustworthy for live use.
> Card 08 keeps lower-capability models from inventing APIs; keep it in context
> whenever authoring strategies or new modules.

## Current System Status (as of 2026-09-02)

### Completed Modules
| Module | Status | Tests |
|--------|--------|-------|
| **Backtest engine** | ✅ Done | Vectorized engine with SL/TP, trailing stop, time exit |
| **Strategy system** | ✅ Done | Pluggable plugin system, 6 built-in strategies |
| **Data layer** | ✅ Done | Synthetic, CSV, mStock (read-only), source tags |
| **Compare command** | ✅ Done | Multi-strategy ranking, equity overlay chart |
| **Forward testing** | ✅ Done | Event-driven runner, SimulatedBroker, state persistence |
| **Portfolio manager** | ✅ Done | Multi-strategy orchestration, per-strategy capital allocation |
| **Risk supervisor** | ✅ Done | Circuit breakers, daily loss limits, kill switch |
| **Bucket separation** | ✅ Done | Live/Paper isolation — independent breakers, scoped controls |
| **Web dashboard** | ✅ Done | Flask UI — backtest, forward test, portfolio (overview/live/paper) |
| **REST + SSE API** | ✅ Done | Full API for all operations, real-time streaming |
| **Authentication** | ✅ Done | Flask-Login, bcrypt, session management |
| **mStock client** | 🔶 Partial | Auth + history work; live execution blocked on network |

### Portfolio Separation (New — 2026-09-02)
The Portfolio Command Center now has **three views**:
- **Overview** (`/portfolio`) — Live prominent, Paper secondary with sandbox framing
- **Live** (`/portfolio/live`) — Scoped to live runners only, capability-driven banner
- **Paper** (`/portfolio/paper`) — Scoped to paper runners only, sandbox framing

Key features:
- Per-bucket state: equity, peak, drawdown, daily P&L derived from runners (not duplicated)
- Independent circuit breakers — paper breach does NOT halt live
- Scoped bulk control — pause/resume/emergency scoped to target bucket
- Master kill — emergency flatten both buckets at once
- Capability flag — REAL MONEY banner driven by broker connection status
- SSE carries embedded bucket data — single stream, frontend filters

See `REFACTOR-PORTFOLIO-LIVE-PAPER-SEPARATION.md` for full details.

### Test Coverage
- **1,700+ tests** passing across the full suite
- **116 portfolio-specific tests** covering:
  - Per-bucket state tracking (23)
  - Flow semantics + capability (11)
  - Breaker independence (27)
  - API endpoints (25)
  - UI views (30)
