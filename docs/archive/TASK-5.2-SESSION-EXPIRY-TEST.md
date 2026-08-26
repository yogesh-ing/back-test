# Task 5.2 — Session Expiry Warning Test

## Objective

Test the session expiry warning system to ensure users are properly notified when their broker session is about to expire, and that the system correctly handles the transition from authenticated → expiring_soon → expired states.

## Test Scenarios

### Scenario 1: Normal Session Flow (No Expiry Warning)

**Setup:**
- Start with authenticated session
- Session has > 10 minutes remaining

**Expected Behavior:**
- ✅ Nav pill shows 🟢 (green) status indicator
- ✅ No warning toast appears
- ✅ Forward test can be started
- ✅ Status remains stable

**Test Steps:**
1. Authenticate with broker credentials
2. Verify session status is "authenticated"
3. Wait 1-2 minutes
4. Verify no expiry warning appears
5. Verify nav pill remains green

### Scenario 2: Session Expiring Soon Warning

**Setup:**
- Start with authenticated session
- Manually set session to expire in 5 minutes (simulated)

**Expected Behavior:**
- ✅ Nav pill transitions from 🟢 to 🟡 (yellow)
- ✅ Warning toast appears: "⚠️ mStock session expiring in 5 minutes"
- ✅ Toast is clickable → opens auth modal
- ✅ Forward test can still be started (session still valid)
- ✅ Warning toast does NOT reappear on subsequent polls (debounce works)

**Test Steps:**
1. Authenticate with broker credentials
2. Modify session expiry time to 5 minutes from now
3. Wait for next status poll (60 seconds)
4. Verify nav pill turns yellow
5. Verify warning toast appears
6. Click toast → verify auth modal opens
7. Verify forward test can still start
8. Wait for another poll cycle
9. Verify warning toast does NOT reappear (debounced)

**Manual Test Procedure:**
```bash
# 1. Start the application
python src/backtest/web/app.py

# 2. Authenticate via UI
#    - Click mStock nav pill
#    - Enter credentials
#    - Complete 2FA

# 3. Open browser console and modify session expiry
#    (This simulates a session that's about to expire)
fetch('/api/broker/status').then(r => r.json()).then(console.log)

# 4. To simulate expiry, you can:
#    - Option A: Modify the session TTL in config to a very short value
#    - Option B: Use a test broker that returns expiring_soon status
#    - Option C: Wait for a real session to approach expiry

# 5. Observe:
#    - Nav pill color change
#    - Warning toast appearance
#    - Toast click behavior
```

### Scenario 3: Session Expired Handling

**Setup:**
- Start with authenticated session
- Let session expire naturally (or simulate expiry)

**Expected Behavior:**
- ✅ Nav pill transitions from 🟡 to 🔴 (red)
- ✅ Error toast appears: "❌ mStock session expired"
- ✅ Error toast is clickable → opens auth modal
- ✅ Forward test button is disabled
- ✅ Forward API returns 403 if attempted
- ✅ User can re-authenticate to restore session

**Test Steps:**
1. Authenticate with broker credentials
2. Wait for session to expire (or simulate expiry)
3. Verify nav pill turns red
4. Verify error toast appears
5. Attempt to start forward test
6. Verify button is disabled (client-side)
7. Verify API returns 403 (server-side)
8. Click error toast → verify auth modal opens
9. Re-authenticate
10. Verify session is restored
11. Verify forward test can start again

### Scenario 4: Expiry Warning Debounce

**Setup:**
- Session is in "expiring_soon" state
- Multiple status polls occur

**Expected Behavior:**
- ✅ Warning toast appears ONCE when first entering expiring_soon state
- ✅ Subsequent polls while in expiring_soon state do NOT trigger new toasts
- ✅ If session returns to "authenticated" state, debounce resets
- ✅ If session enters expiring_soon again, warning toast appears again

**Test Steps:**
1. Authenticate and enter expiring_soon state
2. Verify warning toast appears
3. Dismiss the toast
4. Wait for 3-4 poll cycles
5. Verify no new warning toasts appear
6. Re-authenticate (session returns to authenticated)
7. Enter expiring_soon state again
8. Verify warning toast appears again (debounce reset)

## Automated Test

Run the automated test:
```bash
pytest tests/test_broker_expiry.py -v
```

This test verifies:
- ✅ Session state transitions (authenticated → expiring_soon → expired)
- ✅ Toast notification triggers at correct thresholds
- ✅ Debounce mechanism prevents toast spam
- ✅ Re-authentication clears expiry state

## Manual Verification Checklist

- [ ] Nav pill color changes correctly (🟢 → 🟡 → 🔴)
- [ ] Warning toast appears when session < 10 minutes
- [ ] Error toast appears when session expires
- [ ] Toasts are clickable and open auth modal
- [ ] Toasts auto-dismiss after 10 seconds
- [ ] Debounce prevents duplicate warnings
- [ ] Forward test gate responds to session state
- [ ] Re-authentication restores full functionality
- [ ] No console errors during state transitions

## Implementation Details

**Key Components:**
- `BrokerSessionManager.get_status()` — returns session state with expiry time
- `broker_status.js` — polls status and manages UI state
- Toast system — displays warnings/errors with auto-dismiss
- Debounce logic — prevents duplicate warnings

**State Machine:**
```
unauthenticated → authenticated → expiring_soon → expired
      ↑                                              ↓
      └──────────── re-authenticate ←────────────────┘
```

**Thresholds:**
- Warning threshold: 10 minutes remaining
- Poll interval: 60 seconds
- Toast duration: 10 seconds

## Success Criteria

- ✅ All automated tests pass
- ✅ Manual verification checklist complete
- ✅ No console errors during testing
- ✅ Toast notifications appear at correct times
- ✅ Debounce mechanism works correctly
- ✅ Re-authentication flow works after expiry
- ✅ Forward test gate responds correctly to all states
