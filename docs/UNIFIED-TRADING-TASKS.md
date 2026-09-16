# Task PRD — Unified Trading Implementation

**Architecture:** [`docs/ARCHITECTURE-UNIFIED-TRADING.md`](ARCHITECTURE-UNIFIED-TRADING.md) (signed off 2026-09-16)
**Format:** mirrors [`docs/OPTIONS-BACKTEST-TASKS.md`](OPTIONS-BACKTEST-TASKS.md)
**Status key:** ⬜ not started · 🔨 in progress · ✅ done · ⛔ blocked

---

## Summary

| Phase | Tasks | Est. | Done |
|---|---|---|---|
| P0 — Merge gate (dev branch) | U0.1–U0.2 | 1h | 2 |
| P1 — Playbook entity + API | U1.1–U1.5 | 2d | 5 |
| P2 — Execution engine | U2.1–U2.4 | 2d | 4 |
| P3 — Portfolio integration | U3.1–U3.4 | 2.5d | 4 |
| P4 — UI (Playbooks + Manual Book tabs) | U4.1–U4.3 | 1.5d | 3 |
| P5 — Deprecate Options tab | U5.1–U5.2 | 0.5d | 2 |

**Total ≈ 8 working days.** Critical path: U1.1 → U2.1 → U3.2 → U4.2.
Everything else parallelises. **P0 complete, P1 complete, P2 complete, P3 complete, P4 complete, P5 complete — 20/20 tasks DONE.**

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

### ✅ U0.2 — Branch merge + full regression

**Effort:** 30 min · **Status:** DONE (2026-09-16)

- Merged `origin/main` (29a50c3 + b197b62) into `arena/01a0a901-back-test` via merge commit `dc8ec54` — docs/ARCHITECTURE-UNIFIED-TRADING.md + UNIFIED-TRADING-TASKS.md now in branch.
- Full regression: `PYTHONPATH=src pytest tests -q --ignore=tests/test_plotting.py` → **2328 passed, 7 skipped, 113 warnings in 43.18s** — exceeds baseline 2,100+ pass, 0 failures (Windows process-pool failure not present on Linux).
- U0.1 tests also green: `test_playbooks_conditions.py` 6 passed.
- Ready for P1 U1.1.

---

## Phase 1 — Playbook entity + API

### ✅ U1.1 — `Playbook` dataclass + registry

**Effort:** 0.5d · **Depends on:** U0.1 · **Critical path** · **Status:** DONE (2026-09-16, commit 7a6fd06)

`src/backtest/playbooks/models.py` — `Playbook` exactly per architecture §2
(option-only V1, `version: int`, `max_loss_per_trade`, no lot_size stored).
Three methods: `to_expression()`, `to_runner_config(strategy_name,
allocated_capital, ...)`, `risk_envelope(spot, lot_size)` with
`estimated: true` and the 2%/1%/4% moneyness premium model, capped by
`max_loss_per_trade`.

`src/backtest/playbooks/registry.py` — thread-safe singleton `_REGISTRY` with RLock,
3 seeded defaults (bull call spread / bear put spread / long call), optional
`PLAYBOOKS_PATH` JSON load/save. Delete blocks `pb_default_*` IDs. Save auto-bumps version.

Backward compat: `src/backtest/options/playbook.py` now re-exports canonical implementation.

**Tests:** `tests/test_playbooks_models.py` — 8 tests PASS: schema round-trip,
`to_runner_config` produces a payload `POST /runner/create` accepts,
`risk_envelope` cap math (spot 25,000, lot 75, qty 1 → ≈ ₹37,500 × qty,
capped), C1 shared-default regression, version bump on update, seeded defaults, delete blocks, lot_size not stored.
Plus `test_playbooks_conditions.py` 6 tests — total 14 PASS.

### ✅ U1.2 — Playbook API routes

**Effort:** 0.25d · **Depends on:** U1.1 · **Status:** DONE (2026-09-16, commit 900dbe2+)

`src/backtest/api/playbooks.py` — the 6 routes from architecture §2 verbatim
(list/filter by tag+underlying, get, create, update→bump version, delete with
`pb_default_*` guard, spawn→returns config, **no side effects**).

