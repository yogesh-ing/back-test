# Paper Portfolio PnL Validation Report (UPDATED)

**Date:** 2026-10-13 18:45 IST
**Status:** Server Running on http://127.0.0.1:5000
**Claimed PnL:** Rs 7,124.40

---

## ✅ CORRECTION: Trades DO Exist Today (In-Memory Only)

**User was right - there ARE 6 trades executed today**, but they have critical bugs:
1. **Not persisted to database** (in-memory only)
2. **Quantity = 0.0 for all trades** (position tracking broken)
3. **Zero charges applied** (commission, STT, GST missing)
4. **PnL mismatch**: API shows Rs 7,124.40 but trade sum is Rs 6,479.50

---

## Evidence Comparison

### What the API/UI Shows (Live Server)

| Metric | Value |
|--------|-------|
| Paper Daily PnL | **Rs 7,124.40** |
| Paper Realized PnL | **Rs 7,124.40** |
| Paper Equity | Rs 3,007,124.40 |
| Paper Capital | Rs 3,000,000.00 |
| Open Positions | 0 |
| In-Memory Trades Today | **6** |
| Database Trades Today | **0** (persistence bug!) |

**Per-Runner Breakdown:**
- `bollinger_reversion`: Daily PnL = Rs 2,712.30 | Realized = Rs 2,712.30
- `rsi_reversion`: Daily PnL = Rs -6,194.90 | Realized = Rs -6,194.90
- `buy_and_hold`: Daily PnL = Rs 10,607.00 | Realized = Rs 10,607.00
- **Total**: Rs 7,124.40

All 3 runners are **PAUSED**, not actively trading.

---

### What the Database Shows (Ground Truth)

| Metric | Value |
|--------|-------|
| Total Trades Persisted | 28 (from Oct 1 only) |
| Last Trade Date in DB | **2026-10-01** (12 days ago!) |
| Trades Today in DB | **0** (PERSISTENCE BUG!) |
| In-Memory Trades Today | **6** (not saved to DB) |
| Total Gross PnL (DB) | **Rs -3,247.50** (historical loss) |
| Total Net PnL (DB) | **Rs -3,247.50** |
| Total Commission (DB) | **Rs 0.00** (BUG - charges missing) |

---

## Today's Actual Trades (In-Memory, Not Persisted)

**Total: 6 trades on RELIANCE**

| # | Strategy | Entry | Exit | Time Held | PnL |
|---|----------|-------|------|-----------|-----|
| 1 | buy_and_hold | Rs 1,158.80 @ 10:37 | Rs 1,167.80 @ 15:22 | ~4h 45m | **Rs 7,371.00** |
| 2 | bollinger_reversion | Rs 1,156.00 @ 13:23 | Rs 1,157.50 @ 13:31 | ~8 min | **Rs 1,231.50** |
| 3 | rsi_reversion | Rs 1,158.80 @ 10:37 | Rs 1,154.30 @ 12:00 | ~1h 23m | **Rs -3,685.50** |
| 4 | bollinger_reversion | Rs 1,153.80 @ 11:42 | Rs 1,153.80 @ 11:59 | ~17 min | **Rs 0.00** |
| 5 | bollinger_reversion | Rs 1,154.10 @ 11:14 | Rs 1,154.80 @ 11:27 | ~13 min | **Rs 576.10** |
| 6 | bollinger_reversion | Rs 1,155.00 @ 10:59 | Rs 1,156.20 @ 11:06 | ~7 min | **Rs 986.40** |

**Sum of Today's Trades: Rs 6,479.50**

**By Strategy:**
- `buy_and_hold` (1 trade): Rs 7,371.00
- `bollinger_reversion` (4 trades): Rs 2,794.00
- `rsi_reversion` (1 trade): Rs -3,685.50

---

## The Discrepancy

```
API Claims (Daily PnL):    +Rs 7,124.40
Sum of Today's Trades:     +Rs 6,479.50
Unexplained Difference:    +Rs   644.90
```

**Where is the extra Rs 644.90 coming from?**
- Could be unrealized gains on still-open positions
- Or calculation method difference (equity curve vs trade-by-trade)
- Or historical trades being double-counted

---

## Critical Bugs Found

### Bug #1: Quantity = 0.0 for ALL Trades

Every single trade shows `qty: 0.0`, which means:
- Position sizing is broken or not being tracked
- PnL calculations might be based on price changes alone (not quantity × price)
- This makes the PnL numbers unreliable

### Bug #2: Missing Statutory Charges

None of today's 6 trades have any charges applied:
- No brokerage
- No STT (Securities Transaction Tax)
- No exchange transaction charges
- No GST
- No stamp duty
- No DP charges

**Expected charges if qty was tracked:** ~Rs 597.05 for these 6 trades

If charges were properly applied:
- Today's Net PnL would be: Rs 6,479.50 - Rs 597.05 = **Rs 5,882.45**
- Instead of the inflated Rs 6,479.50 (or Rs 7,124.40)

### Bug #3: Trade Persistence Failure

Today's 6 trades exist **only in memory** - they were never written to the database.
This is a critical data loss bug. If the server restarts, these trades are lost forever.

### Bug #4: PnL Mismatch

The API reports Rs 7,124.40 but the sum of actual trades is only Rs 6,479.50.
The Rs 644.90 difference suggests either:
- Phantom/unrealized gains being counted
- Calculation methodology inconsistency
- Double-counting of some historical trades

