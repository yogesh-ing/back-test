# Task 4.2 — Server-Side Forward Start Guard

## Overview

Task 4.2 implements the **server-side authentication guard** for the Forward Test start endpoint. This is the security layer that complements the client-side button gate (Task 4.1), ensuring that forward testing cannot proceed without a valid broker session — even if the UI is bypassed.

## Implementation

### Files Modified

1. **`src/backtest/api/forward.py`**
   - Added import: `from backtest.brokers.session_manager import get_session_manager`
   - Added authentication check at the start of the `/api/forward/start` endpoint
   - Returns HTTP 403 with structured error response if not authenticated
   - The guard runs **before** any other validation (strategy, dates, etc.)

2. **`tests/test_api_forward.py`**
   - Added `Task42StubBroker` class to simulate authenticated broker sessions
   - Added fixture `authenticated_stub_broker` to inject authenticated broker
   - Updated all existing tests to use the authenticated stub broker
   - Added 6 new tests for the authentication guard:
     - `test_start_without_auth_returns_403`
     - `test_start_with_expired_session_returns_403`
     - `test_start_with_authenticated_session_succeeds`
     - `test_start_with_expiring_soon_session_succeeds`
     - `test_start_guard_runs_before_strategy_validation`
     - `test_stop_does_not_require_auth`

3. **`tests/test_e2e_workflow.py`**
   - Updated to inject authenticated broker for tests that call `/api/forward/start`
   - Ensures e2e workflow tests continue to pass with the new guard

### Key Design Decisions

1. **Guard Placement**: The authentication check runs at the very beginning of the endpoint handler, before any input validation. This ensures consistent behavior and prevents information leakage about valid/invalid strategies when not authenticated.

2. **Session Manager**: Uses the existing `BrokerSessionManager` singleton via `get_session_manager().is_authenticated()`. This method returns `True` only for `STATUS_AUTHENTICATED` and `STATUS_EXPIRING_SOON` states.

3. **Response Format**: Returns a structured JSON response with `success`, `error`, and `message` fields, matching the pattern used elsewhere in the auth API.

4. **Test Coverage**: All existing forward tests continue to work by injecting an authenticated broker stub. New tests verify both rejection (403) and acceptance (200) paths.

## Behavior

### Before Task 4.2

```http
POST /api/forward/start
Content-Type: application/json

{
  "strategy": "sma_crossover",
  "symbol": "DEMO",
  "timeframe": "1D",
  "from_date": "2024-01-01",
  "to_date": "2024-12-31",
  "capital": 10000
}

Response: 200 OK (regardless of auth state)
```

### After Task 4.2

#### Without Authentication

```http
POST /api/forward/start
Content-Type: application/json

{...}

Response: 403 Forbidden
{
  "success": false,
  "error": "broker_not_authenticated",
  "message": "Valid broker session required to start forward test"
}
```

#### With Authentication

```http
POST /api/forward/start
Content-Type: application/json

{...}

Response: 200 OK
{
  "status": "running",
  ...
}
```

## Security Model

Task 4.2 completes the **defense-in-depth** security model:

1. **Client-Side Gate (Task 4.1)**: Prevents accidental misuse by disabling the UI button when not authenticated. This is a UX improvement, not a security boundary.

2. **Server-Side Gate (Task 4.2)**: Enforces authentication at the API level. This is the actual security boundary that cannot be bypassed by:
   - Direct API calls (curl, Postman, etc.)
   - Browser console manipulation
   - Modified frontend code
   - Automated scripts

3. **Session Validation**: The guard uses `is_authenticated()` which checks for `STATUS_AUTHENTICATED` or `STATUS_EXPIRING_SOON`. Expired or unauthenticated sessions are rejected.

## Testing

### Test Results

- **Total tests**: 1716 passed, 4 skipped, 1 failed (pre-existing)
- **New tests**: 7 (6 for auth guard, 1 e2e update)
- **No regressions**: All existing forward tests continue to pass

### Test Coverage

The new tests verify:

1. **Unauthenticated rejection**: Request without any broker session → 403
2. **Expired session rejection**: Request with expired broker session → 403
3. **Authenticated acceptance**: Request with valid session → 200
4. **Expiring-soon acceptance**: Request with session nearing expiry → 200 (still valid)
5. **Guard ordering**: Auth check runs before strategy validation → 403 (not 400)
6. **Stop endpoint**: `/api/forward/stop` does NOT require auth (idempotent operation)

## Integration with Phase 4

Task 4.2 completes Phase 4 of the Broker Authentication epic:

- **Task 4.1** (Client-Side Gate): UX improvement, prevents accidental misuse
- **Task 4.2** (Server-Side Gate): Security boundary, prevents unauthorized access

Together, they provide a complete authentication guard for the Forward Test feature.

## Remaining Work

Phase 5 — Integration & Verification is the final phase:

- **Task 5.1**: Full Authentication Flow Test (manual walkthrough)
- **Task 5.2**: Session Expiry Warning Test (mock 20-min expiry)
- **Task 5.3**: Security Verification Checklist

These tasks focus on manual verification and documentation, not new code.

## Success Criteria

✅ Server-side guard prevents unauthenticated forward test starts
✅ Returns 403 with structured error response
✅ Guard runs before other validation (no information leakage)
✅ All existing tests continue to pass
✅ New tests cover all authentication states
✅ No regressions in other functionality

## Notes

- The guard uses the same session manager as the rest of the auth system
- Expiring-soon sessions are still valid (user has time to re-authenticate)
- The stop endpoint does NOT require auth (allows cleanup even after session expires)
- The status endpoint does NOT require auth (allows UI to poll state)
