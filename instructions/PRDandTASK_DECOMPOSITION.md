# PRD: Unified Trading Bot Platform

---

## 1. Product Overview

### 1.1 Vision
A unified trading bot platform that allows traders to backtest strategies, compare their performance across timeframes, and promote winning strategies to forward testing — all within a single cohesive application.

### 1.2 Problem Statement
Currently, the codebase has all the building blocks — backtester, forward engine, dashboard, strategy registry, data sources — but they operate in isolation. There is no unified UI, no bridge between backtest results and the dashboard, and no workflow that takes a user from research to live trading.

### 1.3 Success Criteria
```
✓ User can run a backtest and see results in the dashboard
✓ User can compare up to 4 strategy/timeframe combinations side by side
✓ User can promote a winning backtest directly to forward test
✓ Dropping a new strategy .py file into strategy/ auto-populates the UI
✓ Forward test page shows live updates from running bot
✓ All pages share the same strategy registry and data sources
```

---

## 2. Users

```
Primary User: Quantitative trader / developer
├── Writes their own strategies in Python
├── Wants to validate before risking capital
├── Comfortable with technical interfaces
└── Needs fast iteration: tweak → test → compare → deploy
```

---

## 3. Application Structure

### 3.1 Navigation
```
┌──────────────────────────────────────────────────────────┐
│  🤖 Trading Bot    [Dashboard] [Backtest] [Compare] [Forward Test] │
└──────────────────────────────────────────────────────────┘
```

### 3.2 Pages Summary
```
Page 1: Dashboard     → System overview, active bot status
Page 2: Backtest      → Single strategy deep dive
Page 3: Compare       → 2-4 slots side by side
Page 4: Forward Test  → Live running bot
```

---

## 4. Functional Requirements

---

### 4.1 Strategy Auto-Discovery System

**Description**: When the app starts, it scans the `strategy/` folder and builds a catalogue of all available strategies. No manual registration required after initial setup.

**Contract every strategy file must follow**:
```python
class MyStrategy(BaseStrategy):
    name        = "Human Readable Name"
    description = "What this strategy does"
    version     = "1.0"
    author      = "Name"
    
    params = {
        "param_name": {
            "default": 14,
            "min":     5,
            "max":     50,
            "type":    "int",
            "label":   "Display Label",
            "tooltip": "What this param does"
        }
    }
    
    def generate_signals(self, candles: pd.DataFrame, params: dict) -> pd.Series:
        ...
```

**API**:
```
GET /api/strategies
└── Returns: list of {name, description, version, author, params}

GET /api/strategies/<name>/params  
└── Returns: param schema for dynamic form rendering
```

**Behaviour**:
- App restart picks up new strategy files automatically
- Invalid files (missing name, params, generate_signals) are skipped with a logged warning
- UI dropdown sorted alphabetically

---

### 4.2 Backtest Page

**Description**: Single strategy, single timeframe, deep dive analysis.

**Configuration Panel**:
```
┌─── CONFIGURATION ──────────────────────────────────────────┐
│  Strategy    [ RSI Mean Revert ▼ ]                         │
│  Symbol      [ BTC/USD ▼ ]                                 │
│  Timeframe   [ 1H ▼ ] [ 4H ▼ ] [ 1D ▼ ] [ 1W ▼ ]         │
│  From        [ 2024-01-01 ]   To  [ 2024-12-31 ]           │
│  Capital     [ $10,000 ]                                    │
│                                                             │
│  ┌─ Strategy Params (dynamic per strategy selected) ──┐    │
│  │  RSI Period [ 14 ]  Overbought [ 70 ]  Oversold [30]│   │
│  └──────────────────────────────────────────────────────┘   │
│                                                             │
│  [ ▶ RUN BACKTEST ]                                         │
└─────────────────────────────────────────────────────────────┘
```

**Results Panel - Metrics Cards**:
```
┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐
│Total P&L │ │Win Rate  │ │Max DD    │ │Sharpe    │ │ Trades   │
│ +$2,847  │ │  64.2%   │ │  -8.3%   │ │  1.42    │ │   203    │
└──────────┘ └──────────┘ └──────────┘ └──────────┘ └──────────┘
```

