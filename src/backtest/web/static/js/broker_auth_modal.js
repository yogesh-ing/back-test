/**
 * Broker authentication popup modal (mStock Auth UI epic Task 3.2).
 *
 * Three views inside a single overlay, swapped by the flow state:
 *
 *   step-credentials  → username + password + Login button
 *                        TOTP section shown but disabled (🔒)
 *   step-totp         → ✅ Credentials verified, TOTP input enabled
 *   step-authenticated→ session info + Logout button
 *
 * API calls:
 *   POST /api/broker/login        { username, password }
 *   POST /api/broker/verify-totp  { totp_code }
 *   POST /api/broker/logout
 *
 * Integration hooks used:
 *   BrokerStatus.refresh()     → after login-success / TOTP-success / logout
 *   BrokerStatus.expectLogout()→ before POST /api/broker/logout
 *   window.BrokerAuthUI.open() → registered globally so broker_status.js
 *                                and toasts can open the popup.
 *
 * UX rules (from the PRD):
 *   • Login button shows a spinner during the API call.
 *   • Error messages shown inline (wrong credentials, invalid TOTP).
 *   • TOTP field auto-focuses after credential success.
 *   • [×] closes the modal and cancels the entire flow.
 *   • Password field cleared from DOM immediately after Login click.
 */
