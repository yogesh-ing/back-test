"""Test mStock quote methods - Method 1 (WebSocket) and Method 2 (REST TypeB)."""
import os
import sys
import json
import requests
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

# Load .env
from dotenv import load_dotenv
load_dotenv()

API_KEY = os.getenv("MSTOCK_API_KEY", "").strip()
USERNAME = os.getenv("MSTOCK_USERNAME", "").strip()
PASSWORD = os.getenv("MSTOCK_PASSWORD", "").strip()

print("=" * 80)
print("mStock Quote Methods Test")
print("=" * 80)
print(f"API Key: {API_KEY[:10]}...{API_KEY[-5:]}" if API_KEY else "API Key: NOT SET")
print(f"Username: {USERNAME}")
print()

# First, we need to authenticate to get a session token
def authenticate():
    """Login with username/password + TOTP to get access token using raw HTTP."""
    
    # Step 1: Login with credentials
    print("Step 1: Logging in with credentials...")
    login_url = "https://api.mstock.trade/openapi/typea/connect/login"
    headers = {'X-Mirae-Version': '1'}
    
    try:
        resp = requests.post(login_url, data={'Username': USERNAME, 'Password': PASSWORD}, 
                           headers=headers, timeout=10)
        print(f"  Response status: {resp.status_code}")
        
        if resp.status_code != 200:
            print(f"  ✗ Login failed: {resp.text[:200]}")
            return None
        
        login_data = resp.json()
        print(f"  Login response: {json.dumps(login_data, indent=2)[:300]}")
        
        # Extract API key from response (if present)
        api_key_from_login = login_data.get("api_key") or API_KEY
        
    except Exception as e:
        print(f"  ✗ Login error: {e}")
        return None
    
    # Step 2: Verify TOTP (you'll need to provide this)
    totp_code = input("\nEnter 6-digit TOTP code: ").strip()
    if len(totp_code) != 6 or not totp_code.isdigit():
        print("Invalid TOTP code!")
        return None
    
    print("\nStep 2: Verifying TOTP...")
    verify_url = "https://api.mstock.trade/openapi/typea/session/verifytotp"
    
    try:
        resp = requests.post(verify_url, data={'api_key': api_key_from_login, 'totp': totp_code},
                           headers=headers, timeout=10)
        print(f"  Response status: {resp.status_code}")
        
        if resp.status_code != 200:
            print(f"  ✗ TOTP verification failed: {resp.text[:200]}")
            return None
        
        verify_data = resp.json()
        print(f"  TOTP response: {json.dumps(verify_data, indent=2)[:300]}")
        
        access_token = verify_data.get("access_token")
        if not access_token:
            print("  ✗ No access_token in response!")
            return None
        
        print(f"  [OK] Authenticated! Access token: {access_token[:20]}...")
        return access_token
        
    except Exception as e:
        print(f"  ✗ TOTP verification error: {e}")
        return None


def test_method2_rest_typeb(access_token):
    """Method 2: REST API /openapi/typeb/instruments/quote with QUOTE mode."""
    print("\n" + "=" * 80)
    print("METHOD 2: REST API - /openapi/typeb/instruments/quote")
    print("=" * 80)
    
    # Extract the inner ACCESS_TOKEN from the outer JWT
    import base64
    parts = access_token.split('.')
    if len(parts) == 3:
        payload = parts[1]
        payload += '=' * (4 - len(payload) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(payload))
        inner_token = decoded.get("ACCESS_TOKEN", access_token)
        print(f"Using inner ACCESS_TOKEN: {inner_token[:20]}...")
    else:
        inner_token = access_token
    
    url = "https://api.mstock.trade/openapi/typeb/instruments/quote"
    
    # Try multiple auth formats
    auth_formats = [
        ("Bearer token (TypeB)", {
            'X-Mirae-Version': '1',
            'Authorization': f'Bearer {inner_token}',
            'X-PrivateKey': API_KEY,
            'Content-Type': 'application/json'
        }),
        ("Token API_KEY:ACCESS_TOKEN (TypeA)", {
            'X-Mirae-Version': '1',
            'Authorization': f'token {API_KEY}:{inner_token}',
            'Content-Type': 'application/json'
        }),
        ("Bearer outer JWT", {
            'X-Mirae-Version': '1',
            'Authorization': f'Bearer {access_token}',
            'X-PrivateKey': API_KEY,
            'Content-Type': 'application/json'
        })
    ]
    
    # Test with NIFTY index contract
    payload = {
        "mode": "QUOTE",  # Fetches quotes along with market depth
        "exchangeTokens": {
            "NSE": ["26000"],  # NIFTY index token
        }
    }
    
    print(f"\nRequest URL: {url}")
    print(f"Payload: {json.dumps(payload, indent=2)}")
    
    for auth_name, headers in auth_formats:
        print(f"\nTrying auth format: {auth_name}")
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=10)
            print(f"  Response Status: {response.status_code}")
            
            if response.status_code == 200:
                data = response.json()
                print(f"\n[OK] SUCCESS with {auth_name}!")
                print(json.dumps(data, indent=2)[:2000])
                
                # Check for bid/ask in response
                if "data" in data:
                    for exchange, instruments in data["data"].items():
                        print(f"\n{exchange} instruments:")
                        for symbol, quote in instruments.items():
                            print(f"  {symbol}:")
                            if isinstance(quote, dict):
                                ltp = quote.get("ltp") or quote.get("last_price")
                                bid = quote.get("bid") or quote.get("best_bid")
                                ask = quote.get("ask") or quote.get("best_ask")
                                print(f"    LTP: {ltp}")
                                print(f"    Bid: {bid}")
                                print(f"    Ask: {ask}")
                                
                                # Check for market depth
                                if "depth" in quote:
                                    print(f"    Market Depth: {quote['depth']}")
                return  # Success, stop trying
            else:
                print(f"  Response: {response.text[:200]}")
                
        except Exception as e:
            print(f"  [FAIL] Error: {e}")
    
    print("\n[FAIL] All auth formats failed!")


