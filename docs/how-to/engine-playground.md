# How to use Engine playground (forward replay)

**Page:** `/forward` · **Use it when…** you want to watch one strategy replay on a
live clock for engine diagnostics. It is a bench, **not a trading book** — no
portfolio bucket-risk controls, and the page says so.

Index: [USER-GUIDE.md](../USER-GUIDE.md) · detail:
[FORWARD-TESTING.md](../FORWARD-TESTING.md),
[OPTIONS-FORWARD-TESTING.md](../OPTIONS-FORWARD-TESTING.md)

## Starting a session

1. Strategy, Instrument, Timeframe (`1min` / `Day`), **Run mode**, **Data source**.
2. Capital, **Replay speed** (`0.25` slow → `500` instant-ish bars/s; the server
   default comes from `--replay-speed`), From / To.
3. `▶ Start`. The clock runs **on the server**, so bars keep being revealed with
   the tab closed; `■ Stop` ends the session.

## Watching it

- Status badge (Idle / Running / Stopped) and the `revealed / total · %` progress
  line.
- Live metric cards and the live equity curve with its buy & hold benchmark.
- **Positions** — entry vs current price, move %, unrealised P&L, bars held.
- **Trade feed** — `✅ / ❌ / ⏳ Open`, paginated.

## Banners you may see

| Banner | Meaning | What to do |
|--------|---------|------------|
| `Pre-filled from a backtest / compare result` | Arrived via `Open in playground` | Adjust replay speed and start |
| Resume banner | A session from a previous page load is still alive | `▶ Resume` to re-attach, or `✕ Start fresh` |

The page re-attaches to its own `state_id` after a refresh, and
`GET /api/forward/status` is a pure read — it never advances the clock.

## API

`POST /api/forward/start` · `GET /api/forward/status?state_id=` ·
`POST /api/forward/stop` · `GET /api/forward/sessions`.
