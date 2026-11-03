/**
 * The three states of an analytics read (finding F8).
 *
 * /analytics used to print "0 Active Strategies", "0 trades" and an em-dash
 * before the overview fetch resolved, and kept printing them after it failed —
 * so a slow read, a dead read and a genuinely empty book were three shades of
 * the same lie. The template now ships every placeholder in the fetching state
 * and analytics.js walks them through fetching → ready | empty | unavailable.
 *
 * These tests mount analytics.js against a stub DOM and drive the three reads
 * end to end: a rejected fetch, a success with nothing in it, and a success
 * with data. The assertions read the placeholder nodes' text AND their
 * data-state, because the text alone could be copied while the state (which
 * the CSS colours) rots.
 *
 * Usage: node tests/js/test_analytics_states.mjs
 */

import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import assert from "node:assert/strict";

const root = fileURLToPath(new URL("../../", import.meta.url));
const src = readFileSync(path.join(root, "src/backtest/web/static/js/analytics.js"), "utf8");
// base.html loads currency.js before every script, so the sandbox must too.
const currencyCode = readFileSync(
    path.join(root, "src/backtest/web/static/js/components/currency.js"), "utf8",
);
const labelsCode = readFileSync(
    path.join(root, "src/backtest/web/static/js/components/labels.js"), "utf8",
);

let passed = 0;
async function test(name, fn) {
    await fn();
    passed += 1;
    console.log(`  ✓ ${name}`);
}

const tick = () => new Promise((r) => setImmediate(r));

function makeNode(id) {
    const node = {
        id,
        textContent: "",
        innerHTML: "",
        hidden: false,
        dataset: {},
        style: {},
        children: [],
        classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
        appendChild(child) { node.children.push(child); return child; },
        querySelector() { return makeNode(`${id}-child`); },
        querySelectorAll() { return []; },
        addEventListener() {},
        getContext: () => ({}),
    };
    return node;
}

/** A page whose overview fetch behaves as told. Returns the nodes it rendered into. */
function mount(fetchImpl) {
    const nodes = {};
    const charts = [];
    const document = {
        readyState: "complete",
        getElementById: (id) => (nodes[id] = nodes[id] || makeNode(id)),
        querySelector: () => null,
        querySelectorAll: () => [],
        addEventListener() {},
        createElement: () => makeNode("created"),
        body: makeNode("body"),
    };
    const sandbox = {
        console: { error() {}, warn() {}, log() {} },
        document,
        fetch: fetchImpl,
        setTimeout: () => 0,
        clearTimeout() {},
        setInterval: () => 0,
        clearInterval() {},
        Chart: class {
            constructor(ctx, cfg) { charts.push(cfg); }
            destroy() {}
        },
        URLSearchParams, Object, Array, JSON, Number, String, Promise, Boolean,
        Math, Date, RegExp, Error, isNaN, parseInt, parseFloat,
    };
    sandbox.window = {
        location: { search: "" },
        history: { pushState() {} },
        addEventListener() {},
    };
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(currencyCode, sandbox, { filename: "currency.js" });
    // base.html also loads the shared label resolver before page scripts; the
    // `globalThis` tail publishes it the way a browser <script> tag would.
    vm.runInContext(labelsCode + "\n;globalThis.Labels = Labels;", sandbox, { filename: "labels.js" });
    vm.runInContext(src, sandbox, { filename: "analytics.js" });
    return { nodes, charts };
}

/** The default fetch: one overview request, canned payload or thrown error. */
function fetchOf(payload) {
    return () => (payload instanceof Error
        ? Promise.reject(payload)
        : Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(payload) }));
}

const METRICS = {
    total_return_pct: 1.5, total_pnl: 32942, sharpe_ratio: 1.2,
    win_rate: 43.6, total_trades: 1234567, winning_trades: 784, losing_trades: 842,
    max_drawdown_pct: 0.27, max_drawdown_amount: 1200,
};

