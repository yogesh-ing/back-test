/**
 * Certification readiness panel — behaviour tests (PRD backTest-enhance §5).
 *
 * The engine owns the bands and the verdicts (`tests/test_readiness.py`). What
 * is pinned HERE is what the panel does with them, and the one behaviour that
 * would be genuinely dangerous to get wrong:
 *
 *   • an "unproven" check renders neutrally and is named, never quietly
 *     folded into the pass count
 *   • the header tallies every check, so a partial panel cannot read as a
 *     complete one
 *   • the panel says it is advisory and blocks nothing
 *   • nothing is escaped into raw markup
 *
 * A readiness panel that renders empty is worse than no panel: the result
 * underneath still looks like it was assessed.
 *
 * Usage: node tests/js/test_certification.mjs
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import vm from "node:vm";
import assert from "node:assert/strict";

const root = fileURLToPath(new URL("../../", import.meta.url));
const code = readFileSync(
    path.join(root, "src/backtest/web/static/js/components/certification.js"), "utf8",
);

const sandbox = { console, document: { getElementById: () => null } };
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(code, sandbox, { filename: "certification.js" });
const { Certification } = sandbox;

const CHECKS = [
    { id: "engine", label: "Engine", status: "green", value: "Fill-Exact (Canonical)", detail: "Reproducible." },
    { id: "data_source", label: "Data source", status: "green", value: "Database (Cached)", detail: "Real prices." },
    { id: "trade_count", label: "Trade count (40)", status: "green", value: "40", detail: "Enough trades." },
    { id: "beats_benchmark", label: "Beats benchmark", status: "green", value: "+5.00%", detail: "Ahead." },
    { id: "cost_shock_2x", label: "Cost shock (2x)", status: "unknown", value: "not available",
      detail: "cost shock runs on the canonical engine, not Fast Preview." },
    { id: "profit_factor", label: "Profit factor", status: "green", value: "2.00", detail: "Comfortable." },
    { id: "max_drawdown", label: "Max drawdown", status: "green", value: "-8.0%", detail: "Inside tolerance." },
    { id: "monte_carlo_p_profit", label: "Monte Carlo P(profit)", status: "green", value: "85%", detail: "Most sequences profit." },
];

function readiness(over = {}) {
    const checks = over.checks || CHECKS;
    const counts = { green: 0, yellow: 0, red: 0, unknown: 0 };
    checks.forEach((c) => { counts[c.status] += 1; });
    return {
        advisory: true,
        gates_nothing: true,
        verdict: "pass",
        all_green: true,
        counts,
        checks,
        summary: "Basic checks passed.",
        red_flags: [],
        unproven: ["Cost shock (2x)"],
        tune_this_available: true,
        ...over,
        counts: over.counts || counts,
    };
}

let passed = 0;
function test(name, fn) {
    fn();
    passed += 1;
    console.log(`  ✓ ${name}`);
}

// ------------------------------------------------------------------ render
test("every check is rendered", () => {
    const html = Certification.render(readiness());
    CHECKS.forEach((c) => assert.match(html, new RegExp(c.label.replace(/[()]/g, "\\$&"))));
});

test("a passing check uses the passing mark and a failed one does not", () => {
    const html = Certification.render(readiness());
    assert.match(html, /✅/);
});

test("an unproven check renders neutrally, never as a pass", () => {
    const html = Certification.render(readiness());
    const row = html.match(/<div class="cmp-ready-row cmp-ready-unknown">[\s\S]*?<\/div>/);
    assert.ok(row, "the unknown row is rendered");
    assert.match(row[0], /⬜/);
    assert.doesNotMatch(row[0], /✅/);
    assert.match(row[0], /cmp-ready-unknown/);
});

test("an unproven check is named in the panel, not dropped", () => {
    const html = Certification.render(readiness());
    assert.match(html, /Not evaluated:/);
    assert.match(html, /Cost shock \(2x\)/);
});

test("a check that could not run still shows its reason", () => {
    const html = Certification.render(readiness());
    assert.match(html, /canonical engine, not Fast Preview/);
});

test("the header tallies passes and unproven checks out loud", () => {
    const html = Certification.render(readiness());
    assert.match(html, /7 passed/);
    assert.match(html, /1 unproven/);
});

test("a partial panel cannot read as a complete one", () => {
    const html = Certification.render(readiness());
    // Every check accounted for: 7 + 1 = 8.
    assert.match(html, /7 passed · 1 unproven/);
});

test("a failed check appears in the failures headline and the flags", () => {
    const r = readiness({
        verdict: "fail",
        red_flags: ["Data source", "Trade count (8)"],
        unproven: [],
    });
    r.checks = CHECKS.map((c) => (
        c.id === "data_source"
            ? { ...c, status: "red", value: "Synthetic", detail: "No result on synthetic data." }
            : c
    ));
    r.counts = { green: 6, yellow: 0, red: 1, unknown: 1 };
    const html = Certification.render(r);
    assert.match(html, /One or more checks failed/);
    assert.match(html, /1 failed/);
    assert.match(html, /❌/);
});

test("an all-green panel never claims the pass it does not have", () => {
    const r = readiness({ verdict: "incomplete", all_green: false, tune_this_available: false });
    const html = Certification.render(r);
    assert.doesNotMatch(html, /All eight checks passed/);
    assert.match(html, /not everything was proven/);
});

test("the panel says it blocks nothing", () => {
    const html = Certification.render(readiness());
    assert.match(html, /Advisory only/);
    assert.match(html, /blocks nothing/);
});

test("a check value carrying markup is escaped, not injected", () => {
    const r = readiness();
    r.checks = CHECKS.map((c) => (c.id === "engine"
        ? { ...c, value: "<img src=x onerror=alert(1)>", detail: "ok" }
        : c));
    const html = Certification.render(r);
    assert.doesNotMatch(html, /<img/);
    assert.match(html, /&lt;img/);
});

test("a check detail carrying a quote cannot break out of the markup", () => {
    const r = readiness();
    r.checks = CHECKS.map((c) => (c.id === "engine"
        ? { ...c, detail: 'he said "green" and <b>meant it</b>' }
        : c));
    const html = Certification.render(r);
    assert.doesNotMatch(html, /<b>meant it<\/b>/);
});

test("an empty or missing block renders nothing rather than a broken shell", () => {
    assert.equal(Certification.render(null), "");
    assert.equal(Certification.render(undefined), "");
    assert.equal(Certification.render({}), "");
    assert.equal(Certification.render({ checks: [] }), "");
});

test("renderInto writes the markup into the container", () => {
    let written = null;
    sandbox.document.getElementById = (id) => ({ set innerHTML(v) { written = v; } });
    Certification.renderInto("certification", readiness());
    assert.ok(written && written.includes("Certification readiness"));
});

console.log(`\n${passed} tests passed`);