**Results Panel - Charts**:
```
Tabs: [Equity Curve] [Drawdown] [Price + Signals]

Equity Curve:
└── X axis: Date, Y axis: Portfolio value
└── Benchmark line (buy and hold) overlaid

Drawdown:
└── X axis: Date, Y axis: Drawdown %
└── Shaded area chart, worst drawdown annotated

Price + Signals:
└── Candlestick chart
└── Buy signals marked (green arrow up)
└── Sell signals marked (red arrow down)
```

**Results Panel - Trade Table**:
```
# │ Date       │ Side  │ Entry    │ Exit     │ PnL    │ Result
1 │ Jan 03     │ LONG  │ 42,100   │ 43,850   │ +$210  │ ✅ Win
2 │ Jan 07     │ SHORT │ 44,200   │ 44,890   │ -$84   │ ❌ Loss
```

**Action Buttons**:
```
[ 💾 Save to Compare ]    [ 📤 Export CSV ]    [ ▶ Promote to Forward Test ]
```

**Save to Compare behaviour**:
- Saves current config + result to session
- User goes to Compare page
- Slot is pre-filled with this result
- Up to 4 saves allowed, oldest dropped if exceeded

**Promote to Forward Test behaviour**:
- Pre-fills Forward Test page with exact same strategy + params + symbol
- User lands on Forward Test page, clicks Start

---

### 4.3 Compare Page

**Description**: Side by side comparison of up to 4 strategy/timeframe combinations.

**Shared Configuration** (top of page, applies to all slots):
```
┌─── SHARED CONFIG ──────────────────────────────────────────┐
│  Symbol      [ BTC/USD ▼ ]                                 │
│  From        [ 2024-01-01 ]   To  [ 2024-12-31 ]           │
│  Capital     [ $10,000 ]                                    │
└─────────────────────────────────────────────────────────────┘
```

**Slot Configuration** (each slot independently):
```
┌─ Slot 1 ──────────────────────┐  ┌─ Slot 2 ──────────────────────┐
│ Strategy [ RSI ▼ ]            │  │ Strategy [ MACD ▼ ]           │
│ TF       [ 1H  ▼ ]            │  │ TF       [ 4H   ▼ ]           │
│ [RSI params...]               │  │ [MACD params...]              │
│                    [✕ Remove] │  │                    [✕ Remove] │
└───────────────────────────────┘  └───────────────────────────────┘

[ + Add Slot ]  (max 4)                    [ ▶ RUN ALL ]
```

**Results - View 1: Metrics Table**:
```
┌──────────────┬─────────┬─────────┬─────────┬─────────┐
│ Metric       │ RSI 1H  │ RSI 4H  │MACD 1H  │ BB 1D   │
├──────────────┼─────────┼─────────┼─────────┼─────────┤
│ Total Return │ +18%    │ +28% 🏆 │ +22%    │ +15%    │
│ Win Rate     │ 58%     │ 64% 🏆  │ 61%     │ 52%     │
│ Max Drawdown │ -12%    │ -8% 🏆  │ -6%     │ -15%    │
│ Sharpe Ratio │ 1.1     │ 1.4 🏆  │ 1.3     │ 0.9     │
│ Total Trades │ 847     │ 203     │ 67      │ 923     │
└──────────────┴─────────┴─────────┴─────────┴─────────┘
Best value per row highlighted green with 🏆
```

**Results - View 2: Overlaid Equity Curves**:
```
All slots on same chart
Each slot a different color (blue, orange, green, red)
Legend: Slot label + color + final return %
Hover tooltip shows all 4 values at same date
```

**Results - View 3: Drawdown Comparison**:
```
All slots drawdown curves overlaid
Same color scheme as equity chart
Worst drawdown period shaded
```

**Per Slot Actions**:
```
Each slot result card has:
[ 🔍 Open in Backtest ]    ← deep dive into this slot
[ ▶ Promote to Forward ]   ← promote this slot's strategy
```

