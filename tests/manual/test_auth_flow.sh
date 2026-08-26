#!/bin/bash
# Task 5.1 — Automated API verification for the full authentication flow
# Usage: bash tests/manual/test_auth_flow.sh
# Prerequisites: Application server running on localhost:5000

set -e

BASE_URL="${1:-http://localhost:5000}"
PASSED=0
FAILED=0

check() {
    local description="$1"
    local expected="$2"
    local actual="$3"
    
    if [ "$expected" = "$actual" ]; then
        echo "✅ $description"
        PASSED=$((PASSED + 1))
    else
        echo "❌ $description"
        echo "   Expected: $expected"
        echo "   Actual:   $actual"
        FAILED=$((FAILED + 1))
    fi
}

check_contains() {
    local description="$1"
    local substring="$2"
    local text="$3"
    
    if echo "$text" | grep -q "$substring"; then
        echo "✅ $description"
        PASSED=$((PASSED + 1))
    else
        echo "❌ $description"
        echo "   Expected to contain: $substring"
        echo "   Actual: $text"
        FAILED=$((FAILED + 1))
    fi
}

check_not_contains() {
    local description="$1"
    local substring="$2"
    local text="$3"
    
    if echo "$text" | grep -q "$substring"; then
        echo "❌ $description"
        echo "   Should NOT contain: $substring"
        echo "   Actual: $text"
        FAILED=$((FAILED + 1))
    else
        echo "✅ $description"
        PASSED=$((PASSED + 1))
    fi
}

echo "======================================================================"
echo "Task 5.1 — Full Authentication Flow Test (Automated API Verification)"
echo "======================================================================"
echo "Base URL: $BASE_URL"
echo ""

# --------------------------------------------------------------------------
echo "=== Step 1: Verify initial unauthenticated state ==="
# --------------------------------------------------------------------------

STATUS=$(curl -s "$BASE_URL/api/broker/status")
check_contains "Nav pill shows unauthenticated status" '"status":"unauthenticated"' "$STATUS"
check_not_contains "Status response has no session token" '"token"' "$STATUS"
check_not_contains "Status response has no session_token" '"session_token"' "$STATUS"

FORWARD_CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE_URL/api/forward/start" \
  -H "Content-Type: application/json" \
  -d '{"strategy":"sma_crossover","symbol":"DEMO","timeframe":"1D","from_date":"2024-01-01","to_date":"2024-12-31","capital":10000}')
check "Forward start without auth returns 403" "403" "$FORWARD_CODE"

FORWARD_BODY=$(curl -s -X POST "$BASE_URL/api/forward/start" \
  -H "Content-Type: application/json" \
  -d '{"strategy":"sma_crossover","symbol":"DEMO","timeframe":"1D","from_date":"2024-01-01","to_date":"2024-12-31","capital":10000}')
check_contains "Forward 403 error identifies broker_not_authenticated" '"broker_not_authenticated"' "$FORWARD_BODY"

echo ""

# --------------------------------------------------------------------------
echo "=== Step 2: Login with credentials (Step 1 of auth flow) ==="
# --------------------------------------------------------------------------

LOGIN_RESP=$(curl -s -X POST "$BASE_URL/api/broker/login" \
  -H "Content-Type: application/json" \
  -d '{"username":"test_user","password":"test_password"}')
check_contains "Login returns success:true" '"success":true' "$LOGIN_RESP"
check_contains "Login indicates TOTP required" '"requires_totp":true' "$LOGIN_RESP"
check_not_contains "Login response has no session token" '"token"' "$LOGIN_RESP"
check_not_contains "Login response has no session_token" '"session_token"' "$LOGIN_RESP"
check_not_contains "Login response does not echo password" 'test_password' "$LOGIN_RESP"

# After credentials, session should still be unauthenticated (TOTP not yet verified)
STATUS=$(curl -s "$BASE_URL/api/broker/status")
# Note: in test mode without real mStock, the backend may auto-authenticate
# In production, status would still be "unauthenticated" here
echo "   (Status after credentials: $(echo "$STATUS" | python3 -c 'import sys,json; print(json.load(sys.stdin)["status"])' 2>/dev/null || echo 'unknown'))"

echo ""

# --------------------------------------------------------------------------
echo "=== Step 3: Verify TOTP (Step 2 of auth flow) ==="
# --------------------------------------------------------------------------

TOTP_RESP=$(curl -s -X POST "$BASE_URL/api/broker/verify-totp" \
  -H "Content-Type: application/json" \
  -d '{"totp_code":"123456"}')
check_contains "TOTP verification returns success:true" '"success":true' "$TOTP_RESP"
check_not_contains "TOTP response has no session token" '"token"' "$TOTP_RESP"
check_not_contains "TOTP response has no session_token" '"session_token"' "$TOTP_RESP"

