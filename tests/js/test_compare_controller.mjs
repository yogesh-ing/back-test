/**
 * Compare page controller — behaviour tests (PRD backTest-enhance §4.1/§4.2).
 *
 * The API tests prove the server honours per-slot symbols; the panel tests
 * prove the widgets render. What is left — and what is easiest to get quietly
 * wrong — is the controller in between: which fields the page actually SENDS.
 *
 * These pin the three claims §4 makes about the request:
 *   • every slot is sent the SHARED timeframe, not one of its own (§4.1)
 *   • generalization sends one strategy + one param set for N symbols (§4.2)
 *   • a failed slot still reaches the table, as a column with an error (§4.1)
 *
 * The DOM is a stub, not a parser: `innerHTML` is stored as text and
 * `querySelector` hands back a stub keyed by selector. That is enough to read
 * the controller's data flow without pretending to be a browser. Module-level
 * `let` bindings are read back through `ctx()` because they live in the
 * context's global lexical scope, not on the sandbox object.
 *
 * Usage: node tests/js/test_compare_controller.mjs
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import vm from "node:vm";
import assert from "node:assert/strict";

const root = fileURLToPath(new URL("../../", import.meta.url));
const read = (p) => readFileSync(path.join(root, p), "utf8");

// ------------------------------------------------------------------ tiny DOM
let elSeq = 0;

class El {
    constructor(id = "", cls = "") {
        this.id = id;
        this._cls = new Set(cls.split(" ").filter(Boolean));
        this._html = "";
        this._children = [];
        this.value = "";
        this.checked = false;
        this.hidden = false;
        this.textContent = "";
        this.dataset = {};
        this.style = {};
        this.attributes = {};
        this._listeners = {};
        this._seq = elSeq++;
    }
    get innerHTML() { return this._html; }
    set innerHTML(v) {
        this._html = String(v);
        this._children = [];
        // A real <select> with options and no explicit selection reports the
        // first option as its value. The controller reads `.value` right after
        // filling a symbol dropdown, so the stub has to agree or the test would
        // be asserting against a browser that does not exist.
        const first = this._html.match(/<option value="([^"]*)"/);
        if (first) this.value = first[1];
    }
    get className() { return [...this._cls].join(" "); }
    set className(v) { this._cls = new Set(String(v).split(" ").filter(Boolean)); }
    classList = {
        add: (...c) => c.forEach((x) => this._cls.add(x)),
        remove: (...c) => c.forEach((x) => this._cls.delete(x)),
        toggle: (c, on) => (on ? this._cls.add(c) : this._cls.delete(c)),
        contains: (c) => this._cls.has(c),
    };
    appendChild(child) { this._children.push(child); return child; }
    remove() { this._children = []; this.removed = true; }
    addEventListener(evt, fn) { (this._listeners[evt] ||= []).push(fn); }
    removeEventListener() {}
    // Own property, not a prototype method: the controller runs in a vm context
    // and V8's contextify proxy only forwards own properties across realms.
    fire = (evt, arg) => { (this._listeners[evt] || []).forEach((fn) => fn(arg)); };
    setAttribute(k, v) { this.attributes[k] = v; }
    getAttribute(k) { return this.attributes[k]; }
    querySelector(sel) { return (this._subs[sel] ||= new El(`${this.id}${sel}`)); }
    querySelectorAll() { return []; }
    get _subs() { return (this.__subs ||= {}); }
}

const byId = {};
const el = (id) => (byId[id] ||= new El(id));

// --------------------------------------------------------------- page state
const STRATEGIES = [
    { name: "sma_crossover" }, { name: "rsi_reversion" }, { name: "macd_trend" },
];
const paramsDoc = {
    sma_crossover: { fields: [{ name: "fast", default: 10 }, { name: "slow", default: 30 }] },
    rsi_reversion: { fields: [{ name: "period", default: 14 }] },
    macd_trend: { fields: [] },
};

function boot({ slotsResponse } = {}) {
    for (const k of Object.keys(byId)) delete byId[k];
    Object.assign(el("symbol"), { value: "INFY" });
    Object.assign(el("fromDate"), { value: "2022-01-01" });
    Object.assign(el("toDate"), { value: "2024-01-01" });
    Object.assign(el("capital"), { value: "100000" });
    Object.assign(el("sharedTimeframe"), { value: "1day" });
    el("genStrategy").value = "sma_crossover";

    const log = { bodies: [], tables: [], toasts: [], comparison: null, panes: [] };

    const sandbox = {
        console,
        document: {
            getElementById: el,
            createElement: (tag) => new El(tag),
            querySelector: (sel) => el(sel),
            querySelectorAll: () => [],
            addEventListener: (evt, fn) => { if (evt === "DOMContentLoaded") sandbox.__boot = fn; },
        },
        fetch: async (url, opts) => {
            // The Compare page asks for the compare venue, which is what keeps
            // option strategies out of its list; the stub answers either form
            // and records what was asked so a test can pin the venue.
            if (url === "/api/strategies" || url.startsWith("/api/strategies?")) {
                log.strategyUrl = url;
                return { ok: true, status: 200, json: async () => STRATEGIES };
            }
            if (url.startsWith("/api/strategies/")) {
                const name = decodeURIComponent(url.split("/")[3]);
                return { ok: true, status: 200, json: async () => paramsDoc[name] || { fields: [] } };
            }
            if (url === "/api/backtest/run-many") {
                log.bodies.push(JSON.parse(opts.body));
                return { ok: true, status: 200, json: async () => slotsResponse };
            }
            throw new Error(`unexpected fetch ${url}`);
        },
        SymbolPicker: {
            mount: () => ({
                select: "symbol",
                timeframesFor: () => ["1min", "5min", "1day"],
                symbols: () => ["INFY", "TCS", "RELIANCE", "HDFCBANK"],
                setValue: () => {},
            }),
        },
        Timeframes: { applyTo: () => ["1min", "5min", "1day"], toCanonical: (v) => v },
        renderParamsInto: (host, doc) => {
            host.__values = Object.fromEntries(doc.fields.map((f) => [f.name, f.default]));
        },
        collectParamsFrom: (host) => (host && host.__values) || {},
        renderCompareTable: (id, slots) => log.tables.push({ id, slots }),
        renderCompareEquity: (id, slots) => log.panes.push(["equity", slots]),
        renderCompareDrawdown: (id, slots) => log.panes.push(["drawdown", slots]),
        Provenance: { renderInto: () => {} },
        ComparePanels: { renderInto: (_id, c) => { log.comparison = c; } },
        SessionState: { compareSlots: [] },
        showToast: (msg, kind) => log.toasts.push({ msg, kind }),
        showLoader: () => {},
        Math, JSON, Object, Array, Number, String, Set, Map, Date, isFinite, Promise, Error,
    };
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(read("src/backtest/web/static/js/compare.js"), sandbox,
        { filename: "compare.js" });

    /** Read or run code inside the controller's own lexical scope. */
    const ctx = (code) => vm.runInContext(code, sandbox);
    return { sandbox, ctx, log };
}

