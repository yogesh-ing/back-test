# Why Your Strategy Isn't Taking Trades - Troubleshooting Guide

## Most Common Causes (in order of likelihood)

### 1. **No Historical Data** ⚠️ MOST COMMON
**Symptom:** Strategy runs but never enters any trades

**Cause:** Strategies need historical price data to calculate indicators (moving averages, RSI, etc.). Without data, they can't evaluate entry conditions.

**Check:**
```bash
python scripts/check_mstock_simple.py
```
Look for "[5] Database Check" section - if it says "No data in database", this is your problem.

**Fix:**
```bash
# Fetch data for specific indices
python scripts/fetch_nse_indices_historical.py --symbols NIFTY,BANKNIFTY --days 30

# Fetch data for all major indices
python scripts/fetch_nse_indices_historical.py
```

---

### 2. Market Is Closed
**Symptom:** Live feed returns no data

**Cause:** NSE market hours are 9:15 AM to 3:30 PM IST, Monday-Friday

**Check:**
```bash
python scripts/check_mstock_simple.py
```
Look for "[3] Market Status" section

**Note:** You can still backtest with historical data even when market is closed. For live/paper trading, market must be open.

---

### 3. Wrong Instrument Selected
**Symptom:** Strategy shows as eligible but won't trade on your selected symbol

**Cause:** Each strategy has an `eligible_instruments` list that restricts which symbols it can trade.

**Current Strategy Restrictions:**
- `NiftyScalper`: Only trades NIFTY, BANKNIFTY, DEMO, INFY
- `BankNiftyStraddle`: Only trades BANKNIFTY, NIFTY
- `DirectionalOptions`: Only trades NIFTY, BANKNIFTY

**Fix:** Either:
1. Select an eligible instrument for your strategy, OR
2. Edit the strategy file to add your desired instrument:
```python
# In src/backtest/strategies/nifty_scalper.py
class NiftyScalper(Strategy):
    eligible_instruments = ["NIFTY", "BANKNIFTY", "NIFTYIT"]  # Add NIFTYIT
```

---

### 4. Strategy Conditions Not Met
**Symptom:** Has data and right instrument, but still no trades

**Cause:** The market conditions don't satisfy the strategy's entry rules.

**Examples:**
- SMA crossover needs the fast MA to cross above/below slow MA
- RSI strategy needs RSI to be below 30 or above 70
- Bollinger Bands needs price to touch bands

**Check:**
1. Look at the strategy code to understand entry conditions
2. Check current price action vs indicator values
3. Add debug logging to see what indicators are calculating

**Add Debug Logging:**
```python
# In your strategy's signal hook (src/backtest/strategies/... or plugins/strategies/...)
def generate_signals(self, candles: pd.DataFrame) -> pd.Series:
    close = candles["close"]
    print(f"Last close: {close.iloc[-1]:.2f}")
    # ... your indicators ...
    print(f"SMA Fast: {fast.iloc[-1]:.2f}, SMA Slow: {slow.iloc[-1]:.2f}")
    print(f"RSI: {rsi.iloc[-1]:.2f}")

    # Then your existing logic...
```

Note: strategies on this platform implement `generate_signals(candles)` or
`entries(candles)` — there is no `next()` method, and a strategy must never
fetch its own data (the engine hands it the `candles` frame).

---

### 5. Risk Management Blocking
**Symptom:** Strategy wants to enter but gets blocked

**Cause:** Risk management rules prevent the trade:
- Daily loss limit reached
- Maximum number of positions reached
- Insufficient capital/margin
- Position size too large

**Check Server Logs:**
Look for messages like:
- "Daily loss limit reached"
- "Max positions exceeded"
- "Insufficient capital"

**Fix:**
1. Check your risk settings in the UI
2. Increase daily loss limit if appropriate
3. Reduce position size
4. Close existing losing positions

---

### 6. mStock API Issues
**Symptom:** Can't fetch live data

**Check:**
```bash
python scripts/check_mstock_simple.py
```

