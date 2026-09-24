# Runner FAQ & UI Gaps — live session notes (2026-09-21)

### GAP-4 (found 2026-09-21, ~11:30 IST): runner trade data is NOT persisted — ✅ RESOLVED (2026-09-22)
- ~~**Answer to "are we storing trades in a DB?": NO.**~~ **Now YES, both layers live:**
  1. `PORTFOLIO_STATE_PATH=data/portfolio_state.json` **is set in `.env`** — runners + books
     survive restarts (restore as PAUSED, resume manually).
  2. `LiveTradePersister` is wired into the live `PortfolioManager` → every fill/trade lands
     in PostgreSQL. ⚠️ **It was silently failing on every write until 2026-09-22**:
     `_ensure_portfolio_row` read `runner.portfolio.cash`, but the simulator `Portfolio`
     exposes `current_cash` — so every persist raised `AttributeError` (fail-soft logged only).
     Fixed in `trade_persistence.py` (defensive getattr, verified INSERTs execute). Closed
     trades from before 2026-09-22 were never persisted — history effectively starts today.
- What EXISTED but was not wired for the live portfolio (now wired):
  - `backtest/db/` — a full SQLAlchemy `DatabaseManager` (PostgreSQL per `.env`
    `FORWARD_TEST_DB_URL=postgresql+psycopg2://...localhost:5432/forward_test`)
    with `trades`, `fills`, `orders`, `equity_curve` tables. Used by the
    **backtest engine**, never by the live `PortfolioManager`.
  - `StrategyRunner(db=...)` accepts a DatabaseManager — but the web manager
    constructs runners WITHOUT one (`portfolio_manager.py:510`).
  - `PortfolioStateStore` (`PORTFOLIO_STATE_PATH`) — JSON snapshot persistence
    (P2.4, built + 16 tests) — but `PORTFOLIO_STATE_PATH` is **not set** in `.env`.
  - `forward_test.db` (SQLite, repo root, last written Aug 23) — stale artifact,
    all tables empty.
- **Fix path (two layers, both cheap):**
  1. Set `PORTFOLIO_STATE_PATH=data/portfolio_state.json` → runners + books
     survive restarts (restore as PAUSED, resume manually).
  2. Wire `DatabaseManager` into `StrategyRunner` (and/or persist closed
     structures in the manager) → every fill/trade/equity point lands in
     PostgreSQL for cross-session history and analytics.

Working reference from today's successful live-data session (`immediate_entry·NIFTY` traded real premiums with TP₅/SL₅ exits and re-entry). Plus three UI gaps found while exploring the tabs — to improve later.

---

## Part 1 — How runners work (Q&A)

### 1. Runner lifecycle — runs until stopped
- A runner is a **loop**: feed polls a bar every ~60s → strategy evaluated on every bar → bridge re-enters after each exit.
- Re-entries per day are capped by `max_reentries_per_day` (default 2) in the expression's exit config.
- Stop via Portfolio → runner control (pause / stop / remove).
- ⚠️ **Server restart kills runners** — RESOLVED via P2.4 persistence (`PORTFOLIO_STATE_PATH` now set in `.env`): runners come back **PAUSED** (fail-closed by design) — resume explicitly.

### 2. Where to see closed and running trades
- **Portfolio page → runner row → deep-dive drawer**:
  - `open_structures_detail` — running legs: entry/current price, live P&L
  - `closed_structures` — every completed trade: realized P&L + `exit_reason` (target / stop / signal_flip / expiry)
- Raw JSON: `GET /api/portfolio/runner/<instance_id>`

### 3. What Playbooks / Manual Options book / Master Audit log are for
| Feature | Purpose |
|---|---|
| **Playbooks** | Own *risk & exits* so strategies own only *direction*. Versioned + snapshotted into each runner for reproducibility. The `expression.exit` block (TP/SL) IS the playbook mechanism. |
| **Manual Options book** | Isolated paper book for hand-placing option structures from the UI — test execution/pricing/exit machinery without a runner. |
| **Master Audit log** | Append-only portfolio-action record (`SPAWN`, control changes, emergency stop) for forensics — separate from trade logs. |

### 4. Audit log status: WORKING (spawn verified live)
- Live evidence 2026-09-21: `[AUDIT] scope=paper action=SPAWN immediate_entry NIFTY TP5SL5 instance_id=685492f9`
- ✅ SPAWN proven in production conditions. ⚠️ control-change / emergency-stop scopes tested only in tests, not exercised live.
- ⚠️ **UI GAP #1: nothing shows under the Audit tab** — backend emits, frontend doesn't render (or reads a different source). To fix.

### 5. Combined equity curve — value and limits
**Designed to teach:**
- Portfolio-level drawdown (all runners losing together)
- Diversification effect (A's losing days vs B's winning days)
- Cost drag across the book

**Why it looked meaningless today:** 1-min coin-flip validator = pure noise, no edge to aggregate, per-second ticking adds nothing.

**Owner's verdict (recorded):** should be **on-demand/snapshot-based** (per-day close, per-session summary, runner comparison) rather than a continuously running line. Continuous line only earns keep for live risk monitoring (sudden drawdown → kill switch).
- ⚠️ **Design change proposed** — snapshot views instead of ticking curve.

