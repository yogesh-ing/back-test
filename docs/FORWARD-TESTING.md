# Forward Testing

## What It Is

Forward testing is **paper trading** — simulating a strategy in real-time without risking real money. The engine replays historical bars one at a time, revealing them gradually to mimic live trading.

## How It Differs from Backtesting

| Aspect | Backtest | Forward Test |
|--------|----------|-------------|
| **Execution** | Vectorized (all bars at once) | Bar-by-bar (replay) |
| **Speed** | Instant | Configurable — `bars_per_second`, default 1 bar/s |
| **Purpose** | "How would this have performed?" | "How does this feel to trade?" |
| **State** | Stateless | Stateful per `state_id`, server-side (survives refresh, not a restart) |
| **Auth required** | No | Yes (broker auth guard) |

## Architecture

```
ForwardTestEngine
    │
    ├── StrategyAdapter (wraps Strategy for bar-by-bar)
    ├── Portfolio (tracks positions, cash, equity)
    ├── Simulator (execution, fills, commission)
    └── State (saved to .live_papertrade_state.json)
```

## Key Files

| File | Purpose |
|------|---------|
| `forward/engine.py` | `ForwardTestEngine` — main loop |
| `forward/paper.py` | CLI commands (`run_walkforward`, `run_live_papertrade`) |
| `forward/portfolio.py` | `StrategyAccount` — tracks positions |
| `forward/strategy_adapter.py` | Wraps `Strategy` for forward-test loop |
| `forward/broker.py` | Broker interface for live feeds |

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

State is saved to `.live_papertrade_state.json`:
```json
{
    "processed_bars": 150,
    "resume_count": 3,
    "last_date": "2024-06-15",
    "positions": [...],
    "equity_curve": [...]
}
```

Resumable on restart with `--resume-on-start` flag.

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
  --poll-seconds 60 \
  --resume-on-start
```

## Safety Rules

1. **No real orders** — all trades are simulated
2. **Auth guard** — `mode: "live"` requires an authenticated broker session (403
   otherwise); `mode: "synthetic"` is explicitly exempt so the loop is testable
   without credentials
3. **State isolation** — sessions are keyed by `state_id` in a bounded registry
   (`MAX_SESSIONS = 20`); stopping one never touches another
4. **No lookahead** — the strategy only ever sees the revealed prefix, and the
   payload's signals/candles are cut at the same bar
5. **Bounded clock** — `bars_per_second` is clamped to 0…5000 so a typo in a
   client body cannot spin a thread
