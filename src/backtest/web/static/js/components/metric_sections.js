/**
 * Richer-metric sections (PRD backTest-enhance Part 1 §2.2).
 *
 *   MetricSections.renderInto("metricSections", metrics)
 *
 * §2.2 is explicit that the EXISTING metrics panel keeps its current layout and
 * the new numbers go in labelled, collapsible sections *below* it. So this
 * component never touches the cards above it — it appends four <details>
 * blocks and, when the sample is too small to trust, a banner.
 *
 * The banner is the one piece of state that actually changes how a reader
 * should read everything else, so it is rendered first, non-dismissably, and
 * carries role="alert". The server owns the decision (`trade_count_flag`); this
 * file only decides what the three states look like.
 */
(function (global) {
    "use strict";

    const FLAG_CLASS = {
        ok: "metric-confidence-ok",
        warn: "metric-confidence-warn",
        insufficient: "metric-confidence-insufficient",
    };

    const FLAG_TEXT = {
        ok: "Sufficient trade count",
        warn: "Thin trade count",
        insufficient: "Insufficient trade count",
    };

    const INSUFFICIENT_BANNER =
        "Fewer than 20 closed trades — all metrics have very high statistical uncertainty. "
        + "Run on a longer date range or different symbol before drawing conclusions.";

    /**
     * One row per metric. `get` pulls the raw value out of the payload; a null
     * return means "nothing to show" and the row is skipped rather than printed
     * as 0.00, which would read as a real measurement.
     */
    const SECTIONS = [
        {
            id: "risk-tail",
            title: "Risk & Tail Metrics",
            note: "The shape of the return distribution, not just its average.",
            rows: [
                { label: "Sortino Ratio", get: (m) => num(m.sortino), hint: "Like Sharpe, but only downside volatility is penalised." },
                { label: "Omega Ratio", get: (m) => num(m.omega), hint: "Winning return area over losing return area — the full shape, not just the tail." },
                { label: "Return Skewness", get: (m) => num(m.skewness), hint: "Negative = tilted toward occasional big losses. A danger signal for short-premium strategies." },
                { label: "Excess Kurtosis", get: (m) => num(m.kurtosis), hint: "Fat tails when positive: 0.00 is normal, not zero risk." },
                { label: "VaR 95%", get: (m) => money(m.var_95_inr), hint: "Loss on a typical bad day (worst 5% of days)." },
                { label: "CVaR / Expected Shortfall 95%", get: (m) => money(m.cvar_95_inr), hint: "Average loss on the days that ARE in that worst 5%. Always worse than VaR." },
                { label: "Ulcer Index", get: (m) => num(m.ulcer_index), hint: "Drawdown depth AND duration in one number. Higher = more painful to hold." },
            ],
        },
        {
            id: "drawdown-detail",
            title: "Drawdown Detail",
            note: "Max drawdown is one number. These say how long it lasted and how often.",
            rows: [
                { label: "Max Drawdown Duration", get: (m) => days(m.max_drawdown_duration_days), hint: "Calendar days from the peak that started the worst drawdown to the bar that ended it." },
                { label: "Max Drawdown Recovery Days", get: (m) => recovery(m), hint: "Days from the trough back to the prior peak. Shown as \"not recovered\" if the run ended underwater." },
                { label: "Time in Drawdown %", get: (m) => pct(m.time_in_drawdown_pct), hint: "Share of the run spent below a previous high." },
                { label: "Drawdowns > 10%", get: (m) => count(m.drawdowns_over_10pct), hint: "How many separate times it dropped more than 10%." },
                { label: "Total Drawdown Episodes", get: (m) => count(m.drawdown_episodes) },
            ],
        },
        {
            id: "trade-quality",
            title: "Trade Quality",
            note: "Whether the result came from many small wins or a few lucky large ones.",
            rows: [
                { label: "Profit Factor", get: (m) => num(m.profit_factor), hint: "Gross profit over gross loss. Above 1.5 is decent; below 1.0 is a losing strategy." },
                { label: "Expectancy per Trade", get: (m) => money(m.expectancy_inr), hint: "Average P&L per trade — win rate and payoff together." },
                { label: "Payoff Ratio", get: (m) => num(m.payoff_ratio), hint: "Average winning trade over average losing trade." },
                { label: "Max Consecutive Wins", get: (m) => count(m.max_consecutive_wins) },
                { label: "Max Consecutive Losses", get: (m) => count(m.max_consecutive_losses), hint: "Capital-tolerance number: how much room a runner needs before the worst case arrives." },
                { label: "Avg Trade Duration", get: (m) => bars(m.avg_trade_duration_bars) },
                { label: "Median Trade Duration", get: (m) => bars(m.median_trade_duration_bars) },
            ],
        },
        {
            id: "statistical-confidence",
            title: "Statistical Confidence",
            note: "How much of the headline number is signal, and how much is sample size.",
            rows: [
                { label: "Closed Trades", get: (m) => count(m.closed_trades), hint: "The sample size behind every point estimate on this page." },
                { label: "Sharpe Standard Error", get: (m) => num(m.sharpe_std_error), hint: "If this is large next to the Sharpe, the edge is mostly noise." },
                { label: "Sharpe 95% CI", get: (m) => ci(m) },
            ],
        },
    ];

    // ------------------------------------------------------------------
    // Formatters. A MISSING value (null/undefined) returns null so the row is
    // skipped instead of printed as a confident-looking 0.00. Anything that
    // IS present is rendered — coercing a numeric string, and passing a
    // non-numeric one through to be escaped, because a row that silently
    // disappears hides a number the operator asked to see.
    // ------------------------------------------------------------------

    function missing(v) {
        return v === null || v === undefined;
    }

    function asNumber(v) {
        const n = Number(v);
        return typeof v === "boolean" || missing(v) || v === "" || !isFinite(n) ? null : n;
    }

    /** Plain text for a value, however it arrived. Escaped by the caller. */
    function text(v) {
        return missing(v) ? null : String(v);
    }

    function num(v) {
        const n = asNumber(v);
        return n === null ? text(v) : n.toFixed(2);
    }
    function pct(v) {
        const n = asNumber(v);
        return n === null ? text(v) : `${n.toFixed(2)}%`;
    }
    function count(v) {
        const n = asNumber(v);
        return n === null ? text(v) : String(Math.round(n));
    }
    function days(v) {
        const n = asNumber(v);
        if (n === null) return text(v);
        const d = Math.round(n);
        return `${d} day${d === 1 ? "" : "s"}`;
    }
    function bars(v) {
        const n = asNumber(v);
        return n === null ? text(v) : `${n.toFixed(1)} bars`;
    }
    function money(v) {
        const n = asNumber(v);
        if (n === null) return text(v);
        return (typeof Money !== "undefined") ? Money.signed(n) : n.toFixed(2);
    }
    function recovery(m) {
        if (m.max_drawdown_recovered === false) return "not recovered";
        return days(m.max_drawdown_recovery_days);
    }
    function ci(m) {
        if (missing(m.sharpe_ci_low) || missing(m.sharpe_ci_high)) return null;
        return `${asNumber(m.sharpe_ci_low).toFixed(2)} → ${asNumber(m.sharpe_ci_high).toFixed(2)}`;
    }

    function esc(v) {
        return String(v === null || v === undefined ? "" : v).replace(/[&<>"']/g, (c) => (
            { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
        ));
    }

    /** The banner. Non-dismissable on purpose — see the file header. */
    function confidenceBanner(m) {
        const flag = m.trade_count_flag;
        if (flag !== "insufficient") return "";
        return `<div class="metric-confidence-banner" role="alert">${esc(INSUFFICIENT_BANNER)}</div>`;
    }

    function confidenceStrip(m) {
        const flag = m.trade_count_flag;
        if (!flag || !FLAG_CLASS[flag]) return "";
        return `<div class="metric-confidence-flag ${FLAG_CLASS[flag]}">`
            + `${esc(FLAG_TEXT[flag])} · ${count(m.closed_trades) || 0} closed trades</div>`;
    }

    function sectionHtml(section, m) {
        const rows = section.rows
            .map((r) => {
                const value = r.get(m);
                if (value === null || value === undefined) return "";
                return `<div class="metric-row">`
                    + `<span class="metric-row-label"${r.hint ? ` title="${esc(r.hint)}"` : ""}>${esc(r.label)}</span>`
                    + `<span class="metric-row-value">${esc(value)}</span>`
                    + `</div>`;
            })
            .filter(Boolean)
            .join("");
        if (!rows) return "";
        return `<details class="metric-section" data-section="${esc(section.id)}">`
            + `<summary class="metric-section-head">${esc(section.title)}</summary>`
            + (section.note ? `<p class="metric-section-note">${esc(section.note)}</p>` : "")
            + `<div class="metric-section-body">${rows}</div>`
            + `</details>`;
    }

    function render(container, m) {
        if (!container) return;
        if (!m || typeof m !== "object") { container.innerHTML = ""; return; }
        const sections = SECTIONS.map((s) => sectionHtml(s, m)).filter(Boolean).join("");
        container.innerHTML = confidenceBanner(m) + confidenceStrip(m) + sections;
    }

    const MetricSections = {
        render,
        renderInto(containerId, metrics) {
            render(document.getElementById(containerId), metrics);
        },
        INSUFFICIENT_BANNER,
        SECTIONS,
    };

    global.MetricSections = MetricSections;
    if (typeof module !== "undefined" && module.exports) module.exports = MetricSections;
}(typeof window !== "undefined" ? window : globalThis));
