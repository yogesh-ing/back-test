# Refactor: Separate Live and Paper in Portfolio Command Center

**Author:** Nirvika
**Date:** 2026-09-02
**Status:** ✅ Implemented (Phases 1-4 complete)
**Scope:** Portfolio UI (templates + JS) + Portfolio API (portfolio.py) + PortfolioManager backend

**Commits:**
- `e57a4ed` Phase 1 backend: per-bucket state, breakers, scoped API, 75 new tests
- `9ca7025` Phase 2-4 frontend: scoped metrics, redesigned Overview, accent colors, AC-16
- `15713e9` Design doc updated with completion status
- `a8b144d` ← Overview back link on scoped pages

---

## 1. Problem Statement

The Portfolio Command Center currently displays **Live** and **Paper** strategy instances together in a single combined view. The equity curve, aggregate metrics (Total Equity, Daily P&L, Realized P&L), risk limits, and instance matrix all blend both buckets into one number.

This creates a **dangerous cognitive load** in live markets:

- A trader watching the equity curve sees ₹10,20,000 but cannot instantly tell how much is from real (live) positions vs simulated (paper) positions.
- Daily P&L of +₹5,000 may include ₹8,000 from paper trades and -₹3,000 from live — the trader thinks they are profitable when they are actually losing real money.
- Emergency actions (Pause All, Emergency Flatten) affect both buckets simultaneously — flattening paper positions is harmless, but flattening live positions executes real broker orders.
- The circuit breaker halts the entire portfolio when a paper trade triggers the loss limit, potentially stopping profitable live strategies.

**The core design flaw:** In a trading system, the view that matters most — the one you rely on when real money is at risk — should be the **default and safest view**. Currently, the safe view (live-only) requires conscious effort to find, while the confusing view (combined) is what you see first.

---

## 2. Current Architecture

### 2.1 File Structure

```
src/backtest/web/templates/
  portfolio.html              # Landing page — bucket cards + combined command center
  portfolio_paper.html        # Paper summary table + command center (data-mode="paper")
  portfolio_live.html         # Live summary table + command center (data-mode="live")
  _portfolio_center.html      # Shared command center component (included by all 3)
  base.html                   # Nav bar — single "Portfolio" link

src/backtest/web/static/js/
  portfolio.js                # All command center JS — matrix, chart, SSE, spawn modal

src/backtest/api/
  portfolio.py                # REST + SSE API — summary, stream, spawn, control

src/backtest/forward/
  portfolio_manager.py        # Backend engine — runners, aggregation, risk
```

### 2.2 How It Works Today

**Routing:**
| Route | Template | `data-mode` | Behavior |
|-------|----------|-------------|----------|
| `GET /portfolio` | `portfolio.html` | `""` (empty) | Shows bucket cards + combined command center |
| `GET /portfolio/paper` | `portfolio_paper.html` | `"paper"` | Shows paper summary table + command center filtered to paper |
| `GET /portfolio/live` | `portfolio_live.html` | `"live"` | Shows live summary table + command center filtered to live |

**Backend:**
- `GET /api/portfolio/summary` — returns ALL runners aggregated; `?mode=paper|live` filters server-side
- `GET /api/portfolio/stream` (SSE) — broadcasts FULL combined snapshot every 1 second; no server-side mode filter
- `POST /api/portfolio/runner/create` — spawns a runner in whichever bucket the form selects
- `POST /api/portfolio/control/<action>` — bulk actions affect ALL runners regardless of bucket

**Frontend (`portfolio.js`):**
- Connects to SSE stream, receives combined snapshot
- In `render()`, filters runners by `PAGE_MODE` client-side: `p.runners = p.runners.filter(r => r.mode === PAGE_MODE)`
- But **metrics** (`renderMetrics`) use the pre-filter `p` object — so Total Equity, Daily P&L, etc. always show the combined total
- Equity curve (`_pccEquity`) is a single combined series — always shows `p.total_equity`
- Matrix, positions, and audit log are filtered to the page's mode

### 2.3 What's Already Separated

- `data-mode` attribute on `#portfolio-page` — the JS reads this to filter the runner list
- The `get_portfolio_summary(mode)` method in `PortfolioManager` already supports server-side bucket filtering
- The spawn modal already has a "Bucket mode" dropdown (paper/live)
- Bucket risk limits are already separate (`BUCKET_RISK_LIMITS` in `bucket_risk.py`)
- Paper landing has a summary table; Live landing has a summary table — these are separate