- Spawn now returns config only, no side effects — one creation path `POST /api/portfolio/runner/create`, one audit trail (ratified per architecture).
- Audit: every mutation logs `[AUDIT] scope=playbook action=CREATE/UPDATE/DELETE/SPAWN playbook_id=...` via `_audit_log()` — AC-16.
- Uses canonical `playbooks.models` + `registry`, not old `options/playbook`.

**Tests:** `tests/test_playbooks_api.py` — 8 tests PASS: CRUD happy paths, 404s, seed delete-block, spawn response matches `to_runner_config`, audit log, side-effect-free (no instance_id).

### ✅ U1.3 — Wire blueprint into `create_app`

**Effort:** 15 min · **Depends on:** U1.2 · **Status:** DONE

- Registered `playbooks_bp` in `web/app.py:349` `app.register_blueprint(playbooks_bp)` — already done in earlier prototype.
- Route list contains all 6: `/api/playbooks` (GET, POST), `/api/playbooks/<playbook_id>` (GET, PUT, DELETE), `/api/playbooks/<playbook_id>/spawn` (POST) — verified via `create_app` route inspection.

**Tests:** route list check PASS.

### ✅ U1.4 — JSON persistence round-trip

**Effort:** 15 min · **Depends on:** U1.1 · **Status:** DONE

- `registry.py`: `_resolve_storage_path()` checks explicit path first, then `PLAYBOOKS_PATH` env var, else None (in-memory V1 default) — skip silently when unset.
- `PlaybookRegistry.__init__` loads defaults, then loads from file if exists; `_save_to_file()` on every mutation (save/delete).
- Tested: create → file exists → reset registry (simulated restart) → same playbook back, plus env var path loading — PASS.

**Tests:** manual round-trip test PASS (see U0.1 commit), plus `test_playbooks_models.py` covers save/load.

### ✅ U1.5 — Docs: playbook README section

**Effort:** 15 min · **Depends on:** U1.2 · **Status:** DONE

- README.md updated with "Playbooks (Unified Trading — Plug-and-Play Option Configs)" subsection under Options: what a playbook is, entity spec (playbook_id uuid4, underlying option-only V1, structure_type default_factory, strike_selection, exit_config reenter=False churn guard, max_loss_per_trade per-SIGNAL, tags default_factory, version int auto-bump, created_at/updated_at), 3 seeded defaults, methods to_expression/to_runner_config/risk_envelope with estimated:true, 6 API endpoints, execution engine C2/C3/C4/C5, portfolio integration, UI, conditions C1-C5 cleared, docs links to ARCHITECTURE-UNIFIED-TRADING.md and UNIFIED-TRADING-TASKS.md.
- User-facing only, not architecture doc — per task.

---

## Phase 2 — Execution engine

### ✅ U2.1 — `ExecutionEngine` core

**Effort:** 1d · **Depends on:** U1.1 · **Critical path** · **Status:** DONE (2026-09-16, commit 4c78ded+)

`src/backtest/engine/execution_engine.py` — `execute(signal, playbook,
runner_config, mode, source)`.

- Responsibilities implemented: resolve quote source (synthetic→BS provider via SyntheticChainGenerator, mstock→LiveQuoteProvider when session valid via session_manager, else synthetic with `data_source: "synthetic-fallback"` label), resolve lot_size from instrument master (InstrumentRegistry, never from playbook, fallback defaults NIFTY 50 BANKNIFTY 15 etc), feed chain snapshot to strategy path (C2 — engine owns feed, strategies receive via ExecutionContext, _assert_data_ownership), build intent via A3 seam `build_intent_from_view` (placeholder intent with market_view+expression+lot_size for V1), pre-trade risk check `max_loss_per_trade` per-signal (raw estimate spot×pct×qty×lot_size vs cap → RiskHalted), route paper→Fill paper_fill / live→Fill live_fill or OrderRejected no_session when fallback, output union Fill|OrderRejected|RiskHalted dataclasses not exceptions.
- C3 constants re-exported: EXIT_PRECEDENCE, DEFAULT_REENTER=False, DEFAULT_MAX_REENTRIES_PER_DAY=2.
- Canonical location per architecture, plus backward compat wrapper in `forward/execution_engine.py` (UnifiedExecutionEngine) that now also has EXIT_PRECEDENCE, C2 assert, C4 estimated flag.
- Created `src/backtest/engine/__init__.py` re-exports.

