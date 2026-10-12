# Web UI

> **Looking for "how do I use this?"** — the operator manual lives in
> [USER-GUIDE.md](USER-GUIDE.md) (index) and [how-to/](how-to/README.md) (one
> step-by-step file per page and tab). This file is the developer reference:
> templates, JS controllers and endpoints.

## Adding a new page (~45 minutes)

1. Create template `src/backtest/web/templates/my_page.html`:
```html
{% extends "base.html" %}
{% block title %}My Page{% endblock %}
{% block content %}
<h1>My Page</h1>
<!-- Your HTML here -->
{% endblock %}
{% block scripts %}
<script src="{{ url_for('static', filename='js/my_page.js') }}"></script>
{% endblock %}
```
2. Create the JS controller `src/backtest/web/static/js/my_page.js`.
3. Add the route in `src/backtest/web/app.py`:
```python
@app.get("/my-page")
def my_page():
    return render_template("my_page.html", active="my-page")
```
4. Add the nav link in `base.html`:
```html
<a href="/my-page" class="nav-link {% if active == 'my-page' %}active{% endif %}">My Page</a>
```

## Pages

Index (all routes are registered in `src/backtest/web/app.py`; nav groups come from
`templates/base.html`). How to *use* each one: [how-to/](how-to/README.md).

| Page | URL | Template | Controller |
|------|-----|----------|------------|
| Backtest | `/`, `/backtest` | `backtest.html` | `backtest.js` |
| Compare strategies | `/compare` | `compare.html` | `compare.js` |
| Parameter optimization | `/optimize`, `/optimize/runs/<id>` | `optimize.html`, `optimize_run.html` | `optimize_setup.js`, `optimize_run.js` |
| Strategy builder | `/strategy-builder` | `strategy_builder.html` | inline |
| Engine playground | `/forward` | `forward.html` | `forward.js` |
| Market data | `/data` | `data_manager.html` | `data_manager.js` |
| Settings | `/settings` | `settings.html` | inline |
| Portfolio (overview + buckets) | `/portfolio`, `/portfolio/paper`, `/portfolio/live` | `portfolio*.html` → `_portfolio_center.html` | `portfolio.js` |
| Strategy analytics | `/analytics` | `analytics.html` | `analytics.js` |
| Risk management | `/risk` | `risk.html` | `components/risk_page.js` |
| Consolidated P&L | `/reporting` | `reporting.html` | `reporting.js` |

(The manual `/options` page was removed — GAP-3; its views live in Portfolio.)

### 1. Backtest (`/backtest`)
**Template:** `templates/backtest.html`
**JS:** `static/js/backtest.js`

Single-strategy deep dive. Configure → Run → See results.

**UI Elements:**
- Strategy dropdown (auto-populated from `/api/strategies`)
- Instrument picker (`SymbolPicker`): `All / Equity / Index / F&O` filter tabs +
  search; symbols with no stored bars are visible but not selectable and say so
- Timeframe selector (1D, 1H, 4H, 1W)
- Date range pickers (From/To)
- Capital input
- Dynamic strategy params (auto-generated from schema)
- "Run Backtest" button

**Results Panel:**
- Metrics cards (P&L, Win Rate, Max Drawdown, Sharpe, Trades). Win Rate is
  measured over **closed** trades only: a run that is still holding shows `—`
  with "nothing closed yet" rather than a misleading 0.00%, and the Trades card
  notes how many positions are still open. Trade rows for open positions read
  `⏳ Open` instead of ✅/❌.
- Chart tabs (Equity Curve, Drawdown, Price + Signals)
- Trade table with pagination
- Save to compare / Export CSV / **Open in playground** buttons (the last one
  pre-fills `/forward` with this run)
- Richer **metric sections** (collapsible), **run checks** (benchmark, cost shock,
  Monte Carlo), an advisory **certification** traffic light and **Tune This**
  (hands the run to `/optimize`, never starts a search by itself)
- **Recent runs** — tiles from the server run ledger (`GET /api/backtest/runs`)

### 2. Compare (`/compare`)
**Template:** `templates/compare.html`
**JS:** `static/js/compare.js`

Run 2-4 strategies side-by-side on the same data.

**UI Elements:**
- 2-4 strategy slots (each with strategy dropdown + params)
- Shared config (symbol, date range, capital)
- "Run Compare" button

**Results:**
- Side-by-side metrics table
- Overlaid equity curves
- Ranking by Sharpe/Return/Drawdown

