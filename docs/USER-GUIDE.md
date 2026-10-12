# User Guide — how to use every page, tab and control

**This is the index.** Each functionality has its own how-to file with the
step-by-step instructions; this page holds the start-up path, the global chrome,
end-to-end workflows and troubleshooting.

Written from the code on 2026-10-04 (branch `main`); re-verified 2026-10-06 for
the data-tab rework and the broker session alerts (branch
`feature/strategy-builder-improvements`).

- Developer reference (templates, JS, endpoints): [WEB-UI.md](WEB-UI.md)
- Why the platform behaves the way it does: [project-overview.md](project-overview.md)
- Architecture: [ARCHITECTURE.md](ARCHITECTURE.md)

---

## 0. Start the app

```powershell
# Windows (see "how to run.md" in the repo root for detail)
$env:PYTHONPATH = "…\src"
python -m backtest.web.app --host 0.0.0.0 --port 5000
```

Open <http://127.0.0.1:5000> and check `GET /health` returns
`{"status":"ok","source":"…"}`. Useful switches (all also env vars —
`.env.example`): `--source` (`synthetic` for a credential-free first run, `db` for
real cached bars), `--log-level DEBUG`, `--log-file logs/app.log`,
`--currency USD`, `--replay-speed 5`.

### A safe first 15 minutes

1. **[Market data](how-to/market-data.md)** — if the instrument you want is missing
   from the dropdowns, fetch it first. A symbol must have data before Research
   pages list it.
2. **[Backtest](how-to/backtest.md)** — pick a strategy + instrument,
   `Run backtest`, read the metric cards and the three chart tabs.
3. **[Compare](how-to/compare.md)** — put two strategies on the same window and see
   which one actually differs.
4. **[Portfolio](how-to/portfolio.md) → PAPER bucket** — `Add instance` to
   paper-trade it on a live clock. Nothing here touches money.
5. Only after that: **[Settings](how-to/settings.md)** (cost models, segments) and
   the LIVE bucket, which requires explicit arming.

---

## 1. How-to index — every page and functionality

### Workspace

| Functionality | Page | How-to |
|---------------|------|--------|
| Portfolio command center (buckets, matrix, spawn, **7 tabs**) | `/portfolio`, `/portfolio/paper`, `/portfolio/live` | [how-to/portfolio.md](how-to/portfolio.md) |
| Strategy analytics (**2 tabs**) | `/analytics` | [how-to/analytics.md](how-to/analytics.md) |
| Risk management (**4 tabs**) | `/risk` | [how-to/risk-management.md](how-to/risk-management.md) |
| Consolidated P&L & tax reporting | `/reporting` | [how-to/pnl-reporting.md](how-to/pnl-reporting.md) |

### Research

| Functionality | Page | How-to |
|---------------|------|--------|
| Backtest | `/`, `/backtest` | [how-to/backtest.md](how-to/backtest.md) |
| Compare strategies (**2 modes, 4 result tabs**) | `/compare` | [how-to/compare.md](how-to/compare.md) |
| Parameter optimization (**5 setup sections, 6 run tabs**) | `/optimize` | [how-to/optimization.md](how-to/optimization.md) |
| Strategy builder (Pine v5 → plugin) | `/strategy-builder` | [how-to/strategy-builder.md](how-to/strategy-builder.md) |

### Operations

| Functionality | Page | How-to |
|---------------|------|--------|
| Engine playground (server-side replay) | `/forward` | [how-to/engine-playground.md](how-to/engine-playground.md) |
| Market data (fetch + inventory) | `/data` | [how-to/market-data.md](how-to/market-data.md) |
| Settings (kill-switch, rates, segments) | `/settings` | [how-to/settings.md](how-to/settings.md) |

### Everywhere

| Functionality | How-to |
|---------------|--------|
| Top bar, risk strip, alert widget, Workspace guide, shortcuts, error toasts | [how-to/global-chrome.md](how-to/global-chrome.md) |

---

## 2. Four end-to-end workflows

### 2.1 Take a strategy from idea to paper

[Market data](how-to/market-data.md) (fetch the symbol) → [Backtest](how-to/backtest.md)
(evidence, check the certification panel) → [Compare](how-to/compare.md) (against a
baseline) → [Optimization](how-to/optimization.md) (only if the shape of the
heatmap is a plateau) → [Portfolio](how-to/portfolio.md) PAPER → `Add instance` →
watch **Positions** and **Activity log**.

