# UI Walkthrough & QA Report — 2026-10-02

Purpose: a per-tab functional walkthrough of the web app, written for developers
who are about to update or extend a feature. It records what each page is *for*,
the fields it exposes, and the non-functional / cosmetic issues found during a
manual browser sweep on a non-trading day (cosmetic + field-presence + dead-control
+ console/network checks; no live fills exercised).

Environment tested: server on `:5000` from `main` @ `f0563c5`, mStock logged in,
3 paper runners (RELIANCE 1min, all PAUSED), 0 live runners. Browser viewport
~731–1024px wide.

Severity legend:
- 🔴 **Functional** — a page/feature is broken or misrepresents data.
- 🟠 **Notable** — wrong/confusing in a way a user will hit; fix before next feature work.
- 🟡 **Cosmetic** — polish; low risk.

---

## Findings summary (actionable)

| # | Sev | Where | Issue |
|---|-----|-------|-------|
| 1 | 🔴 | `/reporting` | Consolidated P&L returns **HTTP 500** on load: `can't compare offset-naive and offset-aware datetimes`. Only crashes when paper trades are included. |
| 2 | 🔴 | `/portfolio/live`, `/portfolio` | "Open Positions — the live book" panel shows a **PAPER** position on the LIVE page. Bucket isolation leaks in the positions panel. |
| 3 | 🟠 | `/settings` | "Active broker" dropdown exposes **test/foreign broker profiles** (Test Broker, Expensive, Panel Broker, Zero, Generic Discount, Robinhood, TD Ameritrade, Ibkr). Default selected is `panel_broker`. |
| 4 | 🟠 | all pages | Global chrome polls `/api/portfolio/summary` + `/api/data/status` + `/api/alerts/active` every ~5s even on research pages that show none of it; portfolio overview fetches summary 2–3×/cycle. |
| 5 | 🟡 | all pages (narrow) | Top risk strip clips "Deployed: …" below ~800px (`overflow:auto` with no wrap). |
| 6 | 🟡 | portfolio pages | Bar clock prints raw seconds: "last 101789s ago" (~28h). Should humanize. |
| 7 | 🟡 | Broker Board | "Expires: 02:04 AM" has no date and no timezone label. |
| 8 | 🟡 | `/data` | Inventory flashes "0 symbols · 0 bars" before the async fetch resolves. Show a loading state. |
| 9 | 🟡 | `/forward` | Symbol is a free-text box while backtest/compare/optimize use the shared `SymbolPicker`. Consistency gap. |
| 10 | 🟡 | footer | Source tag differs by page ("Historical data" vs "mStock data"); likely intentional per routing, but reads as inconsistency. |
| 11 | 🟠 | command center, risk banner | Misleading timers on market-closed days (fake "next bar in 28s" countdown, browser-clock "Last update", sparkline mislabeled "last 6 hours"). **Signed-off spec split out to [`MARKET-STATUS-SPEC-2026-10-02.md`](MARKET-STATUS-SPEC-2026-10-02.md)** — implement copy verbatim. |

Positive: **no console errors** on any page except the reporting 500; **all API calls 200** except reporting; broker board, risk board, spawn modal, and the research forms all render with sensible, non-redundant fields.

---

## 1. Global chrome (every page)

**What it is:** persistent header + risk strip + alerts. Header carries the
workspace breadcrumb, a global search ("Jump to… Ctrl K"), a **Standby/halt pill**,
the broker connection chip (Dhan / mStock), and a theme toggle. Below it, the risk
strip shows Feed state, Daily Loss, DD, Deployed, Positions, plus `Details` and
`Flatten all`.

**Behavior verified:**
- Standby/halt pill (`#halt-pill`) → on portfolio pages activates the "Risk &
  intelligence" tab and scrolls up; elsewhere navigates to `/risk`. Works.
- Broker chip → opens **Broker Connections** modal (see §7).
- Kill-switch defaults to **OFF (live blocked)** on `/settings` — correct safe default.

**Issues:** #4 (polling), #5 (clip), #10 (footer tag).

---

## 2. `/` and `/backtest` — Backtest

**Purpose:** turn a strategy idea into historical evidence.

**Fields (all relevant, none redundant):** Strategy (select), Instrument
(`SymbolPicker`: All/Equity/Index/F&O tabs + search), Timeframe, From, To, Initial
capital (₹), per-strategy params (e.g. Band Width std dev, Band Period), Fast
preview (checkbox). Header links to Compare and Optimize. Data-source line:
"Real Data (PostgreSQL) · certification-grade".

**Status:** clean. No console/network errors.

---

## 3. `/compare` — Compare strategies

**Purpose:** run up to 3 strategy slots on one instrument/window.

