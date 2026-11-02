/**
 * Sprint A4 — the market-hours login prompt in broker_status.js.
 *
 * Same harness shape as test_broker_status.mjs (real script, stub DOM + fetch,
 * driven through its public API), plus the two things A4 adds: a
 * `#broker-login-banner` element and a `market:state` listener, so the tests can
 * move the NSE session state and fire the 09:00 edge without a new poll.
 *
 * The behaviour under test, in one line each:
 *   - nothing auto-opens and no banner renders outside PRE_OPEN/OPEN
 *   - expired / T−10m / unauthenticated-with-stuck-runners each get a banner
 *   - the popup auto-opens once per stage, and dismissing it changes nothing
 *   - only a verified session clears the banner
 *
 * Usage: node tests/js/test_broker_login_prompt.mjs
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
        hidden: false,
        dataset: {},
        attrs: {},
        children: [],
        handlers: {},
        setAttribute(key, value) { this.attrs[key] = value; },
        getAttribute(key) { return this.attrs[key]; },
        addEventListener(type, fn) {
            (this.handlers[type] = this.handlers[type] || []).push(fn);
        },
        appendChild(child) { this.children.push(child); },
        remove() {
            const stack = elements["toast-stack"];
            const idx = stack ? stack.children.indexOf(this) : -1;
            if (idx >= 0) stack.children.splice(idx, 1);
        },
        click() { (this.handlers.click || []).forEach((fn) => fn()); },
    };
}

const elements = {};
for (const id of [
    "broker-status", "broker-status-dot", "broker-status-name", "broker-strip",
    "broker-login-banner", "toast-stack",
]) {
    elements[id] = makeElement(id);
}

const docHandlers = {};
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
        hidden: false,
        getElementById: (id) => elements[id] || null,
        createElement: (tag) => makeElement(`<${tag}>`),
        dispatchEvent: () => {},
        addEventListener: (type, fn) => {
            (docHandlers[type] = docHandlers[type] || []).push(fn);
        },
    },
    window: { __lastMarketState: { session_state: "OPEN", is_open: true } },
};
sandbox.globalThis = sandbox;

const queue = [];
let lastPayload = null;
sandbox.fetch = (url) => {
    assert.equal(url, "/api/broker/status");
    const payload = queue.length ? queue.shift() : lastPayload;
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(payload) });
};

vm.createContext(sandbox);
vm.runInContext(code, sandbox, { filename: "broker_status.js" });
const BrokerStatus = vm.runInContext("BrokerStatus", sandbox);

// ------------------------------------------------------------------- test plumbing

const banner = () => elements["broker-login-banner"];
const opened = [];
sandbox.window.BrokerAuthUI = {
    // Copy into a host-realm object: the opts literal is created inside the vm
    // context, and assert.deepEqual is strict about prototypes across realms.
    open: (opts) => { opened.push(opts ? { broker: String(opts.broker) } : null); },
    close: () => {},
};

const future = (minutes) => new Date(Date.now() + minutes * 60000).toISOString();

function session(name, over = {}) {
    return Object.assign(
        {
            broker: name,
            broker_display_name: name === "mstock" ? "mStock" : name,
            status: "authenticated",
            authenticated: true,
            expires_at: future(240),
            waiting_runners: 0,
        },
        over,
    );
}

function payload(sessions, over = {}) {
    const rows = {};
    for (const [name, row] of Object.entries(sessions)) rows[name] = row;
    const first = Object.values(rows)[0] || {};
    return Object.assign(
        {
            status: first.status || "unauthenticated",
            broker: Object.keys(rows)[0] || "mstock",
            broker_display_name: first.broker_display_name || "Broker",
            expires_at: first.expires_at || null,
            sessions: rows,
            expiry_events: [],
        },
        over,
    );
}

function enqueue(p) { queue.push(p); lastPayload = p; }
function setMarket(sessionState) {
    sandbox.window.__lastMarketState = { session_state: sessionState };
}
function fireMarketState() {
    (docHandlers["market:state"] || []).forEach((fn) => fn());
}

let passed = 0;
async function test(name, fn) {
    await fn();
    passed += 1;
    console.log(`  ✓ ${name}`);
}

// -------------------------------------------------------------------- scenarios

await test("nothing prompts outside the active market window", async () => {
    setMarket("POST_CLOSE");
    opened.length = 0;
    enqueue(payload({ mstock: session("mstock", { status: "expired", authenticated: false, expires_at: null }) }));
    await BrokerStatus.refresh();
    await BrokerStatus.refresh();

    assert.equal(banner().hidden, true);
    assert.equal(banner().textContent, "");
    assert.equal(opened.length, 0);
});

await test("an expired session mid-market raises the red CTA and one popup", async () => {
    setMarket("OPEN");
    opened.length = 0;
    enqueue(payload({ mstock: session("mstock", { status: "expired", authenticated: false, expires_at: null }) }));
    await BrokerStatus.refresh();

    assert.equal(banner().hidden, false);
    assert.equal(banner().textContent, "🔴 Broker login needed");
    assert.equal(banner().dataset.stage, "expired");
    assert.equal(banner().dataset.broker, "mstock");
    assert.match(banner().title, /not valid/);
    assert.deepEqual(opened, [{ broker: "mstock" }]);

    // A 30-second poll must not turn into a popup every 30 seconds.
    await BrokerStatus.refresh();
    await BrokerStatus.refresh();
    assert.deepEqual(opened, [{ broker: "mstock" }]);
    assert.equal(banner().hidden, false);
});

await test("dismissing the popup leaves the banner up", async () => {
    sandbox.window.BrokerAuthUI.close();
    assert.equal(banner().hidden, false);
    assert.equal(banner().textContent, "🔴 Broker login needed");
});

await test("a verified session is the only thing that clears the banner", async () => {
    enqueue(payload({ mstock: session("mstock") }));
    await BrokerStatus.refresh();

    assert.equal(banner().hidden, true);
    assert.equal(banner().textContent, "");
    assert.equal(banner().dataset.stage, undefined);
});

await test("inside T−10m the CTA turns amber and prompts once", async () => {
    opened.length = 0;
    enqueue(payload({
        mstock: session("mstock", { status: "expiring_soon", expires_at: future(7) }),
    }));
    await BrokerStatus.refresh();

    assert.equal(banner().hidden, false);
    assert.equal(banner().textContent, "🟡 Re-login needed (<10m)");
    assert.equal(banner().dataset.stage, "urgent");
    assert.deepEqual(opened, [{ broker: "mstock" }]);

    await BrokerStatus.refresh();
    assert.deepEqual(opened, [{ broker: "mstock" }]);
});

await test("twenty-five minutes out is a toast, not a banner", async () => {
    opened.length = 0;
    enqueue(payload({
        mstock: session("mstock", { status: "expiring_soon", expires_at: future(25) }),
    }));
    await BrokerStatus.refresh();

    assert.equal(banner().hidden, true);
    assert.equal(opened.length, 0);
    // The Task 3.3 warning toast still fires — A4 adds a rung, it does not move one.
    assert.equal(elements["toast-stack"].children.length >= 1, true);
    elements["toast-stack"].children.length = 0;
});

await test("never-logged-in only prompts while a runner is stuck on it", async () => {
    opened.length = 0;
    enqueue(payload({
        mstock: session("mstock", {
            status: "unauthenticated", authenticated: false, expires_at: null, waiting_runners: 0,
        }),
    }));
    await BrokerStatus.refresh();
    assert.equal(banner().hidden, true);
    assert.equal(opened.length, 0);

    enqueue(payload({
        mstock: session("mstock", {
            status: "unauthenticated", authenticated: false, expires_at: null, waiting_runners: 4,
        }),
    }));
    await BrokerStatus.refresh();
    assert.equal(banner().hidden, false);
    assert.equal(banner().dataset.stage, "unauthenticated");
    assert.match(banner().title, /4 runner\(s\) are pricing off a frozen mark/);
    assert.deepEqual(opened, [{ broker: "mstock" }]);
});

await test("the worst broker owns the single banner slot", async () => {
    opened.length = 0;
    enqueue(payload({
        dhan: session("dhan", {
            status: "unauthenticated", authenticated: false, expires_at: null, waiting_runners: 2,
        }),
        mstock: session("mstock", { status: "expired", authenticated: false, expires_at: null }),
    }));
    await BrokerStatus.refresh();

    assert.equal(banner().dataset.broker, "mstock");
    assert.equal(banner().dataset.stage, "expired");
    assert.deepEqual(opened, [{ broker: "mstock" }]);
});

await test("an escalation prompts again — a dead session is new information", async () => {
    opened.length = 0;
    enqueue(payload({
        mstock: session("mstock", {
            status: "expiring_soon", expires_at: future(6), waiting_runners: 0,
        }),
    }));
    await BrokerStatus.refresh();
    assert.deepEqual(opened, [{ broker: "mstock" }]);

    enqueue(payload({ mstock: session("mstock", { status: "expired", authenticated: false, expires_at: null }) }));
    await BrokerStatus.refresh();
    assert.deepEqual(opened, [{ broker: "mstock" }, { broker: "mstock" }]);
    assert.equal(banner().dataset.stage, "expired");
});

await test("the 09:00 PRE_OPEN edge prompts without waiting for a poll", async () => {
    // Parked overnight: the session died earlier while the market was open (so
    // it already prompted once), then the window shut.
    setMarket("POST_CLOSE");
    opened.length = 0;
    enqueue(payload({ mstock: session("mstock", { status: "expired", authenticated: false, expires_at: null }) }));
    await BrokerStatus.refresh();
    assert.equal(banner().hidden, true);
    assert.equal(opened.length, 0);

    // The chip reports the new session state; no /api/broker/status call happens.
    // A tab left open overnight must re-prompt here — the closed window released
    // the once-per-stage latch.
    setMarket("PRE_OPEN");
    fireMarketState();

    assert.equal(banner().hidden, false);
    assert.equal(banner().textContent, "🔴 Broker login needed");
    assert.deepEqual(opened, [{ broker: "mstock" }]);
    setMarket("OPEN");
});

await test("clicking the banner opens that broker's login flow", async () => {
    opened.length = 0;
    banner().click();
    assert.deepEqual(opened, [{ broker: "mstock" }]);
});

await test("the manual pill click still works outside market hours", async () => {
    setMarket("POST_CLOSE");
    opened.length = 0;
    elements["broker-status"].click();
    assert.equal(opened.length, 1);
    setMarket("OPEN");
});

await test("a failed poll keeps the banner rather than flickering it away", async () => {
    enqueue(payload({ mstock: session("mstock", { status: "expired", authenticated: false, expires_at: null }) }));
    await BrokerStatus.refresh();
    assert.equal(banner().hidden, false);

    const good = sandbox.fetch;
    sandbox.fetch = () => Promise.reject(new Error("network down"));
    await BrokerStatus.refresh();
    sandbox.fetch = good;

    assert.equal(banner().hidden, false);
    assert.equal(banner().textContent, "🔴 Broker login needed");
});

console.log(`\nbroker_status.js (A4 login prompt): ${passed} tests passed`);
