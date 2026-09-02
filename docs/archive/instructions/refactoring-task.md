# Junior-Friendly File-by-File Task Tickets

Each ticket below is one **unit of work a junior engineer can pick up and complete independently**. Every ticket has:
- **Exact files** to touch (with absolute paths)
- **What to build** (with function signatures / class skeletons)
- **The exact test to write**
- **Acceptance criteria** (how to know "done")

> **Do work in ticket order.** Each ticket assumes the previous ones are merged. Never start a later ticket until the earlier ones pass.

---

# PHASE 1 — Forward / Paper Module

---

## TICKET P1.1 — Add `mode` and `source` columns to `portfolios`

**Files**

| Path | Action |
|---|---|
| `db/migrations/002_add_mode_source.sql` | **create** |

**What to build**

```sql
-- 002_add_mode_source.sql
ALTER TABLE portfolios
  ADD COLUMN mode   TEXT NOT NULL DEFAULT 'paper'
    CHECK (mode IN ('paper','live')),
  ADD COLUMN source TEXT NOT NULL DEFAULT 'synthetic'
    CHECK (source IN ('synthetic','replay','mstock'));

UPDATE portfolios SET mode='paper', source='synthetic'
  WHERE mode IS NULL OR source IS NULL;
```

**Where to wire it**
Add `002_add_mode_source.sql` alongside `001_initial_schema.sql` so your migration runner picks it up. If you're unsure the runner exists, ask your lead — **do not** hand-run SQL in production.

**Test to write:** `tests/db/test_migrations_002.py`
```python
def test_migration_applies_fresh_and_backfills():
    # 1) apply on an empty DB  → columns exist, defaults are 'paper'/'synthetic'
    # 2) insert a legacy row (no mode/source) → backfill sets them
    # 3) CHECK constraints reject mode='foo'
```

**Acceptance (done when):**
- [ ] Migration applies cleanly on fresh AND existing DB.
- [ ] Existing rows backfilled to `paper`/`synthetic`.
- [ ] INSERT with `mode='bogus'` is rejected.
- [ ] Existing tests still pass (this must be additive).

---

## TICKET P1.2 — Data-source registry (the `mode`+`choice` factory)

**Files**

| Path | Action |
|---|---|
| `data/source_registry.py` | **create** |

**What to build**

```python
# data/source_registry.py
from data.base import BaseSource
from data.db_source import DbSource
from data.synthetic import SyntheticSource
from data.mstock_live_feed import MStockLiveFeed   # created in P3.4

class SourceRegistry:
    def get_source(self, mode: str, choice: str | None = None, **kwargs) -> BaseSource:
        if mode == 'backtest':
            return DbSource(**kwargs)                       # fixed: historical DB
        if mode == 'live':
            return MStockLiveFeed(**kwargs)                 # fixed: real broker feed
        if mode == 'paper':
            if choice == 'mstock':
                return MStockLiveFeed(**kwargs)             # live data, paper risk
            if choice == 'synthetic':
                return SyntheticSource(replay_speed=kwargs.get('replay_speed', 1))
            raise ConfigError(f"paper mode needs source: 'mstock' or 'synthetic', got {choice}")
        raise ConfigError(f"unknown mode: {mode}")
```

**Note:** don't panic if `MStockLiveFeed` doesn't exist yet — stub it, or reference `data.base.BaseSource` with a `NotImplementedError` body until P3.4. The registry is written to the target contract; the feed lands later.

**Test to write:** `tests/data/test_source_registry.py`
```python
def test_backtest_returns_dbsource():      assert isinstance(reg.get_source('backtest'), DbSource)
def test_live_returns_mstock():            assert isinstance(reg.get_source('live'), MStockLiveFeed)
def test_paper_mstock():                   assert isinstance(reg.get_source('paper','mstock'), MStockLiveFeed)
def test_paper_synthetic():                assert isinstance(reg.get_source('paper','synthetic'), SyntheticSource)
def test_paper_no_choice_raises():         pytest.raises(ConfigError, reg.get_source, 'paper', None)
def test_bad_mode_raises():                pytest.raises(ConfigError, reg.get_source, 'banana')
```

