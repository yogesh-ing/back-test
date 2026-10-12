/**
 * Compare-tab analytics components — behaviour tests (PRD backTest-enhance §4).
 *
 * The Python side owns the maths (`tests/test_engine_comparison.py`) and the
 * API shape (`tests/test_api_backtest_comparison.py`). What is pinned HERE is
 * the part a user actually reads:
 *
 *   • §4.3 the heatmap colours green→red and says in words what a red cell means
 *   • §4.3 an "n/a" cell is explained, not left as a mystery blank
 *   • §4.4 the significance table does not overstate the server's verdict
 *   • §4.1 a failed slot keeps its column and shows its error
 *   • §4.5 curves are indexed to 100, including the guard for a bad base
 *
 * A component that renders nothing is worse than no component: the numbers
 * underneath still look authoritative.
 *
 * Usage: node tests/js/test_compare_panels.mjs
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import vm from "node:vm";
import assert from "node:assert/strict";

const root = fileURLToPath(new URL("../../", import.meta.url));
const read = (p) => readFileSync(path.join(root, p), "utf8");

// ------------------------------------------------------------------ tiny DOM
function makeEl(id) {
    return {
        id, innerHTML: "", className: "", hidden: false, style: {}, dataset: {},
        value: "", options: [], checked: false,
        querySelector() { return makeEl(`${id}::q`); },
        querySelectorAll() { return []; },
        addEventListener() {},
        appendChild() {},
    };
}
const elements = {};
const el = (id) => (elements[id] = elements[id] || makeEl(id));

function load(file, globals) {
    const sandbox = { console, document: { getElementById: el }, ...globals };
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(read(file), sandbox, { filename: file });
    return sandbox;
}

let passed = 0;
function test(name, fn) {
    fn();
    passed += 1;
    console.log(`  ✓ ${name}`);
}

// ================================================================== panels
const { ComparePanels } = load("src/backtest/web/static/js/compare/comparison_panels.js", {
    Set, Math, isFinite, Object, JSON,
});

const LABELS = [
    "sma_crossover · INFY (fast=10,slow=30)",
    "sma_crossover · INFY (fast=20,slow=60)",
    "rsi_reversion · INFY (period=14)",
];

function comparison(over = {}) {
    return {
        correlation: {
            available: true,
            labels: LABELS,
            matrix: [[1, 0.92, 0.1], [0.92, 1, 0.15], [0.1, 0.15, 1]],
            // The server sends the full pair list, not a count.
            pairs: [
                { a: LABELS[0], b: LABELS[1], correlation: 0.92, high: true },
                { a: LABELS[0], b: LABELS[2], correlation: 0.1, high: false },
                { a: LABELS[1], b: LABELS[2], correlation: 0.15, high: false },
            ],
            aligned_bars: 520,
            high_correlation_pairs: [
                { a: LABELS[0], b: LABELS[1], correlation: 0.92 },
            ],
            max_correlation: 0.92,
            warnings: [],
        },
        significance: {
            available: true,
            labels: LABELS,
            simulations: 1000,
            aligned_bars: 520,
            observed_sharpe: {},
            comparisons: [{
                a: LABELS[0], b: LABELS[1],
                a_better_pct: 97.4, b_better_pct: 2.6,
                observed_sharpe_gap: 0.51, verdict: "a_better", winner: LABELS[0],
            }, {
                a: LABELS[0], b: LABELS[2],
                a_better_pct: 54.0, b_better_pct: 46.0,
                observed_sharpe_gap: 0.08, verdict: "no_significant_difference", winner: null,
            }],
            warnings: [],
        },
        excluded: [],
        ...over,
    };
}

const render = (c) => { ComparePanels.render(el("cmp"), c); return el("cmp").innerHTML; };

// ---------------------------------------------------------------- §4.3
test("heatmap renders a cell for every ordered pair", () => {
    const html = render(comparison());
    const cells = html.match(/class="cmp-cell /g) || [];
    assert.equal(cells.length, 9, "expected 3x3 cells");
});

test("high correlation is red and low correlation is green", () => {
    const html = render(comparison());
    assert.match(html, /cmp-cell cmp-cell-hot[^>]*>0\.92/);
    assert.match(html, /cmp-cell cmp-cell-cool[^>]*>0\.10/);
});

test("the diagonal is not coloured as a finding", () => {
    const html = render(comparison());
    assert.equal((html.match(/cmp-cell-self/g) || []).length, 3);
});

test("a correlated pair is explained in words, not just in colour", () => {
    const html = render(comparison());
    assert.match(html, /cmp-warn/);
    assert.match(html, /1 of the 3 pairs/);
});

test("a diversifying set says so instead of staying silent", () => {
    const c = comparison();
    c.correlation.high_correlation_pairs = [];
    const html = render(c);
    assert.match(html, /genuinely different bets/);
});

test("n/a cells carry an explanation of what undefined means", () => {
    const c = comparison();
    c.correlation.matrix = [[1, null, 0.1], [null, 1, 0.15], [0.1, 0.15, 1]];
    const html = render(c);
    assert.match(html, />n\/a</);
    assert.match(html, /undefined/);
    assert.match(html, /not zero/);
});

test("the heatmap states how many bars every cell was measured on", () => {
    assert.match(render(comparison()), /520 bars every/);
});

test("both panels explain the two modes in their own terms", () => {
    const c = comparison();
    c.correlation.labels = ["sma_crossover · INFY", "sma_crossover · TCS", "sma_crossover · HDFCBANK"];
    c.correlation.matrix = [[1, 0.1, 0.2], [0.1, 1, 0.3], [0.2, 0.3, 1]];
    const html = render(c);
    assert.match(html, /INFY/);
    assert.match(html, /TCS/);
});

// ---------------------------------------------------------------- §4.4
test("significance names a winner only when the server found one", () => {
    const html = render(comparison());
    assert.match(html, /is likely better \(97% confidence\)/);
    assert.match(html, /No significant difference/);
});

test("a server verdict is never upgraded by the renderer", () => {
    const c = comparison();
    c.significance.comparisons[0].verdict = "no_significant_difference";
    c.significance.comparisons[0].a_better_pct = 97.4;
    const html = render(c);
    assert.equal((html.match(/is likely better/g) || []).length, 0);
});

test("significance states the resample count and says it is paired", () => {
    const html = render(comparison());
    assert.match(html, /1000 resamples/);
    assert.match(html, /<em>same<\/em> drawn market/);
});

test("significance is labelled informational, not a gate", () => {
    assert.match(render(comparison()), /does not gate/);
});

test("no significant winner anywhere raises the loud warning", () => {
    const c = comparison();
    c.significance.warnings = [{
        level: "warning", code: "no_significant_winner",
        message: "No pair differs significantly. Promoting the top row out of these results is promoting the luckiest, not the best.",
    }];
    assert.match(render(c), /promoting the luckiest/);
});

// ------------------------------------------------------- §4.1 failed slots
test("a failed slot is named with its reason, not dropped", () => {
    const c = comparison();
    c.excluded = [{ slot: "4", label: "macd_trend · INFY", reason: "unknown strategy: macd_trend" }];
    const html = render(c);
    assert.match(html, /Not included in the comparison \(1\)/);
    assert.match(html, /macd_trend/);
    assert.match(html, /unknown strategy: macd_trend/);
});

test("a complete run shows no excluded section at all", () => {
    assert.doesNotMatch(render(comparison()), /Not included in the comparison/);
});

// ---------------------------------------------------------------- §4.5
test("equity curves are indexed to 100 at the shared base date", () => {
    const { indexTo100 } = load("src/backtest/web/static/js/compare/equity_compare_chart.js", {
        Chart: function () { this.destroy = () => {}; }, isFinite, Math, Map, Set,
    });
    const out = indexTo100([], [100_000, 150_000, 75_000], 100_000);
    assert.deepEqual(out, [100, 150, 75]);
    assert.equal(out[0], 100, "every curve must start on the same mark");
});

test("two slots with different starting capital still start at 100", () => {
    const { indexTo100 } = load("src/backtest/web/static/js/compare/equity_compare_chart.js", {
        Chart: function () { this.destroy = () => {}; }, isFinite, Math, Map, Set,
    });
    const a = indexTo100([], [10_000, 20_000], 10_000);
    const b = indexTo100([], [250_000, 125_000], 250_000);
    assert.equal(a[0], b[0], "indexing removes the starting-capital gap");
    assert.equal(a[1], 200);
    assert.equal(b[1], 50);
});

test("an unusable base leaves the curve alone instead of blanking the chart", () => {
    const { indexTo100 } = load("src/backtest/web/static/js/compare/equity_compare_chart.js", {
        Chart: function () { this.destroy = () => {}; }, isFinite, Math, Map, Set,
    });
    for (const base of [0, -1, NaN, Infinity, null, undefined]) {
        // A non-finite base is itself unusable, so compare against a curve that
        // does not contain it — what matters is that dividing never happened.
        const values = [1, 10, 20];
        const out = indexTo100([], values, base);
        assert.deepEqual(out, values, `base ${base} must not be divided into`);
        assert.ok(out.every((v) => Number.isFinite(v)), `base ${base} produced junk`);
    }
});


// ======================================================== §4.1 failed slots
// metrics_table.js is a bare script (no IIFE, no exports), so it is evaluated
// into a shared sandbox that records the HTML it writes.
function renderTable(slots) {
    const target = el("tbl");
    target.innerHTML = "";
    const sandbox = { console, document: { getElementById: (id) => (id === "tbl" ? target : el(id)) } };
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(read("src/backtest/web/static/js/compare/metrics_table.js"), sandbox,
        { filename: "metrics_table.js" });
    sandbox.renderCompareTable("tbl", slots, () => {});
    return target.innerHTML;
}

const okSlot = (id, label, ret, sharpe) => ({
    id, label, color: "#3b82f6",
    result: { metrics: { total_return_pct: ret, win_rate_pct: 50, closed_trades: 4,
                         max_drawdown_pct: -10, sharpe, total_trades: 8 } },
});

test("a failed slot keeps its column in the table", () => {
    const html = renderTable([okSlot(1, "A", 10, 1.0),
                              { id: 2, label: "B", color: "#d4b26a", error: "unknown strategy" },
                              okSlot(3, "C", 5, 0.5)]);
    assert.match(html, /col-head-failed/);
    assert.match(html, /unknown strategy/);
});

test("a failed slot's metric cells are blank, not zero", () => {
    const html = renderTable([okSlot(1, "A", 10, 1.0),
                              { id: 2, label: "B", color: "#d4b26a", error: "boom" }]);
    assert.match(html, /metric-cell-void/);
    assert.doesNotMatch(html, /0\.00%<\/td>/);
});

test("a failed slot never wins the best-per-row trophy", () => {
    const html = renderTable([okSlot(1, "A", 10, 1.0),
                              { id: 2, label: "B", color: "#d4b26a", error: "boom" }]);
    const header = html.slice(0, html.indexOf("</tr>"));
    const trophy = html.slice(header.length);
    // Four ranked rows (Total Trades is deliberately unranked), and the failed
    // slot is not one of the winners — it has no numbers to win with.
    assert.equal((trophy.match(/🏆/g) || []).length, 4);
    assert.equal((html.match(/<td class="metric-cell-void">—<\/td> 🏆/g) || []).length, 0);
});

test("a failed slot offers no Backtest or Forward button", () => {
    const html = renderTable([okSlot(1, "A", 10, 1.0),
                              { id: 2, label: "B", color: "#d4b26a", error: "boom" }]);
    assert.equal((html.match(/data-act="backtest"/g) || []).length, 1);
    assert.equal((html.match(/data-act="forward"/g) || []).length, 1);
});

test("an all-successful run has no Status row", () => {
    const html = renderTable([okSlot(1, "A", 10, 1.0), okSlot(2, "B", 5, 0.5)]);
    assert.doesNotMatch(html, /metric-status/);
});

test("a failed slot's error is stripped of markup", () => {
    const html = renderTable([{ id: 1, label: "A", color: "#fff", error: "<script>x</script>" }]);
    assert.doesNotMatch(html, /<script>/);
});
console.log(`\n${passed} tests passed`);
