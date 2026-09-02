# Instructions Index

This folder contains design documents, reference cards, and project management files for the backtest system.

## Remaining reference cards

These cards are still useful as **reference material** for the built system:

| # | Card | Purpose |
|---|------|---------|
| 00 | `00-INVARIANTS.md` | The non-negotiable behavioral contract (no-lookahead, costs, SL/TP) |
| 06 | `06-ACCEPTANCE-TESTS.md` | The 22 acceptance tests that pin behavior |
| 07 | `07-LIVE-BRINGUP.md` | mStock live bring-up checklist (network egress gate) |
| 08 | `08-ALLOWED-BUILDING-BLOCKS.md` | Allowed pandas/numpy primitives for strategies |

> Cards 00 and 08 remain authoritative — the invariants and allowed primitives
> are still the contract for any new strategy or module.

## Archived cards (moved to `docs/archive/instructions/`)

Cards 01–05 were **rebuild-from-scratch guides** written before the system was
built. Since the system is complete, they are historical:

| Card | Was | Status |
|------|-----|--------|
| 01 | Skeleton, env, packaging | ✅ Built — system is pip-installable |
| 02 | Data layer (synthetic, CSV, mStock) | ✅ Built — plus DbSource, source_registry |
| 03 | Strategy plugin system + 4 built-ins | ✅ Built — now 5 strategies |
| 04 | Engine, metrics, plotting, runner, CLI | ✅ Built — plus backtest_driver, engine_loop |
| 05 | Forward testing / paper trading | ✅ Built — plus portfolio manager, risk supervisor |

## Other documents in this folder

| File | Purpose |
|------|---------|
| `README.md` | This index file |
| `ROADMAP.md` | Phased plan (Phase 0→6) |
| `BACKLOG.md` | Tactical enhancement items |
| `ARCHITECTURE-BLUEPRINT.md` | Historical architecture map (stale body, retained as reference) |
| `ENGINEERING-NOTES.md` | Debugging playbook — symptom→cause→where-to-look |
| `REFACTOR-PORTFOLIO-LIVE-PAPER-SEPARATION.md` | Live/Paper separation design + completion status |

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

## Current System Status (as of 2026-09-02)

### Completed Modules
| Module | Status | Tests |
|--------|--------|-------|
| **Backtest engine** | ✅ Done | Vectorized engine with SL/TP, trailing stop, time exit |
| **Strategy system** | ✅ Done | Pluggable plugin system, 5 built-in strategies |
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
