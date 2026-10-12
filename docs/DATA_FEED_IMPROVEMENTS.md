# Data Feed Improvements - Implementation Summary

## Problem Solved

**Issue:** Background data fetch continues after logout with no visible way to stop it.

**Root Cause:** 
- Data fetch runs in a separate thread independent of broker authentication
- Logout only clears session tokens, doesn't affect background tasks
- Stop button existed but was hidden when not on DATA page

## Solution Implemented

### 1. Global Data Fetch Indicator (Navbar) ✅

**What:** A persistent badge in the top navigation bar that shows when any data fetch is running.

**Features:**
- Visible from ANY page (not just DATA tab)
- Shows progress count (e.g., "45/200")
- Animated spinner icon
- Click to stop the fetch immediately
- Color changes on hover (orange → red)
- Auto-hides when fetch completes

**Location:** Top right of navbar, next to broker status button

**Files Modified:**
- `src/backtest/web/templates/base.html` - Added indicator HTML
- `src/backtest/web/static/css/app.css` - Added styling and animations
- `src/backtest/web/static/js/data_fetch_indicator.js` - New file for indicator logic

---

### 2. Logout Warning Dialog ✅

**What:** When clicking "Logout", system checks if a data fetch is running and warns the user.

**Behavior:**
```
If fetch is running:
  → Show dialog: "Data fetch is running (45/200 symbols). Continue in background?"
  → User options:
    - Cancel: Abort logout, keep fetch running
    - OK: Stop the fetch first, then user can logout
    
If no fetch running:
  → Normal logout flow
```

**Files Modified:**
- `src/backtest/web/static/js/broker_auth_modal.js` - Added pre-logout check

---

### 3. Existing Stop Mechanism (Already Working) ✅

The system already had these endpoints:

**Backend API:**
- `POST /api/data/stop` - Signals running job to stop after current symbol
- `GET /api/data/status` - Returns current job status

**Frontend (DATA page):**
- Stop button appears when fetch is running
- Shows "Stopping after current symbol..." message
- Polls every 2 seconds for status updates

**Files:**
- `src/backtest/api/data_manager.py` (lines 89-96)
- `src/backtest/web/static/js/data_manager.js` (line 54-61)
- `src/backtest/web/templates/data_manager.html` (line 46)

---

## Later Improvements to the Fetch Pipeline (PRs #39, #40, #42)

After the logout/stop work, the Data tab fetch itself was hardened
(`src/backtest/api/data_manager.py`):

- **Names-only curated picker** — `FETCH_SCOPES = ("equity,index", "equity",
  "index")`: the form picks from curated universes (NIFTY 200 stock list,
  16 indices, or both), not free-form symbols.
- **Dedupe on persist** — bars are upserted (last occurrence wins), so
  re-fetching a window cannot double-store it.
- **502/503/504 retry ladder** — `_get_historical_with_retry()` retries
  mStock's intermittent gateway 502s with backoff, and a circuit breaker
  (`BREAKER_MIN_FAILURES = 8` post-retry chunk failures within
  `BREAKER_STALL_SECONDS = 180.0` seconds, `CHUNK_FETCH_WORKERS = 4`) aborts
  the job instead of crawling through all 200 symbols collecting errors.
- **Coverage-aware, holiday-aware skip** — `_windows_needing_fetch()` probes
  `_probe_covered_days` first and only fetches missing windows;
  `_load_market_holidays()` reads the `market_holidays` table (seeded
  2022–2026 from the NSE holiday-master API) so holiday-only windows are
  dropped on the first pass instead of re-probed forever.
- **End-exclusive `to` fix** — mStock's historical `to` date is END-EXCLUSIVE,
  so fetches request one day past the wanted window; a single-day fetch used
  to come back empty.

---

## How to Use

### Stopping a Data Fetch

**Method 1: Navbar Indicator (NEW)**
1. Look for orange badge in top-right corner showing "⟳ 45/200"
2. Click it
3. Confirm the stop dialog
4. Fetch will complete current symbol then stop