**V2 Scope (not in V1)**:
```
Month on Month Returns Heatmap per slot
Export comparison as PDF/CSV
Save comparison configuration
```

---

### 4.4 Forward Test Page

**Description**: Live paper trading with real-time updates.

**Pre-fill behaviour**:
```
When promoted from Backtest or Compare:
├── Strategy pre-selected
├── Params pre-filled
├── Symbol pre-selected
└── User just clicks Start
```

**Display**:
```
├── Live equity curve (updates every N seconds)
├── Current positions
├── Live trade feed
├── Running metrics (return, drawdown, win rate)
└── [ ■ Stop Bot ] button
```

---

### 4.5 BacktestAdapter (The Bridge)

**Description**: Thin layer that translates `BacktestResult` into what the dashboard components expect.

```python
class BacktestAdapter:
    def __init__(self, result: BacktestResult): ...
    
    def to_metrics(self)   -> dict:        ...  # metrics cards
    def to_equity(self)    -> dict:        ...  # equity chart data
    def to_drawdown(self)  -> dict:        ...  # drawdown chart data
    def to_trades(self)    -> list[dict]:  ...  # trade table rows
    def to_signals(self)   -> dict:        ...  # price + signals chart
    def to_compare(self)   -> dict:        ...  # compare slot payload
```

---

## 5. Non-Functional Requirements

```
Performance:
├── Single backtest completes in < 5 seconds for 1 year daily data
├── All 4 compare slots run in parallel (not sequential)
└── UI remains responsive during backtest execution (async)

Reliability:
├── Invalid strategy file does not crash the app
├── Failed backtest slot in compare shows error, others complete
└── Forward test survives page refresh (state persisted)

Usability:
├── New strategy file auto-appears after app restart
├── No frontend changes needed to add a new strategy
└── Promote button always carries exact config, no re-entry
```

---

## 6. Data Flow Architecture

```
strategy/registry.py     ← Single source of truth: strategies
data/                    ← Single source of truth: candles
        │
        ├──→ engine/backtester.py
        │         └──→ BacktestResult
        │                   └──→ BacktestAdapter
        │                             ├──→ Backtest Page
        │                             └──→ Compare Page
        │
        └──→ forward/engine.py
                    └──→ simulator/portfolio.py
                                └──→ Forward Test Page
```

---

## 7. Version Scope

```
V1 (This Build)
├── Strategy auto-discovery
├── BaseStrategy contract + validation
├── BacktestAdapter bridge
├── Backtest page (full)
├── Compare page (slots + 3 views)
├── Forward test page (cleaned up + pre-fill)
├── Promote workflow
└── Save to Compare workflow

V2 (Future)
├── Month on Month heatmap in Compare
├── Export results CSV/PDF
├── Save/load comparison configurations
└── Parameter sensitivity analysis
```

---

---

# Task Decomposition

---

## Structure Overview

```
Epic 1: Foundation
Epic 2: Backtest Page
Epic 3: Compare Page
Epic 4: Forward Test Page Cleanup
Epic 5: Cross-Page Workflows
Epic 6: Integration & Testing
```

---

## Epic 1: Foundation

### Task 1.1 — BaseStrategy Contract
**File**: `strategy/base.py`
**What**:
```
- Create BaseStrategy abstract class
- Define required attributes: name, description, version, author, params
- Define required method: generate_signals(candles, params) -> pd.Series
- Add param schema validation (type, min, max, default, label, tooltip)
- Add class method: validate() → raises clear error if contract not met
```
**Acceptance**: Importing a valid strategy works. Importing an invalid one raises `StrategyContractError` with clear message.

---

### Task 1.2 — Strategy Auto-Discovery
**File**: `strategy/registry.py`
**What**:
```
- Scan strategy/ folder using pkgutil.iter_modules
- Import each module
- Find all classes that inherit BaseStrategy
- Skip files that fail validation (log warning, do not crash)
- Build catalogue: {strategy_name: {class, params, description, ...}}
- Expose: get_all(), get(name), get_params(name)
```
**Acceptance**: Drop a new valid .py file in strategy/, restart app, strategy appears in catalogue. Drop invalid file, app still starts.

