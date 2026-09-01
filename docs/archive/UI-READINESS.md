# UI Readiness and Runtime Requirements

## Current state

> ⚠️ **HISTORICAL (archived).** References modules deleted in the P1.4/P4.3
> refactor — `dashboard/`, `alerts/`, `analysis/`, `config_manager/`,
> `marketdata/`, `forward/{paper,broker,portfolio,runner,order_ledger,live_engine}.py`.
> Kept for history only; do not use as current documentation.
>
The dashboard is currently serving the web shell and API scaffolding, but it is not yet fully wired to a real live trading engine.

What exists today:

- `src/backtest/dashboard/app.py`
  - Flask app with the HTML dashboard UI
  - Routes like `/`, `/api/portfolio`, `/api/positions`, `/api/trades`, `/api/orders`, `/api/status`, `/api/all`
  - Control endpoints like `/api/start`, `/api/stop`, `/api/pause`, `/api/resume`
  - Manual order submission endpoints

- `src/backtest/dashboard/data_provider.py`
  - Converts portfolio, performance, trade, engine, and market-data objects into JSON for the browser
  - Returns zero/default values when the dashboard is created without actual runtime objects

- `src/backtest/forward/__init__.py`
  - Exposes forward-testing engine components and dashboard integration objects, but they are not automatically attached to the web app unless explicitly passed in

## What the UI is currently serving

When the app is started with `create_dashboard_app()` without a real `portfolio`, `engine`, or `data_handler`, the browser receives a working HTML shell, but the data is effectively empty.

This is visible in the current implementation:

- `DashboardDataProvider.get_portfolio_overview()` returns zeros if `self.portfolio is None`
- `get_open_positions()` returns `[]` if there is no portfolio
- `get_recent_trades()` returns `[]` without a portfolio or analyzer
- `get_system_status()` and similar methods are designed to return live/runtime values only if their backing objects exist

The dashboard can therefore be opened and rendered, but it does not yet represent a live or meaningful trading system without real objects attached.

## What must exist to make the UI functional

### 1. A real Portfolio instance

The UI expects a live `Portfolio` object with:

- `positions`
- `current_cash`
- `initial_capital`
- `equity_history`
- `closed_positions`
- `open_position(...)`
- `calculate_total_equity()` and/or equivalent
- `calculate_position_value()`

Without this object, the dashboard will show zeros and empty tables.

### 2. A connected engine or loop

The dashboard calls into `provider.engine` for status and control actions. To be truly usable, the app needs a running engine that can:

- start / pause / stop / resume
- poll market data
- evaluate strategy signals
- update portfolio state
- push equity data to charts

Current code only returns a simulated response in `/api/start` and similar endpoints; it does not start a real background process unless a real engine is wired in.

### 3. A real market-data handler

The UI shows market data status and may rely on current quotes for manual order execution. For this to work, a `data_handler` must provide:

- connection status
- latest quotes or candles
- current symbol/market health
- event-driven data updates

At present, the dashboard does not automatically create a live mStock feed or a live market data loop when started.

### 4. A strategy and signal pipeline

To show realistic values in positions, equity curve, and trades, there must be:

- a loaded strategy
- a signal adapter or similar strategy-to-portfolio bridge
- a data feed with fresh bars
- a broker/execution layer

The app has these abstractions in the forward-testing codebase, but they need to be attached to the UI runtime to become active.

### 5. A real `DashboardDataProvider` configuration

The app must be created with actual objects, for example:

```python
from backtest.dashboard.app import create_dashboard_app
from backtest.forward.engine import ForwardTestingEngine

engine = ForwardTestingEngine(config_file="...")
engine.initialize_system()

app = create_dashboard_app(
    portfolio=engine.portfolio,
    performance=engine.performance,
    trade_analyzer=getattr(engine, "trade_analyzer", None),
    engine=engine,
    data_handler=engine.data_handler,
)
```

This is the minimum pattern needed to turn the static dashboard into a live dashboard.

## Current readiness assessment

Status: partially ready

Ready:

- HTML dashboard shell renders correctly
- API endpoints exist and respond with JSON
- basic styling and charts are in place
- dashboard controls are coded

Not yet ready for meaningful live functionality:

- no real portfolio is attached by default
- no live engine loop is started automatically
- no real market-data source is connected in the dashboard bootstrap path
- no thread-safe state publisher is wired to update the browser continuously
- `api/start` etc are not doing real work yet

## Minimum future requirement to use the UI meaningfully

To fully use the dashboard from the browser, the project needs to provide all of the following at runtime:

1. An initialized `Portfolio`
2. A `ForwardTestingEngine` or equivalent background loop
3. A `MarketDataHandler` or equivalent live feed
4. A strategy adapter and execution pipeline
5. A `DashboardDataProvider` connected to those objects
6. A background refresh loop or event-driven update path to keep the UI alive

Without these, the UI is a functional shell, not a working trading dashboard.

## Service vs single-run program

The current dashboard code is designed as a long-running service process, not as a one-shot executable.

Why:

- `create_dashboard_app()` creates a Flask app object
- `run_dashboard()` calls `app.run(...)`, which starts an HTTP server and keeps listening for requests
- the dashboard is meant to stay alive while clients poll `/api/all` and render the UI

This means it behaves like a service when launched in a container, VM, or EC2 instance, but it is not yet production-grade service architecture.

Important distinction:

- `python -m backtest dashboard` / `app.run()` is a server process, not a one-time script
- it remains alive until you stop it
- however, it is still an ad-hoc development server, not a hardened deployment runtime

For Docker / AWS deployment, the recommended architecture is:

1. Keep the dashboard as a Python web app factory (`create_dashboard_app`)
2. Run it under Gunicorn/Uvicorn/Waitress instead of Flask's built-in server
3. Start the trading engine in a background worker or separate process
4. Keep state in a database or persisted JSON/state store
5. Add health checks and graceful shutdown hooks

Example service model:

```bash
gunicorn --bind 0.0.0.0:5000 backtest.dashboard.app:create_dashboard_app\(\) --workers 2
```

or, better, a factory-based entry from a `wsgi.py` wrapper.

## Deployment recommendation

For Docker / AWS, the system should be deployed as a service with:

- web app container for the dashboard
- worker container for the live engine / strategy loop
- optional queue or DB for state persistence
- health endpoint at `/health`
- environment variables for credentials and market config
- graceful restart logic

## Current assessment

The code is service-shaped, but not yet production-service-complete. It can be run continuously, but it still needs:

- proper app factory for deployment
- worker/process separation for the live engine
- real persistence
- production WSGI server
- health checks and startup wiring

This is the key difference between a prototype dashboard and a deployable production service.

## Practical next step

The next implementation step should be to start the dashboard only after an engine is initialized and passed in with a real portfolio object. At that point, the `/api/all` endpoint can return real trading data and the UI can display the active strategy state, positions, P&L, and orders.

For Docker/AWS deployment, the target should be: one service container for the dashboard and one long-running worker process for the trading loop, both started via a container orchestrator or service manager.