### 2.4 What's NOT Separated (The Problem)

| Component | Current State | Issue |
|-----------|---------------|-------|
| **Metrics bar** | Always shows combined | Total Equity mixes live + paper money |
| **Equity curve** | Single combined line | Cannot distinguish real vs simulated P&L |
| **Circuit breaker** | Single global halt | Paper loss halts live trading |
| **Emergency Flatten** | Affects all runners | Flattening paper is wasted panic |
| **Bulk actions** (Pause All, Resume All) | Affects all runners | Pausing live to pause paper is dangerous |
| **Deployed capital** | Combined total | Hides how much real money is at risk |
| **Daily loss limit bar** | Combined usage | Paper losses eat into the live loss budget |

---

## 3. Proposed Design

### 3.1 Principle

> **When real money is on the line, the safe thing should be the easy thing.**

- **Live** is the default view when you click "Portfolio"
- **Paper** is a separate workspace for experimentation
- **Combined** is an opt-in view for strategy development — never the default
- Bulk actions and emergency controls are scoped to the current view

### 3.2 New Navigation Structure

```
Top Nav:  [Dashboard] [Backtest] [Compare] [Forward Test] [Portfolio ▼] [Data]
                                                         ├─ 📊 Overview    ← /portfolio (redesigned)
                                                         ├─ 🔴 Live        ← /portfolio/live
                                                         └─ 📄 Paper       ← /portfolio/paper
```

The single "Portfolio" nav link becomes a dropdown (or stays as a link to the redesigned Overview page). The Overview page becomes the **safe default** — it shows Live as the primary view with Paper as a secondary section.

### 3.3 New Page Layouts

#### 3.3.1 Overview Page (`/portfolio`) — Redesigned

**Purpose:** At-a-glance status of everything, but **Live is prominent, Paper is secondary.**

```
┌─────────────────────────────────────────────────────┐
│  📊 Portfolio Overview                              │
│  ┌──────────────────┐  ┌──────────────────┐         │
│  │ 🔴 LIVE           │  │ 📄 PAPER          │        │
│  │ ₹5,00,000 equity  │  │ ₹3,00,000 equity │        │
│  │ +₹12,400 daily    │  │ +₹3,200 daily    │         │
│  │ 3 running         │  │ 5 running         │        │
│  │ [View Live →]     │  │ [View Paper →]    │        │
│  └──────────────────┘  └──────────────────┘         │
│                                                     │
│  ┌─ Live Equity Curve (standalone) ──────────────┐  │
│  │  [line chart — live only]                      │  │
│  └───────────────────────────────────────────────┘  │
│                                                     │
│  ┌─ Live Instance Matrix ────────────────────────┐  │
│  │  [table — live runners only]                   │  │
│  └───────────────────────────────────────────────┘  │
│                                                     │
│  ┌─ Paper Equity Curve (collapsed/secondary) ───┐  │
│  │  [line chart — paper only, smaller]           │  │
│  └───────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────┘
```

**Key changes:**
- Live and Paper get **separate metric cards** with separate equity, P&L, and runner counts
- Live equity curve is the **primary chart** (larger, prominent)
- Paper equity curve is secondary (smaller, below)
- No "Combined" metrics — you see each bucket's numbers independently
- "＋ Add Instance" button appears on both Live and Paper sections, defaulting to the respective bucket

#### 3.3.2 Live Page (`/portfolio/live`) — The Command Center

**Purpose:** The operational view for live trading. Zero paper contamination.

```
┌─────────────────────────────────────────────────────┐
│  🔴 Live Portfolio Command Center                   │
│  ⚠️  REAL MONEY — only live instances appear here   │
│                                                     │
│  [Metric cards: Total Capital, Equity, Deployed,    │
│   Daily P&L, Realized P&L, Open Positions,          │
│   Daily Loss Limit]                                 │
│                                                     │
│  [＋ Add Live Instance]  [⏸ Pause All]  [▶ Resume]  │
│  [🔴 Emergency Flatten]                             │
│                                                     │
│  [Instance Matrix — live runners only]              │
│  [Combined Equity Curve — live only]                │
│  [Aggregate Open Positions — live only]             │
│  [Master Audit Log — live actions only]             │
└─────────────────────────────────────────────────────┘
```

