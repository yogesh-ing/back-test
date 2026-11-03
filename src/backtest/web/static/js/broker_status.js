/**
 * Broker connection status (mStock auth epic Tasks 3.1 + 3.3).
 *
 * Persistent nav indicator driven by GET /api/broker/status (polled every
 * 60 s, plus one immediate poll on load — no page reload needed to update):
 *
 *   🔴 unauthenticated | expired  → click opens the auth popup (login)
 *   🟡 expiring_soon             → click opens the auth popup (re-auth)
 *   🟢 authenticated             → click opens the session info popup
 *
 * The popup itself is registered by broker_auth_modal.js (Task 3.2) as
 * window.BrokerAuthUI.open(); until then the click is a guarded no-op.
 *
 * Other components integrate via:
 *   BrokerStatus.get()             → last status payload (or null)
 *   BrokerStatus.state()           → last status string
 *   BrokerStatus.refresh()         → force an immediate poll (Promise)
 *   BrokerStatus.expectLogout()    → suppress the "session expired" toast
 *                                     for a user-initiated logout
 *   document "broker:status" event → fired on every applied poll
 *
 * Task 3.3 — session expiry toast: on entering expiring_soon a clickable
 * warning toast fires exactly once per expiry cycle (flag resets on
 * re-authentication or expiry). When a valid session drops to
 * expired/unauthenticated, an error toast asks the user to re-authenticate.
 *
 * Sprint A4 — market-hours login prompt: while the NSE session is PRE_OPEN or
 * OPEN, a broker that cannot trade (session expired, inside its last 10
 * minutes, or never logged in while runners sit on its frozen mark) gets a
 * persistent topbar CTA plus one auto-opened login popup per stage. Both are
 * derived from the poll payload, so dismissing the popup does not clear the
 * banner — only a verified session does. Outside the active window nothing
 * auto-opens and no banner renders; clicking the Brokers pill still works at
 * any hour.
 */
