#!/usr/bin/env python3
"""Fetch historical data for NSE indices and store in market_data_cache."""

import argparse
import sys
from pathlib import Path

# Add project root and src to path
project_root = Path(__file__).resolve().parents[1]
src_dir = project_root / "src"
sys.path.insert(0, str(src_dir))

# Load environment variables
from dotenv import load_dotenv
env_file = project_root / ".env"
if env_file.exists():
    load_dotenv(dotenv_path=env_file)

from backtest.data.mstock_live_feed import MStockLiveFeed, INDEX_SECURITY_TOKENS
from backtest.data.db_source import DbSource
from datetime import datetime, timedelta


def fetch_index_data(symbol: str, days: int = 365) -> bool:
    """Fetch historical data for a single index and save to database."""
    
    print(f"
{'='*60}")
    print(f"Fetching data for: {symbol}")
    print(f"{'='*60}")
    
    db = DbSource()
    feed = MStockLiveFeed()
    
    end_date = datetime.now()
    start_date = end_date - timedelta(days=days)
    
    print(f"Date range: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")
    
    try:
        candles = feed.get_candles(
            symbol=symbol,
            start=start_date.strftime('%Y-%m-%d'),
            end=end_date.strftime('%Y-%m-%d'),
            interval='1day'
        )
        
        if candles is None or candles.empty:
            print(f"[WARNING] No data returned for {symbol}")
            return False
        
        print(f"[OK] Fetched {len(candles)} candles")
        print(f"  First candle: {candles.index[0]}")
        print(f"  Last candle: {candles.index[-1]}")
        print(f"  Price range: {candles['close'].min():.2f} - {candles['close'].max():.2f}")
        
        saved_count = db.save_candles(symbol, candles)
        print(f"[OK] Saved {saved_count} candles to market_data_cache")
        
        return True
        
    except Exception as e:
        print(f"[FAIL] Error fetching {symbol}: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    parser = argparse.ArgumentParser(description="Fetch NSE indices historical data")
    parser.add_argument('--symbols', type=str, help='Comma-separated symbols', default=None)
    parser.add_argument('--days', type=int, help='Days of data (default: 365)', default=365)
    
    args = parser.parse_args()
    
    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(',')]
    else:
        symbols = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY",
                   "NIFTYIT", "NIFTYAUTO", "NIFTYPHARMA", "NIFTYMETAL"]
    
    print(f"
[DATA] NSE Indices Historical Data Fetcher")
    print(f"Symbols: {len(symbols)}, Days: {args.days}")
    
    successful = []
    failed = []
    
    for symbol in symbols:
        if fetch_index_data(symbol, args.days):
            successful.append(symbol)
        else:
            failed.append(symbol)
    
    print(f"
Successful: {len(successful)} - {successful}")
    if failed:
        print(f"Failed: {len(failed)} - {failed}")
    print("
Done! Restart server to see new data.")


if __name__ == "__main__":
    main()
