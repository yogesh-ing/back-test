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
0b. **Fail-closed fix (hours):** `LiveOptionTrader(dry_run=False)` default →
   `True` + explicit live confirmation gate; sweep other live branches for
   fail-open defaults. *(Not yet done — next after CI.)*

### 🟠 P1 — Close the "real data" loop (highest value)
1. **Live option chain + quotes (the last synthetic gap).** Bars now come from
   mStock, but the option chain stack is still synthetic — runner
   `quote_source` reads `synthetic:bs` and options price off BS, not real LTP
   (confirmed: `forward/options_bridge.py` hard-imports `SyntheticChainGenerator`).
   Wire `MStockClient.get_option_chain()` + `get_option_quote()` /
   `LiveQuoteProvider` into `ChainBus`/`OptionsBridge` when `source=mstock` &&
   authenticated; also fix the U2 quote-seam mismatch (see §1 note). **~1 day. Unblocks any meaningful options forward test.**
1b. **(New) Start EOD option-chain snapshot capture the day P1.1 lands.**
   Without stored snapshots (strikes/premiums/IV/OI), options *backtests* stay
   synthetic-BS forever — the PRD admits this. Cheapest while the live wiring
   is open; accrues the data asset that risk-envelope V2 needs.
2. **Exercised live dry-run (T9.5).** The order client and `LiveOptionTrader`
   are tested only against mocks. Run the documented dry-run against a real
   mStock session and record payloads in the experiment doc.
3. **Equity live fills (F-12).** `BrokerFillProvider` + `poll_fill` wiring into
   the broker ABC for `mode=live` equity runners; bucket-level risk anchors.

### 🟠 P2 — Durability & honesty
4. **Runner/portfolio state persistence (Gap #3, "V2").** All forward-test
   state is in-memory; a restart loses the book and resurrects halted
   breakers. Persist runner configs + ledgers; this is also the prerequisite
   for any production WSGI move (see #8).
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
12c. **(New) Corporate actions:** no split/bonus/dividend handling anywhere in
    the data layer — raw NSE daily bars mean one unadjusted 10:1 split poisons
    metrics and any walk-forward split crossing it. Add an adjusted-price
    policy + a `data_quality.yaml` outlier rule (see review §3.3).
13. **Production server:** still Flask dev server; Gunicorn needs
    `--workers 1` until state is externalized (the multi-worker trap, §13 of
    project-overview). Blocked behind #4.
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
2. Live dry-run exercise (T9.5) + F-12 equity fills    ← proves the order path (idempotent + reconciled)
3. Runner-state persistence                            ← survives restarts, unblocks Gunicorn
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
