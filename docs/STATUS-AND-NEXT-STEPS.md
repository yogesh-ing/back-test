# Status — What We Have & What Needs To Be Done

**Date:** 2026-09-17 · **Branch:** `main` @ `b39da03` · **Suite:** ~2,435 passed, 7 skipped (mStock credentials), known Windows process-pool flake
**✅ Architect restoration 2026-09-17:** the P0 missing deliverables are
**restored** on `arena/01a0b02a-back-test` — `feed_registry.py` +
`plugins/__init__.py` (user, via `main`), plus rebuilt test modules
(`test_feed_registry` 22, `test_strategy_conformance` 21, `test_mstock_live_bus`
31), strategy templates, `STRATEGY-AUTHORING.md` and the rebuilt experiment log.
The F-10 lint gate was also found red on committed `src/` (56 findings — more
uncommitted-tree state) and is green again. **Measured on the tree now:
2,457 passed / 4 skipped, JS harnesses green, app boots.** Remaining P0: clean-clone
CI (review §3.1). Full details: `docs/ARCHITECT-REVIEW-2026-09-17.md`.

This is the single consolidated picture, drawn from `UNIFIED-TRADING-TASKS.md`,
`instructions/ROADMAP.md`, `instructions/BACKLOG.md`, `PROJECT-CONTEXT.md`,
`docs/project-overview.md` and the forward-test experiment log. Where a source
doc is stale, this file follows the code.

---

## 1. What we have (working today)

### Backtest core ✅
- Vectorized + risk-aware engine paths that reconcile to `1e-5`; no-lookahead
  (`target.shift(1)`) enforced as invariant #1.
- Indian cost model done properly: STT/exchange/SEBI/stamp/GST per segment,
  options at flat ₹20/order with sell-side-only STT; validated against a real
  contract note. All money is `Decimal` (4 dp money / 8 dp prices).
- 5 built-in equity strategies + market-view options strategies; trade
  accounting has one source of truth (`engine/trades.py`).
- CLI (`list / run / compare / preflight / papertrade`) and web UI
  (backtest, compare, forward, data, health).

### Unified trading stack (P0–P7 of the Unified Trading PRD — 21/21 done ✅ — *except the deliverables below were never committed; on a clean checkout they are absent*)
- **Playbooks:** declarative plug-and-play option configs — dataclass + registry
  (thread-safe, JSON persistence via `PLAYBOOKS_PATH`, 3 seeded defaults,
  version auto-bump), 6 API routes, Portfolio-tab card grid UI, spawn snapshot
  (running runners never mutate on playbook edit).
- **Execution engine:** `mode` × `source` routing (paper→simulator,
  live→broker+margin gate), lot size from instrument master (never the
  playbook), pre-trade `max_loss_per_trade` risk cap, C2 data-ownership assert.
  *Architect note: the U2 "live" quote branch in `engine/execution_engine.py`
  (`_resolve_quote_source`) returns `MStockLiveFeed` — a bar feed — where the
  quote-provider contract is `get_quote(token)` + `source_name`; fix this seam
  during P1.1.*
- **Two-tier exits:** emergency > stop > target > DTE square-off > signal flip;
  same-bar re-entry impossible; `reenter` default **off** with
  `max_reentries_per_day` (closes the −₹41,844 churn finding).
- **Portfolio integration:** manual options book merged into bucket totals;
  flatten closes both books; per-structure option rows visible read-only on
  Portfolio; audit log with scope; slim 7-field spawn form.
- **Strategy plugins:** drop-in `plugins/strategies/*.py` with AST import-ban,
  conformance battery (determinism, output shape, metadata), templates for
  equity + option strategies — `docs/STRATEGY-AUTHORING.md`. *⚠️ the
  `backtest.plugins` package, the two `templates/` files and the doc are NOT in
  the repo — P0.*

### Data bus + mStock ✅ (the recent push — *⚠️ bus file uncommitted, see P0*)
- **Shared Market Data Bus** (`forward/feed_registry.py`): refcounted feeds per
  `(source, symbol, timeframe)`, one shared chain generator per underlying,
  process-wide singletons. *⚠️ This file is MISSING from the repo — it was never
  committed with U6.2/U7.1. The app cannot import `portfolio_manager` without it.*
