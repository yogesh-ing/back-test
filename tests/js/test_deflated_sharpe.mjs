/**
 * The Deflated Sharpe card — PRD backTest-enhance Part 2 §3.
 *
 * The backend is pinned in tests/optimization/test_deflation.py. What is
 * pinned here is that the two numbers stay distinguishable, and that an
 * absent statistic explains itself instead of looking like a zero.
 */
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const src = fs.readFileSync(
    path.join(ROOT, "src/backtest/web/static/js/optimize_run.js"), "utf8");

// The render function under test is self-contained enough to lift out of the
// IIFE rather than standing up the whole page.
const start = src.indexOf("function renderDeflatedSharpe");
assert.ok(start > 0, "renderDeflatedSharpe must exist in optimize_run.js");
const end = src.indexOf("\n    function ", start + 10);
const body = src.slice(start, end > start ? end : undefined);

const nodes = {};
const $ = (id) => nodes[id];
const C = {
    isNum: (v) => typeof v === "number" && Number.isFinite(v),
    fmtNum: (v, d) => (v === null || v === undefined ? "—" : Number(v).toFixed(d)),
    escapeHtml: (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;"),
};
const sandbox = { C, console, $ };
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
const render = vm.runInContext(
    `(() => { ${body}\nreturn renderDeflatedSharpe; })()`, sandbox);

function node() {
    return { textContent: "", innerHTML: "", className: "" };
}
function mount() {
    for (const id of ["dsrBar", "dsrObserved", "dsrVerdict", "dsrFacts", "dsrReason"]) {
        nodes[id] = node();
    }
    return nodes;
}

const OK = {
    status: "ok", deflated_sharpe: 0.89, probability: 0.52, trials: 56,
    observations: 756, observed_sharpe: 1.42, reason: null,
};

let passed = 0;
function test(name, fn) {
    try { fn(); passed += 1; process.stdout.write(`  ✓ ${name}\n`); }
    catch (e) { process.stdout.write(`  ✗ ${name}\n    ${e.message}\n`); process.exitCode = 1; }
}

// ----------------------------------------------------------------- present
test("the bar and the observed Sharpe are both shown", () => {
    const n = mount();
    render(OK, { sharpe: 1.42 });
    assert.equal(n.dsrBar.textContent, "0.89");
    assert.equal(n.dsrObserved.textContent, "1.42");
});

test("a result that clears the bar says so", () => {
    const n = mount();
    render(OK, { sharpe: 1.42 });
    assert.match(n.dsrVerdict.className, /pos/);
    assert.match(n.dsrVerdict.textContent, /Clears the bar by 0\.53/);
});

test("a result that misses the bar says so, and by how much", () => {
    const n = mount();
    render(OK, { sharpe: 0.4 });
    assert.match(n.dsrVerdict.className, /neg/);
    assert.match(n.dsrVerdict.textContent, /short by 0\.49/);
});

test("the number of combinations tried is stated", () => {
    const n = mount();
    render(OK, { sharpe: 1.42 });
    assert.match(n.dsrFacts.innerHTML, /Chances taken/);
    assert.match(n.dsrFacts.innerHTML, /56/);
});

test("the probability is labelled as a probability", () => {
    /**
     * A bare "0.52" next to a Sharpe reads as another Sharpe. The label is
     * what keeps the two scales from being confused.
     */
    const n = mount();
    render(OK, { sharpe: 1.42 });
    assert.match(n.dsrFacts.innerHTML, /Likely genuinely positive/);
    assert.match(n.dsrFacts.innerHTML, /52%/);
});

// ----------------------------------------------------------------- absent
test("an absent statistic explains itself rather than showing a zero", () => {
    const n = mount();
    render({ status: "insufficient_data", deflated_sharpe: null,
        reason: "Only 2 combination(s) tested." }, { sharpe: 1.42 });
    assert.equal(n.dsrBar.textContent, "—");
    assert.match(n.dsrReason.textContent, /Only 2 combination/);
    assert.equal(n.dsrVerdict.textContent, "", "no verdict without a bar to judge");
    assert.equal(n.dsrFacts.innerHTML, "", "no facts to show");
});

test("a missing block still says something useful", () => {
    const n = mount();
    render(null, { sharpe: 1.42 });
    assert.match(n.dsrReason.textContent, /multiple-testing/);
});

test("the observed Sharpe still shows when the deflation does not", () => {
    const n = mount();
    render({ status: "insufficient_data", reason: "n/a" }, { sharpe: 0.77 });
    assert.equal(n.dsrObserved.textContent, "0.77");
});

// ----------------------------------------------------------------- edges
test("a missing observed Sharpe does not claim to clear or miss", () => {
    const n = mount();
    render(OK, {});
    assert.equal(n.dsrBar.textContent, "0.89");
    assert.equal(n.dsrVerdict.textContent, "", "nothing to compare against");
});

test("non-numeric metrics do not print NaN", () => {
    const n = mount();
    render(OK, { sharpe: "abc" });
    assert.equal(n.dsrObserved.textContent, "—");
    assert.doesNotMatch(n.dsrVerdict.textContent, /NaN/);
});

test("a zero bar is shown, not treated as absent", () => {
    const n = mount();
    render({ ...OK, deflated_sharpe: 0 }, { sharpe: 1.4 });
    assert.equal(n.dsrBar.textContent, "0.00");
    assert.match(n.dsrVerdict.className, /pos/);
});

console.log(`\n${passed} tests passed`);