const okResult = (ret = 10, sharpe = 1.0) => ({
    metrics: { total_return_pct: ret, sharpe, win_rate_pct: 50, closed_trades: 4,
               max_drawdown_pct: -5, total_trades: 8 },
    equity: { dates: ["2022-01-03", "2022-01-04"], values: [100_000, 110_000] },
});

/** Boot the page and settle the async init. */
async function ready(env) {
    await env.sandbox.__boot();
    await new Promise((r) => setTimeout(r, 0));
    return env;
}

let passed = 0;
const tests = [];
function test(name, fn) { tests.push([name, fn]); }

// ---------------------------------------------------------------- §4.1
test("the strategy list is fetched for the compare venue", async () => {
    // Options cannot be compared back here (the DB has candles, no chains), so
    // the page must ask the server for the compare-venue list, not the full one.
    const env = await ready(boot());
    assert.match(env.log.strategyUrl || "", /[?&]venue=compare\b/);
});

test("every slot is sent the shared timeframe, never one of its own", async () => {
    const env = await ready(boot({ slotsResponse: { results: { 1: okResult(), 2: okResult() } } }));
    // Point the two slots at different granularities by hand — the controller
    // must not be able to honour that any more.
    env.ctx("slots[0].timeframe = '5min'; slots[1].timeframe = '1min';");
    await env.sandbox.runAll();
    const body = env.log.bodies.at(-1);
    assert.equal(body.shared.capital, 100000);
    for (const slot of body.slots) {
        assert.equal(slot.timeframe, "1day", "timeframe is a shared condition (§4.1)");
    }
});

