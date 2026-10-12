/**
 * Provenance badges (PRD backTest-enhance Part 1 §1.1 + §1.2).
 *
 *   renderProvenance(containerId, provenance)
 *
 * Every result page shows, permanently and non-dismissably, which ENGINE and
 * which DATA produced the numbers. A Sharpe is not a claim on its own — the
 * claim is "this Sharpe, from this engine, on this data, over these bars".
 * The server is the single authority for the wording (payload.warnings), so
 * this file only maps severity -> class and renders.
 *
 * Renders nothing at all when there is no provenance, so a page that has not
 * run yet is unchanged.
 */
(function (global) {
    "use strict";

    const TIER_CLASS = {
        canonical: "prov-badge-ok",
        approximate: "prov-badge-warn",
        mixed: "prov-badge-warn",
        options: "prov-badge-info",
        unknown: "prov-badge-warn",
    };

    const LEVEL_CLASS = { error: "prov-banner-error", warning: "prov-banner-warn", info: "prov-banner-info" };

    function esc(v) {
        return String(v === null || v === undefined ? "" : v).replace(/[&<>"']/g, (c) => (
            { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
        ));
    }

    /** Human date range, or "" when the run did not report one. */
    function rangeLabel(p) {
        const r = p.date_range || {};
        const from = r.from || p.data_from;
        const to = r.to || p.data_to;
        if (!from && !to) return "";
        if (!from || !to) return esc(from || to);
        return from === to ? esc(from) : `${esc(from)} → ${esc(to)}`;
    }

    /** Badge tooltip — the detail is in the title so the row stays one line. */
    function engineTitle(p) {
        const bits = [`engine: ${p.engine_used || "?"}`];
        if (p.symbol) bits.push(`symbol: ${p.symbol}`);
        if (p.timeframe) bits.push(`timeframe: ${p.timeframe}`);
        if (p.bars_count) bits.push(`bars: ${p.bars_count}`);
        return esc(bits.join(" · "));
    }

    function dataTitle(p) {
        const bits = [`source: ${p.data_source || "?"}`];
        const range = rangeLabel(p);
        if (range) bits.push(`range: ${range.replace(/&amp;/g, "&")}`);
        if (p.data_fetch_date) bits.push(`fetched: ${p.data_fetch_date}`);
        return esc(bits.join(" · "));
    }

    function render(container, p) {
        if (!container) return;
        if (!p || typeof p !== "object") { container.innerHTML = ""; return; }

        const tier = p.engine_tier || (p.engine_canonical ? "canonical" : "unknown");
        const badges = [
            `<span class="prov-badge ${TIER_CLASS[tier] || TIER_CLASS.unknown}" title="${engineTitle(p)}">`
            + `Engine: ${esc(p.engine_label || "Unknown")}</span>`,
            `<span class="prov-badge ${p.data_source_real ? "prov-badge-ok" : "prov-badge-error"}" `
            + `title="${dataTitle(p)}">Data: ${esc(p.data_source_label || "Unknown")}</span>`,
        ];
        const range = rangeLabel(p);
        if (range) {
            badges.push(`<span class="prov-badge prov-badge-plain" title="Requested range">${range}</span>`);
        }
        if (p.bars_count) {
            badges.push(`<span class="prov-badge prov-badge-plain" title="Bars in the run">${p.bars_count} bars</span>`);
        }

        const warnings = Array.isArray(p.warnings) ? p.warnings : [];
        const banners = warnings.map((w) => (
            `<div class="prov-banner ${LEVEL_CLASS[w.level] || LEVEL_CLASS.info}" role="alert">${esc(w.message)}</div>`
        )).join("");

        container.innerHTML = `<div class="prov-badges">${badges.join("")}</div>${banners}`;
    }

    const Provenance = {
        render,
        /** Convenience: resolve the container by id. */
        renderInto(containerId, provenance) {
            render(document.getElementById(containerId), provenance);
        },
        esc,
    };

    global.Provenance = Provenance;
    if (typeof module !== "undefined" && module.exports) module.exports = Provenance;
}(typeof window !== "undefined" ? window : globalThis));