### 3. Forward Test (`/forward`)
**Template:** `templates/forward.html`
**JS:** `static/js/forward.js`

Paper-trading replay driven by a **server-side clock** — the bars keep being
revealed with the tab closed, and the page re-attaches to its own `state_id`
after a refresh.

**UI Elements:**
- Strategy + symbol + date range + capital + **replay speed** (bars/s; the
  server default comes from `--replay-speed`)
- "Start" / "Stop", status badge (Idle / Running / Stopped)
- Progress line + bar (`revealed / total · %`)
- Live metric cards, live equity curve (mark-to-market on the revealed prefix,
  with the buy & hold benchmark)
- Positions table with entry vs current price, move %, unrealised P&L, bars held
- Trade feed via the shared `TradeTable` (pagination, ✅/❌/⏳ Open)

### 3b. Portfolio Command Center (`/portfolio`, `/portfolio/paper`, `/portfolio/live`)
**Template:** `templates/portfolio*.html` → `_portfolio_center.html`
**JS:** `static/js/portfolio.js` + `components/position_actions.js` + `components/orders_tab.js`

Runner matrix + bucket metrics over the SSE snapshot, with seven trading tabs —
`Positions · Equity · Orders · Risk & intelligence · Trade history · Playbooks ·
Activity log` (step-by-step usage: [how-to/portfolio.md](how-to/portfolio.md)):

- **Aggregate Open Positions** — one flat row per open position (equity + option
  structures, legs listed underneath), with Target / Stop / net Δ-Θ columns and an
  **Actions** column: `🛑 SL`, `🎯 TP`, `◐ 50%`, `✕ All`. Each opens a modal that
  names the row it will act on and posts to `/api/portfolio/position/action`;
  level validation is the *server's* (it checks the live mark), so refusals are
  shown verbatim and the modal stays open.
- **Orders** — the `OrderLedger` read surface (`/api/portfolio/orders`): PENDING
  with age and a cancel button, FILLED with requested-vs-filled price and
  adverse-positive slippage, REJECTED with its reason, CANCELLED. Polled at 3 s
  while visible; the tab badge counts working + rejected orders from the SSE
  snapshot. Phase 3 adds a `✎ Amend` action on order resting at a venue
  (quantity / limit price, venue-first), `⏰ Aging only` filtering, and age
  bands (warn 60 s / alert 5 min) rendered as tinted rows and badged age cells.

Behaviours are pinned in a stub DOM by `tests/js/test_position_actions.mjs` and
`tests/js/test_orders_tab.mjs` (see `docs/PORTFOLIO-CENTER.md`).

**Risk Board → Portfolio Intelligence** (`components/portfolio_intelligence.js`,
mounted in `#pi-root` inside `#tab-risk`): collapsible Portfolio Greeks,
Concentration, Correlation heatmap, Market Regime and Market Activity sections.
Fast data (`/api/portfolio/greeks`, `/concentration`) every 1 s and slow data
(`/api/portfolio/intelligence`, `/correlation`) every 30 s — only while the tab
and the page are visible. Section state: `localStorage["pi.section.<name>"]`.
Deep links `…?tab=risk#pi-<section>` switch tab, expand and flash the section.
See `docs/PORTFOLIO-INTELLIGENCE.md`.

### 4. Consolidated P&L (`/reporting`)
**Template:** `templates/reporting.html`
**JS:** `static/js/reporting.js`

One statement across every broker and both books: gross → fees → net → estimated
tax → net after tax, then by-broker, by-tax-category and the trade ledger, with
export buttons for the PDF statement, the ITR annexure workbook (xlsx) and the
trade ledger (csv) — plus a contract-note reconciliation box per broker and a
dry-run "email this" button.

**UI Elements:**
- Period selector (defaults to FY-to-date), `include_paper` toggle, broker filter (csv)
- Ladder card row: Gross P&L · Net P&L · Estimated tax · Net after tax · Cost+tax drag
- By-broker and tax-categorisation tables (rate, schedule, treatment, loss rule)
- "What this report does and does not know" — warnings, data notes, source provenance
- Reconciliation per broker: paste the note's net P&L (+ optional fees) → PASS/WARNING/FAIL
- Export buttons → `POST /api/reporting/pnl/export/{pdf,itr,trades}`
- Demo-book toggle (labelled `SIMULATED`, excluded from tax) for previews

