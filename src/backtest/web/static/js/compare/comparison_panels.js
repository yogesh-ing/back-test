/**
 * Cross-strategy comparison panels (PRD backTest-enhance §4.3 / §4.4).
 *
 *   ComparePanels.renderInto("comparePanels", comparison)
 *
 * Two panels, both answering a question the metrics table cannot:
 *
 * • **Correlation** — are these four rows four different bets, or the same bet
 *   four times? Green means diversifying, red means redundant. §4.3 flags
 *   anything above 0.8, and the panel says so in words as well as colour:
 *   a red square on its own does not tell a reader whether to do anything.
 * • **Significance** — is the top row actually better, or just the luckiest of
 *   the set? This is the panel that stops "promote the winner" becoming
 *   "promote whichever one got lucky", and it is explicitly informational —
 *   nothing here gates anything.
 *
 * The server owns every verdict; this file only renders. Notably it does NOT
 * second-guess the significance call: showing "probably different" next to a
 * server verdict of "no significant difference" would be inventing a claim.
 */
(function (global) {
    "use strict";

    const HIGH_CORRELATION = 0.8;

    function missing(v) { return v === null || v === undefined || v === ""; }

    function num(v) { return typeof v === "number" && isFinite(v) ? v.toFixed(2) : null; }

    function esc(v) {
        return String(v === null || v === undefined ? "" : v).replace(/[&<>"']/g, (c) => (
            { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
        ));
    }

    /** Green at 0 (fully diversifying), red at 1 (fully redundant). */
    function heatColor(c) {
        if (c === null) return "cmp-cell-flat";
        const abs = Math.abs(c);
        if (abs >= 0.8) return "cmp-cell-hot";
        if (abs >= 0.5) return "cmp-cell-warm";
        if (abs >= 0.25) return "cmp-cell-mild";
        return "cmp-cell-cool";
    }

    function banner(w) {
        const cls = w.level === "error" ? "cmp-banner-error"
            : w.level === "warning" ? "cmp-banner-warn" : "cmp-banner-info";
        return `<div class="cmp-banner ${cls}" role="alert">${esc(w.message)}</div>`;
    }

    function unavailable(reason) {
        return `<p class="cmp-unavailable">${esc(reason || "not available")}</p>`;
    }

    /** Split a "strategy · SYMBOL (params)" label into its three parts. */
    function parseLabel(label) {
        const m = String(label).match(/^\s*([^·]*?)\s*(?:·\s*([^·(]*?)\s*)?(?:\(([^)]*)\))?\s*$/);
        if (!m) return { strategy: String(label), symbol: "", params: "" };
        return {
            strategy: (m[1] || "").trim(),
            symbol: (m[2] || "").trim(),
            params: (m[3] || "").trim(),
        };
    }

    /** Shorten one label to something cell-sized, keeping the named parts. */
    function shortLabel(label, keep) {
        const p = parseLabel(label);
        const picked = [];
        if (keep.strategy) picked.push(p.strategy);
        if (keep.symbol) picked.push(p.symbol);
        if (keep.params) picked.push(p.params);
        return picked.filter(Boolean).join(" ") || p.strategy || String(label);
    }

    /**
     * Decide which parts of the label actually distinguish the slots.
     *
     * A column header that reads "INFY" four times tells the reader nothing, so
     * any part that is identical across every slot is dropped — including the
     * strategy name in Test Generalization, where the whole point is that the
     * one strategy is held constant. Compare Strategies keeps strategy+params
     * because that pair is what differs.
     */
    function axisLabels(labels) {
        const parts = (labels || []).map(parseLabel);
        const distinct = (pick) => new Set(parts.map(pick).filter(Boolean)).size;
        const show = {
            strategy: distinct((p) => p.strategy) > 1,
            symbol: distinct((p) => p.symbol) > 1,
            params: distinct((p) => p.params) > 1,
        };
        // Nothing distinguishes them, so fall back to naming the strategy
        // rather than emitting four blank headers.
        if (!show.strategy && !show.symbol && !show.params) show.strategy = true;
        return parts.map((p) => shortLabel(
            `${p.strategy}${p.symbol ? " · " + p.symbol : ""}${p.params ? " (" + p.params + ")" : ""}`,
            show,
        ));
    }

    // ------------------------------------------------------------------
    // §4.3 Correlation heatmap
    // ------------------------------------------------------------------

    function renderCorrelation(cm) {
        if (!cm) return unavailable();
        if (!cm.available) return unavailable(cm.reason);
        const labels = cm.labels || [];
        const axis = axisLabels(labels);
        const matrix = cm.matrix || [];

        const head = axis.map((a, i) => `<th class="cmp-ax" title="${esc(labels[i])}">${esc(a)}</th>`).join("");
        const rows = labels.map((label, i) => {
            const cells = (matrix[i] || []).map((c, j) => {
                const self = i === j;
                return `<td class="cmp-cell ${self ? "cmp-cell-self" : heatColor(c)}" `
                    + `title="${esc(labels[j])} vs ${esc(label)}: ${c === null ? "undefined (no variation)" : c.toFixed(3)}">`
                    + `${self ? "—" : (c === null ? "n/a" : c.toFixed(2))}</td>`;
            }).join("");
            return `<tr><th class="cmp-ax" title="${esc(label)}">${esc(axis[i])}</th>${cells}</tr>`;
        }).join("");

        const high = cm.high_correlation_pairs || [];
        const verdict = high.length
            ? `<div class="cmp-verdict cmp-warn">${high.length} of the ${cm.pairs.length} pairs `
              + `are above ${HIGH_CORRELATION} correlation. Holding them together is `
              + `roughly one position, not ${labels.length}.</div>`
            : `<div class="cmp-verdict cmp-ok">No pair above ${HIGH_CORRELATION} correlation `
              + `— these are genuinely different bets.</div>`;

        return verdict
            + (cm.warnings || []).map(banner).join("")
            + `<table class="cmp-heatmap"><thead><tr><th class="cmp-ax"></th>${head}</tr></thead>`
            + `<tbody>${rows}</tbody></table>`
            + `<p class="cmp-footnote">Computed over the ${cm.aligned_bars} bars every `
            + `result shares, so every cell is measured on the same evidence. `
            + `&ldquo;n/a&rdquo; means one of the pair never moved, so its correlation is undefined `
            + `&mdash; not zero.</p>`;
    }

    // ------------------------------------------------------------------
    // §4.4 Statistical significance
    // ------------------------------------------------------------------

    function renderSignificance(sg) {
        if (!sg) return unavailable();
        if (!sg.available) return unavailable(sg.reason);

        const axis = axisLabels(sg.labels || (sg.comparisons || []).flatMap((c) => [c.a, c.b])
            .filter((v, i, arr) => arr.indexOf(v) === i));
        const named = (label) => {
            const i = (sg.labels || []).indexOf(label);
            return i >= 0 && axis[i] ? axis[i] : shortLabel(label, { params: true });
        };
        const rows = (sg.comparisons || []).map((c) => {
            const better = c.a_better_pct;
            let verdict, cls;
            if (c.verdict === "a_better") {
                verdict = `${named(c.a)} is likely better (${better.toFixed(0)}% confidence)`;
                cls = "cmp-ok";
            } else if (c.verdict === "b_better") {
                verdict = `${named(c.b)} is likely better (${c.b_better_pct.toFixed(0)}% confidence)`;
                cls = "cmp-ok";
            } else {
                verdict = "No significant difference";
                cls = "cmp-muted";
            }
            return `<tr>`
                + `<td class="cmp-pair" title="${esc(c.a)}"><span class="cmp-dot cmp-dot-a"></span>${esc(named(c.a))}</td>`
                + `<td class="cmp-pair" title="${esc(c.b)}"><span class="cmp-dot cmp-dot-b"></span>${esc(named(c.b))}</td>`
                + `<td class="num">${esc(num(c.observed_sharpe_gap))}</td>`
                + `<td class="${cls}">${esc(verdict)}</td>`
                + `</tr>`;
        }).join("");

        return (sg.warnings || []).map(banner).join("")
            + `<table class="cmp-sig"><thead><tr><th>A</th><th>B</th><th class="num">Sharpe gap</th>`
            + `<th>Verdict</th></tr></thead><tbody>${rows}</tbody></table>`
            + `<p class="cmp-footnote">Paired bootstrap over ${sg.simulations} resamples of the `
            + `${sg.aligned_bars} shared bars. Every strategy sees the <em>same</em> drawn market `
            + `days, so a difference in the Sharpe distributions comes from how each one traded, `
            + `not from resampling the market differently. Informational only — this does not gate `
            + `anything.</p>`;
    }

    // ------------------------------------------------------------------

    function render(container, comparison) {
        if (!container) return;
        if (!comparison || typeof comparison !== "object") { container.innerHTML = ""; return; }
        const cm = comparison.correlation;
        const sg = comparison.significance;
        const excluded = comparison.excluded || [];

        const skipped = excluded.length
            ? `<details class="cmp-panel cmp-excluded"><summary class="cmp-head">`
              + `Not included in the comparison (${excluded.length})</summary>`
              + `<div class="cmp-body">`
              + excluded.map((e) => `<div class="cmp-row"><span class="cmp-label">${esc(e.label)}</span>`
                + `<span class="cmp-bad">${esc(e.reason)}</span></div>`).join("")
              + `<p class="cmp-footnote">A slot that failed to run is named here rather than `
              + `dropped — a quietly shorter comparison is worse than a visibly incomplete one.</p>`
              + `</div></details>`
            : "";

        container.innerHTML =
            `<details class="cmp-panel" data-panel="correlation" open>`
            + `<summary class="cmp-head">Inter-Strategy Correlation</summary>`
            + `<div class="cmp-body">${renderCorrelation(cm)}</div></details>`
            + `<details class="cmp-panel" data-panel="significance">`
            + `<summary class="cmp-head">Statistical Significance</summary>`
            + `<div class="cmp-body">${renderSignificance(sg)}</div></details>`
            + skipped;
    }

    const ComparePanels = {
        render,
        renderInto(containerId, comparison) {
            render(document.getElementById(containerId), comparison);
        },
        heatColor,
        parseLabel,
        shortLabel,
        axisLabels,
    };

    global.ComparePanels = ComparePanels;
    if (typeof module !== "undefined" && module.exports) module.exports = ComparePanels;
}(typeof window !== "undefined" ? window : globalThis));
