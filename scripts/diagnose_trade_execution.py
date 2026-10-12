#!/usr/bin/env python3
"""Diagnose why instant buy trades aren't executing.

Checks the entire trading pipeline from strategy signals to order execution.
"""

import sys
from pathlib import Path

project_root = Path(__file__).resolve().parents[1]
src_dir = project_root / "src"
sys.path.insert(0, str(src_dir))

from dotenv import load_dotenv
load_dotenv(project_root / ".env")

print("="*70)
print("TRADE EXECUTION DIAGNOSTIC")
print("="*70)

# 1. Check if strategy is registered
print("\n[1] Strategy Registration:")
try:
    from backtest.strategy.registry import list_strategies
    
    strategies = list_strategies()
    strategy_names = [s.get('name') for s in strategies]
    
    print(f"  Total strategies: {len(strategies)}")
    
    if 'atm_instant_buy' in strategy_names:
        print("  [OK] atm_instant_buy is registered")
    else:
        print("  [FAIL] atm_instant_buy NOT registered")
        print(f"  Available: {', '.join(sorted(strategy_names))}")
        
except Exception as e:
    print(f"  [FAIL] Error: {e}")

# 2. Test signal generation
print("\n[2] Signal Generation Test:")
try:
    import pandas as pd
    from backtest.strategies.atm_instant_buy import AtmInstantBuy
    
    # Create sample data
    dates = pd.date_range('2024-01-01', periods=10, freq='D')
    candles = pd.DataFrame({
        'open': [100] * 10,
        'high': [101] * 10,
        'low': [99] * 10,
        'close': [100.5] * 10,
        'volume': [1000] * 10,
    }, index=dates)
    
    strategy = AtmInstantBuy()
    signals = strategy.generate_signals(candles)
    
    print(f"  Candles: {len(candles)} bars")
    print(f"  Signals: {signals.tolist()}")
    print(f"  All BUY (1): {all(s == 1 for s in signals)}")
    
    if all(s == 1 for s in signals):
        print("  [OK] Strategy generates correct signals")
    else:
        print("  [FAIL] Strategy not generating BUY signals")
        
except Exception as e:
    print(f"  [FAIL] Error: {e}")
    import traceback
    traceback.print_exc()

# 3. Check instrument eligibility
print("\n[3] Instrument Eligibility:")
try:
    strategy = AtmInstantBuy()
    eligible = getattr(strategy, 'eligible_instruments', None)
    
    if eligible is None:
        print("  [OK] No restrictions - can trade any instrument")
    else:
        print(f"  [INFO] Restricted to: {eligible}")
        
    # Test NIFTY
    test_symbols = ["NIFTY", "BANKNIFTY"]
    for sym in test_symbols:
        can_trade = eligible is None or sym in eligible
        status = "[OK]" if can_trade else "[BLOCKED]"
        print(f"  {status} Can trade {sym}")
        
except Exception as e:
    print(f"  [FAIL] Error: {e}")

# 4. Check database for price data
print("\n[4] Price Data Availability:")
try:
    from backtest.data.db_source import DbSource
    from datetime import datetime, timedelta
    
    db = DbSource()
    test_symbol = "NIFTY"
    end_date = datetime.now()
    start_date = end_date - timedelta(days=7)
    
    try:
        df = db.get_candles(test_symbol, start=start_date, end=end_date)
        if df is not None and not df.empty:
            print(f"  [OK] {test_symbol}: {len(df)} candles available")
            print(f"       Latest: {df.index[-1]} @ {df['close'].iloc[-1]:.2f}")
        else:
            print(f"  [FAIL] {test_symbol}: NO DATA in database")
            print(f"         Run: python scripts/fetch_indices.py {test_symbol}")
    except Exception as e:
        print(f"  [FAIL] {test_symbol}: {e}")
        
except Exception as e:
    print(f"  [FAIL] Database error: {e}")