**Fields:** Symbol, From, To, Capital, shared Timeframe, Fast Preview (all slots),
then 3 slots each = Strategy + its params. "Slots" section present.

**Status:** clean.

---

## 4. `/optimize` — Parameter optimization

**Purpose:** grid/random/Bayesian/genetic search over a parameter space.

**Fields:** "1 · Strategy & data" (preset loader, strategy + description, symbol,
timeframe with "only granularities actually stored" hint, From/To, capital);
"2 · Parameters" table = Opt checkbox · Parameter · Current · Min · Max · Step ·
# (combination count, live "2 selected · 55 combinations"); "3 · Objective &
search" = Maximize (Sharpe) + Method (Grid/Random/Bayesian/Genetic).

**Status:** clean and well-designed. Note: `/optimize/runs/<id>` (results
dashboard) not exercised — no completed run in this environment.

---

## 5. `/data` — Market data manager

**Purpose:** fetch historical OHLCV into Postgres and inspect coverage.

**Fields:** "Fetch historical data" = Timeframe, From, To, Instruments picker
(tabs/search/multi-select), Start/Stop; "Data inventory" = Refresh + totals
(symbols/bars/timeframes) + per-symbol table.

**Verified:** `/api/data/inventory` returns 205 symbols / 3,328,345 bars and the
table renders correctly once loaded.

**Issues:** #8 (flash of "0 symbols" before fetch resolves).

---

## 6. `/portfolio`, `/portfolio/paper`, `/portfolio/live` — Capital & execution

**Purpose:** the trading command center. Overview = both buckets side by side;
paper/live = bucket-scoped (`data-mode` on `#portfolio-page`).

**Shared center contains:** command-center header (Add instance, Pause/Resume all,
Export charts, Emergency flatten [live only]); metric cards (Total Capital, Equity,
Deployed, Daily/Realized P&L, Open Positions, Daily Loss Limit); Strategy instances
table (search + status filter + sort); tabs Positions / Equity / Orders / Risk &
intelligence / Trade history / Playbooks / Activity log.

**Spawn (Add instance) modal — routing-only, 15 fields, conditional display:**
Instance name (optional), Strategy, Playbook (optional), Target type, Timeframe,
Bucket mode, Data source, Segment (hidden unless configured), Broker, Instrument
(+ hidden option-symbol select), Lots per leg (options), Universe/Pool, Max pool
positions, Allocated capital. Field set is appropriate — no stray trading-logic
fields. Broker dropdown now populates from `/api/broker/list` (the `symbol_picker.js`
fix from `f0563c5` is confirmed working: mStock + Dhan present).

**🔴 Issue #2 — bucket isolation leak (positions panel):**
On `/portfolio/live`, "Open Positions — the live book" rendered a position tagged
`PAUSED · paper · options_index` (`buy_and_hold·RELIANCE·1min`), while the instance
table above correctly showed "No instances deployed in this bucket". The page's own
footer states "other buckets never appear here".

Root cause: `render(p)` in `static/js/portfolio.js` filters `p.runners` by
`PAGE_MODE` (line ~723) but then `renderAggregatePositions(p)` takes the
`p.positions` fast-path (line ~535) → `renderPositionRows(p.positions)` which is
**not** filtered by bucket. The SSE `/api/portfolio/stream` frame carries a combined
`positions` array where each row has a `mode` field (verified: `mode:"paper"`).

Fix direction: filter `p.positions` by `(x.mode||"paper") === PAGE_MODE` alongside
`p.runners`, mirroring the existing runner filter. Same leak applies to the overview
page's embedded LIVE center.

---

## 7. Broker Connections modal (header chip)

**Purpose:** show every broker's session + segments from one call.

**Content verified:** Dhan (Not connected, segment "Equity Intraday (Paper, ₹8L)",
Login); mStock (Authenticated, "remembered today", segment "Index Options (Paper,
₹12L)", Logout + Reconcile, "UI" active badge). Refresh + close present.

**Issues:** #7 (expiry lacks date + tz).

---

## 8. Risk Board / `/risk` — Risk management

**Purpose:** exposure, limit usage, kill-switch state.

**Content verified:** Global Risk Dashboard cards = Daily Loss Used (1.9%),
Drawdown (0.0%, "Peak protected"), Capital Deployed (9.0%), Gross Exposure (31.6%),
Open Positions (1), System Status (LIVE). "All Buckets" filter, Refresh, Export CSV.
Collapsible sections: Portfolio Greeks, Concentration, Strategy Correlation, Market
Regime, Market Activity (OI & liquidity), Stress Test (−25%), Reset Breaker.

**Status:** clean and comprehensive.

---

## 9. `/analytics` — Strategy analytics

**Purpose:** performance/execution/risk view across paper and live.