test("dates and capital are sent once, in the shared block", async () => {
    const env = await ready(boot({ slotsResponse: { results: { 1: okResult(), 2: okResult() } } }));
    await env.sandbox.runAll();
    const body = env.log.bodies.at(-1);
    assert.equal(body.shared.from_date, "2022-01-01");
    assert.equal(body.shared.to_date, "2024-01-01");
    for (const slot of body.slots) {
        assert.equal(slot.from_date, undefined, "dates live in shared, not per slot");
        assert.equal(slot.capital, undefined, "capital is shared too");
    }
});

test("strategies mode sends no per-slot symbol", async () => {
    const env = await ready(boot({ slotsResponse: { results: { 1: okResult(), 2: okResult() } } }));
    await env.sandbox.runAll();
    for (const slot of env.log.bodies.at(-1).slots) {
        assert.equal(slot.symbol, undefined, "strategies mode compares on the shared symbol");
    }
});

test("a failed slot still reaches the table as a column", async () => {
    const env = await ready(boot({
        slotsResponse: {
            results: { 1: okResult(), 2: { error: "unknown strategy: nope" } },
            provenance: {},
            comparison: { correlation: { available: false }, significance: { available: false } },
        },
    }));
    await env.sandbox.runAll();
    const rendered = env.log.tables.at(-1);
    assert.equal(rendered.slots.length, 2, "the failed slot keeps its column (§4.1)");
    const failed = rendered.slots.find((x) => x.result && x.result.error);
    assert.ok(failed, "with its error intact");
    assert.match(failed.result.error, /unknown strategy/);
});

test("a failed slot is never sent to a chart", async () => {
    const env = await ready(boot({
        slotsResponse: {
            results: { 1: okResult(), 2: { error: "boom" } },
            provenance: {},
            comparison: null,
        },
    }));
    await env.sandbox.runAll();
    env.sandbox.renderChartForPane("equity");
    const [, slots] = env.log.panes.at(-1);
    assert.equal(slots.length, 1, "a chart has nothing to draw for a failed slot");
});

test("a partial failure is reported as a warning, not a success", async () => {
    const env = await ready(boot({
        slotsResponse: {
            results: { 1: okResult(), 2: { error: "boom" } },
            provenance: {},
            comparison: null,
        },
    }));
    await env.sandbox.runAll();
    const last = env.log.toasts.at(-1);
    assert.equal(last.kind, "warning");
    assert.match(last.msg, /1 failed/);
});

test("the comparison panels receive the server's block", async () => {
    const env = await ready(boot({
        slotsResponse: {
            results: { 1: okResult() },
            provenance: {},
            comparison: { correlation: { available: true, labels: ["A"], matrix: [[1]] } },
        },
    }));
    await env.sandbox.runAll();
    assert.ok(env.log.comparison, "the Comparison tab is populated from the response");
    assert.equal(env.log.comparison.correlation.labels[0], "A");
});