await test("a failed read says unavailable everywhere, never zero", async () => {
    const { nodes } = mount(fetchOf(new Error("Failed to fetch")));
    await tick(); await tick();

    assert.equal(nodes.activeRunnersPill.textContent, "Unavailable");
    assert.equal(nodes.activeRunnersPill.dataset.state, "unavailable");
    assert.equal(nodes.overviewTradeCount.textContent, "unavailable");
    assert.equal(nodes.overviewTradeCount.dataset.state, "unavailable");
    assert.match(nodes.strategyTableBody.innerHTML, /Could not read the strategy table/);
    assert.equal(nodes.portfolioEquityEmpty.dataset.state, "unavailable");
    assert.equal(nodes.portfolioEquityEmpty.hidden, false);
    assert.equal(nodes.portfolioEquityEmptyTitle.textContent, "Equity curve unavailable");
    assert.equal(nodes.portfolioEquityChart.hidden, true);
    // the zero-as-a-fact strings must not survive the failure either
    assert.doesNotMatch(nodes.activeRunnersPill.textContent, /^0 /);
});

await test("a read still in flight says fetching, from the very first paint", async () => {
    let release;
    const gate = new Promise((r) => { release = r; });
    const { nodes, charts } = mount(() => gate.then(() => ({
        ok: true, status: 200,
        json: () => Promise.resolve({
            success: true, strategy_cards: [], active_runners: 0, total_runners: 0,
            portfolio_equity_curve: [], alerts: [],
        }),
    })));
    assert.equal(charts.length, 0);

    // nothing has resolved yet — this is the state the server-rendered HTML shows
    assert.equal(nodes.activeRunnersPill.textContent, "Fetching…");
    assert.equal(nodes.activeRunnersPill.dataset.state, "fetching");
    assert.equal(nodes.overviewTradeCount.textContent, "fetching…");
    assert.match(nodes.strategyTableBody.innerHTML, /Fetching strategy performance/);
    assert.equal(nodes.portfolioEquityEmpty.dataset.state, "fetching");
    assert.equal(nodes.portfolioEquityChart.hidden, true);
    release();
    await tick(); await tick();
    assert.equal(nodes.activeRunnersPill.dataset.state, "empty");
});

await test("an empty book says no strategies deployed, not 0", async () => {
    const { nodes, charts } = mount(fetchOf({
        success: true, strategy_cards: [], active_runners: 0, total_runners: 0,
        portfolio_metrics: METRICS, portfolio_equity_curve: [], alerts: [],
    }));
    await tick(); await tick();

    assert.equal(nodes.activeRunnersPill.textContent, "No strategies deployed");
    assert.equal(nodes.activeRunnersPill.dataset.state, "empty");
    assert.equal(nodes.noStrategiesEmpty.style.display, "block");
    assert.equal(nodes.portfolioEquityEmpty.dataset.state, "empty");
    assert.equal(nodes.portfolioEquityEmpty.hidden, false);
    assert.equal(nodes.portfolioEquityEmptyTitle.textContent, "No equity points in this period");
    assert.equal(charts.length, 0, "an empty curve must not build an axis-less chart");
});

await test("an arrived read flips every placeholder to ready and groups its counts", async () => {
    const { nodes, charts } = mount(fetchOf({
        success: true,
        strategy_cards: [{
            instance_id: "i1", name: "ema_pullback", strategy_name: "ema_pullback",
            mode: "paper", symbols: ["RELIANCE"], status: "RUNNING", metrics: METRICS,
            health: { badge: "🟢 Healthy", status: "green" }, mini_curve: [],
        }],
        active_runners: 1, total_runners: 2,
        portfolio_metrics: METRICS,
        portfolio_equity_curve: [{ date: "2026-11-01", equity: 100, drawdown_pct: 0 }, { date: "2026-11-02", equity: 101, drawdown_pct: 0 }],
        alerts: [],
    }));
    await tick(); await tick();

    assert.equal(nodes.activeRunnersPill.textContent, "1 / 2 Active Strategies");
    assert.equal(nodes.activeRunnersPill.dataset.state, "ready");
    assert.equal(nodes.overviewTradeCount.dataset.state, "ready");
    assert.equal(nodes.overviewTradeCount.textContent, "12,34,567 trades (784W / 842L)");
    assert.equal(nodes.portfolioEquityEmpty.hidden, true);
    assert.equal(nodes.portfolioEquityChart.hidden, false);
    assert.equal(charts.length, 1);
    // the comparison table's Trades column groups the same way
    const row = nodes.strategyTableBody.children[0];
    assert.match(row.innerHTML, /12,34,567/);
});

console.log(`\n${passed} tests passed`);
