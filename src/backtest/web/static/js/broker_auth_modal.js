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

    function showCredentials() {
        setTitle("🔐 mStock Login");
        showView("credentials");
        if (credError()) credError().textContent = "";
        if (usernameInput()) usernameInput().focus();
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
            const result = await postJSON("/api/broker/login", { username, password });
            if (result.success && result.requires_totp) {
                showTotp();
            } else if (result.success) {
                // login succeeded but no TOTP required — go straight to authenticated
                await refreshAndShowAuth();
            } else {
                // show error but don't call showCredentials() which clears it
                if (credError()) credError().textContent = result.message || "Login failed";
                setTitle("🔐 mStock Login");
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
        setTitle("🔐 mStock Login");
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
            const result = await postJSON("/api/broker/verify-totp", { totp_code: code });
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

    // ---- view: authenticated (Step 3) --------------------------------------

    function showAuthenticated() {
        const status = BrokerStatus && BrokerStatus.get();
        const name = (status && status.broker_display_name) || "mStock";

        setTitle(`🟢 ${name} Connected`);
        showView("auth");

        if (brokerNameEl()) brokerNameEl().textContent = name;
        if (expiresEl()) {
            expiresEl().textContent = formatExpiry(status && status.expires_at);
        }
    }

    async function refreshAndShowAuth() {
        if (BrokerStatus && typeof BrokerStatus.refresh === "function") {
            await BrokerStatus.refresh();
        }
        showAuthenticated();
        // Auto-close modal after brief delay so user sees success then continues
        setTimeout(() => close(), 1500);
    }

    // ---- logout ------------------------------------------------------------

    async function handleLogout() {
        if (BrokerStatus && typeof BrokerStatus.expectLogout === "function") {
            BrokerStatus.expectLogout();
        }
        try {
            await postJSON("/api/broker/logout", {});
        } catch (err) {
            // ignore — we're clearing UI state regardless
        }
        if (BrokerStatus && typeof BrokerStatus.refresh === "function") {
            await BrokerStatus.refresh();
        }
        showCredentials();
    }

    // ---- open / close ------------------------------------------------------

    function open() {
        const ov = overlay();
        if (!ov) return;
        ov.classList.add("open");

        // Seed the correct view based on current auth state.
        const state = BrokerStatus && BrokerStatus.state();
        if (state === "authenticated" || state === "expiring_soon") {
            showAuthenticated();
        } else {
            showCredentials();
        }
    }

    function close() {
        const ov = overlay();
        if (!ov) return;
        ov.classList.remove("open");
        // Reset to credentials view so next open starts fresh.
        showCredentials();
        if (credError()) credError().textContent = "";
        if (totpError()) totpError().textContent = "";
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

        // Credentials enter-key
        const pi = passwordInput();
        if (pi) pi.addEventListener("keydown", (e) => {
            if (e.key === "Enter") handleLogin();
        });

        // logout
        const lo = logoutBtn();
        if (lo) lo.addEventListener("click", handleLogout);

        // Register globally so broker_status.js and toasts can call it.
        window.BrokerAuthUI = { open, close };
    }

    // Auto-init when the overlay markup is present.
    if (document.getElementById("broker-auth-overlay")) {
        init();
    }

    return { open, close };
})();