**Endpoints:** `GET /api/reporting/pnl/consolidated`, `GET /api/reporting/config`,
`POST /api/reporting/pnl/export/{pdf,itr,trades}`, `POST /api/reporting/pnl/reconcile`,
`POST /api/reporting/email`.

Tax rates, reconciliation tolerances and the mailer live in
`config/reporting.yaml` (override the file with `REPORTING_CONFIG_PATH`); every
export carries the "estimate, verify with a chartered accountant" disclaimer.

### 5. Cost & Risk Settings (`/settings`)
**Template:** `templates/settings.html` (inline JS)

Broker cost models, segments and the global live kill-switch.

**Broker cards are grouped by adoption:**
- **In use** — expanded: the active broker plus every broker a segment or the
  data routing points at. These are the brokers that price your runs.
- **In config/brokers.yaml** and **Built-in presets** — collapsed behind
  *Show N other broker(s)*. This is the rate catalogue: 10+ presets exist so a
  new broker is a rates change, not a code change. Nothing here is charged
  until a segment points at it.

Each card carries its provenance (`config/brokers.yaml`, built-in preset, or
*this row wins* when a panel edit differs from the file), its validation stamp,
and an **Edit / validate** button.

**Where a rate comes from** — the fee engine resolves **DB row → `config/brokers.yaml`
→ built-in preset**. So:
1. a broker only in the yaml (e.g. `dhan`) is editable in the panel — the first
   save creates the DB row, audited;
2. after that, the DB row wins and a later yaml edit does nothing until the
   panel row is changed back (the card says so);
3. a contract-note validation stamps the profile, which is also what the live
   arming gate checks.

**Endpoints:** `GET/PUT /api/settings/brokers[/<id>]`, `GET .../<id>/audit`,
`POST .../<id>/validate`, `GET/PUT /api/settings/active-broker`,
`GET/PUT/DELETE /api/settings/segments[/<id>]`, the kill-switch endpoints and the
segment live-arming checklist (see `docs/WEB-UI.md` §Reporting for the P&L side).

### 6. Strategy builder (`/strategy-builder`)
**Template:** `templates/strategy_builder.html` (inline JS)

Pine v5 → Python plugin converter. Convert → criteria gate → validate → save:

- **Convert** (`POST /api/pine/convert`) runs the whole server pipeline:
  parse → codegen → generated-code validation → entry check → **conformance
  preflight** (the generated plugin is imported and run through the plugin
  conformance battery, so a script that would only die at load dies here with
  the reason) → metadata. `strategy.order(...)` scripts with `when=` conditions
  convert through a **stateful `generate_signals()` path**
  (`src/backtest/pine/converter.py`); `indicator()`-style scripts with no
  `strategy.entry` are refused with an explanation.
- **Readable translation** — the convert response carries a server-computed
  `metadata.readable` summary: entry criteria, entry strike (options:
  moneyness + type + expiry selects), take profit, stop loss and an optional
  signal exit. Anything the script left out is flagged MISSING and must be
  typed in; **Save stays hidden until every required criterion has a value**
  (segment selection included).
- **Validate** (`POST /api/pine/validate`) runs a quick 30-day backtest of the
  generated code and prints the metrics or the rejection reason.
- **Save** (`POST /api/pine/save`) re-checks criteria + segment server-side,
  writes the plugin and loads it into the registry (no restart needed).
- **Copy LLM prompt** (`GET /api/pine/prompt`) for scripts too complex to
  auto-convert.

### 7. Market data (`/data`)
**Template:** `templates/data_manager.html`
**JS:** `static/js/data_manager.js`

The instrument picker is **names-only**: a checkbox list (symbol, name and
coverage label) behind All / Equity / Index tabs plus search — there is no
per-instrument date-range selection; one From/To pair governs the fetch.
Fetching **with nothing ticked** fetches the active tab's curated universe —
All = NIFTY 200 stocks + NSE indices, Equity = the 200 stocks, Index = the
indices — sent as `scope`; the server rejects unknown scopes (400) and the old
"no list = every NSE/BSE stock" whole-catalogue fallback is gone. One payload
(`GET /api/data/coverage`, `include_catalogue=0`) renders both the picker and
the inventory table, so they can never disagree about what is on disk.

