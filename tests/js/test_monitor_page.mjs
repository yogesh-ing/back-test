/**
 * Portfolio Intelligence page (/monitor) — MonitorView render tests.
 *
 * Pins the things a trader acts on: severity ordering and the ack control,
 * signs/labels on the Greek cards (a short-gamma book must SAY short
 * convexity), concentration bars coloured by the alert that fired, the
 * correlation heatmap's diagonal/missing cells, and HTML-escaping of runner
 * names (free text from the spawn form).
 *
 * Usage: node tests/js/test_monitor_page.mjs
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import vm from "node:vm";
import assert from "node:assert/strict";

const root = fileURLToPath(new URL("../../", import.meta.url));
const read = (p) => readFileSync(path.join(root, p), "utf8");

// No `document` in the context: monitor.js must export MonitorView and stop
// before the controller touches the DOM.
const ctx = vm.createContext({ console });
vm.runInContext(read("src/backtest/web/static/js/components/currency.js"), ctx);
vm.runInContext(read("src/backtest/web/static/js/monitor.js"), ctx);
const V = ctx.MonitorView;

let passed = 0;
function test(name, fn) {
    fn();
    passed += 1;
    console.log(`  ✓ ${name}`);
}

// A trimmed real snapshot shape (see PortfolioMonitor.snapshot).
const alerts = [
    { id: "a1", key: "greeks:gamma:portfolio", category: "greeks", severity: "warning",
      title: "Short gamma", message: "Book loses on big moves", recommendation: "Buy wings",
      occurrences: 3, acknowledged: false, misses: 0 },
    { id: "a2", key: "concentration:underlying:NIFTY", category: "concentration",
      severity: "critical", title: "100% of exposure in NIFTY", message: "₹3.9M in NIFTY",
      occurrences: 14, acknowledged: false, misses: 1 },
    { id: "a3", key: "correlation:hedge:x", category: "correlation", severity: "info",
      title: "Hedge pair", message: "offsetting", occurrences: 1, acknowledged: false, misses: 0 },
];
const greeks = {
    position_count: 3,
    totals: { delta_1pct: -1200, gamma_2pct_pnl: -4800, vega: -2500, theta_day: 9000,
              net_premium: 46277.5, margin_used: 150000, margin_capital: 200000, margin_pct: 0.75 },
    ratios: { delta_1pct: -0.006, gamma_2pct: -0.024 },
    by_strategy: [{ strategy_name: "<img src=x onerror=alert(1)>", strategy_kind: "strangle",
                    mode: "paper", delta_1pct: -1200, delta_units: { NIFTY: -4.7 },
                    gamma_pnl_1pct: -1200, vega: -2500, theta_day: 9000, net_premium: 46277.5,
                    positions: 2, long_legs: 0, short_legs: 2 }],
    by_underlying: [{ underlying: "NIFTY", spot: 25354.9, delta_units: -4.7, delta_1pct: -1200,
                      gamma_1pct: -3.2, vega: -2500, positions: 2 }],
    legs: [{ strategy_name: "S", symbol: "NIFTY25350CE", instrument_type: "option", side: "SHORT",
             lots: 2, dte_days: 30.1, iv: 0.12, iv_source: "default", delta: 0.51,
             delta_1pct: -9800, position_theta_day: 4500, structure_type: "strangle" }],
    iv_sources: { contract: 1, default: 1 },
    scenarios: {
        spot: [{ label: "Spot -2.0%", spot_pct: -2, pnl: -5000, pnl_pct: -0.025, delta_gamma_pnl: -4900 },
               { label: "Spot +2.0%", spot_pct: 2, pnl: -7000, pnl_pct: -0.035, delta_gamma_pnl: -7100 }],
        iv: [{ label: "IV +5 pts", iv_pts: 5, pnl: -12500, pnl_pct: -0.06 }],
        time_decay_1d: { pnl: 9000, pnl_pct: 0.045 },
        stress: [{ label: "Crash: −2% & IV +5", pnl: -17000, pnl_pct: -0.085 }],
        worst: { label: "Crash: −2% & IV +5", pnl: -17000, pnl_pct: -0.085 },
    },
    alerts: [alerts[0]],
};

test("alerts sort critical first, info filter, ack button only when actionable", () => {
    const html = V.renderAlerts(alerts);
    assert.ok(html.indexOf("100% of exposure") < html.indexOf("Short gamma"));
    assert.ok(html.includes('data-alert-id="a2"'));
    assert.ok(!html.includes('data-alert-id="a3"'), "info alerts are not ackable");
    assert.ok(html.includes("clearing"), "misses>0 shows the hysteresis state");
    const noInfo = V.renderAlerts(alerts, { showInfo: false });
    assert.ok(!noInfo.includes("Hedge pair"));
    const acked = V.renderAlerts([{ ...alerts[0], acknowledged: true }]);
    assert.ok(acked.includes("acknowledged") && !acked.includes("mon-ack"));
});

test("no alerts reads as all-clear", () => {
    assert.ok(V.renderAlerts([]).includes("No active"));
    assert.ok(V.renderCounts({ critical: 1, warning: 2, info: 0, unacknowledged: 3 })
        .includes("3 unacknowledged"));
});

test("greek cards carry sign, direction words and the alert's severity", () => {
    const html = V.renderGreekCards(greeks);
    assert.ok(html.includes("SHORT bias"));
    assert.ok(html.includes("short convexity"));
    assert.ok(html.includes("short vol"));
    assert.ok(html.includes("collecting decay"));
    assert.ok(html.includes("credit (received)"));
    assert.ok(html.includes("-₹1,200"), "delta in money, signed");
    assert.ok(html.includes("mon-card-warning"), "gamma card picks up the warning");
    assert.ok(html.includes("75.0%"), "margin % shown");
});

test("scenario table: worst line, Δ-Γ column, stress rows", () => {
    const html = V.renderScenarios(greeks.scenarios);
    assert.ok(html.includes("Worst modelled"));
    assert.ok(html.includes("Crash: −2% &amp; IV +5"), "labels are escaped, not dropped");
    assert.ok(html.includes("-₹4,900"), "delta-gamma estimate shown next to full reval");
    assert.ok(html.includes("Time decay"));
});

test("strategy names are HTML-escaped everywhere", () => {
    const html = V.renderByStrategy(greeks);
    assert.ok(!html.includes("<img"), "raw tag must not survive");
    assert.ok(html.includes("&lt;img"));
    assert.ok(html.includes("TOTAL"));
    assert.equal(V.esc(`"a'&`), "&quot;a&#39;&amp;");
});

test("empty book renders placeholders, never throws", () => {
    const empty = { position_count: 0, totals: {}, ratios: {}, by_strategy: [], by_underlying: [],
                    legs: [], scenarios: { spot: [], iv: [], time_decay_1d: {}, stress: [], worst: null } };
    assert.ok(V.renderByStrategy(empty).includes("No open positions"));
    assert.ok(V.renderLegs(empty).includes("No open legs"));
    assert.ok(V.renderGreekCards(empty).includes("flat"));
    V.renderScenarios(empty.scenarios);
    V.renderConcentration(null);
    V.renderCorrelation(null);
    V.renderRegime(null);
    V.renderSummary({});
});

test("legs flag default IV and list IV sources", () => {
    assert.ok(V.renderLegs(greeks).includes("mon-iv-default"));
    assert.ok(V.renderIvSources(greeks).includes("1 default"));
});

test("concentration bars are clamped and coloured by the alert that fired", () => {
    const conc = {
        total_notional: 3.9e6, gross_leverage: 19.5, effective_underlyings: 1, herfindahl: 1,
        exposure_count: 3, strategy_count: 3,
        by_underlying: [{ underlying: "NIFTY", pct: 1.0, notional: 3.9e6, delta_notional: 1.4e5, strategy_count: 3 }],
        by_group: [{ group: "INDIA_INDEX", pct: 1.2, underlyings: ["NIFTY"] }],
        strike_clusters: [{ key: "NIFTY|25350|2026-10-29", underlying: "NIFTY", strike: 25350,
                            expiry: "2026-10-29", positions: 3, lots: 6, option_types: ["CE", "PE"],
                            strategies: ["A", "B"] }],
        alerts: [{ key: "concentration:underlying:NIFTY", severity: "critical" },
                 { key: "concentration:strike:NIFTY|25350|2026-10-29", severity: "warning" }],
    };
    const r = V.renderConcentration(conc);
    assert.ok(r.underlying.includes("mon-bar-critical"));
    assert.ok(r.underlying.includes("width:100.0%"));
    assert.ok(r.groups.includes("width:100.0%"), "pct > 1 is clamped");
    assert.ok(r.strikes.includes("mon-row-warn"));
    assert.ok(r.strikes.includes("CE/PE"));
});

test("correlation heatmap: diagonal dash, missing cell dot, source label", () => {
    const corr = {
        strategies: ["A", "B", "C"], matrix: [[1, 0.9, null], [0.9, 1, -0.8], [null, -0.8, 1]],
        observations: 120, min_observations: 20, status: "ok", method: "pearson",
        series_source: "tick_samples", effective_strategies: 1.4, diversification_ratio: 1.2,
        strategies_with_variance: 3, alerts: [],
    };
    const r = V.renderCorrelation(corr);
    assert.equal((r.matrix.match(/>—</g) || []).length, 3, "one dash per diagonal cell");
    assert.ok(r.matrix.includes(">·<"), "null correlation shows as a dot");
    assert.ok(r.matrix.includes("0.90") && r.matrix.includes("-0.80"));
    assert.ok(r.meta.includes("same-tick samples"));
    assert.ok(V.corrColor(0.9).startsWith("rgba(239"), "positive = red (losses cluster)");
    assert.ok(V.corrColor(-0.9).startsWith("rgba(34"), "negative = green (hedge)");
    const thin = V.renderCorrelation({ ...corr, strategies: ["A"], matrix: [], status: "need_two_strategies" });
    assert.ok(thin.matrix.includes("at least two"));
});

test("regime: badge class, flags, fit table with mismatch highlight", () => {
    const r = V.renderRegime({
        symbol: "NIFTY", regime: "high_vol", label: "High volatility", transitioning: true,
        range_expanding: true, vol_index: 27.4, vol_index_source: "option_iv",
        vol_index_change_pct: 24.1, realized_vol: 30.2, current_range_pct: 1.2, avg_range_pct: 0.6,
        bars: 200, periods_per_year: 252, data_source: "runner_bars",
        strategy_fit: [{ strategy_name: "Iron fly", structure_type: "iron_fly", fit: "mismatch",
                         recommendation: "Reduce size",
                         profile: { optimal_regime: "low_vol", optimal_vol_range: [10, 18], tags: ["short_premium"] } }],
    });
    assert.ok(r.badge.includes("mon-regime-high"));
    assert.ok(r.badge.includes("transitioning") && r.badge.includes("range expanding"));
    assert.ok(r.stats.includes("+24.1%"));
    assert.ok(r.fit.includes("mon-row-warn") && r.fit.includes("10–18") && r.fit.includes("short_premium"));
    assert.ok(r.source.includes("book&#39;s option IV"), "source label, escaped");
});

test("summary pills show scope, P&L sign and alert severity", () => {
    const html = V.renderSummary({ mode: "paper",
        summary: { strategies: 5, positions: 7, option_legs: 4, equity: 1e6, daily_pnl: -2500 },
        alert_counts: { critical: 1, warning: 2 } });
    assert.ok(html.includes("paper"));
    assert.ok(html.includes("mon-pill neg") || html.includes("mon-pill  neg") || html.includes(" neg\""));
    assert.ok(html.includes("-₹2,500"));
    assert.ok(html.includes("mon-pill-crit"));
});

console.log(`\nmonitor page: ${passed} tests passed`);
