# Answers Confirmed — Summary Before Epic Draft

Let me reflect back everything cleanly before writing the epic.

---

## Confirmed Decisions

```
Deployment      : Local now → AWS/GCP later
                  HTTPS will be mandatory at deployment stage
                  We note this but don't block on it now

TOTP            : Manual entry from authenticator app
                  User opens Google Authenticator → types 6 digits

Session Expiry  : Warn the user (yellow indicator → red when expired)
                  Not silent failure

Forward Guard   : Start button DISABLED until mStock authenticated
                  No more confusion with synthetic/dummy data running

Multi-broker    : mStock only now
                  BUT build the auth layer generically
                  Broker = "mStock" is just one implementation
                  Easy to add Zerodha/Upstox later by adding new broker class

API Keys        : Stay in .env file
                  NOT in the UI
                  UI only handles Username + Password + TOTP

Live Trading    : OUT OF SCOPE for this epic
                  Noted as future milestone

Multi-strategy  
Forward Testing : OUT OF SCOPE for this epic
                  Noted as future milestone

This Epic Scope : UI Authentication flow for mStock ONLY
```

---

## Pending Items Log (For Future Milestones)

```
┌─────────────────────────────────────────────────────────┐
│                  FUTURE MILESTONE TRACKER               │
├──────────┬──────────────────────────────────────────────┤
│ M3-M5    │ Add Zerodha broker auth                      │
│ M3-M5    │ Add Upstox broker auth                       │
│ M3-M5    │ Add other brokers via generic broker layer   │
├──────────┼──────────────────────────────────────────────┤
│ Post FT  │ Live trading integration via mStock API      │
│ Post FT  │ Multi-strategy simultaneous forward testing  │
├──────────┼──────────────────────────────────────────────┤
│ Deploy   │ HTTPS enforcement (mandatory before AWS/GCP) │
│ Deploy   │ Secure credential transit review             │
└──────────┴──────────────────────────────────────────────┘
```

---

# Epic: mStock Authentication UI

## Epic Summary

> Add a broker authentication layer to the UI that allows users to securely authenticate with mStock via a two-step flow (Credentials → TOTP). The session established is then used exclusively by the Forward Testing engine. The Forward Test Start button remains disabled until a valid authenticated session exists. The architecture is broker-agnostic to support future broker additions.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                     GENERIC BROKER LAYER                    │
│                                                             │
│   BrokerAuthBase (Abstract)                                 │
│   ├── login(username, password) → temp_token                │
│   ├── verify_totp(totp_code)   → session_token             │
│   ├── get_status()             → {valid, expires_at}       │
│   └── logout()                                             │
│                                                             │
│   MStockBroker(BrokerAuthBase)  ← Only implementation now  │
│   ZerodhaBroker(BrokerAuthBase) ← Future                   │
│   UpstoxBroker(BrokerAuthBase)  ← Future                   │
└──────────────────────────┬──────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│                    SESSION STORE (Backend)                  │
│                                                             │
│   {                                                         │
│     "broker": "mstock",                                     │
│     "session_token": "xxx",   ← never sent to browser      │
│     "expires_at": timestamp,                                │
│     "status": "authenticated"                               │
│   }                                                         │
└──────────────────────────┬──────────────────────────────────┘
                           │
              ┌────────────┴────────────┐
              ▼                         ▼
┌─────────────────────┐     ┌───────────────────────┐
│   Auth Status API   │     │  Forward Engine        │
│   GET /api/broker/  │     │  Uses session_token    │
│   status            │     │  internally            │
│                     │     │  Never exposes to UI   │
└─────────────────────┘     └───────────────────────┘
              │
              ▼
