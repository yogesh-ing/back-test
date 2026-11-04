/**
 * BT-02 — a failed backtest must stop claiming that one is running.
 *
 * runBacktest() paints a "Running backtest…" loader into #metricsCards before
 * the POST and, until this slice, only cleared it on the success path. A failed
 * run therefore left a spinner directly under the sticky banner saying it had
 * failed — two opposite states on screen at once. This is the same untruthful-
 * state class as F8 (docs/UI-QA-VISUAL-PASS-2026-11-03.md): a loading claim may
 * only be shown while the request is actually in flight.
 *
 * Usage: node tests/js/test_backtest_failure_clears_loader.mjs
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import vm from "node:vm";
import assert from "node:assert/strict";

const root = fileURLToPath(new URL("../../", import.meta.url));
const code = readFileSync(path.join(root, "src/backtest/web/static/js/backtest.js"), "utf8");

const LOADER_MARKUP = /class="loader"/;

function makeEl(id) {
    return {
        id,
        innerHTML: "",
        textContent: "",
        value: "",
        hidden: false,
        disabled: false,
        checked: false,
        style: {},
        dataset: {},
        attrs: {},
        classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
        setAttribute(k, v) { this.attrs[k] = v; },
        removeAttribute(k) { delete this.attrs[k]; },
        getAttribute(k) { return this.attrs[k] ?? null; },
        focus() {},
        querySelector(sel) { return makeEl(`${id}::${sel}`); },
        querySelectorAll() { return []; },
        addEventListener() {},
    };
}

/** Boot backtest.js with the given POST outcome and return the sandbox + elements. */
function boot({ ok, status = 200, errorBody, onLoaderShown }) {
    const elements = {};
    const el = (id) => (elements[id] = elements[id] || makeEl(id));
    const sandbox = {
        document: {
            getElementById: el,
            querySelector: () => makeEl("q"),
            querySelectorAll: () => [],
            createElement: () => makeEl("created"),
            body: { dataset: {}, appendChild() {}, classList: { add() {}, remove() {} } },
            addEventListener() {},
        },
        window: {
            location: { href: "", pathname: "/backtest" },
            addEventListener() {},
            Chart: undefined,
            DataSourceGate: undefined,
        },
        fetch: async () => ({
            ok,
            status,
            json: async () => (ok ? errorBody : errorBody),
        }),
        setTimeout: (fn) => fn && undefined,
        showToast() {},
        showLoader(id, message) {
            const node = el(id);
            node.innerHTML = `<div class="loader"><div class="spinner"></div><div class="muted">${message}</div></div>`;
            if (onLoaderShown) onLoaderShown(node.innerHTML);
        },
        hideLoader(id) {
            const node = el(id);
            node.innerHTML = "";
        },
        Timeframes: { toCanonical: (v) => v },
        // the shared dynamic-param helpers live in components/params.js
        collectParamsFrom: () => ({}),
        renderParamsInto() {},
        applyOverridesInto() {},
        renderMetricsCards() {},
        Labels: { strategy: (v) => v },
        Money: { signed: (v) => String(v), count: (v) => String(v) },
        TuneThis: { mintResultId: () => "res-1" },
        SymbolPicker: { mount: () => ({ timeframesFor: () => [] }) },
        Chart: undefined,
        console,
    };
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(code, sandbox);
    return { sandbox, el, elements };
}

async function run(sandbox) {
    // The form must pass the three guards or runBacktest returns before the POST.
    const set = (id, value) => { sandbox.document.getElementById(id).value = value; };
    set("strategy", "bollinger_reversion");
    set("symbol", "TCS");
    set("fromDate", "2019-01-01");
    set("toDate", "2019-12-31");
    await sandbox.runBacktest();
}

const DATA_ERROR = "data error: Symbol 'TCS' has no 1day data between 2019-01-01 and 2019-12-31.";

// 1. A failed run ends the loading claim and says so, once.
{
    const { sandbox, el } = boot({ ok: false, status: 400, errorBody: { error: DATA_ERROR, request_id: "abcd1234" } });
    await run(sandbox);
    assert.equal(
        LOADER_MARKUP.test(el("metricsCards").innerHTML),
        false,
        `a failed run must not leave a spinner on screen, got: ${el("metricsCards").innerHTML}`,
    );
    assert.equal(el("runError").hidden, false, "the sticky failure banner must be shown");
    assert.match(el("runError").textContent, /data error/);
    assert.equal(el("results").hidden, true, "the result pane must go away with the run that failed");
    assert.equal(el("emptyState").hidden, false, "the page returns to its empty state");
    assert.equal(el("runBtn").disabled, false, "the operator may correct the dates and run again");
    assert.equal(el("runBtn").getAttribute("aria-busy"), null, "aria-busy must not outlive the run");
}

// 2. The loader is still shown while a run is in flight — the fix must not
//    delete the only honest "this is working" signal, and must not clear it
//    before the response lands.
{
    const shown = [];
    const { sandbox, el } = boot({
        ok: true, status: 200, onLoaderShown: (html) => shown.push(html),
        errorBody: { metrics: {}, trades: [], equity: {}, readiness: {}, persisted: true },
    });
    await run(sandbox);
    assert.equal(shown.length, 1, `the loader must be painted once while the run is in flight, got ${shown.length}`);
    assert.match(shown[0], /Running backtest…/);
    assert.equal(el("metricsCards").innerHTML, "", "the loader must be gone once the run answered");
}

console.log("2 tests passed");
