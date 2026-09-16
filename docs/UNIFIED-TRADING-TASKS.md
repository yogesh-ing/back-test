# Task PRD — Unified Trading Implementation

**Architecture:** [`docs/ARCHITECTURE-UNIFIED-TRADING.md`](ARCHITECTURE-UNIFIED-TRADING.md) (signed off 2026-09-16)
**Format:** mirrors [`docs/OPTIONS-BACKTEST-TASKS.md`](OPTIONS-BACKTEST-TASKS.md)
**Status key:** ⬜ not started · 🔨 in progress · ✅ done · ⛔ blocked

---

## Summary

| Phase | Tasks | Est. | Done |
|---|---|---|---|
| P0 — Merge gate (dev branch) | U0.1–U0.2 | 1h | 0 |
| P1 — Playbook entity + API | U1.1–U1.5 | 2d | 0 |
| P2 — Execution engine | U2.1–U2.4 | 2d | 0 |
| P3 — Portfolio integration | U3.1–U3.4 | 2.5d | 0 |
| P4 — UI (Playbooks + Manual Book tabs) | U4.1–U4.3 | 1.5d | 0 |
| P5 — Deprecate Options tab | U5.1–U5.2 | 0.5d | 0 |

**Total ≈ 8 working days.** Critical path: U1.1 → U2.1 → U3.2 → U4.2.
Everything else parallelises.

---

## Phase 0 — Merge gate (on the dev branch, before it merges to main)

### ✅ U0.1 — Clear consultant conditions C1–C5

**Effort:** 1h · **Blocks:** everything · **Status:** DONE (2026-09-16, branch arena/01a0a901-back-test, commit c6fb3f9+)

C1 mutable defaults → `field(default_factory=...)` on `Playbook.tags`/`exit_config`.
- Fixed in `src/backtest/options/playbook.py`: `tags: list = field(default_factory=list)`, `exit_config: dict = field(default_factory=lambda: {...})`, `structure_type` also default_factory, version changed from str "1.0" to int 1 per final spec, added `updated_at`, defensive copy in `__post_init__`, version int coercion.
- Tests: `tests/test_playbooks_conditions.py::test_c1_tags_not_shared`, `test_c1_exit_config_not_shared`, `test_c1_version_is_int`, `test_c1_timestamps_exist` — all PASS.

C2 data-ownership rule → docstring + one engine assert (strategy data comes from
engine args only).
- Fixed in `src/backtest/forward/execution_engine.py`: module docstring explicitly states C2 rule (strategies NEVER call broker/quote APIs, all bars+chain snapshots flow engine→strategy), added `_assert_data_ownership()` method that asserts ExecutionContext is not None and has chain/bar data from engine, called in `check_risk()`. Logs debug for unit test mocks but enforces in production.
- Verified: `get_chain_snapshot()` is engine-owned feed, strategies call via ExecutionContext, never broker APIs directly.

C3 two-tier exits → module docstring + precedence list in
engine constants.
- Fixed in `src/backtest/forward/execution_engine.py`: added `EXIT_PRECEDENCE` constant list per architecture §1 (0 emergency Engine unconditional, 1 stop_loss_pct Playbook, 2 take_profit_pct Playbook, 3 time_dte_square_off Playbook-configured engine-executed, 4 signal_flip Strategy last), plus `DEFAULT_REENTER=False` (churn guard, -₹41,844 evidence) and `DEFAULT_MAX_REENTRIES_PER_DAY=2`. Module docstring documents two-tier.

C4 `risk_envelope()` returns `estimated: true`.
- Fixed in `src/backtest/options/playbook.py`: `risk_envelope()` now returns `{"estimated": True}` plus existing fields, with docstring noting C4 and lot_size from instrument master never stored. V1 2%/1%/4% model capped by max_loss_per_trade.
- Fixed in `src/backtest/web/static/js/playbooks.js`: card now shows `Risk cap: ₹X <span chip>estimated</span> max loss / signal (per-signal, not per-day)` + version badge `v{version}` with tooltip about snapshot reproducibility.
- Tests: `tests/test_playbooks_conditions.py::test_c4_risk_envelope_estimated_flag`, `test_c4_risk_envelope_capped` — PASS.

