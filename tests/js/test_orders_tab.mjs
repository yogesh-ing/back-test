/**
 * Orders tab — behaviour tests (Live Order Management).
 *
 * The Orders tab is the answer to "did my order fill, and at what price?", so
 * the assertions here are about the three questions a live operator asks the
 * ledger and the three ways the tab could lie about them:
 *
 *   * Is it still working?   PENDING rows carry their age and the only cancel
 *                            button in the app — and nothing else does.
 *   * What did it cost?      slippage is adverse-positive: a positive number
 *                            is money lost, whichever side the order was.
 *   * Why did it fail?       a REJECTED row shows the venue/engine reason.
 *
 * Plus the plumbing that keeps it honest: a paper page queries with its own
 * scope, the badge is a nudge seeded from the SSE snapshot (so it works while
 * the tab is closed), and the rows are only polled while the tab is open.
 *
 * Usage: node tests/js/test_orders_tab.mjs
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import vm from "node:vm";
import assert from "node:assert/strict";

const root = fileURLToPath(new URL("../../", import.meta.url));
const load = (rel) => readFileSync(path.join(root, rel), "utf8");

// ------------------------------------------------------------------ stub DOM
function makeEl(id) {
    const handlers = {};
    const el = {
        id,
        innerHTML: "",
        textContent: "",
        value: "",
        hidden: false,
        disabled: false,
        title: "",
        style: {},
        dataset: {},
        _handlers: handlers,
        classList: {
            _s: new Set(),
            add(c) { this._s.add(c); },
            remove(c) { this._s.delete(c); },
            contains(c) { return this._s.has(c); },
        },
        addEventListener(type, fn) { (handlers[type] = handlers[type] || []).push(fn); },
        fire(type, event) { (handlers[type] || []).forEach((fn) => fn(event || {})); },
        querySelector() { return makeEl(`${id}>q`); },
        querySelectorAll() { return []; },
        focus() {},
        select() {},
        setAttribute() {},
        appendChild(c) { return c; },
    };
    return el;
}

const elements = {};
const el = (id) => (elements[id] = elements[id] || makeEl(id));
const document = {
    body: { dataset: { currencyCode: "INR", currencySymbol: "₹", currencyLocale: "en-IN" } },
    getElementById: el,
    querySelectorAll: () => [],
    addEventListener() {},
};

// ------------------------------------------------------------ fetch + toasts
const requests = [];
let respond = () => ({ ok: true, status: 200, json: async () => ({ success: true, orders: [], summary: {} }) });
const toasts = [];
const changed = [];

const sandbox = {
    console,
    setTimeout,
    URLSearchParams,
    document,
    confirm: () => true,
    fetch: (url, opts) => {
        requests.push({ url, opts });
        return Promise.resolve(respond(url, opts));
    },
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(load("src/backtest/web/static/js/components/currency.js"), sandbox);
vm.runInContext(load("src/backtest/web/static/js/components/orders_tab.js"), sandbox);
const OT = sandbox.OrdersTab;

let tests = 0;
async function test(name, fn) {
    try {
        await fn();
        tests += 1;
    } catch (err) {
        console.error(`FAIL: ${name}\n  ${err.message}`);
        process.exitCode = 1;
    }
}
const settle = () => new Promise((resolve) => setImmediate(resolve));

// ------------------------------------------------------------------- fixture
function order(overrides = {}) {
    return {
        client_order_id: "PRT-paper-1-1",
        instance_id: "inst-eq",
        symbol: "RELIANCE",
        side: "BUY",
        quantity: 100,
        order_type: "MARKET",
        limit_price: null,
        status: "FILLED",
        created_ts: "2026-09-23T09:15:00+00:00",
        updated_ts: "2026-09-23T09:15:01+00:00",
        filled_qty: 100,
        avg_fill_price: 100.25,
        filled_ts: "2026-09-23T09:15:01+00:00",
        requested_price: 100.0,
        slippage: 0.25,
        slippage_pct: 0.0025,
        reject_reason: null,
        tag: { kind: "entry", reason: "sma_cross" },
        broker_order_id: null,
        cancellable: false,
        ...overrides,
    };
}

const FILLED = order();
const PENDING = order({
    client_order_id: "PRT-live-1-7",
    instance_id: "inst-live",
    status: "PENDING",
    avg_fill_price: null,
    slippage: null,
    slippage_pct: null,
    filled_qty: 0,
    cancellable: true,
    created_ts: "2026-09-23T09:40:00+00:00",
    tag: { kind: "entry", reason: "sma_cross" },
});
const REJECTED = order({
    client_order_id: "PRT-live-1-8",
    instance_id: "inst-live",
    symbol: "TCS",
    status: "REJECTED",
    avg_fill_price: null,
    slippage: null,
    slippage_pct: null,
    reject_reason: "insufficient margin",
    tag: { kind: "entry" },
});
const LOSING_SIDE = order({
    client_order_id: "PRT-paper-1-2",
    symbol: "INFY",
    side: "SELL",
    slippage: 1.5,
    slippage_pct: 0.001,
    requested_price: 1500.0,
    avg_fill_price: 1498.5,
});

const SUMMARY = {
    total: 4, pending: 1, filled: 2, cancelled: 0, rejected: 1,
    status_counts: { FILLED: 2, PENDING: 1, REJECTED: 1 }, fills: 2,
    avg_slippage: 0.875, avg_slippage_pct: 0.00175, worst_slippage: 1.5,
    slippage_samples: 2, oldest_pending_age_s: 95.0,
};

function serve(orders, summary = SUMMARY) {
    respond = () => ({
        ok: true,
        status: 200,
        json: async () => ({ success: true, orders, summary, mode: "paper" }),
    });
}

function fresh() {
    requests.length = 0;
    toasts.length = 0;
    changed.length = 0;
    OT._state.orders = [];
    OT._state.summary = null;
    OT._state.lastFetch = 0;
    OT._state.inFlight = false;
    OT._state.filters = { status: "", search: "", limit: 100 };
    el("orders-body").innerHTML = "";
    el("orders-summary").innerHTML = "";
    el("orders-tab-badge").hidden = true;
    el("orders-tab-badge").textContent = "";
    el("orders-search").value = "";
    el("orders-status").value = "";
    el("orders-limit").value = "100";
    el("tab-orders").hidden = true;
    OT.init({ mode: "paper", toast: (message, kind) => toasts.push({ m: message, k: kind }), onChanged: () => changed.push(true) });
}

const bodyHtml = () => el("orders-body").innerHTML;
const lastToast = () => toasts[toasts.length - 1] || {};

// -------------------------------------------------------------------- tests

await test("refresh queries the page's own scope and renders the ledger", async () => {
    fresh();
    serve([FILLED, PENDING, REJECTED]);
    await OT.refresh(true);

    assert.equal(requests.length, 1);
    assert.ok(requests[0].url.startsWith("/api/portfolio/orders?"), requests[0].url);
    assert.ok(requests[0].url.includes("mode=paper"), "a paper page must not list live orders");
    assert.ok(requests[0].url.includes("limit=100"));

    const html = bodyHtml();
    assert.ok(html.includes("RELIANCE") && html.includes("TCS"));
    assert.ok(html.includes("Filled") && html.includes("Pending") && html.includes("Rejected"));
    assert.ok(html.includes("insufficient margin"), "a rejection states its reason");
    assert.ok(html.includes("100.00") && html.includes("100.25"), "requested and filled price");
});

await test("only a working order offers a cancel button", async () => {
    fresh();
    serve([FILLED, PENDING, REJECTED]);
    await OT.refresh(true);
    const html = bodyHtml();
    const cancels = (html.match(/data-cancel=/g) || []).length;
    assert.equal(cancels, 1, "one pending order, one cancel button");
    assert.ok(html.includes('data-cancel="PRT-live-1-7"'));
    const pendingRow = html.slice(html.indexOf("PRT-live-1-7"), html.indexOf("PRT-live-1-7") + 400);
    assert.ok(pendingRow.includes("Cancel"));
});

await test("slippage is adverse-positive and coloured as money lost, whichever side", async () => {
    fresh();
    serve([FILLED, LOSING_SIDE]);
    await OT.refresh(true);
    const html = bodyHtml();
    // BUY filled above the request: positive slippage, a loss.
    assert.ok(html.includes('class="num pnl-neg" title="worse than the request">+₹0.25'));
    assert.ok(html.includes("(0.25%)"));
    // SELL filled below the request: also positive (adverse), also a loss —
    // the sign never means "the price went up".
    const sellSlice = html.slice(html.indexOf("INFY"));
    assert.ok(sellSlice.includes("+₹1.50") && sellSlice.includes("pnl-neg"), sellSlice.slice(0, 200));
    assert.ok(!html.includes("pnl-pos >"), "no adverse fill may render as a gain");
});

await test("price improvement reads as a gain (negative slippage)", async () => {
    fresh();
    serve([order({ slippage: -0.5, slippage_pct: -0.005, avg_fill_price: 99.5 })]);
    await OT.refresh(true);
    const html = bodyHtml();
    assert.ok(html.includes("pnl-pos"), html.slice(0, 300));
    assert.ok(html.includes("better than the request"));
    assert.ok(html.includes("-₹0.50"));
});

await test("the summary strip answers the operator's questions at a glance", async () => {
    fresh();
    serve([FILLED, PENDING, REJECTED]);
    await OT.refresh(true);
    const summary = el("orders-summary").innerHTML;
    for (const label of ["Working", "Filled", "Rejected", "Cancelled", "Avg slippage", "Worst slippage", "Oldest working"]) {
        assert.ok(summary.includes(label), `summary is missing ${label}`);
    }
    assert.ok(summary.includes("orders-stat-warn"), "a working order is a warning, not a number");
    assert.ok(summary.includes("orders-stat-bad"), "a rejection is bad news");
    assert.ok(summary.includes("95s"), "the oldest working order shows its age");
});

await test("the badge nudges about working or rejected orders and hides when clean", async () => {
    fresh();
    serve([FILLED, PENDING, REJECTED]);
    await OT.refresh(true);
    const badge = el("orders-tab-badge");
    assert.equal(badge.hidden, false);
    assert.equal(badge.textContent, "1⏳ 1⛔");

    fresh();
    serve([FILLED], { ...SUMMARY, pending: 0, rejected: 0, total: 1 });
    await OT.refresh(true);
    assert.equal(el("orders-tab-badge").hidden, true, "nothing needs attention");
});

await test("noteTick seeds the badge from the SSE snapshot without fetching", async () => {
    fresh();
    OT.noteTick({ orders_summary: { ...SUMMARY, pending: 2, rejected: 0 } });
    await settle();
    assert.equal(requests.length, 0, "the badge must not cost a request");
    const badge = el("orders-tab-badge");
    assert.equal(badge.hidden, false);
    assert.equal(badge.textContent, "2⏳");
    assert.ok(el("orders-summary").innerHTML.includes("Working"), "the strip is ready before the tab opens");
});

await test("rows are polled only while the Orders tab is open", async () => {
    fresh();
    serve([FILLED]);
    el("tab-orders").hidden = true;
    OT.noteTick({});
    await settle();
    assert.equal(requests.length, 0, "closed tab, no polling");

    el("tab-orders").hidden = false;
    OT._state.lastFetch = 0;
    OT.noteTick({});
    await settle();
    assert.equal(requests.length, 1, "open tab, one refresh");

    // …and the 3 s throttle stops a 1 Hz render loop from becoming a 1 Hz
    // ...fetch loop.
    OT.noteTick({});
    await settle();
    assert.equal(requests.length, 1, "a second tick inside the window is throttled");

    await OT.refresh(true);
    assert.equal(requests.length, 2, "an explicit refresh still goes out");
});

await test("status and limit are server filters; search is a client filter", async () => {
    fresh();
    serve([FILLED, PENDING, REJECTED]);
    await OT.refresh(true);
    const baseline = requests.length;

    el("orders-search").value = "TCS";
    el("orders-search").fire("input");
    await settle();
    assert.equal(requests.length, baseline, "searching must not refetch");
    assert.ok(bodyHtml().includes("TCS"));
    assert.ok(!bodyHtml().includes("RELIANCE"));

    el("orders-status").value = "REJECTED";
    el("orders-status").fire("change");
    await settle();
    assert.ok(requests[requests.length - 1].url.includes("status=REJECTED"));

    el("orders-limit").value = "500";
    el("orders-limit").fire("change");
    await settle();
    assert.ok(requests[requests.length - 1].url.includes("limit=500"));
});

await test("cancelling posts to that order's own url and refreshes the tab", async () => {
    fresh();
    serve([FILLED, PENDING]);
    await OT.refresh(true);
    const before = requests.length;

    const button = { dataset: { cancel: "PRT-live-1-7" } };
    button.closest = (sel) => (sel === "[data-cancel]" ? button : null);
    el("orders-body").fire("click", { target: button });
    await settle();

    const cancellation = requests.find((r) => r.url.includes("/cancel"));
    assert.ok(cancellation, "no cancel request went out");
    assert.equal(cancellation.url, "/api/portfolio/orders/PRT-live-1-7/cancel");
    assert.equal(cancellation.opts.method, "POST");
    assert.ok(requests.length > before, "the tab re-reads the ledger after a cancel");
    assert.ok(lastToast().m.includes("Order cancelled"));
    assert.equal(changed.length, 1, "the host is told the book may have moved");
});

await test("a refused cancel shows the server's reason and still refreshes", async () => {
    fresh();
    respond = (url) => (url.includes("/cancel")
        ? { ok: false, status: 409, json: async () => ({ success: false, error: "order PRT-live-1-7 is FILLED — only PENDING orders can be cancelled" }) }
        : { ok: true, status: 200, json: async () => ({ success: true, orders: [FILLED], summary: SUMMARY }) });
    await OT.refresh(true);
    const before = requests.length;

    const button = { dataset: { cancel: "PRT-live-1-7" } };
    button.closest = (sel) => (sel === "[data-cancel]" ? button : null);
    el("orders-body").fire("click", { target: button });
    await settle();

    assert.ok(lastToast().m.includes("only PENDING orders can be cancelled"), lastToast().m);
    assert.equal(lastToast().k, "error");
    assert.ok(requests.length > before, "a failed cancel still re-reads the truth");
});

await test("the empty state says which emptiness it is", async () => {
    fresh();
    serve([]);
    await OT.refresh(true);
    assert.ok(bodyHtml().includes("No orders in this scope yet"));

    fresh();
    serve([FILLED]);
    await OT.refresh(true);
    el("orders-search").value = "NOTHING-MATCHES";
    el("orders-search").fire("input");
    assert.ok(bodyHtml().includes("No orders match the current filter"));
});

if (process.exitCode) {
    console.error(`\n${tests} passed, some failed`);
} else {
    console.log(`---ORDERS TAB OK---`);
    console.log(`${tests} tests passed`);
}