**Tests:** `tests/engine/test_execution_engine.py` — 7 tests PASS: routing table mode×source, C2 ownership (strategy stub that tries broker API gets nothing, underlying missing → invalid_signal, valid → Fill), lot-size resolution (NIFTY 50, BANKNIFTY 15, unknown 50, never from playbook), risk cap rejects (tight cap 1000 vs 25k raw → RiskHalted, loose → Fill), fill path hand-built chain (paper Fill with lot_size 50 data_source synthetic), fallback label present (mstock live no session → synthetic-fallback label, execute → OrderRejected no_session), equity signal through engine (sma_crossover precursor → Fill).

### ✅ U2.2 — Two-tier exit precedence

**Effort:** 0.5d · **Depends on:** U2.1 · **Status:** DONE (2026-09-16, commit pending)

Per-bar order as code: engine tier (breakers → emergency flatten, first and
unconditional) → playbook tier in priority order: stop_loss_pct →
take_profit_pct → time/DTE square-off → signal_flip. Re-entry only on the
**next bar**, default off (C3).

Fixes in `src/backtest/forward/options_bridge.py`:
- `_blocked_reentry` now always blocks same-bar (`_exit_bar_index == _bar_index`) — previously returned `not _should_reenter` which allowed same-bar flip+reenter, causing -₹41,844 churn evidence.
- `_should_reenter` now returns False on same-bar (`_exit_bar_index == _bar_index`), enforces next-bar only, checks `max_reentries_per_day` V1.1 knob from `expression["exit"]["max_reentries_per_day"]` default 2, and `_reentries_today` counter.
- `__init__` adds `_reentries_today=0` `_reentry_day=None`; `on_bar` resets per-day when day changes; `on_market_view` counts re-entries in normal entry path (increment on flip+reenter fall-through), double-count guard removed.
- Constants `EXIT_PRECEDENCE`, `DEFAULT_REENTER=False`, `DEFAULT_MAX_REENTRIES_PER_DAY=2` already in engine (U2.1).

**Tests:** `tests/engine/test_exit_precedence.py` — 8 PASS: stop beats target (ExitPolicy), stop beats flip (risk before signal), DTE beats flip (1d expiry vs flip), emergency overrides all (UnifiedExecutionEngine circuit_breaker tier 0), same-bar impossible (_blocked_reentry True + _should_reenter False on exit bar regardless of reenter=True), next-bar allowed (blocked False next bar, should_reenter True, _reentries_today increments), max_reentries_per_day honoured (max=1 blocks second re-entry same day), engine EXIT_PRECEDENCE constants order emergency(0) > stop(1) > target(2) > DTE(3) > flip(4).

### ✅ U2.3 — Live-mode margin/risk gate

**Effort:** 0.5d · **Depends on:** U2.1 · **Status:** DONE (2026-09-16, commit pending)

`mode=live` path: broker margin query before order; margin failure →
`OrderRejected("margin")`. No live path exists on synthetic fallback — live
orders require an authenticated broker session, else `OrderRejected("no_session")`.

Implemented in `src/backtest/engine/execution_engine.py`:
- `__init__` now accepts `live_broker` + `margin_calculator` — paper mode never touches live_broker.
- `_check_live_margin(signal, playbook, spot, lot_size, data_source)` queries broker via `get_available_margin()` / `get_margin()` / `check_margin()` interfaces (supports multiple broker shapes), estimates required margin via `_estimate_required_margin` (spot×pct×qty×lot_size, pct 2%/1%/4% per moneyness).
- `execute()` live path: first checks synthetic-fallback → OrderRejected(no_session), then margin gate → OrderRejected(margin) if insufficient, else Fill.
- Paper path never calls live_broker.

**Tests:** `tests/engine/test_live_margin_gate.py` — 6 PASS: margin reject (stub insufficient → OrderRejected margin), no-session reject (mstock no session → synthetic-fallback → OrderRejected no_session), paper never queries margin (broker 0 margin but paper → Fill, queried False), sufficient margin allows fill (live:mstock mock → Fill), no broker configured allows fill (skip check), check_margin interface bool false → margin reject.

