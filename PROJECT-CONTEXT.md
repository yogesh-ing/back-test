# Backtester Project Context

## Quick State
Build offline backtesting engine + live mStock connectivity. 19/22 acceptance tests pass. Cards 0-6 complete. Card 07 Phase 1 done (CLI wired). Live polling + state persistence deferred.

## Architecture
```
src/backtest/
├── data/       # Data sources (synthetic, CSV, mStock API)
├── strategy/   # Strategy base + registry (4 strategies included)
├── engine/     # Backtester (vectorized + risk-aware paths), metrics, plotting
├── forward/    # Walk-forward + paper trading runner
├── live/       # mStock auth (TOTP/OTP), API client, preflight checks
└── cli.py      # Commands: list, run, compare, preflight, papertrade
```

## Invariants (Must Hold)
1. **No-lookahead**: Position @ bar t = signal @ bar t-1. Use `target.shift(1)`
2. **Signal clipping**: target ∈ [-1, 1] (long/flat/short)
3. **Per-bar consistency**: risk-aware path mirrors vectorized math
4. **Stop/target exits**: exit forced intrabar, position zeroed, cost added, re-entry blocked
5. **Walkforward reconciliation**: equity matches vectorized backtest (tol 1e-5)
6. **Trade accounting**: one source of truth — `engine/trades.py` feeds both `compute_metrics`
   and `BacktestAdapter`. Trade P&L is equity-based (costs included), `num_trades` counts
   round trips (an open position counts, marked to final close) and `win_rate` is over
   **closed** trades only. Never re-derive either number from the position sign.
7. **Everything is observable**: `backtest.logging_config.configure_logging()` is installed by
   every entry point; `--log-level DEBUG` must explain any empty/flat result (`docs/LOGGING.md`).

## Data Contract
Canonical OHLCV frame: lowercase cols (open, high, low, close, volume), tz-naive DatetimeIndex ascending.

## CLI Commands
```
backtest list                           # List 4 strategies
backtest run --strategy X --from D1 --to D2   # Single backtest
backtest compare --strategies X,Y,Z --from D1 --to D2  # Multi-strategy
backtest preflight                      # DNS/HTTPS/auth checks
backtest papertrade --mode walkforward --strategies X --from D1 --to D2  # Paper trade
```

## Test Status
- ✅ 1582 passed, 4 skipped (mStock credentials) — `PYTHONPATH=src pytest tests/ -q`
  (as of 2026-08-31, post-F-01; the count drifts with each refactor ticket)
- ✅ 36 JS behaviour assertions across 4 Node harnesses (`tests/js/*.mjs`)
- ⚠ Sandbox note: rebuild the venv each session —
  `python3 -m venv /home/user/.venv && /home/user/.venv/bin/pip install -q -r requirements.txt pytest-cov flake8`

## Key Files & Current State

| File | Purpose | Status |
|------|---------|--------|
| engine/backtester.py | Vectorized quick-screen engine | ✅ Stable (lagged signals) — canonical path is `backtest_driver` |
| engine/backtest_driver.py | Backtest on the shared engine loop | ✅ New (P2.1) — same loop as `PaperRunner` |
| engine/trades.py | Trade walk + stats (cards and table share it) | ✅ New (G1/G2) — equity-based, open trade excluded from win_rate |
| simulator/engine_loop.py | Canonical bar-clock loop (submit → fill at next bar's open) | ✅ New (P2.1) |
| simulator/execution.py | `OrderExecutor` — `submit()`/`step()` bar clock + `execute()` | ✅ New (P1.3) |
| forward/paper_runner.py | Walk-forward / live paper CLI + `PaperRunner` (canonical loop) + command-center `StrategyRunner`/`OrderLedger`/`PaperBroker` | ✅ Re-architected (P1.4) |
| forward/strategy_adapter.py | Strategy → Signal → Order, **no fills** | ✅ Signal-only (F-01) |
| logging_config.py | Handlers, levels, request ids | ✅ New (U1) — see docs/LOGGING.md |
| auth.py | TOTP (HMAC-SHA1) + OTP flows, session cache | ✅ Complete |
| mstock.py | API client + data normalization | ✅ Complete |
| preflight.py | DNS/HTTPS/auth checks | ✅ Complete |
| cli.py | All 5 commands wired | ✅ Complete |

## Known Limitations
- Timeframe is cosmetic on synthetic/CSV sources (daily bars only) — see gap G6 / U2
- Command-center (portfolio) state is in-memory only (V1; "persistence V2")
- **Live broker fills still open** (findings F-12): bucket UI + mode/source tags done
  (P4.1), but `BrokerFillProvider` + `MStockLiveFeed` wiring, `poll_fill` in the broker
  ABC, and bucket-level risk anchors remain
- Auth tests require mStock credentials (skipped)

## Next Steps (If Continuing)
1. Wire `mode='live'` command-center runners through `BrokerFillProvider` + `MStockLiveFeed` (F-12)
2. Add portfolio/runner state persistence across restarts (V2)
3. Test mStock auth with real credentials
4. Finish the docs pass for `instructions/ARCHITECTURE-BLUEPRINT.md` (see its top banner)

## Build Dependencies
Python 3.10+, pandas, numpy, requests, python-dotenv, matplotlib, pytest

## Key Constants
- PYTHONPATH=src (module imports)
- Default capital: 100k
- Default commission: 0.03%
- Default slippage: 0.05%
- Walk-forward equity tolerance: 1e-5 (reconciliation)