---

### Task 1.3 — Strategy API Endpoints
**File**: `api/strategies.py`
**What**:
```
GET /api/strategies
└── Returns: [{name, description, version, author}]

GET /api/strategies/<name>/params
└── Returns: {param_name: {default, min, max, type, label, tooltip}}
```
**Acceptance**: Both endpoints return correct JSON. Unknown strategy name returns 404.

---

### Task 1.4 — BacktestAdapter
**File**: `adapters/backtest_adapter.py`
**What**:
```
- Takes BacktestResult as input
- to_metrics()   → {total_return, win_rate, max_drawdown, sharpe, total_trades, ...}
- to_equity()    → {dates: [], values: [], benchmark: []}
- to_drawdown()  → {dates: [], values: [], worst_dd: float, worst_dd_date: str}
- to_trades()    → [{id, date, side, entry, exit, pnl, result}]
- to_signals()   → {candles: [], buys: [], sells: []}
- to_compare()   → combined payload for compare slot
```
**Acceptance**: Feed a known BacktestResult, verify each method returns correct shape and values.

---

### Task 1.5 — Backtest API Endpoint
**File**: `api/backtest.py`
**What**:
```
POST /api/backtest/run
Body: {
    strategy: str,
    symbol: str,
    timeframe: str,
    from_date: str,
    to_date: str,
    capital: float,
    params: dict
}
Response: {
    metrics: {...},
    equity: {...},
    drawdown: {...},
    trades: [...],
    signals: {...}
}

- Runs backtester with given config
- Passes result through BacktestAdapter
- Returns unified JSON response
- Handles errors: unknown strategy, bad dates, insufficient data
```
**Acceptance**: POST with valid body returns correct structure. POST with unknown strategy returns 400 with clear message.

---

### Task 1.6 — Parallel Backtest API Endpoint
**File**: `api/backtest.py`
**What**:
```
POST /api/backtest/run-many
Body: {
    shared: {symbol, from_date, to_date, capital},
    slots: [
        {id: 1, strategy, timeframe, params},
        {id: 2, strategy, timeframe, params},
        ...
    ]
}
Response: {
    results: {
        1: {metrics, equity, drawdown, trades, signals} | {error: str},
        2: {metrics, equity, drawdown, trades, signals} | {error: str},
        ...
    }
}

- Runs all slots in parallel using ThreadPoolExecutor
- Each slot result keyed by slot id
- Failed slot returns {error: message}, others still return results
```
**Acceptance**: 4 slots run in parallel. One intentionally broken slot returns error, other 3 return results.

---

## Epic 2: Backtest Page

### Task 2.1 — Backtest Page Route
**File**: `dashboard/app.py`
**What**:
```
- Add GET /backtest route
- Renders backtest.html template
- Passes available symbols list to template
```

---

### Task 2.2 — Backtest Page Template Structure
**File**: `dashboard/templates/backtest.html`
**What**:
```
- Extends base.html (shared nav)
- Two column layout: config panel left, results right
- Config panel: strategy dropdown, symbol, timeframe, dates, capital
- Dynamic params section (empty on load, populated by JS)
- Results section: hidden on load, shown after run
- Metrics cards row
- Chart tabs: Equity | Drawdown | Price+Signals
- Chart containers (chart.js or plotly)
- Trade table
- Action buttons: Save to Compare, Export CSV, Promote to Forward
```

---

### Task 2.3 — Strategy Dropdown Dynamic Params (JS)
**File**: `dashboard/static/js/backtest.js`
**What**:
```
- On page load: fetch /api/strategies → populate dropdown
- On strategy select change:
    fetch /api/strategies/<name>/params
    → dynamically render param input fields
    → each field respects type (int/float), min, max, default
    → show label and tooltip
- On timeframe change: update state
- On Run click:
    collect all form values
    POST to /api/backtest/run
    show loading spinner
    on response: populate results section
```
**Acceptance**: Select RSI → RSI params appear. Select MACD → MACD params replace them. Params use correct types and defaults.

