# Broker Auth Epic — Task Tracker & Handoff

Tracks `instructions/Generic_Broker_Authentication.md` (mStock
Authentication UI epic). Work happens on branch `arena/01a03995-back-test`.

**Last updated:** 2026-08-26 · **Progress: 13 / 13 tasks complete 🎉 ALL PHASES DONE**

```
Phase 1 ✅██████████████████████ Generic broker backend layer (1.1 1.2 1.3)
Phase 2 ✅██████████████████████ Auth API endpoints (2.1 2.2)
Phase 3 ✅██████████████████████ Auth UI components (3.1 3.2 3.3)
Phase 4 ✅██████████████████████ Forward start guard (4.1 client + 4.2 server)
Phase 5 ✅██████████████████████ Integration & verification (5.1 5.2 5.3)
```

## ✨ Epic Complete — All Tasks Delivered

The Broker Authentication Epic is fully implemented and tested. All 13 tasks across 5 phases have been completed successfully.

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

### Phase 3 — Authentication UI Components ✅ COMPLETE

| # | Task | Deliverable | Status | Commit |
|---|------|-------------|--------|--------|
| 3.1 | Nav broker status icon | `web/templates/base.html` + `web/static/js/broker_status.js` | ✅ | `4792a19` |
| 3.2 | Auth popup modal | `web/templates/components/broker_auth_modal.html` + `web/static/js/broker_auth_modal.js` | ✅ | Current session |
| 3.3 | Session expiry toast | `web/static/js/broker_status.js` | ✅ | `4792a19` |

### Phase 4 — Forward Test Page Guard ✅ COMPLETE

| # | Task | Deliverable | Status |
|---|------|-------------|--------|
| 4.1 | Gate Forward Start button on auth status | `web/static/js/forward.js` (client-side gate) | ✅ **Complete** |
| 4.2 | Server-side guard on `/api/forward/start` | `src/backtest/api/forward.py` | ✅ **Complete** |

### Phase 5 — Integration & Verification ✅ COMPLETE

| # | Task | Deliverable | Status |
|---|------|-------------|--------|
| 5.1 | Full auth flow integration test | `tests/manual/test_auth_flow_integration.py` (7 tests) | ✅ **Complete** |
| 5.2 | Session expiry warning test | `tests/test_broker_expiry.py` (11 tests) | ✅ **Complete** |
| 5.3 | Security verification checklist | `tests/test_security_verification.py` (29 tests) | ✅ **Complete** |

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

**Frontend (Tasks 3.1 + 3.2 + 3.3, tested via Node harnesses):**

* Nav pill `#broker-status` on every page (dot `#broker-status-dot` +
  name `#broker-status-name`), 🔴/🟡/🟢/⚪ with tooltips.
* `BrokerStatus` global: `get()` / `state()` / `refresh()` /
  `expectLogout()`; polls every 60 s; dispatches document event
  **`broker:status`** with the status payload on every poll.
* Auth modal overlay `#broker-auth-overlay` on every page via
  `components/broker_auth_modal.html` include. Three views (credentials
  → TOTP → authenticated) swapped by flow state. Registered as
  `window.BrokerAuthUI = { open, close }`. Password cleared from DOM
  immediately after Login click; TOTP field auto-focuses after credential
  success; spinner on buttons during API calls; inline errors for bad
  credentials / invalid TOTP; Logout calls `expectLogout()` before
  `POST /api/broker/logout` then `refresh()`.

**Integration hooks available for the pending tasks:**

| Hook | Consumer |
|---|---|
| `window.BrokerAuthUI.open()` | Task 4.1 `forward.js` — clicking the disabled Start button opens the auth popup |
| `BrokerStatus.refresh()` — force immediate poll after login/logout | Used by modal after every state change |
| `BrokerStatus.expectLogout()` — suppress "session expired" toast | Used by modal right before `POST /api/broker/logout` |
| `broker:status` document event + `BrokerStatus.state()` | Task 4.1 `forward.js` gate |
| `get_session_manager().is_authenticated()` | Task 4.2 server-side guard |

---

## Pick-up instructions

**No remaining tasks — the epic is complete!** 🎉

If you need to extend or modify the system:

* Add a new broker: subclass `BrokerAuthBase`, add to broker selector (future)
* Add new API endpoints: follow the pattern in `src/backtest/api/broker_auth.py`
* Add new UI pages: follow the pattern in `src/backtest/web/templates/` and `static/js/`
* Run tests: `PYTHONPATH=src python -m pytest tests/ -q -k "not live"` (1740 passed)

---

## How to run the verification suite

```bash
# venv at /home/user/.venv (pip install -r requirements.txt once)
cd /home/user/back-test
PYTHONPATH=src .venv/bin/python -m pytest tests/ -q -k "not live"

# Broker-auth specific tests
PYTHONPATH=src .venv/bin/python -m pytest tests/test_broker_base.py \
    tests/test_broker_mstock.py tests/test_broker_session_manager.py \
    tests/test_api_broker_auth.py tests/test_broker_ui.py \
    tests/test_api_forward.py tests/manual/test_auth_flow_integration.py \
    tests/test_broker_expiry.py tests/test_security_verification.py -v
node tests/js/test_broker_status.mjs
node tests/js/test_broker_auth_modal.mjs
node tests/js/test_forward_auth_gate.mjs
```

**Final totals:** 1740 passed / 3 skipped / 1 failed — the failure is
pre-existing and unrelated (`test_mstock_auth::test_login_sends_sdk_headers`
needs `MSTOCK_API_KEY` in the environment; documented in TASK-TRACKER.md).

Per-task test counts: 1.1 → 13, 1.2 → 28, 1.3+2.2 → 20, 2.1 → 24,
3.1+3.3 → 7 pytest + 12 node, 3.2 → 7 pytest + 14 node,
4.1 → 2 pytest + 8 node, 4.2 → 7 pytest (auth guard tests),
5.1 → 7 pytest (integration tests), 5.2 → 11 pytest (expiry tests),
5.3 → 29 pytest (security tests).

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
