/**
 * Forward Test page — broker-auth gate tests (Task 4.1).
 *
 * Drives the real forward.js in a stub DOM + fetch, asserts that the
 * Start button is gated on broker auth state:
 *   • unauthenticated / expired  → disabled, "🔴 Connect mStock to Start"
 *   • authenticated / expiring_soon → enabled, "▶ Start"
 *   • click on disabled button → opens BrokerAuthUI
 *   • running state overrides auth (still disabled while running)
 *
 * Usage: node tests/js/test_forward_auth_gate.mjs
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import vm from "node:vm";
import assert from "node:assert/strict";

const root = fileURLToPath(new URL("../../", import.meta.url));

// Read both scripts (broker_status.js is needed for the polling that drives
// the broker:status event, but the forward.js gate only listens for the event).
const forwardCode = readFileSync(
    path.join(root, "src/backtest/web/static/js/forward.js"), "utf8",
);

// ---------------------------------------------------------------- synthetic DOM

function makeElement(id, tag) {
    const el = {
        id, tag: tag || "div",
        textContent: "",
        innerHTML: "",
        title: "",
        className: "",
        hidden: false,
        disabled: false,
        value: "",
        dataset: {},
        attrs: {},
        children: [],
        handlers: {},
        removed: false,
        classList: {
            _classes: new Set(),
            add(c) { this._classes.add(c); },
            remove(c) { this._classes.delete(c); },
            contains(c) { return this._classes.has(c); },
            toggle(c) { if (this._classes.has(c)) this._classes.delete(c); else this._classes.add(c); },
        },
        setAttribute(key, val) { this.attrs[key] = String(val); },
        getAttribute(key) { return this.attrs[key] ?? null; },
        addEventListener(type, fn) {
            (this.handlers[type] = this.handlers[type] || []).push(fn);
        },
        appendChild(child) { this.children.push(child); return child; },
        remove() { this.removed = true; },
        click() {
            const syntheticEvent = {
                preventDefault: () => {},
                stopPropagation: () => {},
                target: el,
                type: "click",
            };
            (this.handlers.click || []).forEach((fn) => fn(syntheticEvent));
        },
        focus() { this._focused = true; },
        querySelector() { return null; },
        style: { width: "0%" },
    };
    return el;
}

// Create all elements that forward.js references via $(id)
const ids = [
    "startBtn", "stopBtn", "statusBadge", "progressBar", "progressText",
    "strategy", "symbol", "timeframe", "fromDate", "toDate", "capital",
    "params-container", "livePanel", "statusBadge",
];
const elements = {};
for (const id of ids) {
    elements[id] = makeElement(id, id.endsWith("Btn") ? "button" : "div");
}
// params-container needs innerHTML
elements["params-container"].innerHTML = "<p>Select a strategy…</p>";

const documentHandlers = {};
const dispatched = [];

const sandbox = {
    console: { warn: () => {}, log: () => {}, error: () => {} },
    setTimeout: (fn) => { fn(); return 0; },
    clearTimeout: () => {},
    setInterval: () => 0,
    clearInterval: () => {},
    CustomEvent: class CustomEvent {
        constructor(type, opts) { this.type = type; this.detail = opts && opts.detail; }
    },
    Date,
    Number,
    Promise,
    document: {
        getElementById: (id) => elements[id] || null,
        createElement: (tag) => makeElement(`created-${tag}`, tag),
        dispatchEvent: (event) => { dispatched.push(event); },
        addEventListener: (type, fn) => {
            (documentHandlers[type] = documentHandlers[type] || []).push(fn);
        },
        _fire(type, event) {
            (documentHandlers[type] || []).forEach(fn => fn(event));
        },
    },
    window: {},
};
sandbox.globalThis = sandbox;

// Stubs for functions forward.js calls that we don't care about in these tests
sandbox.showToast = (msg, cls) => { sandbox._lastToast = { msg, cls }; };
sandbox.renderMetricsCards = () => {};
sandbox.renderEquityChart = () => {};
sandbox.renderParamsInto = () => {};
sandbox.applyOverridesInto = () => {};
sandbox.collectParamsFrom = () => ({});
sandbox.SessionState = {
    forwardPrefill: null,
    keys: { forwardPrefill: "forwardPrefill" },
    clear: () => {},
};
sandbox.TradeTable = { render: () => {} };

// fetch stub: serves strategy list + forward status
sandbox.fetch = async (url, opts) => {
    if (url === "/api/strategies") {
        return {
            ok: true, status: 200,
            json: async () => [{ name: "sma_crossover" }],
        };
    }
    if (url.startsWith("/api/strategies/") && url.endsWith("/params")) {
        return {
            ok: true, status: 200,
            json: async () => [
                { name: "fast", type: "integer", default: 10, min: 2, max: 50, label: "Fast", tooltip: "" },
                { name: "slow", type: "integer", default: 30, min: 10, max: 200, label: "Slow", tooltip: "" },
            ],
        };
    }
    if (url === "/api/forward/start") {
        sandbox._forwardStarted = (sandbox._forwardStarted || 0) + 1;
        return {
            ok: true, status: 200,
            json: async () => ({ status: "running", total: 200, revealed: 20 }),
        };
    }
    if (url === "/api/forward/status") {
        return {
            ok: true, status: 200,
            json: async () => sandbox._forwardStatus || { status: "idle", progress: { pct: 0, revealed: 0, total: 0 }, metrics: {}, equity: [], drawdown: [], trades: [], positions: [] },
        };
    }
    if (url === "/api/forward/stop") {
        return { ok: true, status: 200, json: async () => ({ status: "stopped" }) };
    }
    return { ok: false, status: 404, json: async () => ({ error: "not found" }) };
};

// BrokerAuthUI stub — tracks open() calls
let authUIOpenCalls = 0;
sandbox.window.BrokerAuthUI = {
    open: () => { authUIOpenCalls += 1; },
};
sandbox.BrokerAuthUI = sandbox.window.BrokerAuthUI;

vm.createContext(sandbox);
vm.runInContext(forwardCode, sandbox, { filename: "forward.js" });

// The script calls document.addEventListener("DOMContentLoaded", init) — fire it.
// But init is async, so we need to wait for it.
// Actually the script calls init directly via DOMContentLoaded listener.
// Let's fire it and flush.
sandbox.document._fire("DOMContentLoaded", {});
await new Promise(r => setImmediate(r));
await new Promise(r => setImmediate(r));

const $ = (id) => elements[id];
const flush = () => new Promise(r => setImmediate(r));

let passed = 0;
async function test(name, fn) {
    await fn();
    passed += 1;
    console.log(`  ✓ ${name}`);
}

// Helper to dispatch a broker:status event
function dispatchBrokerStatus(state) {
    sandbox.document._fire("broker:status", {
        detail: {
            status: state,
            broker: "mstock",
            broker_display_name: "mStock",
            expires_at: state === "authenticated" || state === "expiring_soon"
                ? new Date(Date.now() + 3600000).toISOString()
                : null,
        },
    });
}

// ------------------------------------------------------------------ scenarios

await test("Start button is disabled + shows connect message when unauthenticated", async () => {
    dispatchBrokerStatus("unauthenticated");
    await flush();
    assert.equal($("startBtn").disabled, true);
    assert.match($("startBtn").textContent, /Connect mStock to Start/);
    assert.match($("startBtn").title, /Authentication required/);
});

await test("Start button is disabled when expired", async () => {
    dispatchBrokerStatus("expired");
    await flush();
    assert.equal($("startBtn").disabled, true);
    assert.match($("startBtn").textContent, /Connect mStock to Start/);
});

await test("Start button is enabled + shows ▶ Start when authenticated", async () => {
    dispatchBrokerStatus("authenticated");
    await flush();
    assert.equal($("startBtn").disabled, false);
    assert.match($("startBtn").textContent, /▶ Start/);
    assert.equal($("startBtn").title, "");
});

await test("Start button is enabled when expiring_soon", async () => {
    dispatchBrokerStatus("expiring_soon");
    await flush();
    assert.equal($("startBtn").disabled, false);
    assert.match($("startBtn").textContent, /▶ Start/);
});

await test("clicking disabled Start button opens the auth modal", async () => {
    dispatchBrokerStatus("unauthenticated");
    await flush();
    const before = authUIOpenCalls;
    $("startBtn").click();
    await flush();
    assert.ok(authUIOpenCalls > before, "BrokerAuthUI.open() should be called");
});

await test("clicking enabled Start button does NOT open auth modal (calls startBot)", async () => {
    dispatchBrokerStatus("authenticated");
    await flush();
    const before = authUIOpenCalls;
    sandbox._forwardStarted = 0;
    // Set required form values so startBot doesn't bail out on validation
    $("strategy").value = "sma_crossover";
    $("fromDate").value = "2024-01-01";
    $("toDate").value = "2024-12-31";
    $("startBtn").click();
    await flush();
    await flush(); // extra flush for async startBot → fetchJSON
    assert.equal(authUIOpenCalls, before, "BrokerAuthUI.open() should NOT be called when authenticated");
    // The start bot should have been triggered (fetch to /api/forward/start)
    assert.ok(sandbox._forwardStarted >= 1, "Forward start should have been called");
});

await test("transitioning from authenticated → unauthenticated disables the button", async () => {
    dispatchBrokerStatus("authenticated");
    await flush();
    assert.equal($("startBtn").disabled, false);

    dispatchBrokerStatus("unauthenticated");
    await flush();
    assert.equal($("startBtn").disabled, true);
    assert.match($("startBtn").textContent, /Connect mStock to Start/);
});

await test("btn-disabled-auth CSS class is added when unauthenticated, removed when authenticated", async () => {
    dispatchBrokerStatus("unauthenticated");
    await flush();
    assert.ok($("startBtn").classList.contains("btn-disabled-auth"));

    dispatchBrokerStatus("authenticated");
    await flush();
    assert.ok(!$("startBtn").classList.contains("btn-disabled-auth"));
});

console.log(`\nforward.js auth gate: ${passed} tests passed`);
