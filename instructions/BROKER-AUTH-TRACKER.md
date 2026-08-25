# Broker Auth Epic — Task Tracker & Handoff

Tracks `instructions/Generic_Broker_Authentication.md` (mStock
Authentication UI epic). Work happens on branch `arena/01a03995-back-test`.

**Last updated:** 2026-08-25 · **Progress: 7 / 13 tasks complete (Phases 1–2 done, Phase 3 nearly done)**

```
Phase 1 ✅██████████████████████ Generic broker backend layer (1.1 1.2 1.3)
Phase 2 ✅██████████████████████ Auth API endpoints (2.1 2.2)
Phase 3 ██████████░░░░░░░░░░░░░  3.1 ✅  3.3 ✅  ·  3.2 ⬜ (auth modal)
Phase 4 ░░░░░░░░░░░░░░░░░░░░░░░░ 4.1 ⬜  4.2 ⬜ (forward start guard)
Phase 5 ░░░░░░░░░░░░░░░░░░░░░░░░ E2E walkthrough + security checklist
```

---

## Task status

### Phase 1 — Generic Broker Auth Backend Layer ✅ COMPLETE

| # | Task | Deliverable | Status | Commit |
|---|------|-------------|--------|--------|
| 1.1 | `BrokerAuthBase` abstract class | `src/backtest/brokers/base.py` + package `__init__.py` (status constants `SESSION_STATUSES`) | ✅ | `cfeef91` |
| 1.2 | `MStockBroker` two-step auth | `src/backtest/brokers/mstock.py` | ✅ | `c6dd56a` |
| 1.3 | `BrokerSessionManager` singleton | `src/backtest/brokers/session_manager.py` | ✅ | `fb969f4` |

### Phase 2 — Authentication API Endpoints ✅ COMPLETE

| # | Task | Deliverable | Status | Commit |
|---|------|-------------|--------|--------|
| 2.1 | Auth API routes | `src/backtest/api/broker_auth.py` (mounted in `web/app.py`) | ✅ | `085faef` |
| 2.2 | Session expiry background monitor | `src/backtest/brokers/session_manager.py` (5-min daemon thread) | ✅ | `fb969f4` |

### Phase 3 — Authentication UI Components 🟡 IN PROGRESS

| # | Task | Deliverable | Status | Commit |
|---|------|-------------|--------|--------|
| 3.1 | Nav broker status icon | `web/templates/base.html` + `web/static/js/broker_status.js` | ✅ | `4792a19` |
| 3.2 | Auth popup modal | `web/templates/components/broker_auth_modal.html` + `web/static/js/broker_auth_modal.js` | ⬜ **NEXT** | — |
| 3.3 | Session expiry toast | `web/static/js/broker_status.js` | ✅ | `4792a19` |

### Phase 4 — Forward Test Page Guard ⬜ PENDING

| # | Task | Deliverable | Status |
|---|------|-------------|--------|
| 4.1 | Gate Forward Start button on auth status | `web/templates/forward.html` + `web/static/js/forward.js` | ⬜ |
| 4.2 | Server-side guard on `/api/forward/start` | `src/backtest/api/forward.py` (or `broker_auth.py`) | ⬜ |

### Phase 5 — Integration & Verification ⬜ PENDING

| # | Task | Status |
|---|------|--------|
| 5.1 | Full auth flow manual walkthrough (7 steps) | ⬜ needs Task 3.2 |
| 5.2 | Session expiry warning test (mock 20-min expiry) | ⬜ needs 3.2 (toast+nav already covered by tests) |
| 5.3 | Security verification checklist | ⬜ partial (see below) |

---

## What exists already (do not rebuild)

**Backend (all tested, no browser needed):**

* `backtest.brokers` package: `BrokerAuthBase` ABC → `MStockBroker`
  (TypeA endpoints `/openapi/typea/connect/login` +
  `/openapi/typea/session/verifytotp`, in-memory state only, temp context
  cleared on success / kept on wrong TOTP for retry) →
  `BrokerSessionManager` singleton (`get_session_manager()`) with
  `login/verify_totp/logout/get_status/is_authenticated/
  get_active_session_token` + 5-min expiry monitor daemon that flags
  `expiring_soon` once per cycle and clears expired tokens (no auto-renew).
* API: `POST /api/broker/login | verify-totp | logout`, `GET /api/broker/status`
  — generic errors only, token never in responses, status fails closed.
  `create_app()` auto-starts the monitor.
* Env knobs (`.env.example`): `MSTOCK_API_KEY`, `MSTOCK_BASE_URL`,
  `MSTOCK_SESSION_TTL_MINUTES` (default 390 = trading session).

**Frontend (Tasks 3.1 + 3.3, tested via Node harness):**

* Nav pill `#broker-status` on every page (dot `#broker-status-dot` +
  name `#broker-status-name`), 🔴/🟡/🟢/⚪ with tooltips.
* `BrokerStatus` global: `get()` / `state()` / `refresh()` /
  `expectLogout()`; polls every 60 s; dispatches document event
  **`broker:status`** with the status payload on every poll.

**Integration hooks already built for the pending tasks:**