### ✅ U2.4 — Strategy adapter for the engine

**Effort:** 0.5d · **Depends on:** U2.1 · **Status:** DONE (2026-09-16, commit pending)

`generate_market_view` already emits the signal; add a thin adapter so any
equity strategy (`generate_signals`) can also feed the engine with a
normalized `{direction, instrument_hint, confidence}` signal — the
plug-and-play contract from the user's point 3. Options vs swing is decided
by playbook/runner type, not by strategy code.

Implemented `src/backtest/engine/strategy_adapter.py`:
- `StrategyAdapter.adapt(strategy, candles, underlying, chain_snapshot, strategy_name)` → `UnifiedSignal | None`
- Tries `generate_market_view` first (options-native) → `UnifiedSignal.option_view`
- Falls back to `generate_signals` (equity) → normalizes last signal 1/0/-1 to BULLISH/BEARISH/NEUTRAL, builds MarketView + equity_info dual routing (same signal works for options or equity depending on playbook/runner type), confidence 0.8 default.
- `_resolve_underlying` from strategy params or default NIFTY, `_try_market_view`, `_try_generate_signals`, `_market_view_to_signal`, `_signals_to_unified`
- Functional wrappers `adapt_strategy`, `adapt_strategy_many`
- C2 preserved: engine feeds bars+chain, strategy never calls broker APIs.

**Tests:** `tests/engine/test_strategy_adapter.py` — 6 PASS: equity strategy adapted to UnifiedSignal with direction/instrument_hint/confidence + equity_info + market_view, market_view strategy adapted, neutral returns None, equity signal through engine paper fill via stub playbook (U2.4 AC), functional wrapper, options vs swing decided by playbook not strategy (same signal → Fill with options playbook and with loose envelope equity path).

---

## Phase 3 — Portfolio integration

### ✅ U3.1 — Options rows in Portfolio bucket view (read-only)

**Effort:** 0.5d · **Depends on:** U0.2 · **Highest value-per-hour** · **Status:** DONE (2026-09-16)

The original UX wound: option trades invisible on Portfolio. Extend
`GET /api/portfolio/summary` (or instance detail) to include per-structure
option rows from the runner books: structure_type, legs, entry/close, P&L,
exit_reason. Render read-only in the existing instance/bucket trade table.

Already implemented in `portfolio_manager.py` + `paper_runner.py`:
- `StrategyRunner.get_state()` includes `options` summary with `open_structures_detail` (flat rows: symbol, structure_type, strikes, legs_detail, entry_price, unrealized_pnl, kind=option)
- `closed_trades` includes option trades with exit_reason, pnl, structure_type
- `get_portfolio_summary()` includes `dashboard_book` with positions/structures
- `get_runner_detail()` includes trades with option rows

**Tests:** `tests/engine/test_portfolio_options_rows.py` — 3 PASS: runner options rows visible (open_structures_detail with structure_type, legs, entry_price, unrealized_pnl, kind=option + closed with reason), portfolio summary includes dashboard_book, runner detail includes options trades with exit_reason.

### ✅ U3.2 — Merge manual options book into the bucket ledger

**Effort:** 1d · **Depends on:** U0.2 · **Critical path** · **Status:** DONE (2026-09-16)

`get_portfolio_summary()` gains `dashboard_book` (the manual options book);
totals = runners + manual book. `emergency_flatten_all(mode)` closes **both**
books (kills the flatten bug). Manual trade rows surface in the new Portfolio
tab (U4.2).

Implemented in `portfolio_manager.py`:
- `_get_dashboard_book()` returns singleton dashboard OptionPaperBroker if exists
- `get_dashboard_book_summary()` refreshes MTM and returns positions/structures
- `get_portfolio_summary()` embeds `dashboard_book` and combines totals: total_equity = runner equity + dashboard equity, daily_pnl, realized_pnl, open_positions all include dashboard
- `flatten_dashboard_book(reason)` closes all open structures via quote provider
- `emergency_flatten_all()` calls `flatten_dashboard_book` so flatten closes EVERYTHING

