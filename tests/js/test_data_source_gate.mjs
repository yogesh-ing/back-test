/**
 * The data-source gate in the browser.
 *
 * The server refuses a disabled source with a 409 — that is pinned in
 * tests/test_api_data_source_policy.py. What is pinned here is that the user
 * finds out *before* clicking: the banner renders, the control is disabled,
 * and the reason survives a hover.
 */
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const src = fs.readFileSync(
    path.join(ROOT, "src/backtest/web/static/js/components/data_source_gate.js"), "utf8");

const nodes = {};
const sandbox = {
    console,
    document: { getElementById: (id) => (id in nodes ? nodes[id] : null) },
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(src, sandbox, { filename: "data_source_gate.js" });
const Gate = sandbox.DataSourceGate;

let passed = 0;
function test(name, fn) {
    try { fn(); passed += 1; process.stdout.write(`  ✓ ${name}\n`); }
    catch (e) { process.stdout.write(`  ✗ ${name}\n    ${e.message}\n`); process.exitCode = 1; }
}
function node(id, extra = {}) {
    nodes[id] = Object.assign({
        innerHTML: "", disabled: false, title: "", dataset: {},
        classList: { add() {}, remove() {} },
        removeAttribute(name) { if (name === "title") this.title = ""; },
    }, extra);
    return nodes[id];
}
const ALLOWED = {
    active: "db", allowed: true, certifiable: true, refusal: null,
    sources: [{ name: "db", enabled: true, label: "Real Data (PostgreSQL)", certifiable: true }],
};
const BLOCKED = {
    active: "synthetic", allowed: false, certifiable: false,
    refusal: "Data source 'Synthetic (random walk)' is disabled in config/data_sources.yaml.",
    sources: [
        { name: "synthetic", enabled: false, label: "Synthetic (random walk)", certifiable: false },
        { name: "db", enabled: true, label: "Real Data (PostgreSQL)", certifiable: true },
    ],
};

// ------------------------------------------------------------- rendering
test("an allowed source renders one quiet line", () => {
    const el = node("g");
    assert.equal(Gate.mount("g", { status: ALLOWED }), true);
    assert.match(el.innerHTML, /Real Data \(PostgreSQL\)/);
    assert.match(el.innerHTML, /certification-grade/);
    assert.doesNotMatch(el.innerHTML, /disabled/);
});

test("a non-certifiable source says so without being blocked", () => {
    const el = node("g");
    const status = { ...ALLOWED, certifiable: false };
    assert.equal(Gate.mount("g", { status }), true, "allowed is not the same as certifiable");
    assert.match(el.innerHTML, /not certification-grade/);
});

test("a disabled source blocks, loudly, with the reason", () => {
    const el = node("g");
    assert.equal(Gate.mount("g", { status: BLOCKED }), false);
    assert.match(el.innerHTML, /Data source disabled/);
    assert.match(el.innerHTML, /config\/data_sources\.yaml/);
    assert.match(el.innerHTML, /role="alert"/);
});

test("the blocked panel names the file to edit", () => {
    const el = node("g");
    Gate.mount("g", { status: BLOCKED });
    assert.match(el.innerHTML, /Change <code>config\/data_sources\.yaml<\/code>/);
});

// -------------------------------------------------------------- controls
test("a blocked source disables the run button", () => {
    const btn = node("runBtn");
    Gate.mount("g", { status: BLOCKED, blockIds: ["runBtn"] });
    assert.equal(btn.disabled, true);
    assert.match(btn.title, /disabled/);
});

test("an allowed source leaves the button alone", () => {
    const btn = node("runBtn");
    Gate.mount("g", { status: ALLOWED, blockIds: ["runBtn"] });
    assert.equal(btn.disabled, false);
    assert.equal(btn.title, "");
});

test("a button already disabled for its own reasons stays disabled", () => {
    // optStart starts life disabled until a strategy is picked. Unblocking
    // must not hand the user a clickable button that does nothing.
    const btn = node("optStart", { disabled: true });
    Gate.mount("g", { status: BLOCKED, blockIds: ["optStart"] });
    Gate.mount("g", { status: ALLOWED, blockIds: ["optStart"] });
    assert.equal(btn.disabled, true, "restored to the state we found, not to enabled");
});

test("unblocking clears the title and the marker", () => {
    const btn = node("runBtn");
    Gate.mount("g", { status: BLOCKED, blockIds: ["runBtn"] });
    Gate.mount("g", { status: ALLOWED, blockIds: ["runBtn"] });
    assert.equal(btn.title, "");
    assert.equal(btn.dataset.dsgPinned, undefined);
});

test("repeated blocking does not clobber the saved state", () => {
    const btn = node("runBtn");
    Gate.mount("g", { status: BLOCKED, blockIds: ["runBtn"] });
    Gate.mount("g", { status: BLOCKED, blockIds: ["runBtn"] });
    Gate.mount("g", { status: ALLOWED, blockIds: ["runBtn"] });
    assert.equal(btn.disabled, false, "the second pass must not record 'already disabled'");
});

test("a control that does not exist is not an error", () => {
    delete nodes["ghost"];
    assert.doesNotThrow(() => Gate.mount("g", { status: BLOCKED, blockIds: ["ghost"] }));
});

// --------------------------------------------------------------- parsing
test("status parses from a data attribute string", () => {
    assert.equal(Gate.parseStatus(JSON.stringify(ALLOWED)).active, "db");
});

test("junk in the data attribute is ignored, not thrown on", () => {
    assert.equal(Gate.parseStatus("{not json"), null);
    assert.equal(Gate.parseStatus(""), null);
    assert.equal(Gate.parseStatus(null), null);
});

test("a missing status renders nothing rather than a broken shell", () => {
    const el = node("g");
    assert.equal(Gate.mount("g", { status: null }), null);
    assert.equal(el.innerHTML, "");
});

test("a missing status does NOT block the button", () => {
    /**
     * Fail open here, on purpose: the 409 is the actual control, this is only
     * the explanation. Blocking on a missing attribute would put a dead Run
     * button under a tooltip reading "undefined".
     */
    const btn = node("runBtn");
    assert.equal(Gate.mount("g", { status: undefined, blockIds: ["runBtn"] }), null);
    assert.equal(btn.disabled, false);
    assert.equal(btn.title, "");
});

test("an unrecognised source name falls back to the raw name", () => {
    const el = node("g");
    Gate.mount("g", { status: { active: "weird", allowed: true, certifiable: true, sources: [] } });
    assert.match(el.innerHTML, /weird/);
});

// -------------------------------------------------------------- escaping
test("a hostile label or refusal is escaped", () => {
    const el = node("g");
    const hostile = {
        active: "x", allowed: false, certifiable: false,
        refusal: "<img src=x onerror=alert(1)>",
        sources: [{ name: "x", enabled: false, label: "<b>bold</b>" }],
    };
    Gate.mount("g", { status: hostile });
    assert.doesNotMatch(el.innerHTML, /<img/);
    assert.doesNotMatch(el.innerHTML, /<b>/);
    assert.match(el.innerHTML, /&lt;img/);
});

console.log(`\n${passed} tests passed`);
