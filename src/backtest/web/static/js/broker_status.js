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
 */
const BrokerStatus = (() => {
    const POLL_MS = 60000;   // PRD: poll every 60 seconds
    const TOAST_MS = 10000;  // PRD: toast auto-dismisses after 10 seconds

    const DOTS = {
        authenticated: "🟢",
        expiring_soon: "🟡",
        expired: "🔴",
        unauthenticated: "🔴",
        unknown: "⚪",
    };

    let current = null;           // last payload from /api/broker/status
    let prevState = null;         // last applied status string
    let expiryToastShown = false; // once per expiry cycle (Task 3.3)
    let suppressExpiryToast = false; // set by expectLogout() (modal logout)
    let started = false;

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
        if (dot) {
            dot.textContent = DOTS[state] || DOTS.unknown;
            dot.dataset.state = state;
        }
        if (nameEl) nameEl.textContent = displayName();
        if (btn) {
            btn.title = tooltip(state);
            btn.setAttribute("aria-label", tooltip(state));
        }
    }

    // ---- Task 3.3: expiry toasts ---------------------------------------

    function openAuthPopup() {
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

    // ---- polling ---------------------------------------------------------

    function apply(payload) {
        current = payload;
        const next = payload && payload.status;
        render(next);
        handleTransition(prevState, next);
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
            return null;
        }
    }

    function start() {
        if (started) return;
        started = true;
        refresh();
        setInterval(refresh, POLL_MS);
        const btn = byId("broker-status");
        if (btn) btn.addEventListener("click", openAuthPopup);
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
