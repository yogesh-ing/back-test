/**
 * Optimization UI helpers (static/js/optimize_common.js) — behaviour tests.
 *
 * The setup page shows "N combinations" before anything is submitted; that
 * number must equal what the backend will actually run
 * (ParameterSpec.size(), decimal-exact), or the user budgets for the wrong
 * run. The pytest wrapper (tests/optimization/test_js_helpers.py) generates
 * cases from the Python implementation and passes them as a JSON file, so
 * the two stay in lock-step; the literal cases below cover the float traps.
 *
 * Usage: node tests/js/test_optimize_common.mjs [parity_cases.json]
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import vm from "node:vm";
import assert from "node:assert/strict";

const root = fileURLToPath(new URL("../../", import.meta.url));
const code = readFileSync(
    path.join(root, "src/backtest/web/static/js/optimize_common.js"), "utf8",
);
const ctx = { console, setTimeout, clearTimeout };
ctx.globalThis = ctx;
vm.createContext(ctx);
vm.runInContext(code, ctx);
const C = ctx.OptCommon;
assert.ok(C, "OptCommon must be exported on globalThis");

let passed = 0;
function test(name, fn) {
    try {
        fn();
        passed += 1;
    } catch (err) {
        console.error(`FAIL ${name}\n${err.stack}`);
        process.exitCode = 1;
    }
}

// ------------------------------------------------------------- grid counting
test("gridCount integer ranges", () => {
    assert.equal(C.gridCount(5, 50, 5), 10);
    assert.equal(C.gridCount(5, 52, 5), 10); // last value 50, 55 exceeds max
    assert.equal(C.gridCount(7, 7, 1), 1);
});

test("gridCount is decimal-safe for float steps", () => {
    assert.equal(C.gridCount(0.1, 0.5, 0.1), 5); // naive float maths gives 4
    assert.equal(C.gridCount(0.3, 0.7, 0.1), 5);
    assert.equal(C.gridCount(1.0, 3.0, 0.25), 9);
    assert.equal(C.gridCount(0.001, 0.01, 0.001), 10);
});

test("gridCount rejects invalid input", () => {
    assert.equal(C.gridCount(10, 5, 1), 0);
    assert.equal(C.gridCount(1, 5, 0), 0);
    assert.equal(C.gridCount(1, 5, -1), 0);
    assert.equal(C.gridCount("x", 5, 1), 0);
});

test("totalCombinations multiplies checked rows only", () => {
    const rows = [
        { optimize: true, min: 5, max: 40, step: 5 },     // 8
        { optimize: true, min: 50, max: 200, step: 25 },  // 7
        { optimize: false, min: 1, max: 100, step: 1 },   // ignored
    ];
    assert.equal(C.totalCombinations(rows), 56);
    assert.equal(C.totalCombinations([]), 1);
});

// ------------------------------------------------------------------ formats
test("fmtMetric picks the unit per metric", () => {
    assert.equal(C.fmtMetric("total_return", 0.1234), "12.34%");
    assert.equal(C.fmtMetric("max_drawdown", -0.083), "-8.3%");
    assert.equal(C.fmtMetric("win_rate", 55), "55.0%"); // already a percentage
    assert.equal(C.fmtMetric("total_trades", 41.6), "42");
    assert.equal(C.fmtMetric("sharpe", null), "—");
    assert.equal(C.fmtMetric("sharpe", "abc"), "—");
});

test("fmtDelta formats changes in the metric's own unit", () => {
    assert.equal(C.fmtDelta("total_return", 0.4574), "+45.74%");
    assert.equal(C.fmtDelta("win_rate", 2.9), "+2.9 pp");
    assert.equal(C.fmtDelta("win_rate", -3), "-3.0 pp");
    assert.equal(C.fmtDelta("total_trades", -11), "-11");
    assert.equal(C.fmtDelta("expectancy", 3719.4), "+3719"); // no Money global here
    assert.ok(!C.fmtDelta("drawdown_duration_days", -1120).includes("."));
    assert.equal(C.fmtDelta("sharpe", 0), "0.000");
});

test("fmtPct signed flag and fmtDuration", () => {
    assert.equal(C.fmtPct(0.05, 1, { signed: true }), "+5.0%");
    assert.equal(C.fmtPct(-0.05, 1, { signed: true }), "-5.0%");
    assert.equal(C.fmtDuration(42), "42s");
    assert.equal(C.fmtDuration(125), "2m 05s");
    assert.equal(C.fmtDuration(3725), "1h 02m");
});

test("fmtParams marks engine knobs", () => {
    assert.equal(C.fmtParams({ fast: 5, "engine.delta_target": 0.4 }), "fast=5, ⚙delta_target=0.4");
    assert.equal(C.fmtParams(null), "—");
});

test("escapeHtml neutralises markup (param names come from plugins)", () => {
    assert.equal(C.escapeHtml('<img src=x onerror="a">'),
        "&lt;img src=x onerror=&quot;a&quot;&gt;");
    assert.equal(C.escapeHtml(null), "");
});

test("statusBadge escapes and classifies", () => {
    assert.ok(C.statusBadge("completed").includes("opt-status-ok"));
    assert.ok(C.statusBadge("<b>").includes("&lt;b&gt;"));
});

// ------------------------------------------------------------ heatmap maths
test("normalise ignores nulls and handles flat surfaces", () => {
    const n = C.normalise([[1, null], [3, 2]]);
    assert.equal(n.min, 1);
    assert.equal(n.max, 3);
    assert.equal(n.scale(2), 0.5);
    assert.ok(Number.isNaN(n.scale(null)));
    const flat = C.normalise([[2, 2]]);
    assert.equal(flat.scale(2), 0);
    assert.equal(C.normalise([[null]]).min, null);
});

test("heatColor clamps and is transparent for missing cells", () => {
    assert.equal(C.heatColor(NaN), "transparent");
    assert.equal(C.heatColor(-5), C.heatColor(0));
    assert.equal(C.heatColor(9), C.heatColor(1));
    assert.notEqual(C.heatColor(0), C.heatColor(1));
});

test("robustnessLabel bands", () => {
    assert.equal(C.robustnessLabel(8.1).text, "Robust");
    assert.equal(C.robustnessLabel(5).text, "Moderate");
    assert.equal(C.robustnessLabel(3.74).text, "Fragile");
    assert.equal(C.robustnessLabel(null).text, "n/a");
});

test("isImprovement knows drawdown is negative and some metrics are lower-better", () => {
    assert.equal(C.isImprovement("max_drawdown", -0.19, -0.12), true);
    assert.equal(C.isImprovement("max_drawdown", -0.12, -0.19), false);
    assert.equal(C.isImprovement("drawdown_duration_days", 1841, 721), true);
    assert.equal(C.isImprovement("sharpe", 0.39, 0.83), true);
    assert.equal(C.isImprovement("sharpe", null, 0.83), null);
});

// -------------------------------------------------- parity with the backend
const casesPath = process.argv[2];
if (casesPath) {
    const cases = JSON.parse(readFileSync(casesPath, "utf8"));
    test(`gridCount matches ParameterSpec.size() on ${cases.length} cases`, () => {
        const bad = cases.filter(([lo, hi, st, want]) => C.gridCount(lo, hi, st) !== want)
            .map(([lo, hi, st, want]) => `${lo}..${hi}/${st}: js=${C.gridCount(lo, hi, st)} py=${want}`);
        assert.deepEqual(bad, []);
    });
}

if (!process.exitCode) console.log(`ok - ${passed} optimize_common tests passed`);
