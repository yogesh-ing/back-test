# Docker / AWS Service Architecture for the Backtest Dashboard

## Goal

Run the trading dashboard and live strategy loop as resilient long-lived services, not as one-off scripts or ad-hoc local processes.

## Current code shape

The project already has the right building blocks for this model:

- `src/backtest/dashboard/app.py`
  - Flask app with `/health`, `/api/*`, and UI HTML
  - long-running HTTP server when `app.run()` is called
- `src/backtest/forward/engine.py`
  - forward-testing / live-style engine concept
- `src/backtest/live/mstock.py`
  - live market data client
- `src/backtest/dashboard/data_provider.py`
  - backend adapter for dashboard JSON

This means the app is already service-shaped, but it still needs deployment wiring.

## Recommended architecture

Use two main runtime services:

1. Dashboard service
   - hosts the browser UI
   - serves REST API
   - reads current portfolio/engine state
   - exposes `/health`

2. Trading worker service
   - owns the running engine loop
   - polls live market data
   - updates portfolio and strategy state
   - persists state snapshots
   - pushes updates to the dashboard via shared storage or a lightweight queue

## Why split into two services?

This separation makes deployment easier and more reliable:

- the dashboard can be scaled independently
- the trading engine can run continuously without being tied to the web process
- restarts do not kill the whole application
- Docker/AWS health checks are simpler
- the UI remains accessible even if the strategy loop is paused or restarting

## Recommended runtime components

### A. Dashboard container

Responsibilities:

- start a production WSGI server
- serve the dashboard UI at port 5000
- call into a shared state store or database
- use `/health` for liveness checks

Recommended server:

- Gunicorn
- optionally behind Nginx if needed

Example command:

```bash
gunicorn --bind 0.0.0.0:5000 --workers 2 backtest.dashboard.app:create_dashboard_app
```

Notes:

- `app.run()` is OK for local dev
- `gunicorn` is the proper production choice for Docker/AWS

### B. Trading worker container

Responsibilities:

- initialize the portfolio and engine
- connect to live market feed
- run the strategy loop
- update shared state or state file
- handle graceful shutdown

Example responsibilities:

```python
# pseudocode
engine = ForwardTestingEngine(config_file="...")
engine.initialize_system()
engine.start()
```

The worker should persist state at defined intervals, for example:

- `state/live_state.json`
- SQLite DB
- Redis cache for ephemeral state
- Postgres for durable portfolio position state

### C. Shared persistence layer

For real service operation, use one of:

- SQLite for a single-instance deployment
- Postgres for production deployment
- Redis or in-memory store for fast ephemeral state

Recommended production default:

- Postgres for core state and trade history
- Redis for lightweight queue/state cache
- local file snapshots as backup or recovery state

## Suggested project layout for deployment

```text
back-test/
├── src/
├── docker/
│   ├── Dockerfile.dashboard
│   ├── Dockerfile.worker
│   └── docker-compose.yml
├── config/
│   └── app.yaml
├── state/
│   └── live_state.json
├── .env
├── requirements.txt
└── README.md
```

## Docker deployment pattern

### docker-compose.yml example

```yaml
version: '3.9'

services:
  dashboard:
    build:
      context: .
      dockerfile: docker/Dockerfile.dashboard
    ports:
      - "5000:5000"
    environment:
      PYTHONPATH: /app/src
      MSTOCK_API_KEY: ${MSTOCK_API_KEY}
      MSTOCK_USERNAME: ${MSTOCK_USERNAME}
      MSTOCK_PASSWORD: ${MSTOCK_PASSWORD}
      MSTOCK_AUTH_MODE: ${MSTOCK_AUTH_MODE}
      MSTOCK_BASE_URL: ${MSTOCK_BASE_URL}
    depends_on:
      - worker
    restart: unless-stopped

  worker:
    build:
      context: .
      dockerfile: docker/Dockerfile.worker
    environment:
      PYTHONPATH: /app/src
      MSTOCK_API_KEY: ${MSTOCK_API_KEY}
      MSTOCK_USERNAME: ${MSTOCK_USERNAME}
      MSTOCK_PASSWORD: ${MSTOCK_PASSWORD}
      MSTOCK_AUTH_MODE: ${MSTOCK_AUTH_MODE}
      MSTOCK_BASE_URL: ${MSTOCK_BASE_URL}
    restart: unless-stopped
```

### Dockerfile.dashboard

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PYTHONPATH=/app/src
EXPOSE 5000
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "backtest.dashboard.app:create_dashboard_app()"]
```

### Dockerfile.worker

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PYTHONPATH=/app/src
CMD ["python", "-m", "backtest.forward.engine"]
```

## AWS deployment recommendations

### Option 1: EC2 or ECS with containers

Best for simple deployment and lower operational overhead.

Recommended setup:

- 1 ECS task for dashboard
- 1 ECS task for trading worker
- Application Load Balancer in front of dashboard
- secrets stored in AWS Secrets Manager or SSM Parameter Store
- ECR image repository for both apps

### Option 2: ECS Fargate

Good default choice when you want a service model without managing EC2 hosts.

- dashboard service: public or private ALB target
- worker service: private task without public access
- environment variables from Secrets Manager
- health checks enabled per task

### Option 3: EKS

Best for advanced orchestration and scaling, if you are already Kubernetes-based.

- separate Deployments for dashboard and worker
- ConfigMaps / Secrets for runtime config
- persistent volumes for state if needed

## Health and reliability requirements

Each service should have:

- `/health` endpoint for the dashboard
- process heartbeat for the trading worker
- graceful shutdown handling
- restart policy
- stored state for resume-after-restart

Recommended health checks:

- dashboard: HTTP 200 on `/health`
- worker: read last heartbeat or state timestamp file

## Recommended state model

### For a simple deployable version

Use:

- state file or SQLite DB for resume state
- a JSON snapshot of portfolio and engine status
- handle startup with resume-on-start logic

Example state keys:

- last_processed_bar
- last_update_time
- portfolio_snapshot
- positions
- cash
- strategy_state
- last_error

## Production hardening checklist

- Replace Flask dev server with Gunicorn
- Put credentials in Secrets Manager / env-only injection
- Add `/health` endpoint and container health probes
- Save exact state for resume on crash
- Use structured logs
- Separate dashboard and worker processes
- Add retry logic for network/auth failures
- Add rate limits / request timeouts for web API
- Add database persistence for trades/history

## Recommended final deployment target

For easiest deployment and maintainability:

- Docker Compose for local/dev
- ECS Fargate or EC2 for AWS production
- separate dashboard and worker containers
- Postgres + Redis as shared state services
- Gunicorn as dashboard runtime
- persistent snapshot + trade DB for resume-safe engine state

## Bottom line

Yes: the current project is already close to a service model.

But for true Docker/AWS deployment, the app should be structured as:

- one long-lived dashboard service
- one long-lived trading worker service
- the engine should not be started as a one-shot script inside the web process

That is the proper path to effortless deployment and reliability.