**Key changes:**
- All metrics, charts, and tables show **live runners only**
- Emergency Flatten only flattens **live positions** (real broker orders)
- Pause All / Resume All only affect **live runners**
- Circuit breaker is **per-bucket** — a paper breach does not halt live trading
- Visual warning banner: "REAL MONEY" — reinforces that this is the real view

#### 3.3.3 Paper Page (`/portfolio/paper`) — The Sandbox

**Purpose:** Experimentation and strategy tuning. No real money at risk.

```
┌─────────────────────────────────────────────────────┐
│  📄 Paper Portfolio — Sandbox                       │
│  Simulated fills only — no real money at risk        │
│                                                     │
│  [Metric cards: Total Capital, Equity, Deployed,    │
│   Daily P&L, Realized P&L, Open Positions]          │
│                                                     │
│  [＋ Add Paper Instance]  [⏸ Pause All]  [▶ Resume] │
│                                                     │
│  [Instance Matrix — paper runners only]             │
│  [Combined Equity Curve — paper only]               │
│  [Aggregate Open Positions — paper only]            │
│  [Master Audit Log — paper actions only]            │
└─────────────────────────────────────────────────────┘
```

**Key changes:**
- Relaxed tone — "Sandbox" framing
- No Emergency Flatten button (nothing to panic about with fake money)
- Circuit breaker still works but only halts paper runners
- Can freely experiment without affecting live state

---

## 4. Technical Changes Required

### 4.1 Backend — `portfolio_manager.py`

| Change | Description | Priority |
|--------|-------------|----------|
| **Per-bucket equity tracking** | Add `_bucket_equity: Dict[str, float]` and `_bucket_peak: Dict[str, float]` to track equity/peak per bucket separately | P0 |
| **Per-bucket circuit breaker** | Add `_bucket_halted: Dict[str, bool]` — a paper halt does not affect live runners | P0 |
| **Per-bucket daily P&L anchors** | `_bucket_day_start: Dict[str, float]` — daily loss is measured per bucket | P0 |
| **Scoped `get_portfolio_summary()`** | When `mode` is provided, return metrics computed ONLY from that bucket's runners (already partially done — extend to equity/peak/drawdown) | P0 |
| **Scoped emergency flatten** | `emergency_flatten_all(mode=)` — flatten only one bucket | P1 |
| **Scoped bulk actions** | `pause_all(mode=)`, `resume_all(mode=)` — affect only one bucket | P1 |
| **Combined summary (opt-in)** | `get_portfolio_summary(mode=None)` still works for the Overview page — but it's no longer the default view | P2 |

### 4.2 API — `portfolio.py`

| Change | Description | Priority |
|--------|-------------|----------|
| **Add `?mode=` to SSE stream** | `/api/portfolio/stream?mode=live` — server-side filtered SSE so the live page only receives live data | P0 |
| **Add `?mode=` to bulk control** | `POST /api/portfolio/control/pause_all?mode=live` — scoped bulk actions | P1 |
| **Add `?mode=` to emergency stop** | `POST /api/portfolio/emergency_stop?mode=live` — scoped emergency | P1 |
| **New endpoint: `/api/portfolio/buckets`** | Returns per-bucket summary (equity, P&L, runner count) for the Overview page | P1 |

### 4.3 Frontend — `portfolio.js`

| Change | Description | Priority |
|--------|-------------|----------|
| **Metrics render from filtered data** | When `PAGE_MODE` is set, `renderMetrics()` computes from the filtered runners, not the full `p` object | P0 |
| **Separate equity curves** | Overview page renders two charts (live + paper). Scoped pages render one chart (their bucket only) | P0 |
| **Scoped emergency flatten** | Emergency button sends `mode=PAGE_MODE` to the API | P0 |
| **Scoped bulk actions** | Pause All / Resume All send `?mode=PAGE_MODE` | P0 |
| **Remove Emergency Flatten from Paper page** | Paper page does not show the Emergency Flatten button | P1 |
| **Visual distinction** | Live page gets red accent / warning banner. Paper page gets blue/neutral accent | P1 |
| **SSE URL includes mode** | Scoped pages connect to `/api/portfolio/stream?mode=<mode>` | P0 |