**Content verified:** Overview tab; Period (Last 30 Days) + Mode (All Modes)
filters; Portfolio Overview = Total Return, Portfolio Sharpe (−1.55 "Poor"),
Win Rate (40%, "5 trades 2W/3L"), Max Drawdown; "0 / 3 Active Strategies".
Compare backtests + Refresh actions.

**Status:** clean.

---

## 10. `/reporting` — Consolidated P&L   🔴 BROKEN

**Purpose:** every broker/both books → one statement (gross → costs → net → tax).

**Fields:** From, To, Brokers (free text "All brokers, or comma-separated names"),
Include paper trades (checkbox), Generate report; outputs = PDF statement, ITR
annexures (xlsx), Trade ledger (csv), Email (dry run), demo book toggle.

**🔴 Issue #1 — 500 on load (page auto-generates and crashes):**
`GET /api/reporting/pnl/consolidated?…&include_paper=true` → 500, UI shows
"report generation failed: can't compare offset-naive and offset-aware datetimes".

Traceback: `src/backtest/reporting/consolidator.py:317`
```python
report.trades = sorted(selected, key=lambda t: (t.exit_time or datetime.min, t.symbol))
```
The sort compares a tz-aware `exit_time` against the naive `datetime.min` sentinel
and/or against naive timestamps from another source.

Diagnostic (reproduced via curl): `include_paper=false` → **works**;
`include_paper=true` → **crashes**. So paper trade timestamps and broker/live trade
timestamps disagree on tz-awareness, and mixing them breaks the sort.

Fix direction: normalize every `exit_time` to a single awareness (e.g. coerce to
UTC-aware) before sorting, and use an aware sentinel (or sort with `None` handled
separately). This is the top item to fix — the page is unusable with paper trades on.

---

## 11. `/forward` — Engine playground

**Purpose:** single-strategy paper replay for engine diagnostics (not a trading book).

**Fields:** Strategy, Symbol (free text), Timeframe, Run mode, Data source, Capital,
Replay speed, From, To. Clear "Diagnostics only · paper replay without portfolio
bucket-risk controls" banner.

**Issues:** #9 (free-text symbol vs shared SymbolPicker). Minor: the default
strategy shown was an options strategy (`atm_instant_buy`) while the symbol
placeholder suggests equities ("e.g. MAZDOCK, INFY") — consider tying the symbol
hint to the selected strategy's instrument kind.

---

## 12. `/settings` — Workspace settings   🟠

**Purpose:** broker cost models, execution segments, safeguards.

**Content verified:** Global live kill-switch (OFF = live blocked) + Arm live
execution; "In use (3)" broker cost cards (Dhan, Mstock shown, with
config/brokers.yaml source, segment, contract-note validation status, STT/GST
rates); collapsed "12 more available".

**🟠 Issue #3 — test/foreign brokers in the "Active broker" selector:**
`#settings-active-broker` options include `test_broker`, `expensive`,
`panel_broker`, `zero`, `generic_discount`, `robinhood`, `td_ameritrade`, `ibkr`.
These are fee-model presets (built-in + `config/brokers.yaml`), not execution
venues, and several are US brokers or obvious test fixtures. The **currently
selected value is `panel_broker`** (a test preset) even though `config/brokers.yaml`
sets `active_broker: zerodha`. Display names for the auto-derived entries are
naive `.title()` ("Ibkr", "Td Ameritrade").

Fix direction: gate the selector to real/connected brokers (or at least hide the
test fixtures and non-IN venues), and make the default reflect the config's
`active_broker` rather than a test preset.

---

## 13. `/strategy-builder` — Pine Script converter

**Purpose:** TradingView Pine v5 → Python strategy plugin.

**Fields:** Strategy name (optional), Segment (required, "Choose a segment…"),
Pine Script v5 source (textarea with `//@version=5` placeholder), Converted Python
strategy (output pane).

**Status:** clean. The "Segment required" gate (from `238f993`) is present.

---

## Notes for whoever picks these up

- **Finding #11's enhancement spec** (holiday feed gate + market_holidays +
  header chip + verbatim UI copy, signed off by the operator 2026-10-02) lives in
  its own file: `MARKET-STATUS-SPEC-2026-10-02.md` — review and implement that
  separately from the bug fixes here.
- #1 and #2 are the two real functional bugs; both have exact file:line and a
  reproduction. Fix #1 first (reporting is fully broken with paper on).
- #2's fix is one line in `portfolio.js` `render()`; add a test that a live-scoped
  render drops paper positions (there's already a `test_options_spawn_ui.py` pattern
  for "component loaded on every page" — a bucket-isolation assertion would fit).
- #3 and #4 are the kind of thing to fold into the next feature touching Settings /
  the base layout, not standalone.
- Everything else is cosmetic polish.
