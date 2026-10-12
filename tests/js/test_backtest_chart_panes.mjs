/**
 * Backtest result chart panes — behaviour tests.
 *
 * The adapter sends a run's series as objects (`{dates, values}` for equity and
 * drawdown, `{candles, buys, sells}` for signals). Slice 3 of the run ledger
 * wrapped the pane dispatch in `Array.isArray(data)`, which is false for every
 * real payload — so Equity curve, Drawdown and Price & signals rendered nothing
 * for any run, fresh or stored. That is invisible to a metrics-only check and to
 * the chart modules' own harnesses, which call the renderers directly.
 *
 * Pinned here:
 *  - each pane hands its renderer the canvas id and the object payload
 *  - a series with no points draws nothing and tears down the previous chart,
 *    so a run cannot inherit the last run's curve
 *  - an unknown pane or no result at all is inert, not an exception
 *
 * Usage: node tests/js/test_backtest_chart_panes.mjs
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import vm from "node:vm";
import assert from "node:assert/strict";

const root = fileURLToPath(new URL("../../", import.meta.url));
const code = readFileSync(
    path.join(root, "src/backtest/web/static/js/backtest.js"), "utf8");

// ------------------------------------------------------------------ tiny DOM
function makeCanvas(id) {
    return {
        id, dataset: {}, className: "", _destroyed: 0,
        addEventListener() {}, replaceChildren() {},
        querySelectorAll: () => [],
        destroy() { this._destroyed += 1; },
    };
}

const canvases = {};
["equityChart", "drawdownChart", "signalsChart"].forEach(
    (id) => { canvases[id] = makeCanvas(id); });

// The live chart attached to a canvas, per Chart.js `getChart`.
let attached = null;
// Recorded renderer calls; shared into the vm as the global `calls`.
const CALLS = [];

const sandbox = {
    console,
    calls: CALLS,
    fetch: () => Promise.resolve({ json: () => ({}) }),
    setTimeout: () => 0,
    document: {
        getElementById: (id) => canvases[id] || null,
        querySelector: () => null,
        querySelectorAll: () => [],
        addEventListener: () => {},
        createElement: () => makeCanvas("detached"),
    },
    window: { Chart: { getChart: () => attached } },
};
vm.createContext(sandbox);
vm.runInContext(code, sandbox);
["renderEquityChart", "renderDrawdownChart", "renderSignalsChart"].forEach((name) => {
    vm.runInContext(
        `${name} = (canvasId, data) => { calls.push(["${name}", canvasId, data]); };`,
        sandbox);
});

let passed = 0;
function test(name, fn) { fn(); passed += 1; console.log(`ok - ${name}`); }
const run = (pane, result) => {
    CALLS.length = 0;
    vm.runInContext(`lastRun = ${JSON.stringify({ config: {}, result })}`, sandbox);
    vm.runInContext(`renderChartForPane(${JSON.stringify(pane)})`, sandbox);
    return CALLS.slice();
};

const EQUITY = { dates: ["2026-01-01", "2026-01-02"], values: [100, 104], benchmark: [100, 101] };
const DRAWDOWN = { dates: ["2026-01-01", "2026-01-02"], values: [0, -0.02], worst_dd_pct: -2, worst_dd_date: "2026-01-02" };
const SIGNALS = { candles: [{ date: "2026-01-01", close: 100 }], buys: [], sells: [] };

test("equity pane plots the stored {dates, values} object", () => {
    const drawn = run("equity", { equity: EQUITY });
    assert.equal(drawn.length, 1, "renderEquityChart was never called");
    assert.equal(drawn[0][0], "renderEquityChart");
    assert.equal(drawn[0][1], "equityChart");
    assert.equal(JSON.stringify(drawn[0][2]), JSON.stringify(EQUITY));
});

test("drawdown pane plots its series", () => {
    const drawn = run("drawdown", { drawdown: DRAWDOWN });
    assert.deepEqual(drawn.map((c) => [c[0], c[1]]), [["renderDrawdownChart", "drawdownChart"]]);
    assert.equal(JSON.stringify(drawn[0][2]), JSON.stringify(DRAWDOWN));
});

test("signals pane plots candles, not an array test", () => {
    const drawn = run("signals", { signals: SIGNALS });
    assert.deepEqual(drawn.map((c) => [c[0], c[1]]), [["renderSignalsChart", "signalsChart"]]);
    assert.equal(JSON.stringify(drawn[0][2]), JSON.stringify(SIGNALS));
});

test("a series with no points plots nothing", () => {
    attached = null;
    assert.deepEqual(run("equity", { equity: { dates: [], values: [] } }), []);
    assert.deepEqual(run("equity", { equity: {} }), []);
    assert.deepEqual(run("signals", {}), []);
    assert.deepEqual(run("drawdown", { drawdown: null }), []);
});

test("an empty series tears down the previous chart instead of leaving it up", () => {
    attached = canvases.equityChart;
    canvases.equityChart._destroyed = 0;
    run("equity", { equity: { dates: [], values: [] } });
    assert.equal(canvases.equityChart._destroyed, 1);
    attached = null;
});

test("an empty series cannot crash the pane", () => {
    attached = null;
    run("equity", { equity: undefined });
    run("signals", { signals: { buys: [] } });
    run("metrics", { equity: EQUITY });
    run("equity", null);
});

test("the metrics tab is not a chart pane", () => {
    assert.deepEqual(run("metrics", { equity: EQUITY }), []);
});

console.log(`${passed} tests passed`);