### 4.4 Templates

| File | Change | Priority |
|------|--------|----------|
| `portfolio.html` | Redesign as Overview — two bucket cards with separate metrics, two equity curves, links to scoped pages | P0 |
| `portfolio_live.html` | Add "REAL MONEY" warning banner. Remove paper-related elements. Emergency Flatten scoped to live | P0 |
| `portfolio_paper.html` | Add "Sandbox" framing. Remove Emergency Flatten button. Relaxed tone | P0 |
| `_portfolio_center.html` | Make emergency/bulk buttons aware of `center_mode` — hide or scope them | P0 |
| `base.html` | Add dropdown or sub-nav for Portfolio (Overview / Live / Paper) | P1 |

### 4.5 CSS (`app.css`)

| Change | Description | Priority |
|--------|-------------|----------|
| **Live accent** | Red/orange accent for live metrics and headers | P1 |
| **Paper accent** | Blue/neutral accent for paper metrics and headers | P1 |
| **Overview layout** | Two-column bucket cards, stacked equity curves | P1 |

---

## 5. Data Flow (After Refactor)

### 5.1 Live Page

```
Browser                          Server
  │                                │
  │── GET /portfolio/live ────────→│  Renders portfolio_live.html (data-mode="live")
  │←── HTML ──────────────────────│
  │                                │
  │── GET /api/portfolio/stream?mode=live ──→│  SSE: only live runner snapshots
  │←── event: portfolio ──────────│  { runners: [live-only], metrics: live-only }
  │                                │
  │── renderMetrics(live_data) ───│  Shows live equity, live P&L only
  │── renderChart(live_data) ─────│  Live equity curve only
  │── renderMatrix(live_data) ────│  Live runners in matrix only
```

### 5.2 Overview Page

```
Browser                          Server
  │                                │
  │── GET /portfolio ─────────────→│  Renders portfolio.html (Overview)
  │←── HTML ──────────────────────│
  │                                │
  │── GET /api/portfolio/buckets ─→│  Returns { paper: {...}, live: {...} }
  │←── JSON ──────────────────────│
  │                                │
  │── GET /api/portfolio/stream ──→│  SSE: combined snapshot
  │←── event: portfolio ──────────│  JS splits into live/paper for separate charts
```

---

## 6. Migration / Compatibility

- **No breaking API changes.** `GET /api/portfolio/summary` without `?mode=` still returns the combined view — the Overview page uses this.
- **No DB changes.** All state is in-memory; the refactor is purely in aggregation logic and UI.
- **Existing tests** test `get_portfolio_summary()` without mode — these continue to pass.
- **New tests** needed for per-bucket equity/peak tracking and scoped bulk actions.

---

## 7. Acceptance Criteria

- [x] **AC-1:** Clicking "Portfolio" in the nav shows the Overview page with separate Live and Paper sections
- [x] **AC-2:** The Live page shows ONLY live runners — zero paper contamination in metrics, charts, matrix
- [x] **AC-3:** The Paper page shows ONLY paper runners — zero live contamination
- [x] **AC-4:** Emergency Flatten on the Live page only flattens live positions (does not touch paper)
- [x] **AC-5:** Pause All / Resume All on a scoped page only affects that bucket's runners
- [x] **AC-6:** A circuit breaker trip on paper does NOT halt live trading
- [x] **AC-7:** A circuit breaker trip on live does NOT halt paper trading
- [x] **AC-8:** The Live page has a visual warning (red accent / "REAL MONEY" banner)
- [x] **AC-9:** The Paper page has a visual distinction (blue accent / "Sandbox" banner)
- [x] **AC-10:** Equity curves are bucket-scoped — live chart shows only live equity, paper chart shows only paper equity
- [x] **AC-11:** The Overview page shows both buckets' summary side-by-side
- [x] **AC-12:** SSE stream carries embedded bucket data — no `?mode=` needed on SSE (C4)
- [x] **AC-13:** All existing tests pass (no regressions) — 116 portfolio tests, 1,700+ full suite

---

## 8. Task Decomposition (for Architect)

### Phase 1: Backend Separation (P0)