const BrokerStatus = (() => {
    const POLL_MS = 60000;   // PRD: poll every 60 seconds
    const TOAST_MS = 10000;  // PRD: toast auto-dismisses after 10 seconds
    // Sprint A4 — mirrors the server's EXPIRING_URGENT_WINDOW_MINUTES: inside
    // this window the prompt escalates from "expiring" to "act now".
    const URGENT_WINDOW_MINUTES = 10;

    const DOTS = {
        authenticated: "🟢",
        expiring_soon: "🟡",
        expired: "🔴",
        unauthenticated: "🔴",
        unknown: "⚪",
    };

    let current = null;           // last payload from /api/broker/status
    let prevState = null;         // last applied status string
    let prevFingerprint = null;   // multi-broker auth fingerprint for cross-tab wake
    let pollTimer = null;         // governed setTimeout handle when PollCadence is present
    let expiryToastShown = false; // once per expiry cycle (Task 3.3)
    let suppressExpiryToast = false; // set by expectLogout() (modal logout)
    let started = false;
    let autoOpened = {};          // {broker: stage} — one auto-popup per stage

    const byId = (id) => document.getElementById(id);
    const isValidSession = (s) => s === "authenticated" || s === "expiring_soon";

    function displayName() {
        return (current && current.broker_display_name) || "Broker";
    }

    function formatExpiry() {
        if (!current || !current.expires_at) return "";
        const d = new Date(current.expires_at);
        if (Number.isNaN(d.getTime())) return current.expires_at;
        return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    }

    function tooltip(state) {
        const name = displayName();
        if (state === "authenticated") return `${name}: connected (expires ${formatExpiry() || "—"})`;
        if (state === "expiring_soon") return `${name}: session expiring soon — click to re-authenticate`;
        if (state === "expired") return `${name}: session expired — click to re-authenticate`;
        return `${name}: not connected — click to log in`;
    }

    function render(state) {
        const dot = byId("broker-status-dot");
        const nameEl = byId("broker-status-name");
        const btn = byId("broker-status");
        const strip = byId("broker-strip");
        const sessions = current && current.sessions;

        // Multi-broker PRD Phase A: when the payload carries the per-broker
        // sessions map, the header renders one dot per broker (the status
        // strip); the legacy single dot+name is hidden.
        if (strip && sessions && Object.keys(sessions).length) {
            strip.innerHTML = "";
            for (const name of Object.keys(sessions)) {
                const row = sessions[name];
                const item = document.createElement("span");
                item.className = "broker-strip-item";
                item.dataset.broker = name;
                item.dataset.state = row.status || "unknown";
                // CSS renders the status dot separately; keep the original
                // text for assistive technology and the stable status contract.
                item.dataset.label = row.broker_display_name || name;
                const d = row.authenticated ? DOTS[row.status] || "🟢"
                    : (row.status === "expired" ? DOTS.expired : "⚪");
                item.textContent = `${d} ${row.broker_display_name || name}`;
                item.title = `${row.broker_display_name || name}: ${row.status || "unknown"}`;
                strip.appendChild(item);
            }
            if (dot) dot.hidden = true;
            if (nameEl) nameEl.hidden = true;
            if (btn) {
                btn.title = "Broker connections — click to open the Broker Board";
                btn.setAttribute("aria-label", btn.title);
            }
            return;
        }

        if (dot) {
            dot.hidden = false;
            dot.textContent = DOTS[state] || DOTS.unknown;
            dot.dataset.state = state;
        }
        if (nameEl) { nameEl.hidden = false; nameEl.textContent = displayName(); }
        if (btn) {
            btn.title = tooltip(state);
            btn.setAttribute("aria-label", tooltip(state));
        }
    }

    // ---- Task 3.3: expiry toasts ---------------------------------------

    function openAuthPopup() {
        // Multi-broker Phase A: the Broker Board is the landing surface when
        // available (grid of per-broker cards); the single auth modal stays
        // the fallback for pages that don't render the board.
        if (window.BrokerBoard && typeof window.BrokerBoard.open === "function") {
            window.BrokerBoard.open();
            return;
        }
        if (window.BrokerAuthUI && typeof window.BrokerAuthUI.open === "function") {
            window.BrokerAuthUI.open();
        }
    }

    function makeToast(className, text, onClick) {
        const stack = byId("toast-stack");
        if (!stack) return;
        const toast = document.createElement("div");
        toast.className = className;
        toast.setAttribute("role", "button");
        toast.textContent = text;
        let dismissed = false;
        const dismiss = () => {
            if (!dismissed) { dismissed = true; toast.remove(); }
        };
        toast.addEventListener("click", () => { dismiss(); if (onClick) onClick(); });
        stack.appendChild(toast);
        setTimeout(dismiss, TOAST_MS);
    }

    function minutesLeft() {
        if (!current || !current.expires_at) return 30;
        const ms = new Date(current.expires_at).getTime() - Date.now();
        if (Number.isNaN(ms)) return 30;
        return Math.max(1, Math.round(ms / 60000));
    }

    // Multi-broker PRD Phase A: per-broker expiry toasts. The status payload
    // carries `expiry_events` — one (broker, kind) per session transition,
    // consumed server-side exactly once. Events for the UI-active broker are
    // skipped here (the legacy transition toast below already covers it).
    function handleExpiryEvents(payload) {
        const events = (payload && payload.expiry_events) || [];
        const sessions = (payload && payload.sessions) || {};
        const activeBroker = payload && payload.broker;
        for (const evt of events) {
            if (!evt || evt.broker === activeBroker) continue;
            const row = sessions[evt.broker] || {};
            const name = row.broker_display_name || evt.broker;
            if (evt.kind === "expiring_soon") {
                makeToast(
                    "toast warning clickable",
                    `⚠️ ${name} session expiring soon. Click here to re-authenticate.`,
                    openAuthPopup,
                );
            } else if (evt.kind === "expired") {
                makeToast(
                    "toast error clickable",
                    `${name} session expired — runners routed to ${name} pause entries. Click to re-authenticate.`,
                    openAuthPopup,
                );
            }
        }
    }

    function handleTransition(prev, next) {
        if (next === "expiring_soon") {
            if (prev !== "expiring_soon" && !expiryToastShown) {
                expiryToastShown = true;
                makeToast(
                    "toast warning clickable",
                    `⚠️ ${displayName()} session expiring in ~${minutesLeft()} minutes. Click here to re-authenticate.`,
                    openAuthPopup,
                );
            }
        } else if (next === "authenticated") {
            expiryToastShown = false; // fresh cycle after re-authentication
        } else if (isValidSession(prev) && (next === "expired" || next === "unauthenticated")) {
            expiryToastShown = false;
            if (suppressExpiryToast) {
                suppressExpiryToast = false; // user-initiated logout — no toast
            } else {
                makeToast(
                    "toast error clickable",
                    `${displayName()} session expired — please re-authenticate.`,
                    openAuthPopup,
                );
            }
        }
    }

    // ---- Sprint A4: market-hours login prompt ------------------------------

    function marketActive() {
        // Server truth, via components/market_chip.js. Unknown (the chip has not
        // reported yet) counts as closed: the chip fires `market:state` on load
        // and that event re-runs this evaluation, so a late answer still lands
        // the prompt — rather than a guess landing a popup at 21:00.
        const s = typeof window !== "undefined" ? window.__lastMarketState : null;
        const state = s && s.session_state;
        return state === "PRE_OPEN" || state === "OPEN";
    }

    function rowMinutesLeft(row) {
        const at = row && row.expires_at;
        if (!at) return null;
        const ms = new Date(at).getTime() - Date.now();
        if (Number.isNaN(ms)) return null;
        return ms / 60000;
    }

    function loginStage(row) {
        const state = row && row.status;
        if (state === "expired") return "expired";
        if (state === "unauthenticated") return "unauthenticated";
        if (state === "expiring_soon") {
            const left = rowMinutesLeft(row);
            if (left !== null && left <= URGENT_WINDOW_MINUTES) return "urgent";
        }
        return null;
    }

    const STAGE_RANK = { expired: 3, urgent: 2, unauthenticated: 1 };
    const BANNER_TEXT = {
        expired: "🔴 Broker login needed",
        unauthenticated: "🔴 Broker login needed",
        urgent: "🟡 Re-login needed (<10m)",
    };

    function loginPrompt(payload) {
        if (!marketActive()) return null;
        const sessions = (payload && payload.sessions) || {};
        let best = null;
        for (const name of Object.keys(sessions)) {
            const row = sessions[name] || {};
            const stage = loginStage(row);
            if (!stage) continue;
            // Never-logged-in is only news when a book is actually stuck on it;
            // a session that died mid-market is news on its own.
            if (stage === "unauthenticated" && !(row.waiting_runners > 0)) continue;
            if (!best || STAGE_RANK[stage] > STAGE_RANK[best.stage]) {
                best = { broker: name, stage, row };
            }
        }
        return best;
    }

    function renderLoginBanner(prompt) {
        const el = byId("broker-login-banner");
        if (!el) return;
        if (!prompt) {
            el.hidden = true;
            delete el.dataset.stage;
            delete el.dataset.broker;
            el.textContent = "";
            return;
        }
        const row = prompt.row || {};
        const name = row.broker_display_name || prompt.broker;
        const waiting = row.waiting_runners || 0;
        el.dataset.stage = prompt.stage;
        el.dataset.broker = prompt.broker;
        el.textContent = BANNER_TEXT[prompt.stage] || BANNER_TEXT.expired;
        el.title = prompt.stage === "urgent"
            ? `${name} session expires in under ${URGENT_WINDOW_MINUTES} minutes`
              + " — click to re-authenticate now"
            : waiting
                ? `${name} is not logged in — ${waiting} runner(s) are pricing off a`
                  + " frozen mark. Click to re-authenticate."
                : `${name} session is not valid — live feeds and runner entries are`
                  + " stopped. Click to re-authenticate.";
        el.setAttribute("aria-label", el.title);
        el.hidden = false;
    }

    function openAuthPopupFor(broker) {
        // Targeted open: the modal pre-selects that broker's flow, which is the
        // point of an auto-popup — the operator should not have to pick the
        // broker that just died out of a grid.
        if (window.BrokerAuthUI && typeof window.BrokerAuthUI.open === "function") {
            window.BrokerAuthUI.open(broker ? { broker } : undefined);
            return;
        }
        openAuthPopup();
    }

    function releaseLatches(payload) {
        const sessions = (payload && payload.sessions) || {};
        for (const name of Object.keys(autoOpened)) {
            const row = sessions[name];
            if (!row || !loginStage(row)) delete autoOpened[name];
        }
    }

    function maybeAutoOpen(prompt) {
        if (!prompt) return;
        if (autoOpened[prompt.broker] === prompt.stage) return; // once per stage
        autoOpened[prompt.broker] = prompt.stage;
        openAuthPopupFor(prompt.broker);
    }

    function evaluateLoginPrompt(payload) {
        const data = payload || current;
        const prompt = loginPrompt(data);
        renderLoginBanner(prompt);
        if (!marketActive()) {
            // The window shut: the latch's whole job is to stop a 30-second poll
            // from nagging, and the next open is a fresh chance to act. Without
            // this a tab left open overnight would never re-prompt at 09:00.
            autoOpened = {};
            return;
        }
        // Release before latching the new prompt, so a session that came back
        // and died again during one page life prompts a second time.
        releaseLatches(data);
        maybeAutoOpen(prompt);
    }

    // ---- polling ---------------------------------------------------------

    function authFingerprint(payload) {
        if (!payload) return "";
        const parts = [String(payload.status || "")];
        const sessions = payload.sessions;
        if (sessions && typeof sessions === "object") {
            for (const k of Object.keys(sessions).sort()) {
                const row = sessions[k] || {};
                parts.push(`${k}:${row.status || ""}:${row.authenticated ? 1 : 0}`);
            }
        }
        return parts.join("|");
    }

    function schedulePoll(payload) {
        const pc = typeof window !== "undefined" && window.PollCadence;
        if (!pc) return;
        if (pollTimer) {
            clearTimeout(pollTimer);
            pollTimer = null;
        }
        const delay = pc.brokerStatusDelay(payload || current);
        if (delay === null) return;
        pollTimer = setTimeout(refresh, delay);
    }

    function apply(payload) {
        current = payload;
        const next = payload && payload.status;
        const fp = authFingerprint(payload);
        // Schedule next governed poll BEFORE handleTransition so toast timers
        // remain the most recently queued timer in deterministic tests.
        schedulePoll(payload);
        render(next);
        handleTransition(prevState, next);
        handleExpiryEvents(payload);
        evaluateLoginPrompt(payload);
        if (prevFingerprint !== null && fp !== prevFingerprint) {
            const pc = typeof window !== "undefined" && window.PollCadence;
            if (pc) pc.notifyBrokerAuthChanged(fp);
        }
        prevFingerprint = fp;
        prevState = next;
        document.dispatchEvent(new CustomEvent("broker:status", { detail: payload }));
    }

    async function refresh() {
        try {
            const resp = await fetch("/api/broker/status");
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            const payload = await resp.json();
            apply(payload);
            return payload;
        } catch (err) {
            // Keep the last known state; grey dot only while never fetched.
            console.warn("[broker-status] status poll failed:", err && err.message);
            if (!prevState) render("unknown");
            schedulePoll(current);
            return null;
        }
    }

    function start() {
        if (started) return;
        started = true;
        const pc = typeof window !== "undefined" && window.PollCadence;
        const canListen = !!document && typeof document.addEventListener === "function";
        refresh();
        if (!pc) {
            setInterval(refresh, POLL_MS);
        } else {
            pc.onBrokerAuthWake(() => { refresh(); });
            if (canListen) {
                document.addEventListener("visibilitychange", () => {
                    if (!document.hidden) {
                        refresh();
                    } else {
                        schedulePoll(current);
                    }
                });
            }
        }
        if (canListen) {
            // Sprint A4: the market-state edge (09:00 PRE_OPEN, the 15:30 close)
            // decides whether the login prompt may show at all, so it re-runs the
            // evaluation even when no poll is due. Registered with or without
            // PollCadence — the banner is not a polling-cadence feature.
            document.addEventListener("market:state", () => {
                schedulePoll(current);
                evaluateLoginPrompt(current);
            });
        }
        const btn = byId("broker-status");
        if (btn) btn.addEventListener("click", openAuthPopup);
        const banner = byId("broker-login-banner");
        if (banner) {
            banner.addEventListener("click", () => openAuthPopupFor(banner.dataset.broker));
        }
    }

    return {
        start,
        refresh,
        get: () => current,
        state: () => prevState,
        expectLogout: () => { suppressExpiryToast = true; },
    };
})();

// Auto-start on every page that renders the nav indicator.
if (document.getElementById("broker-status")) {
    BrokerStatus.start();
}