---

### Task 2.4 — Equity Curve Chart
**File**: `dashboard/static/js/charts/equity_chart.js`
**What**:
```
- Line chart: portfolio value over time
- Benchmark line (buy and hold) overlaid in grey
- Hover tooltip: date, portfolio value, benchmark value
- Responsive width
- Reusable function: renderEquityChart(containerId, data)
```

---

### Task 2.5 — Drawdown Chart
**File**: `dashboard/static/js/charts/drawdown_chart.js`
**What**:
```
- Area chart: drawdown % over time (negative values)
- Filled red/pink below zero
- Worst drawdown point annotated with label
- Hover tooltip: date, drawdown %
- Reusable function: renderDrawdownChart(containerId, data)
```

---

### Task 2.6 — Price + Signals Chart
**File**: `dashboard/static/js/charts/signals_chart.js`
**What**:
```
- Candlestick or line chart of price
- Buy signals: green upward arrow markers on chart
- Sell signals: red downward arrow markers on chart
- Hover tooltip: date, OHLCV, signal if any
- Reusable function: renderSignalsChart(containerId, data)
```

---

### Task 2.7 — Metrics Cards Component
**File**: `dashboard/static/js/components/metrics_cards.js`
**What**:
```
- Renders metrics card row from metrics dict
- Cards: Total P&L, Win Rate, Max Drawdown, Sharpe, Total Trades
- P&L card: green if positive, red if negative
- Drawdown card: color scaled by severity
- Reusable function: renderMetricsCards(containerId, metrics)
```

---

### Task 2.8 — Trade Table Component
**File**: `dashboard/static/js/components/trade_table.js`
**What**:
```
- Renders trade list as paginated table (20 rows per page)
- Columns: #, Date, Side, Entry, Exit, PnL, Result
- PnL cell: green positive, red negative
- Result cell: ✅ Win / ❌ Loss
- Sortable columns
- Reusable function: renderTradeTable(containerId, trades)
```

---

### Task 2.9 — Save to Compare Logic
**File**: `dashboard/static/js/backtest.js`
**What**:
```
- On "Save to Compare" click:
    store {config, result} in sessionStorage under key "compare_slots"
    max 4 slots, if 4 already exist: show warning "Compare is full"
    show success toast: "Saved to Compare (slot N/4)"
- On Compare page load: read sessionStorage, pre-fill slots
```

---

### Task 2.10 — Export CSV
**File**: `api/backtest.py` + JS
**What**:
```
- Backend: GET /api/backtest/export?session_id=X
    returns trades as CSV download

- Frontend: on Export click
    trigger download
```

---

## Epic 3: Compare Page

### Task 3.1 — Compare Page Route
**File**: `dashboard/app.py`
**What**:
```
- Add GET /compare route
- Renders compare.html template
```

---

### Task 3.2 — Compare Page Template Structure
**File**: `dashboard/templates/compare.html`
**What**:
```
- Extends base.html
- Shared config bar at top: symbol, date range, capital
- Slots row: 2-4 slot cards side by side
- Add Slot button (hidden when 4 slots exist)
- Run All button
- Results section: hidden until run
- Three result tabs: Metrics Table | Equity Curves | Drawdown
- Per-slot action buttons in results
```

---

### Task 3.3 — Slot Management (JS)
**File**: `dashboard/static/js/compare.js`
**What**:
```
- Start with 2 slots on load
- Add Slot: clone slot card template, assign next id, max 4
- Remove Slot: remove card, reassign labels, min 1 slot
- Each slot has independent strategy dropdown + params
- On strategy select in slot: fetch params, render in that slot only
- On page load: check sessionStorage for saved slots, pre-fill
```
**Acceptance**: Add/remove slots works. Each slot params independent. Selecting RSI in Slot 1 does not affect Slot 2.