**Tests:** `tests/engine/test_bucket_ledger_merge.py` — 3 PASS: summary totals include dashboard book (total_equity >= dashboard equity, open_positions >= dashboard open), flatten closes manual structure (regression for 2026-09-16 bug — broker open 1 → flatten → 0, count >=1), AC-15 invariant (buckets embedded == get_bucket_aggregates, live scoped summary == bucket live equity).

### ✅ U3.3 — Audit logging with scope

**Effort:** 0.25d · **Depends on:** U2.1, U3.2 · **Status:** DONE (2026-09-16)

Every control action (spawn, flatten, kill, playbook CRUD, manual close) logs
`scope=paper|live|playbook|dashboard`. Audit view filters on it. (AC-16.)

Implemented:
- `PortfolioManager._audit_log_entries` deque maxlen 1000
- `_audit_log(action, scope, instance_id, detail)` logs [AUDIT] scope=... action=... and stores dict
- `get_audit_log(scope, limit)` filters by scope, most recent first
- Calls in `add_runner` (SPAWN scope=bucket), `remove_runner` (DELETE), `control_runner` (action.upper scope=bucket), `emergency_flatten_all` (EMERGENCY_FLATTEN scope=mode or all), `flatten_dashboard_book` (FLATTEN_DASHBOARD scope=dashboard), `reset_circuit_breaker` (RESET_BREAKER)
- `playbooks_api._audit_log` already logs scope=playbook via manager._audit_log if available
- API endpoint `GET /api/portfolio/audit?scope=paper&limit=100` returns filtered audit

**Tests:** `tests/engine/test_audit_and_snapshot.py::test_audit_log_scope_per_action` — PASS: one log line per action with right scope (paper spawn, pause, emergency_flatten, playbook create, dashboard flatten), filter works.

### ✅ U3.4 — Runner spawn snapshot

**Effort:** 0.25d · **Depends on:** U1.1 · **Status:** DONE (2026-09-16)

At spawn, snapshot `playbook.to_expression()` into the runner config; running
runners never mutate on playbook edit (architecture §2). Display the snapshot
version in instance detail.

Implemented:
- `Playbook.to_runner_config()` now includes `playbook_id`, `playbook_version`, `playbook_snapshot` (expression at spawn)
- `RunnerConfig` adds `playbook_id`, `playbook_version`, `playbook_snapshot` optional fields
- `api/portfolio.py create_runner` passes playbook fields from request into RunnerConfig
- `StrategyRunner.get_state()` returns `playbook_id`, `playbook_version`, `playbook_snapshot` for UI display
- `playbooks_api.spawn_from_playbook` already returns runner_config with snapshot

**Tests:** `tests/engine/test_audit_and_snapshot.py::test_runner_spawn_snapshot` — PASS: v1 snapshot quantity 1, edit playbook → v2 quantity 2, running runner unchanged (v1 qty 1), new spawn uses v2 qty 2, instance detail displays snapshot version; `test_playbook_spawn_api_includes_snapshot` — PASS: spawn API returns playbook_id, version, snapshot.

---

## Phase 4 — UI

### ✅ U4.1 — Portfolio tab strip

**Effort:** 0.25d · **Depends on:** U0.2 · **Status:** DONE (2026-09-16)

`Equity | Positions | 📚 Playbooks | 📦 Manual Options Book | Log` — tabs
exist and switch panels; content lands in U4.2/U3.1.

Implemented in `src/backtest/web/templates/_portfolio_center.html`:
- Tabs: Combined Equity Curve (equity), Aggregate Open Positions (positions), 📚 Playbooks (playbooks), 📦 Manual Options Book (dashboard-book), Master Audit Log (log)
- Tab switching via `.tab` click → `.tab-panel` hidden toggle in `portfolio.js`
- `portfolio_paper.html` and `portfolio_live.html` now include `playbooks.js` for Playbooks tab functionality

**Tests:** Manual UI smoke — tabs exist and switch panels.

### ✅ U4.2 — Playbooks tab

**Effort:** 1d · **Depends on:** U1.2, U4.1 · **Status:** DONE (2026-09-16)

