/**
 * Broker auth epic Tasks 3.1 + 3.3 — browser-logic tests for broker_status.js.
 *
 * Runs under plain Node (no JS test framework in this repo): evaluates the
 * real script in a sandbox with a stub DOM + fetch, drives it through its
 * public API, and asserts the nav indicator / toast behaviour. Launched from
 * tests/test_broker_ui.py (skipped when node is unavailable).
 *
 * Usage: node tests/js/test_broker_status.mjs
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import vm from "node:vm";
import assert from "node:assert/strict";

const root = fileURLToPath(new URL("../../", import.meta.url));
const code = readFileSync(
    path.join(root, "src/backtest/web/static/js/broker_status.js"), "utf8",
);

// ---------------------------------------------------------------- synthetic DOM

function makeElement(id) {
    return {
        id,
        textContent: "",
        title: "",
        className: "",
        dataset: {},
        attrs: {},
        children: [],
        handlers: {},
        removed: false,
        setAttribute(key, value) { this.attrs[key] = value; },
        getAttribute(key) { return this.attrs[key]; },
        addEventListener(type, fn) {
            (this.handlers[type] = this.handlers[type] || []).push(fn);
        },
        appendChild(child) { this.children.push(child); },
        remove() {
            this.removed = true;
            const stack = elements["toast-stack"];
            const idx = stack ? stack.children.indexOf(this) : -1;
            if (idx >= 0) stack.children.splice(idx, 1);
        },
        click() { (this.handlers.click || []).forEach((fn) => fn()); },
    };
}

const elements = {};
for (const id of ["broker-status", "broker-status-dot", "broker-status-name", "toast-stack"]) {
    elements[id] = makeElement(id);
}

const dispatched = [];
const timers = [];

const sandbox = {
    console: { warn: () => {} },
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length - 1; },
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
        createElement: (tag) => makeElement(`<${tag}>`),
        dispatchEvent: (event) => { dispatched.push(event); },
    },
    window: {},
};
sandbox.globalThis = sandbox;

// fetch stub: serves a queue of payloads; last payload repeats once queue empties.
const queue = [];
let fetchCalls = 0;
sandbox.fetch = (url) => {
    fetchCalls += 1;
    assert.equal(url, "/api/broker/status");
    const payload = queue.length ? queue.shift() : lastPayload;
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(payload) });
};
let lastPayload = null;

vm.createContext(sandbox);
vm.runInContext(code, sandbox, { filename: "broker_status.js" });
// Top-level `const` stays lexically scoped inside the context — pull the
// binding out explicitly.
const BrokerStatus = vm.runInContext("BrokerStatus", sandbox);

const dot = () => elements["broker-status-dot"];
const nameEl = () => elements["broker-status-name"];
const btn = () => elements["broker-status"];
const toasts = () => elements["toast-stack"].children;
const flush = () => new Promise((resolve) => setImmediate(resolve));

function enqueue(...payloads) {
    for (const p of payloads) { queue.push(p); lastPayload = p; }
}

let passed = 0;
async function test(name, fn) {
    await fn();
    passed += 1;
    console.log(`  ✓ ${name}`);
}

// ------------------------------------------------------------------ scenarios

const future = (minutes) => new Date(Date.now() + minutes * 60000).toISOString();

await test("initial unauthenticated state renders red dot + broker name", async () => {
    enqueue({
        status: "unauthenticated", broker: "mstock",
        broker_display_name: "mStock", expires_at: null,
    });
    await BrokerStatus.refresh();
    assert.equal(dot().textContent, "🔴");
    assert.equal(dot().dataset.state, "unauthenticated");
    assert.equal(nameEl().textContent, "mStock");
    assert.match(btn().title, /not connected — click to log in/);
    assert.equal(BrokerStatus.state(), "unauthenticated");
});

await test("authenticated renders green dot with expiry time in tooltip", async () => {
    enqueue({
        status: "authenticated", broker: "mstock",
        broker_display_name: "mStock", expires_at: future(120),
    });
    await BrokerStatus.refresh();
    assert.equal(dot().textContent, "🟢");
    assert.match(btn().title, /connected \(expires/);
    assert.equal(toasts().length, 0);
});

await test("entering expiring_soon fires the warning toast exactly once (3.3)", async () => {
    enqueue({
        status: "expiring_soon", broker: "mstock",
        broker_display_name: "mStock", expires_at: future(25),
    });
    await BrokerStatus.refresh();
    assert.equal(dot().textContent, "🟡");
    assert.equal(toasts().length, 1);
    assert.match(toasts()[0].textContent, /session expiring in ~25 minutes/);
    assert.match(toasts()[0].textContent, /Click here to re-authenticate/);
    assert.equal(toasts()[0].className, "toast warning clickable");

    // Repeated polls in the same state must NOT re-fire the toast.
    await BrokerStatus.refresh();
    await BrokerStatus.refresh();
    assert.equal(toasts().length, 1);
});

await test("toast click opens the auth popup and dismisses", async () => {
    let opened = 0;
    sandbox.window.BrokerAuthUI = { open: () => { opened += 1; } };
    toasts()[0].click();
    assert.equal(opened, 1);
    assert.equal(toasts().length, 0);
    delete sandbox.window.BrokerAuthUI;
});

await test("re-auth resets the cycle — a second expiring window toasts again", async () => {
    enqueue({ status: "authenticated", broker: "mstock", broker_display_name: "mStock", expires_at: future(120) });
    await BrokerStatus.refresh();
    assert.equal(toasts().length, 0);

    enqueue({ status: "expiring_soon", broker: "mstock", broker_display_name: "mStock", expires_at: future(20) });
    await BrokerStatus.refresh();
    assert.equal(toasts().length, 1);
    assert.match(toasts()[0].textContent, /~20 minutes/);
    toasts()[0].remove(); // clean slate for the next test
});

await test("session loss after the window fires the expired toast + red dot", async () => {
    enqueue({ status: "unauthenticated", broker: "mstock", broker_display_name: "mStock", expires_at: null });
    await BrokerStatus.refresh();
    assert.equal(dot().textContent, "🔴");
    assert.equal(toasts().length, 1);
    assert.match(toasts()[0].textContent, /session expired — please re-authenticate/);
    assert.equal(toasts()[0].className, "toast error clickable");
    toasts()[0].remove();
});

await test("expectLogout() suppresses the expired toast for user-initiated logout", async () => {
    enqueue({ status: "authenticated", broker: "mstock", broker_display_name: "mStock", expires_at: future(120) });
    await BrokerStatus.refresh();

    BrokerStatus.expectLogout();
    enqueue({ status: "unauthenticated", broker: "mstock", broker_display_name: "mStock", expires_at: null });
    await BrokerStatus.refresh();
    assert.equal(toasts().length, 0); // intentional logout — no alarm
});

await test("fetch failure keeps the last known state (no flicker, no toast)", async () => {
    const originalFetch = sandbox.fetch;
    sandbox.fetch = () => Promise.reject(new Error("network down"));
    const result = await BrokerStatus.refresh();
    sandbox.fetch = originalFetch;
    assert.equal(result, null);
    assert.equal(dot().textContent, "🔴"); // last applied state retained
    assert.equal(toasts().length, 0);
});

await test("nav pill click opens the auth popup via the registered hook", async () => {
    let opened = 0;
    sandbox.window.BrokerAuthUI = { open: () => { opened += 1; } };
    btn().click();
    delete sandbox.window.BrokerAuthUI;
    assert.equal(opened, 1);
});

await test("auto-start bound polling: script start() consumed the first payload", () => {
    // The script auto-starts when #broker-status exists (it did in this
    // sandbox), so at least the initial poll ran during load.
    assert.ok(fetchCalls >= 1);
});

await test("toast auto-dismisses after 10 seconds (captured timer)", async () => {
    enqueue({ status: "authenticated", broker: "mstock", broker_display_name: "mStock", expires_at: future(120) });
    await BrokerStatus.refresh();
    enqueue({ status: "expiring_soon", broker: "mstock", broker_display_name: "mStock", expires_at: future(15) });
    await BrokerStatus.refresh();
    assert.equal(toasts().length, 1);

    const toastTimer = timers[timers.length - 1];
    assert.equal(toastTimer.ms, 10000);
    toastTimer.fn(); // simulate the 10 s elapse
    assert.equal(toasts().length, 0);
});

await test("every applied poll dispatches a broker:status document event", async () => {
    const before = dispatched.length;
    enqueue({ status: "authenticated", broker: "mstock", broker_display_name: "mStock", expires_at: future(90) });
    await BrokerStatus.refresh();
    assert.equal(dispatched.length, before + 1);
    assert.equal(dispatched[dispatched.length - 1].type, "broker:status");
    assert.equal(dispatched[dispatched.length - 1].detail.status, "authenticated");
});

console.log(`\nbroker_status.js: ${passed} tests passed`);
