/**
 * Richer metric sections — behaviour tests (PRD backTest-enhance §2.2).
 *
 * The Python side (tests/test_metrics_sections.py) owns the arithmetic; what is
 * pinned HERE is that the page renders the four labelled sections below the
 * existing cards, that the "fewer than 20 closed trades" banner actually
 * appears when the server says `insufficient` and stays away when it doesn't,
 * and that a missing metric renders as nothing rather than as a confident
 * 0.00 — a fabricated zero is the one failure mode a metrics panel cannot have.
 *
 * Usage: node tests/js/test_metric_sections.mjs
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import vm from "node:vm";
import assert from "node:assert/strict";

const root = fileURLToPath(new URL("../../", import.meta.url));
const code = readFileSync(
    path.join(root, "src/backtest/web/static/js/components/metric_sections.js"), "utf8",
);

// ------------------------------------------------------------------ tiny DOM
function makeEl(id) {
    return {
        id, innerHTML: "", className: "", hidden: false,
        children: [], style: {}, dataset: {},
        querySelector() { return makeEl(`${id}::sub`); },
        querySelectorAll() { return []; },
        addEventListener() {},
    };
}

const elements = {};
const el = (id) => (elements[id] = elements[id] || makeEl(id));

// Money is a separate global component; the section must work without it.
const sandbox = { console, document: { getElementById: el }, String, Number, Array, Math, isFinite };
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(code, sandbox, { filename: "metric_sections.js" });

const { MetricSections } = sandbox;

/** A metrics block as the API now ships it. */
function metrics(over = {}) {
    return {
        sharpe: 1.42, sortino: 1.89, omega: 1.34, skewness: 0.21, kurtosis: 0.64,
        ulcer_index: 4.2, var_95_inr: -2100, cvar_95_inr: -3150,
        max_drawdown_duration_days: 47, max_drawdown_recovery_days: 61,
        max_drawdown_recovered: true, time_in_drawdown_pct: 23, drawdowns_over_10pct: 3,
        drawdown_episodes: 5,
        profit_factor: 1.68, expectancy_inr: 4230, payoff_ratio: 1.84,
        max_consecutive_wins: 5, max_consecutive_losses: 4,
        avg_trade_duration_bars: 12.5, median_trade_duration_bars: 9,
        closed_trades: 47, trade_count_flag: "ok",
        sharpe_std_error: 0.19, sharpe_ci_low: 1.05, sharpe_ci_high: 1.79,
        ...over,
    };
}

let passed = 0;
function test(name, fn) {
    fn();
    passed += 1;
    console.log(`  ✓ ${name}`);
}

// ------------------------------------------------------------------ sections
test("renders the four sections the PRD names, below the existing cards", () => {
    MetricSections.render(el("a"), metrics());
    const html = el("a").innerHTML;
    assert.match(html, /Risk &amp; Tail Metrics/);
    assert.match(html, /Drawdown Detail/);
    assert.match(html, /Trade Quality/);
    assert.match(html, /Statistical Confidence/);
});

test("each section is collapsible (<details>/<summary>), not always-open markup", () => {
    MetricSections.render(el("b"), metrics());
    const html = el("b").innerHTML;
    assert.equal((html.match(/<details/g) || []).length, 4);
    assert.equal((html.match(/<summary/g) || []).length, 4);
});

test("does not touch the existing metrics grid", () => {
    // The PRD: "The existing metrics panel keeps its current layout". This
    // component must never write into the cards container.
    MetricSections.render(el("metricSections"), metrics());
    assert.equal(elements["metricsCards"], undefined, "must not create/touch #metricsCards");
});

test("renders the headline §2 values with their units", () => {
    MetricSections.render(el("c"), metrics());
    const html = el("c").innerHTML;
    assert.match(html, /1\.34/);          // Omega
    assert.match(html, /0\.21/);          // skewness
    assert.match(html, /47 days/);        // max drawdown duration
    assert.match(html, /23\.00%/);        // time in drawdown
    assert.match(html, /1\.84/);          // payoff ratio
    assert.match(html, /12\.5 bars/);     // avg duration
    assert.match(html, /9\.0 bars/);      // median duration
});

test("a drawdown that never recovered says so instead of inventing a duration", () => {
    MetricSections.render(el("d"), metrics({ max_drawdown_recovered: false }));
    assert.match(el("d").innerHTML, /not recovered/);
});

test("CVaR is shown alongside VaR — the tail number is the point of the section", () => {
    MetricSections.render(el("e"), metrics());
    const html = el("e").innerHTML;
    assert.match(html, /VaR 95%/);
    assert.match(html, /CVaR \/ Expected Shortfall 95%/);
});