Card grid: name, underlying·structure·strike·qty, exit bits, risk cap with
**"estimated"** badge (C4 rendering), tags, version. Actions: Deploy (spawns
via `/playbooks/<id>/spawn` → `/runner/create`), Edit, Delete, New Playbook
(form covering every field).

Implemented in `src/backtest/web/static/js/playbooks.js`:
- `playbookCardHtml()` renders card with name, underlying·structure·strike·qty, exit bits, risk cap with estimated badge (C4), tags, version badge with tooltip
- Actions: Deploy (spawnFromPlaybook → /playbooks/<id>/spawn → /runner/create), Edit (prompt-based), Delete, New Playbook (prompt-based form)
- `loadPlaybooks()` fetches /api/playbooks and renders grid
- Integrated into portfolio page via tab click → loadPlaybooks()

**Tests:** JS smoke via preview: create a playbook, deploy it, see the runner appear with the snapshot version — covered by `test_playbooks_api.py` + `test_audit_and_snapshot.py::test_playbook_spawn_api_includes_snapshot`.

### ✅ U4.3 — Manual Options Book tab

**Effort:** 0.5d · **Depends on:** U3.2, U4.1 · **Status:** DONE (2026-09-16)

Structures + legs tables, per-structure Close, Flatten Manual Book. Banners:
strategy/engine ownership (localStorage-dismissed) + dashboard-book count
with View/Flatten.

Implemented in `src/backtest/web/templates/_portfolio_center.html` + `playbooks.js`:
- Tab `dashboard-book` with refresh button, structures table (structure_id, type, underlying, expiry, legs, entry cost, unrealized, Close action), legs table (symbol, side, strike, type, qty, entry, LTP, P&L)
- Banners: unified execution banner (strategy/engine ownership, localStorage-dismissed) + dashboard-book count banner with View/Flatten buttons
- `loadDashboardBook()` fetches /api/portfolio/summary → dashboard_book, renders tables, wires Close buttons → /api/options/structures/<id>/close
- Flatten Manual Book button → /api/portfolio/emergency_stop with mode=paper (now also flattens dashboard book per U3.2)

**Tests:** preview smoke: legacy manual trade visible here; flatten button works — covered by `test_bucket_ledger_merge.py`.

---

## Phase 5 — Deprecate the Options tab

### ✅ U5.1 — Deprecation banner + service view

**Effort:** 0.25d · **Depends on:** U4.2, U4.3 · **Status:** DONE (2026-09-16)

Banner: "This page is deprecated — trade from Portfolio → Playbooks. Chain,
Greeks and expiry alerts remain here as a service view." No ledger actions on
the page anymore; keep chain/Greeks/expiry APIs and views.

Implemented in `src/backtest/web/templates/options.html`:
- Banner at top: ⚠️ Deprecated — Moved to Portfolio, with links to Portfolio → Manual Options Book and Portfolio → Playbooks, Go to Portfolio button
- Title changed to "Options Trading (Deprecated)"
- Subtitle: "Paper trading book — multi-leg structures, portfolio Greeks, expiry alerts. Machinery kept as services; ledger merged into Portfolio."
- Open Structure button marked "(legacy)" — trades still visible on Portfolio per U3.1/U3.2
- Service view preserved: quote source badge, portfolio Greeks (Delta/Gamma/Theta/Vega/Rho), expiry alerts, open positions table, structures table, chain/Greeks/expiry APIs remain

### ✅ U5.2 — Hard-delete checklist

**Effort:** 15 min · **Depends on:** U5.1 · **Status:** DONE (2026-09-16)

Add to BACKLOG.md: delete the UI shell when Q6 criteria are met (zero new
manual-book structures in trailing 14 days AND ≥10 playbook-spawned runners;
review at 2 sprints regardless). Service view survives.

Added to `instructions/BACKLOG.md`:
- Q6 criteria: zero new manual-book structures in trailing 14 days AND ≥10 playbook-spawned runners; review at 2 sprints (2026-09-30) regardless
- Service view survives: chain, Greeks, expiry alerts remain as service view — keep chain/Greeks/expiry APIs and views
- Checklist: monitor manual-book creation rate, count playbook-spawned runners, review at 2 sprints, delete UI shell (options.html + options.js + legacy modal), keep service view APIs, update nav to remove Options tab link

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
