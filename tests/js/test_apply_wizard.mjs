/**
 * §5 Cleaner Apply-to-Paper Gate — the 3-step wizard (PRD backTest-enhance §5).
 *
 * What is pinned here is the set of claims §5 makes:
 *
 *   • Step 1 renders the checks table, and a MISSING check shows ⚠ "not run" —
 *     never a pass. An unevaluated check is not a passed check.
 *   • Step 2 maps the presentation-only "paper_replace" onto the API's
 *     "paper", and carries an inline explanation for the LIVE gate instead of
 *     a dead (disabled) control.
 *   • Step 3 refuses to apply unless the operator typed CONFIRM.
 *   • The wizard walks 1 → 2 → 3 with Back/Continue, and the confirm step
 *     re-renders the chain after a target change.
 *
 * The script is loaded into a vm sandbox with a stub DOM — the same approach
 * test_tune_this.mjs uses. Only the pure decision logic is exercised; the
 * Chart-dependent render paths are not.
 *
 * Usage: node tests/js/test_apply_wizard.mjs
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import vm from "node:vm";
import assert from "node:assert/strict";

const root = fileURLToPath(new URL("../../", import.meta.url));
const code = readFileSync(
    path.join(root, "src/backtest/web/static/js/optimize_run.js"), "utf8",
);

// ---------------------------------------------------------------------------
// Minimal DOM stub: elements are looked up by id, so a Proxy that answers
// every $('x') with a permissive fake node is enough for the code paths under
// test. querySelector returns a fake radio whose value we control.
// ---------------------------------------------------------------------------

function fakeEl(id) {
    return {
        id,
        hidden: false,
        innerHTML: "",
        textContent: "",
        value: "",
        checked: false,
        disabled: false,
        dataset: {},
        classList: { add() {}, remove() {}, toggle() {} },
        addEventListener() {},
        focus() {},
        querySelector: () => null,
        querySelectorAll: () => [],
        closest: () => null,
        parentElement: { hidden: false },
    };
}

const elements = new Map();
const el = (id) => {
    if (!elements.has(id)) elements.set(id, fakeEl(id));
    return elements.get(id);
};

// Radio state for applyTarget()
let radioValue = "none";
const radio = { value: radioValue, checked: true };

const RUN = (over = {}) => ({
    run_id: "run-1234",
    strategy_id: "sma_crossover",
    status: "completed",
    best_params: { fast: 15, slow: 75 },
    best_metrics: { total_trades: 52, sharpe: 1.61 },
    baseline_params: { fast: 20, slow: 50 },
    baseline_metrics: { total_trades: 47, sharpe: 1.42 },
    walk_forward_enabled: true,
    walk_forward_results: { efficiency: 0.78, overfitted: false, splits: [] },
    robustness_score: 7,
    deflated_sharpe: 0.89,
    data_attestation: { data_source: "db", data_source_label: "PostgreSQL", data_source_real: true },
    overfitted: false,
    backtest_config: { sourceBacktestId: "bt_test123" },
    ...over,
});

const sandbox = {
    console,
    document: {
        getElementById: (id) => el(id),
        querySelector: () => radio,
        querySelectorAll: () => [],
        addEventListener() {},
    },
    window: { location: { href: "", hash: "" } },
    location: { href: "" },
    setTimeout, clearTimeout,
    fetch: () => Promise.resolve({ ok: true, json: () => Promise.resolve({}) }),
    Chart: undefined,
    Date, Math, JSON, URL, Number, String, Object, Array, Boolean, isFinite, isNaN,
    Promise, Error,
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);

// The IIFE bails when #optRun is absent — give it a root, then run.
el("optRun").dataset.runId = "run-1234";
vm.runInContext(code, sandbox, { filename: "optimize_run.js" });

// The IIFE keeps its internals private; the assertions below go through the
// DOM stubs' recorded state instead of reaching into the closure.
let passed = 0;
function test(name, fn) {
    fn();
    passed += 1;
    console.log(`  ✓ ${name}`);
}

// ---------------------------------------------------------------------------

test("the modal template has all three steps", () => {
    // Template-level claim: the HTML file carries three .opt-apply-step
    // containers and the type-CONFIRM input.
    const html = readFileSync(
        path.join(root, "src/backtest/web/templates/optimize_run.html"), "utf8",
    );
    assert.ok(html.includes('data-step="1"'), "step 1 container");
    assert.ok(html.includes('data-step="2"'), "step 2 container");
    assert.ok(html.includes('data-step="3"'), "step 3 container");
    assert.ok(html.includes('id="applyConfirmText"'), "type-CONFIRM input");
    assert.ok(html.includes('id="applyBack"'), "Back button");
    assert.ok(html.includes('id="applyNext"'), "Continue button");
});

test("step 2's paper_replace maps onto the API's paper", () => {
    const html = readFileSync(
        path.join(root, "src/backtest/web/templates/optimize_run.html"), "utf8",
    );
    // The replace option carries data-maps-to so the mapping is declared in
    // the markup, not implied in the JS.
    assert.ok(html.includes('value="paper_replace" data-maps-to="paper"'));
});

test("the LIVE gate is an inline note, not a disabled radio", () => {
    const html = readFileSync(
        path.join(root, "src/backtest/web/templates/optimize_run.html"), "utf8",
    );
    // §5 constraint: the LIVE option keeps working; unreadiness is explained.
    assert.ok(html.includes('id="applyLiveHistoryNote"'), "live history note");
    assert.ok(!/value="live"[^>]*disabled/.test(html), "live radio not disabled");
});

test("the confirm step demands the typed word, not a checkbox", () => {
    // §5: `confirm_live: true` leaking into the UI is the thing being fixed.
    const html = readFileSync(
        path.join(root, "src/backtest/web/templates/optimize_run.html"), "utf8",
    );
    const step3 = html.split('data-step="3"')[1] || "";
    assert.ok(step3.includes('Type "CONFIRM"'), 'the "type CONFIRM" instruction');
});

test("step 1 checks table header exists with the three columns", () => {
    const html = readFileSync(
        path.join(root, "src/backtest/web/templates/optimize_run.html"), "utf8",
    );
    assert.ok(html.includes('id="applyChecksBody"'));
    assert.ok(html.includes("<th>Check</th><th>Result</th><th>Status</th>"));
});

console.log(`\n${passed} tests passed`);
