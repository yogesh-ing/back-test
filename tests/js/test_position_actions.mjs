/**
 * Position actions — behaviour tests (Live Order Management).
 *
 * The positions table's four action buttons are the only controls in the app
 * that move money on ONE position, so their three failure modes are pinned
 * here rather than left to a browser:
 *
 *   1. acting on the wrong row (the payload must carry the row's own
 *      instance_id + position_key, and a row that left the book must be
 *      refused, not guessed at),
 *   2. inventing a level the engine would refuse (the server validates against
 *      the live mark; its refusal must be shown verbatim and the modal must
 *      stay open so the operator can correct it),
 *   3. claiming a fill that did not happen (a live order that was only PLACED
 *      at the venue must not read as closed).
 *
 * Usage: node tests/js/test_position_actions.mjs
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
let respond = () => ({ ok: true, status: 200, json: async () => ({ success: true }) });
const toasts = [];
const done = [];

const sandbox = {
    console,
    setTimeout,
    URLSearchParams,
    document,
    fetch: (url, opts) => {
        requests.push({ url, opts });
        return Promise.resolve(respond(url, opts));
    },
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(load("src/backtest/web/static/js/components/currency.js"), sandbox);
vm.runInContext(load("src/backtest/web/static/js/components/position_actions.js"), sandbox);
const PA = sandbox.PositionActions;

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
const ROW = {
    instance_id: "inst-eq",
    position_key: "RELIANCE",
    kind: "equity",
    runner: "EQ-1",
    symbol: "RELIANCE",
    label: "RELIANCE",
    side: "LONG",
    qty: 100,
    units: 100,
    entry_price: 100.0,
    current_price: 104.5,
    unrealized_pnl: 450.0,
    stop_loss: null,
    target: null,
    can_partial_close: true,
    stale: false,
};

const table = el("aggregate-positions");

function buttonFor(verb, row = ROW) {
    const button = {
        dataset: { posAct: verb, id: row.instance_id, key: row.position_key || row.symbol },
        disabled: false,
    };
    button.closest = (sel) => (sel === "[data-pos-act]" ? button : null);
    return button;
}

function init(rows) {
    PA.init({
        rows,
        tableId: "aggregate-positions",
        toast: (message, kind) => toasts.push({ m: message, k: kind }),
        onDone: () => done.push(true),
    });
}

const lastBody = () => JSON.parse(requests[requests.length - 1].opts.body);
const lastToast = () => toasts[toasts.length - 1] || {};

// ------------------------------------------------------------------- tests

await test("buttonCell renders all four verbs with the row's own identity", () => {
    const html = PA.buttonCell(ROW);
    for (const verb of ["modify_sl", "modify_target", "close_fraction", "close_all"]) {
        assert.ok(html.includes(`data-pos-act="${verb}"`), `missing verb ${verb}`);
    }
    assert.ok(html.includes('data-id="inst-eq"'));
    assert.ok(html.includes('data-key="RELIANCE"'));
    assert.ok(!html.includes("disabled"), "a scalable equity position can scale out");

    const structure = PA.buttonCell({ ...ROW, kind: "option", position_key: "struct_9", can_partial_close: false });
    assert.ok(structure.includes('data-pos-act="close_fraction"'));
    assert.ok(structure.includes("disabled"), "structures close atomically — 50% must be disabled");
    assert.ok(structure.includes('data-key="struct_9"'));
});

await test("clicking a row's button opens that row's modal, named and pre-filled", () => {
    init([ROW]);
    table.fire("click", { target: buttonFor("modify_sl") });
    assert.equal(el("pos-sl-modal").hidden, false);
    assert.ok(el("pos-sl-context").innerHTML.includes("RELIANCE"));
    assert.ok(el("pos-sl-hint").textContent.includes("104.50"), "hint names the live mark");
    assert.equal(el("pos-sl-value").value, "", "nothing armed yet");
    PA.close("pos-sl-modal");

    init([{ ...ROW, stop_loss: 96.5 }]);
    table.fire("click", { target: buttonFor("modify_sl") });
    assert.equal(el("pos-sl-value").value, 96.5, "an existing stop is editable, not blank");
});

await test("submitting a stop posts the row's own ids and reports what was armed", async () => {
    requests.length = 0;
    toasts.length = 0;
    done.length = 0;
    respond = () => ({ ok: true, status: 200, json: async () => ({ success: true, value: 99.5 }) });
    init([ROW]);
    table.fire("click", { target: buttonFor("modify_sl") });
    el("pos-sl-value").value = "99.5";
    el("pos-sl-submit").fire("click");
    await settle();

    assert.equal(requests.length, 1);
    assert.equal(requests[0].url, "/api/portfolio/position/action");
    assert.equal(requests[0].opts.method, "POST");
    assert.deepEqual(lastBody(), {
        instance_id: "inst-eq",
        position_key: "RELIANCE",
        action: "modify_stop_loss",
        value: 99.5,
    });
    assert.equal(el("pos-sl-modal").hidden, true, "success closes the modal");
    assert.ok(lastToast().m.includes("99.50"), lastToast().m);
    assert.ok(lastToast().m.includes("RELIANCE"));
    assert.equal(lastToast().k, "success");
    assert.equal(done.length, 1, "the host is told to refresh");
});

await test("clearing a level sends the clear verb, not a zero price", async () => {
    requests.length = 0;
    toasts.length = 0;
    respond = () => ({ ok: true, status: 200, json: async () => ({ success: true, value: null }) });
    init([{ ...ROW, stop_loss: 96.5 }]);
    table.fire("click", { target: buttonFor("modify_sl") });
    el("pos-sl-clear").fire("click");
    await settle();
    assert.deepEqual(lastBody(), {
        instance_id: "inst-eq",
        position_key: "RELIANCE",
        action: "clear_stop_loss",
    });
    assert.equal(el("pos-sl-modal").hidden, true);
});

await test("a refused level is surfaced verbatim and the modal stays open", async () => {
    requests.length = 0;
    toasts.length = 0;
    done.length = 0;
    const refusal = "stop-loss 250.00 is at or above the current mark 104.50 — it would exit immediately; use Close instead";
    respond = () => ({ ok: false, status: 409, json: async () => ({ success: false, error: refusal }) });
    init([ROW]);
    table.fire("click", { target: buttonFor("modify_sl") });
    el("pos-sl-value").value = "250";
    el("pos-sl-submit").fire("click");
    await settle();

    assert.equal(lastToast().m, refusal, "the engine's own words, not a paraphrase");
    assert.equal(lastToast().k, "error");
    assert.equal(el("pos-sl-modal").hidden, false, "a refusal must not close the modal");
    assert.equal(done.length, 0, "nothing changed in the book");
});

await test("an empty level is refused without a network call", async () => {
    requests.length = 0;
    toasts.length = 0;
    init([ROW]);
    table.fire("click", { target: buttonFor("modify_target") });
    el("pos-target-value").value = "";
    el("pos-target-submit").fire("click");
    await settle();
    assert.equal(requests.length, 0);
    assert.ok(lastToast().m.includes("Enter a target level"));
});

await test("a row that left the book is refused, never acted on", async () => {
    requests.length = 0;
    toasts.length = 0;
    init([]); // the snapshot no longer carries that position
    table.fire("click", { target: buttonFor("close_all") });
    await settle();
    assert.equal(requests.length, 0, "no order for a position that is gone");
    assert.ok(lastToast().m.includes("no longer in the book"));
});

await test("close-all reports the fill price and marked P&L", async () => {
    requests.length = 0;
    toasts.length = 0;
    respond = () => ({
        ok: true,
        status: 200,
        json: async () => ({ success: true, status: "filled", qty_closed: 100, price: 104.4, remaining_qty: 0 }),
    });
    init([ROW]);
    table.fire("click", { target: buttonFor("close_all") });
    el("pos-closeall-confirm").fire("click");
    await settle();

    assert.deepEqual(lastBody(), {
        instance_id: "inst-eq",
        position_key: "RELIANCE",
        action: "close_all",
    });
    assert.equal(el("pos-closeall-modal").hidden, true);
    const message = lastToast().m;
    assert.ok(message.includes("Closed RELIANCE"), message);
    assert.ok(message.includes("104.40"), message);
    assert.ok(message.includes("+₹450.00"), message);
});

await test("a live close that was only PLACED does not read as filled", async () => {
    requests.length = 0;
    toasts.length = 0;
    respond = () => ({
        ok: true,
        status: 200,
        json: async () => ({ success: true, status: "placed", coid: "PRT-live-1-1" }),
    });
    init([{ ...ROW, runner: "LIVE-1", instance_id: "inst-live" }]);
    table.fire("click", { target: buttonFor("close_all", { ...ROW, instance_id: "inst-live" }) });
    el("pos-closeall-confirm").fire("click");
    await settle();
    const message = lastToast().m;
    assert.ok(message.includes("PLACED"), message);
    assert.ok(message.includes("not filled yet"), message);
});

await test("scaling out reports units closed, price and what is left", async () => {
    requests.length = 0;
    toasts.length = 0;
    respond = () => ({
        ok: true,
        status: 200,
        json: async () => ({ success: true, status: "filled", qty_closed: 50, price: 104.6, remaining_qty: 50 }),
    });
    init([ROW]);
    table.fire("click", { target: buttonFor("close_fraction") });
    el("pos-partial-value").value = "0.5";
    el("pos-partial-submit").fire("click");
    await settle();

    assert.equal(lastBody().fraction, 0.5);
    const message = lastToast().m;
    assert.ok(message.includes("Closed 50%"), message);
    assert.ok(message.includes("50 units @ 104.60"), message);
    assert.ok(message.includes("still working"), message);
});

await test("an option structure closes in full only — the fraction is forced to 1", async () => {
    requests.length = 0;
    toasts.length = 0;
    respond = () => ({
        ok: true,
        status: 200,
        json: async () => ({ success: true, status: "filled", qty_closed: 900, price: 38.05, remaining_qty: 0 }),
    });
    const structure = {
        ...ROW,
        kind: "option",
        position_key: "struct_9",
        label: "NIFTY bull_call_spread",
        qty: 12,
        units: 900,
        can_partial_close: false,
    };
    init([structure]);
    table.fire("click", { target: buttonFor("close_fraction", structure) });
    assert.equal(el("pos-partial-value").value, "1", "the input is pinned to 1");
    assert.ok(el("pos-partial-note").innerHTML.includes("legs close together"));

    el("pos-partial-submit").fire("click");
    await settle();
    assert.equal(lastBody().position_key, "struct_9");
    assert.equal(lastBody().fraction, 1, "an option structure cannot half-close");
});

await test("a nonsense fraction is refused client-side", async () => {
    requests.length = 0;
    toasts.length = 0;
    init([ROW]);
    table.fire("click", { target: buttonFor("close_fraction") });
    el("pos-partial-value").value = "0";
    el("pos-partial-submit").fire("click");
    await settle();
    assert.equal(requests.length, 0);
    assert.ok(lastToast().m.includes("between 0 and 1"));
});

if (process.exitCode) {
    console.error(`\n${tests} passed, some failed`);
} else {
    console.log(`---POSITION ACTIONS OK---`);
    console.log(`${tests} tests passed`);
}
