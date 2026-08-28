# Web UI

## Pages

### 1. Backtest (`/backtest`)
**Template:** `templates/backtest.html`
**JS:** `static/js/backtest.js`

Single-strategy deep dive. Configure → Run → See results.

**UI Elements:**
- Strategy dropdown (auto-populated from `/api/strategies`)
- Symbol dropdown (hardcoded: DEMO, BTCUSD, ETHUSD, NIFTY, INFY)
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
- Save to Compare / Export CSV / Promote to Forward buttons

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

Paper-trading replay. Bar-by-bar progression.

**UI Elements:**
- Strategy + symbol + date range config
- "Start" / "Stop" buttons
- Progress bar (bars revealed / total)
- Live equity chart
- Positions table
- Trade log

### 4. Dashboard (`/dashboard`)
**Template:** `templates/dashboard.html`
**JS:** `static/js/dashboard.js`

Overview of all strategies and their status.

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

## JavaScript Architecture

### Components (`static/js/components/`)
- `params_form.js` — Dynamic form generation from strategy param schema
- `metrics_cards.js` — Renders metric cards (P&L, Sharpe, etc.)
- `trade_table.js` — Sortable, paginated trade table
- `loader.js` — Loading spinner
- `toast.js` — Notification toasts

### Charts (`static/js/charts/`)
- `equity_chart.js` — Equity curve (line chart)
- `drawdown_chart.js` — Drawdown percentage (area chart)
- `signals_chart.js` — Price line + buy/sell scatter markers

### Page Controllers
- `backtest.js` — Orchestrates backtest page
- `compare.js` — Orchestrates compare page
- `forward.js` — Orchestrates forward test page
- `dashboard.js` — Orchestrates dashboard
- `session_state.js` — LocalStorage session persistence
- `broker_auth_modal.js` — Auth modal for broker login
- `broker_status.js` — Broker connection status indicator

## Static Assets
```
static/
├── css/           # Stylesheets
├── js/
│   ├── components/    # Reusable UI components
│   ├── charts/        # Chart.js chart wrappers
│   ├── compare/       # Compare-specific charts
│   ├── backtest.js    # Backtest page controller
│   ├── compare.js     # Compare page controller
│   ├── forward.js     # Forward test page controller
│   └── dashboard.js   # Dashboard controller
└── img/           # Images
```