// ---------------------------------------------------------------- §4.2
test("generalization sends ONE strategy and param set for every symbol", async () => {
    const env = await ready(boot({ slotsResponse: { results: { 1: okResult(), 2: okResult(), 3: okResult() } } }));
    env.sandbox.setMode("generalization");
    env.ctx("slots[0].symbol = 'INFY'; slots[1].symbol = 'TCS'; addSlot(); slots[2].symbol = 'RELIANCE';");
    await env.sandbox.runAll();
    const body = env.log.bodies.at(-1);
    assert.equal(body.slots.length, 3);
    assert.equal(new Set(body.slots.map((x) => x.strategy)).size, 1, "one strategy (§4.2)");
    assert.equal(new Set(body.slots.map((x) => JSON.stringify(x.params))).size, 1, "one param set (§4.2)");
    assert.deepEqual(body.slots.map((x) => x.symbol).sort(), ["INFY", "RELIANCE", "TCS"]);
});

test("generalization is capped at four symbols", async () => {
    const env = await ready(boot());
    env.sandbox.setMode("generalization");
    env.ctx("addSlot(); addSlot(); addSlot(); addSlot(); addSlot();");
    assert.equal(env.ctx("slots.length"), 4, "§4.2 allows up to four symbols");
});

test("generalization still shares dates, capital and timeframe", async () => {
    const env = await ready(boot({ slotsResponse: { results: { 1: okResult(), 2: okResult() } } }));
    env.sandbox.setMode("generalization");
    await env.sandbox.runAll();
    const body = env.log.bodies.at(-1);
    assert.equal(body.shared.capital, 100000);
    assert.equal(body.shared.from_date, "2022-01-01");
    for (const slot of body.slots) {
        assert.equal(slot.timeframe, "1day");
        assert.ok(slot.symbol, "each slot names its own symbol");
    }
});

test("switching modes rebuilds the slots rather than mutating them", async () => {
    const env = await ready(boot());
    const before = env.ctx("slots.length");
    env.sandbox.setMode("generalization");
    assert.ok(env.ctx("slots.every(s => s.symbol !== undefined)"), "slots are symbol rows now");
    assert.ok(env.ctx("slots.every(s => s.strategy === '')"), "no per-slot strategy left over");
    env.sandbox.setMode("strategies");
    assert.equal(env.ctx("slots.length"), before, "switching back does not accumulate slots");
    assert.ok(env.ctx("slots.every(s => s.symbol === '')"), "no per-slot symbol left over");
});

test("the slot label names what distinguishes it in each mode", async () => {
    const env = await ready(boot());
    env.ctx("slots[0].strategy = 'sma_crossover'; slots[0].symbol = 'INFY';");
    assert.equal(env.ctx("slots[0].label"), "sma_crossover");
    env.sandbox.setMode("generalization");
    // Change the symbol the way the page does — set the <select> and fire the
    // event it would fire — rather than poking the model directly.
    env.ctx(`
        var sel = slots[0].card.querySelector('.slot-symbol');
        sel.value = 'TCS';
        sel.fire('change');
    `);
    assert.equal(env.ctx("slots[0].symbol"), "TCS");
    assert.equal(env.ctx("slots[0].label"), "TCS", "in generalization the symbol is the axis label");
});

test("generalization hides the shared symbol and timeframe controls", async () => {
    const env = await ready(boot());
    env.sandbox.setMode("generalization");
    assert.equal(el("generalizationSetup").hidden, false);
    assert.equal(el("sharedTimeframeRow").hidden, true, "the symbol pane is not part of this mode");
    env.sandbox.setMode("strategies");
    assert.equal(el("generalizationSetup").hidden, true);
    assert.equal(el("sharedTimeframeRow").hidden, false);
});

// ---------------------------------------------------------------------- run
let failed = 0;
for (const [name, fn] of tests) {
    try {
        await fn();
        passed += 1;
        console.log(`  ✓ ${name}`);
    } catch (err) {
        failed += 1;
        console.log(`  ✗ ${name}\n      ${err.message.split("\n")[0]}`);
    }
}
console.log(`\n${passed} tests passed${failed ? `, ${failed} FAILED` : ""}`);
if (failed) process.exitCode = 1;
