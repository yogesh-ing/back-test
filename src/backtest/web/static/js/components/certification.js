/**
 * Certification readiness — the §5 traffic light.
 *   Certification.renderInto("certification", readiness)
 *
 * The panel makes no decision. It shows eight checks, says which passed,
 * which failed, and — the part that carries the weight — which could not be
 * evaluated at all.
 *
 * "Unknown" is a real state here, not a gap in the design. A Quick-Screen run
 * has no cost shock, and a run with two closed trades has no Monte Carlo to
 * speak of. Rendering those as green would hand the reader a tick they did not
 * earn on the most promotional panel on the page; rendering them red would
 * penalise the same underlying fact twice. So they render neutrally, carry the
 * reason, and never count as a pass. The header counts them out loud for the
 * same reason: a panel that quietly omitted three checks would read as a
 * complete result.
 *
 * Advisory only. Nothing here disables a button or blocks a navigation.
 */
(function (global) {
    "use strict";

    const ICON = { green: "✅", yellow: "⚠️", red: "❌", unknown: "⬜" };
    const VERDICT = {
        pass: ["cmp-ready-pass", "All eight checks passed"],
        fail: ["cmp-ready-fail", "One or more checks failed"],
        incomplete: ["cmp-ready-part", "No failures, but not everything was proven"],
    };

    function esc(v) {
        return String(v === null || v === undefined ? "" : v)
            .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
    }

    function renderCheck(c) {
        const icon = ICON[c.status] || ICON.unknown;
        const detail = c.detail ? `<p class="cmp-ready-detail">${esc(c.detail)}</p>` : "";
        return `<div class="cmp-ready-row cmp-ready-${esc(c.status)}">`
            + `<span class="cmp-ready-icon" aria-hidden="true">${icon}</span>`
            + `<span class="cmp-ready-label">${esc(c.label)}</span>`
            + `<span class="cmp-ready-value">${esc(c.value)}</span>`
            + `</div>${detail}`;
    }

    function render(readiness) {
        if (!readiness || typeof readiness !== "object") return "";
        const checks = readiness.checks || [];
        if (!checks.length) return "";
        const counts = readiness.counts || {};
        const [cls, headline] = VERDICT[readiness.verdict] || VERDICT.incomplete;

        // Spell the unaccounted-for checks out. A reader who sees "6/8" and
        // nothing else has to guess what the other two were.
        const tally = [
            counts.green ? `${counts.green} passed` : null,
            counts.yellow ? `${counts.yellow} weak` : null,
            counts.red ? `${counts.red} failed` : null,
            counts.unknown ? `${counts.unknown} unproven` : null,
        ].filter(Boolean).join(" · ");

        const unproven = (readiness.unproven || []).length
            ? `<p class="cmp-ready-note">Not evaluated: ${esc((readiness.unproven || []).join(", "))}. `
              + `A check that could not run is not a check that passed.</p>`
            : "";

        return `<div class="cmp-ready" data-verdict="${esc(readiness.verdict)}">`
            + `<div class="cmp-ready-head ${cls}">`
            + `<span class="cmp-ready-title">Certification readiness</span>`
            + `<span class="cmp-ready-tally">${esc(tally)}</span>`
            + `</div>`
            + `<p class="cmp-ready-headline ${cls}">${esc(headline)}</p>`
            + `<div class="cmp-ready-grid">${checks.map(renderCheck).join("")}</div>`
            + unproven
            + `<p class="cmp-ready-summary">${esc(readiness.summary)}</p>`
            + `<p class="cmp-ready-advisory">Advisory only. This panel does not make the `
            + `decision and blocks nothing — the hard gates live in Optimize and in the `
            + `Paper&nbsp;&rarr;&nbsp;Live gate.</p>`
            + `</div>`;
    }

    const Certification = {
        render,
        renderInto(containerId, readiness) {
            const el = document.getElementById(containerId);
            if (el) el.innerHTML = render(readiness);
        },
    };

    global.Certification = Certification;
    if (typeof module !== "undefined" && module.exports) module.exports = Certification;
}(typeof window !== "undefined" ? window : globalThis));
