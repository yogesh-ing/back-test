/**
 * Forward page live widgets — behaviour tests (PRD 4.2, gap G3).
 *
 * The Forward page shipped with a live equity chart, live metric cards and a
 * progress line that never rendered: the chart was handed a bare number array
 * (renderEquityChart expects {dates, values}), the metric-card renderer was
 * loaded but never called, and #progressText was never written. These tests drive
 * the real renderLive() so those widgets cannot silently regress again.
 *
 * Usage: node tests/js/test_forward_widgets.mjs
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import vm from "node:vm";
import assert from "node:assert/strict";

const root = fileURLToPath(new URL("../../", import.meta.url));
const forwardCode = readFileSync(path.join(root, "src/backtest/web/static/js/forward.js"), "utf8");
const currencyCode = readFileSync(
    path.join(root, "src/backtest/web/static/js/components/currency.js"), "utf8",
);

// ----------------------------------------------------------------- stub DOM
function makeEl(id) {
    return {
        id, innerHTML: "", textContent: "", className: "", hidden: false, disabled: false,
        value: "", title: "", style: {}, dataset: {},
        classList: { _s: new Set(), add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
                     contains(c) { return this._s.has(c); } },
        querySelector() { return makeEl(`${id}>q`); },
        querySelectorAll() { return []; },
        addEventListener() {},
        setAttribute() {},
        appendChild(c) { return c; },
        prepend(c) { return c; },
    };
}
const elements = {};
const el = (id) => (elements[id] = elements[id] || makeEl(id));

const calls = { equity: [], metrics: [], tradeTable: [], fetch: [] };

const sandbox = {
    console,
    setTimeout: () => 0, clearTimeout: () => {}, setInterval: () => 0, clearInterval: () => {},
    CustomEvent: class { constructor(t, o) { this.type = t; this.detail = o && o.detail; } },
    Date, Number, Math, String, JSON, Promise, Object, Array, encodeURIComponent,
    document: {
        // renderLive() itself touches no document, but the module body may look
        // for a <body> while wiring, so expose a permissive one.
        getElementById: (id) => el(id),
        createElement: () => makeEl("created"),
        querySelector: () => el("document>q"),
        addEventListener: () => {},
        body: { dataset: { currencySymbol: "₹", currencyCode: "INR", currencyLocale: "en-IN" } },
    },
    fetch: async (url, opts) => {
        calls.fetch.push(url);
        return { ok: true, status: 200, json: async () => ({ status: "idle" }) };
    },
};
sandbox.globalThis = sandbox;
sandbox.showToast = () => {};
sandbox.SessionState = { forwardPrefill: null, keys: {}, clear: () => {}, get: () => null };
sandbox.renderParamsInto = () => {};
sandbox.applyOverridesInto = () => {};
sandbox.collectParamsFrom = () => ({});
// The three widgets under test: record what forward.js hands them.
sandbox.renderEquityChart = (canvasId, data) => { calls.equity.push({ canvasId, data }); };
sandbox.renderMetricsCards = (containerId, metrics) => { calls.metrics.push({ containerId, metrics }); };
sandbox.TradeTable = { render: (containerId, trades) => { calls.tradeTable.push({ containerId, trades }); } };

vm.createContext(sandbox);
vm.runInContext(currencyCode, sandbox, { filename: "currency.js" });
vm.runInContext(forwardCode, sandbox, { filename: "forward.js" });

const { renderLive } = sandbox;

let passed = 0;
function test(name, fn) {
    fn();
    passed += 1;
    console.log(`  ✓ ${name}`);
}

const SNAPSHOT = {
    state_id: "abc123",
    status: "running",
    progress: { revealed: 13, total: 65, pct: 20 },
    metrics: {
        total_pnl: -415.6, total_return_pct: -4.16, win_rate_pct: 0,
        max_drawdown_pct: -4.33, sharpe: -3.79, total_trades: 1,
        closed_trades: 0, open_trades: 1, final_equity: 9584.4,
    },
    equity: { dates: ["2024-01-01", "2024-01-02", "2024-01-03"],
              values: [10000, 9900, 9584.4],
              benchmark: [10000, 10100, 10200] },
    drawdown: { dates: ["2024-01-01"], values: [0], worst_dd_pct: -4.33, worst_dd_date: "2024-01-03" },
    trades: [{ id: 1, date: "2024-03-11", exit_date: "2024-03-20", side: "LONG", entry: 111.57,
               exit: 106.75, pnl: -415.6, result: "Loss", is_open: true }],
    signals: { candles: [], buys: [], sells: [] },
    positions: [{ symbol: "DEMO", side: "LONG", qty: 1, exposure_pct: 100, entry: 111.57,
                  current: 106.75, price_change_pct: -4.32, unrealized_pnl: -415.6,
                  unrealized_pnl_pct: -4.34, entry_date: "2024-03-11", bars_held: 9 }],
    config: { strategy: "sma_crossover", symbol: "DEMO" },
    total_bars: 13, total_trades: 1, last_bar_ts: "2024-01-19", market_open: true,
    unrealized_pnl: -415.6, error: null,
};

test("the live equity chart receives the adapter object, not a bare array", () => {
    calls.equity.length = 0;
    renderLive(SNAPSHOT);
    assert.equal(calls.equity.length, 1, "renderEquityChart must be called on each snapshot");
    const { canvasId, data } = calls.equity[0];
    assert.equal(canvasId, "equityChart");
    assert.ok(data && !Array.isArray(data), "must be an object with dates/values");
    assert.deepEqual(data.dates, SNAPSHOT.equity.dates);
    assert.deepEqual(data.values, SNAPSHOT.equity.values);
    assert.deepEqual(data.benchmark, SNAPSHOT.equity.benchmark);
});

test("live metric cards are rendered from the snapshot", () => {
    calls.metrics.length = 0;
    renderLive(SNAPSHOT);
    assert.equal(calls.metrics.length, 1, "renderMetricsCards was never called before G3");
    assert.equal(calls.metrics[0].containerId, "metricsCards");
    assert.equal(calls.metrics[0].metrics.total_pnl, -415.6);
    assert.equal(calls.metrics[0].metrics.closed_trades, 0);
});

test("progress text and bar track the cursor", () => {
    renderLive(SNAPSHOT);
    assert.equal(el("progressText").textContent, "13 / 65 bars · 20%");
    assert.equal(el("progressFill").style.width, "20%");
});

test("positions render entry, current and the mark-to-market P&L", () => {
    renderLive(SNAPSHOT);
    const html = el("positionsBody").innerHTML;
    assert.match(html, /DEMO/);
    assert.match(html, /₹111\.57/);
    assert.match(html, /₹106\.75/);
    assert.match(html, /-4\.34%/, "unrealised % must come from the payload");
    assert.match(html, /-₹415\.60/);
    assert.match(html, /9b/, "bars held is shown");
});

test("the trade feed reuses the shared table component", () => {
    calls.tradeTable.length = 0;
    renderLive(SNAPSHOT);
    assert.equal(calls.tradeTable.length, 1);
    assert.equal(calls.tradeTable[0].containerId, "tradeTable-wrap");
    assert.equal(calls.tradeTable[0].trades.length, 1);
});

test("rendering one snapshot costs no extra HTTP round trip", () => {
    calls.fetch.length = 0;
    renderLive(SNAPSHOT);
    assert.deepEqual(calls.fetch, [], "the /api/forward/equity double-fetch is gone");
});

test("an idle/empty snapshot leaves the widgets alone instead of throwing", () => {
    calls.equity.length = 0;
    calls.metrics.length = 0;
    renderLive({ status: "idle", progress: { revealed: 0, total: 0, pct: 0 } });
    assert.equal(calls.equity.length, 0);
    assert.equal(calls.metrics.length, 0);
});

console.log(`\nforward live widgets: ${passed} tests passed`);
