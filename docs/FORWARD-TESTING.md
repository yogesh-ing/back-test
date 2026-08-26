# Forward Testing

## What It Is

Forward testing is **paper trading** — simulating a strategy in real-time without risking real money. The engine replays historical bars one at a time, revealing them gradually to mimic live trading.

## How It Differs from Backtesting

| Aspect | Backtest | Forward Test |
|--------|----------|-------------|
| **Execution** | Vectorized (all bars at once) | Bar-by-bar (replay) |
| **Speed** | Instant | Slow (simulates real-time) |
| **Purpose** | "How would this have performed?" | "How does this feel to trade?" |
| **State** | Stateless | Stateful (positions persist) |
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

### Start Forward Test
```bash
POST /api/forward/start
{
    "strategy": "sma_crossover",
    "symbol": "RELIANCE",
    "timeframe": "1D",
    "from_date": "2024-01-01",
    "to_date": "2024-12-31",
    "capital": 100000,
    "params": {"fast": 20, "slow": 50}
}
```
**Response:** `{status: "running"}` or 403 if not authenticated.

### Check Status
```bash
GET /api/forward/status
```
**Response:**
```json
{
    "status": "running",
    "progress": {"revealed": 150, "total": 250},
    "metrics": {"sharpe": 1.2, "total_return": 0.15, ...},
    "equity": [{"date": "2024-01-01", "value": 100000}, ...],
    "trades": [...],
    "positions": [...]
}
```

### Stop Forward Test
```bash
POST /api/forward/stop
```

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
2. **Auth guard** — forward test requires broker authentication (403 if not logged in)
3. **State isolation** — each forward test session is independent
4. **No lookahead** — bars revealed one at a time, strategy can only see past data
