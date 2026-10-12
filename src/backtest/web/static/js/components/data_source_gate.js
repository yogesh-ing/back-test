/**
 * Data-source gate — say what the run is measured on.
 *
 *   DataSourceGate.mount("dataSourceGate", { blockIds: ["runBtn"] })
 *
 * PRD change of direction (2026-09-30): the app RESOLVES its source before
 * the page renders (app.resolve_source falls back to the best enabled
 * source), so the disabled-synthetic case reaches the browser as an allowed
 * fallback. This component therefore shows three states:
 *
 *   • allowed           → a quiet green badge naming the source
 *   • allowed + fell_back → same green badge, plus one line saying what was
 *                          requested instead — informational, never a ⛔
 *                          banner, never blocking (a banner punishing an
 *                          operator for a deliberate config choice taught
 *                          them to ignore it)
 *   • NOT allowed        → the only blocking state: no enabled source exists
 *                          at all. The server 409 remains the authority.
 */
(function (global) {
    "use strict";

    function esc(v) {
        return String(v === null || v === undefined ? "" : v)
            .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
    }

    function labelFor(status) {
        const row = (status.sources || []).find((s) => s.name === status.active);
        if (row && row.label) return row.label;
        return status.active || "unknown";
    }

    function renderInto(el, status) {
        if (!el) return null;
        if (!status) { el.innerHTML = ""; return null; }

        if (status.allowed) {
            const cert = status.certifiable
                ? '<span class="pos">certification-grade</span>'
                : '<span class="muted">not certification-grade</span>';
            // A fallback is stated in one quiet line under the badge —
            // provenance the operator can see, without alarm styling.
            const fallback = status.fell_back
                ? `<div class="dsg-fallback muted small">Requested source '${esc(status.requested)}' is disabled — running on '${esc(labelFor(status))}' instead.</div>`
                : '';
            el.innerHTML = `<div class="dsg dsg--ok">Data source:
                <strong>${esc(labelFor(status))}</strong> · ${cert}</div>${fallback}`;
            return true;
        }

        el.innerHTML = `<div class="dsg dsg--blocked" role="alert">
            <div class="dsg-title">⛔ Data source disabled</div>
            <p class="dsg-body">${esc(status.refusal || "This source is not available.")}</p>
            <p class="dsg-hint muted small">Runs on this page are refused until an enabled source is
                configured. Change <code>config/data_sources.yaml</code> and restart.</p>
        </div>`;
        return false;
    }

    /**
     * Disable the given control ids while the source is disabled, and put the
     * reason on the title so a hover explains the dead button.
     */
    function blockControls(ids, reason) {
        let blocked = 0;
        (ids || []).forEach((id) => {
            const node = document.getElementById(id);
            if (!node || node.dataset.dsgPinned === "1") return;
            node.dataset.dsgPinned = "1";
            node.dataset.dsgWasDisabled = node.disabled ? "1" : "";
            node.disabled = true;
            node.title = reason || "Data source disabled";
            node.classList.add("dsg-disabled");
            blocked += 1;
        });
        return blocked;
    }

    function unblockControls(ids) {
        (ids || []).forEach((id) => {
            const node = document.getElementById(id);
            if (!node || node.dataset.dsgPinned !== "1") return;
            // Only restore what we actually changed: several of these buttons
            // are disabled for their own reasons until a strategy is picked.
            node.disabled = node.dataset.dsgWasDisabled === "1";
            delete node.dataset.dsgPinned;
            delete node.dataset.dsgWasDisabled;
            node.removeAttribute("title");
            node.classList.remove("dsg-disabled");
        });
    }

    function parseStatus(raw) {
        if (!raw) return null;
        if (typeof raw === "object") return raw;
        try { return JSON.parse(raw); } catch (e) { return null; }
    }

    const DataSourceGate = {
        esc,
        parseStatus,
        labelFor,
        renderInto,
        blockControls,
        unblockControls,
        /**
         * @param {string} containerId  where to render the banner
         * @param {object} opts         {status, blockIds}
         * @returns {boolean|null} true when allowed, false when blocked,
         *   null when the server did not say — in which case nothing is
         *   blocked and the 409 remains the authority.
         */
        mount(containerId, opts) {
            opts = opts || {};
            const status = parseStatus(opts.status);
            const el = document.getElementById(containerId);
            if (!status) {
                // Fail open *here*, deliberately. The server is the real
                // control; this component only makes its answer visible.
                // Blocking on a missing attribute would put a dead Run button
                // under a tooltip reading "undefined" on any page that forgot
                // to pass the status — a far worse failure than an unstyled
                // refusal.
                if (el) el.innerHTML = "";
                return null;
            }
            const allowed = renderInto(el, status);
            if (allowed) {
                unblockControls(opts.blockIds);
            } else {
                blockControls(opts.blockIds, status.refusal);
            }
            return allowed === true;
        },
    };

    global.DataSourceGate = DataSourceGate;
    if (typeof module !== "undefined" && module.exports) module.exports = DataSourceGate;
}(typeof window !== "undefined" ? window : globalThis));
