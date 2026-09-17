# Status — What We Have & What Needs To Be Done

**Date:** 2026-09-17 · **Branch:** `main` @ `b39da03` · **Suite:** ~2,435 passed, 7 skipped (mStock credentials), known Windows process-pool flake

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

### Unified trading stack (P0–P7 of the Unified Trading PRD — 21/21 done ✅)
- **Playbooks:** declarative plug-and-play option configs — dataclass + registry
  (thread-safe, JSON persistence via `PLAYBOOKS_PATH`, 3 seeded defaults,
  version auto-bump), 6 API routes, Portfolio-tab card grid UI, spawn snapshot
  (running runners never mutate on playbook edit).
- **Execution engine:** `mode` × `source` routing (paper→simulator,
  live→broker+margin gate), lot size from instrument master (never the
  playbook), pre-trade `max_loss_per_trade` risk cap, C2 data-ownership assert.
- **Two-tier exits:** emergency > stop > target > DTE square-off > signal flip;
  same-bar re-entry impossible; `reenter` default **off** with
  `max_reentries_per_day` (closes the −₹41,844 churn finding).
- **Portfolio integration:** manual options book merged into bucket totals;
  flatten closes both books; per-structure option rows visible read-only on
  Portfolio; audit log with scope; slim 7-field spawn form.
- **Strategy plugins:** drop-in `plugins/strategies/*.py` with AST import-ban,
  conformance battery (determinism, output shape, metadata), templates for
  equity + option strategies — `docs/STRATEGY-AUTHORING.md`.

### Data bus + mStock ✅ (the recent push)
- **Shared Market Data Bus** (`forward/feed_registry.py`): refcounted feeds per
  `(source, symbol, timeframe)`, one shared chain generator per underlying,
  process-wide singletons.
- **mStock live bars into the bus (U7.1):** one background poll thread for all
  mstock symbols, market-hours gate, dedupe, error-soft — one sweep = one API
  call regardless of runner count. Committed as `b39da03`.
- **mStock auth + order layer:** login/TOTP/OTP, session manager, order
  place/modify/cancel, fill polling, multi-leg `LiveOptionTrader`
  (dry-run default). 202 stocks / 467K daily bars + 154K instruments in Postgres.

### Portfolio command center ✅
- Live/Paper buckets with independent circuit breakers, per-bucket derived
  accounting, scoped bulk control, master kill, capability banner, single SSE
  stream, three views (Overview / Live / Paper). 116 portfolio tests.

---

## 2. What needs to be done

### 🔴 P1 — Close the "real data" loop (highest value)
1. **Live option chain + quotes (the last synthetic gap).** Bars now come from
   mStock, but the option chain stack is still synthetic — runner
   `quote_source` reads `synthetic:bs` and options price off BS, not real LTP.
   Wire `MStockClient.get_option_chain()` + `get_option_quote()` /
   `LiveQuoteProvider` into `ChainBus`/`OptionsBridge` when `source=mstock` &&
   authenticated. **~1 day. Unblocks any meaningful options forward test.**
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
7. **Consultant sign-off on 6 open questions** (`docs/consultant quest review.md` §0.4):
   playbook scope (option-only vs equity), risk-envelope V2 (BS+IV+SPAN
   inputs), exit precedence confirmation, re-enter policy, playbook
   versioning, hard-delete metric.

### 🟡 P3 — Engine depth (Roadmap Phase 1–2 leftovers)
8. Position sizing (fixed-fraction / fixed-cash / ATR-target), fill models
   (next-open vs close, pluggable slippage), trade log export (CSV/JSON).
9. Richer metrics: Sortino, expectancy, profit factor, monthly heatmap.
10. Parameter optimization + walk-forward analysis (grid/random search,
    rolling in-sample/out-of-sample), Monte-Carlo / deflated Sharpe.
11. Chain-shape refactor for multi-leg V2 structures (straddles/condors) —
    Phase B, do **not** bundle with anything.

### 🟢 P4 — Platform hygiene (low urgency, real debt)
12. **Orphaned code:** `forward/live_engine.py` (697 lines, zero importers —
    delete or wire it), dead config files (`market_data.yaml`,
    `time_sync.yaml`), legacy `dashboard/app.py` slated for retirement.
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
1. Live option chain/quotes wiring  ← makes forward tests REAL
2. Live dry-run exercise (T9.5)     ← proves the order path
3. Runner-state persistence         ← survives restarts, unblocks Gunicorn
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
| Write a strategy | `docs/STRATEGY-AUTHORING.md` |
| Forward-test experiment log (honest findings) | `docs/OPTIONS-FORWARD-TEST-EXPERIMENT.md` |
| Big-picture plan | `instructions/ROADMAP.md` + `instructions/BACKLOG.md` |
| Playbook architecture sign-off | `docs/consultant quest review.md` |