- **mStock live bars into the bus (U7.1):** one background poll thread for all
  mstock symbols, market-hours gate, dedupe, error-soft — one sweep = one API
  call regardless of runner count. Committed as `b39da03` *minus the bus itself
  and its test module (`tests/forward/test_mstock_live_bus.py`)*.
- **mStock auth + order layer:** login/TOTP/OTP, session manager, order
  place/modify/cancel, fill polling, multi-leg `LiveOptionTrader`
  (*⚠️ not "dry-run default": `options/live_trading.py` has
  `dry_run: bool = False` — fail-open on a live-money path; flip to `True` +
  explicit confirm before T9.5*). 202 stocks / 467K daily bars + 154K
  instruments in Postgres (unverifiable in-repo; external DB).

### Portfolio command center ✅
- Live/Paper buckets with independent circuit breakers, per-bucket derived
  accounting, scoped bulk control, master kill, capability banner, single SSE
  stream, three views (Overview / Live / Paper). 116 portfolio tests.

---

## 2. What needs to be done

### 🔴 P0 — ~~Restore the uncommitted U6/U7 deliverables~~ **DONE 2026-09-17** — remaining: CI
0. ~~Recover/restore the files that were never committed~~ **DONE** — merged
   `feed_registry.py` + `plugins/__init__.py` from `main`; rebuilt the rest
   (tests, templates, docs) on `arena/01a0b02a-back-test` with the F-10 lint
   gate green and the full suite passing on the committed tree. **Still open
   from this item:** the clean-clone CI gate (pytest + `node --test tests/js`
   + `create_app()` smoke) so a "green on my machine" state can never ship again.
0b. ~~**Fail-closed fix (hours):** `LiveOptionTrader(dry_run=False)` default →
   `True` + explicit live confirmation gate~~ **DONE 2026-09-17:** the trader
   now defaults to `dry_run=True`; arming real orders requires all three gates
   (`dry_run=False` + `confirm_live=True` + env `ALLOW_LIVE_ORDERS=1`), else
   `ValueError` before any broker call (see `docs/OPTIONS-PAPER-LIVE.md` §3).
   Gate tests added (`TestFailClosedGate`); live-path tests explicitly arm.
0c. **Clean-clone CI — READY, needs one permitted push.** The workflow
   (`.github/workflows/ci.yml`, prepared in the workspace) + the smoke script
   (`scripts/smoke_check.py`, **on the branch**) implement the full gate:
   clean install → `flake8 src/` (F-10) → full pytest (incl. `test_plotting`)
   → boot smoke (create_app + canonical loop) → `node --test tests/js/test_*.mjs`.
   The coding-agent token cannot push files under `.github/workflows/` —
   land the workflow with one push from an account with workflow permission
   (see the architect review addendum for the exact steps). Windows stays out
   of the matrix until the known process-pool flake is quarantined or fixed.
   *(Then: enable branch protection on `main` requiring the CI check.)*

### 🟠 P1 — Close the "real data" loop (highest value)
1. **Live option chain + quotes (the last synthetic gap).** ~~Wire
   `MStockClient.get_option_chain()` + `get_option_quote()` / `LiveQuoteProvider`
   into `ChainBus`/`OptionsBridge` when `source=mstock` && authenticated~~
   **WIRED 2026-09-17** (`tests/test_live_options_wiring.py`, 31 tests): 
   `LiveChainProvider` serves real chains + LTP behind the generator duck type;
   `ChainBus.acquire(source="mstock")` shares ONE provider per underlying
   (rate-limit rule); runners route by `source` with a **labelled**
   `synthetic:bs` fallback when no session; the U2 engine seam now returns a
   real `LiveQuoteProvider` gated on a session check that actually exists.
   **Remaining:** T9.5 — exercise it against a REAL mStock session and record
   payloads (user action, credentials required).
1b. **(New) Start EOD option-chain snapshot capture the day P1.1 lands.**
   **DONE 2026-09-17** (`tests/test_chain_snapshots.py`): `OptionChainSnapshot`
   table + `ChainSnapshotRecorder` (terms for the whole chain in one
   instrument-master call, quotes for the nearest expiry via one call per
   expiry, append-only batches) + `scripts/snapshot_option_chains.py`
   (one-shot for cron, or `--interval-seconds` loop). **User action:** schedule
   it against the authenticated session — the data asset accrues from day one.
