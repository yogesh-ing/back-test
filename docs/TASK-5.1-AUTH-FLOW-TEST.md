# Task 5.1 — Full Authentication Flow Test

## Objective

Perform a manual end-to-end walkthrough of the complete authentication flow to verify all components work together correctly. This test validates the integration of:

- Nav pill status indicator (Task 3.1)
- Auth modal (Task 3.2)
- Session expiry toasts (Task 3.3)
- Client-side forward start gate (Task 4.1)
- Server-side forward start guard (Task 4.2)

## Test Environment Setup

### Prerequisites

1. **Start the application server:**
   ```bash
   cd /home/user/back-test
   source venv/bin/activate
   PYTHONPATH=src python -m backtest.web.app --host 0.0.0.0 --port 5000
   ```

2. **Open browser to:** `http://localhost:5000/dashboard`

3. **Open browser DevTools:**
   - Network tab (to verify no tokens in responses)
   - Console tab (to check for errors)
   - Application tab (to inspect cookies/storage)

4. **Note:** For this test, we'll use the **test mode** which simulates authentication without requiring real mStock credentials. The test mode is automatically enabled when `MSTOCK_API_KEY` is not set.

## 7-Step Authentication Flow Test

### Step 1: Verify Initial State (Unauthenticated)

**Action:** Open the application in a fresh browser session (clear cookies/storage).

**Expected Results:**
- ✅ Nav pill shows 🔴 (red dot) + "mStock" text
- ✅ Tooltip on hover: "mStock: not connected — click to log in"
- ✅ Navigate to `/forward` page
- ✅ Start button is disabled with text: "🔴 Connect mStock to Start"
- ✅ Start button tooltip: "Authentication required before starting forward test"
- ✅ Clicking the disabled Start button opens the auth modal

**Verification Commands:**
```bash
# Check nav pill state via API
curl http://localhost:5000/api/broker/status
# Expected: {"status":"unauthenticated","broker":"mstock","broker_display_name":"mStock","expires_at":null}

# Check forward start returns 403
curl -X POST http://localhost:5000/api/forward/start \
  -H "Content-Type: application/json" \
  -d '{"strategy":"sma_crossover","symbol":"DEMO","timeframe":"1D","from_date":"2024-01-01","to_date":"2024-12-31","capital":10000}'
# Expected: 403 Forbidden with error "broker_not_authenticated"
```

---

### Step 2: Click Nav Pill → Modal Opens at Step 1 (Credentials)

**Action:** Click the nav pill (🔴 mStock) in the top-right corner.

**Expected Results:**
- ✅ Modal overlay appears with semi-transparent background
- ✅ Modal title: "🔐 mStock Login"
- ✅ Modal shows Step 1 view:
  - Username field (enabled, empty)
  - Password field (enabled, empty)
  - "Login" button (enabled)
  - Divider line
  - "🔒 TOTP Verification" section (disabled/greyed out)
  - TOTP input field (disabled)
  - "Continue" button (disabled)
- ✅ Close button (×) visible in top-right
- ✅ Pressing Escape key closes the modal
- ✅ Clicking outside the modal (on overlay) closes the modal

**Browser DevTools Check:**
- ✅ No errors in Console
- ✅ No network requests yet (modal is client-side)

---

### Step 3: Enter Credentials → Login → TOTP Step Enabled

**Action:** 
1. Enter username: `test_user`
2. Enter password: `test_password`
3. Click "Login" button

**Expected Results:**
- ✅ Login button shows spinner during API call
- ✅ Password field is cleared immediately after click (security measure)
- ✅ API call: `POST /api/broker/login` with `{username, password}`
- ✅ Network tab shows the request body (username/password present)
- ✅ After success, modal transitions to Step 2 view:
  - "✅ Credentials verified" message (green)
  - Divider line
  - "TOTP Verification" section (now enabled)
  - TOTP input field (enabled, auto-focused)
  - "Continue" button (enabled)
- ✅ Modal title remains: "🔐 mStock Login"
- ✅ Username field is no longer visible (cleared from view)

**Browser DevTools Check:**
- ✅ Network tab: `POST /api/broker/login` → 200 OK
- ✅ Response body: `{"success":true,"message":"","requires_totp":true}`
- ✅ **CRITICAL:** Response does NOT contain any session token
- ✅ Console: No errors

**API Verification:**
```bash
# Verify session state after credentials (but before TOTP)
curl http://localhost:5000/api/broker/status
# Expected: status is still "unauthenticated" (TOTP not yet verified)
```

---

### Step 4: Enter TOTP → Continue → Authenticated

**Action:**
1. Enter TOTP code: `123456` (test mode accepts any 6-digit code)
2. Click "Continue" button