| Task | Description | Files | Estimate |
|------|-------------|-------|----------|
| **T1.1** | Add per-bucket equity/peak/day-start tracking to `PortfolioManager` | `portfolio_manager.py` | 2h |
| **T1.2** | Make `get_portfolio_summary(mode)` compute metrics from filtered runners only (equity, peak, drawdown, daily P&L) | `portfolio_manager.py` | 2h |
| **T1.3** | Add per-bucket circuit breaker (halt per bucket, not globally) | `portfolio_manager.py`, `risk_supervisor.py` | 3h |
| **T1.4** | Add `?mode=` support to SSE stream (server-side filter) | `portfolio.py` | 1h |
| **T1.5** | Add `?mode=` to bulk control endpoints (pause_all, resume_all) | `portfolio.py` | 1h |
| **T1.6** | Add `?mode=` to emergency stop endpoint | `portfolio.py` | 1h |
| **T1.7** | Add `GET /api/portfolio/buckets` endpoint for Overview page | `portfolio.py` | 1h |
| **T1.8** | Write tests for per-bucket equity tracking and scoped bulk actions | `tests/` | 3h |

### Phase 2: Frontend Separation (P0)

| Task | Description | Files | Estimate |
|------|-------------|-------|----------|
| **T2.1** | Fix `renderMetrics()` to compute from filtered runners when `PAGE_MODE` is set | `portfolio.js` | 2h |
| **T2.2** | Make equity curve bucket-scoped (separate series per bucket) | `portfolio.js` | 2h |
| **T2.3** | Scope SSE connection to `?mode=` on scoped pages | `portfolio.js` | 1h |
| **T2.4** | Scope emergency/bulk buttons to send `mode=` | `portfolio.js` | 1h |
| **T2.5** | Hide Emergency Flatten button on Paper page | `_portfolio_center.html` | 0.5h |

### Phase 3: Template Redesign (P1)

| Task | Description | Files | Estimate |
|------|-------------|-------|----------|
| **T3.1** | Redesign `portfolio.html` as Overview with dual bucket sections | `portfolio.html` | 3h |
| **T3.2** | Add "REAL MONEY" warning banner to Live page | `portfolio_live.html` | 1h |
| **T3.3** | Add "Sandbox" framing to Paper page | `portfolio_paper.html` | 1h |
| **T3.4** | Add dropdown/sub-nav for Portfolio in `base.html` | `base.html` | 1h |

### Phase 4: Visual Polish (P1)

| Task | Description | Files | Estimate |
|------|-------------|-------|----------|
| **T4.1** | Live accent colors (red/orange) for metrics and headers | `app.css` | 1h |
| **T4.2** | Paper accent colors (blue/neutral) for metrics and headers | `app.css` | 1h |
| **T4.3** | Overview page two-column layout for bucket cards | `app.css` | 1h |

### Total Estimate: ~30 hours

---

## 9. Open Questions

1. **Should the Overview page show a combined equity curve at all?** Option A: No — only per-bucket curves. Option B: Yes, but as a small "combined" chart below the two bucket charts.
2. **Should paper instances be spawnable from the Live page?** Currently the spawn modal has a bucket selector. If the page is scoped, should the bucket be locked to the page's mode?
3. **Should the circuit breaker be fully independent per bucket, or should a live breach also pause paper as a safety measure?** (I recommend fully independent — paper should never affect live.)
4. **Navigation pattern:** Dropdown under "Portfolio" in the top nav, or keep it as a single link to Overview with tabs/sub-nav inside the page?

---

## 10. Appendix: Current Code References

| File | Key Lines | What It Does |
|------|-----------|--------------|
| `portfolio_manager.py:225-280` | `get_portfolio_summary()` | Aggregates all runners — the core logic to refactor |
| `portfolio_manager.py:200-220` | `_aggregate_equity()`, `_aggregate_daily_pnl()` | Combined aggregation — needs per-bucket versions |
| `portfolio.js:195-210` | `render(p)` | Client-side filtering — metrics bypass the filter |
| `portfolio.js:260-290` | `renderChart(p)` | Single combined equity curve |
| `portfolio.py:55-65` | `summary()` endpoint | Returns combined or scoped summary |
| `portfolio.py:150-170` | `stream()` endpoint | SSE broadcast — no server-side mode filter |
| `_portfolio_center.html:1-10` | `data-mode` attribute | Existing mechanism for bucket scoping |
