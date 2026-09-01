# Forward Testing

## Change Log — Forward-engine fill-timing fix (F-01)

> **Date:** 2026-08-31 · **Tickets:** F-01 (fix) / F-05 (API changelog) / F-15
> (canonical-number change) / F-17 (transition-based signals)
>
> **Added 2026-09-01 · Ticket F-04** — state file format v2 (see below).
>
> **Updated 2026-09-01 · Ticket #7** — state file format **v3**: full resume
> fidelity (executor queue + engine runtime); see State Persistence below.

### State-file format v3 (F-04 + #7)

`state/forward_state.json` carries `state_version: 3`, `engine_id`, the run
classification `mode` / `source` (see → State Persistence below), and the
`executor`/`engine_runtime` resume fields. **Old v1/v2 files still load**
(portfolio + adapter state restored; in-flight execution state is not
available from them) and are rewritten to v3 on the next save; files from a
*newer* version are refused.

### Removed API (breaking) — F-05

Anything importing these old `StrategyAdapter` names must switch:

| Removed | Replacement / note |
|---|---|
| `StrategyAdapter.execute_signals(...)` | `StrategyAdapter.create_orders(...)` — returns `Order` objects, **no fill side-effect** |
| `StrategyAdapter.on_order_filled(...)` | removed — the adapter no longer owns an executor or observes fills |
| `StrategyAdapter(executor=...)` | constructor param removed — the **engine** owns the `OrderExecutor` and drives the bar clock |
| `ForwardTestingEngine` fill behavior | orders now **arm** and fill at the **next bar's open** (was: signal bar's close) |

### Behavioral change — F-17

- Persistent signals now fire **once per transition** (`0→1` / `1→0`), not every bar.
  Signal decisions are transition-based (`_last_target`), not portfolio-position-based.
- Unfilled **submitted** orders still retry via the executor queue (`step()` re-attempts
  working orders at each next bar's open).
- **Creation-time rejections** (insufficient funds, `can_open_position` denial, short
  disabled) no longer retry — they are logged with a `skip_reason`.

### Canonical-number change (F-15)

> **Note:** the forward-engine look-ahead fix changes forward P&L results vs. prior
> versions. Fills now anchor to the **next bar's OPEN** (not the signal bar's CLOSE),
> so equity/returns will differ. This is the intended, correct behavior — see the
> migration design docs (§P1.3 fill timing, `tests/simulator/test_fill_timing.py`) and
> the one-line note in `docs/PORTFOLIO-CENTER.md` → Behavior changes.

---

## What It Is

Forward testing is **paper trading** — simulating a strategy bar-by-bar without
risking real money. There are **two distinct forward paths** in this repo; keep
them straight:

1. **`ForwardTestingEngine`** (`forward/engine.py`, CLI / Docker / systemd) — the
   real bar-by-bar engine. Live loop polls market data; backtest replay mode
   iterates historical bars. Signals are generated bar-by-bar and **executed**
   through the simulator's order/fill machinery.
2. **Web Forward replay** (`api/forward.py`, the `/forward` page) — **not an
   execution path.** It runs the whole backtest up front and merely *reveals* the
   precomputed equity/position series bar-by-bar on a server clock. No orders, no
   fills, no DB writes.

The CLI walk-forward / live-paper commands (`papertrade`) run through a third,
canonical loop — `PaperRunner` on `simulator/engine_loop.py` — the same loop
`BacktestDriver` uses.

## How It Differs from Backtesting

| Aspect | Backtest (vectorized quick-screen) | Forward engine / PaperRunner |
|--------|-------------------------------------|------------------------------|
| **Execution** | Vectorized, all bars at once (fill ≈ previous close) | Bar-by-bar via the simulator order/fill path |
| **Fill anchor** | `shift(1)` position — fill at prior bar's close | signal on bar `t` → **fill at bar `t+1`'s OPEN** (F-01) |
| **Speed** | Instant | Configurable loop interval / replay speed |
| **Purpose** | "How would this have performed?" | "How does this feel to trade?" |
| **State** | Stateless | Stateful (portfolio, orders, fills, equity) |
| **Cost model** | flat commission/slippage percentages | fee + slippage models through `OrderExecutor` |

## Architecture

```
ForwardTestingEngine (forward/engine.py)
    │
    ├── StrategyAdapter (forward/strategy_adapter.py)
    │      signal generation + Order creation ONLY (no fills — F-01)
    │
    ├── OrderExecutor (simulator/execution.py)
    │      engine drives the bar clock: submit(order) → step(next bar)
    │      fills at the NEXT bar's open. The only fill path.
    │
    ├── Portfolio (simulator/portfolio.py) — cash, positions, orders, equity
    ├── PositionSizer / RiskManager / StopManager (simulator/)
    ├── MarketDataHandler (live/ or mock) — bar source in live mode
    └── State (state/forward_state.json — engine snapshot, atomic write)
```

