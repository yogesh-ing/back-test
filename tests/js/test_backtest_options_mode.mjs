/**
 * Options mode wiring on the Backtest tab (plan V3 §4).
 *
 * Pins the controller claims that a browser would otherwise have to verify by
 * eye: the toggle gates a separate endpoint, the model fields ride the config
 * only when options mode is on, the mock-data banner appears with the toggle,
 * the vol-regime note renders from the payload, and a model-priced run is kept
 * out of the equity comparison (decision a).
 *
 * Usage: node tests/js/test_backtest_options_mode.mjs
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import vm from "node:vm";
import assert from "node:assert/strict";

const root = fileURLToPath(new URL("../../", import.meta.url));
const code = readFileSync(path.join(root, "src/backtest/web/static/js/backtest.js"), "utf8");

function makeEl(id) {
    return {
        id, innerHTML: "", textContent: "", value: "", hidden: false,
        disabled: false, checked: false, style: {}, dataset: {}, attrs: {},
        classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
        setAttribute(k, v) { this.attrs[k] = v; },
        removeAttribute(k) { delete this.attrs[k]; },
        getAttribute(k) { return this.attrs[k] ?? null; },
        focus() {},
        querySelector(sel) { return makeEl(`${id}::${sel}`); },
        querySelectorAll() { return []; },
        addEventListener() {},
        selectedOptions: [],
    };
}

function boot() {
    const elements = {};
    const el = (id) => (elements[id] = elements[id] || makeEl(id));
    const fetchCalls = [];
    const toasts = [];
    const compareSlots = [];
    const sandbox = {
        document: {
            getElementById: el,
            querySelector: () => makeEl("q"),
            querySelectorAll: () => [],
            createElement: () => makeEl("created"),
            body: { dataset: {}, appendChild() {}, classList: { add() {}, remove() {} } },
            addEventListener() {},
        },
        window: { location: { href: "", pathname: "/backtest" }, addEventListener() {}, Chart: undefined },
        fetch: async (url, opts) => {
            fetchCalls.push({ url, opts });
            return {
                ok: true, status: 200,
                json: async () => ({
                    metrics: {}, trades: [], equity: {}, readiness: {}, persisted: true,
                    pricing_feed: "model_bs", config: { engine: "options" },
                    vol_regime: { note: "Avg IV during backtest: 14.2% (low-vol regime)." },
                }),
            };
        },
        setTimeout: () => undefined,
        showToast: (msg, kind) => toasts.push({ msg, kind }),
        showLoader() {}, hideLoader() {},
        Timeframes: { toCanonical: (v) => v, applyTo() {} },
        collectParamsFrom: () => ({}), renderParamsInto() {}, applyOverridesInto() {},
        renderMetricsCards() {}, renderEquityChart() {}, renderDrawdownChart() {}, renderSignalsChart() {},
        Provenance: { renderInto() {} }, MetricSections: { renderInto() {} },
        RunChecks: { renderInto() {} }, Certification: { renderInto() {} },
        TuneThis: { mintResultId: () => "res-1", renderInto() {} },
        TradeTable: { render() {} },
        Labels: { strategy: (v) => v, warm: async () => {} },
        SymbolPicker: { mount: () => ({ timeframesFor: () => [], setValue() {} }) },
        SessionState: {
            addCompareSlot: (run) => { compareSlots.push(run); return { ok: true, index: compareSlots.length }; },
            maxCompareSlots: 4, keys: {}, clear() {},
        },
        DataSourceGate: undefined,
        console,
    };
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(code, sandbox);
    return { sandbox, el, fetchCalls, toasts, compareSlots };
}

let passed = 0;
function test(name, fn) { fn(); passed += 1; console.log(`  ✓ ${name}`); }

// The section starts hidden (kill switch off by default in the DOM).
function revealAndToggle(sandbox, el, on) {
    el("optionsModelSection").hidden = false;
    el("optionsMode").checked = on;
}

test("options mode is off while the section is hidden, even if checked", () => {
    const { sandbox, el } = boot();
    el("optionsMode").checked = true;
    el("optionsModelSection").hidden = true;
    assert.equal(sandbox.optionsModeOn(), false);
});

test("options mode is on only when revealed AND checked", () => {
    const { sandbox, el } = boot();
    revealAndToggle(sandbox, el, true);
    assert.equal(sandbox.optionsModeOn(), true);
    revealAndToggle(sandbox, el, false);
    assert.equal(sandbox.optionsModeOn(), false);
});

test("an equity run config carries no model fields", () => {
    const { sandbox, el } = boot();
    el("strategy").value = "sma_crossover";
    el("symbol").value = "DEMO";
    const cfg = sandbox.collectConfig();
    assert.equal(cfg.pricing_feed, undefined);
    assert.equal(cfg.model_params, undefined);
});

test("an options run config carries the feed, exits and IV override", () => {
    const { sandbox, el } = boot();
    revealAndToggle(sandbox, el, true);
    el("strategy").value = "sma_crossover";
    el("symbol").value = "DEMO";
    el("pricingFeed").value = "model_bs";
    el("modelIv").value = "20";
    const cfg = sandbox.collectConfig();
    assert.equal(cfg.pricing_feed, "model_bs");
    assert.equal(JSON.stringify(cfg.model_params), JSON.stringify({ iv_source: "constant", iv_constant: 0.2 }));
    assert.equal(cfg.exits.exit_on_signal_flip, true);
});

test("a blank IV override leaves model_params empty (realized vol)", () => {
    const { sandbox, el } = boot();
    revealAndToggle(sandbox, el, true);
    el("modelIv").value = "";
    assert.equal(JSON.stringify(sandbox.collectConfig().model_params), "{}");
});

test("the mock-data banner appears with the toggle and clears without it", () => {
    const { sandbox, el } = boot();
    revealAndToggle(sandbox, el, true);
    sandbox.syncOptionsModeUI();
    assert.equal(el("optionsModelBanner").hidden, false);
    assert.match(el("optionsModelBanner").innerHTML, /MODEL-PRICED/);
    assert.equal(el("pricingFeedRow").hidden, false);
    revealAndToggle(sandbox, el, false);
    sandbox.syncOptionsModeUI();
    assert.equal(el("optionsModelBanner").hidden, true);
    assert.equal(el("pricingFeedRow").hidden, true);
});

test("the vol-regime note renders from the payload and hides when absent", () => {
    const { sandbox, el } = boot();
    sandbox.renderVolRegimeNote({ note: "Avg IV during backtest: 14.2% (low-vol regime)." });
    assert.equal(el("volRegimeNote").hidden, false);
    assert.match(el("volRegimeNote").textContent, /low-vol regime/);
    sandbox.renderVolRegimeNote(null);
    assert.equal(el("volRegimeNote").hidden, true);
});

test("a model-priced run is kept out of the equity comparison (decision a)", () => {
    const { sandbox, el, toasts, compareSlots } = boot();
    vm.runInContext("lastRun = { result: { pricing_feed: 'model_bs' } };", sandbox);
    sandbox.saveToCompare();
    assert.equal(compareSlots.length, 0, "no compare slot may be added for a model run");
    assert.ok(toasts.some((t) => t.kind === "warning" && /can't be compared/.test(t.msg)));
});

test("an equity run still saves to compare", () => {
    const { sandbox, compareSlots } = boot();
    vm.runInContext("lastRun = { result: { pricing_feed: null } };", sandbox);
    sandbox.saveToCompare();
    assert.equal(compareSlots.length, 1);
});

// --- the endpoint switch (async) --------------------------------------------
async function runOptions() {
    const { sandbox, el, fetchCalls } = boot();
    revealAndToggle(sandbox, el, true);
    el("strategy").value = "sma_crossover";
    el("symbol").value = "DEMO";
    el("fromDate").value = "2021-01-01";
    el("toDate").value = "2024-01-01";
    el("pricingFeed").value = "model_bs";
    await sandbox.runBacktest();
    return fetchCalls;
}

const calls = await runOptions();
assert.ok(calls.some((c) => c.url === "/api/backtest/options-model"),
    `options mode must POST to the model endpoint, got ${calls.map((c) => c.url)}`);
passed += 1;
console.log("  ✓ options mode POSTs to /api/backtest/options-model");

console.log(`\n${passed} tests passed`);