---

## Part 2 — UI gaps to improve (found 2026-09-21)

### Triage (owner decision, 2026-09-21 11:46 IST)
**FIX NOW (live-market dependent — wrong data / data loss while trading):**
- GAP-4: trade persistence (memory-only book → restart wipes history) — ✅ FIXED (2026-09-22: state path enabled + Postgres persister fixed; see GAP-4 above)
- mStock 408 rate-limit resilience (bars/quotes dropped during transient failures) — ✅ FIXED (one retry + backoff in `mstock_live_feed.py`)

**PARKED (cosmetic or synthetic-testable — UI/UX, no live-data risk):**
- GAP-1: empty Audit tab — FIXED (see below)
- GAP-2: playbook "pick NIFTY or BANKNIFTY" error — FIXED (see below)
- GAP-3: Manual Options book — ✅ REMOVED (2026-09-22, owner decision "remove it"): `/options` page, nav link, Manual Options Book tab, banner, and options.js deleted. Manual option trading lives in Portfolio → Playbooks via runner instances.
- On-demand equity snapshots — ✅ DONE (2026-09-22): `GET /api/portfolio/equity/snapshot` + "↻ Refresh Snapshot" button render session summary, day-close series, and per-runner comparison on demand; SSE no longer appends equity points per frame. Portfolio win-rate aggregation fixed (was summing per-runner fractions).
- Hot strategy upload (loader work, testable with synthetic strategies)

### GAP-1: Audit tab is empty — ✅ FIXED (2026-09-21 evening)
- **Symptom:** Master Audit log tab shows nothing.
- **Evidence backend works:** AUDIT SPAWN lines in server log.
- **Root cause (TWO stacked bugs):**
  1. **Frontend:** `renderAudit()` only showed browser-session events from clicks in that tab — it never called the audit API.
  2. **Backend (found while verifying):** `get_audit_log(scope="all")` treated `"all"` as a literal scope — no entry ever has scope `"all"`, so `?scope=all` returned `[]` forever. Contract says `all` = no filter. Fixed in `portfolio_manager.py`.
- **Fix:** tab-open now fetches `/api/portfolio/audit?scope=all&limit=200` and renders backend entries merged with live `addAudit` events on top; backend honors `all` as no-filter.
- **Tests:** `tests/test_audit_tab.py` (5 synthetic) + existing audit suite green. Verified in preview: 4 real SPAWN entries render, a live PAUSE control action appears at top immediately.

### GAP-2: Playbook strategy deploy errors: "Option strategies trade an index — pick NIFTY or BANKNIFTY" — ✅ FIXED (2026-09-21 evening)
- **Symptom:** deploying a playbook-backed strategy rejects the symbol.
- **Suspect:** playbook path validates `underlying` against a hardcoded allowlist and the UI passes something else (symbol case, universe name, or equity symbol).
- **Impact:** blocks the main playbook workflow.
- **Root cause (verified in code):** `portfolio.js` `submitSpawn()` checked `body.symbol` against `OPTION_INDEXES` BEFORE reading `spawn-symbol` into the body — the symbol was only attached *after* the check, so it was always `""` and every option deploy failed. Two fixes:
  1. **Frontend:** symbol is read into the body before validation (guard test pins the ordering).
  2. **Backend (defense in depth):** `/api/portfolio/runner/create` now rejects option runners on non-index symbols with the same message — previously it accepted RELIANCE + option instrument, which would spawn an inert runner.
- **Tests:** `tests/test_option_symbol_validation.py` (5 synthetic, incl. a source-ordering regression guard); verified end-to-end in the preview — NIFTY option runner spawns (201), RELIANCE option runner rejected (400), equity runner on RELIANCE unaffected.

### GAP-3: Manual Options book has no buttons / unclear intent — ✅ RESOLVED: REMOVED (2026-09-22)
- **Decision:** owner chose **remove** over rebuild — runners/playbooks supersede hand-placed trades.
- **What was deleted:** `/options` page route + template, `static/js/options.js`, nav link, Portfolio "📦 Manual Options Book" tab + banner + aggregate-rows wiring, dashboard-book JS in playbooks.js.
- **What was KEPT:** `options_api.py` JSON endpoints + the book singleton — the portfolio manager still merges the legacy book into summaries and Emergency Flatten still flattens it (so any pre-existing manual positions remain visible/closeable via API and flatten).
- **Tests:** `tests/test_options_web.py` now pins the page as 404 and the nav link as gone.

---

## Session evidence (what "works" means here)
- 2026-09-21 09:51:02 — ENTRY long_call NIFTY 23400 CE @ ₹154 real premium, lot 65
- 2026-09-21 09:51:58 — EXIT target ₹68 ≥ ₹5 → **+₹58.50**
- 2026-09-21 09:52:58 — RE-ENTRY automatically
- 2026-09-21 09:53:58 — EXIT stop −₹182 ≤ −₹5 → **−₹191.75**
- Quote fix that enabled it: mStock LTP endpoint keys on trading symbol (`NFO:NIFTY26SEP23300CE`), not numeric token.
