# Implementation Plan — with Deep-Dive on `simulator/` (for Jr Engineers)

This plan keeps your confirmed decision order (D7: forward → backtest → live → UI) but **front-loads a full walkthrough of `simulator/`** so a junior engineer can implement against it without guessing.

Every `simulator/` file gets: **what it does, its public API, the flow it participates in, and what to touch vs. leave alone.**

---

## PART 0 — UNDERSTAND THE ENGINE FIRST (read this before writing any code)

### 0.1 The core loop — memorize this

The entire system reduces to ONE loop, the same for backtest, paper, and live:

```python
for each new bar:
    1. STRATEGY: compute signal from completed bars (≤ bar t)
    2. ORDER: if signal says buy/sell → create an Order (target price = bar t+1's open)
    3. EXECUTE: OrderExecutor tries to fill it at t+1 open   ← the ONLY place fills happen
    4. APPLY: Fill → portfolio.apply_fill() updates positions + cash + P&L
    5. TRACK: PerformanceCalculator records equity_curve point
```

Everything else (data sources, broker auth, UI) is just **feeding this loop** or **displaying its output**. If you ever feel lost, come back to this loop.

### 0.2 What each `simulator/` file owns

| File | Owns | Public types |
|---|---|---|
| `order.py` | Order lifecycle, validation, DB row | `Order`, `OrderStatus`, `PendingOrder`, `NewOrder` |
| `fill.py` | Immutable fill record, fee/slippage attribution | `Fill`, `FillStatus` |
| `portfolio.py` | Cash, positions, orders, can-open-position checks | `Portfolio` |
| `position.py` | Signed qty, avg entry, mark-to-market | `Position` |
| `execution.py` | Fill engine: market/limit/stop, realism, latency | `OrderExecutor` |
| `position_sizing.py` | How much to buy/sell | `PositionSizer` |
| `risk_manager.py` | Per-instance risk limits | `RiskManager`, `RiskConfig` |
| `stop_manager.py` | Stop-loss / take-profit / trailing | `StopManager`, `StopConfig` |
| `fees.py`, `slippage.py`, `commission.py` | Cost models | `FeeModel`, `SlippageModel`, `CommissionModel` |
| `performance.py` | Equity curve + rollup metrics | `PerformanceCalculator` |
| `trade_analyzer.py` | Round-trip statistics | `TradeAnalyzer` |

---

## PART 1 — PHASE 1: Forward/Paper Module (Keystone)

**Goal:** Make `simulator/` the canonical engine for paper runs, add `mode`/`source`, fix the look-ahead leak, let the user pick live or synthetic data. **The system must keep running throughout.**

### Step 1.1 — Add `mode`/`source` columns (5 min, run first)

```sql
ALTER TABLE portfolios
  ADD COLUMN mode   TEXT NOT NULL DEFAULT 'paper' CHECK (mode IN ('paper','live')),
  ADD COLUMN source TEXT NOT NULL DEFAULT 'synthetic'
    CHECK (source IN ('synthetic','replay','mstock'));
```

**Jr engineer note:** this is additive — existing rows get `paper/synthetic` defaults, nothing breaks. Run the equivalent in SQLite dev (`ALTER TABLE ... ADD COLUMN`). Update `db/models.py` model classes (`Portfolio.mode`, `Portfolio.source`).

### Step 1.2 — Fix the look-ahead leak in `OrderExecutor` (THE correctness fix)

**The bug:** signal computed from bars *including* bar `t`, then filled at bar `t`'s close. That's cheating.

**The fix — enforcement point is only `OrderExecutor.execute_order`:**

```python
# simulator/execution.py (conceptual — adapt to existing structure)
class OrderExecutor:
    def execute_order(self, order: Order, current_bar, next_open_price=None):
        # FIX: fill at next bar's OPEN, never current bar's close
        fill_price = order.filled_price or next_open_price or current_bar.open
        # ... produce Fill, then portfolio.apply_fill(fill)
```

