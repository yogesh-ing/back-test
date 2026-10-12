/**
 * The setup page's data confirmation box — PRD backTest-enhance Part 2 §2.
 *
 * The server is pinned in tests/optimization/test_attestation.py. What is
 * pinned here is the promise the PRD makes about the page: a preview must not
 * invent a bar count, synthetic must be visibly gated, and a box that failed
 * to load must not read as a box that says the data is fine.
 */
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const src = fs.readFileSync(
    path.join(ROOT, "src/backtest/web/static/js/optimize_setup.js"), "utf8");

/** Lift a single top-level function out of the IIFE by brace counting.
 *  Slicing to "the next `    function`" silently swallows the rest of the file
 *  when the lifted function happens to be the last one. */
function lift(name) {
    const start = src.indexOf(`function ${name}`);
    assert.ok(start > 0, `${name} must exist in optimize_setup.js`);
    let depth = 0;
    let seen = false;
    for (let i = src.indexOf("{", start); i < src.length; i += 1) {
        if (src[i] === "{") { depth += 1; seen = true; }
        else if (src[i] === "}") {
            depth -= 1;
            if (seen && depth === 0) return src.slice(start, i + 1);
        }
    }
    throw new Error(`unbalanced braces lifting ${name}`);
}
const body = lift("renderAttestation") + "\n" + lift("attestationCleared");