Shared canonical loop: `simulator/engine_loop.run_engine_loop` — used by
`PaperRunner` (papertrade CLI) and `BacktestDriver` (backtest). It hard-codes the
same `submit → step(next-open)` rule.

## Key Files

| File | Purpose |
|------|---------|
| `forward/engine.py` | `ForwardTestingEngine` — main loop (live + backtest replay) |
| `forward/paper_runner.py` | `PaperRunner` (canonical loop), CLI walk-forward/live paper trade (`run_walkforward`, `run_live_papertrade`), `StrategyRunner` + `OrderLedger` + `PaperBroker` (portfolio center), `StrategyAccount`/`StrategyPortfolio` |
| `forward/strategy_adapter.py` | wraps `Strategy` — signals + `Order` creation only (no fills) |
| `simulator/engine_loop.py` | canonical bar-clock loop (arm on submit, fill at next bar's open) |
| `simulator/execution.py` | `OrderExecutor` — `submit`/`step` bar-clock API + `execute` |
| `forward/portfolio_manager.py` | multi-strategy command center (see PORTFOLIO-CENTER.md) |

## API Endpoints

### Start

```bash
POST /api/forward/start
{
    "strategy": "sma_crossover",
    "symbol": "RELIANCE",
    "timeframe": "1D",
    "from_date": "2024-01-01",          # optional — defaults are reported back
    "to_date": "2024-12-31",            # optional
    "capital": 100000,
    "params": {"fast": 20, "slow": 50},
    "mode": "synthetic",                # "live" requires broker auth (403 otherwise)
    "bars_per_second": 5                # optional clock override (0 = manual stepping)
}
```

```json
{
  "status": "running",
  "state_id": "9f3c1a7e6b0d42c1a0e7f5d3b1a9c8e2",
  "total": 262, "revealed": 1, "bars_per_second": 5.0,
  "symbol": "RELIANCE", "strategy": "sma_crossover",
  "config": { "from_date": "2024-01-01", "to_date": "2024-12-31", "timeframe": "1D",
              "capital": 100000.0, "params": {"fast": 20, "slow": 50} },
  "defaults_applied": []
}
```

- `state_id` addresses **this** replay: pass `?state_id=…` to `/status`, `/trades`,
  `/equity`, and in the `/stop` body. Omit it and you get the most recently
  started session (which is what the Dashboard does).
- The date range is **optional** — the PRD start body is `{strategy, symbol,
  params}` — but anything the server fills in is listed in `defaults_applied`
  and echoed in `config`, and the page warns when it is non-empty. Malformed
  (`01-01-2024`) or inverted ranges are a `400` before any data is fetched.
- `403` when `mode` is `live` and no broker session is authenticated.

> ⚠️ **Replay, not execution.** This endpoint reveals a precomputed backtest; it
> does **not** run the strategy per bar and does not write orders/fills. For real
> per-bar execution use `ForwardTestingEngine` or the `papertrade` CLI.

### The clock

Bars are revealed by a **server-side timer** (`FORWARD_REPLAY_SPEED`, default 1
bar/s) — not by polling. `GET /api/forward/status` is a pure read, so:

- two open tabs (or the Dashboard's 3 s refresh) cannot double-advance one run;
- closing the browser does not freeze the bot;
- the replay completes and auto-stops on its own;
- `bars_per_second: 0` freezes it, for step-by-step debugging (`session.advance(n)`).

### Status

```bash
GET /api/forward/status?state_id=9f3c…
```

```json
{
  "state_id": "9f3c…", "status": "running",
  "progress": { "revealed": 61, "total": 262, "pct": 23.28 },
  "metrics": { "total_pnl": -3036.32, "total_return_pct": -3.04, "win_rate_pct": 0.0,
               "max_drawdown_pct": -4.1, "sharpe": -1.8, "total_trades": 1,
               "closed_trades": 0, "open_trades": 1, "final_equity": 96963.68 },
  "equity":   { "dates": ["…"], "values": […], "benchmark": […] },
  "drawdown": { "dates": ["…"], "values": […], "worst_dd_pct": -4.1, "worst_dd_date": "…" },
  "trades":   [ { "id": 1, "date": "2024-03-11", "exit_date": "…", "side": "LONG",
                  "entry": 111.57, "exit": 106.75, "pnl": -415.6, "result": "Loss",
                  "is_open": true } ],
  "signals":  { "candles": […], "buys": […], "sells": […] },
  "positions": [ { "symbol": "RELIANCE", "side": "LONG", "qty": 1.0, "exposure_pct": 100.0,
                   "entry": 104.48, "current": 101.28, "price_change_pct": -3.06,
                   "unrealized_pnl": -3036.32, "unrealized_pnl_pct": -3.13,
                   "entry_date": "2024-03-11", "bars_held": 9 } ],
  "config": { "strategy": "sma_crossover", "symbol": "RELIANCE", "timeframe": "1D", "…": "…" },
  "total_bars": 61, "last_bar_ts": "2024-03-27", "market_open": true,
  "unrealized_pnl": -3036.32, "error": null
}
```

`equity` / `drawdown` / `trades` / `metrics` use the **same shape as the
Backtest page**, so `equity_chart.js`, `drawdown_chart.js`, `metrics_cards.js` and
`trade_table.js` render them unchanged.

`win_rate_pct` counts **closed** trades only (see `docs/BACKTEST-ENGINE.md` →
Trade Accounting); with nothing closed yet the UI shows `—` rather than 0.00%.

### Other endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/forward/stop` | stop one session (`{"state_id": …}` in the body) or the active one |
| GET | `/api/forward/sessions` | every replay in memory, newest first, `active` flagged |
| GET | `/api/forward/trades` | trade rows with `status: "open" | "closed"` |
| GET | `/api/forward/equity` | `[{ts, equity}]` — `/status.equity` is the component-ready form |

Unknown `state_id` → `404 {"error": "unknown session: …"}`; no session started →
`{"status": "idle", "progress": {"revealed": 0, "total": 0, "pct": 0.0}}`.

**No-lookahead in the payload.** Signals and candles are cut at the revealed bar,
so a replay at 23% progress cannot show an entry from month 8 (gap G4 — the
filter used to be a no-op).

## State Persistence

- **CLI live-paper mode** (`papertrade --mode live`) saves/loads
  `.live_papertrade_state.json` (default; `--state-file` overrides) and resumes
  with `--resume-on-start`:
  ```json
  {
      "processed_bars": 150,
      "resume_count": 3,
      "last_date": "2024-06-15",
      "positions": [...],
      "equity_curve": [...]
  }
  ```
- **`ForwardTestingEngine`** snapshots full system state to
  `state/forward_state.json` (`system.state_file`) every N minutes and on stop —
  atomic write, restorable on restart.
  - **Format v3 (F-04 + ticket #7, 2026-09):** the payload carries
    `state_version: 3`, `engine_id` (the `portfolios.portfolio_id`), the run
    classification `mode` (`paper`|`live`) + `source`
    (`synthetic`|`replay`|`mstock`) — the same vocabulary as the `portfolios`
    columns (migration 002), derived from the engine's **actual** `config.data`
    (a `backtest` data mode is stored as the `paper` bucket — simulated
    fills). Source strings come from the canonical
    `backtest.data.source_tags.SOURCE_TAGS`.
  - **v3 = full resume fidelity (ticket #7):** `executor` captures the
    in-flight bar-clock queue (pending orders + which are already armed) and
    `engine_runtime` captures `loop_count`, the last processed bar timestamp
    per symbol, and per-symbol processed-bar counts. A restored engine
    continues the same canonical bar clock — an order armed at teardown fills
    at the very next bar's open, and a backtest replay resumes at the next
    unprocessed bar instead of re-running the history.
  - **Legacy v1/v2 files still load** — they are normalized in memory
    (`mode`/`source` filled from the engine config; invalid values warn +
    fall back) and rewritten to v3 on the next save. Caveat: v2 files have no
    in-flight execution state, so a resume from one may miss an order that
    was armed at teardown (portfolio + adapter state still restore). A file
    whose `state_version` is *newer* than this build is refused (warn + no
    load).
- **Web Forward sessions** are **in-memory only** (`MAX_SESSIONS = 20`): they
  survive a page refresh but not a process restart.

## Risk limits (per-bucket, ticket #9)

Risk limits are keyed on the run's classification (`mode` × `source` from
`_classify()`), never on a global knob. The canonical defaults live in
one place — `backtest/simulator/bucket_risk.py::BUCKET_RISK_LIMITS` — and
flow through the engine at the same point classification resolves:

| Bucket | Caps (canonical defaults) | Source gate |
|---|---|---|
| `paper` (simulated fills) | free play — all exposure caps open (None), leverage 1 | any source (`synthetic`, `replay`, `mstock`) |
| `live` (real fills) | `max_position_value` 10 000, `max_position_pct` 0.10, `max_gross_exposure_pct` 0.50, `max_open_positions` 5, `min_trade_value` 1 000, leverage 1 | **only `mstock`** — `live`/`synthetic` and `live`/`replay` are refused before any trading (fake data must never feed real fills) |

Wiring:

- `initialize_system` calls `resolve_bucket_risk(mode, source, ...)` right
  after `_classify`; an unknown bucket/source **raises** (never a soft warn).
- The bucket becomes the portfolio's limits (`PortfolioLimits`),
  the sizer's hard constraints (`SizingConstraints` — the size that reaches
  the executor is already bucket-limited), and the pre-trade `RiskManager`
  config (drawdown/daily-loss limits stay config-level).
- Every **real-fill** order passes the bucket risk check before
  `executor.submit`; paper stays free play (permissive caps, no order-time
  check).
- The T8 no-downgrade guard has risk teeth: when a restored portfolio's
  classification had to be changed, the **open book** is checked against the
  target bucket's caps — a violation refuses the run ("RISK REFUSAL") instead
  of silently trading at the wrong size.
- Explicit config overrides per bucket: `risk.buckets.<bucket>` in
  `forward_testing.yaml` (or `config_dict["risk"]["buckets"]`) merges over
  the canonical defaults; `None` disables a cap explicitly.

The canonical backtest runner (`run_backtest`) resolves the `paper` bucket
(permissive), so historical P&L is unchanged by this ticket.

## CLI Usage

### Walkforward Mode
```bash
PYTHONPATH=src python -m backtest papertrade \
  --mode walkforward \
  --strategies sma_crossover,rsi_reversion \
  --source synthetic \
  --symbol DEMO \
  --from 2024-01-01 \
  --to 2024-12-31 \
  --interval 1day \
  --capital 100000
```

### Live Mode (with polling)
```bash
PYTHONPATH=src python -m backtest papertrade \
  --mode live \
  --strategies sma_crossover \
  --source synthetic \
  --symbol DEMO \
  --from 2024-01-01 \
  --to 2024-12-31 \
  --interval 1day \
  --poll-seconds 60 \
  --state-file .live_papertrade_state.json \
  --resume-on-start
```

Both modes run through `forward/paper_runner.py` on the shared
`simulator/engine_loop.py` loop (fills at the next bar's open).

## Forward-engine traffic rules (after F-01)

1. **Signal on bar `t`** → adapter produces `Signal` + `Order` (`create_orders`),
   engine calls `executor.submit(order)` — the order is **armed**, not filled.
2. **`executor.step(bar t+1)`** → the order fills at **`t+1`'s OPEN**. The signal
   bar's close is never a fill price.
3. Only **new** bars advance the clock — repeated polls of the same bar do not
   cause a second fill (`_last_bar_ts` dedupe in `ForwardTestingEngine`).
4. Stops (StopManager) still create exits as orders; risk checks (`RiskManager`)
   gate `can_open_position` at order-creation time.

## Safety Rules

1. **Live is never simulated (ticket #8)** — the forward engine wires the
   executor to a real broker (`BrokerFillProvider`) whenever
   `config.data.mode: "live"`: the portfolio, state file and DB rows all
   classify `live`/`mstock`, and fills come from the venue (via
   `place_order` + `poll_fill`), never from the simulated pricing engine.
   Paper runs keep the simulated provider. An order still working after an
   unfilled poll is POLLED again — the venue is never double-placed.
2. **Auth guard** — `mode: "live"` on the web replay requires an authenticated
   broker session (403 otherwise); `mode: "synthetic"` is explicitly exempt so
   the loop is testable without credentials. Direct fills fail cleanly without
   a session (mStock `_require_session`).
3. **No silent downgrade, with risk teeth** — a live run restores/classifies
   as live (a stale paper-tagged portfolio is upgraded with a warning); a
   paper run never claims live. And per-bucket risk limits (see `Risk limits
   (per-bucket)`): a mis-classified portfolio whose open book violates the
   target bucket's caps is **refused**, never traded at the wrong size.
4. **State isolation** — web sessions are keyed by `state_id` in a bounded
   registry (`MAX_SESSIONS = 20`); stopping one never touches another.
4. **No lookahead** — the strategy only ever sees completed bars; the engine fills
   at the **next bar's open**, never the signal bar's close.
5. **Bounded clock** — `bars_per_second` is clamped to 0…5000; engine loop
   interval and replay speed are config-bounded.