**Jr engineer note:** You are not computing signals here — you're only *filling*. The strategy layer already decided *what* to trade on `t`; this method decides *at what price* the fill happens: **the next bar's open**. Find every place a fill price is derived from the *current* close and change it to the *next* open.

**Acceptance test (must pass before moving on):**
```python
def test_no_same_bar_fill():
    # signal appears on bar t
    # assert the fill price == bar (t+1).open, and NOT bar t.close
```
Run `pytest tests/test_forward/test_no_same_bar_fill.py` (create it if absent).

### Step 1.3 — Wire data sources into the paper path

Currently `source` is hardwired. Add a `data/` layer that the paper loop pulls bars from. The engine never cares *where* the bar came from — it only receives `(timestamp, open, high, low, close, volume)`.

**Create `data/` with three source classes (new files):**

```python
# data/base.py
class DataSourceBase(abc.ABC):
    @abc.abstractmethod
    def get_bars(self, symbol, timeframe, start, end) -> Iterator[CandleDict]: ...
    @abc.abstractmethod
    def is_synthetic(self) -> bool: ...

# data/synthetic.py  — pull the existing SyntheticFeed logic here
class SyntheticSource(DataSourceBase):
    def __init__(self, seed=None, drift=0, vol=0.01):
        # random-walk OHLCV generator; supports speed multiplier
        self.speed = 1.0   # replay speed: 1.0 = 1 real second per bar, >1 = faster
    def get_bars(self, ...): yield bar  # yields one bar at a time, paced by self.speed

# data/db_source.py  — wrap existing DbSource
class DbSource(DataSourceBase): ...   # TimescaleDB replay, existing logic moved here

# data/mstock.py     — wrap live/mstock.py MStockClient polling
class MStockSource(DataSourceBase): ...  # real-time broker feed
```

**Config gains a `data` section.** In `config/forward_testing.yaml`:
```yaml
data:
  source: synthetic        # synthetic | replay | mstock
  synthetic:
    speed: 10              # replay speed multiplier
    seed: 42               # reproducible
```