| Hook | Consumer |
|---|---|
| `window.BrokerAuthUI.open()` — guarded click target on the nav pill + toasts | Task 3.2 modal must register itself as `window.BrokerAuthUI = { open }` |
| `BrokerStatus.refresh()` — force immediate poll after login/logout | Task 3.2 calls it after every state change |
| `BrokerStatus.expectLogout()` — suppress "session expired" toast | Task 3.2 calls it right before `POST /api/broker/logout` |
| `broker:status` document event + `BrokerStatus.state()` | Task 4.1 `forward.js` gate |
| `get_session_manager().is_authenticated()` | Task 4.2 server-side guard |

---

## Pick-up instructions for remaining tasks

### Task 3.2 — Auth popup modal (next)

Files: `src/backtest/web/templates/components/broker_auth_modal.html`,
`src/backtest/web/static/js/broker_auth_modal.js` (include both in
`base.html` after `broker_status.js`).

Three views per the PRD (single modal, swap sections):

1. **STEP 1**: username + password fields + Login button; TOTP section
   present but disabled with 🔒. Spinner on the button during the call;
   inline error text on failure; **clear the password input from the DOM
   immediately after the Login click**.
2. **STEP 2** (after `requires_totp: true`): "✅ Credentials verified",
   TOTP input + Continue enabled, **auto-focus the TOTP field**.
   Wrong code → inline error, field stays enabled (backend keeps temp
   context for retry). `[×]` closes and cancels the whole flow.
3. **AUTHENTICATED**: status / expires-at (`BrokerStatus.get().expires_at`,
   format like 03:45 PM) / broker name + Logout button.

Calls: `POST /api/broker/login`, `POST /api/broker/verify-totp`,
`POST /api/broker/logout`. After any success/logout call
`BrokerStatus.refresh()`; before logout call `BrokerStatus.expectLogout()`.
On open, seed the view from `BrokerStatus.state()`. Register
`window.BrokerAuthUI = { open: ... }`.

Verify: extend `tests/test_broker_ui.py` (markup on every page) and add
`tests/js/test_broker_auth_modal.mjs` modelled on
`tests/js/test_broker_status.mjs` (stub fetch + DOM; assert view
transitions, password field cleared, error paths, logout wiring).

### Task 4.1 — Forward start button gate

`forward.js`: on load + on every `broker:status` event, set Start button:
disabled + "🔴 Connect mStock to Start" + tooltip when
`unauthenticated|expired` (click opens `BrokerAuthUI.open()`); enabled +
"▶ Start Forward Test" when `authenticated|expiring_soon`.

### Task 4.2 — Server-side forward start guard

In `api/forward.py::start`: check `get_session_manager().is_authenticated()`
first; else return 403
`{"success": false, "error": "broker_not_authenticated", "message": ...}`.
Add tests to `tests/test_api_forward.py` (and make sure existing forward
tests inject a stub authenticated broker or are updated for the 403).

### Phase 5 — verification

* 5.1/5.2 manual walkthrough against a live preview
  (`PYTHONPATH=src python -m backtest.web.app --host 0.0.0.0 --port 5000`);
  🟡/🟢 need real mStock credentials in `.env` — otherwise flip states by
  injecting a stub broker via `get_session_manager().set_broker(...)`.
* 5.3 checklist — items already verified by automated tests: token never in
  responses (`test_api_broker_auth.py`), password never returned, no
  credentials in logs (broker only logs outcomes), 403 guard = Task 4.2.
  Remaining manual items: browser network-tab eyeball, HTTPS note for
  deployment (already on the future-milestone list).

---

## How to run the verification suite

```bash
# venv at /home/user/.venv (pip install -r requirements.txt once)
cd /home/user/back-test
PYTHONPATH=src .venv/bin/python -m pytest tests/ -q -k "not live"

# Broker-auth additions
PYTHONPATH=src .venv/bin/python -m pytest tests/test_broker_base.py \
    tests/test_broker_mstock.py tests/test_broker_session_manager.py \
    tests/test_api_broker_auth.py tests/test_broker_ui.py -v
node tests/js/test_broker_status.mjs
```

**Current totals:** 1677 passed / 3 skipped / 1 failed — the failure is
pre-existing and unrelated (`test_mstock_auth::test_login_sends_sdk_headers`
needs `MSTOCK_API_KEY` in the environment; documented in TASK-TRACKER.md).

Per-task test counts: 1.1 → 13, 1.2 → 28, 1.3+2.2 → 20, 2.1 → 24,
3.1+3.3 → 7 pytest + 12 node.

## Conventions / deviations from the PRD

* PRD top-level paths map into the repo layout: `brokers/…` →
  `src/backtest/brokers/…`, `dashboard/routes/…` → `src/backtest/api/…`,
  `dashboard/templates|static/…` → `src/backtest/web/templates|static/…`
  (same deviation as PRD V1, recorded in TASK-TRACKER.md).
* Task 2.2 was implemented together with 1.3 (same file), and 3.3 with 3.1
  (same file) — both noted above.
* Frontend has no build step: plain JS IIFEs matching
  `session_state.js`/`toast.js` style; JS behaviour tested via Node
  harnesses under `tests/js/` (skipped automatically if node is missing).