Look for:
- "[1] Credentials" - should all be [OK]
- "[2] Session Token" - should be [OK]
- "[4] mStock API Test" - should show security token

**Common Issues:**
- Invalid credentials in `.env` file
- TOTP authentication failed (wrong authenticator setup)
- mStock API is down (rare)

**Fix:**
1. Verify credentials in `.env`
2. Re-authenticate if using TOTP
3. Restart the server after fixing credentials

---

## Quick Diagnostic Checklist

Run this first:
```bash
python scripts/check_mstock_simple.py
```

Then check each item:

- [ ] Credentials loaded? (Section 1)
- [ ] Session token obtained? (Section 2)
- [ ] Market is open? (Section 3)
- [ ] Historical data exists? (Section 5) ← **MOST IMPORTANT**
- [ ] Instrument is eligible? (Section 6)

If all pass, then:
- [ ] Check strategy logic conditions
- [ ] Check risk management settings
- [ ] Check server logs for errors

---

## Your Current Situation (as of 2026-09-30)

Based on the diagnostic run:

✅ **Working:**
- mStock credentials loaded
- Session token valid
- Market is OPEN (9:49 AM IST)
- Strategy eligibility correct for NIFTY/BANKNIFTY

❌ **Problem:**
- **NO HISTORICAL DATA** for NIFTY or BANKNIFTY in database

**Immediate Fix:**
```bash
# This is currently running in background
python scripts/fetch_nse_indices_historical.py --symbols NIFTY,BANKNIFTY --days 30
```

After it completes:
1. Restart your backtesting server
2. Try invoking the strategy again
3. It should now take trades if conditions are met

---

## Understanding the Data Flow

```
User selects symbol → DATA tab checks coverage.py
                                    ↓
                    Checks database for bars
                                    ↓
                If no data → Shows "No data loaded"
                                    ↓
            User must fetch data first (via script or UI)
                                    ↓
                Data saved to market_data_cache table
                                    ↓
            Strategy can now access historical prices
                                    ↓
                Indicators calculate correctly
                                    ↓
                    Entry conditions evaluated
                                    ↓
                        Trade executed (if met)
```

**Key Point:** Everything depends on having data in the database first!

---

## Fetching Data - Complete Guide

### Method 1: Automated Script (Recommended)
```bash
# All major indices (1 year)
python scripts/fetch_nse_indices_historical.py

# Specific indices only
python scripts/fetch_nse_indices_historical.py --symbols NIFTY,BANKNIFTY,NIFTYIT

# More historical depth
python scripts/fetch_nse_indices_historical.py --symbols NIFTY --days 365
```

### Method 2: DATA Tab UI
1. Go to DATA tab in browser
2. Select symbol from dropdown
3. Click "Fetch Data" button
4. Wait for confirmation

### Method 3: Legacy Script (for equities)
```bash
# For NIFTY 500 stocks
python scripts/fetch_nifty500_historical.py --timeframe 1day
```

---

## Still Not Working?

If you've checked everything above and strategies still don't trade:

1. **Check Server Console Logs**
   - Look for error messages
   - Look for "blocked by risk management" messages
   - Look for calculation errors

2. **Verify Strategy is Actually Running**
   - Check if `generate_signals()` (or `entries()`) is being called
   - Add a simple `print("Strategy running")` to confirm

3. **Test with Simple Strategy**
   - Try `BuyAndHold` strategy - it should always buy
   - If even that doesn't work, it's definitely a data/API issue

4. **Check Portfolio State**
   - Are you already at max positions?
   - Is there sufficient capital?
   - Any existing P&L blocking new trades?

5. **Enable Verbose Logging**
```python
import logging
logging.basicConfig(level=logging.DEBUG)
```

---

## Contact Points for Help

If none of this helps, gather this info:
1. Output of `python scripts/check_mstock_simple.py`
2. Last 20 lines of server console
3. Which strategy you're trying to run
4. Which symbol/instrument selected
5. Screenshot of your backtest configuration

This will help diagnose quickly!
