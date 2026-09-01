# Portfolio Command Center (Multi-Strategy Forward Testing)

The Forward Testing Engine's **Portfolio Command Center** runs a diversified
book of strategy instances simultaneously — up to 50+ `StrategyRunner` workers
managed by one `PortfolioManager`, with portfolio-wide circuit breakers and an
order-tagging ledger that keeps every fill isolated to its owning runner.

> Source PRD: `instructions/forword-testing.md`
> Full task tracker: `instructions/refactoring-task.md`, `instructions/refactoring-implementationPlan.md`

## Architecture (two layers)

```
PortfolioManager (control tower)
  ├── OrderLedger          PRT-{instance}-{ts}-{seq} tags + fill routing
  ├── PaperBroker          V1 execution (fills at bar close via OrderExecutor)
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
| `forward/paper_runner.py` | `StrategyRunner` + `RunnerConfig` (isolated container, pool scanning, PnL), `OrderLedger` (tag/routing), `PaperBroker`, `StrategyAccount` / `StrategyPortfolio` |
| `forward/portfolio_manager.py` | `PortfolioManager` — lifecycle, aggregation, halt latch, tick dispatch, singleton |
| `forward/risk_supervisor.py` | `RiskSupervisor` / `GlobalRiskConfig` / `RiskReport` |
| `forward/feed.py` | `SyntheticFeed` — deterministic random-walk bars, warmup |
| `data/universe.py` | Symbol universe registry (`NIFTY_50`, `TOP_10_CRYPTO`, …) |
| `api/portfolio.py` | REST + SSE blueprint (`/api/portfolio/*`, `/api/portfolio/stream`) |
| `web/templates/portfolio.html`, `portfolio_paper.html`, `portfolio_live.html`, `_portfolio_center.html` + `web/static/js/portfolio.js`, `deep_dive.js` | Command Center UI (landing + per-bucket pages) |

## Run it

```bash
PYTHONPATH=src python -m backtest.web.app --host 0.0.0.0 --port 5000 --source synthetic
# open http://localhost:5000/portfolio          (landing: both buckets)
# open http://localhost:5000/portfolio/paper    (paper bucket)
# open http://localhost:5000/portfolio/live     (live bucket)
```

Click **＋ Add Instance**, or spawn via the API:

```bash
curl -X POST localhost:5000/api/portfolio/runner/create -H 'Content-Type: application/json' -d '{
  "name": "Swing Momentum", "strategy": "donchian_breakout",
  "target_type": "SYMBOL_UNIVERSE", "universe_id": "NIFTY_50",
  "timeframe": "1hour", "allocated_capital": 2500000, "max_pool_positions": 5,
  "mode": "paper", "source": "synthetic"}'
```

`mode` (`paper`|`live`, default `paper`) and `source` (`synthetic`|`replay`|`mstock`,
default `synthetic`) tag the instance (ticket P4.1); `live` execution wiring is the
remaining F-12 item, so `mode: "live"` today still uses simulated fills.

Circuit-breaker demo (PRD acceptance step 5):

```bash
curl -X POST localhost:5000/api/portfolio/test/breach -H 'Content-Type: application/json' \
     -d '{"crash_pct": 0.30}'
```

## API

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/api/portfolio/summary?mode=` | Aggregate stats + per-instance rows (optional bucket scope: `paper` / `live`) |
| GET | `/api/portfolio/universes` | Universe catalogue |
| POST | `/api/portfolio/runner/create` | Spawn a runner (accepts `mode`/`source`) |
| GET | `/api/portfolio/runner/<id>` | Deep-dive detail |
| POST | `/api/portfolio/runner/<id>/control` | `pause` / `resume` / `stop` / `flatten` / `start` / `deep_dive` |
| DELETE | `/api/portfolio/runner/<id>` | Remove a runner |
| POST | `/api/portfolio/control/<action>` | `pause_all` / `resume_all` / `stop_all` / `emergency_flatten` / `reset_breaker` |
| POST | `/api/portfolio/emergency_stop` | Global emergency flatten + halt |
| POST | `/api/portfolio/test/breach` | Simulated crash (circuit-breaker test) |
| GET | `/api/portfolio/stream` | SSE — JSON snapshot every second (bucket-scoped when on a bucket page) |

## Behavior changes & known caveats

- **Fill anchor (portal center):** `StrategyRunner` signals computed on a bar and
  executed by `PaperBroker.submit_market` fill at the **supplied price (the bar's
  close)** through each runner's zero-cost `OrderExecutor` (`free_executor`).
  This is a separate subsystem from the `ForwardTestingEngine` — the **F-01
  look-ahead fix (F-15)** changed the engine's fills to the **next bar's open**,
  so **engine P&L numbers changed** while command-center numbers were not
  affected by that fix.
- **Transition-based signals (F-17):** `StrategyAdapter` decisions are
  transition-based (`_last_target`); persistent signals fire once per `0→1`/`1→0`
  transition. Command-center runners (`StrategyRunner._signal_for`) use their own
  per-buffer signal evaluation and are unaffected.
- **Fill timing vs backtest (P1.5):** backtest ≈ forward only on **gapless** bars
  (`open[t] == close[t-1]`); on real gapped data the two anchors differ by design.
- **In-memory only:** runners/manager state survives a page refresh, not a process
  restart (documented V1; persistence is V2).
- **Paper vs live:** `mode=paper` = simulated fills everywhere; `mode=live` buckets
  are wired for tags/UI but **live broker fills are still open** (findings F-12 —
  `BrokerFillProvider` + `MStockLiveFeed` exist but the forward-engine wiring and
  `poll_fill` in the broker ABC remain).

## Tests & benchmark

```bash
PYTHONPATH=src pytest tests/test_portfolio_engine.py tests/test_api_portfolio.py tests/test_circuit_breakers.py -q
PYTHONPATH=src python benchmarks/benchmark_portfolio.py
```

50-runner benchmark: ~311 ms/tick, 1,287 fills with **0** cross-contamination,
~130 MB RSS (2.6 MB/runner); breaker halt measured at ~15 ms (budget < 500 ms).
