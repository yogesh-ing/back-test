"""Test mStock WebSocket for real-time market depth."""
import os
import sys
import json
import time
from pathlib import Path

# Add src and reference-code to path
sys.path.insert(0, str(Path(__file__).parent / "src"))
ref_path = Path(__file__).parent.parent / "reference-code" / "mstock-pytradingapi-typeA-OG"
if ref_path.exists():
    sys.path.insert(0, str(ref_path))

from dotenv import load_dotenv
load_dotenv()

API_KEY = os.getenv("MSTOCK_API_KEY", "").strip()

print("=" * 80)
print("mStock WebSocket Market Depth Test")
print("=" * 80)

# Get cached session token
cache_file = Path(__file__).parent / ".mstock_session_token"
if not cache_file.exists():
    print("[FAIL] No cached session token found. Please login first.")
    sys.exit(1)

access_token = cache_file.read_text().strip()
print(f"[OK] Using cached token: {access_token[:30]}...")

# Try to import MTicker
try:
    from backtest.data.mticker import MTicker
    print("[OK] MTicker class loaded successfully")
except ImportError as e:
    print(f"[FAIL] Cannot import MTicker: {e}")
    sys.exit(1)

SOCKET_URL = "wss://ws.mstock.trade"

# Track received ticks
tick_count = [0]
received_quotes = {}

def on_ticks(ws, ticks):
    """Callback when tick data arrives."""
    tick_count[0] += len(ticks)
    
    for tick in ticks:
        token = tick.get("instrument_token")
        if token:
            # Extract key fields
            last_price = tick.get("last_price", 0)
            mode = tick.get("mode", "unknown")
            depth = tick.get("depth")
            
            print(f"\n[Tick #{tick_count[0]}] Token: {token}, Mode: {mode}")
            print(f"  LTP: {last_price}")
            
            if depth:
                bids = depth.get("bid", [])
                asks = depth.get("ask", [])
                print(f"  Market Depth:")
                print(f"    Best Bid: {bids[0]['price'] if bids else 'N/A'} ({bids[0]['quantity'] if bids else 0} qty)")
                print(f"    Best Ask: {asks[0]['price'] if asks else 'N/A'} ({asks[0]['quantity'] if asks else 0} qty)")
                
                # Store for analysis
                received_quotes[str(token)] = {
                    "ltp": last_price,
                    "best_bid": bids[0]["price"] if bids else 0,
                    "best_ask": asks[0]["price"] if asks else 0,
                    "depth_levels": len(bids),
                }
            else:
                print(f"  No depth data (mode={mode})")
    
    # Stop after receiving 5 ticks
    if tick_count[0] >= 5:
        print(f"\n[OK] Received {tick_count[0]} ticks, stopping...")
        ws.close()

def on_connect(ws, response):
    """Called when WebSocket connects."""
    print("[OK] WebSocket connected!")
    
    # Send login (required by mStock)
    try:
        mticker.send_login_after_connect()
        print("[OK] Login sent")
    except Exception as e:
        print(f"[FAIL] Login error: {e}")
        return
    
    # Subscribe to NIFTY index (token 26000) in FULL mode for depth
    print("\nSubscribing to NSE:26000 (NIFTY) in FULL mode...")
    ws.subscribe([26000])
    ws.set_mode(MTicker.MODE_FULL, [26000])
    print("[OK] Subscribed!")

def on_close(ws, code, reason):
    """Called when WebSocket closes."""
    print(f"\nWebSocket closed: code={code}, reason={reason}")
    
    # Print summary
    if received_quotes:
        print("\n" + "=" * 80)
        print("SUMMARY - Real Market Depth Data Received:")
        print("=" * 80)
        for token, quote in received_quotes.items():
            print(f"\nToken {token}:")
            print(f"  LTP:      {quote['ltp']}")
            print(f"  Best Bid: {quote['best_bid']}")
            print(f"  Best Ask: {quote['best_ask']}")
            spread = quote['best_ask'] - quote['best_bid'] if quote['best_ask'] > 0 else 0
            print(f"  Spread:   {spread:.2f}")
            print(f"  Depth Levels: {quote['depth_levels']}")
    else:
        print("\n[WARN] No quotes with depth received")

def on_error(ws, code, reason):
    """Called on WebSocket error."""
    print(f"[FAIL] WebSocket error: code={code}, reason={reason}")

# Create MTicker instance
print("\nInitializing MTicker...")
mticker = MTicker(
    api_key=API_KEY,
    access_token=access_token,
    root=SOCKET_URL,
    debug=True,  # Enable debug logging
    reconnect=True,
)

# Assign callbacks
mticker.on_ticks = on_ticks
mticker.on_connect = on_connect
mticker.on_close = on_close
mticker.on_error = on_error

# Connect
print("Connecting to WebSocket...")
print("(Will stop automatically after 5 ticks or press Ctrl+C)\n")

try:
    mticker.connect(threaded=False)  # Blocking call
except KeyboardInterrupt:
    print("\n\nStopped by user")
finally:
    print("\nTest complete!")