**Also update** `config/forward_testing.yaml`:
```yaml
mode: paper
source: synthetic
replay_speed: 5
```

**Acceptance (done when):**
- [ ] Every `(mode, choice)` combination returns the correct source class.
- [ ] Invalid combos raise `ConfigError` with a clear message.
- [ ] Test file passes.

---

## TICKET P1.3 — Fix the fill-timing look-ahead bug in `OrderExecutor`

> **This is the single most important ticket in the whole migration.** Read the deep-dive in the design doc §4 / impl plan Phase 1 first.

**Files**

| Path | Action |
|---|---|
| `simulator/execution.py` | **edit** |

**What to build**

Change the executor so an order placed on bar `t` fills on bar `t+1`'s **open** — never bar `t`'s close.

```python
# simulator/execution.py  (target shape)
class OrderExecutor:
    def __init__(self, ...):
        self._pending: list[Order] = []     # orders submitted but not yet filled
        # ... existing init ...

    def submit(self, order: Order) -> None:
        """Queue the order. It will fill on the NEXT completed bar's open."""
        self._pending.append(order)

    def step(self, completed_bar) -> list[Fill]:
        """Called with each NEW completed bar. Fills only orders from PREVIOUS bar."""
        fills = []
        still_pending = []
        for order in self._pending:
            if order._entry_triggered:          # submitted on a prior bar → fill NOW
                fill = self._make_fill(order, completed_bar.open)   # ← next bar OPEN
                fills.append(fill)
            else:
                order._entry_triggered = True   # submitted this bar → wait one more
                still_pending.append(order)
        self._pending = still_pending
        return fills
```

> **The behavioral law:** an order submitted during `step(t)` is NOT in `_pending` under `_entry_triggered=True` until `step(t+1)`. So it can never fill at bar `t`'s price.

**Test to write:** `tests/simulator/test_fill_timing.py`
```python
def test_no_lookahead_single_bar():
    ex = OrderExecutor(...)
    bar_t = Bar(open=100, close=105, ...)
    # submit on bar t
    ex.submit(order_for_bar_t)
    fills_after_t = ex.step(bar_t)          # feeds bar t
    assert fills_after_t == []              # NOT filled yet (no look-ahead!)
    bar_t1 = Bar(open=103, close=110, ...)  # next bar
    fills_after_t1 = ex.step(bar_t1)
    assert len(fills_after_t1) == 1
    assert fills_after_t1[0].price == 103   # MUST be bar t+1's OPEN

def test_multi_order_stays_in_sequence():
    # two orders submitted on consecutive bars fill in order, each at next open
```

