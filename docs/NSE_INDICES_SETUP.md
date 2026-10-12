# NSE Indices Backtesting Setup

## Overview

Your backtesting system now supports **16 NSE indices** including major broad-market and sectoral indices. These indices are available for both historical backtesting and live/paper trading.

## Available Indices

### Broad Market Indices
| Symbol | Name | mStock Token | Lot Size |
|--------|------|--------------|----------|
| NIFTY | NIFTY 50 | 26000 | 65 |
| BANKNIFTY | NIFTY BANK | 26009 | 30 |
| FINNIFTY | NIFTY NEXT 50 | 26037 | 60 |
| MIDCPNIFTY | NIFTY MIDCAP 150 | 26051 | 120 |
| SENSEX | S&P BSE SENSEX | 51 | 20 |
| INDIAVIX | INDIA VIX | 26017 | - |

### Sectoral Indices
| Symbol | Name | mStock Token | Lot Size |
|--------|------|--------------|----------|
| NIFTYIT | NIFTY IT | 26008 | 60 |
| NIFTYAUTO | NIFTY AUTO | 26029 | 50 |
| NIFTYPHARMA | NIFTY PHARMA | 26023 | 60 |
| NIFTYMETAL | NIFTY METAL | 26030 | 50 |
| NIFTYREALTY | NIFTY REALTY | 26018 | 50 |
| NIFTYFMCG | NIFTY FMCG | 26021 | 40 |
| NIFTYENERGY | NIFTY ENERGY | 26020 | 50 |
| NIFTYMEDIA | NIFTY MEDIA | 26031 | 50 |
| NIFTYPSUBANK | NIFTY PSU BANK | 26025 | 60 |
| NIFTYINFRA | NIFTY INFRA | 26019 | 50 |

## Configuration Changes Made

The following files were updated to add indices support:

1. **`src/backtest/data/coverage.py`** - Added all 16 indices to `INDEX_UNIVERSE`
2. **`src/backtest/data/mstock_live_feed.py`** - Added security tokens and quote symbol mappings
3. **`src/backtest/api/symbols.py`** - Added lot size fallbacks for all sectoral indices

## How to Fetch Historical Data

### Option 1: Use the Automated Script (Recommended)

```bash
# Fetch data for all major indices (1 year of data)
python scripts/fetch_nse_indices_historical.py

# Fetch specific indices only
python scripts/fetch_nse_indices_historical.py --symbols NIFTY,BANKNIFTY,NIFTYIT

# Fetch more historical data (e.g., 2 years)
python scripts/fetch_nse_indices_historical.py --days 730

# Fetch only sectoral indices
python scripts/fetch_nse_indices_historical.py --symbols NIFTYAUTO,NIFTYPHARMA,NIFTYMETAL,NIFTYIT
```

### Option 2: Use the DATA Tab UI

After running the fetch script above, you can also fetch additional data through the UI:

1. Go to **DATA tab** in the backtesting interface
2. Select any index symbol from the dropdown (they should now appear)
3. Click "Fetch Data" to get the latest bars
4. The system will use mStock API to pull historical data

## Verification Steps

After fetching data, verify it's available:

1. **Restart the backtesting server** (if running)
2. Go to **DATA tab**
3. You should see all 16 indices in the symbol picker
4. Select an index → you should see coverage information showing available date range
5. Try creating a simple backtest with that index

## Using Indices in Strategies

### Strategy Eligibility

Some strategies have hardcoded eligible instruments. To use new indices with existing strategies, update their `eligible_instruments`:

**Example - Making a strategy work with all indices:**

```python
# In your strategy file (e.g., src/backtest/strategies/nifty_scalper.py)
class NiftyScalper(Strategy):
    # Old version - limited to 2 indices
    # eligible_instruments = ["NIFTY", "BANKNIFTY"]
    
    # New version - works with all indices
    eligible_instruments = [
        "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY",
        "NIFTYIT", "NIFTYAUTO", "NIFTYPHARMA", "NIFTYMETAL",
        "NIFTYREALTY", "NIFTYFMCG", "NIFTYENERGY", "NIFTYMEDIA",
        "NIFTYPSUBANK", "NIFTYINFRA"
    ]
```

### Creating Index-Specific Strategies

You can create strategies that trade specific sectors:

```python
class TechSectorMomentum(Strategy):
    """Trade only IT sector index"""
    eligible_instruments = ["NIFTYIT"]
    
class BankingPair(Strategy):
    """Trade banking sector pairs"""
    eligible_instruments = ["BANKNIFTY", "NIFTYPSUBANK"]
```

## Important Notes

### API Endpoints
- **Historical candles:** `instruments/historical/{segment}/{token}/{interval}`
  — T+1 (today's session is not in the history until it settles); the `to`
  date is **end-exclusive**, so a one-day fetch must request `to + 1 day`.
- **Live quotes:** `instruments/quote/ohlc` is **index-only**; equities have no
  quote/ohlc symbol entry.
- **Equity intraday (today):** `instruments/intraday/{segment_id}/{instrument_token}/minute`
  — keyed by scriptmaster instrument token, not tradingsymbol (NSE segment
  id 1). Indices never appear in scriptmaster, so they use the fixed token map
  below.

### Data Availability
- **Historical data**: Must be fetched first using the script or DATA tab.
  Current 1-min backfill status: NIFTY complete; BANKNIFTY / FINNIFTY /
  MIDCPNIFTY paused; SENSEX / INDIAVIX deferred (see `docs/DATABASE.md`).
- **Live data**: Requires valid mStock API credentials
- **Update frequency**: Daily bars are T+1 (next day), live quotes are real-time

### Token Accuracy
The mStock security tokens come from the fixed, value-verified map
`INDEX_SECURITY_TOKENS` in `src/backtest/data/mstock_live_feed.py` — indexes
never appear in scriptmaster, so this map is the only token source for them.
If you encounter errors fetching data for a specific index:

1. Check the mStock documentation for the correct token
2. Update `INDEX_SECURITY_TOKENS` in `mstock_live_feed.py`
3. Restart the server

### Lot Sizes
Lot sizes are approximate and based on NSE standards as of 2026. For accurate lot sizes:
- The system will try to fetch from mStock scriptmaster first
- Falls back to the values in `_FALLBACK_LOT_SIZES` if offline

## Troubleshooting

### "No symbols available in DATA tab"
- Ensure you've restarted the server after making configuration changes
- Check that mStock API credentials are configured

### "No data for this symbol"
- Run the fetch script: `python scripts/fetch_nse_indices_historical.py`
- Or use the DATA tab to fetch data for that specific symbol

### "Invalid security token" error
- The token might be incorrect in `mstock_live_feed.py`
- Verify the token against mStock documentation
- Try fetching data for NIFTY first (token 26000 is well-established)

### Strategy doesn't show my index
- Check the strategy's `eligible_instruments` list
- Add your desired index to that list

## Next Steps

1. ✅ **Configuration complete** - All files updated
2. ⏳ **Fetch historical data** - Run the fetch script
3. ⏳ **Verify in UI** - Check DATA tab shows all indices
4. ⏳ **Create test backtest** - Run a simple strategy on NIFTY or BANKNIFTY
5. ⏳ **(Optional) Update strategies** - Add new indices to strategy eligibility lists

## Sources

- [NSE India - Live Market Indices](https://www.nseindia.com/market-data/live-market-indices)
- [NSE Sectoral Indices](https://www.nseindia.com/static/products-services/indices-sectoral)
- [NiftyIndices.com](https://www.niftyindices.com/)
- [List of All NSE Nifty Indices](https://dhan.co/all-nse-indices/)
