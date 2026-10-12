/**
 * Provenance badges — behaviour tests (PRD backTest-enhance §1.1 + §1.2).
 *
 * The Python side (tests/test_provenance.py) owns the wording; what is pinned
 * HERE is that the page actually shows it and cannot be talked out of it: a
 * synthetic-data result must render the red banner, a quick-screen result the
 * yellow one, and a canonical real-data result neither. A component that
 * silently renders nothing is worse than no component — the numbers look
 * certification-grade with no badge above them at all.
 *
 * Usage: node tests/js/test_provenance.mjs
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import vm from "node:vm";
import assert from "node:assert/strict";

const root = fileURLToPath(new URL("../../", import.meta.url));
const code = readFileSync(
    path.join(root, "src/backtest/web/static/js/components/provenance.js"), "utf8",
);

// ------------------------------------------------------------------ tiny DOM
function makeEl(id) {
    return {
        id, innerHTML: "", className: "", hidden: false,
        children: [], style: {}, dataset: {},
        querySelector() { return makeEl(`${id}::sub`); },
        querySelectorAll() { return []; },
        addEventListener() {},
    };
}

const elements = {};
const el = (id) => (elements[id] = elements[id] || makeEl(id));

const sandbox = { console, document: { getElementById: el }, String, Number, Array };
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(code, sandbox, { filename: "provenance.js" });

const { Provenance } = sandbox;

/** A provenance block as the API would stamp it. */
function prov(over = {}) {
    return {
        data_source: "synthetic",
        data_source_label: "Synthetic",
        data_source_real: false,
        engine_used: "backtest_driver",
        engine_label: "Fill-Exact (Canonical)",
        engine_canonical: true,
        engine_tier: "canonical",
        symbol: "RELIANCE",
        timeframe: "1D",
        date_range: { from: "2020-01-01", to: "2024-12-31" },
        data_from: "2020-01-01",
        data_to: "2024-12-31",
        data_fetch_date: null,
        bars_count: 1247,
        warnings: [{
            level: "error",
            code: "non_real_data",
            message: "Results based on Synthetic data. Real-data certification required before paper testing.",
        }],
        ...over,
    };
}

let passed = 0;
function test(name, fn) {
    fn();
    passed += 1;
    console.log(`  ✓ ${name}`);
}

// ------------------------------------------------------------------ badges
test("always shows which engine and which data produced the numbers", () => {
    Provenance.render(el("a"), prov());
    const html = el("a").innerHTML;
    assert.match(html, /Engine: Fill-Exact \(Canonical\)/);
    assert.match(html, /Data: Synthetic/);
    assert.match(html, /prov-badge/);
});

test("canonical engine and real data render green, no banner", () => {
    Provenance.render(el("b"), prov({
        data_source: "db", data_source_label: "Real (PostgreSQL)", data_source_real: true,
        data_fetch_date: "2026-09-27", warnings: [],
    }));
    const html = el("b").innerHTML;
    assert.match(html, /prov-badge-ok/);
    assert.ok(!html.includes("prov-banner"), "a clean real-data run must not cry wolf");
});

test("synthetic data renders the red, non-dismissable banner", () => {
    Provenance.render(el("c"), prov());
    const html = el("c").innerHTML;
    assert.match(html, /prov-banner-error/);
    assert.match(html, /Real-data certification required before paper testing/);
    // Nothing in the strip may offer a close control — it is advisory, not dismissable.
    assert.ok(!/<button/i.test(html), "the provenance banner must not be dismissable");
});

test("quick-screen renders the yellow approximate warning", () => {
    Provenance.render(el("d"), prov({
        engine_used: "quick_screen",
        engine_label: "Quick-Screen (Approximate)",
        engine_canonical: false,
        engine_tier: "approximate",
        data_source: "db", data_source_label: "Real (PostgreSQL)", data_source_real: true,
        warnings: [{ level: "warning", code: "approximate_engine",
                     message: "These numbers are approximate." }],
    }));
    const html = el("d").innerHTML;
    assert.match(html, /prov-badge-warn/);
    assert.match(html, /prov-banner-warn/);
    assert.match(html, /These numbers are approximate/);
});

test("data and engine warnings stack, both visible", () => {
    Provenance.render(el("e"), prov({
        engine_used: "quick_screen", engine_label: "Quick-Screen (Approximate)",
        engine_canonical: false, engine_tier: "approximate",
        warnings: [
            { level: "error", code: "non_real_data", message: "Synthetic data." },
            { level: "warning", code: "approximate_engine", message: "Approximate engine." },
        ],
    }));
    assert.equal((el("e").innerHTML.match(/prov-banner-/g) || []).length, 2);
});

test("mixed engines say the comparison is not like-for-like", () => {
    Provenance.render(el("f"), prov({
        engine_used: "mixed", engine_label: "Mixed (see each result)",
        engine_canonical: false, engine_tier: "mixed",
        data_source: "db", data_source_label: "Real (PostgreSQL)", data_source_real: true,
        warnings: [{ level: "warning", code: "mixed_engine", message: "not like-for-like" }],
    }));
    assert.match(el("f").innerHTML, /not like-for-like/);
});

test("coverage detail rides on the badges: range and bar count", () => {
    Provenance.render(el("g"), prov());
    const html = el("g").innerHTML;
    assert.match(html, /2020-01-01 → 2024-12-31/);
    assert.match(html, /1247 bars/);
});

test("a single-day range is not rendered with a redundant arrow", () => {
    Provenance.render(el("h"), prov({
        date_range: { from: "2024-03-01", to: "2024-03-01" },
        data_from: "2024-03-01", data_to: "2024-03-01",
    }));
    assert.match(el("h").innerHTML, /2024-03-01/);
    assert.ok(!el("h").innerHTML.includes("→"));
});

test("renderInto resolves the container by id", () => {
    Provenance.renderInto("i", prov());
    assert.match(el("i").innerHTML, /Engine:/);
});

test("a missing container is a no-op, not a crash", () => {
    Provenance.renderInto("does-not-exist", prov());  // must not throw
});

test("no provenance renders an empty strip (a page that has not run yet)", () => {
    Provenance.render(el("j"), null);
    assert.equal(el("j").innerHTML, "");
    Provenance.render(el("j"), undefined);
    assert.equal(el("j").innerHTML, "");
});

test("server-supplied warning text is escaped, never injected as HTML", () => {
    Provenance.render(el("k"), prov({
        warnings: [{ level: "error", code: "x", message: "<img src=x onerror=alert(1)>" }],
    }));
    const html = el("k").innerHTML;
    assert.ok(!html.includes("<img"), "raw markup must not survive into the DOM");
    assert.match(html, /&lt;img/);
});

test("badge labels are escaped too", () => {
    Provenance.render(el("l"), prov({ data_source_label: "<b>Real</b>" }));
    assert.ok(!el("l").innerHTML.includes("<b>Real</b>"));
});

console.log(`\nprovenance badges: ${passed} tests passed`);