---

### Task 3.4 — Run All Logic (JS)
**File**: `dashboard/static/js/compare.js`
**What**:
```
- On Run All click:
    validate all slots have strategy + timeframe selected
    collect shared config + all slot configs
    POST to /api/backtest/run-many
    show loading state on each slot card
    on response:
        populate results for each slot
        show results section
        highlight winners in metrics table
```

---

### Task 3.5 — Compare Metrics Table
**File**: `dashboard/static/js/compare/metrics_table.js`
**What**:
```
- Renders N-column table (N = number of slots)
- Rows: Total Return, Win Rate, Max DD, Sharpe, Total Trades
- Per row: find best value, highlight that cell green + 🏆
- Slot label as column header (e.g. "RSI 1H", "MACD 4H")
- Reusable function: renderCompareTable(containerId, slotResults)
```

---

### Task 3.6 — Overlaid Equity Curves Chart
**File**: `dashboard/static/js/compare/equity_compare_chart.js`
**What**:
```
- Multi-line chart, one line per slot
- Color palette: blue, orange, green, red (consistent)
- Legend: slot label + color + final return %
- Hover tooltip: shows all slot values at same date (crosshair)
- Reusable function: renderCompareEquity(containerId, slotResults)
```

---

### Task 3.7 — Overlaid Drawdown Chart
**File**: `dashboard/static/js/compare/drawdown_compare_chart.js`
**What**:
```
- Multi-line drawdown chart, one line per slot
- Same color scheme as equity chart
- Worst point per line annotated
- Reusable function: renderCompareDrawdown(containerId, slotResults)
```

---

### Task 3.8 — Per Slot Action Buttons
**File**: `dashboard/static/js/compare.js`
**What**:
```
- "Open in Backtest" button per slot result:
    saves slot config to sessionStorage key "backtest_prefill"
    navigates to /backtest
    backtest page reads prefill and populates form

- "Promote to Forward" button per slot result:
    saves slot config to sessionStorage key "forward_prefill"
    navigates to /forward
    forward page reads prefill and populates form
```

---

## Epic 4: Forward Test Page Cleanup

### Task 4.1 — Forward Page Pre-fill
**File**: `dashboard/static/js/forward.js`
**What**:
```
- On page load: check sessionStorage for "forward_prefill"
- If exists: pre-fill strategy, params, symbol
- Clear prefill from sessionStorage after reading
- Show "Pre-filled from backtest result" notice banner
```

---

### Task 4.2 — Forward Page Template Cleanup
**File**: `dashboard/templates/forward.html`
**What**:
```
- Consistent nav with other pages
- Clean config section matching backtest page style
- Dynamic strategy params (same JS component as backtest page)
- Live equity chart (reuse equity_chart.js)
- Live metrics cards (reuse metrics_cards.js)
- Live trade feed table
- Start / Stop controls
- Status indicator: Idle | Running | Stopped
```

---

### Task 4.3 — Forward API Alignment
**File**: `api/forward.py`
**What**:
```
- POST /api/forward/start  {strategy, symbol, params}
- POST /api/forward/stop
- GET  /api/forward/status → {status, metrics, equity, positions, trades}
- Ensure status endpoint returns same shape as backtest adapter output
  so frontend components are reusable
```

---

## Epic 5: Cross-Page Workflows

### Task 5.1 — Shared Navigation Component
**File**: `dashboard/templates/base.html`
**What**:
```
- Base template with nav bar
- Nav items: Dashboard, Backtest, Compare, Forward Test
- Active page highlighted
- All other templates extend this
- Consistent header, footer, CSS imports
```

---

### Task 5.2 — Session State Manager
**File**: `dashboard/static/js/session_state.js`
**What**:
```
- Thin wrapper around sessionStorage
- set(key, value), get(key), clear(key)
- Keys:
    "compare_slots"    → array of {config, result}
    "backtest_prefill" → {config}
    "forward_prefill"  → {config}
- Max compare slots enforcement (4)
- Expiry: cleared on browser close (sessionStorage default)
```