C5 confirm `create_app` exists (it does — `web/app.py:244`) and the `emergency_stop`
endpoint name is real on the branch.
- Verified: `src/backtest/web/app.py:245 def create_app(` exists, `src/backtest/api/portfolio.py:310 @portfolio_bp.post("/api/portfolio/emergency_stop")` exists, UI calls `/api/portfolio/emergency_stop` in `portfolio.js:684` and `playbooks.js:465`. No code change needed, documented here.

**Tests:** C1 regression (two playbooks don't share a list); C4 flag test — both in `tests/test_playbooks_conditions.py`, 6 tests PASS.

**Completion criteria met:** All C1-C5 cleared on branch, ready for U0.2 merge + full regression.

### ⬜ U0.2 — Branch merge + full regression

**Effort:** 30 min. Merge dev branch → main; run the full suite
(`cd src && python -m pytest ../tests -q`). Expected baseline: ~2,100+ pass,
1 pre-existing Windows process-pool failure. Stop if anything else fails.

---

## Phase 1 — Playbook entity + API

### ⬜ U1.1 — `Playbook` dataclass + registry

**Effort:** 0.5d · **Depends on:** U0.1 · **Critical path**

`src/backtest/playbooks/models.py` — `Playbook` exactly per architecture §2
(option-only V1, `version: int`, `max_loss_per_trade`, no lot_size stored).
Three methods: `to_expression()`, `to_runner_config(strategy_name,
allocated_capital, ...)`, `risk_envelope(spot, lot_size)` with
`estimated: true` and the 2%/1%/4% moneyness premium model, capped by
`max_loss_per_trade`.

`src/backtest/playbooks/registry.py` — thread-safe singleton `_REGISTRY`,
3 seeded defaults (bull call spread / bear put spread / long call), optional
`PLAYBOOKS_PATH` JSON load/save. Delete blocks `pb_default_*` IDs.

**Tests:** `tests/test_playbooks_models.py` — schema round-trip,
`to_runner_config` produces a payload `POST /runner/create` accepts,
`risk_envelope` cap math (spot 25,000, lot 75, qty 1 → ≈ ₹37,500 × qty,
capped), C1 shared-default regression, version bump on update.

### ⬜ U1.2 — Playbook API routes

**Effort:** 0.25d · **Depends on:** U1.1

`src/backtest/api/playbooks.py` — the 6 routes from architecture §2 verbatim
(list/filter by tag+underlying, get, create, update→bump version, delete with
`pb_default_*` guard, spawn→returns config, **no side effects**).

**Tests:** `tests/test_playbooks_api.py` — CRUD happy paths, 404s, seed
delete-block, spawn response matches `to_runner_config`, audit log line per
mutation (`scope="playbook"`).

### ⬜ U1.3 — Wire blueprint into `create_app`

**Effort:** 15 min · **Depends on:** U1.2

Register `playbooks_bp` in `web/app.py`; add URL to the nav JSON if one exists.

**Tests:** route list contains all 6 (use the existing app-fixture pattern).

### ⬜ U1.4 — JSON persistence round-trip

**Effort:** 15 min · **Depends on:** U1.1

Load registry from `PLAYBOOKS_PATH` at startup; save on every mutation.
Skip silently when the env var is unset (in-memory V1 default).

**Tests:** create → restart-simulated fresh registry → same playbook back.

### ⬜ U1.5 — Docs: playbook README section

**Effort:** 15 min · **Depends on:** U1.2

README: new "Playbooks" subsection under Options (what one is, the 6
endpoints, the 3 seeds). Not the architecture doc — user-facing only.

---

## Phase 2 — Execution engine

### ⬜ U2.1 — `ExecutionEngine` core

**Effort:** 1d · **Depends on:** U1.1 · **Critical path**

`src/backtest/engine/execution_engine.py` — `execute(signal, playbook,
runner_config, mode, source)`.

Responsibilities (architecture §1): resolve quote source
(synthetic→BS provider, mstock→`LiveQuoteProvider` when session valid, else
synthetic **with a `data_source: "synthetic-fallback"` label on the result**);
resolve lot size from the instrument master (never from the playbook); feed
chain snapshot to the strategy path (C2 — strategy receives data, never
fetches); build intent via the existing A3 seam (`build_intent_from_view`);
pre-trade risk check `max_loss_per_trade` (per-signal); route paper→
`OptionPaperBroker` / live→broker with margin check.

Output union: `Fill | OrderRejected(reason) | RiskHalted(reason)` — dataclasses,
not exceptions, so callers can't miss a rejection.

**Tests:** `tests/engine/test_execution_engine.py` — routing table (mode ×
source), C2 ownership (a strategy stub that tries a broker API call gets
nothing — engine feeds data), lot-size resolution, risk cap rejects, fill
path on a hand-built chain, fallback label present.

### ⬜ U2.2 — Two-tier exit precedence

**Effort:** 0.5d · **Depends on:** U2.1

Per-bar order as code: engine tier (breakers → emergency flatten, first and
unconditional) → playbook tier in priority order: stop_loss_pct →
take_profit_pct → time/DTE square-off → signal_flip. Re-entry only on the
**next bar**, default off (C3).

**Tests:** `tests/engine/test_exit_precedence.py` — parameterised: stop beats
target; stop beats flip; DTE beats flip; emergency overrides all; re-entry
same-bar impossible; `max_reentries_per_day` honoured (V1.1 knob, default 2).

### ⬜ U2.3 — Live-mode margin/risk gate

**Effort:** 0.5d · **Depends on:** U2.1

`mode=live` path: broker margin query before order; margin failure →
`OrderRejected("margin")`. No live path exists on synthetic fallback — live
orders require an authenticated broker session, else `OrderRejected("no_session")`.

**Tests:** margin reject; no-session reject; paper mode never queries margin.

### ⬜ U2.4 — Strategy adapter for the engine

**Effort:** 0.5d · **Depends on:** U2.1

`generate_market_view` already emits the signal; add a thin adapter so any
equity strategy (`generate_signals`) can also feed the engine with a
normalized `{direction, instrument_hint, confidence}` signal — the
plug-and-play contract from the user's point 3. Options vs swing is decided
by playbook/runner type, not by strategy code.

**Tests:** one equity strategy (sma_crossover) driven through the engine to a
paper fill via a stub playbook.

---

## Phase 3 — Portfolio integration

### ⬜ U3.1 — Options rows in Portfolio bucket view (read-only)

**Effort:** 0.5d · **Depends on:** U0.2 · **Highest value-per-hour**

The original UX wound: option trades invisible on Portfolio. Extend
`GET /api/portfolio/summary` (or instance detail) to include per-structure
option rows from the runner books: structure_type, legs, entry/close, P&L,
exit_reason. Render read-only in the existing instance/bucket trade table.

**Tests:** API returns rows for a runner with an open + a settled structure;
UI smoke check via preview tools.

### ⬜ U3.2 — Merge manual options book into the bucket ledger

**Effort:** 1d · **Depends on:** U0.2 · **Critical path**

`get_portfolio_summary()` gains `dashboard_book` (the manual options book);
totals = runners + manual book. `emergency_flatten_all(mode)` closes **both**
books (kills the flatten bug). Manual trade rows surface in the new Portfolio
tab (U4.2).

**Tests:** summary totals include dashboard book; flatten closes a manual
structure (regression for the 2026-09-16 bug); AC-15-style invariant —
Live-page numbers === Overview live-card numbers.

### ⬜ U3.3 — Audit logging with scope

**Effort:** 0.25d · **Depends on:** U2.1, U3.2

Every control action (spawn, flatten, kill, playbook CRUD, manual close) logs
`scope=paper|live|playbook|dashboard`. Audit view filters on it. (AC-16.)

**Tests:** one log line per action with the right scope.

### ⬜ U3.4 — Runner spawn snapshot

**Effort:** 0.25d · **Depends on:** U1.1

At spawn, snapshot `playbook.to_expression()` into the runner config; running
runners never mutate on playbook edit (architecture §2). Display the snapshot
version in instance detail.

**Tests:** edit playbook → running runner's config unchanged; new spawn uses
the new version.

---

## Phase 4 — UI

### ⬜ U4.1 — Portfolio tab strip

**Effort:** 0.25d · **Depends on:** U0.2

`Equity | Positions | 📚 Playbooks | 📦 Manual Options Book | Log` — tabs
exist and switch panels; content lands in U4.2/U3.1.

### ⬜ U4.2 — Playbooks tab

**Effort:** 1d · **Depends on:** U1.2, U4.1

Card grid: name, underlying·structure·strike·qty, exit bits, risk cap with
**"estimated"** badge (C4 rendering), tags, version. Actions: Deploy (spawns
via `/playbooks/<id>/spawn` → `/runner/create`), Edit, Delete, New Playbook
(form covering every field).

**Tests:** JS smoke via preview: create a playbook, deploy it, see the runner
appear with the snapshot version.

### ⬜ U4.3 — Manual Options Book tab

**Effort:** 0.5d · **Depends on:** U3.2, U4.1

Structures + legs tables, per-structure Close, Flatten Manual Book. Banners:
strategy/engine ownership (localStorage-dismissed) + dashboard-book count
with View/Flatten.

**Tests:** preview smoke: legacy manual trade visible here; flatten button
works.

---

## Phase 5 — Deprecate the Options tab

### ⬜ U5.1 — Deprecation banner + service view

**Effort:** 0.25d · **Depends on:** U4.2, U4.3

Banner: "This page is deprecated — trade from Portfolio → Playbooks. Chain,
Greeks and expiry alerts remain here as a service view." No ledger actions on
the page anymore; keep chain/Greeks/expiry APIs and views.

### ⬜ U5.2 — Hard-delete checklist

**Effort:** 15 min · **Depends on:** U5.1

Add to BACKLOG.md: delete the UI shell when Q6 criteria are met (zero new
manual-book structures in trailing 14 days AND ≥10 playbook-spawned runners;
review at 2 sprints regardless). Service view survives.

---

## Concurrency & sequencing

```
P0 (dev) ──► P1 ──► P2 ──► P3 ──► P4 ──► P5
              │      │      │
              └──────┴──────┴── U3.1 can start right after P0 (highest value-per-hour)
```

- **Do U3.1 first after the merge** — it fixes the original complaint (option
  trades invisible on Portfolio) for ~4h before any new machinery lands.
- P1 and P2 can run in parallel after U1.1 exists (U2.1 depends on the
  playbook entity, not the API).
- Chain-shape refactor (straddles/condors) is **Phase B** — richer option
  playbooks wait for it. Do not bundle.

## Acceptance criteria (summary)

1. AC-A: A playbook created in the UI can be deployed and trades on the paper
   bucket with zero code changes (plug-and-play, user point 1).
2. AC-B: Every option trade — runner or manual — is visible on the Portfolio
   page with P&L and exit reason (user point 2).
3. AC-C: A brand-new strategy class, emitting only the normalized signal, runs
   without touching the engine or broker APIs (user point 3 / C2).
4. AC-D: Exit precedence holds: emergency > stop > target > DTE > flip;
   same-bar re-entry impossible.
5. AC-E: `flatten` closes both books; totals = runners + manual book; Live
   page numbers === Overview numbers.
6. AC-F: `risk_envelope` and every UI surface showing it says "estimated".
7. AC-G: Full suite green (except the known Windows process-pool failure);
   every task ships with its tests in the same change.

## Out of scope (tracked elsewhere)

- Synthetic-feed → mStock wiring (Gap #1, highest-value next task *after*
  this rollout).
- Chain-shape refactor for multi-leg V2 structures (Phase B).
- Runner-state persistence (Gap #3).
- Per-runner vs per-bucket accounting (separate decision).
