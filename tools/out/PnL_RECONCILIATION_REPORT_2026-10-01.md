# PnL Reconciliation Report - 2026-10-01

**Generated:** 2026-10-13 15:40 IST
**Analysis Date:** Most recent trading day (2026-10-01)
**Note:** No trades found for today (2026-10-13)

---

## Executive Summary

### Critical Finding: **ALL CHARGES/STT ARE MISSING**

- **Total Trades Analyzed:** 28
- **Trades Without Charges:** 28/28 (100%)
- **Expected Total Charges:** Rs 130.57
- **Recorded Charges:** Rs 0.00
- **Missing Charges:** Rs 130.57

This is a **critical anomaly** - all statutory charges (brokerage, STT, exchange fees, GST, DP charges) are completely absent from the trade records.

---

## PnL Breakdown by Strategy

| Strategy | Count | Gross PnL | Net PnL | Expected Charges | Status |
|----------|-------|-----------|---------|------------------|--------|
| buy_and_hold | 3 | Rs 9,708.00 | Rs 9,708.00 | Rs 99.87 | **CHARGES MISSING** |
| bollinger_reversion | 15 | Rs -408.50 | Rs -408.50 | Rs 18.45 | **CHARGES MISSING** |
| rsi_reversion | 10 | Rs -12,547.00 | Rs -12,547.00 | Rs 12.25 | **CHARGES MISSING** |
| **TOTAL** | **28** | **Rs -3,247.50** | **Rs -3,247.50** | **Rs 130.57** | **ALL MISSING** |

---

## Charge Component Analysis

For a typical intraday trade (RELIANCE, qty 1, entry Rs 1173.10, exit Rs 1170.30):

**Expected Charges per Intraday Trade:** Rs 1.23
- Brokerage: Rs 0.70
- STT: Rs 0.29
- Exchange: Rs 0.06
- GST: Rs 0.14
- DP Charges: Rs 0.00

For a typical delivery trade (RELIANCE, qty 1, entry Rs 1173.10, exit Rs 1177.10):

**Expected Charges per Delivery Trade:** Rs 33.29
- Brokerage: Rs 0.00 (zero brokerage broker)
- STT: Rs 2.35 (0.1% on both sides)
- Exchange: Rs 15.40
- GST: Rs 0.02
- DP Charges: Rs 15.34 (delivery charge)

---

## Impact Assessment

### Current State
The system currently shows:
- `gross_pnl` = `net_pnl` for ALL trades
- `commission_total` = 0.00 for ALL trades
- `slippage_total` = 0.00 for ALL trades

This means the PnL figures are **inflated** and do not reflect real trading costs.

### Real-World Impact
If these were live trades, the actual net PnL would be:
- **Reported Net PnL:** Rs -3,247.50
- **Actual Net PnL (with charges):** Rs -3,378.07
- **Difference:** Rs -130.57 (4.0% worse than reported)

---

## Root Cause Analysis

The trades table has columns for:
- `commission_total` - should contain all charges
- `slippage_total` - should contain execution slippage

Both are populated with 0.00, indicating that either:
1. The fee calculation engine is not being called during trade execution, OR
2. The calculated fees are not being persisted to the database, OR
3. These are backtest/paper trades where fees were intentionally disabled

### Likely Scenarios

**Scenario A: Backtest Data**
These appear to be backtest results where:
- Fee calculation was disabled (ZeroCommission model)
- Slippage was set to zero
- This is acceptable for backtesting but NOT for live trading

**Scenario B: Paper Trading Bug**
If this is paper/live trading data, there's a critical bug where:
- The `IndiaEquityFees` engine is not being invoked
- Or the calculated fees are not being saved to the database

---

## Verification File

A detailed CSV file has been generated at:
`tools/out/strategy_trades_2026-10-01.csv`

This file contains:
- All 28 trades with strategy names
- Entry/exit prices and times (IST)
- Quantities and holding periods
- Expected vs recorded charges
- Trade type classification (Intraday/Delivery)

You can use this to verify if trades executed according to strategy criteria.

---

## Recommendations

### Immediate Actions

1. **Determine Trading Mode**
   - If these are backtest trades: Document that fees are excluded by design
   - If these are paper trades: Fix the fee calculation immediately
   - If these are live trades: **CRITICAL BUG - fix immediately**

2. **Check Fee Engine Integration**
   ```python
   # Verify fee calculation is wired in forward/engine.py or paper_runner.py
   # Look for CommissionCalculator usage
   ```

3. **Enable Charges in Live/Paper Mode**
   - Ensure `IndiaEquityFees` is configured
   - Verify `CommissionCalculator.calculate()` is called on each fill
   - Persist results to `commission_total` column

### Long-term Improvements

1. **Add Validation Guard**
   - Add a check that prevents persisting trades with zero charges in live mode
   - Log warnings when `commission_total == 0` for non-backtest trades

2. **Dashboard Alert**
   - Show "Charges Not Applied" warning in portfolio UI when fees are missing
   - Display expected vs actual charges prominently

3. **Reconciliation Automation**
   - Run this reconciliation script daily
   - Alert when charge discrepancies exceed tolerance

---

## Detailed Trade Analysis

### Strategy Performance (with expected charges applied)

#### 1. buy_and_hold (3 trades)
- **Current Reported:** +Rs 9,708.00
- **With Charges:** +Rs 9,608.13
- **Impact:** -Rs 99.87 (-1.0%)
- **Status:** Still profitable, but less so

#### 2. bollinger_reversion (15 trades)
- **Current Reported:** -Rs 408.50
- **With Charges:** -Rs 426.95
- **Impact:** -Rs 18.45 (+4.5% worse)
- **Status:** Losses amplified by charges

#### 3. rsi_reversion (10 trades)
- **Current Reported:** -Rs 12,547.00
- **With Charges:** -Rs 12,559.25
- **Impact:** -Rs 12.25 (+0.1% worse)
- **Status:** Large losses, marginally worse with charges

---

## Anomaly Detection Results

✅ **PnL Formula Consistency:** PASS
- All trades satisfy: `gross_pnl - commission - slippage = net_pnl`
- Since commission and slippage are both 0, gross equals net

❌ **Charge Application:** FAIL
- 100% of trades missing expected statutory charges
- Total missing: Rs 130.57

⚠️ **Data Quality:** SUSPECT
- Multiple duplicate trades (same symbol, price, time)
- Suggests backtest replay or data duplication issue

---

## Next Steps

1. **Review the CSV file** (`tools/out/strategy_trades_2026-10-01.csv`) to verify strategy execution logic
2. **Confirm trading mode** (backtest vs paper vs live)
3. **Fix fee integration** if this is paper/live trading
4. **Re-run reconciliation** after fixes to verify charges are applied correctly

---

**Script Location:** `src/reconcile_pnl.py`
**Usage:** `python src/reconcile_pnl.py --date YYYY-MM-DD`