2. **Exercised live dry-run (T9.5).** The order client and `LiveOptionTrader`
   are tested only against mocks. Run the documented dry-run against a real
   mStock session and record payloads in the experiment doc.
3. ~~**Equity live fills (F-12)**~~ — **DONE (2026-09-17).** `mode='live'`
   equity runners on the multi-runner forward path now route orders through
   `LiveEquityGateway` (`src/backtest/forward/live_gateway.py`) — a live
   runner can no longer silently paper-fill. Fail-closed arming
   (`confirm_live=True` + `ALLOW_LIVE_ORDERS` + authenticated session —
   same invariant as LiveOptionTrader); one `place_order` per ledger order
   (the `client_order_id` rides to the venue; the venue id is stamped on
   the ledger order); fills come back by polling (manager tick pump) as
   CUMULATIVE-DELTA only (re-polls can never double-book), booked at the
   broker's ACTUAL price into the SAME shared portfolio/ledger path.
   `reconcile()` compares the venue order book each 300 ticks —
   rejected/expired orders are cancelled locally with a WARNING. Restarts
   re-arm POLLING via the P2.4 state file (working set `coid→venue_id`
   survives) — never re-place. Disarmed boot skips a persisted live runner
   with a WARNING and still boots. Bucket-level risk anchors were already
   mode-aware; 19 tests in `tests/test_live_equity_gateway.py` +
   `tests/live_test_support.py` fakes for live-bucket suites.