**Expected Results:**
- ✅ Continue button shows spinner during API call
- ✅ API call: `POST /api/broker/verify-totp` with `{totp_code: "123456"}`
- ✅ After success, modal transitions to Step 3 (Authenticated) view:
  - Modal title: "🟢 mStock Connected"
  - Status: "Authenticated"
  - Expires At: [formatted time, e.g., "03:45 PM"]
  - Broker: "mStock"
  - "Logout" button (red, enabled)
- ✅ Modal overlay remains open (user must explicitly close or logout)
- ✅ Nav pill in background updates to 🟢 (green dot)
- ✅ Nav pill tooltip: "mStock: connected (expires 03:45 PM)"

**Browser DevTools Check:**
- ✅ Network tab: `POST /api/broker/verify-totp` → 200 OK
- ✅ Response body: `{"success":true,"message":"","expires_at":"2026-08-26T..."}`
- ✅ **CRITICAL:** Response does NOT contain the session token
- ✅ Console: No errors

**API Verification:**
```bash
# Verify session is now authenticated
curl http://localhost:5000/api/broker/status
# Expected: {"status":"authenticated","broker":"mstock","broker_display_name":"mStock","expires_at":"..."}

# Verify forward start now succeeds
curl -X POST http://localhost:5000/api/forward/start \
  -H "Content-Type: application/json" \
  -d '{"strategy":"sma_crossover","symbol":"DEMO","timeframe":"1D","from_date":"2024-01-01","to_date":"2024-12-31","capital":10000}'
# Expected: 200 OK with {"status":"running",...}
```

---

### Step 5: Verify Forward Page Start Button Enabled

**Action:** Navigate to `/forward` page (or refresh if already there).

**Expected Results:**
- ✅ Start button is enabled with text: "▶ Start Forward Test"
- ✅ Start button has no special tooltip (normal behavior)
- ✅ Can successfully start a forward test:
  - Select a strategy
  - Configure parameters
  - Click "Start" button
  - Forward test begins (status changes to "Running")
- ✅ Nav pill remains 🟢 (green)

**Browser DevTools Check:**
- ✅ Network tab: `POST /api/forward/start` → 200 OK
- ✅ Console: No errors
- ✅ Forward test progresses normally

---

### Step 6: Click Logout → Session Cleared

**Action:**
1. Click the nav pill (🟢 mStock) to open the modal
2. Modal shows Step 3 (Authenticated) view
3. Click "Logout" button

**Expected Results:**
- ✅ API call: `POST /api/broker/logout`
- ✅ After success, modal transitions back to Step 1 (Credentials) view:
  - Modal title: "🔐 mStock Login"
  - Username field (enabled, empty)
  - Password field (enabled, empty)
  - "Login" button (enabled)
  - TOTP section (disabled again)
- ✅ Nav pill updates to 🔴 (red dot)
- ✅ Nav pill tooltip: "mStock: not connected — click to log in"
- ✅ Navigate to `/forward` page
- ✅ Start button is disabled again: "🔴 Connect mStock to Start"
- ✅ No "session expired" toast appears (because this was a user-initiated logout)

**Browser DevTools Check:**
- ✅ Network tab: `POST /api/broker/logout` → 200 OK
- ✅ Response body: `{"success":true}`
- ✅ Console: No errors

**API Verification:**
```bash
# Verify session is cleared
curl http://localhost:5000/api/broker/status
# Expected: {"status":"unauthenticated",...}

# Verify forward start is blocked again
curl -X POST http://localhost:5000/api/forward/start \
  -H "Content-Type: application/json" \
  -d '{"strategy":"sma_crossover",...}'
# Expected: 403 Forbidden
```

---

### Step 7: Verify Re-Authentication Flow Works

**Action:**
1. Modal is still open at Step 1 (from logout)
2. Enter credentials again: `test_user` / `test_password`
3. Click "Login"
4. Enter TOTP: `654321`
5. Click "Continue"

**Expected Results:**
- ✅ Full authentication flow completes successfully (same as Steps 3-4)
- ✅ Nav pill updates to 🟢 (green)
- ✅ Forward Start button is enabled again
- ✅ Can start a new forward test successfully
- ✅ Session is fully functional

**Browser DevTools Check:**
- ✅ All API calls succeed (200 OK)
- ✅ No tokens in any response bodies
- ✅ No console errors

---

## Security Verification (During All Steps)

### Network Tab Inspection

**Throughout the entire test, verify:**

1. ✅ **No session tokens in responses:**
   - `GET /api/broker/status` → no token field
   - `POST /api/broker/login` → no token field
   - `POST /api/broker/verify-totp` → no token field
   - `POST /api/broker/logout` → no token field

2. ✅ **Password cleared from DOM:**
   - After clicking "Login", inspect the password field
   - Field should be empty (value="")

3. ✅ **No credentials in logs:**
   - Server console should not log usernames or passwords
   - Only log outcomes: "login successful", "TOTP verified", etc.

4. ✅ **Session token never sent to browser:**
   - Search all network responses for the session token
   - Token should never appear in any response

