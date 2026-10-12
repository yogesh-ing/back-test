#!/usr/bin/env python3
"""Diagnostic tool to check mStock live feed status and debug trading issues.

Usage:
    python scripts/check_mstock_feed.py [--symbol SYMBOL] [--test-trade]

This script checks:
1. mStock API credentials are loaded
2. Session token is valid
3. Can fetch live quotes for indices
4. Can fetch historical data
5. Market hours status
6. Strategy eligibility (if --test-trade is used)
"""

import argparse
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta

# Add project root to path
project_root = Path(__file__).resolve().parents[1]
src_dir = project_root / "src"
sys.path.insert(0, str(src_dir))

# Load environment variables from .env file manually
from dotenv import load_dotenv
env_file = project_root / ".env"
if env_file.exists():
    load_dotenv(dotenv_path=env_file)
    print(f"Loaded .env from: {env_file}")
else:
    print("WARNING: .env file not found")

import os
import requests


def check_credentials():
    """Check if mStock credentials are configured."""
    print("\n" + "="*60)
    print("1. CREDENTIALS CHECK")
    print("="*60)
    
    api_key = os.getenv("MSTOCK_API_KEY", "").strip()
    username = os.getenv("MSTOCK_USERNAME", "").strip()
    password = os.getenv("MSTOCK_PASSWORD", "").strip()
    auth_mode = os.getenv("MSTOCK_AUTH_MODE", "otp").strip()
    
    checks = [
        ("MSTOCK_API_KEY", bool(api_key), f"{'OK' if api_key else 'FAIL'} Present: {api_key[:8]}..." if api_key else "FAIL Missing"),
        ("MSTOCK_USERNAME", bool(username), f"OK Present: {username}" if username else "FAIL Missing"),
        ("MSTOCK_PASSWORD", bool(password), "OK Present: ***" if password else "FAIL Missing"),
        ("MSTOCK_AUTH_MODE", True, f"OK Mode: {auth_mode}"),
    ]
    
    all_ok = True
    for name, present, msg in checks:
        status = "OK" if present else "FAIL"
        print(f"  [{status}] {name}: {msg}")
        if not present and name != "MSTOCK_AUTH_MODE":
            all_ok = False
    
    if not all_ok:
        print("\nWARNING: Some credentials are missing!")
        print("   Check your .env file or environment variables.")
        return False
    
    print("\nAll credentials present")
    return True


def check_session_token():
    """Check if we can get a valid session token."""
    print("\n" + "="*60)
    print("2. SESSION TOKEN CHECK")
    print("="*60)
    
    try:
        from backtest.live.auth import get_session_token
        
        print("  Attempting to get session token...")
        token = get_session_token()
        
        if token:
            print(f"  OK Session token obtained: {token[:20]}...")
            return True
        else:
            print("  FAIL Failed to get session token")
            print("  Possible causes:")
            print("    - Invalid credentials")
            print("    - OTP/TOTP authentication failed")
            print("    - mStock API is down")
            return False
            
    except Exception as e:
        print(f"  FAIL Error getting session token: {e}")
        print(f"  Details: {type(e).__name__}: {str(e)}")
        return False