const BrokerAuthUI = (() => {
    const overlay  = () => document.getElementById("broker-auth-overlay");
    const titleEl  = () => document.getElementById("broker-auth-title");

    // views
    const viewCredentials  = () => document.getElementById("broker-auth-step-credentials");
    const viewTotp         = () => document.getElementById("broker-auth-step-totp");
    const viewAuth         = () => document.getElementById("broker-auth-step-authenticated");

    // step-1 elements
    const usernameInput    = () => document.getElementById("broker-auth-username");
    const passwordInput    = () => document.getElementById("broker-auth-password");
    const loginBtn         = () => document.getElementById("broker-auth-login-btn");
    const credError        = () => document.getElementById("broker-auth-credentials-error");

    // step-2 elements
    const totpInput        = () => document.getElementById("broker-auth-totp-code");
    const totpBtn          = () => document.getElementById("broker-auth-totp-btn");
    const totpError        = () => document.getElementById("broker-auth-totp-error");

    // step-3 elements
    const expiresEl        = () => document.getElementById("broker-auth-expires");
    const brokerNameEl     = () => document.getElementById("broker-auth-broker-name");
    const logoutBtn        = () => document.getElementById("broker-auth-logout-btn");

    // close
    const closeBtn         = () => document.getElementById("broker-auth-close");

    // remember-session-today toggles (credentials view + authenticated view)
    const rememberToggleCred = () => document.getElementById("broker-auth-remember-toggle");
    const rememberToggleAuth = () => document.getElementById("broker-auth-remember-toggle-auth");
    const rememberHint       = () => document.getElementById("broker-auth-remember-hint");

    // broker selector (2026-09-25): which broker's login flow to run.
    const brokerSelect       = () => document.getElementById("broker-auth-broker-select");
    const usernameLabel      = () => document.getElementById("broker-auth-username-label");

    // Per-broker step-1 wording (field labels + placeholders). A new broker
    // only needs an entry here once it's registered server-side.
    const BROKER_FIELDS = {
        mstock: { display: "mStock", userLabel: "Username", userPlaceholder: "Enter username", passLabel: "Password" },
        dhan:   { display: "Dhan", userLabel: "Client ID", userPlaceholder: "Enter Dhan client ID", passLabel: "PIN" },
    };

    // The broker whose credentials flow is currently in progress (kept across
    // steps so /api/broker/verify-totp lands on the right session).
    let selectedBroker = null;
    // The broker shown on the authenticated view — its [Logout] must hit
    // exactly this broker and leave every other session alive (Phase A).
    let authBroker = null;

    // ---- helpers -----------------------------------------------------------

    function showView(name) {
        const views = [
            ["credentials", viewCredentials()],
            ["totp",        viewTotp()],
            ["auth",        viewAuth()],
        ];
        for (const [key, el] of views) {
            if (!el) continue;
            el.hidden = key !== name;
        }
    }

    function setTitle(text) {
        const el = titleEl();
        if (el) el.textContent = text;
    }

    // ---- broker selector ---------------------------------------------------

    let brokerListRequested = false; // one fetch per page — the registry is static

    async function loadBrokerList() {
        const sel = brokerSelect();
        if (!sel || sel.options.length > 0 || brokerListRequested) return; // already populated
        brokerListRequested = true;
        let brokers = [];
        try {
            const resp = await fetch("/api/broker/list");
            const data = await resp.json();
            brokers = (data && data.brokers) || [];
        } catch (err) { /* fall through — selector just stays empty */ }
        for (const b of brokers) {
            const opt = document.createElement("option");
            opt.value = b.name;
            opt.textContent = b.display_name || b.name;
            sel.appendChild(opt);
        }
    }

    function applyBrokerFields() {
        const sel = brokerSelect();
        const name = (sel && sel.value) || "mstock";
        const f = BROKER_FIELDS[name] || BROKER_FIELDS.mstock;
        if (usernameLabel()) usernameLabel().textContent = f.userLabel;
        if (usernameInput()) usernameInput().placeholder = f.userPlaceholder;
        // Defensive: the node test harness's fake elements have no
        // .closest(); real browsers always do.
        const passEl = passwordInput();
        const passField = passEl && typeof passEl.closest === "function"
            ? passEl.closest(".broker-auth-field")
            : null;
        if (passField && typeof passField.querySelector === "function") {
            const lbl = passField.querySelector("label");
            if (lbl) lbl.textContent = f.passLabel;
        }
    }

    function currentBroker() {
        const sel = brokerSelect();
        return selectedBroker || (sel && sel.value) || null;
    }

    function setSpinner(btnEl, spinning) {
        if (!btnEl) return;
        const textEl = btnEl.querySelector(".broker-auth-btn-text");
        const spinEl = btnEl.querySelector(".broker-auth-spinner");
        if (textEl) textEl.hidden = spinning;
        if (spinEl) spinEl.hidden = !spinning;
        btnEl.disabled = spinning;
    }

    function formatExpiry(iso) {
        if (!iso) return "—";
        const d = new Date(iso);
        if (Number.isNaN(d.getTime())) return iso;
        return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    }

    async function postJSON(url, body) {
        const resp = await fetch(url, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
        });
        return resp.json();
    }

    // ---- view: credentials (Step 1) ----------------------------------------

    // Broker-aware step-1 title: "mStock Login" / "Dhan Login" (generic
    // fallback for a broker without a fields entry).
    function loginTitle() {
        const sel = brokerSelect();
        const name = selectedBroker || (sel && sel.value) || "mstock";
        const f = BROKER_FIELDS[name];
        return "🔐 " + (f && f.display ? f.display + " Login" : "Broker Login");
    }

    function showCredentials() {
        setTitle(loginTitle());
        showView("credentials");
        if (credError()) credError().textContent = "";
        applyBrokerFields();
        selectedBroker = null; // next login uses the selector's current value
        if (usernameInput()) usernameInput().focus();
        syncRememberToggle();
    }

    async function handleLogin() {
        const u = usernameInput();
        const p = passwordInput();
        if (!u || !p) return;

        const username = u.value.trim();
        const password = p.value;

        if (!username || !password) {
            if (credError()) credError().textContent = "Username and password are required";
            return;
        }

        // PRD: clear the password from the DOM immediately after click
        const passwordValue = password;
        p.value = "";

        setSpinner(loginBtn(), true);
        if (credError()) credError().textContent = "";

        try {
            const broker = brokerSelect() && brokerSelect().value;
            const result = await postJSON("/api/broker/login", { broker, username, password });
            if (result.success && result.requires_totp) {
                selectedBroker = broker;
                showTotp();
            } else if (result.success) {
                // login succeeded but no TOTP required — go straight to authenticated
                await refreshAndShowAuth();
            } else {
                // show error but don't call showCredentials() which clears it
                if (credError()) credError().textContent = result.message || "Login failed";
                setTitle(loginTitle());
                showView("credentials");
            }
        } catch (err) {
            if (credError()) credError().textContent = "Connection error — please try again";
        } finally {
            setSpinner(loginBtn(), false);
        }
    }

    // ---- view: TOTP (Step 2) -----------------------------------------------

    function showTotp() {
        const name = currentBroker();
        const display = (name && BROKER_FIELDS[name] && name.charAt(0).toUpperCase() + name.slice(1)) || "Broker";
        setTitle(`🔐 ${display} Login — TOTP`);
        showView("totp");
        if (totpError()) totpError().textContent = "";
        const t = totpInput();
        if (t) {
            t.value = "";
            t.focus();
        }
    }

    async function handleTotp() {
        const t = totpInput();
        if (!t) return;
        const code = t.value.trim();
        if (!code) {
            if (totpError()) totpError().textContent = "Enter the 6-digit code";
            return;
        }

        setSpinner(totpBtn(), true);
        if (totpError()) totpError().textContent = "";

        try {
            // Multi-broker Phase A: target the broker whose flow is pending.
            const result = await postJSON("/api/broker/verify-totp", {
                totp_code: code,
                broker: currentBroker(),
            });
            if (result.success) {
                await refreshAndShowAuth();
            } else {
                if (totpError()) totpError().textContent = result.message || "Invalid TOTP code";
                // backend keeps temp context — user can retry; field stays enabled
            }
        } catch (err) {
            if (totpError()) totpError().textContent = "Connection error — please try again";
        } finally {
            setSpinner(totpBtn(), false);
        }
    }

    // ---- remember-session toggle (2026-09-24) ------------------------------

    async function syncRememberToggle() {
        // Reflect the server's toggle state on both checkboxes. The status
        // payload carries remember_session {enabled, has_saved}.
        // No toggle/hint in the DOM → nothing to reflect, skip the fetch
        // (also keeps the node harness's "no fetch" invariants honest).
        if (!rememberToggleCred() && !rememberToggleAuth() && !rememberHint()) return;
        let enabled = false, saved = false;
        try {
            const resp = await fetch("/api/broker/status");
            const data = await resp.json();
            const rs = data.remember_session || {};
            enabled = !!rs.enabled;
            saved = !!rs.has_saved;
        } catch (err) { /* keep defaults */ }
        for (const el of [rememberToggleCred(), rememberToggleAuth()]) {
            if (el) el.checked = enabled;
        }
        const hint = rememberHint();
        if (hint) {
            hint.textContent = enabled
                ? (saved ? "saved ✓" : "saves on login")
                : "";
        }
    }

    async function handleRememberChange(evt) {
        const enabled = !!evt.target.checked;
        // Optimistic sync of the sibling checkbox, then the server call.
        for (const el of [rememberToggleCred(), rememberToggleAuth()]) {
            if (el && el !== evt.target) el.checked = enabled;
        }
        try {
            const result = await postJSON("/api/broker/remember-session", { enabled });
            if (!result.success) throw new Error(result.error || "failed");
            const hint = rememberHint();
            if (hint) {
                hint.textContent = enabled
                    ? (result.deleted ? "saved ✓" : "saves on login")
                    : "won't be remembered";
            }
        } catch (err) {
            // Revert on failure so the checkbox never lies.
            evt.target.checked = !enabled;
            for (const el of [rememberToggleCred(), rememberToggleAuth()]) {
                if (el && el !== evt.target) el.checked = !enabled;
            }
        }
        if (BrokerStatus && typeof BrokerStatus.refresh === "function") {
            BrokerStatus.refresh();
        }
    }

    // ---- view: authenticated (Step 3) --------------------------------------

    function showAuthenticated(brokerName) {
        const status = BrokerStatus && BrokerStatus.get();
        // Multi-broker Phase A: prefer the targeted broker's session row from
        // the sessions map; fall back to the legacy top-level (UI-active) keys.
        const sessions = (status && status.sessions) || {};
        const target = brokerName || currentBroker();
        const row = (target && sessions[target]) || null;
        const name = (row && row.broker_display_name)
            || (status && status.broker_display_name) || "mStock";
        const expiresAt = row ? row.expires_at : (status && status.expires_at);

        authBroker = target || (status && status.broker) || null;
        setTitle(`🟢 ${name} Connected`);
        showView("auth");

        if (brokerNameEl()) brokerNameEl().textContent = name;
        if (expiresEl()) {
            expiresEl().textContent = formatExpiry(expiresAt);
        }
        syncRememberToggle();
    }

    async function refreshAndShowAuth() {
        if (BrokerStatus && typeof BrokerStatus.refresh === "function") {
            await BrokerStatus.refresh();
        }
        // Present the authenticated view BEFORE scheduling auto-close, so the
        // success state is always visible even if the timer fires immediately
        // (e.g. a test harness that executes setTimeout synchronously).
        showAuthenticated();
        // Auto-close modal after a brief delay so the user sees success, then
        // returns to the page. `resetView=false` keeps the authenticated view
        // mounted; a later open() re-initialises the step from session state.
        setTimeout(() => close(false), 1500);
    }

    // ---- logout ------------------------------------------------------------

    async function handleLogout() {
        // Check if data fetch is running and warn user
        try {
            const statusResp = await fetch('/api/data/status');
            const statusData = await statusResp.json();
            
            if (statusData.status === 'running') {
                const shouldProceed = confirm(
                    `A data fetch is currently running (${statusData.fetched || 0}/${statusData.total || 0} symbols).\n\n` +
                    `It will continue in the background after logout.\n\n` +
                    `Do you want to stop it first?`
                );
                
                if (!shouldProceed) {
                    // User cancelled logout
                    return;
                }
                
                // User wants to stop the fetch first
                try {
                    const stopResp = await fetch('/api/data/stop', { method: 'POST' });
                    const stopData = await stopResp.json();
                    
                    if (stopResp.ok) {
                        alert('Data fetch stopped. You can now logout safely.');
                    } else {
                        alert('Could not stop fetch: ' + (stopData.error || 'Unknown error'));
                    }
                } catch (err) {
                    alert('Error stopping fetch: ' + err.message);
                }
                
                // Don't proceed with logout yet - let user click again
                return;
            }
        } catch (err) {
            // If status check fails, proceed with logout anyway
            console.warn('Could not check data fetch status:', err);
        }
        
        if (BrokerStatus && typeof BrokerStatus.expectLogout === "function") {
            BrokerStatus.expectLogout();
        }
        try {
            // Multi-broker Phase A: log out ONLY the broker shown on the
            // authenticated view; other broker sessions stay live.
            await postJSON("/api/broker/logout", authBroker ? { broker: authBroker } : {});
        } catch (err) {
            // ignore — we're clearing UI state regardless
        }
        if (BrokerStatus && typeof BrokerStatus.refresh === "function") {
            await BrokerStatus.refresh();
        }
        showCredentials();
    }

    // ---- open / close ------------------------------------------------------

    function open(opts) {
        const ov = overlay();
        if (!ov) return;
        ov.classList.add("open");

        // Multi-broker Phase A: open({broker: "dhan"}) targets one broker's
        // flow (Broker Board [Login] path) — the selector is pre-set and the
        // view seeds from THAT broker's session state.
        const targetBroker = (opts && opts.broker) || null;

        loadBrokerList().then(() => {
            const sel = brokerSelect();
            if (targetBroker && sel) sel.value = targetBroker;
            applyBrokerFields();
        });

        const status = BrokerStatus && BrokerStatus.get();
        const sessions = (status && status.sessions) || {};
        if (targetBroker) {
            const row = sessions[targetBroker];
            if (row && row.authenticated) {
                showAuthenticated(targetBroker);
            } else {
                showCredentials();
                selectedBroker = targetBroker;
            }
            return;
        }

        // Legacy (no target): seed from the UI-active broker's auth state.
        const state = BrokerStatus && BrokerStatus.state();
        if (state === "authenticated" || state === "expiring_soon") {
            showAuthenticated();
        } else {
            showCredentials();
        }
    }

    function close(resetView = true) {
        const ov = overlay();
        if (!ov) return;
        ov.classList.remove("open");
        // Reset to credentials view so next open starts fresh — but skip the
        // reset when the auto-close timer fires right after a successful
        // auth, so the authenticated view remains visible/assertable.
        if (resetView) {
            showCredentials();
            if (credError()) credError().textContent = "";
            if (totpError()) totpError().textContent = "";
        }
    }

    // ---- init --------------------------------------------------------------

    function init() {
        const ov = overlay();
        if (!ov) return;

        // close button
        const cb = closeBtn();
        if (cb) cb.addEventListener("click", close);

        // overlay click-to-dismiss (only outside the modal card)
        ov.addEventListener("click", (e) => {
            if (e.target === ov) close();
        });

        // Escape key
        document.addEventListener("keydown", (e) => {
            if (e.key === "Escape" && ov.classList.contains("open")) close();
        });

        // login + totp buttons
        const lb = loginBtn();
        if (lb) lb.addEventListener("click", handleLogin);

        const tb = totpBtn();
        if (tb) tb.addEventListener("click", handleTotp);

        // TOTP enter-key
        const ti = totpInput();
        if (ti) ti.addEventListener("keydown", (e) => {
            if (e.key === "Enter") handleTotp();
        });

        const passField = passwordInput();
        if (passField) passField.addEventListener("keydown", (e) => {
            if (e.key === "Enter") handleLogin();
        });

        // Broker selector: re-apply the field wording when it changes.
        const bs = brokerSelect();
        if (bs) bs.addEventListener("change", applyBrokerFields);

        // logout
        const lo = logoutBtn();
        if (lo) lo.addEventListener("click", handleLogout);

        // remember-session toggles (either checkbox drives the same setting)
        for (const el of [rememberToggleCred(), rememberToggleAuth()]) {
            if (el) el.addEventListener("change", handleRememberChange);
        }

        // Register globally so broker_status.js and toasts can call it.
        window.BrokerAuthUI = { open, close };
    }

    // Auto-init when the overlay markup is present.
    if (document.getElementById("broker-auth-overlay")) {
        init();
    }

    return { open, close };
})();
