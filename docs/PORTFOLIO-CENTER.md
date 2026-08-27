# Portfolio Command Center (Multi-Strategy Forward Testing)

The Forward Testing Engine's **Portfolio Command Center** runs a diversified
book of strategy instances simultaneously — up to 50+ `StrategyRunner` workers
managed by one `PortfolioManager`, with portfolio-wide circuit breakers and an
order-tagging ledger that keeps every fill isolated to its owning runner.

> Full task tracker: `instructions/PORTFOLIO-MULTI-STRATEGY-TRACKER.md`
> Source PRD: `instructions/forword-testing-multi-stratergy.md`

## Architecture (two layers)

```
PortfolioManager (control tower)
  ├── OrderLedger          PRT-{instance}-{ts}-{seq} tags + fill routing
  ├── PaperBroker          V1 execution (fills at bar close)
  ├── RiskSupervisor       daily-loss + max-drawdown breakers, concentration warning
  ├── SyntheticFeed        per-second OHLCV bars (swappable for mStock)
  └── StrategyRunner × N   isolated capital bucket, positions, trades, PnL
        ├── SINGLE_SYMBOL   one ticker (e.g. BTC/USD)
        └── SYMBOL_UNIVERSE  a curated pool (NIFTY_50, TOP_10_CRYPTO, …),
                             signals ranked → top-K entries within the bucket
```

Key files (`src/backtest/`):

| File | Role |
|---|---|
| `forward/runner.py` | `StrategyRunner` + `RunnerConfig` — isolated container, pool scanning, PnL |
| `forward/portfolio_manager.py` | `PortfolioManager` — lifecycle, aggregation, halt latch, tick dispatch, singleton |
| `forward/risk_supervisor.py` | `RiskSupervisor` / `GlobalRiskConfig` / `RiskReport` |
| `forward/order_ledger.py` | `OrderLedger` (tag/routing) + `PaperBroker` |
| `forward/feed.py` | `SyntheticFeed` — deterministic random-walk bars, warmup |
| `data/universe.py` | Symbol universe registry (`NIFTY_50`, `TOP_10_CRYPTO`, …) |
| `api/portfolio.py` | REST + SSE blueprint (`/api/portfolio/*`, `/api/portfolio/stream`) |
| `web/templates/portfolio.html`, `web/static/js/portfolio.js`, `web/static/js/deep_dive.js` | Command Center UI |

## Run it

```bash
PYTHONPATH=src python -m backtest.web.app --host 0.0.0.0 --port 5000 --source synthetic
# open http://localhost:5000/portfolio
```

Click **＋ Add Instance**, or spawn via the API:

```bash
curl -X POST localhost:5000/api/portfolio/runner/create -H 'Content-Type: application/json' -d '{
  "name": "Swing Momentum", "strategy": "donchian_breakout",
  "target_type": "SYMBOL_UNIVERSE", "universe_id": "NIFTY_50",
  "timeframe": "1d", "allocated_capital": 2500000, "max_pool_positions": 5}'
```

Circuit-breaker demo (PRD acceptance step 5):

```bash
curl -X POST localhost:5000/api/portfolio/test/breach -H 'Content-Type: application/json' \
     -d '{"crash_pct": 0.30}'
```

## API

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/api/portfolio/summary` | Aggregate stats + per-instance rows |
| GET | `/api/portfolio/universes` | Universe catalogue |
| POST | `/api/portfolio/runner/create` | Spawn a runner |
| GET | `/api/portfolio/runner/<id>` | Deep-dive detail |
| POST | `/api/portfolio/runner/<id>/control` | `pause` / `resume` / `stop` / `flatten` / `deep_dive` |
| DELETE | `/api/portfolio/runner/<id>` | Remove a runner |
| POST | `/api/portfolio/control/<action>` | `pause_all` / `resume_all` / `stop_all` / `emergency_flatten` / `reset_breaker` |
| POST | `/api/portfolio/emergency_stop` | Global emergency flatten + halt |
| POST | `/api/portfolio/test/breach` | Simulated crash (circuit-breaker test) |
| GET | `/api/portfolio/stream` | SSE — JSON snapshot every second |

## Tests & benchmark

```bash
PYTHONPATH=src pytest tests/test_portfolio_engine.py tests/test_api_portfolio.py tests/test_circuit_breakers.py -q
PYTHONPATH=src python benchmarks/benchmark_portfolio.py
```

50-runner benchmark: ~311 ms/tick, 1,287 fills with **0** cross-contamination,
~130 MB RSS (2.6 MB/runner); breaker halt measured at ~15 ms (budget < 500 ms).
