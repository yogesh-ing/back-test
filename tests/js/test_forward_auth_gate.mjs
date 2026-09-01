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
        /** Dispatch a real event so tests drive the same handlers the browser would. */
        _fire(type, extra) {
            const syntheticEvent = {
                preventDefault: () => {},
                stopPropagation: () => {},
                target: el,
                type,
                ...(extra || {}),
            };
            (this.handlers[type] || []).forEach((fn) => fn(syntheticEvent));
        },
        click() { this._fire("click"); },
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
    "params-container", "livePanel", "runMode", "dataSource", "prefillBanner",
    "symbolList", "taxonomyHint", "resumeBanner", "resumedSessionId",
    "resumeBtn", "freshStartBtn",
    // Live panel widgets touched by renderLive() — without them a successful
    // start throws inside poll() and the error toast hides the one under test.
    "liveStatusBanner", "marketDot", "marketLabel", "lastBarInfo", "barsProcessed",
    "tradesCount", "equityDisplay", "unrealizedPnl", "positionsBody",
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
sandbox.showToast = (msg, cls) => {
    sandbox._lastToast = { msg, cls };
    (sandbox._toasts = sandbox._toasts || []).push({ msg, cls });
};
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
// forward.js uses the shared Money helper in the live banner/positions; the
// real page loads it from a separate script, so the harness provides it too.
sandbox.Money = {
    format: (v) => `$${Number(v).toFixed(2)}`,
    signed: (v) => (v >= 0 ? "+" : "") + Number(v).toFixed(2),
};

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
        sandbox._startBody = opts && opts.body ? JSON.parse(opts.body) : null;
        return {
            ok: true, status: 200,
            json: async () => sandbox._startResponse
                || { status: "running", total: 200, revealed: 20 },
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

// Ticket #10: the page now has two taxonomy controls — runMode (paper|live)
// and dataSource (synthetic|replay|mstock). Paper is the default and needs no
// auth; live requires the broker session.

await test("paper mode (default) starts without auth — button enabled", async () => {
    dispatchBrokerStatus("unauthenticated");
    await flush();
    assert.equal($("runMode").value, "", "harness leaves controls unset");
    // runSelection() defaults to paper when the control is absent, so the
    // safe default changed from the old implicit-live to honest paper.
    assert.equal($("startBtn").disabled, false, "paper replay needs no broker session");
    assert.match($("startBtn").textContent, /▶ Start/);
});

await test("live mode + unauthenticated disables + shows connect message", async () => {
    dispatchBrokerStatus("unauthenticated");
    await flush();
    $("runMode").value = "live";
    $("dataSource").value = "mstock";
    $("runMode")._fire("change");
    await flush();
    assert.equal($("startBtn").disabled, true);
    assert.match($("startBtn").textContent, /Connect mStock to Start/);
    assert.match($("startBtn").title, /Authentication required/);
    // Risk hint goes loud for the live bucket (T9-aware).
    assert.match($("taxonomyHint").textContent, /LIVE/);
    $("runMode").value = "";
    $("dataSource").value = "";
    $("runMode")._fire("change");
    await flush();
});

await test("live mode + expired disables + shows connect message", async () => {
    dispatchBrokerStatus("expired");
    await flush();
    $("runMode").value = "live";
    $("dataSource").value = "mstock";
    $("runMode")._fire("change");
    await flush();
    assert.equal($("startBtn").disabled, true);
    assert.match($("startBtn").textContent, /Connect mStock to Start/);
    $("runMode").value = "";
    $("dataSource").value = "";
    $("runMode")._fire("change");
    await flush();
});

await test("Start button is enabled + shows ▶ Start when authenticated", async () => {
    dispatchBrokerStatus("authenticated");
    await flush();
    $("runMode").value = "live";
    $("dataSource").value = "mstock";
    $("runMode")._fire("change");
    await flush();
    assert.equal($("startBtn").disabled, false);
    assert.match($("startBtn").textContent, /▶ Start/);
    assert.equal($("startBtn").title, "");
    $("runMode").value = "";
    $("dataSource").value = "";
    $("runMode")._fire("change");
    await flush();
});

await test("clicking disabled Start button opens the auth modal", async () => {
    dispatchBrokerStatus("unauthenticated");
    await flush();
    $("runMode").value = "live";
    $("dataSource").value = "mstock";
    $("runMode")._fire("change");
    await flush();
    const before = authUIOpenCalls;
    $("startBtn").click();
    await flush();
    assert.ok(authUIOpenCalls > before, "BrokerAuthUI.open() should be called");
    $("runMode").value = "";
    $("dataSource").value = "";
    $("runMode")._fire("change");
    await flush();
});

await test("clicking enabled Start button does NOT open auth modal (calls startBot)", async () => {
    dispatchBrokerStatus("authenticated");
    await flush();
    const before = authUIOpenCalls;
    sandbox._forwardStarted = 0;
    // Set every form value startBot validates (symbol became required with the
    // live-mode work — leaving it out here is what let this test go stale).
    $("strategy").value = "sma_crossover";
    $("symbol").value = "reliance";
    $("timeframe").value = "1D";
    $("capital").value = "10000";
    $("fromDate").value = "2024-01-01";
    $("toDate").value = "2024-12-31";
    $("startBtn").click();
    await flush();
    await flush(); // extra flush for async startBot → fetchJSON
    assert.equal(authUIOpenCalls, before, "BrokerAuthUI.open() should NOT be called when authenticated");
    // The start bot should have been triggered (fetch to /api/forward/start)
    assert.ok(sandbox._forwardStarted >= 1, "Forward start should have been called");

    // …and the payload must carry the whole config, symbol upper-cased.
    const body = sandbox._startBody;
    assert.equal(body.strategy, "sma_crossover");
    assert.equal(body.symbol, "RELIANCE");
    assert.equal(body.timeframe, "1D");
    assert.equal(body.from_date, "2024-01-01");
    assert.equal(body.to_date, "2024-12-31");
    assert.equal(body.capital, 10000);
    // Taxonomy (ticket #10): the payload carries BOTH dimensions; when the
    // controls are absent the safe default is paper/synthetic — never the old
    // implicit "live" label that the replay could not honour.
    assert.equal(body.mode, "paper");
    assert.equal(body.source, "synthetic");
});

await test("missing symbol blocks the start and warns instead of posting", async () => {
    dispatchBrokerStatus("authenticated");
    await flush();
    sandbox._forwardStarted = 0;
    $("symbol").value = "";
    $("startBtn").click();
    await flush();
    await flush();
    assert.equal(sandbox._forwardStarted, 0, "startBot must not POST without a symbol");
    assert.match(sandbox._lastToast.msg, /symbol/i, "the user is told what is missing");
    sandbox._toasts = [];
    $("symbol").value = "reliance";
});

await test("paper + mstock starts without broker auth and sends mode/source", async () => {
    dispatchBrokerStatus("unauthenticated");
    await flush();
    $("runMode").value = "paper";
    $("dataSource").value = "mstock";
    $("runMode")._fire("change");
    $("dataSource")._fire("change");
    await flush();
    assert.equal($("startBtn").disabled, false, "paper replay needs no broker session");

    sandbox._forwardStarted = 0;
    $("startBtn").click();
    await flush();
    await flush();
    assert.ok(sandbox._forwardStarted >= 1, "paper mode should start immediately");
    assert.equal(sandbox._startBody.mode, "paper");
    assert.equal(sandbox._startBody.source, "mstock");
    $("runMode").value = "";
    $("dataSource").value = "";
    $("runMode")._fire("change");
    $("dataSource")._fire("change");
});

await test("server-defaulted date range is announced as a warning toast", async () => {
    dispatchBrokerStatus("authenticated");
    await flush();
    sandbox._forwardStarted = 0;
    sandbox._startResponse = {
        status: "running", total: 200, revealed: 20,
        defaults_applied: ["from_date", "to_date"],
        config: { from_date: "2020-01-01", to_date: "2026-08-28" },
    };
    $("startBtn").click();
    await flush();
    await flush();
    // Assert against the whole toast log: poll() may toast afterwards.
    const warned = (sandbox._toasts || []).filter(
        (t) => t.cls === "warning" && /2020-01-01 → 2026-08-28/.test(t.msg),
    );
    assert.equal(warned.length, 1, "a server-defaulted window must be announced once");
    assert.match(warned[0].msg, /from_date, to_date/);
    sandbox._startResponse = null;
});

await test("live: transitioning authenticated → unauthenticated disables the button", async () => {
    $("runMode").value = "live";
    $("dataSource").value = "mstock";
    $("runMode")._fire("change");
    dispatchBrokerStatus("authenticated");
    await flush();
    assert.equal($("startBtn").disabled, false);

    dispatchBrokerStatus("unauthenticated");
    await flush();
    assert.equal($("startBtn").disabled, true);
    assert.match($("startBtn").textContent, /Connect mStock to Start/);
    $("runMode").value = "";
    $("dataSource").value = "";
    $("runMode")._fire("change");
    await flush();
});

await test("live: btn-disabled-auth CSS added when unauthenticated, removed when authenticated", async () => {
    $("runMode").value = "live";
    $("dataSource").value = "mstock";
    $("runMode")._fire("change");
    dispatchBrokerStatus("unauthenticated");
    await flush();
    assert.ok($("startBtn").classList.contains("btn-disabled-auth"));

    dispatchBrokerStatus("authenticated");
    await flush();
    assert.ok(!$("startBtn").classList.contains("btn-disabled-auth"));
    $("runMode").value = "";
    $("dataSource").value = "";
    $("runMode")._fire("change");
    await flush();
});

// Ticket #10 — resume affordance (T7-aware): init re-attaches to a running
// session and shows the banner with its id; "Start fresh" clears it.
await test("resume banner appears for a running session; fresh start hides it", async () => {
    sandbox._forwardStatus = {
        status: "running", state_id: "deadbeefcafe",
        progress: { revealed: 3, total: 10, pct: 30 },
        metrics: {}, equity: { dates: [], values: [], benchmark: [] },
        drawdown: {}, trades: [], positions: [],
        config: { mode: "paper", source: "synthetic" },
    };
    await sandbox.document._fire("DOMContentLoaded", {});
    await flush();
    await flush();
    assert.equal($("resumeBanner").hidden, false);
    assert.equal($("resumedSessionId").textContent, "deadbeef");

    $("freshStartBtn").click();
    await flush();
    assert.equal($("resumeBanner").hidden, true);
    sandbox._forwardStatus = null;
});

console.log(`\nforward.js auth gate: ${passed} tests passed`);