**Acceptance (done when):**
- [ ] An order submitted on bar `t` **never** fills at bar `t`'s close.
- [ ] It fills at bar `t+1`'s **open** on the next `step()` call.
- [ ] `test_no_lookahead_single_bar` passes.
- [ ] All existing `simulator/` tests still pass (you changed behavior — verify you didn't silently break other callers; if you did, find them and fix them to use `submit()` + `step()`, not inline fills).

---

## TICKET P1.4 — `PaperRunner`: one run = one Portfolio + one source + one strategy

**Files**

| Path | Action |
|---|---|
| `forward/paper_runner.py` | **create** |
| `forward/paper.py`, `forward/broker.py`, `forward/portfolio.py`, `forward/runner.py`, `forward/order_ledger.py` | **delete** (after tests pass) |

**What to build**

```python
# forward/paper_runner.py
from simulator.execution import OrderExecutor
from simulator.portfolio import Portfolio

class PaperRunner:
    """Drives ONE paper run. Reuses simulator/ — no custom engines."""
    def __init__(self, portfolio: Portfolio, source, strategy,
                 executor: OrderExecutor, order_queue):
        self.portfolio  = portfolio
        self.source     = source
        self.strategy   = strategy
        self.executor   = executor
        self.order_queue = order_queue

    def run(self):
        for bar in self.source.iter_bars():          # synthetic fast OR mstock real-time
            signal = self.strategy.on_bar(bar)
            if signal:
                order = signal.to_order(bar.symbol)
                self.order_queue.submit(order)       # keep client_order_id idempotency
                self.executor.submit(order)          # queue for next-bar open fill
            for fill in self.executor.step(bar):     # fills on NEXT bar open
                self.portfolio.apply_fill(fill)
            self.portfolio.record_equity(bar.close)  # equity snapshot per bar
        return self.portfolio.performance_summary()
```

> **Junior note:** the engine never changes between synthetic and live. Only `self.source.iter_bars()` differs. That's the whole single-engine point.

**Test to write:** `tests/forward/test_paper_runner.py`
```python
def test_end_to_end_synthetic_run():
    runner = PaperRunner(portfolio=Portfolio(...),
                         source=SyntheticSource(...),
                         strategy=FakeBuyStrategy(), ...)
    summary = runner.run()
    # assert: orders were placed, fills happened at next-bar open,
    # positions updated, equity curve recorded, all rows tagged mode='paper'

def test_paper_rows_tagged_mode_paper():
    # after run, every wrote row has mode='paper'
```

**Then delete** the 5 superseded files (only after the test above passes).

**Acceptance (done when):**
- [ ] End-to-end synthetic paper run produces orders/fills/positions/equity.
- [ ] All DB rows tagged `mode='paper'`.
- [ ] Old `forward/paper.py` etc. deleted and nothing still imports them (`grep` across `src/`).
- [ ] `test_forward.py`, `test_portfolio_engine.py` updated (they may reference the old paper modules — fix imports to `PaperRunner`).

---

# PHASE 1 GUARDRAIL (do before Phase 2)

## TICKET P1.5 — The canonical equivalency test

> **Why this matters:** this is the test that proves backtest ≈ forward (the whole point of the migration).

**Files**

| Path | Action |
|---|---|
| `tests/simulator/test_engine_equivalency.py` | **create** |

**What to build**

```python
def test_same_strategy_backtest_eq_forward_within_cost():
    # Run the SAME strategy object through:
    #  (a) BacktestDriver over DbSource
    #  (b) PaperRunner  over the SAME DbSource (as replay source)
    backtest_pnl  = run_backtest(STRAT, SYMBOL, ...)
    forward_pnl   = run_forward(STRAT, SYMBOL, same_data, ...)
    # Equally-timed signals + identical fill timing ⇒ PnL matches within cost tolerance
    assert abs(backtest_pnl - forward_pnl) < COST_TOLERANCE
```

**Acceptance (done when):**
- [ ] Same strategy, same bars ⇒ backtest P&L ≈ forward P&L within `COST_TOLERANCE`.
- [ ] If they diverge, you've found a second fill-timing or cost-model leak — **stop and report before continuing.**

---

# PHASE 2 — Unify Backtest

---

## TICKET P2.1 — `BacktestDriver`: backtest on the `simulator/` step-loop

**Files**

| Path | Action |
|---|---|
| `engine/backtest_driver.py` | **create** |

**What to build**

```python
# engine/backtest_driver.py
class BacktestDriver:
    """Same loop as PaperRunner, but over historical DbSource bars."""
    def __init__(self, db_source, strategy, portfolio, executor, order_queue):
        ...
    def run(self):
        for bar in self.db_source.iter_bars():
            signal = self.strategy.on_bar(bar)
            if signal:
                self.order_queue.submit(signal.to_order(...))
                self.executor.submit(that_order)
            for fill in self.executor.step(bar):
                self.portfolio.apply_fill(fill)
            self.portfolio.record_equity(bar.close)
        return self.portfolio.performance_summary()
```

> **Junior note:** this is the **same** loop as `PaperRunner`. If you find yourself copy-pasting more than ~10 lines, factor a shared `run_engine_loop(source, strategy, executor, portfolio, order_queue)` helper into `simulator/engine_loop.py` and have both `PaperRunner` and `BacktestDriver` call it. **Reuse > duplicate.**

**Test to write:** `tests/engine/test_backtest_driver.py`
```python
def test_backtest_matches_forward_same_data():  # ties into P1.5
def test_backtest_records_positions_and_equity():
def test_quick_screen_mode_still_works():
```

**Acceptance (done when):**
- [ ] `BacktestDriver` produces same P&L as `PaperRunner` on identical bars (P1.5 test passes for backtest too).

---

## TICKET P2.2 — Route backtest API to the driver

**Files**

| Path | Action |
|---|---|
| `api/backtest.py` | **edit** |

**What to build**

Replace the vectorized / risk / walk-forward paths with `BacktestDriver().run()`. Keep the fast vectorized path only behind `mode='quick_screen'`:

```python
def run_backtest(req):
    if req.mode == 'quick_screen':   # optional fast rough filter only
        return run_vectorized_quick(req)
    driver = build_driver(req)
    return driver.run()              # canonical result from simulator/
```

**Test to write:** `tests/api/test_api_backtest.py` — existing tests updated to call the new driver path; keep a `quick_screen` test.

**Acceptance (done when):**
- [ ] `POST /api/backtest/run` returns results from `BacktestDriver`.
- [ ] `quick_screen` still returned when requested.
- [ ] Backtest UI page shows identical output shape (UI unchanged this phase).

---

## TICKET P2.3 — Swap `ThreadPoolExecutor` → `ProcessPoolExecutor`

**Files**

| Path | Action |
|---|---|
| `api/backtest.py` (the run-many / compare endpoint) | **edit** |

**What to build**

```python
from concurrent.futures import ProcessPoolExecutor

# ThreadPoolExecutor(max_workers=4)  →  ProcessPoolExecutor()
with ProcessPoolExecutor() as pool:
    results = pool.map(run_single_backtest, job_params)
```

> **Junior note:** each process is separate — strategy and portfolio must be **picklable** (no lambdas, no `threading` objects inside). The top-level `run_single_backtest(params)` function must take plain args and return plain dicts.

**Test to write:** `tests/api/test_api_backtest_parallel.py`
```python
def test_multiple_backtests_run_in_process_pool():
    results = run_many([...3 jobs...])
    assert len(results) == 3 and all(job['status']=='success')
```

**Acceptance (done when):**
- [ ] Multi-backtest uses processes, not threads.
- [ ] All jobs complete correctly; no pickling errors.

---

# PHASE 3 — Live Order Path

---

## TICKET P3.1 — Add the order contract to the broker ABC

**Files**

| Path | Action |
|---|---|
| `brokers/base.py` | **edit** |

**What to build**

```python
# brokers/base.py — add:
class BrokerOrderBase(ABC):
    @abstractmethod
    def place_order(self, order) -> BrokerOrderId: ...
    @abstractmethod
    def modify_order(self, order) -> None: ...
    @abstractmethod
    def cancel_order(self, order) -> None: ...
    @abstractmethod
    def get_order_book(self) -> list[BrokerOrder]: ...
    @abstractmethod
    def calculate_order_margin(self, order) -> MarginInfo: ...
```

**Test:** update `tests/brokers/test_base_contract.py` so a fake broker must implement all 5 methods or it can't instantiate.

**Acceptance (done when):**
- [ ] ABC enforces the order surface; fake broker in tests implements all 5.

---

## TICKET P3.2 — Implement mStock order HTTP calls

**Files**

| Path | Action |
|---|---|
| `brokers/mstock.py` | **edit** |

**What to build**

Implement each method from P3.1 against mStock's TypeA API. Reuse the existing session/token:

```python
class MStockBroker(BrokerAuthBase, BrokerOrderBase):
    def place_order(self, order):
        resp = self._session.post(
            "/openapi/typea/orders/regular",
            json=self._map_order_to_broker_payload(order),
            headers=self._session_token_headers(),
        )
        resp.raise_for_status()
        return BrokerOrderId(resp.json()["order_id"])

    def cancel_order(self, order):
        resp = self._session.delete(f"/openapi/typea/orders/{order.broker_order_id}", ...)
        resp.raise_for_status()

    # ... implement modify_order, get_order_book, calculate_order_margin similarly
```

> **Endpoint map (from `docs/archive/mstock-typea-api-reference.md`):**
> `place_order` → `POST /openapi/typea/orders/regular`
> `modify_order` → `PUT .../orders/{id}` · `cancel_order` → `DELETE .../orders/{id}`
> `get_order_book` → `GET .../orders` · `calculate_order_margin` → `POST .../calculate-margin`

**Test to write:** `tests/brokers/test_mstock_order_mock.py` — mock `requests` responses, assert:
- correct method + URL + payload for each call
- response parsed into `BrokerOrderId`
- non-200 raises

**Acceptance (done when):**
- [ ] Each method hits the right endpoint with the right auth header.
- [ ] Responses parsed; errors raise.
- [ ] **Never against a real account in CI** — always mocked.

---

## TICKET P3.3 — Pluggable fill provider in `OrderExecutor`

**Files**

| Path | Action |
|---|---|
| `simulator/execution.py` | **edit** |
| `simulator/fill_providers.py` | **create** |

**What to build**

```python
# simulator/fill_providers.py
class SimulatedFillProvider:
    """Paper — fills at next-bar open + fee/slippage models."""
    def get_fill(self, order, next_bar) -> Fill:
        price = next_bar.open
        slip  = self.slippage_model.apply(price)
        fee   = self.fee_model.compute(price, order.quantity)
        return Fill(order=order, price=slip, quantity=order.quantity, fee=fee)

class BrokerFillProvider:
    """Live — sends to broker, gets REAL fill back."""
    def __init__(self, broker): self.broker = broker
    def get_fill(self, order, next_bar) -> Fill:
        broker_id = self.broker.place_order(order)   # ← the ONLY real divergence
        real      = self.broker.poll_fill(broker_id)
        return Fill.from_broker(real, broker_order_id=broker_id)
```

And in `execution.py`, `step()` calls `self._fill_provider.get_fill(order, completed_bar)` instead of building the fill inline.

> **Junior note:** this is the **only** code path that differs between paper and live. Everything above `Fill` (portfolio, position, risk, metrics) is shared — that's what keeps live P&L equal to paper P&L (same math, real fills).

**Test to write:** `tests/simulator/test_fill_providers.py`
```python
def test_simulated_fill_uses_next_bar_open_and_fees():
def test_broker_fill_calls_place_order_and_sets_broker_id():
def test_executor_uses_injected_provider():   # executor with mock provider fills via it
```

**Acceptance (done when):**
- [ ] Paper runs use `SimulatedFillProvider`.
- [ ] Live runs use `BrokerFillProvider` (ordering via `submit`).
- [ ] All existing simulator tests pass (provide simulated provider by default).

---

## TICKET P3.4 — Fold old polling into a live feed; delete `live_engine.py`

**Files**

| Path | Action |
|---|---|
| `data/mstock_live_feed.py` | **create** |
| `forward/live_engine.py` | **delete** |

**What to build**

Move the 60s mStock polling loop from `forward/live_engine.py` into a proper feed class:

```python
# data/mstock_live_feed.py
class MStockLiveFeed(BaseSource):
    def __init__(self, mstock_client, poll_interval_s=60):
        self.client = mstock_client
        self.interval = poll_interval_s
    def iter_bars(self):
        while True:
            bar = self.client.get_latest(...)      # real-time bar
            yield bar
            time.sleep(self.interval)
```

**Delete** `forward/live_engine.py` entirely. **Also delete** any reference to phantom `forward_test_state` / `forward_test_trades` / `forward_test_equity` tables (they have no DDL anywhere — dead code).

**Acceptance (done when):**
- [ ] `MStockLiveFeed` yields real-time bars.
- [ ] `forward/live_engine.py` gone; `grep live_engine src/` returns nothing.
- [ ] No references to phantom `forward_test_*` tables remain.

---

# PHASE 4 — Portfolio UI Restructure + Cleanup

---

## TICKET P4.1 — Landing page + Paper/Live subpages

**Files**

| Path | Action |
|---|---|
| `templates/portfolio.html` | **create** (landing) |
| `templates/portfolio_paper.html` | **create** |
| `templates/portfolio_live.html` | **create** |
| `api/portfolio.py` | **edit** |
| `web/app.py` (routes) | **edit** |

**What to build**

Routes:
```python
@app.route('/portfolio')                  → render portfolio.html  (combined summary)
@app.route('/portfolio/paper')           → render portfolio_paper.html (mode=='paper')
@app.route('/portfolio/live')            → render portfolio_live.html  (mode=='live')
```

API filtering:
```python
def list_instances(mode=None):
    q = PortfolioInstance.query
    if mode: q = q.filter_by(mode=mode)
    return q.all()
```

**Acceptance (done when):**
- [ ] `/portfolio` shows summary of both buckets.
- [ ] `/portfolio/paper` = mode 'paper' only; `/portfolio/live` = 'live' only.
- [ ] No live instances leak onto the paper page or vice-versa.

---

## TICKET P4.2 — Visible mode/source badges

**Files**

| Path | Action |
|---|---|
| `templates/_macros.html` | **create** |

**What to build**

```html
{% macro badge(mode, source) %}
  <span class="badge {{ 'badge-live' if mode=='live' else 'badge-paper' }}">
    {{ mode|upper }}/{{ source|upper }}
  </span>
{% endmacro %}
```

**Acceptance (done when):**
- [ ] Every instance card shows `PAPER/SYNTH`, `PAPER/MSTOCK`, or `LIVE/MSTOCK`.
- [ ] Badges are text-based (accessible), not color-only.

---

## TICKET P4.3 — Delete orphans / unify config / fix timeframe drift

**Files**

| Path | Action |
|---|---|
| `marketdata/`, `alerts/`, `analysis/`, `config_manager/`, `dashboard/` | **delete** |
| `config/market_data.yaml`, `config/time_sync.yaml` | **delete** |
| `db/config.py` | **edit** (single DB-URL authority) |
| `data/db_source.py`, `(old) live_engine` refs | **edit** (call `db/config.py`) |
| timeframe naming | **edit** to one canonical set |

**What to build**

Unify DB-URL resolution into `db/config.py` only. Fix timeframe drift to ONE canonical naming (decide with your lead: e.g. `1min` / `4hour` / `1d`, and update schema CHECK + config + code consistently).

**Acceptance (done when):**
- [ ] `grep -r "marketdata/\|live_engine\|config_manager\|dashboard/" src/` returns nothing.
- [ ] Exactly one file resolves the DB URL.
- [ ] One canonical timeframe naming used across config, DB, and engine.

---

# THE COMPLETE TICKET BOARD

| # | Ticket | Files (create/edit/delete) | Tests |
|---|---|---|---|
| P1.1 | mode/source columns | `002_add_mode_source.sql` (+runner) | `test_migrations_002` |
| P1.2 | source registry | `data/source_registry.py` | `test_source_registry` |
| P1.3 | **fill-timing fix** | `simulator/execution.py` | `test_fill_timing` |
| P1.4 | PaperRunner | `forward/paper_runner.py` + delete 5 ❌ | `test_paper_runner` |
| P1.5 | backtest≈forward proof | (test only) | `test_engine_equivalency` |
| P2.1 | BacktestDriver | `engine/backtest_driver.py` | `test_backtest_driver` |
| P2.2 | route API → driver | `api/backtest.py` | `test_api_backtest` |
| P2.3 | process pool | `api/backtest.py` | `test_api_backtest_parallel` |
| P3.1 | broker ABC order contract | `brokers/base.py` | `test_base_contract` |
| P3.2 | mStock HTTP orders | `brokers/mstock.py` | `test_mstock_order_mock` |
| P3.3 | pluggable fill provider | `simulator/fill_providers.py` + `execution.py` | `test_fill_providers` |
| P3.4 | live feed + delete old | `data/mstock_live_feed.py` + delete `live_engine.py` | + update | 
| P4.1 | UI subpages | `templates/*` + `api/portfolio.py` + routes | `test_portfolio_ui_views` |
| P4.2 | badges | `templates/_macros.html` | (visual) |
| P4.3 | delete orphans / unify | 5 dirs ❌ + `db/config.py` | update |

---

## Two final rules for every ticket

1. **Never delete a file until its replacement passes tests.** e.g. don't delete `forward/paper.py` until `test_paper_runner` is green on `PaperRunner`.
2. **If a test you didn't touch fails — stop.** It means you broke a contract. Read the failure, find the caller, fix it; don't skip the test.

That's the complete file-by-file ticket board. When your lead is ready to start assigning, every ticket is independently offerable — a junior can pick up P1.1 while another does P1.2, etc. (P1.3 fill-timing is foundational, so start it early.)

Want me to expand any single ticket to a **full copy-paste-ready implementation** (complete file contents, not just skeletons)? If so, name the ticket number.