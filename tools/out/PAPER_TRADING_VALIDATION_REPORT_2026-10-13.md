# Paper Trading Validation Report — 2026-10-13

## Executive Summary

A comprehensive validation harness was created to test all 4 critical bugs discovered on 2026-10-13 in a single automated run. This eliminates the need for day-by-day bug hunting.

**Harness location:** `src/backtest/validate_paper_trading.py`  
**Usage:** `PYTHONPATH=src python -m backtest.validate_paper_trading --verbose`

## Test Results

| # | Check | Status | Details |
|---|-------|--------|---------|
| 1 | Fill Quantities Non-Zero | **PASS** ✓ | All 2 fills have valid quantities > 0 |
| 2 | Charges Calculated | **PASS** ✓ | Total charges = Rs 37.69 (STT + brokerage + GST) |
| 3 | Trade Persistence Tracking | ⚠ WARN | `_closed_trades` infrastructure exists but not populated by legacy engine_loop |
| 4 | Daily PnL Calculation | **FAIL** ✗ | Reported Rs -37.69 vs expected Rs -82.33 (diff = Rs 44.64 = unrealized trade PnL) |
| 5 | Position Lifecycle | **PASS** ✓ | 1 round-trip completed successfully |
| 6 | Equity Curve Recording | **FAIL** ✗ | No equity curve recorded by old PaperRunner |

### What Passed (Critical Bugs Fixed)

#### Bug #1: Zero Quantities — FIXED ✓
- **Before:** All API trades showed qty=0.0
- **After:** All fills have valid quantities
- **Fix applied:** Quantity validation in fill execution path

#### Bug #2: Missing Charges — FIXED ✓
- **Before:** No STT, brokerage, or GST calculated
- **After:** Rs 37.69 total charges correctly calculated
  - Entry order: Rs 11.95
  - Exit order: Rs 25.74
- **Fix applied:** CommissionCalculator wired to Zerodha fee structure

### What Needs Attention

#### Issue #3: Trade Tracking Gap
- **Problem:** `_closed_trades` list exists (infrastructure added) but remains empty
- **Root cause:** Old `PaperRunner` delegates to `run_engine_loop()` which doesn't populate it
- **Impact:** Cannot calculate realized PnL per-trade
- **Fix needed:** Wire trade closure callbacks in engine_loop or migrate to StrategyRunner architecture

#### Issue #4: PnL Mismatch
- **Problem:** Daily PnL shows only fees (-Rs 37.69), missing realized trade PnL (+Rs 44.64)
- **Root cause:** Same as #3 — `_closed_trades` empty, so realized_today = 0
- **Current formula:** `daily_pnl = 0 (realized) + 0 (unrealized) - 37.69 (fees) = -37.69`
- **Expected formula:** `daily_pnl = 44.64 (realized) + 0 (unrealized) - 37.69 (fees) = +6.95`
- **Fix needed:** Populate `_closed_trades` first

#### Issue #6: No Equity Curve
- **Problem:** `equity_curve` list remains empty after run
- **Root cause:** Old `PaperRunner` doesn't record per-bar equity snapshots
- **Impact:** Cannot track intraday equity path or calculate max drawdown
- **Fix needed:** Add equity recording to engine_loop or use StrategyRunner

## Architecture Analysis

The codebase has **two parallel runner architectures**:

### Old Architecture (Legacy)
- **Class:** `PaperRunner` (line 3603)
- **Driver:** `run_engine_loop()` from `simulator/engine_loop.py`
- **Used by:** Tests, simple paper runs
- **Status:** Has bug fix infrastructure but engine_loop doesn't populate it

### New Architecture (Modern)
- **Class:** `StrategyRunner` (line 980)
- **Driver:** PortfolioManager with tick-driven loop
- **Used by:** Live paper trading via web UI
- **Status:** Fully implements all bug fixes (daily_pnl, _closed_trades, equity_curve)

**Recommendation:** Either:
1. **Migrate old PaperRunner** to call StrategyRunner internally, OR
2. **Update engine_loop** to populate trade tracking and equity curve, OR  
3. **Deprecate old PaperRunner** and update all callers to use StrategyRunner

## Validation Harness Benefits

Running this harness catches all bugs in **one shot** instead of waiting days:

- **Synthetic data:** Generates predictable 1-min bars (375 bars = full trading day)
- **Deterministic strategy:** Buy at bar 50, exit at bar 200
- **Realistic fees:** Zerodha broker model with actual STT/brokerage/GST rates
- **Comprehensive checks:** Validates quantities, charges, persistence, PnL, positions, equity
- **Fast execution:** Completes in <5 seconds vs waiting for market hours

## Next Steps

1. **Immediate:** Deploy current fixes to main branch (already done — commit 898ca9b)
2. **Short-term:** Choose architecture path (migrate/deprecate/update) and fix remaining gaps
3. **Long-term:** Run validation harness before every deployment to catch regressions

## How to Use

```bash
# Quick validation (exit code 0 = pass, 1 = fail)
PYTHONPATH=src python -m backtest.validate_paper_trading

# Verbose output with details
PYTHONPATH=src python -m backtest.validate_paper_trading --verbose

# Enable synthetic source for testing
BACKTEST_DATA_PROFILE=testing PYTHONPATH=src python -m backtest.validate_paper_trading
```

## Files Modified

- `src/backtest/forward/paper_runner.py` — Added bug fixes to old PaperRunner class
- `src/backtest/validate_paper_trading.py` — NEW: Comprehensive validation harness
- `tests/forward/test_paper_trading_validation.py` — NEW: pytest version of validation

---

**Report generated:** 2026-10-13 21:00 IST  
**Validation harness version:** 1.0  
**Commits validated:** 898ca9b (main branch)
