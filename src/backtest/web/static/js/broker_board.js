/**
 * Broker Board — multi-broker connection grid (PRD-001 Phase A, UI-SPEC-001).
 *
 * A grid of per-broker status cards driven by GET /api/broker/status
 * (`sessions` map). Multiple cards can be Authenticated simultaneously:
 *
 *   ● mStock  Authenticated  Expires 18:32   [Logout] [Reconcile]
 *   ● Dhan    Authenticated  Expires 23:45   [Logout] [Reconcile]
 *   ○ ...     Not connected                  [Login]
 *
 * Each card also lists the segments served by that broker (Phase B —
 * loaded from GET /api/segments when available).
 *
 * Integration:
 *   window.BrokerBoard.open()  — header status strip click lands here
 *   BrokerAuthUI.open({broker}) — per-card [Login]
 *   POST /api/broker/logout {broker} — per-card [Logout]
 *   POST /api/broker/<name>/reconcile — per-card [Reconcile] (live sessions)
 */
const BrokerBoard = (() => {
    const overlay = () => document.getElementById("broker-board-overlay");
    const grid = () => document.getElementById("broker-board-grid");

    const DOTS = {
        authenticated: "🟢",
        expiring_soon: "🟡",
        expired: "🔴",
        unauthenticated: "⚪",
    };

    let segmentsCache = null; // [{name, display_name, broker, mode, allocated_capital}]

    function fmtTime(iso) {
        if (!iso) return "—";
        const d = new Date(iso);
        if (Number.isNaN(d.getTime())) return iso;
        const now = new Date();
        const isToday = d.toDateString() === now.toDateString();
        const datePart = isToday ? "Today" : d.toLocaleDateString("en-IN", { month: "short", day: "numeric" });
        const timePart = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
        return `${datePart} ${timePart} IST`;
    }

    function fmtCapital(v) {
        if (v == null || Number.isNaN(Number(v))) return "";
        const n = Number(v);
        if (n >= 10000000) return `₹${(n / 10000000).toFixed(n % 10000000 ? 1 : 0)}Cr`;
        if (n >= 100000) return `₹${(n / 100000).toFixed(n % 100000 ? 1 : 0)}L`;
        return `₹${n.toLocaleString("en-IN")}`;
    }

    async function loadSegments() {
        if (segmentsCache !== null) return segmentsCache;
        try {
            const resp = await fetch("/api/segments");
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            const data = await resp.json();
            segmentsCache = (data && data.segments) || [];
        } catch (err) {
            segmentsCache = []; // segments API not available — cards render without
        }
        return segmentsCache;
    }

    function segmentsFor(brokerName) {
        return (segmentsCache || []).filter((s) => s.broker === brokerName);
    }

    function statusLabel(row) {
        if (!row) return "Not connected";
        if (row.status === "authenticated") return "Authenticated";
        if (row.status === "expiring_soon") return "Expiring soon";
        if (row.status === "expired") return "Session expired";
        return "Not connected";
    }

    function card(row) {
        const name = row.broker;
        const live = !!row.authenticated;
        const dot = DOTS[row.status] || DOTS.unauthenticated;
        const el = document.createElement("div");
        el.className = `broker-card ${live ? "broker-card-live" : ""}`;
        el.dataset.broker = name;

        const segs = segmentsFor(name);
        const segHtml = segs.length
            ? `<div class="broker-card-segments"><span class="muted">Segments:</span>${segs
                  .map(
                      (s) =>
                          `<div class="broker-card-segment">• ${s.display_name || s.name}` +
                          ` <span class="muted">(${s.mode === "live" ? "Live" : "Paper"}` +
                          `${s.allocated_capital ? ", " + fmtCapital(s.allocated_capital) : ""})</span></div>`
                  )
                  .join("")}</div>`
            : "";

        el.innerHTML = `
            <div class="broker-card-head">
                <span class="broker-card-dot">${dot}</span>
                <span class="broker-card-name">${row.broker_display_name || name}</span>
                ${row.ui_active ? '<span class="broker-card-active-tag" title="UI focus (display only)">UI</span>' : ""}
            </div>
            <div class="broker-card-status">${statusLabel(row)}</div>
            <div class="broker-card-expiry muted">${live ? `Expires: ${fmtTime(row.expires_at)}` : "&nbsp;"}</div>
            ${row.remembered ? '<div class="broker-card-remembered muted">💾 remembered today</div>' : ""}
            ${segHtml}
            <div class="broker-card-actions"></div>
            <div class="broker-card-msg muted" hidden></div>
        `;

        const actions = el.querySelector(".broker-card-actions");
        if (live) {
            const logoutBtn = document.createElement("button");
            logoutBtn.className = "btn btn-danger btn-small";
            logoutBtn.textContent = "Logout";
            logoutBtn.addEventListener("click", () => handleLogout(name, el));
            actions.appendChild(logoutBtn);

            const reconcileBtn = document.createElement("button");
            reconcileBtn.className = "btn btn-ghost btn-small";
            reconcileBtn.textContent = "Reconcile";
            reconcileBtn.title = "Fetch open orders from the broker and compare with platform state";
            reconcileBtn.addEventListener("click", () => handleReconcile(name, el));
            actions.appendChild(reconcileBtn);
        } else {
            const loginBtn = document.createElement("button");
            loginBtn.className = "btn btn-primary btn-small";
            loginBtn.textContent = "Login";
            loginBtn.addEventListener("click", () => {
                close();
                if (window.BrokerAuthUI && typeof window.BrokerAuthUI.open === "function") {
                    window.BrokerAuthUI.open({ broker: name });
                }
            });
            actions.appendChild(loginBtn);
        }
        return el;
    }

    function showMsg(cardEl, text, isError) {
        const msg = cardEl && cardEl.querySelector(".broker-card-msg");
        if (!msg) return;
        msg.hidden = false;
        msg.textContent = text;
        msg.classList.toggle("broker-card-msg-error", !!isError);
    }

    async function handleLogout(name, cardEl) {
        if (window.BrokerStatus && typeof BrokerStatus.expectLogout === "function") {
            BrokerStatus.expectLogout();
        }
        try {
            await fetch("/api/broker/logout", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ broker: name }),
            });
        } catch (err) { /* re-render reflects reality either way */ }
        if (window.BrokerStatus && typeof BrokerStatus.refresh === "function") {
            await BrokerStatus.refresh();
        }
        render();
    }

    async function handleReconcile(name, cardEl) {
        showMsg(cardEl, "Reconciling…", false);
        try {
            const resp = await fetch(`/api/broker/${encodeURIComponent(name)}/reconcile`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: "{}",
            });
            const data = await resp.json();
            if (!resp.ok || data.success === false) {
                showMsg(cardEl, data.message || data.error || "Reconcile failed", true);
                return;
            }
            const r = data.result || data;
            const parts = [];
            if (r.checked != null) parts.push(`${r.checked} checked`);
            if (r.settled != null) parts.push(`${r.settled} settled`);
            if (r.mismatched != null) parts.push(`${r.mismatched} mismatched`);
            showMsg(cardEl, `Reconciled: ${parts.join(", ") || "ok"}`, (r.mismatched || 0) > 0);
        } catch (err) {
            showMsg(cardEl, "Reconcile failed — connection error", true);
        }
    }

    async function render() {
        const g = grid();
        if (!g) return;
        let payload = null;
        try {
            const resp = await fetch("/api/broker/status");
            payload = await resp.json();
        } catch (err) {
            g.innerHTML = '<div class="muted">Broker status unavailable.</div>';
            return;
        }
        await loadSegments();
        const sessions = (payload && payload.sessions) || {};
        const names = Object.keys(sessions);
        g.innerHTML = "";
        if (!names.length) {
            g.innerHTML = '<div class="muted">No brokers registered.</div>';
            return;
        }
        for (const name of names) g.appendChild(card(sessions[name]));
    }

    function open() {
        const ov = overlay();
        if (!ov) return;
        segmentsCache = null; // refresh segment mapping on every open
        ov.classList.add("open");
        render();
    }

    function close() {
        const ov = overlay();
        if (ov) ov.classList.remove("open");
    }

    function init() {
        const ov = overlay();
        if (!ov) return;
        const closeBtn = document.getElementById("broker-board-close");
        if (closeBtn) closeBtn.addEventListener("click", close);
        const refreshBtn = document.getElementById("broker-board-refresh");
        if (refreshBtn) refreshBtn.addEventListener("click", () => { segmentsCache = null; render(); });
        ov.addEventListener("click", (e) => { if (e.target === ov) close(); });
        document.addEventListener("keydown", (e) => {
            if (e.key === "Escape" && ov.classList.contains("open")) close();
        });
        // Re-render the board whenever a fresh status poll lands while open.
        document.addEventListener("broker:status", () => {
            if (ov.classList.contains("open")) render();
        });
        window.BrokerBoard = { open, close, render };
    }

    if (document.getElementById("broker-board-overlay")) init();

    return { open, close, render };
})();
