#!/usr/bin/env python3
import sys, os
from pathlib import Path

project_root = Path(__file__).resolve().parents[1]
src_dir = project_root / "src"
sys.path.insert(0, str(src_dir))

from dotenv import load_dotenv
load_dotenv(project_root / ".env")

from backtest.data.mstock_live_feed import MStockLiveFeed
from backtest.data.db_source import DbSource
from datetime import datetime, timedelta

def fetch(symbol, days=30):
    db = DbSource()
    feed = MStockLiveFeed()
    end_date = datetime.now()
    start_date = end_date - timedelta(days=days)
    
    print(f"Fetching {symbol} ({days} days)...")
    
    try:
        candles = feed.get_candles(symbol, start_date.strftime("%Y-%m-%d"), 
                                   end_date.strftime("%Y-%m-%d"), "1day")
        
        if candles is None or candles.empty:
            print(f"  No data for {symbol}")
            return False
        
        print(f"  Got {len(candles)} candles, saving...")
        db.save_candles(symbol, candles)
        print(f"  Saved!")
        return True
    except Exception as e:
        print(f"  Error: {e}")
        return False

if __name__ == "__main__":
    # Parse arguments: last arg can be days (number), rest are symbols
    args = sys.argv[1:]
    
    if not args:
        symbols = ["NIFTY", "BANKNIFTY"]
        days = 30
    elif args[-1].isdigit():
        symbols = args[:-1]
        days = int(args[-1])
    else:
        symbols = args
        days = 30
    
    print(f"Fetching {len(symbols)} symbols for {days} days\n")
    
    for sym in symbols:
        fetch(sym.upper(), days)
    
    print("\nDone! Restart server.")