### Console Inspection

**Verify no errors or warnings:**
- ✅ No JavaScript errors
- ✅ No failed network requests (except intentional 403s)
- ✅ No CORS errors
- ✅ No authentication-related warnings

---

## Automated Test Script

To automate the API-level verification (Steps 1, 3, 4, 6):

```bash
#!/bin/bash
# test_auth_flow.sh — Automated API verification for Task 5.1

BASE_URL="http://localhost:5000"

echo "=== Step 1: Verify initial unauthenticated state ==="
STATUS=$(curl -s $BASE_URL/api/broker/status)
echo "Status: $STATUS"
echo "$STATUS" | grep -q '"status":"unauthenticated"' && echo "✅ Unauthenticated" || echo "❌ Failed"

FORWARD_RESP=$(curl -s -o /dev/null -w "%{http_code}" -X POST $BASE_URL/api/forward/start \
  -H "Content-Type: application/json" \
  -d '{"strategy":"sma_crossover","symbol":"DEMO","timeframe":"1D","from_date":"2024-01-01","to_date":"2024-12-31","capital":10000}')
echo "Forward start (no auth): HTTP $FORWARD_RESP"
[ "$FORWARD_RESP" = "403" ] && echo "✅ Blocked" || echo "❌ Failed"

echo ""
echo "=== Step 3: Login with credentials ==="
LOGIN_RESP=$(curl -s -X POST $BASE_URL/api/broker/login \
  -H "Content-Type: application/json" \
  -d '{"username":"test_user","password":"test_password"}')
echo "Login response: $LOGIN_RESP"
echo "$LOGIN_RESP" | grep -q '"success":true' && echo "✅ Login successful" || echo "❌ Failed"
echo "$LOGIN_RESP" | grep -q "token" && echo "❌ Token found in response!" || echo "✅ No token in response"

echo ""
echo "=== Step 4: Verify TOTP ==="
TOTP_RESP=$(curl -s -X POST $BASE_URL/api/broker/verify-totp \
  -H "Content-Type: application/json" \
  -d '{"totp_code":"123456"}')
echo "TOTP response: $TOTP_RESP"
echo "$TOTP_RESP" | grep -q '"success":true' && echo "✅ TOTP verified" || echo "❌ Failed"
echo "$TOTP_RESP" | grep -q "token" && echo "❌ Token found in response!" || echo "✅ No token in response"

STATUS=$(curl -s $BASE_URL/api/broker/status)
echo "Status after auth: $STATUS"
echo "$STATUS" | grep -q '"status":"authenticated"' && echo "✅ Authenticated" || echo "❌ Failed"

FORWARD_RESP=$(curl -s -o /dev/null -w "%{http_code}" -X POST $BASE_URL/api/forward/start \
  -H "Content-Type: application/json" \
  -d '{"strategy":"sma_crossover","symbol":"DEMO","timeframe":"1D","from_date":"2024-01-01","to_date":"2024-12-31","capital":10000}')
echo "Forward start (authenticated): HTTP $FORWARD_RESP"
[ "$FORWARD_RESP" = "200" ] && echo "✅ Allowed" || echo "❌ Failed"

echo ""
echo "=== Step 6: Logout ==="
LOGOUT_RESP=$(curl -s -X POST $BASE_URL/api/broker/logout)
echo "Logout response: $LOGOUT_RESP"
echo "$LOGOUT_RESP" | grep -q '"success":true' && echo "✅ Logout successful" || echo "❌ Failed"

STATUS=$(curl -s $BASE_URL/api/broker/status)
echo "Status after logout: $STATUS"
echo "$STATUS" | grep -q '"status":"unauthenticated"' && echo "✅ Unauthenticated" || echo "❌ Failed"

echo ""
echo "=== Task 5.1 Complete ==="
```

---

## Success Criteria

All 7 steps must pass with the following outcomes:

- ✅ Nav pill correctly reflects authentication state (🔴 → 🟢 → 🔴)
- ✅ Modal transitions correctly through all 3 views
- ✅ Credentials are cleared from DOM after login
- ✅ No session tokens appear in any API response
- ✅ Forward Start button is gated on authentication (client-side)
- ✅ Forward API returns 403 without authentication (server-side)
- ✅ Forward API returns 200 with authentication
- ✅ Logout clears session and re-enables the gate
- ✅ Re-authentication flow works cleanly
- ✅ No JavaScript errors or console warnings
- ✅ No "session expired" toast on user-initiated logout

---

## Notes

- This test requires a running application server
- For production testing with real mStock credentials, replace `test_user`/`test_password` with actual credentials and use a real TOTP code from an authenticator app
- The test mode (no `MSTOCK_API_KEY`) simulates successful authentication for testing purposes
- All automated API checks can be run via the provided bash script
- Manual browser verification is required for UI behavior (modal transitions, button states, toasts)
