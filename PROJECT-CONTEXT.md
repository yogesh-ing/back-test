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
- ✅ 19 passed (all active acceptance tests)
- ⏳ 3 skipped (mStock auth — require credentials)

## Key Files & Current State

| File | Purpose | Status |
|------|---------|--------|
| backtester.py | Vectorized + risk-aware engine | ✅ Fixed (lagged signals, return calc before zeroing) |
| paper.py | Walk-forward runner | ✅ Fixed (pre-computed shifted signals) |
| broker.py | Per-bar fills reconciliation | ✅ Working |
| auth.py | TOTP (HMAC-SHA1) + OTP flows, session cache | ✅ Complete |
| mstock.py | API client + data normalization | ✅ Complete |
| preflight.py | DNS/HTTPS/auth checks | ✅ Complete |
| cli.py | All 5 commands wired | ✅ Complete |

## Known Limitations
- Live mode not implemented (stub with instructions)
- State persistence deferred
- Auth tests require mStock credentials (skipped)
- No portfolio multi-strategy allocation system yet

## Next Steps (If Continuing)
1. Implement live polling loop (poll mStock on schedule, call strategy signals, broker.step, save state)
2. Add state persistence across restarts
3. Test mStock auth with real credentials
4. Add position sizing / portfolio allocation

## Build Dependencies
Python 3.10+, pandas, numpy, requests, python-dotenv, matplotlib, pytest

## Key Constants
- PYTHONPATH=src (module imports)
- Default capital: 100k
- Default commission: 0.03%
- Default slippage: 0.05%
- Walk-forward equity tolerance: 1e-5 (reconciliation)
