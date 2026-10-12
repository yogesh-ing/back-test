/**
 * Single-run checks (PRD backTest-enhance §3) — benchmark, cost shock,
 * Monte Carlo.
 *
 *   RunChecks.renderInto("runChecks", {benchmark, cost_shock, monte_carlo, metrics})
 *
 * Three panels that each qualify the headline numbers above them, so they go
 * BELOW the metric sections and are collapsed by default: a reader who wants
 * them opens them, and a reader who doesn't is not buried.
 *
 * Two honesty rules run through all three panels:
 *
 * 1. **Absence is stated, never implied.** A check that could not run says why
 *    in its own row. A silently missing panel reads as a panel that passed.
 * 2. **A number the server flagged as degenerate is not dressed up.** The
 *    cost-shock base and the Monte Carlo reorder/bootstrap split are both
 *    carried through to the UI rather than hidden; see RENDERING NOTES below.
 *
 * RENDERING NOTES — two payload facts the UI must not flatten:
 *
 * • `cost_shock.base_bps_source === "default"` means the run itself was
 *   frictionless, so the stress base is the 5 bps NSE default and the cards
 *   above were produced at 0 bps. The panel says so; printing "Base (0.05%)"
 *   without that context would imply the run was costed.
 * • `monte_carlo.reorder` and `.bootstrap` are DIFFERENT experiments. Under
 *   `reorder` the final equity is mathematically invariant (a shuffle does not
 *   change a sum), so only the drawdown spread is meaningful there. The panel
 *   therefore shows the final-equity distribution from `bootstrap` and labels
 *   the `reorder` numbers as path-only.
 */