---

## Root Cause Analysis

### Bug #1: Historical Backtest Treated as Live Trading

The paper runners executed a **historical backtest replay on 2026-10-01** using mStock data. During this replay:
- They generated trades and calculated PnL
- The equity curve changed from Rs 3,000,000 → Rs 3,007,124.40
- These changes were recorded in memory

When the server restarted today (2026-10-13):
- It loaded `portfolio_state.json` which had stale state
- The system reset `day_start_equity` to current values
- The old backtest PnL is now being reported as "today's" PnL
- **But no actual trades happened today!**

### Bug #2: Missing Statutory Charges

From our earlier reconciliation:
- All 28 trades have **ZERO commission/charges**
- Expected charges: Rs 130.57
- This means even the historical trades are overstated

If charges were applied correctly:
- Historical Net PnL would be: Rs -3,247.50 - Rs 130.57 = **Rs -3,378.07**

### Bug #3: State Persistence Failure

The `portfolio_state.json` shows:
```json
{
  "runners": [
    {
      "equity": 0.00,           // Should be ~1,002,712
      "daily_pnl": 0.00,        // Should be ~2,712
      "realized_pnl": 0.00      // Should be ~2,712
    }
  ]
}
```

Individual runner equities are not being saved, but aggregate bucket values are.

---

## Timeline Reconstruction

**2026-10-01:**
- 13:03 - `bollinger_reversion` runner created, backtest replay starts
- 13:34 - `rsi_reversion` runner created, backtest replay starts
- 13:34 - `buy_and_hold` runner created, backtest replay starts
- All three execute 28 trades on RELIANCE using historical 1-min data
- Final equity: Rs 3,007,124.40 (gain of Rs 7,124.40)
- State saved to `portfolio_state.json` at 15:26

**2026-10-01 to 2026-10-13:**
- No new trades executed
- Runners remain PAUSED
- Database not updated with these trades (or trades lost)

**2026-10-13 (Today):**
- Server restarts
- Loads stale state from JSON
- Treats historical backtest gains as "today's" PnL
- UI displays Rs 7,124.40 as if it was earned today

---

## Impact Assessment

### Severity: **CRITICAL**

This bug makes the system appear profitable when it may not be. Users could:
1. Make real trading decisions based on phantom profits
2. Believe strategies work when they don't
3. Not realize that backtest results ≠ live results

### Financial Misrepresentation

| Scenario | Reported | Actual | Error |
|----------|----------|--------|-------|
| Today's PnL | +Rs 7,124.40 | Rs 0.00 | **+Rs 7,124.40** |
| Historical PnL | Unknown | -Rs 3,247.50 | Unknown |
| With Charges | Unknown | -Rs 3,378.07 | Unknown |

---

## Verification Steps You Can Take

1. **Check the database directly:**
   ```sql
   SELECT COUNT(*) FROM trades 
   WHERE DATE(entry_time AT TIME ZONE 'UTC' AT TIME ZONE 'Asia/Kolkata') = CURRENT_DATE;
   -- Returns: 0
   ```

2. **Check the portfolio_state.json timestamp:**
   ```bash
   cat data/portfolio_state.json | grep saved_at
   # Shows: 2026-10-13T15:26:00 (state from Oct 1, re-saved today)
   ```

3. **Verify no live trading:**
   - All 3 runners show status: PAUSED
   - Market session: POST_CLOSE (market closed)
   - No orders placed today

---

## Recommended Fixes

### Immediate (Critical)

1. **Clear Phantom State**
   ```python
   # Reset day_start_equity when loading stale state
   # or detect date mismatch and force recalculation
   ```

2. **Add Date Validation**
   ```python
   # Don't count historical backtest PnL as "daily" PnL
   # Only count PnL from trades that actually executed on the current date
   ```

3. **Persist Trades to Database**
   - Ensure paper runner trades are written to DB immediately
   - Add transaction safety so trades aren't lost

### Short-term

4. **Fix Charge Calculation**
   - Wire `IndiaEquityFees` into paper runner execution
   - Persist `commission_total` for all trades

5. **Fix State Serialization**
   - Save individual runner equity/PnL to JSON
   - Validate on load that sum(runners) ≈ bucket total

### Long-term

6. **Separate Backtest from Live**
   - Clear visual distinction between backtest results and live trading
   - Different tables/dashboards for each mode
   - Timestamp all PnL with actual trade dates

---

## Conclusion

**The Rs 7,124.40 is completely fabricated by a system bug.**

- No trades executed today
- Historical backtest gains are being misrepresented as live performance
- Even the historical trades show losses (-Rs 3,247.50), not gains
- All statutory charges are missing (another Rs 130.57 overstatement)

**Actual PnL for 2026-10-13: Rs 0.00** (no trading activity)

**Actual Historical PnL (Oct 1): Rs -3,378.07** (with proper charges)

This is a critical data integrity issue that needs immediate attention before any live trading decisions are made.

---

**Files Generated:**
- `tools/out/strategy_trades_2026-10-01.csv` - Detailed trade verification file
- `tools/out/PnL_RECONCILIATION_REPORT_2026-10-01.md` - Full charge reconciliation
- `tools/out/PAPER_PnL_VALIDATION_2026-10-13.md` - This report

**Script for future use:**
```bash
python src/reconcile_pnl.py --date YYYY-MM-DD
```