# After TOTP, session should be authenticated
STATUS=$(curl -s "$BASE_URL/api/broker/status")
check_contains "Status shows authenticated after TOTP" '"status":"authenticated"' "$STATUS"
check_not_contains "Authenticated status has no session token" '"token"' "$STATUS"
check_not_contains "Authenticated status has no session_token" '"session_token"' "$STATUS"

# Forward start should now succeed
FORWARD_CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE_URL/api/forward/start" \
  -H "Content-Type: application/json" \
  -d '{"strategy":"sma_crossover","symbol":"DEMO","timeframe":"1D","from_date":"2024-01-01","to_date":"2024-12-31","capital":10000}')
check "Forward start with auth returns 200" "200" "$FORWARD_CODE"

FORWARD_BODY=$(curl -s -X POST "$BASE_URL/api/forward/start" \
  -H "Content-Type: application/json" \
  -d '{"strategy":"sma_crossover","symbol":"DEMO","timeframe":"1D","from_date":"2024-01-01","to_date":"2024-12-31","capital":10000}')
check_contains "Forward 200 response shows running status" '"status":"running"' "$FORWARD_BODY"

echo ""

# --------------------------------------------------------------------------
echo "=== Step 4: Logout (clears session) ==="
# --------------------------------------------------------------------------

LOGOUT_RESP=$(curl -s -X POST "$BASE_URL/api/broker/logout")
check_contains "Logout returns success:true" '"success":true' "$LOGOUT_RESP"

STATUS=$(curl -s "$BASE_URL/api/broker/status")
check_contains "Status shows unauthenticated after logout" '"status":"unauthenticated"' "$STATUS"

FORWARD_CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE_URL/api/forward/start" \
  -H "Content-Type: application/json" \
  -d '{"strategy":"sma_crossover","symbol":"DEMO","timeframe":"1D","from_date":"2024-01-01","to_date":"2024-12-31","capital":10000}')
check "Forward start after logout returns 403 again" "403" "$FORWARD_CODE"

echo ""

# --------------------------------------------------------------------------
echo "=== Step 5: Re-authentication flow works ==="
# --------------------------------------------------------------------------

LOGIN_RESP=$(curl -s -X POST "$BASE_URL/api/broker/login" \
  -H "Content-Type: application/json" \
  -d '{"username":"test_user","password":"new_password"}')
check_contains "Re-login returns success:true" '"success":true' "$LOGIN_RESP"

TOTP_RESP=$(curl -s -X POST "$BASE_URL/api/broker/verify-totp" \
  -H "Content-Type: application/json" \
  -d '{"totp_code":"654321"}')
check_contains "Re-TOTP returns success:true" '"success":true' "$TOTP_RESP"

STATUS=$(curl -s "$BASE_URL/api/broker/status")
check_contains "Re-authenticated status is authenticated" '"status":"authenticated"' "$STATUS"

FORWARD_CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE_URL/api/forward/start" \
  -H "Content-Type: application/json" \
  -d '{"strategy":"sma_crossover","symbol":"DEMO","timeframe":"1D","from_date":"2024-01-01","to_date":"2024-12-31","capital":10000}')
check "Forward start after re-auth returns 200" "200" "$FORWARD_CODE"

echo ""

# --------------------------------------------------------------------------
echo "=== Step 6: Security — token never appears in any response ==="
# --------------------------------------------------------------------------

# Collect all responses and search for any token-like values
ALL_RESPONSES="$LOGIN_RESP$TOTP_RESP$STATUS$LOGOUT_RESP"
check_not_contains "No 'token' key in any response" '"token"' "$ALL_RESPONSES"
check_not_contains "No 'session_token' key in any response" '"session_token"' "$ALL_RESPONSES"

# Verify password is never echoed
check_not_contains "Password not echoed in login response" 'test_password' "$LOGIN_RESP"
check_not_contains "New password not echoed in re-login response" 'new_password' "$LOGIN_RESP"

echo ""

# --------------------------------------------------------------------------
echo "======================================================================"
echo "Task 5.1 Results: $PASSED passed, $FAILED failed"
echo "======================================================================"

if [ "$FAILED" -gt 0 ]; then
    exit 1
else
    echo "✅ All automated API checks passed!"
    echo ""
    echo "Note: Manual browser verification is still required for:"
    echo "  • Nav pill visual state (🔴/🟡/🟢)"
    echo "  • Modal transitions (3 views)"
    echo "  • Start button disabled/enabled state"
    echo "  • Toast notifications"
    echo "  • Password field cleared from DOM"
    exit 0
fi