// ------------------------------------------------------------------ banner
test("insufficient trade count raises the warning banner", () => {
    MetricSections.render(el("f"), metrics({ trade_count_flag: "insufficient", closed_trades: 7 }));
    const html = el("f").innerHTML;
    assert.match(html, /metric-confidence-banner/);
    assert.match(html, /Fewer than 20 closed trades/);
    assert.match(html, /statistical uncertainty/);
});

test("the banner is announced, and is not dismissable", () => {
    MetricSections.render(el("g"), metrics({ trade_count_flag: "insufficient" }));
    const html = el("g").innerHTML;
    assert.match(html, /role="alert"/);
    assert.ok(!/<button/i.test(html), "the sample-size warning must not be dismissable");
});

test("ok and warn raise no banner — a clean run must not cry wolf", () => {
    MetricSections.render(el("h"), metrics({ trade_count_flag: "ok" }));
    assert.ok(!el("h").innerHTML.includes("metric-confidence-banner"));
    MetricSections.render(el("h"), metrics({ trade_count_flag: "warn", closed_trades: 24 }));
    assert.ok(!el("h").innerHTML.includes("metric-confidence-banner"));
});

test("all three confidence states stay visually distinguishable", () => {
    for (const [flag, cls] of [
        ["ok", "metric-confidence-ok"],
        ["warn", "metric-confidence-warn"],
        ["insufficient", "metric-confidence-insufficient"],
    ]) {
        MetricSections.render(el(`i-${flag}`), metrics({ trade_count_flag: flag }));
        assert.match(el(`i-${flag}`).innerHTML, new RegExp(cls));
    }
});

test("an absent flag renders no strip and no banner (older payload)", () => {
    MetricSections.render(el("j"), { sharpe: 1.1 });
    const html = el("j").innerHTML;
    assert.ok(!html.includes("metric-confidence-banner"));
    assert.ok(!html.includes("metric-confidence-flag"));
});

// ------------------------------------------------------------------ honesty
test("a missing metric is omitted, never shown as 0.00", () => {
    const partial = { trade_count_flag: "ok", closed_trades: 47, sharpe: 1.0 };
    MetricSections.render(el("k"), partial);
    const html = el("k").innerHTML;
    assert.ok(!html.includes("Omega Ratio"), "no value, no row");
    assert.ok(!/0\.00%<\/span>/.test(html), "no fabricated zero percentage");
    // Empty sections collapse away rather than leaving a titled husk.
    assert.ok(!html.includes("Drawdown Detail"));
});

test("a section with no data is not rendered at all", () => {
    MetricSections.render(el("l"), { trade_count_flag: "ok" });
    const html = el("l").innerHTML;
    assert.ok(!html.includes("<details"), "a section with nothing in it is not a section");
});

test("null is treated as missing, not as a number", () => {
    MetricSections.render(el("m"), metrics({ omega: null, ulcer_index: undefined }));
    const html = el("m").innerHTML;
    assert.ok(!html.includes("Omega Ratio"));
    assert.ok(!html.includes("Ulcer Index"));
});

test("no metrics at all renders an empty container (page has not run yet)", () => {
    MetricSections.render(el("n"), null);
    assert.equal(el("n").innerHTML, "");
    MetricSections.render(el("n"), undefined);
    assert.equal(el("n").innerHTML, "");
});

test("renderInto resolves the container by id", () => {
    MetricSections.renderInto("o", metrics());
    assert.match(el("o").innerHTML, /Trade Quality/);
});

test("a missing container is a no-op, not a crash", () => {
    MetricSections.renderInto("does-not-exist", metrics());  // must not throw
});

// ------------------------------------------------------------------ escaping
test("a value carrying markup is escaped, never injected as HTML", () => {
    // Values are numbers by contract, but a string slipping through the API
    // must not become markup.
    MetricSections.render(el("q"), metrics({ omega: "<img src=x onerror=alert(1)>" }));
    const html = el("q").innerHTML;
    assert.ok(!html.includes("<img"), "raw markup must not survive into the DOM");
    assert.match(html, /&lt;img/);
});

test("an unrecognised confidence flag renders no class, rather than echoing it", () => {
    // The flag is looked up in a table, never interpolated — a crafted value
    // must not be able to smuggle a class or a quote onto the element.
    MetricSections.render(el("r"), metrics({ trade_count_flag: 'x" onclick="alert(1)' }));
    const html = el("r").innerHTML;
    assert.ok(!html.includes("onclick="), "the flag must not reach the DOM as an attribute");
    assert.ok(!html.includes("metric-confidence-flag"), "unknown flag renders no strip");
    assert.ok(!html.includes("metric-confidence-banner"), "unknown flag is not `insufficient`");
});

console.log(`\nricher metric sections: ${passed} tests passed`);