┌─────────────────────────────────────────────────────────────┐
│                         UI LAYER                            │
│                                                             │
│  Nav Icon: 🔴/🟡/🟢 mStock                                  │
│  Auth Popup: Username + Password → TOTP                     │
│  Forward Page: Start button gated on auth status            │
└─────────────────────────────────────────────────────────────┘
```

---

## Session State Machine

```
┌─────────────────────────────────────────────────────────────┐
│                    SESSION STATES                           │
│                                                             │
│   UNAUTHENTICATED                                           │
│   │  Nav Icon: 🔴                                           │
│   │  Forward Start: DISABLED                                │
│   │  Popup: Shows login form                                │
│   ↓                                                         │
│   CREDENTIALS_VERIFIED (temp state)                         │
│   │  Nav Icon: 🟡 (Awaiting TOTP)                           │
│   │  Forward Start: DISABLED                                │
│   │  Popup: TOTP field enabled                              │
│   ↓                                                         │
│   AUTHENTICATED                                             │
│   │  Nav Icon: 🟢                                           │
│   │  Forward Start: ENABLED                                 │
│   │  Popup: Shows session info + logout                     │
│   ↓                                                         │
│   EXPIRING_SOON (within 30 min of expiry)                   │
│   │  Nav Icon: 🟡 Warning                                   │
│   │  Forward Start: ENABLED (still valid)                   │
│   │  Toast warning shown to user                            │
│   ↓                                                         │
│   EXPIRED                                                   │
│      Nav Icon: 🔴                                           │
│      Forward Start: DISABLED                                │
│      Toast: "Session expired, please re-authenticate"       │
└─────────────────────────────────────────────────────────────┘
```

---

## Task Decomposition

---

### Phase 1: Generic Broker Auth Backend Layer

#### Task 1.1: Create `BrokerAuthBase` Abstract Class
* **File**: `brokers/base.py`
* **Action**: Define the generic interface all brokers must implement.
* **Implementation**:
  ```python
  from abc import ABC, abstractmethod

  class BrokerAuthBase(ABC):

      broker_name: str = "unnamed"
      broker_display_name: str = "Unknown Broker"

      @abstractmethod
      def login(self, username: str, password: str) -> dict:
          """
          Step 1 auth. Returns:
          {"success": bool, "message": str, "requires_totp": bool}
          """
          pass

      @abstractmethod
      def verify_totp(self, totp_code: str) -> dict:
          """
          Step 2 auth. Returns:
          {"success": bool, "message": str, "expires_at": str}
          """
          pass

      @abstractmethod
      def get_session_status(self) -> dict:
          """
          Returns:
          {"status": str, "expires_at": str | None, "broker": str}
          status: unauthenticated | authenticated | expiring_soon | expired
          """
          pass

      @abstractmethod
      def logout(self) -> None:
          pass
  ```
* **Verification**: Confirm abstract methods cannot be skipped by subclass without raising `TypeError`.

---

#### Task 1.2: Implement `MStockBroker` Class
* **File**: `brokers/mstock.py`
* **Action**: Implement the mStock-specific authentication flow using the mStock API. API credentials (API key etc.) loaded from `.env` via `python-dotenv`. Username and password are received at runtime from the UI — never stored.
* **State held in-memory only**:
  ```python
  class MStockBroker(BrokerAuthBase):
      broker_name = "mstock"
      broker_display_name = "mStock"

      def __init__(self):
          self._session_token = None
          self._expires_at = None
          self._temp_auth_context = None  # holds intermediate state between step 1 and step 2
  ```
* **Key behaviours**:
  * `login()`: Calls mStock credentials endpoint. On success stores temp auth context. Does NOT store username/password after call completes.
  * `verify_totp()`: Uses temp context + TOTP to finalize session. Stores `session_token` and `expires_at` in memory. Clears temp context.
  * `get_session_status()`: Computes status by comparing current time against `expires_at`. Returns `expiring_soon` if within 30 minutes.
  * `logout()`: Clears all in-memory session state.
* **Verification**: Unit test mock flow — login → verify_totp → get_session_status returns `authenticated`.

---

#### Task 1.3: Create `BrokerSessionManager` Singleton
* **File**: `brokers/session_manager.py`
* **Action**: Central registry that holds the active broker instance and exposes it to the Forward Engine and API routes.
* **Responsibilities**:
  * Holds single active broker instance.
  * Exposes `get_active_session_token()` for Forward Engine use.
  * Exposes `get_status()` for API polling.
  * Is the only component that ever touches the raw session token.
* **Verification**: Confirm Forward Engine can retrieve session token without any direct dependency on `MStockBroker` class.

---

### Phase 2: Authentication API Endpoints

#### Task 2.1: Implement Auth API Routes
* **File**: `dashboard/routes/broker_auth.py`
* **Endpoints**:

  ```
  POST /api/broker/login
  Body: { "username": str, "password": str }
  Response: { "success": bool, "message": str, "requires_totp": bool }
  Note: Password is used in this request and immediately discarded

  POST /api/broker/verify-totp
  Body: { "totp_code": str }
  Response: { "success": bool, "message": str, "expires_at": str }

  GET /api/broker/status
  Response: {
    "status": "unauthenticated|authenticated|expiring_soon|expired",
    "broker": "mstock",
    "broker_display_name": "mStock",
    "expires_at": str | null
  }

  POST /api/broker/logout
  Response: { "success": bool }
  ```

* **Security Notes**:
  * Password never logged, never stored, never returned in any response.
  * Session token never included in any API response to the browser.
  * All endpoints return generic error messages (no stack traces to browser).

* **Verification**: Test each endpoint independently. Confirm session token is absent from all response payloads.

---

#### Task 2.2: Session Expiry Background Monitor
* **File**: `brokers/session_manager.py`
* **Action**: Add a lightweight background thread that polls session expiry every 5 minutes.
* **Behaviour**:
  * If session transitions to `expiring_soon` → sets an internal flag.
  * If session transitions to `expired` → clears session token, updates status.
  * Does NOT auto-renew (user must re-authenticate manually).
* **Verification**: Unit test with a mock session set to expire in 25 minutes. Confirm status transitions to `expiring_soon` correctly.

---

### Phase 3: Authentication UI Components

#### Task 3.1: Add Broker Status Icon to Navigation Bar
* **Files**: `dashboard/templates/base.html`, `dashboard/static/js/broker_status.js`
* **Action**: Add persistent broker connection indicator to the nav bar.
* **Behaviour**:
  ```
  🔴 mStock  → Unauthenticated or Expired (clickable → opens auth popup)
  🟡 mStock  → Expiring Soon (clickable → opens auth popup for re-auth)
  🟢 mStock  → Authenticated (clickable → opens session info popup)
  ```
* **Polling**: `broker_status.js` polls `GET /api/broker/status` every 60 seconds and updates the icon colour and tooltip dynamically.
* **Verification**: Manually trigger each status state via API and confirm icon updates without page reload.

---

#### Task 3.2: Build Authentication Popup Modal
* **Files**: `dashboard/templates/components/broker_auth_modal.html`, `dashboard/static/js/broker_auth_modal.js`
* **Action**: Build the two-step auth modal.

```
STEP 1 VIEW (Initial State)
┌─────────────────────────────────────────┐
│  🔐 mStock Login                    [×] │
├─────────────────────────────────────────┤
│                                         │
│  Username  [_________________________]  │
│  Password  [_________________________]  │
│                                         │
│            [ Login ]                    │
│                                         │
│  ─────────────────────────────────────  │
│                                         │
│  TOTP Verification                      │
│  [ Enter 6-digit code ] ← DISABLED 🔒   │
│            [ Continue ] ← DISABLED      │
│                                         │
└─────────────────────────────────────────┘