The fetch job (`POST /api/data/fetch`, background thread in
`backtest/api/data_manager.py`) dedupes catalogue aliases, retries mStock's
intermittent 502s with pacing plus a per-job circuit breaker, and skips
coverage-aware: days already stored, and windows that touch only **market
holidays**, are not re-probed (`skipped` is reported back and shown in the
completion toast).

The topbar **data-freshness chip** (`#data-freshness-chip` in `base.html`,
server-rendered from `GET /api/data/freshness`, live-updated by
`components/freshness_chip.js` + `data_fetch_indicator.js`) shows how stale
the price library is. It appears only after a server restart when the running
process predates it.

**Endpoints:** `POST /api/data/fetch` · `/api/data/stop` · `/api/data/clear`,
`GET /api/data/status` · `/api/data/coverage` · `/api/data/inventory` ·
`/api/data/freshness`.

### Global: Alert widget (every page)
**Template:** `templates/base.html` (`#alert-widget`, rendered only when
`PORTFOLIO_INTELLIGENCE_ENABLED`) **JS:** `static/js/components/alert_widget.js`

Bottom-right portfolio alert panel: minimized pill with count and severity
breakdown, expanded list (View Details / Dismiss), toast for new critical and
warning alerts, and a detail modal (`#alert-detail-modal`, created on demand)
with current state, what it means, contributing strategies, typical responses
and subscribed strategies — no position-changing buttons. Broker-session alerts
(`broker_session_expiring` / `broker_session_expired`, raised by the session
manager and also pushed to the outbound notifier channels — Telegram etc. per
`config/alerts.yaml`) additionally carry a **Re-login** button that opens the
same broker auth popup the nav chip uses (password → TOTP); the alert
auto-resolves on verified re-login. Live on the next server restart. Polls
`/api/alerts/active` every 10 s (30 s when the page is hidden) and re-renders
only when the payload `version` changes; expanded state in
`localStorage["pi.alertWidget.expanded"]`. Adds `body.has-alert-widget` so
page content can keep clear of it. Pinned by `tests/js/test_alert_widget.mjs`.
See `docs/ALERTS-GUIDE.md`.

## Money formatting

Every amount goes through `static/js/components/currency.js` (`Money.format`,
`Money.signed`), configured by `--currency` / `BACKTEST_CURRENCY` (default **INR**
→ `₹` with `en-IN` lakh/crore grouping). The page picks it up from
`<body data-currency-symbol="…">`, rendered by a template context processor, and
`GET /api/config` exposes the same values. This replaced the old split where
Backtest/Compare printed `$` and Forward printed `₹` for identical numbers.

## Debugging a request

Every response carries `X-Request-Id`; every `/api` error body carries the same
value as `request_id`, and the UI appends it to the error toast
(`data error: … [req 979be616]`). Grep that id in the server log — or in
`--log-file` output — for the exact traceback and the decisions that produced it
(bars fetched, engine path, per-slot results). Run the app with
`--log-level DEBUG`; see [LOGGING.md](LOGGING.md).

## API Endpoints

### Backtest
| Method | Endpoint | Body | Response |
|--------|----------|------|----------|
| POST | `/api/backtest/run` | `{strategy, symbol, from_date, to_date, capital, params, timeframe}` | `{config, metrics, equity, drawdown, trades}` |
| POST | `/api/backtest/run-many` | `{shared: {...}, slots: [{id, strategy, params}]}` | `{results: {id: payload}}` |

### Forward

| Method | Endpoint | Notes |
|--------|----------|-------|
| POST | `/api/forward/start` | `{strategy, symbol, timeframe?, from_date?, to_date?, capital?, params?, mode?, bars_per_second?}` → `{state_id, total, revealed, config, defaults_applied}` |
| GET | `/api/forward/status?state_id=` | snapshot (pure read — never advances the clock) |
| POST | `/api/forward/stop` | `{state_id?}` — defaults to the active session |
| GET | `/api/forward/sessions` | replays in memory |

Details: [FORWARD-TESTING.md](FORWARD-TESTING.md).

### Strategies
| Method | Endpoint | Response |
|--------|----------|----------|
| GET | `/api/strategies` | `[{name, description, version, author}]` |
| GET | `/api/strategies/<name>/params` | `{param: {default, min, max, type, label, tooltip}}` |

### Forward Test
| Method | Endpoint | Body | Response |
|--------|----------|------|----------|
| POST | `/api/forward/start` | `{strategy, symbol, timeframe, from_date, to_date, capital, params}` | `{status: "running"}` |
| POST | `/api/forward/stop` | — | `{status: "stopped"}` |
| GET | `/api/forward/status` | — | `{status, metrics, equity, drawdown, trades, positions, progress}` |