And `ForwardTestingConfig` dataclass gets matching fields (don't forget: this YAML is read by the **engine entry point only**, not the Flask app — see §1 of design doc).

**Jr engineer note:** When the user picks a source, the **paper loop starts the matching DataSource and calls `.get_bars()`**, feeding the engine. Synthetic and replay are deterministic; mstock is real-time. The engine code does not branch on source — that's the whole point.

### Step 1.4 — Build the paper runner on `simulator/`

This is where you bring `ForwardTestingEngine` together with the new data layer. The existing `forward/runner.py` and `forward/paper.py` get **deleted** and their useful orchestration re-expressed here.

**Conceptual flow (`engine.py` → paper loop):**

```python
# src/backtest/forward/engine.py (reworked)

class PaperTradingEngine:
    def __init__(self, config):
        self.config = config
        self.source = self._build_source(config)   # from Step 1.3
        self.portfolio = Portfolio(...)            # simulator/portfolio.py
        self.sizer = PositionSizer(config.sizing)
        self.risk = RiskManager(config.risk)
        self.executor = OrderExecutor(...)         # simulator/execution.py
        self.stop_mgr = StopManager(config.stops)
        self.perf = PerformanceCalculator()

    def run(self):
        for bar in self.source.get_bars(symbol, timeframe, start, end):
            # 1. strategy signal → order (on bar t)
            # 2. executor fills at bar t+1 open    ← the only fill point
            # 3. apply_fill → positions + cash + pnl
            # 4. stop_mgr checks existing positions
            # 5. perf.record(equity)
            self._step(bar)
```

**Jr engineer note:** The single biggest trap is *writing a second fill path*. If you find yourself computing P&L anywhere other than **`portfolio.apply_fill`** via **`OrderExecutor`**, stop — you're recreating the bug. One fill path only.

**Delete after merge:** `forward/paper.py`, `forward/portfolio.py` (the *engine* one), `forward/order_ledger.py`'s `PaperBroker`. Keep `forward/config` loading.

### Step 1.5 — API endpoint: `POST /api/forward/start` accepts source

`api/forward.py` currently takes no config; extend the request body:
```json
{
  "strategy_id": "sma_cross",
  "symbols": ["NIFTY"],
  "source": "synthetic",
  "synthetic_speed": 10,
  "initial_capital": 10000
}
```
The endpoint constructs a `PaperTradingEngine`, stores it in the session registry, and starts the daemon loop. **All state stays in-process** (see design §10 — do not change this in Phase 1).

**Guardrail checkpoint (end of Phase 1):**
- `pytest tests/test_forward/` green.
- `pytest tests/test_api_forward.py` green.
- Manual: start a paper run on synthetic (speed 10), watch it finish fast; start on mstock (market hours) and confirm real bars come in.

---

## PART 2 — PHASE 2: Unify Backtest onto the Engine

**Goal:** Swap backtest's three separate math paths for the same `simulator/` loop, so backtest P&L == forward P&L.

### Step 2.1 — Reuse the loop, change only the source

Backtest = the SAME loop as Phase 1, but source is **always `DbSource`** and speed is irrelevant (you iterate as fast as the DB returns bars, no pacing).

**Create a thin adapter so backtest and paper share one run engine:**

```python
# engine/runner.py (new — shared by backtest, paper, live later)
class EngineRunner:
    def __init__(self, strategy, source, portfolio, executor, ...): ...
    def run(self): ...   # the one loop from Part 0
```

- Backtest calls `EngineRunner(strategy, DbSource(...), ...)`.
- Paper calls `EngineRunner(strategy, source_user_picked, ...)`.
- **Zero logic divergence** — same loop, different source.

### Step 2.2 — Replace the three backtest paths

- `_run_vectorized`, `_run_with_risk`, walk-forward `SimulatedBroker.step` → **one `EngineRunner` path**.
- Keep the vectorized path ONLY as an optional "rough screen" flag (`rough=False` default). Canonical results come from `EngineRunner`.
- Remove the 4-slot `ThreadPoolExecutor` cap → `ProcessPoolExecutor` (pandas releases the GIL; use `max_workers=cpu_count()`).

### Step 2.3 — Alignment test (the acceptance criterion)

```python
def test_backtest_equals_forward_given_same_data():
    # run strategy on the SAME historical bars via backtest path and paper-synthetic path
    # assert equity_curve within cost tolerance (fills use same next-open rule)
```

**Jr engineer note:** If this test fails, the difference is *always* a fill-timing or cost-model divergence. Check: (1) both paths fill at next-bar open, (2) both apply identical fee/slippage from `simulator/fees.py`. Never hand-write different cost math in the backtest.

**Guardrail:** `test_backtest.py`, `test_engine_trades.py`, `test_compare.py` stay green. Backtest UI unchanged.

---

## PART 3 — PHASE 3: Live Order Path (Greenfield)

### Step 3.1 — Extend the broker ABC

`brokers/base.py` today: login/verify/session/logout only. **Add the order contract** as a new abstract base so mStock must implement it:

```python
# brokers/order_base.py
class BrokerOrderBase(abc.ABC):
    @abc.abstractmethod
    def place_order(self, order: NewOrder) -> BrokerOrderRef: ...
    @abc.abstractmethod
    def modify_order(self, order_id, updates): ...
    @abc.abstractmethod
    def cancel_order(self, order_id): ...
    @abc.abstractmethod
    def cancel_all(self): ...
    @abc.abstractmethod
    def get_order_book(self): ...
    @abc.abstractmethod
    def get_order_details(self, order_id): ...
    @abc.abstractmethod
    def calculate_order_margin(self, order) -> float: ...
```

### Step 3.2 — Implement mStock HTTP calls

Map to the endpoints already documented in `docs/archive/mstock-typea-api-reference.md`:
- `place_order` → `POST /openapi/typea/orders/regular`
- `modify_order` → `PUT .../orders/{order-id}`
- `cancel_order` → `DELETE .../orders/{order-id}`
- `cancel_all` → `DELETE .../orders/regular`
- `get_order_book` / `get_order_details` → GET endpoints
- `calculate_order_margin` → `POST .../calculate-margin`

**Jr engineer note:** This is **network I/O against a real broker** — handle auth token, timeout, and non-200 responses → map to `OrderStatus.REJECTED`. Reuse the existing `MStockClient` auth/session machinery; don't re-implement login.

### Step 3.3 — Pluggable fill provider in `OrderExecutor`

`OrderExecutor` gains a strategy to decide where fills come from:

```python
class FillProvider(abc.ABC):
    @abc.abstractmethod
    def submit_and_wait(self, order, timeout) -> Fill | Rejection: ...

class SimulatedFillProvider(FillProvider):
    # current behavior: fill at next-open with slippage/fees      (paper/synthetic/backtest)
class BrokerFillProvider(FillProvider):
    # live: broker.place_order(order) → poll → map to Fill        (live)
```

`OrderExecutor` uses `SimulatedFillProvider` unless `mode == "live"`. **Same order/position/fee math either way** — only the fill origin changes. This is the *only* split between paper and live in the entire engine.

### Step 3.4 — Live feed driver (rewire, don't adopt)

`live_engine.py` (orphaned) has the real mStock polling loop but writes to **phantom tables**. Fold its polling into `data/mstock.py` (`MStockSource`), and **delete** the rest of `live_engine.py` including the phantom-table code.

**Live start rule:** `POST /api/forward/start` with `mode=live` requires `source=mstock` + a valid broker auth session. Reject otherwise. `broker_order_id` on `orders` rows now gets populated by `BrokerFillProvider`.

**Guardrail:** `test_api_broker_auth.py` extended for the new endpoints.

---

## PART 4 — PHASE 4: Portfolio UI Restructure + Cleanup

### Step 4.1 — Landing + subpages

- **Landing** (`/portfolio`) — summary cards, both buckets, alerts. Reads the `PortfolioManager` aggregation.
- **`/portfolio/paper`** — paper/synthetic instances, replay controls, `SYNTH`/`PAPER` badges, paper metrics.
- **`/portfolio/live`** — live instances, real positions, live risk gate, kill-switch. **No synthetic noise.**

Use one template with a base layout and two subpage templates; keep the existing CSS/component library.

### Step 4.2 — Visibly flag everything

Badges everywhere a run is shown: `PAPER·SYNTH`, `PAPER·LIVE-DATA`, `LIVE·BROKER`. This kills the "is this real?" ambiguity permanently.

### Step 4.3 — Cleanup (run only after Phases 1–3 are green)

**Delete:** `marketdata/`, `alerts/`, `analysis/`, `config_manager/`, `dashboard/`, `forward/paper.py`, `forward/broker.py`, `forward/portfolio.py`, `live_engine.py`, `config/market_data.yaml`, `config/time_sync.yaml`.

**Unify:** DB-URL resolution into `db/config.py` (single authority). Fix timeframe drift (`1H`/`4hour`/`minute` → schema CHECK values `1m,1h,1d`).

**Don't break:** `test_dashboard.py`, `test_web_components.py` — update or archive with their modules.

---

## Summary Table — What a Jr Engineer Should Touch in Each Phase

| Phase | Touch (edit/create) | Do NOT touch |
|---|---|---|
| **0 (+1.1–1.5)** | `db/models.py`, `simulator/execution.py` (fill timing), `data/*` (new), `forward/engine.py`, `api/forward.py`, `config/forward_testing.yaml` | `simulator/portfolio.py`, `position.py`, `fees.py` (already correct) |
| **2** | `engine/runner.py` (new), `engine/backtester.py` (routes to runner), `engine/compare.py` | change fill/cost math in backtest (must use `simulator/`) |
| **3** | `brokers/order_base.py` (new ABC), `brokers/mstock.py` (HTTP), `simulator/execution.py` (fill provider), `data/mstock.py` | re-implement auth |
| **4** | `web/templates/*portfolio*`, cleanup deletions, `db/config.py` | strategy registry |

**Golden rule for every phase:** *If you find yourself computing P&L or filling an order anywhere except `OrderExecutor` → `portfolio.apply_fill`, stop — you are recreating the bug the plan exists to fix.*

---