---

### Task 5.3 — Toast Notification Component
**File**: `dashboard/static/js/components/toast.js`
**What**:
```
- showToast(message, type)  type: success | warning | error
- Auto dismisses after 3 seconds
- Stacks if multiple toasts
- Used by: Save to Compare, Promote, errors
```

---

### Task 5.4 — Loading State Component
**File**: `dashboard/static/js/components/loader.js`
**What**:
```
- showLoader(containerId, message)
- hideLoader(containerId)
- Used by: Run Backtest, Run All
- Shows spinner + message in container
```

---

## Epic 6: Integration & Testing

### Task 6.1 — Strategy Contract Tests
**File**: `tests/test_strategy_base.py`
**What**:
```
- Test valid strategy passes validation
- Test missing name fails with clear error
- Test missing params fails
- Test missing generate_signals fails
- Test invalid param schema (missing default) fails
- Test auto-discovery finds valid files
- Test auto-discovery skips invalid files without crashing
```

---

### Task 6.2 — BacktestAdapter Tests
**File**: `tests/test_backtest_adapter.py`
**What**:
```
- Create synthetic BacktestResult
- Test to_metrics() returns correct keys and values
- Test to_equity() returns correct shape
- Test to_drawdown() worst_dd is correct
- Test to_trades() trade list correct length and fields
- Test to_compare() returns combined payload
```

---

### Task 6.3 — Backtest API Tests
**File**: `tests/test_api_backtest.py`
**What**:
```
- POST /api/backtest/run valid body → 200 correct shape
- POST /api/backtest/run unknown strategy → 400
- POST /api/backtest/run bad dates → 400
- POST /api/backtest/run-many 4 slots → all 4 results returned
- POST /api/backtest/run-many 1 broken slot → error in that slot, others ok
```

---

### Task 6.4 — Strategy API Tests
**File**: `tests/test_api_strategies.py`
**What**:
```
- GET /api/strategies → returns list with correct fields
- GET /api/strategies/<valid_name>/params → returns param schema
- GET /api/strategies/<invalid_name>/params → 404
```

---

### Task 6.5 — End to End Workflow Test
**File**: `tests/test_e2e_workflow.py`
**What**:
```
- Simulate: load strategies → run backtest → get result → adapt → verify display data
- Simulate: run 4 slot compare → verify parallel results → verify winner detection
- Simulate: backtest result → promote config → verify forward pre-fill shape
```

---

### Task 6.6 — Existing Strategy Migration
**File**: All files in `strategy/`
**What**:
```
- Audit all existing strategy files
- Add name, description, version, author, params metadata to each
- Ensure generate_signals signature matches contract
- Verify each appears in registry after migration
```

---

## Task Summary Table

```
Epic 1: Foundation          (6 tasks)  ← Do this first, everything depends on it
Epic 2: Backtest Page       (10 tasks) ← Core user value
Epic 3: Compare Page        (8 tasks)  ← Differentiating feature
Epic 4: Forward Page        (3 tasks)  ← Cleanup + integration
Epic 5: Cross-Page          (4 tasks)  ← Glue and polish
Epic 6: Testing             (6 tasks)  ← Confidence layer
─────────────────────────────────────
Total                       37 tasks
```

---

## Recommended Build Order

```
Week 1: Epic 1 (Foundation)
         Tasks 1.1 → 1.2 → 1.3 → 1.4 → 1.5 → 1.6
         Everything else depends on these

Week 2: Epic 2 (Backtest Page)
         Tasks 2.1 → 2.2 → 2.3 → 2.7 → 2.4 → 2.5 → 2.6 → 2.8 → 2.9 → 2.10
         Get single backtest working end to end first

Week 3: Epic 3 (Compare Page) + Epic 5 (Cross-page)
         Compare slots → Run All → Charts → Per slot actions
         Session state + Toast + Loader components

Week 4: Epic 4 (Forward Cleanup) + Epic 6 (Testing)
         Forward pre-fill → Cleanup → All tests → Migration
```