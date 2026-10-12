#!/usr/bin/env python3
"""Simple diagnostic to check mStock feed status.

Usage:
    python scripts/check_mstock_simple.py
"""

import sys
from pathlib import Path

# Add project root and src to path
project_root = Path(__file__).resolve().parents[1]
src_dir = project_root / "src"
sys.path.insert(0, str(src_dir))

# Load environment variables from .env file manually
from dotenv import load_dotenv
env_file = project_root / ".env"
if env_file.exists():
    load_dotenv(dotenv_path=env_file)
    print(f"[OK] Loaded .env from: {env_file}")
else:
    print("[WARNING] .env file not found")

import os
from datetime import datetime, timezone, timedelta


def main():
    print("\n" + "="*70)
    print("mStock Feed & Strategy Diagnostic")
    print("="*70)
    
    # 1. Check credentials
    print("\n[1] Credentials:")
    api_key = os.getenv("MSTOCK_API_KEY", "").strip()
    username = os.getenv("MSTOCK_USERNAME", "").strip()
    password = os.getenv("MSTOCK_PASSWORD", "").strip()
    auth_mode = os.getenv("MSTOCK_AUTH_MODE", "otp").strip()
    
    if api_key and username and password:
        print(f"  [OK] MSTOCK_API_KEY: {api_key[:8]}...")
        print(f"  [OK] MSTOCK_USERNAME: {username}")
        print(f"  [OK] MSTOCK_PASSWORD: ***")
        print(f"  [OK] MSTOCK_AUTH_MODE: {auth_mode}")
    else:
        print("  [FAIL] Missing credentials - check .env file")
        return
    
    # 2. Check session token
    print("\n[2] Session Token:")
    try:
        from backtest.live.auth import get_session_token
        token = get_session_token()
        if token:
            print(f"  [OK] Token obtained: {token[:20]}...")
        else:
            print("  [FAIL] No token returned")
            return
    except Exception as e:
        print(f"  [FAIL] Error: {e}")
        return
    
    # 3. Market hours
    print("\n[3] Market Status:")
    now_utc = datetime.now(timezone.utc)
    ist = now_utc + timedelta(hours=5, minutes=30)
    is_weekday = ist.weekday() < 5
    market_open_time = ist.replace(hour=9, minute=15, second=0, microsecond=0)
    market_close_time = ist.replace(hour=15, minute=30, second=0, microsecond=0)
    is_market_hours = market_open_time <= ist <= market_close_time
    
    print(f"  Time (IST): {ist.strftime('%Y-%m-%d %H:%M:%S')} ({ist.strftime('%A')})")
    print(f"  Market Hours: {'[OK] Yes' if is_market_hours else '[INFO] No'}")
    
    if not is_market_hours:
        if is_weekday:
            if ist < market_open_time:
                until_open = market_open_time - ist
                hours, remainder = divmod(until_open.seconds, 3600)
                minutes = remainder // 60
                print(f"  Market opens in: {hours}h {minutes}m")
            else:
                print("  Market already closed for today")
        else:
            print("  Weekend - market closed")
    
    # 4. Test mStock API call
    print("\n[4] mStock API Test:")
    try:
        from backtest.data.mstock_live_feed import MStockLiveFeed, INDEX_SECURITY_TOKENS
        
        feed = MStockLiveFeed()
        
        # Try to resolve a security token
        test_symbol = "NIFTY"
        if test_symbol in INDEX_SECURITY_TOKENS:
            token_val = INDEX_SECURITY_TOKENS[test_symbol]
            print(f"  [OK] Security token for {test_symbol}: {token_val}")
        else:
            print(f"  [WARNING] No security token mapped for {test_symbol}")
        
        # Check if we have any client
        if feed.client:
            print(f"  [OK] Client available: {type(feed.client).__name__}")
        else:
            print(f"  [INFO] No injected client - will use HTTP API directly")
        
    except Exception as e:
        print(f"  [FAIL] Error: {e}")
        import traceback
        traceback.print_exc()
    
    # 5. Check database for data
    print("\n[5] Database Check:")
    try:
        from backtest.data.db_source import DbSource
        
        db = DbSource()
        test_symbols = ["NIFTY", "BANKNIFTY"]
        
        for symbol in test_symbols:
            end_date = datetime.now()
            start_date = end_date - timedelta(days=7)
            
            try:
                df = db.get_candles(symbol, start=start_date, end=end_date)
                if df is not None and not df.empty:
                    print(f"  [OK] {symbol}: {len(df)} candles (latest: {df.index[-1]})")
                else:
                    print(f"  [WARNING] {symbol}: No data in database")
                    print(f"           Run: python scripts/fetch_nse_indices_historical.py --symbols {symbol}")
            except Exception as e:
                print(f"  [INFO] {symbol}: Query failed - {e}")
                
    except Exception as e:
        print(f"  [FAIL] Database error: {e}")
    
    # 6. Strategy eligibility
    print("\n[6] Strategy Eligibility:")
    try:
        # Import strategy classes directly
        from backtest.strategies.nifty_scalper import NiftyScalper
        from backtest.strategies.banknifty_straddle import BankNiftyStraddle
        from backtest.strategies.option_directional import DirectionalOptions
        
        strategies = [
            ("NiftyScalper", NiftyScalper),
            ("BankNiftyStraddle", BankNiftyStraddle),
            ("DirectionalOptions", DirectionalOptions),
        ]
        
        test_instruments = ["NIFTY", "BANKNIFTY", "NIFTYIT"]
        
        for name, strat_class in strategies:
            strat = strat_class()
            eligible = getattr(strat, 'eligible_instruments', [])
            
            print(f"\n  {name}:")
            print(f"    Eligible: {eligible if eligible else 'ALL (no restriction)'}")
            
            for inst in test_instruments:
                can_trade = not eligible or inst in eligible
                status = "[OK]" if can_trade else "[BLOCKED]"
                print(f"      {status} {inst}")
                
    except Exception as e:
        print(f"  [FAIL] Error loading strategies: {e}")
        import traceback
        traceback.print_exc()
    
    # Summary
    print("\n" + "="*70)
    print("DIAGNOSIS COMPLETE")
    print("="*70)
    print("\nIf strategies are not taking trades, common causes:")
    print("  1. Market is closed (no live data)")
    print("  2. No historical data loaded (strategy needs context)")
    print("  3. Strategy conditions not met (check entry logic)")
    print("  4. Instrument not in eligible_instruments list")
    print("  5. Risk management blocking (daily loss limit, max positions)")
    print("  6. Insufficient capital/margin")
    print("\nNext steps:")
    print("  - Check server logs for error messages")
    print("  - Verify strategy conditions are being evaluated")
    print("  - Ensure you have recent price data loaded")
    print("="*70 + "\n")


if __name__ == "__main__":
    main()
