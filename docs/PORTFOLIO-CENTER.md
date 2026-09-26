# Portfolio Command Center (Multi-Strategy Forward Testing)

The Forward Testing Engine's **Portfolio Command Center** runs a diversified
book of strategy instances simultaneously — up to 50+ `StrategyRunner` workers
managed by one `PortfolioManager`, with portfolio-wide circuit breakers and an
order-tagging ledger that keeps every fill isolated to its owning runner.

> Source PRD: `instructions/archive/forword-testing.md`
> Full task tracker: `instructions/archive/refactoring-task.md`, `instructions/archive/refactoring-implementationPlan.md`

## Architecture (two layers)

```
PortfolioManager (control tower)
  ├── OrderLedger          PRT-{instance}-{ts}-{seq} tags + fill routing
  ├── PaperBroker          V1 execution (fills at bar close via OrderExecutor)
  ├── RiskSupervisor       daily-loss + max-drawdown breakers, concentration warning
  ├── SyntheticFeed        per-second OHLCV bars (swappable for mStock)
  ├── IntelligenceService  portfolio Greeks / concentration / correlation / regime
  │     └── AlertBroker    alert pub/sub → alert widget, subscribed strategies, audit log
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
| `intelligence/*`, `alerts/*`, `api/intelligence.py` | Portfolio Intelligence & alerts — see [PORTFOLIO-INTELLIGENCE.md](PORTFOLIO-INTELLIGENCE.md) |
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
| GET | `/api/portfolio/stream` | SSE — JSON snapshot every second (bucket-scoped when on a bucket page), carries `positions` + `orders_summary` + a compact `intelligence` block (net Greeks, scenarios, alert counts) |
| GET | `/api/portfolio/positions?mode=` | Flat row per open position (equity + option structures) with its manual stop/target |
| POST | `/api/portfolio/position/action` | `modify_stop_loss` / `clear_stop_loss` / `modify_target` / `clear_target` / `close_fraction` / `close_all` on one position |
| GET | `/api/portfolio/orders?mode=&instance_id=&status=&limit=` | The order ledger, newest first + a summary strip (counts, slippage, oldest working age) |
| POST | `/api/portfolio/orders/<coid>/cancel` | Cancel a still-PENDING order — **at the venue first** for a live order (404 unknown, 409 already terminal / venue refused) |
| POST | `/api/portfolio/orders/<coid>/modify` | Amend a working order's quantity and/or limit price at the venue (409 for a paper/terminal/untracked order or a venue refusal) |

Portfolio Intelligence adds `/api/portfolio/greeks`, `/api/portfolio/concentration`,
`/api/portfolio/correlation`, `/api/portfolio/intelligence`, `/api/market/*` and
`/api/alerts/*` — documented in [PORTFOLIO-INTELLIGENCE.md](PORTFOLIO-INTELLIGENCE.md#api).

## Risk Board — Portfolio Intelligence

The **Risk Board** tab now opens with five collapsible sections above the
per-runner risk table (open/closed state kept in `localStorage`):

| Section | Content | Refresh |
|---|---|---|
| Portfolio Greeks | net Δ / Γ / Vega / Θ with bias, scenario P&L (NIFTY ±2%, IV ±5, crash), per-strategy breakdown | 1 s |
| Concentration | exposure % per underlying (flag above 60%), strike clusters (3+) | 1 s |
| Correlation | runner-vs-runner P&L correlation heatmap (> 0.8 highlighted) | 5 min |
| Market Regime | VIX band (or labelled realized-vol proxy), 24 h change, transition badge, history chart, strategy regime fit | 30 s view / 5 min samples |
| Market Activity | OI anomalies and liquidity dry-ups from option chains (collapsed by default) | 30 s |

Polling only runs while the Risk Board tab and the browser tab are visible.
On `/portfolio/paper` and `/portfolio/live` the numbers are scoped to that
bucket; *All buckets* aggregates both. Alert deep links
(`/portfolio?tab=risk#pi-greeks`) open this tab, expand the section and
highlight it.

Alerts derived from these numbers appear in the global **alert widget**
(bottom-right, every page) — see [ALERTS-GUIDE.md](ALERTS-GUIDE.md). They are
information only: no alert closes or blocks a position; subscribed strategies
decide for themselves ([STRATEGY-ALERTS.md](STRATEGY-ALERTS.md)).

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
- **State persistence (V2, 2026-09-23):** runner configs, books, anchors and manual
  stop/target levels round-trip through `PORTFOLIO_STATE_PATH`; a restored runner comes
  back **PAUSED** (fail-closed). Two deliberate exceptions: breaker latches are written
  but **not re-armed** on boot (a halt guards one session's P&L — a restored latch trapped
  the dashboard in a permanent "🔴 HALTED"), and **ledger orders are not persisted**, so the
  Orders tab starts empty after a restart and fills the moment new orders route through.
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

---

## Live Order Management (the two trading tabs)

The Command Center answers "what am I holding" and "did my order fill" with two
separate tabs — the positions tab was **enhanced**, the Orders tab is new, and
every other tab is untouched.

**Open Positions** — one flat row per open position across runners (an option
structure is one net row, with its legs listed underneath). Each row carries its
side, size, entry, mark, P&L, the **Target** and **Stop** levels when armed, net
Δ/Θ for option structures, and an **Actions** column:

| Button | Sends | Notes |
|---|---|---|
| 🛑 SL | `modify_stop_loss` | Refused if it would fire immediately; Clear level disarms |
| 🎯 TP | `modify_target` | Same validation on the other side of the mark |
| ◐ 50% | `close_fraction` | Equity only — an option structure closes atomically, so it is pinned to 100% |
| ✕ All | `close_all` | Full exit at market |

Levels are **prices**, not percentages: the share price for an equity position,
the **net premium per unit** for an option structure (for a credit structure the
stop sits above the mark and the target below — the engine derives the side). They
are evaluated on every bar **and** on every mark move, so a manual stop exits at
market even if the strategy never trades again; the strategy's own exit policy
still runs, and whichever fires first wins. On a live runner "close" can only be
**placed** at the venue (`status: "placed"`, a client order id, no fill yet) — the
UI says so instead of claiming the position is closed.

**Orders** — the engine's own `OrderLedger`, not a second bookkeeping layer:
PENDING (with age, and the only cancel button in the app), FILLED (requested vs
filled price), REJECTED (with the venue/engine reason — a paper order that cannot
fill is rejected, it never rests forever), CANCELLED. Slippage is
**adverse-positive per unit** (+₹2 means the fill cost money, whichever side).
The summary strip carries counts, average/worst slippage and the oldest working
order's age; the tab badge (seeded from the SSE snapshot, so it works while the
tab is closed) counts working + rejected orders. Rows are polled at 3 s only
while the tab is visible.

**Advanced order management (Phase 3).**

| Feature | Behaviour |
|---|---|
| Amend a working order | `✎ Amend` (live orders only) → quantity and/or limit price. The **venue is asked first**; a refusal leaves local state untouched and is shown verbatim with the modal still open. The client order id, the original `requested_price` (slippage keeps measuring the *original* decision) and the fill history survive — an amend is not a cancel-and-replace. Only terms that actually differ are sent. |
| Order aging alerts | A working order past **60 s** is `warn`, past **5 min** `alert`. The band ships with the row (`aging`, `age_s`), the row is tinted, the age cell is badged (⏰/🚨), the summary strip counts the bands, and the engine writes **one audit entry per band per order** — an alert that repeats every tick is noise an operator learns to ignore. Filter the tab with `⏰ Aging only`. |
| Auto-retry on rejection | Opt-in per runner (`RunnerConfig.retry_policy`), **off by default**. A refused order that is *safe* to re-send (see below) is queued and retried on the next bars, up to `max_attempts`, no faster than `cooldown_s` and at most once per bar. Every attempt is its own audited ledger row carrying `retry_of` / `retry_attempt`, so the tab shows a lineage rather than duplicate mystery orders. |

Auto-retry is deliberately narrow, because a retry loop is a way to discover a
risk limit N times:

* **Only retryable refusals are retried.** A paper failure leaves nothing at a
  venue, so it is retryable. A *live* placement error is **not**: a timeout can
  mean "accepted, acknowledgment lost", and re-sending there is how one intent
  becomes two live orders. Those are blocked with `RETRY_REFUSED` and a
  "reconcile before re-sending" note in the runner's signal log.
* **The budget is finite.** `max_attempts=2` means at most three sends. A spent
  budget is remembered (`order_retries.blocked`) so the next bar's identical
  signal cannot quietly re-open it — a transition signal keeps firing while its
  condition holds, which would otherwise make `max_attempts` meaningless.
* **The block lifts two ways**: the strategy stops asking for that action (a
  genuinely new decision), or `rearm_after_s` (default 300 s) expires, which
  starts a *new* episode at a bounded rate instead of either giving up forever
  on a long venue outage or hammering it.
* Successful retries clear the block; `get_state()["order_retries"]` reports
  `pending` / `blocked` / `raised` / `recovered` / `exhausted`.

---

## Options (paper & live)

The equity command center above trades stock/underlying positions. Option
structures run through a parallel, options-aware stack with the same shape:
expression layer (strike/expiry selection + structure builders) → atomic
multi-leg execution → risk → expiry handling. Full guide:
**[OPTIONS-PAPER-LIVE.md](OPTIONS-PAPER-LIVE.md)**.

Equity vs options at a glance:

| | Equity runners (above) | Option structures |
|---|---|---|
| Signal | strategy `generate_signals` (+1/0/−1) | `generate_market_view` → `TradeIntent` |
| Execution | `PaperBroker.submit_market` (one symbol per order) | `OptionPaperBroker.execute_structure` (all legs or none) |
| Grouping | runner bucket | `structure_id` per multi-leg trade |
| Risk | `RiskSupervisor` (daily loss / drawdown breakers) | `PreTradeRiskCheck` (margin / position / notional caps) + spread margin |
| Costs | `PAPER_FREE_PROFILE` (zero) | `CommissionCalculator` — full NFO statutory stack (₹20/order + STT + GST + …) |
| End of life | manual close / stop | `ExpiryManager` auto-square-off → cash settlement at intrinsic |
| UI | `/portfolio`, `/portfolio/paper`, `/portfolio/live` | `/options` |

Example: build a bullish view into a spread and execute it atomically:

```python
from datetime import date
from decimal import Decimal
from backtest.options.selector import create_selector
from backtest.options.structures import create_structure
from backtest.options.paper_trading import OptionPaperBroker, FakeQuoteProvider
from backtest.strategy.intent import Direction, MarketView

view = MarketView(direction=Direction.BULLISH, underlying="NIFTY",
                  spot_price=Decimal("24800"))
strikes = create_selector("atm").pick_strikes(Decimal("24800"),
                    [Decimal(s) for s in range(24400, 25201, 100)],
                    Direction.BULLISH, count=2)
intent = create_structure("bull_call_spread").build(
    view, strikes, chain, date(2026, 9, 24), strategy_name="donchian_bull")

broker = OptionPaperBroker(capital=1_000_000, commission_per_lot=20)
positions = broker.execute_structure(intent, FakeQuoteProvider(default_price=120))
```

The options tests (`tests/test_options_*.py`) cover the whole stack,
including the end-to-end integration suite and the ₹27.63 contract-note
fee anchor.