def check_market_hours():
    """Check if market is currently open."""
    print("\n" + "="*60)
    print("3. MARKET HOURS CHECK")
    print("="*60)
    
    now_utc = datetime.now(timezone.utc)
    ist = now_utc + timedelta(hours=5, minutes=30)
    
    # NSE market hours: 9:15 AM to 3:30 PM IST, Mon-Fri
    is_weekday = ist.weekday() < 5
    market_open_time = ist.replace(hour=9, minute=15, second=0, microsecond=0)
    market_close_time = ist.replace(hour=15, minute=30, second=0, microsecond=0)
    is_market_hours = market_open_time <= ist <= market_close_time
    
    print(f"  Current time (UTC): {now_utc.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Current time (IST): {ist.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Day of week: {ist.strftime('%A')}")
    print(f"  Is weekday: {'OK Yes' if is_weekday else 'FAIL No (weekend)'}")
    print(f"  Is market hours: {'OK Yes' if is_market_hours else 'FAIL No'}")
    
    if is_market_hours:
        remaining = market_close_time - ist
        hours, remainder = divmod(remaining.seconds, 3600)
        minutes = remainder // 60
        print(f"  Market closes in: {hours}h {minutes}m")
    elif is_weekday:
        if ist < market_open_time:
            until_open = market_open_time - ist
            hours, remainder = divmod(until_open.seconds, 3600)
            minutes = remainder // 60
            print(f"  Market opens in: {hours}h {minutes}m")
        else:
            print(f"  Market already closed for today")
    
    return is_weekday and is_market_hours


def check_live_quote(symbol: str = "NIFTY"):
    """Check if we can fetch live quotes."""
    print("\n" + "="*60)
    print(f"4. LIVE QUOTE CHECK ({symbol})")
    print("="*60)
    
    try:
        from backtest.data.mstock_live_feed import MStockLiveFeed, QUOTE_SYMBOL_NAMES
        
        feed = MStockLiveFeed()
        
        if not feed.is_available():
            print("  FAIL mStock feed is not available")
            print("  Check credentials and session token above")
            return False
        
        quote_symbol = QUOTE_SYMBOL_NAMES.get(symbol, symbol)
        print(f"  Fetching live quote for: {quote_symbol}")
        
        # Try to get latest bar
        bar = feed.client.get_latest_bar(quote_symbol) if feed.client else None
        
        if bar:
            print(f"  OK Live quote received:")
            print(f"    Timestamp: {bar.get('ts', 'N/A')}")
            print(f"    Open: {bar.get('open', 'N/A')}")
            print(f"    High: {bar.get('high', 'N/A')}")
            print(f"    Low: {bar.get('low', 'N/A')}")
            print(f"    Close: {bar.get('close', 'N/A')}")
            print(f"    Volume: {bar.get('volume', 'N/A')}")
            return True
        else:
            print("  WARNING  No live quote returned")
            print("  This might be OK if market is closed")
            return False
            
    except Exception as e:
        print(f"  FAIL Error fetching live quote: {e}")
        import traceback
        traceback.print_exc()
        return False