STEP 2 VIEW (After Credential Success)
┌─────────────────────────────────────────┐
│  🔐 mStock Login                    [×] │
├─────────────────────────────────────────┤
│                                         │
│  ✅ Credentials verified                │
│                                         │
│  ─────────────────────────────────────  │
│                                         │
│  TOTP Verification                      │
│  [ Enter 6-digit code ] ← ENABLED ✅    │
│            [ Continue ] ← ENABLED       │
│                                         │
└─────────────────────────────────────────┘

AUTHENTICATED VIEW
┌─────────────────────────────────────────┐
│  🟢 mStock Connected                [×] │
├─────────────────────────────────────────┤
│                                         │
│  Status    : Authenticated              │
│  Expires At: 03:45 PM today             │
│  Broker    : mStock                     │
│                                         │
│            [ Logout ]                   │
│                                         │
└─────────────────────────────────────────┘
```

* **UX Rules**:
  * Login button shows spinner during API call.
  * Error messages shown inline (wrong credentials, invalid TOTP).
  * TOTP field auto-focuses after credential success.
  * Modal cannot be dismissed mid-flow (only via [×] which cancels entire flow).
  * Password field cleared from DOM immediately after Login click.

---

#### Task 3.3: Session Expiry Toast Notification
* **Files**: `dashboard/static/js/broker_status.js`
* **Action**: Trigger a non-blocking toast notification when session transitions to `expiring_soon`.
* **Toast Content**:
  ```
  ⚠️  mStock session expiring in 30 minutes.
      Click here to re-authenticate.
  ```
* **Behaviour**:
  * Toast appears once per expiry cycle (not every poll).
  * Clicking toast opens auth popup.
  * Toast auto-dismisses after 10 seconds.
* **Verification**: Mock `expiring_soon` status and confirm toast fires once, not repeatedly.

---

### Phase 4: Forward Test Page Guard

#### Task 4.1: Gate Forward Test Start Button on Auth Status
* **Files**: `dashboard/templates/forward.html`, `dashboard/static/js/forward.js`
* **Action**: Forward Test "Start Engine" button is controlled by broker auth status.
* **Behaviour**:
  ```
  Status = unauthenticated / expired:
    Button: [ 🔴 Connect mStock to Start ] ← disabled, greyed out
    Tooltip: "Authentication required before starting forward test"
    Clicking button → opens auth modal directly

  Status = authenticated / expiring_soon:
    Button: [ ▶ Start Forward Test ] ← active, enabled
  ```
* **Implementation**: On page load and on each status poll, `forward.js` reads broker status and updates button state accordingly.
* **Verification**: Confirm button cannot be activated via browser console JS injection (server-side guard on `/api/forward/start` also checks session validity).

---

#### Task 4.2: Server-Side Forward Start Guard
* **File**: `dashboard/routes/broker_auth.py`
* **Action**: `POST /api/forward/start` checks `BrokerSessionManager.get_status()` before allowing engine to start.
* **Response if not authenticated**:
  ```json
  {
    "success": false,
    "error": "broker_not_authenticated",
    "message": "Valid broker session required to start forward test"
  }
  ```
* **Verification**: Attempt to call `/api/forward/start` without authentication. Confirm 403 response with correct error code.

---

### Phase 5: Integration & Verification

#### Task 5.1: Full Authentication Flow Test
* **Action**: End-to-end manual walkthrough:
  1. Open app → Confirm nav shows 🔴 mStock.
  2. Confirm Forward Test Start button is disabled.
  3. Click mStock icon → Confirm modal opens at Step 1.
  4. Enter credentials → Click Login → Confirm TOTP field enables.
  5. Enter TOTP → Click Continue → Confirm nav shows 🟢 mStock.
  6. Confirm Forward Test Start button is now enabled.
  7. Click Logout → Confirm nav returns to 🔴 and Start button disables.

#### Task 5.2: Session Expiry Warning Test
* **Action**: Mock session to expire in 20 minutes. Confirm:
  1. Nav icon transitions from 🟢 to 🟡.
  2. Toast notification appears once.
  3. Clicking toast opens re-auth modal.
  4. After mock expiry → nav shows 🔴, Start button disables.

#### Task 5.3: Security Verification Checklist
```
[ ] Session token never appears in browser network tab responses
[ ] Password field cleared from DOM after Login click
[ ] No credentials or tokens written to any log file
[ ] /api/forward/start returns 403 without valid session
[ ] Re-authentication flow works cleanly after expiry
[ ] HTTPS noted as mandatory before any cloud deployment
```

---

## Pending Items Added to Future Milestone Log

```
┌─────────────────────────────────────────────────────────┐
│              UPDATED FUTURE MILESTONE TRACKER           │
├──────────┬──────────────────────────────────────────────┤
│ M3-M5    │ Add Zerodha broker auth (generic layer ready)│
│ M3-M5    │ Add Upstox broker auth (generic layer ready) │
│ M3-M5    │ Broker selector UI (if multiple brokers)     │
├──────────┼──────────────────────────────────────────────┤
│ Post FT  │ Live trading integration via mStock API      │
│ Post FT  │ Multi-strategy simultaneous forward testing  │
│ Post FT  │ TOTP auto-generation (secret stored securely)│
├──────────┼──────────────────────────────────────────────┤
│ Deploy   │ HTTPS enforcement (mandatory for AWS/GCP)    │
│ Deploy   │ Secure credential transit audit              │
│ Deploy   │ Session token encryption at rest             │
└──────────┴──────────────────────────────────────────────┘
```

---

Ready to begin implementation starting with **Phase 1: Generic Broker Auth Backend Layer**?