(function (global) {
    "use strict";

    const STATUS_CLASS = {
        robust: "runcheck-ok",
        fragile: "runcheck-warn",
        broken: "runcheck-error",
        insufficient_trades: "runcheck-warn",
    };

    const STATUS_TEXT = {
        robust: "Edge survives 3× slippage",
        fragile: "Fragile to execution costs",
        broken: "Edge is not robust",
        insufficient_trades: "Too few trades to judge",
    };

    const LEVEL_CLASS = { error: "runcheck-banner-error", warning: "runcheck-banner-warn", info: "runcheck-banner-info" };

    const INSUFFICIENT_BANNER =
        "Fewer than 20 closed trades — all metrics have very high statistical uncertainty. "
        + "Run on a longer date range or different symbol before drawing conclusions.";

    // ------------------------------------------------------------------
    // Formatters. `text`/`num` return null when there is genuinely nothing to
    // say, so a row is dropped rather than printed as a confident 0.00.
    // ------------------------------------------------------------------

    function missing(v) { return v === null || v === undefined || v === ""; }

    function asNumber(v) {
        const n = Number(v);
        return missing(v) || typeof v === "boolean" || v === "" || !isFinite(n) ? null : n;
    }

    function num(v) { const n = asNumber(v); return n === null ? (missing(v) ? null : String(v)) : n.toFixed(2); }
    function pct(v) { const n = asNumber(v); return n === null ? (missing(v) ? null : String(v)) : `${n.toFixed(2)}%`; }
    function money(v) {
        const n = asNumber(v);
        if (n === null) return missing(v) ? null : String(v);
        return (typeof Money !== "undefined") ? Money.signed(n) : n.toFixed(2);
    }
    function esc(v) {
        return String(v === null || v === undefined ? "" : v).replace(/[&<>"']/g, (c) => (
            { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
        ));
    }

    /** Wrap a panel body in a collapsed <details>, or "" if there is nothing. */
    function panel(id, title, note, bodyHtml, open) {
        if (!bodyHtml) return "";
        return `<details class="runcheck-panel" data-panel="${esc(id)}"${open ? " open" : ""}>`
            + `<summary class="runcheck-head">${esc(title)}</summary>`
            + (note ? `<p class="runcheck-note">${esc(note)}</p>` : "")
            + `<div class="runcheck-body">${bodyHtml}</div>`
            + `</details>`;
    }

    /** A short "why this is empty" line — used instead of rendering nothing. */
    function unavailable(reason) {
        return `<p class="runcheck-unavailable">${esc(reason || "not available for this run")}</p>`;
    }

    function banner(w) {
        if (!w) return "";
        return `<div class="runcheck-banner ${LEVEL_CLASS[w.level] || LEVEL_CLASS.info}" role="alert">`
            + `${esc(w.message)}</div>`;
    }

    // ------------------------------------------------------------------
    // §3.1 Benchmark
    // ------------------------------------------------------------------

    function renderBenchmark(b, m) {
        if (!b || !b.available) return unavailable(b && b.reason);
        const rows = [
            ["Strategy return", pct(m && m.total_return_pct)],
            ["Buy & Hold return", pct(asNumber(b.total_return) * 100)],
            ["Alpha", pct(asNumber(b.alpha) * 100)],
            ["Beta", num(b.beta)],
            ["Strategy Sharpe", num(m && m.sharpe)],
            ["Buy & Hold Sharpe", num(b.sharpe)],
            ["Strategy max DD", pct(m && m.max_drawdown_pct)],
            ["Buy & Hold max DD", pct(asNumber(b.max_drawdown) * 100)],
        ].filter((r) => r[1] !== null);

        const beat = asNumber(b.alpha) > 0;
        return `<div class="runcheck-verdict ${beat ? "runcheck-ok" : "runcheck-warn"}">`
            + (beat
                ? `Strategy beat buy-and-hold by ${pct(asNumber(b.alpha) * 100)}.`
                : `Buy-and-hold matched or beat the strategy by ${pct(Math.abs(asNumber(b.alpha) * 100))}.`)
            + `</div>`
            + `<div class="runcheck-rows">${rows.map(row).join("")}</div>`
            + `<p class="runcheck-footnote">Alpha is simple excess return over the same period, `
            + `not the regression intercept. Beta is ${num(b.beta)} — the market moves `
            + `${betaWord(asNumber(b.beta))} this strategy did.</p>`;
    }

    function betaWord(b) {
        if (b === null) return "independently of";
        const a = Math.abs(b);
        if (a < 0.3) return "far more than";
        if (a < 0.8) return "more than";
        if (a < 1.2) return "about as much as";
        return a < 2 ? "less than" : "much less than";
    }

    function row(r) {
        return `<div class="runcheck-row"><span class="runcheck-label" `
            + `title="${esc(r[2] || "")}">${esc(r[0])}</span>`
            + `<span class="runcheck-value">${esc(r[1])}</span></div>`;
    }

    // ------------------------------------------------------------------
    // §3.2 Cost shock
    // ------------------------------------------------------------------

    function renderCostShock(cs) {
        if (!cs || !cs.available) return unavailable(cs && cs.reason);
        const scenarios = cs.scenarios || [];
        if (!scenarios.length) return unavailable("no scenarios were produced");

        const status = cs.status || "robust";
        const baseIsDefault = cs.base_bps_source === "default";
        const headline = `<div class="runcheck-verdict ${STATUS_CLASS[status] || STATUS_CLASS.robust}">`
            + `${esc(STATUS_TEXT[status] || status)}</div>`;

        const rows = scenarios.map((s) => {
            const dropped = s.trades_dropped > 0
                ? `<span class="runcheck-caveat" title="An all-in order needs buying power for the slipped price; `
                  + `past this point the account cannot fund the round trip.">${s.trades_dropped} fewer trades</span>`
                : "";
            return `<tr>`
                + `<td>${esc(s.label)} <span class="runcheck-bps">${pct(s.slippage_pct)}</span>${dropped}</td>`
                + `<td class="num ${s.total_return_pct > 0 ? "pos" : "neg"}">${esc(pct(s.total_return_pct))}</td>`
                + `<td class="num">${esc(num(s.sharpe))}</td>`
                + `<td class="num">${s.profitable ? '<span class="runcheck-yes">yes</span>' : '<span class="runcheck-no">no</span>'}</td>`
                + `</tr>`;
        }).join("");

        const actual = cs.actual
            ? `<p class="runcheck-footnote">Your run above was produced at `
              + `${esc(pct(asNumber(cs.actual.slippage_bps) / 100))} slippage `
              + `(${esc(pct(cs.actual.total_return_pct))} return). The table re-runs the same `
              + `configuration so the only thing varying down it is the slippage multiple.</p>`
            : "";

        const baseNote = baseIsDefault
            ? `<p class="runcheck-caveat-block">The run itself was frictionless, so "Base" is the `
              + `5 bps NSE default — 2× and 3× of zero slippage would be zero, which would `
              + `report every strategy as robust without testing anything.</p>`
            : "";

        return headline + banner(cs.warning) + baseNote
            + `<table class="runcheck-table"><thead><tr><th>Cost Scenario</th>`
            + `<th class="num">Total Return</th><th class="num">Sharpe</th><th class="num">Profitable?</th>`
            + `</tr></thead><tbody>${rows}</tbody></table>` + actual;
    }

    // ------------------------------------------------------------------
    // §3.3 Monte Carlo
    // ------------------------------------------------------------------

    function renderMonteCarlo(mc) {
        if (!mc || !mc.available) return unavailable(mc && mc.reason);
        const boot = mc.bootstrap || {};
        const reorder = mc.reorder || {};
        if (boot.final_equity_is_invariant) return unavailable("bootstrap distribution unavailable");

        const sims = Number(mc.simulations) || 0;
        const conc = mc.concentration || {};
        const rows = [
            ["Median final equity", money(boot.median_final_equity), "Across all resampled sequences"],
            ["5th percentile (pessimistic)", money(boot.p5_final_equity), "Worst 5% of resampled outcomes"],
            ["95th percentile (optimistic)", money(boot.p95_final_equity), "Best 5% of resampled outcomes"],
            ["Probability of profit", pct(boot.profit_probability_pct), `${sims} resampled trade sequences`],
            ["Best trade's share of profit", pct(conc.best_trade_share_pct), "How much of gross profit rests on one trade"],
        ].filter((r) => r[1] !== null);

        // The reorder block is a DIFFERENT experiment. Its ending equity is
        // invariant by construction, so only the drawdown spread is real.
        const pathRows = [
            ["Drawdown, median ordering", pct(reorder.median_max_drawdown_pct)],
            ["Drawdown, worst 5% of orderings", pct(reorder.p95_max_drawdown_pct)],
        ].filter((r) => r[1] !== null);

        return `<div class="runcheck-rows">${rows.map(row).join("")}</div>`
            + `<p class="runcheck-section-label">Same trades, different order</p>`
            + `<div class="runcheck-rows">${pathRows.map(row).join("")}</div>`
            + `<p class="runcheck-footnote">Reordering the same trades cannot change the ending `
            + `equity — a shuffle does not change a sum — so only the path moves. The final-equity `
            + `spread above comes from resampling the trades <em>with</em> replacement. Drawdowns `
            + `here are measured at trade boundaries, not on every bar.</p>`
            + (mc.warnings || []).map((w) => `<div class="runcheck-subbanner ${LEVEL_CLASS[w.level] || LEVEL_CLASS.info}">${esc(w.message)}</div>`).join("");
    }

    // ------------------------------------------------------------------

    function render(container, payload) {
        if (!container) return;
        if (!payload || typeof payload !== "object") { container.innerHTML = ""; return; }
        const m = payload.metrics || {};
        const flag = m.trade_count_flag;
        const trustBanner = flag === "insufficient"
            ? `<div class="runcheck-banner runcheck-banner-error" role="alert">${esc(INSUFFICIENT_BANNER)}</div>`
            : "";

        container.innerHTML = trustBanner
            + panel("benchmark", "Benchmark — vs Buy & Hold",
                "Same symbol, same bars, same capital. Did the strategy add anything?",
                renderBenchmark(payload.benchmark, m))
            + panel("cost-shock", "Cost Shock",
                "Does the edge survive fills worse than the ones you modelled?",
                renderCostShock(payload.cost_shock))
            + panel("monte-carlo", `Monte Carlo — Trade Sequence Resampling`,
                "Was this result lucky, or typical?",
                renderMonteCarlo(payload.monte_carlo));
    }

    const RunChecks = {
        render,
        renderInto(containerId, payload) {
            render(document.getElementById(containerId), payload);
        },
        INSUFFICIENT_BANNER,
        STATUS_TEXT,
    };

    global.RunChecks = RunChecks;
    if (typeof module !== "undefined" && module.exports) module.exports = RunChecks;
}(typeof window !== "undefined" ? window : globalThis));