def test_method1_websocket(access_token):
    """Method 1: WebSocket streaming via MTicker."""
    print("\n" + "=" * 80)
    print("METHOD 1: WebSocket Streaming - MTicker")
    print("=" * 80)
    
    # Check if tradingapi_b is available
    try:
        from tradingapi_b.mticker import MTicker
        print("[OK] MTicker class found!")
    except ImportError:
        print("[FAIL] MTicker not available - tradingapi_b package not installed")
        print("\nTo install, you would need:")
        print("  pip install tradingapi-b")
        print("  OR copy the MTicker class from mStock SDK")
        return
    
    SOCKET_URL = "wss://ws.mstock.trade"
    
    print(f"\nInitializing MTicker...")
    m_ticker = MTicker(API_KEY, access_token, SOCKET_URL)
    
    tick_count = [0]  # Use list to allow mutation in closure
    
    def on_ticks(ws, ticks):
        tick_count[0] += 1
        print(f"\n[Tick #{tick_count[0]}] Market Depth Data received:")
        print(json.dumps(ticks, indent=2, default=str))
        
        # Stop after receiving 3 ticks for testing
        if tick_count[0] >= 3:
            print("\n[OK] Received 3 ticks, stopping...")
            ws.close()
    
    def on_connect(ws, response):
        print("[OK] Connected to WebSocket successfully!")
        
        # Required by mStock to maintain session immediately after connection
        m_ticker.send_login_after_connect()
        print("[OK] Sent login after connect")
        
        # Subscribe to NIFTY index (token 26000)
        # Mode 3 = full quote with market depth
        print("\nSubscribing to NSE:26000 (NIFTY) in QUOTE mode...")
        ws.subscribe("NSE", [26000], mode=3)
        print("[OK] Subscribed!")
    
    def on_close(ws, code, reason):
        print(f"\nWebSocket closed. Code: {code}, Reason: {reason}")
    
    def on_error(ws, error):
        print(f"\n[FAIL] WebSocket error: {error}")
    
    # Assign callbacks
    m_ticker.on_ticks = on_ticks
    m_ticker.on_connect = on_connect
    m_ticker.on_close = on_close
    m_ticker.on_error = on_error
    
    # Start listening
    print("\nConnecting to WebSocket...")
    try:
        m_ticker.connect()
    except KeyboardInterrupt:
        print("\n\nStopped by user")


if __name__ == "__main__":
    # Try to get existing session token first
    print("Checking for existing session...")
    cache_file = Path(__file__).parent / ".mstock_session_token"
    
    if cache_file.exists():
        access_token = cache_file.read_text().strip()
        print(f"[OK] Using cached session token: {access_token[:30]}...")
    else:
        print("No cached session, authenticating with mStock...")
        access_token = authenticate()
        
        if not access_token:
            print("\n[FAIL] Authentication failed. Cannot proceed with tests.")
            sys.exit(1)
    
    print("\n" + "=" * 80)
    print("Which method do you want to test?")
    print("=" * 80)
    print("1. Method 2 only (REST TypeB - simpler)")
    print("2. Method 1 only (WebSocket - real-time)")
    print("3. Both methods")
    
    choice = input("\nEnter choice (1/2/3): ").strip()
    
    if choice in ("1", "3"):
        test_method2_rest_typeb(access_token)
    
    if choice in ("2", "3"):
        test_method1_websocket(access_token)
    
    print("\n" + "=" * 80)
    print("Tests complete!")
    print("=" * 80)