# 5. Check running instances
print("\n[5] Running Forward Instances:")
try:
    import requests
    
    resp = requests.get('http://127.0.0.1:5000/api/forward/runners')
    if resp.status_code == 200:
        runners = resp.json()
        print(f"  Active runners: {len(runners)}")
        
        for r in runners:
            name = r.get('name', 'unnamed')
            status = r.get('status', 'unknown')
            symbol = r.get('symbol', '?')
            strategy = r.get('strategy', '?')
            
            print(f"    - {name}: {strategy} on {symbol} ({status})")
            
            # Check if atm_instant_buy is running
            if strategy == 'atm_instant_buy':
                print(f"      [OK] Found atm_instant_buy runner!")
    else:
        print(f"  [FAIL] API returned {resp.status_code}")
        
except Exception as e:
    print(f"  [FAIL] Could not check runners: {e}")

# 6. Check portfolio state
print("\n[6] Portfolio State:")
try:
    import json
    
    state_file = project_root / "data" / "portfolio_state.json"
    if state_file.exists():
        with open(state_file) as f:
            state = json.load(f)
        
        positions = state.get('positions', [])
        orders = state.get('orders', [])
        
        print(f"  Open positions: {len(positions)}")
        print(f"  Total orders: {len(orders)}")
        
        if positions:
            print("  Recent positions:")
            for pos in positions[-3:]:
                print(f"    - {pos.get('symbol')}: {pos.get('qty')} @ {pos.get('entry_price')}")
        else:
            print("  [INFO] No open positions")
            
        if orders:
            recent_orders = [o for o in orders if o.get('status') in ['pending', 'filled']]
            print(f"\n  Recent orders: {len(recent_orders)}")
            for order in recent_orders[-3:]:
                print(f"    - {order.get('symbol')}: {order.get('side')} {order.get('status')}")
    else:
        print(f"  [INFO] No portfolio state file found")
        
except Exception as e:
    print(f"  [FAIL] Error: {e}")

# 7. Common blockers
print("\n[7] Common Trade Blockers:")
blockers = []

# Check if market is closed
from datetime import datetime, timezone, timedelta
now_utc = datetime.now(timezone.utc)
ist = now_utc + timedelta(hours=5, minutes=30)
is_weekday = ist.weekday() < 5
market_open = ist.replace(hour=9, minute=15) <= ist <= ist.replace(hour=15, minute=30)

if not (is_weekday and market_open):
    blockers.append("Market is CLOSED (trades only execute during market hours)")
else:
    print("  [OK] Market is OPEN")

# Check risk limits
try:
    import json
    state_file = project_root / "data" / "portfolio_state.json"
    if state_file.exists():
        with open(state_file) as f:
            state = json.load(f)
        
        risk = state.get('risk_limits', {})
        daily_loss = risk.get('daily_loss_limit', 0)
        current_pnl = state.get('current_pnl', 0)
        
        if daily_loss > 0 and current_pnl <= -daily_loss:
            blockers.append(f"Daily loss limit hit: {current_pnl:.2f} / {-daily_loss:.2f}")
        else:
            print("  [OK] Within daily loss limits")
            
        max_positions = risk.get('max_positions', 10)
        current_positions = len(state.get('positions', []))
        
        if current_positions >= max_positions:
            blockers.append(f"Max positions reached: {current_positions}/{max_positions}")
        else:
            print(f"  [OK] Positions: {current_positions}/{max_positions}")
            
except Exception as e:
    print(f"  [WARN] Could not check risk limits: {e}")

if blockers:
    print("\n  ⚠️  BLOCKERS FOUND:")
    for b in blockers:
        print(f"    - {b}")
else:
    print("  [OK] No obvious blockers")

# Summary
print("\n" + "="*70)
print("DIAGNOSIS SUMMARY")
print("="*70)
print("\nTo test instant buy:")
print("  1. Ensure you have price data (check #4)")
print("  2. Create a forward instance with atm_instant_buy strategy")
print("  3. Use symbol: NIFTY or BANKNIFTY")
print("  4. Timeframe: 1min for quick results")
print("  5. Check logs for signal generation")
print("\nIf still not working, check:")
print("  - Server console logs for errors")
print("  - Risk management settings")
print("  - Broker connectivity (for live mode)")
print("="*70)