### 2.2 Investigate a bad day

Risk strip `Details` (6 h breakdown + top losers) → [/risk](how-to/risk-management.md)
`📜 Audit Timeline` (what tripped, when) → Portfolio **Trade history**
(`Exit Reason`) → **Orders** (slippage, aging) → [Analytics](how-to/analytics.md)
for whether it is one bad day or a dying edge.

### 2.3 Get a brand-new instrument working

Header broker chip → `Login` → [Market data](how-to/market-data.md) → pick the
All/Equity/Index tab and tick instruments (tick none and the active tab's
curated universe is fetched — 200 NSE stocks, the indices, or both) →
`Start fetch` → `Refresh` on inventory to confirm the bars landed → the symbol
now appears in Backtest/Compare/Optimize dropdowns.

### 2.4 Prepare to go live (deliberately)

[Settings](how-to/settings.md) → verify broker profile + contract-note validation →
set segment limits → read the arming checklist → global kill-switch `ON` → LIVE
bucket → `Add instance` → keep **Risk & intelligence** open, and remember
`Emergency flatten` and the risk strip's `Flatten all` are one click away.

---

## 3. Troubleshooting

| Symptom | Likely cause | What to do |
|---------|--------------|------------|
| Symbol missing from a dropdown | No bars stored | [Market data](how-to/market-data.md) → fetch it |
| Toast ends with `[req …]` | Server-side error | Grep that id in `logs/app.log` — [global chrome](how-to/global-chrome.md) |
| `0 trades` / empty results | Window too short, or params never fire | Check From/To and the strategy's parameters; `--log-level DEBUG` |
| Win rate shows `—` | Nothing has closed yet | Correct behaviour — the Trades card says how many are open |
| Forward start refused (403) | No broker session (live mode requires auth) | Re-login via the broker chip; the web replay only paper-trades — live mode is refused by design |
| Broker login expiring / session expired alert | Broker session has < 30 min left, or is dead (feeds and entries pause fail-closed) | Click `Re-login` on the alert (password + TOTP); it closes itself once the session is verified |
| Fetch keeps failing | Broker outage / no session | Re-login via the broker chip; the fetch circuit breaker stops it grinding |
| A live close says `placed` | Venue acknowledgment is asynchronous | Expected — the UI refuses to claim the position is closed |
| Alerts not appearing | Intelligence disabled at boot | Check `PORTFOLIO_INTELLIGENCE_ENABLED` |

**Glossary.** *Bucket* = the paper or live capital partition. *Runner/instance* =
one deployed strategy. *Segment* = capital + broker + mode + risk limits mandate.
*Playbook* = declarative option-strategy config. *Ledger* = server-side history of
runs/orders (survives restarts). *Fast preview* = approximate vectorised engine.

---

## 4. Where the rest of the documentation lives

| I want to… | Read |
|------------|------|
| Know what every page *is* (developer view) | [WEB-UI.md](WEB-UI.md) |
| Understand the engine, costs and no-lookahead rule | [project-overview.md](project-overview.md), [BACKTEST-ENGINE.md](BACKTEST-ENGINE.md) |
| Write a strategy or plugin | [STRATEGY-AUTHORING.md](STRATEGY-AUTHORING.md), [STRATEGY-GUIDELINES.md](STRATEGY-GUIDELINES.md) |
| Operate the command center / endpoints | [PORTFOLIO-CENTER.md](PORTFOLIO-CENTER.md) |
| Trade options (paper, live, backtest) | [OPTIONS-PAPER-LIVE.md](OPTIONS-PAPER-LIVE.md), [OPTIONS-BACKTEST-PRD.md](OPTIONS-BACKTEST-PRD.md) |
| Add a data source or a table | [DATA-SOURCES.md](DATA-SOURCES.md), [DATABASE.md](DATABASE.md) |
| Turn intelligence and alerts on/off | [PORTFOLIO-INTELLIGENCE.md](PORTFOLIO-INTELLIGENCE.md), [ALERTS-GUIDE.md](ALERTS-GUIDE.md) |
| Debug a request | [LOGGING.md](LOGGING.md) |
| See what is done vs still open | [OPEN-ITEMS-TRACKER.md](OPEN-ITEMS-TRACKER.md) |
| Run the app / deploy it | `how to run.md` (root), README *Quick Start* |