def check_historical_data(symbol: str = "NIFTY", days: int = 7):
    """Check if we can fetch historical data."""
    print("\n" + "="*60)
    print(f"5. HISTORICAL DATA CHECK ({symbol}, last {days} days)")
    print("="*60)
    
    try:
        from backtest.data.mstock_live_feed import MStockLiveFeed
        from backtest.data.db_source import DbSource
        
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days)
        
        # Check database first
        db = DbSource()
        df_db = db.get_candles(symbol, start=start_date, end=end_date, timeframe="1day")
        
        if df_db is not None and not df_db.empty:
            print(f"  OK Database has {len(df_db)} candles for {symbol}")
            print(f"    Date range: {df_db.index[0]} to {df_db.index[-1]}")
            print(f"    Latest close: {df_db['close'].iloc[-1]:.2f}")
            return True
        else:
            print(f"  WARNING  No historical data in database for {symbol}")
            print(f"    Run: python scripts/fetch_nse_indices_historical.py --symbols {symbol}")
            
            # Try fetching from mStock directly
            print(f"\n  Attempting to fetch from mStock API...")
            feed = MStockLiveFeed()
            
            if not feed.is_available():
                print("  FAIL mStock feed not available")
                return False
            
            df_api = feed.fetch_historical(
                symbol=symbol,
                start_date=start_date.strftime('%Y-%m-%d'),
                end_date=end_date.strftime('%Y-%m-%d'),
                interval='1day'
            )
            
            if df_api is not None and not df_api.empty:
                print(f"  OK Successfully fetched {len(df_api)} candles from mStock")
                print(f"    Date range: {df_api.index[0]} to {df_api.index[-1]}")
                print(f"    Latest close: {df_api['close'].iloc[-1]:.2f}")
                print(f"\n  TIP Tip: Save this to database with:")
                print(f"    db.save_candles('{symbol}', df_api)")
                return True
            else:
                print(f"  FAIL Failed to fetch from mStock API")
                return False
                
    except Exception as e:
        print(f"  FAIL Error checking historical data: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_strategy_trade(strategy_name: str = None):
    """Test if a strategy can identify trade opportunities."""
    print("\n" + "="*60)
    print("6. STRATEGY TRADE ELIGIBILITY TEST")
    print("="*60)
    
    try:
        from backtest.strategies import get_all_strategies
        
        strategies = get_all_strategies()
        
        if not strategies:
            print("  FAIL No strategies found")
            return False
        
        print(f"  Found {len(strategies)} strategies\n")
        
        for strat_class in strategies:
            strat = strat_class()
            name = strat.name if hasattr(strat, 'name') else strat.__class__.__name__
            
            if strategy_name and name.lower() != strategy_name.lower():
                continue
            
            eligible = getattr(strat, 'eligible_instruments', [])
            
            print(f"  Strategy: {name}")
            print(f"    Eligible instruments: {eligible if eligible else 'ALL (no restriction)'}")
            
            # Check if it would trade on common indices
            test_symbols = ["NIFTY", "BANKNIFTY", "NIFTYIT"]
            would_trade = []
            
            for sym in test_symbols:
                if not eligible or sym in eligible:
                    would_trade.append(sym)
            
            if would_trade:
                print(f"    OK Would trade on: {', '.join(would_trade)}")
            else:
                print(f"    FAIL Would NOT trade on test symbols")
            
            print()
        
        return True
        
    except Exception as e:
        print(f"  FAIL Error testing strategies: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    parser = argparse.ArgumentParser(description="Check mStock live feed status")
    parser.add_argument('--symbol', type=str, default='NIFTY', help='Symbol to test (default: NIFTY)')
    parser.add_argument('--test-trade', action='store_true', help='Test strategy trade eligibility')
    parser.add_argument('--strategy', type=str, help='Test specific strategy by name')
    
    args = parser.parse_args()
    
    print("\n" + "="*60)
    print("mStock Live Feed Diagnostic Tool")
    print("="*60)
    print(f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    # Run checks
    creds_ok = check_credentials()
    
    if not creds_ok:
        print("\n" + "="*60)
        print("DIAGNOSIS: Credentials missing - fix .env file first")
        print("="*60)
        return
    
    token_ok = check_session_token()
    market_open = check_market_hours()
    quote_ok = check_live_quote(args.symbol)
    hist_ok = check_historical_data(args.symbol)
    
    if args.test_trade or args.strategy:
        test_strategy_trade(args.strategy)
    
    # Summary
    print("\n" + "="*60)
    print("SUMMARY")
    print("="*60)
    
    results = [
        ("Credentials", creds_ok),
        ("Session Token", token_ok),
        ("Market Hours", market_open),
        ("Live Quotes", quote_ok),
        ("Historical Data", hist_ok),
    ]
    
    for name, ok in results:
        status = "OK" if ok else "FAIL"
        print(f"  {status} {name}")
    
    print("\n" + "="*60)
    if all(ok for _, ok in results):
        print("OK ALL CHECKS PASSED - mStock feed should be working")
        print("\nIf strategies still aren't trading, check:")
        print("  1. Strategy logic conditions (are they met?)")
        print("  2. Position sizing and capital")
        print("  3. Risk management rules (daily loss, max positions)")
        print("  4. Logs for error messages")
    else:
        print("FAIL SOME CHECKS FAILED")
        print("\nFix the failing checks above before running strategies")
    print("="*60 + "\n")


if __name__ == "__main__":
    main()