### 🟠 P2 — Durability & honesty
4. ~~**Runner/portfolio state persistence (Gap #3, "V2")**~~ — **DONE
   (P2.4, wired 2026-09-17).** Opt-in via `state_path=` ctor arg or
   `PORTFOLIO_STATE_PATH`. Every control-plane mutation (add/remove/control/
   pause/resume/stop/flatten/breaker-reset/anchors/shutdown) plus every 60th
   tick snapshots atomically (tmp+`os.replace`) to JSON: runner configs with
   instance identity, the full `Portfolio.to_dict` book (cash/positions/
   orders/equity history), runtime scalars, option bridge books (structures,
   legs, broker cash, bar-clock), and manager + bucket breaker latches.
   A fresh manager rehydrates everything; persisted RUNNING comes back
   **PAUSED** (fail-closed — nothing trades until a human resumes), tripped
   breakers STAY tripped. Corrupt files are renamed `*.corrupt` and boot is
   clean; schema mismatch is ignored; a failed write never corrupts the last
   good state. No path → byte-identical V1 behaviour. 16 tests:
   `tests/forward/test_state_persistence.py`.
5. **Portfolio-page option trade rows.** API exposes them; render
   per-structure rows in the instance/bucket trade table UI (U3.1 backend
   landed, UI polish remains).
6. **Options-tab hard delete (U5.2 checklist).** Criteria: zero new
   manual-book structures in trailing 14 days **and** ≥10 playbook-spawned
   runners; review by **2026-09-30** regardless. Delete
   `options.html` + `options.js` shell; keep chain/Greeks/expiry services.
7. **Consultant sign-off on 6 open questions** (root **`CONSULTANT_RESPONSE.md`**, "Open Questions" §0.4 — *path corrected; no `docs/consultant quest review.md` exists*):
   playbook scope (option-only vs equity), risk-envelope V2 (BS+IV+SPAN
   inputs), exit precedence confirmation, re-enter policy, playbook
   versioning, hard-delete metric.

### 🟡 P3 — Engine depth (Roadmap Phase 1–2 leftovers)
8. Position sizing presets for the **vectorized engine + CLI** (fixed-fraction /
   fixed-cash / ATR-target), fill models (next-open vs close, pluggable
   slippage), trade log export (CSV/JSON). *Scope correction: sizers already
   exist on the forward side (`strategy_adapter.py`, `simulator/position_sizing.py`,
   `backtest_driver.size_fn`) — the gap is presets/exposure on the vectorized path.*
9. Richer metrics: Sortino, expectancy, profit factor, monthly heatmap.
10. Parameter optimization + walk-forward analysis (grid/random search,
    rolling in-sample/out-of-sample), Monte-Carlo / deflated Sharpe.
11. Chain-shape refactor for multi-leg V2 structures (straddles/condors) —
    Phase B, do **not** bundle with anything.

### 🟢 P4 — Platform hygiene (low urgency, real debt)
12. ~~Orphaned code: `forward/live_engine.py` (697 lines)~~ **already deleted**
    (folded into `data/mstock_live_feed.py` per P3.4; its old tests now exercise
    `ForwardTestingEngine`) ~~dead config files (`market_data.yaml`, `time_sync.yaml`)~~
    **already removed** — both closed by architect review. Remaining here:
    legacy `/dashboard` route + `dashboard.html` template slated for retirement.
12b. **(New) Repo hygiene:** `graphify-out/` + `.idea/` tracked despite
    `.gitignore` (52 files) → `git rm -r --cached`; move shared test fixtures
    out of test-to-test imports (`test_bucket_risk.py` ← `test_live_engine.py`)
    into a helpers/conftest module.
12c. ~~**(New) Corporate actions**~~ — **DONE (2026-09-17).** Policy wired:
     `src/backtest/data/corporate_actions.py`. Storage stays RAW;
     back-adjustment happens at READ time (`AdjustedSource`, wrapped into
     `mode='backtest'` by `SourceRegistry` when
     `config/data_quality.yaml → daily_bar.corporate_actions.enabled: true`).
     Split/bonus factors: pre-ex-date prices × factor, volume ÷ factor,
     latest bars stay traded. `corporate_actions` DB table (`models.py`) is
     the ops upkeep path; `data/corporate_actions.csv` the flat-file path.
     The gate: `daily_return_outliers` flags |1-day returns| > ±40%
     (configurable) as suspected unadjusted actions — run
     `scripts/scan_split_suspects.py --root data --strict` on a schedule;
     unexplained suspects WARN in every adjusted read too. Default OFF ⇒
     byte-identical legacy behaviour. 34 tests:
     `tests/test_corporate_actions.py`. **Operator TODO:** populate
     `data/corporate_actions.csv` for the live universe (NSE announcements),
     then flip `enabled: true`.
13. **Production server:** still Flask dev server. #4 (state persistence) is
    DONE — the restart-survival half of the Gunicorn blocker is closed; the
    multi-worker trap remains (shared mutable state across workers), so
    Gunicorn still wants `--workers 1` + `--preload`, or an external store
    before scaling out.
14. Timeframe cosmetic on synthetic/CSV (daily bars only — gap G6).
15. Money inexact on SQLite (NUMERIC→float) — Postgres for anything reported.
16. Broker cost rates are FY 2024-25 — re-verify against a fresh contract note.
17. Alerts (email/Telegram) for breaker trips; strategy auto-kill on
    underperformance (Roadmap 3d).

---

## 3. Suggested order

```
0. Restore missing U6/U7 files + CI clean-clone gate   ← P0, nothing else is real until this is done
0b. Fail-closed defaults on LiveOptionTrader           ← hours, do first
1. Live option chain/quotes wiring                     ← makes forward tests REAL
1b. Start EOD option-chain snapshot capture            ← rides #1; options research data accrues
2. Live dry-run exercise (T9.5) + F-12 equity fills    ← F-12 DONE (idempotent + reconciled); T9.5 = real-session exercise remains
3. Runner-state persistence                            ← DONE (P2.4): survives restarts, fail-closed
3b. Corporate-action policy + split-suspect gate        ← DONE (12c): raw storage, read-time adjust, ±40% gate
4. Consultant answers → risk envelope V2
5. Engine depth (sizing, metrics, optimization) in parallel
```

---

## 4. Pointers

| Want… | Read |
|---|---|
| Task-by-task unified trading record | `docs/UNIFIED-TRADING-TASKS.md` |
| Full platform tour | `docs/project-overview.md` |
| Invariants | `PROJECT-CONTEXT.md` |
| Architect cross-verification of this file + new gaps | `docs/ARCHITECT-REVIEW-2026-09-17.md` |
| Write a strategy | `docs/STRATEGY-AUTHORING.md` *(missing from repo — P0)* |
| Forward-test experiment log (honest findings) | `docs/OPTIONS-FORWARD-TEST-EXPERIMENT.md` *(missing from repo — P0)* |
| Big-picture plan | `instructions/ROADMAP.md` + `instructions/BACKLOG.md` |
| Playbook architecture sign-off + the 6 open questions | root `CONSULTANT_RESPONSE.md` |