### Broker Auth
| Method | Endpoint | Body | Response |
|--------|----------|------|----------|
| POST | `/api/broker/login` | `{username, password}` | `{status: "totp_required"}` |
| POST | `/api/broker/verify-totp` | `{totp}` | `{status: "authenticated"}` |

### Health
| Method | Endpoint | Response |
|--------|----------|----------|
| GET | `/health` | `{status: "ok", source: "synthetic"}` |

### Portfolio Intelligence & Alerts
| Method | Endpoint | Response |
|--------|----------|----------|
| GET | `/api/portfolio/greeks` · `/concentration` · `/correlation` · `/intelligence` | Aggregated analytics (`?mode=paper\|live`) |
| GET | `/api/market/regime` · `/api/market/oi-activity?symbol=` | Regime + strategy fit; OI / liquidity activity |
| POST | `/api/market/vix` · `/api/market/chain-activity` | Feed a VIX print / option-chain rows |
| GET | `/api/alerts/active` · `/api/alerts/history` · `/api/alerts/<id>` · `/api/alerts/subscriptions` | Alerts |
| POST | `/api/alerts/<id>/dismiss` · `/review` · `/resolve` | Alert lifecycle |

### Reporting (PRD-002)
| Method | Endpoint | Response |
|--------|----------|----------|
| GET | `/api/reporting/pnl/consolidated?from_date=&to_date=&include_paper=&brokers=&demo=` | The consolidated report (summary ladder, by broker/mode/category, tax, trades) |
| GET | `/api/reporting/config` | Tax rules, reconciliation thresholds, mailer state (never the password) |
| POST | `/api/reporting/pnl/export/pdf` · `/itr` · `/trades` | PDF statement (`itr`: `format=xlsx\|csv\|json`) |
| POST | `/api/reporting/pnl/reconcile` | `{broker, contract_note_pnl, contract_note_fees?, fees_total?}` or `{notes: {broker: {pnl, fees}}}` |
| POST | `/api/reporting/email` | `{dry_run: true}` writes a `.eml` to `var/reporting/outbox`; sending needs SMTP configured |

Malformed dates / missing note figures are 400s; a missing database is an empty
report with a warning, never a 500.

All return 503 when started with `--disable-portfolio-intelligence`. Full
reference: `docs/PORTFOLIO-INTELLIGENCE.md`.

## JavaScript Architecture

### Components (`static/js/components/`)
- `params_form.js` — Dynamic form generation from strategy param schema
- `metrics_cards.js` — Renders metric cards (P&L, Sharpe, etc.)
- `trade_table.js` — Sortable, paginated trade table
- `loader.js` — Loading spinner
- `toast.js` — Notification toasts
- `alert_widget.js` — Global portfolio alert widget + detail modal (every page)
- `freshness_chip.js` — Topbar data-freshness chip (`data_fetch_indicator.js` re-renders it when a fetch finishes)
- `portfolio_intelligence.js` — Risk Board intelligence sections (Greeks, concentration, correlation, regime)

### Charts (`static/js/charts/`)
- `equity_chart.js` — Equity curve (line chart)
- `drawdown_chart.js` — Drawdown percentage (area chart)
- `signals_chart.js` — Price line + buy/sell scatter markers

### Page Controllers
- `backtest.js` — Orchestrates backtest page
- `compare.js` — Orchestrates compare page
- `forward.js` — Orchestrates forward test page
- `reporting.js` — Consolidated P&L page (view models + exports/reconcile wiring)
- `session_state.js` — LocalStorage session persistence
- `broker_auth_modal.js` — Auth modal for broker login
- `broker_status.js` — Broker connection status indicator

## Static Assets
```
static/
├── css/           # tokens.css + app/workspace/theme stylesheets
├── fonts/         # Self-hosted Inter + JetBrains Mono (woff2, licenses)
├── vendor/        # Pinned Chart.js 4.4.1 (+ licenses)
└── js/
    ├── components/    # Reusable UI components
    ├── charts/        # Chart.js chart wrappers
    ├── compare/       # Compare-specific charts
    ├── backtest.js    # Backtest page controller
    ├── compare.js     # Compare page controller
    └── forward.js     # Forward test page controller
```
