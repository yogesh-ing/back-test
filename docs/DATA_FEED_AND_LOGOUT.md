# Data Feed & Logout Behavior - Explanation & Fixes

## Current Behavior (as of 2026-09-30)

### What Happens When You Logout

When you click "Logout" in the broker panel:

1. ✅ **Broker session token is cleared** - Can't place orders anymore
2. ✅ **Authentication state is reset** - Need to login again for trading
3. ❌ **Background data fetch continues** - Still downloading historical data
4. ❌ **No visible stop button** - Stop button exists but is hidden when not running

### Why This Happens

The system has **two separate systems**:

```
┌─────────────────────────────────────┐
│  Broker Authentication System       │
│  - Login/Logout                     │
│  - Session tokens                   │
│  - Order placement                  │
│  - Cleared on logout ✓              │
└─────────────────────────────────────┘

┌─────────────────────────────────────┐
│  Data Fetch System (Background)     │
│  - Runs in separate thread          │
│  - Independent of auth              │
│  - Continues after logout           │
│  - Has its own stop mechanism       │
└─────────────────────────────────────┘
```

## Broker Session Lifecycle (PR #41)

The mStock session has behaviours beyond the logout button:

- **Login is per-process.** The session token is cached per Flask process
  (`.mstock_session_token`, see `src/backtest/live/auth.py`); a second process
  or worker does not share the session.
- **The mobile app can kill the API session.** Logging in on the mStock phone
  app invalidates the web session without the server knowing — the next broker
  call fails even though the UI still looks logged in.
- **Expiry is detected, not assumed.** `src/backtest/brokers/session_manager.py`
  runs a 5-minute monitor (`MONITOR_INTERVAL_SECONDS = 300.0`) that classifies
  the session as expiring-soon or expired and publishes platform alerts
  (`broker_session_expiring` / `broker_session_expired`).
- **The operator is told three ways:** the in-app alert widget on every page,
  Telegram (outbound alert channels), and a re-login popup —
  `src/backtest/web/static/js/components/alert_widget.js` surfaces
  `SESSION_ALERTS` and wires the "re-login" button.

## How to Stop a Running Data Fetch

### Method 1: Use the Stop Button (Already Exists!)

1. Go to **DATA tab** at `http://127.0.0.1:5000/data`
2. Look for the **"⏹ Stop"** button (red, below "Start Fetch")
3. Click it - the fetch will stop after completing the current symbol
4. You'll see: "Stopping after current symbol..." message

**Note:** The stop button only appears when a fetch is running. If you don't see it, no fetch is currently active.

### Method 2: API Call

```bash
curl -X POST http://127.0.0.1:5000/api/data/stop
```

### Method 3: Check Status First

```bash
# See if anything is running
curl http://127.0.0.1:5000/api/data/status

# Response shows:
{
  "status": "running",    # or "idle", "done", "error"
  "symbol": "RELIANCE",   # current symbol being fetched
  "fetched": 45,
  "total": 200,
  ...
}
```

## Problem: Stop Button Not Visible

If you can't see the stop button on the DATA page, there might be a UI issue. Let me check and fix this.

## Solution: Make Stop More Visible

I'll add:
1. A persistent "Stop Data Fetch" button in the header
2. Better status indicators
3. Warning when trying to logout with active fetch

---

## Implementation Plan

### 1. Add Global Stop Indicator to Header

Add a small badge/button in the main navigation that shows if any data fetch is running, accessible from any page.

### 2. Warn Before Logout if Fetch Active

When user clicks logout, check if a data fetch is running and warn them:
- "A data fetch is still running (45/200 symbols). It will continue in the background."
- Offer to stop it first

### 3. Show Stop Button More Prominently

Make the stop button always visible when a job is running, not just on the DATA page.

---

## Quick Fix Right Now

To immediately stop your current data fetch:

**Option A - Via Browser:**
1. Open `http://127.0.0.1:5000/data`
2. Press F12 to open DevTools → Console
3. Paste this:
```javascript
fetch('/api/data/stop', {method: 'POST'})
  .then(r => r.json())
  .then(d => console.log('Stop result:', d))
```
4. Press Enter

**Option B - Via Command Line:**
```bash
curl -X POST http://127.0.0.1:5000/api/data/stop
```

**Option C - Check what's running first:**
```bash
curl http://127.0.0.1:5000/api/data/status
```

---

## Files Involved

| File | Purpose |
|------|---------|
| `src/backtest/api/data_manager.py` | Backend API for fetch/stop |
| `src/backtest/web/static/js/data_manager.js` | Frontend logic |
| `src/backtest/web/templates/data_manager.html` | UI template |
| `src/backtest/brokers/session_manager.py` | Broker logout (separate system) |

---

## Recommended Improvements

1. **Add global fetch status** - Small indicator in navbar showing active fetches
2. **Persist stop button visibility** - Don't hide it once shown
3. **Logout warning** - Alert user if background tasks are running
4. **Kill all background tasks on logout** - Optional setting

Would you like me to implement any of these?