const nodes = {};
const $ = (id) => nodes[id];
const C = {
    isNum: (v) => typeof v === "number" && Number.isFinite(v),
    fmtNum: (v, d) => Number(v).toFixed(d),
    escapeHtml: (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;"),
    api: async () => ({}),
    toast: () => {},
};
const state = { attestation: null, syntheticAck: false, lastEstimate: null };
// The tick handler re-runs the start-button's enabled state, which lives in
// renderEstimate. Stubbed here; its own behaviour is not what this file pins.
const sandbox = { C, console, $, state, renderEstimate() {} };
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
const api = vm.runInContext(
    `(() => { ${body}\nreturn { renderAttestation, attestationCleared }; })()`, sandbox);

function node() {
    return { textContent: "", innerHTML: "", className: "", hidden: true, checked: false,
             addEventListener(_e, fn) { this.onChange = fn; } };
}
function mount() {
    for (const id of ["optAttestation", "optSyntheticAck"]) nodes[id] = node();
    state.attestation = null;
    state.syntheticAck = false;
    return nodes;
}

const REAL = {
    attestation: { data_source: "db", data_source_label: "Real (PostgreSQL)",
                   data_source_real: true, symbol: "RELIANCE", timeframe: "1day",
                   bars_count: null, date_from: "2020-01-01", date_to: "2024-12-31",
                   data_fetch_date: "2026-09-27", stale: false, stale_days: 2,
                   requires_acknowledgement: false },
    warnings: [],
    satisfied: true,
    acknowledgement: null,
};
const SYNTHETIC = {
    attestation: { data_source: "synthetic", data_source_label: "Synthetic",
                   data_source_real: false, symbol: "DEMO", timeframe: "1day",
                   bars_count: null, date_from: "2021-01-01", date_to: "2023-12-31",
                   data_fetch_date: null, stale: false, stale_days: null,
                   requires_acknowledgement: true },
    warnings: [{ level: "error", code: "synthetic_data", message: "Synthetic data." }],
    satisfied: false,
    acknowledgement: "I understand this is synthetic data and results are not certification-grade",
};

let passed = 0;
function test(name, fn) {
    try { fn(); passed += 1; process.stdout.write(`  ✓ ${name}\n`); }
    catch (e) { process.stdout.write(`  ✗ ${name}\n    ${e.message}\n`); process.exitCode = 1; }
}

// -------------------------------------------------------------- what it says
test("the source is named in words", () => {
    const n = mount();
    api.renderAttestation(REAL);
    assert.match(n.optAttestation.innerHTML, /Real \(PostgreSQL\)/);
});

test("a preview does not invent a bar count", () => {
    /** Bar count is not knowable until the candles load. A box that guessed
     *  would be a box that lies. */
    const n = mount();
    api.renderAttestation(REAL);
    assert.doesNotMatch(n.optAttestation.innerHTML, /1247/);
    assert.match(n.optAttestation.innerHTML, /shown after the run/);
});

test("a measured bar count is shown once there is one", () => {
    const n = mount();
    api.renderAttestation({ ...REAL, attestation: { ...REAL.attestation, bars_count: 1247 } });
    assert.match(n.optAttestation.innerHTML, /1,247/);
});

test("zero bars is shown as zero, not as unknown", () => {
    const n = mount();
    api.renderAttestation({ ...REAL, attestation: { ...REAL.attestation, bars_count: 0 } });
    assert.match(n.optAttestation.innerHTML, />0</);
});

test("the range is shown with both ends", () => {
    const n = mount();
    api.renderAttestation(REAL);
    assert.match(n.optAttestation.innerHTML, /2020-01-01 → 2024-12-31/);
});

test("a fetch date the source cannot give says so", () => {
    /** None and "today" are different answers. None is the one worth saying. */
    const n = mount();
    api.renderAttestation({
        ...REAL,
        attestation: { ...REAL.attestation, data_fetch_date: null },
    });
    assert.match(n.optAttestation.innerHTML, /unknown/);
});

// -------------------------------------------------------------- the gate
test("synthetic asks for the tick and the tick is not preselected", () => {
    const n = mount();
    api.renderAttestation(SYNTHETIC);
    assert.match(n.optAttestation.innerHTML, /I understand this is synthetic data/);
    assert.equal(n.optSyntheticAck.checked, false);
    assert.match(n.optAttestation.className, /synthetic/);
});

test("synthetic does not clear the gate on its own", () => {
    const n = mount();
    state.attestation = SYNTHETIC;
    assert.equal(api.attestationCleared(), false);
});

test("ticking the box clears the gate", () => {
    const n = mount();
    state.attestation = SYNTHETIC;
    api.renderAttestation(SYNTHETIC);
    n.optSyntheticAck.checked = true;
    n.optSyntheticAck.onChange();
    assert.equal(state.syntheticAck, true);
    assert.equal(api.attestationCleared(), true);
});

test("real data never asks for the tick", () => {
    const n = mount();
    api.renderAttestation(REAL);
    assert.equal(n.optSyntheticAck.hidden, true);
    state.attestation = REAL;
    assert.equal(api.attestationCleared(), true);
});

test("a box that failed to load does not clear the gate", () => {
    /** It must not look like a box that says the data is fine. */
    state.attestation = null;
    assert.equal(api.attestationCleared(), false);
});

test("warnings are shown, not swallowed", () => {
    const n = mount();
    api.renderAttestation({
        ...REAL,
        warnings: [{ level: "warning", code: "stale_data", message: "Data was last fetched 90 days ago." }],
    });
    assert.match(n.optAttestation.innerHTML, /90 days ago/);
});

test("stale data warns and still allows the run", () => {
    /** Re-fetching is the operator's decision. A box that refuses work for
     *  reasons they cannot act on teaches them to ignore it. */
    const n = mount();
    state.attestation = REAL;
    api.renderAttestation({
        ...REAL,
        attestation: { ...REAL.attestation, stale: true, stale_days: 90 },
        warnings: [{ level: "warning", code: "stale_data", message: "Data was last fetched 90 days ago." }],
    });
    assert.match(n.optAttestation.innerHTML, /90 days ago/);
    assert.equal(api.attestationCleared(), true);
});

test("messages are escaped", () => {
    const n = mount();
    api.renderAttestation({ ...REAL, warnings: [{ level: "error", message: "<img src=x onerror=1>" }] });
    assert.doesNotMatch(n.optAttestation.innerHTML, /<img/);
});

// ------------------------------------------------------------- shipping
test("the setup page ships the box", () => {
    const html = fs.readFileSync(path.join(ROOT, "src/backtest/web/templates/optimize.html"), "utf8");
    assert.match(html, /id="optAttestation"/);
});

test("the submit path sends the acknowledgement the operator gave", () => {
    /** So the stored run records what was agreed to. The server re-checks it
     *  regardless — sending it is not for the server's sake. */
    assert.match(src, /dataAttestation\s*=\s*\{/);
    assert.match(src, /acknowledged:\s*true/);
});

test("the refusal is matched on its code, never on its wording", () => {
    /** A refusal whose meaning lives in a sentence breaks on the next reword. */
    assert.match(src, /e\.code === 'synthetic_data_not_acknowledged'/);
});

console.log(`\n${passed} tests passed`);
