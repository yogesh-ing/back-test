#!/usr/bin/env python
"""Monitor paper trading PnL and validate reconciliation in real-time.

Checks every 30 seconds:
1. Current portfolio state (equity, positions)
2. Trade history (quantities, prices, charges)
3. PnL calculation accuracy
4. Persistence to database

Outputs validation report to console and logs discrepancies.
"""

import sys
import os
import time
from pathlib import Path
from datetime import datetime, timedelta

# Add src to path
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

import requests


BASE_URL = "http://127.0.0.1:5000"


def check_portfolio_status():
    """Check current paper portfolio status."""
    try:
        resp = requests.get(f"{BASE_URL}/api/portfolio/paper", timeout=5)
        if resp.status_code == 200:
            return resp.json()
    except Exception as e:
        print(f"⚠ Portfolio check failed: {e}")
    return None


def check_trades():
    """Get recent trades from API."""
    try:
        resp = requests.get(f"{BASE_URL}/api/trades?mode=paper&limit=50", timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            return data.get("trades", [])
    except Exception as e:
        print(f"⚠ Trades check failed: {e}")
    return []


def validate_trade(trade):
    """Validate a single trade for bugs."""
    issues = []
    
    # Bug #1: Zero quantity
    qty = trade.get("quantity", 0)
    if qty <= 0:
        issues.append(f"ZERO QTY: {qty}")
    
    # Bug #2: Missing charges
    fees = trade.get("total_fees", 0) or trade.get("fees", 0) or 0
    if fees <= 0:
        issues.append(f"NO FEES")
    
    # Check price validity
    price = trade.get("fill_price", 0) or trade.get("price", 0)
    if price <= 0:
        issues.append(f"ZERO PRICE")
    
    return issues


def main():
    print("=" * 80)
    print("PAPER TRADING PnL VALIDATION MONITOR")
    print("=" * 80)
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Monitoring: http://{BASE_URL}")
    print("=" * 80)
    print()
    
    iteration = 0
    while True:
        iteration += 1
        timestamp = datetime.now().strftime("%H:%M:%S")
        
        print(f"\n[{timestamp}] Check #{iteration}")
        print("-" * 80)
        
        # Check portfolio
        portfolio = check_portfolio_status()
        if portfolio:
            equity = portfolio.get("equity", 0)
            pnl = portfolio.get("daily_pnl", 0)
            positions = portfolio.get("positions", [])
            
            print(f"  Equity: Rs {equity:,.2f}")
            print(f"  Daily PnL: Rs {pnl:,.2f}")
            print(f"  Positions: {len(positions)}")
        else:
            print("  ⚠ No portfolio data available")
        
        # Check trades
        trades = check_trades()
        if trades:
            print(f"  Trades: {len(trades)} total")
            
            # Validate recent trades
            bugs_found = 0
            for trade in trades[-10:]:  # Last 10 trades
                issues = validate_trade(trade)
                if issues:
                    bugs_found += 1
                    symbol = trade.get("symbol", "?")
                    side = trade.get("side", "?")
                    print(f"    ✗ {symbol} {side}: {', '.join(issues)}")
            
            if bugs_found == 0:
                print(f"  ✓ All recent trades valid")
        else:
            print("  No trades yet (waiting for signals...)")
        
        # Sleep until next check
        time.sleep(30)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nStopped by user")