**Method 2: DATA Page**
1. Go to `/data` page
2. Red "⏹ Stop" button will be visible
3. Click it

**Method 3: API Call**
```bash
curl -X POST http://127.0.0.1:5000/api/data/stop
```

### Checking Fetch Status

**Via Browser Console:**
```javascript
fetch('/api/data/status').then(r => r.json()).then(console.log)
```

**Via Command Line:**
```bash
curl http://127.0.0.1:5000/api/data/status
```

**Response:**
```json
{
  "status": "running",      // or "idle", "done", "error"
  "symbol": "RELIANCE",     // currently fetching
  "fetched": 45,            // completed
  "total": 200,             // total to fetch
  "bars_total": 12345,      // total bars inserted
  "elapsed": "2m 34s"       // time elapsed
}
```

---

## Testing Checklist

After restarting the server, verify:

- [ ] Server starts without errors
- [ ] Navigate to any page - navbar should load normally
- [ ] Start a data fetch from DATA tab
- [ ] Orange indicator should appear in navbar
- [ ] Click indicator → should show stop confirmation
- [ ] Try logging out while fetch is running → should see warning dialog
- [ ] After stopping fetch → indicator should disappear
- [ ] Check browser console for any JavaScript errors

---

## Files Changed

| File | Changes | Purpose |
|------|---------|---------|
| `base.html` | Added indicator div + script tag | UI structure |
| `app.css` | Added `.data-fetch-indicator` styles | Visual appearance |
| `data_fetch_indicator.js` | NEW FILE | Indicator logic |
| `broker_auth_modal.js` | Modified `handleLogout()` | Pre-logout warning |
| `DATA_FEED_AND_LOGOUT.md` | NEW FILE | User documentation |
| `DATA_FEED_IMPROVEMENTS.md` | NEW FILE | This technical doc |

---

## Architecture Notes

### Why Data Fetch Continues After Logout

The system has two independent subsystems:

```
┌──────────────────────────────────────┐
│  Broker Authentication               │
│  - Session tokens                    │
│  - Login/Logout                      │
│  - Cleared on logout ✓               │
│  - Lifetime: Until explicit logout   │
└──────────────────────────────────────┘

┌──────────────────────────────────────┐
│  Data Fetch Engine                   │
│  - Background thread                 │
│  - Independent of auth               │
│  - Has its own lifecycle             │
│  - Persists across logins/logouts    │
└──────────────────────────────────────┘
```

This design is intentional because:
1. Data fetching is a long-running operation (can take hours for 200 stocks)
2. Users might want to logout but keep downloading data
3. Authentication and data operations are separate concerns

However, the new improvements give users **visibility and control** over this behavior.

---

## Future Enhancements (Optional)

If needed, we could add:

1. **Kill Switch** - Force immediate termination (not graceful stop)
2. **Auto-stop on Logout** - Setting to automatically stop all fetches on logout
3. **Fetch Queue Management** - Pause/resume scheduled fetches
4. **Progress Notifications** - Toast when fetch completes
5. **Batch Control** - Stop/pause multiple fetch jobs

---

## Quick Reference

**To stop a fetch RIGHT NOW:**
```bash
# Terminal
curl -X POST http://127.0.0.1:5000/api/data/stop

# Or browser console
fetch('/api/data/stop', {method: 'POST'})
```

**To check what's running:**
```bash
curl http://127.0.0.1:5000/api/data/status
```

**To see inventory of downloaded data:**
```bash
curl http://127.0.0.1:5000/api/data/inventory
```

---

## Restart Required

After making these changes, you need to restart the Flask server:

```bash
# Find the process
netstat -ano | findstr :5000
# Kill it (replace PID)
taskkill /F /PID <PID>

# Or just Ctrl+C in the terminal where it's running

# Then restart
python src/backtest/web/app.py
```

The changes will take effect immediately after restart.
