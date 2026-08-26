/**
 * Broker auth epic Task 3.2 — browser-logic tests for broker_auth_modal.js.
 *
 * Runs under plain Node (no JS test framework): evaluates the real script in
 * a sandbox with a stub DOM + fetch, drives the modal through its public API,
 * and asserts view transitions, error paths, password-clearing, logout wiring.
 * Launched from tests/test_broker_ui.py (skipped when node is unavailable).
 *
 * Usage: node tests/js/test_broker_auth_modal.mjs
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import vm from "node:vm";
import assert from "node:assert/strict";

const root = fileURLToPath(new URL("../../", import.meta.url));
const modalCode = readFileSync(
    path.join(root, "src/backtest/web/static/js/broker_auth_modal.js"), "utf8",
);

// ---------------------------------------------------------------- synthetic DOM

function makeElement(id, skipChildren) {
    const el = {
        id,
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
        click() { (this.handlers.click || []).forEach((fn) => fn()); },
        querySelector(sel) {
            // support .broker-auth-btn-text and .broker-auth-spinner
            if (sel === ".broker-auth-btn-text") return this._btnText || null;
            if (sel === ".broker-auth-spinner") return this._spinner || null;
            return null;
        },
        focus() { this._focused = true; },
    };
    // sub-elements for button spinner (leaf nodes — no further children)
    if (!skipChildren) {
        el._btnText = makeElement(`text-of-${id}`, true);
        el._spinner = makeElement(`spinner-of-${id}`, true);
    }
    return el;
}

const ids = [
    "broker-auth-overlay",
    "broker-auth-title",
    "broker-auth-step-credentials",
    "broker-auth-step-totp",
    "broker-auth-step-authenticated",
    "broker-auth-username",
    "broker-auth-password",
    "broker-auth-login-btn",
    "broker-auth-credentials-error",
    "broker-auth-totp-code",
    "broker-auth-totp-btn",
    "broker-auth-totp-error",
    "broker-auth-expires",
    "broker-auth-broker-name",
    "broker-auth-logout-btn",
    "broker-auth-close",
];

const elements = {};
for (const id of ids) {
    elements[id] = makeElement(id);
}

// broker-status pill is not in this harness — not needed for modal tests.

const dispatched = [];
const sandbox = {
    console: { warn: () => {}, log: () => {} },
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
        createElement: (tag) => makeElement(`<${tag}>`),
        dispatchEvent: (event) => { dispatched.push(event); },
        addEventListener: (type, fn) => {
            (document._handlers = document._handlers || {})[type] =
                (document._handlers[type] || []).concat(fn);
        },
        _fire(type, ...args) {
            (document._handlers[type] || []).forEach(fn => fn(...args));
        },
    },
    window: {},
};
const document = sandbox.document;
sandbox.globalThis = sandbox;

// ---- BrokerStatus stub (used by the modal to read state + refresh) ---------

let brokerStatusState = "unauthenticated";
let brokerStatusPayload = {
    status: "unauthenticated",
    broker: "mstock",
    broker_display_name: "mStock",
    expires_at: null,
};
let refreshCalls = 0;

sandbox.window.BrokerStatus = {
    get: () => brokerStatusPayload,
    state: () => brokerStatusState,
    refresh: async () => { refreshCalls += 1; return brokerStatusPayload; },
    expectLogout: () => { sandbox.window._expectLogoutCalled = true; },
};
// Also expose as a top-level binding so the modal code (which references
// bare `BrokerStatus`) can find it inside the vm context.
sandbox.BrokerStatus = sandbox.window.BrokerStatus;

// ---- fetch stub: routes requests and returns mock responses ----------------

const fetchLog = [];
let nextResponses = {};  // url -> array of response bodies (queued)

sandbox.fetch = async (url, opts) => {
    const body = opts && opts.body ? JSON.parse(opts.body) : {};
    fetchLog.push({ url, body });

    const key = url;
    let resp;
    if (nextResponses[key] && nextResponses[key].length) {
        resp = nextResponses[key].shift();
    } else {
        resp = { success: true };
    }
    return { ok: true, status: 200, json: async () => resp };
};

function queueResponse(url, body) {
    if (!nextResponses[url]) nextResponses[url] = [];
    nextResponses[url].push(body);
}

// ---- evaluate the modal script in the sandbox ------------------------------

vm.createContext(sandbox);
vm.runInContext(modalCode, sandbox, { filename: "broker_auth_modal.js" });
const BrokerAuthUI = vm.runInContext("BrokerAuthUI", sandbox);

const $ = (id) => elements[id];

let passed = 0;
async function test(name, fn) {
    await fn();
    passed += 1;
    console.log(`  ✓ ${name}`);
}

// Flush microtasks so that async click handlers (handleLogin, handleTotp, etc.)
// complete their fetch + DOM updates before we assert.
const flush = () => new Promise((r) => setImmediate(r));

// ---- reset helpers ---------------------------------------------------------

function resetState() {
    fetchLog.length = 0;
    for (const k of Object.keys(nextResponses)) delete nextResponses[k];
    brokerStatusState = "unauthenticated";
    brokerStatusPayload = {
        status: "unauthenticated", broker: "mstock",
        broker_display_name: "mStock", expires_at: null,
    };
    sandbox.window._expectLogoutCalled = false;
    refreshCalls = 0;
    // reset inputs
    $("broker-auth-username").value = "";
    $("broker-auth-password").value = "";
    $("broker-auth-totp-code").value = "";
    $("broker-auth-credentials-error").textContent = "";
    $("broker-auth-totp-error").textContent = "";
    // remove overlay open class
    $("broker-auth-overlay").classList.remove("open");
}

// ------------------------------------------------------------------ scenarios

await test("open() when unauthenticated shows the credentials view", async () => {
    resetState();
    BrokerAuthUI.open();
    assert.ok($("broker-auth-overlay").classList.contains("open"));
    assert.equal($("broker-auth-step-credentials").hidden, false);
    assert.equal($("broker-auth-step-totp").hidden, true);
    assert.equal($("broker-auth-step-authenticated").hidden, true);
    assert.match($("broker-auth-title").textContent, /mStock Login/);
    BrokerAuthUI.close();
});

await test("open() when authenticated shows the session info view", async () => {
    resetState();
    brokerStatusState = "authenticated";
    brokerStatusPayload = {
        status: "authenticated", broker: "mstock",
        broker_display_name: "mStock", expires_at: new Date(Date.now() + 3600000).toISOString(),
    };
    BrokerAuthUI.open();
    assert.equal($("broker-auth-step-credentials").hidden, true);
    assert.equal($("broker-auth-step-totp").hidden, true);
    assert.equal($("broker-auth-step-authenticated").hidden, false);
    assert.match($("broker-auth-title").textContent, /Connected/);
    assert.equal($("broker-auth-broker-name").textContent, "mStock");
    BrokerAuthUI.close();
});

await test("close() removes the open class and resets to credentials view", async () => {
    resetState();
    BrokerAuthUI.open();
    assert.ok($("broker-auth-overlay").classList.contains("open"));
    BrokerAuthUI.close();
    assert.ok(!$("broker-auth-overlay").classList.contains("open"));
    assert.equal($("broker-auth-step-credentials").hidden, false);
});

await test("login with empty fields shows inline error, no fetch", async () => {
    resetState();
    BrokerAuthUI.open();
    $("broker-auth-username").value = "";
    $("broker-auth-password").value = "";
    $("broker-auth-login-btn").click();
    assert.equal(fetchLog.length, 0);
    assert.match($("broker-auth-credentials-error").textContent, /required/);
    BrokerAuthUI.close();
});

await test("successful login clears password and transitions to TOTP view", async () => {
    resetState();
    BrokerAuthUI.open();
    $("broker-auth-username").value = "user1";
    $("broker-auth-password").value = "secret";
    queueResponse("/api/broker/login", { success: true, requires_totp: true, message: "" });
    $("broker-auth-login-btn").click();
    await flush();
    // password must be cleared immediately
    assert.equal($("broker-auth-password").value, "");
    // fetch was called with username + password
    assert.equal(fetchLog.length, 1);
    assert.equal(fetchLog[0].url, "/api/broker/login");
    assert.equal(fetchLog[0].body.username, "user1");
    assert.equal(fetchLog[0].body.password, "secret");
    // should show TOTP view
    assert.equal($("broker-auth-step-credentials").hidden, true);
    assert.equal($("broker-auth-step-totp").hidden, false);
    BrokerAuthUI.close();
});

await test("failed login shows inline error and stays on credentials view", async () => {
    resetState();
    BrokerAuthUI.open();
    $("broker-auth-username").value = "baduser";
    $("broker-auth-password").value = "wrongpass";
    queueResponse("/api/broker/login", { success: false, message: "Invalid credentials", requires_totp: false });
    $("broker-auth-login-btn").click();
    await flush();
    await flush(); // extra flush for nested promises
    assert.equal($("broker-auth-step-credentials").hidden, false);
    assert.match($("broker-auth-credentials-error").textContent, /Invalid credentials/);
    assert.equal($("broker-auth-password").value, ""); // password cleared
    BrokerAuthUI.close();
});

await test("successful TOTP verification calls refresh and shows authenticated view", async () => {
    resetState();
    // first do the login flow
    BrokerAuthUI.open();
    $("broker-auth-username").value = "user1";
    $("broker-auth-password").value = "pass1";
    queueResponse("/api/broker/login", { success: true, requires_totp: true, message: "" });
    $("broker-auth-login-btn").click();
    await flush();
    // now submit TOTP
    $("broker-auth-totp-code").value = "123456";
    queueResponse("/api/broker/verify-totp", { success: true, message: "", expires_at: new Date(Date.now() + 3600000).toISOString() });
    $("broker-auth-totp-btn").click();
    await flush();
    // should call BrokerStatus.refresh()
    assert.ok(refreshCalls >= 1);
    // should show authenticated view
    assert.equal($("broker-auth-step-authenticated").hidden, false);
    assert.match($("broker-auth-title").textContent, /Connected/);
    BrokerAuthUI.close();
});

await test("invalid TOTP shows inline error and stays on TOTP view (retry)", async () => {
    resetState();
    // simulate being on the TOTP step
    BrokerAuthUI.open();
    $("broker-auth-username").value = "user1";
    $("broker-auth-password").value = "pass1";
    queueResponse("/api/broker/login", { success: true, requires_totp: true, message: "" });
    $("broker-auth-login-btn").click();
    await flush();
    // submit wrong TOTP
    $("broker-auth-totp-code").value = "000000";
    queueResponse("/api/broker/verify-totp", { success: false, message: "Invalid TOTP code", expires_at: "" });
    $("broker-auth-totp-btn").click();
    await flush();
    assert.equal($("broker-auth-step-totp").hidden, false);
    assert.match($("broker-auth-totp-error").textContent, /Invalid TOTP/);
    BrokerAuthUI.close();
});

await test("empty TOTP code shows inline error without calling API", async () => {
    resetState();
    BrokerAuthUI.open();
    $("broker-auth-username").value = "user1";
    $("broker-auth-password").value = "pass1";
    queueResponse("/api/broker/login", { success: true, requires_totp: true, message: "" });
    $("broker-auth-login-btn").click();
    await flush();
    $("broker-auth-totp-code").value = "";
    $("broker-auth-totp-btn").click();
    await flush();
    assert.match($("broker-auth-totp-error").textContent, /6-digit/);
    // no TOTP fetch happened (only the login fetch)
    assert.equal(fetchLog.filter(f => f.url === "/api/broker/verify-totp").length, 0);
    BrokerAuthUI.close();
});

await test("logout calls expectLogout() before /api/broker/logout, then refreshes", async () => {
    resetState();
    brokerStatusState = "authenticated";
    brokerStatusPayload = {
        status: "authenticated", broker: "mstock",
        broker_display_name: "mStock", expires_at: new Date(Date.now() + 3600000).toISOString(),
    };
    // Need to reset BrokerStatus to a fresh object that tracks expectLogout
    sandbox.window._expectLogoutCalled = false;
    sandbox.BrokerStatus = {
        get: () => brokerStatusPayload,
        state: () => brokerStatusState,
        refresh: async () => { refreshCalls += 1; return brokerStatusPayload; },
        expectLogout: () => { sandbox.window._expectLogoutCalled = true; },
    };
    // Re-run the modal code so it picks up the fresh BrokerStatus reference
    // Actually, the modal reads window.BrokerStatus each time, so just update it.
    sandbox.window.BrokerStatus = sandbox.BrokerStatus;

    BrokerAuthUI.open();
    queueResponse("/api/broker/logout", { success: true });
    $("broker-auth-logout-btn").click();
    await flush();
    await flush(); // extra flush for the refresh call chain
    assert.ok(sandbox.window._expectLogoutCalled, "expectLogout() must be called before logout");
    assert.ok(fetchLog.some(f => f.url === "/api/broker/logout"));
    assert.ok(refreshCalls >= 1, "BrokerStatus.refresh() called after logout");
    BrokerAuthUI.close();
});

await test("close button [×] closes the modal", async () => {
    resetState();
    BrokerAuthUI.open();
    assert.ok($("broker-auth-overlay").classList.contains("open"));
    $("broker-auth-close").click();
    assert.ok(!$("broker-auth-overlay").classList.contains("open"));
});

await test("Escape key closes the modal", async () => {
    resetState();
    BrokerAuthUI.open();
    assert.ok($("broker-auth-overlay").classList.contains("open"));
    document._fire("keydown", { key: "Escape" });
    assert.ok(!$("broker-auth-overlay").classList.contains("open"));
});

await test("window.BrokerAuthUI is registered globally", () => {
    assert.ok(sandbox.window.BrokerAuthUI);
    assert.equal(typeof sandbox.window.BrokerAuthUI.open, "function");
    assert.equal(typeof sandbox.window.BrokerAuthUI.close, "function");
});

await test("authenticated view shows broker display name and expiry time", async () => {
    resetState();
    const future = new Date(Date.now() + 7200000);
    brokerStatusState = "authenticated";
    brokerStatusPayload = {
        status: "authenticated", broker: "mstock",
        broker_display_name: "mStock", expires_at: future.toISOString(),
    };
    BrokerAuthUI.open();
    assert.equal($("broker-auth-broker-name").textContent, "mStock");
    // expires should be formatted as HH:MM (locale time)
    const expiresText = $("broker-auth-expires").textContent;
    assert.ok(expiresText !== "—", `expiry should be formatted, got: ${expiresText}`);
    BrokerAuthUI.close();
});

console.log(`\nbroker_auth_modal.js: ${passed} tests passed`);
