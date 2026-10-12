/**
 * "Tune This" — the Backtest → Optimize hand-off (PRD backTest-enhance §6).
 *
 * §6 is a small feature with several ways to be quietly wrong, so what is
 * pinned here is the set of claims it makes:
 *
 *   • every field §6 lists is actually carried, including the ENGINE
 *   • the walk-forward split is 2/3 : 1/3, and a window too short to split is
 *     reported rather than submitted and rejected
 *   • the result id is a SESSION handle, and is labelled as one — it is not
 *     dressed up as a stored backtest record
 *   • the button is never hidden, because a weak result is the ordinary
 *     reason to want to tune
 *
 * The engine this carries data to is `quick_screen` whenever the backtest was
 * a Quick-Screen run, which is the whole point: §1.1 was about a run being
 * read on one engine and judged on another.
 *
 * Usage: node tests/js/test_tune_this.mjs
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import vm from "node:vm";
import assert from "node:assert/strict";

const root = fileURLToPath(new URL("../../", import.meta.url));
const code = readFileSync(
    path.join(root, "src/backtest/web/static/js/components/tune_this.js"), "utf8",
);

const sandbox = { console, document: { getElementById: () => null } };
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(code, sandbox, { filename: "tune_this.js" });
const { TuneThis } = sandbox;

const RUN = (over = {}) => ({
    resultId: "bt_test123",
    result: {
        config: {
            strategy: "sma_crossover", symbol: "infy",
            from_date: "2022-01-01", to_date: "2024-01-01",
            capital: 250000, timeframe: "1day", engine: "",
            params: { fast: 10, slow: 30 },
        },
        provenance: { data_source: "db" },
        readiness: { all_green: true, tune_this_available: true, red_flags: [], unproven: [] },
        ...over.result,
    },
    ...over.run,
});

let passed = 0;
function test(name, fn) {
    fn();
    passed += 1;
    console.log(`  ✓ ${name}`);
}

// ------------------------------------------------------------ every §6 field
test("carries the strategy", () => {
    assert.equal(TuneThis.buildPrefill(RUN()).strategyId, "sma_crossover");
});

test("carries the symbol, upper-cased", () => {
    assert.equal(TuneThis.buildPrefill(RUN()).symbol, "INFY");
});

test("carries the date range", () => {
    const p = TuneThis.buildPrefill(RUN());
    assert.equal(p.startDate, "2022-01-01");
    assert.equal(p.endDate, "2024-01-01");
});

test("carries the timeframe and capital", () => {
    const p = TuneThis.buildPrefill(RUN());
    assert.equal(p.timeframe, "1day");
    assert.equal(p.initialCapital, 250000);
});

test("carries the current parameters as the baseline", () => {
    assert.deepEqual(TuneThis.buildPrefill(RUN()).params, { fast: 10, slow: 30 });
});

test("defaults the objective to sharpe and the method to bayesian", () => {
    const p = TuneThis.buildPrefill(RUN());
    assert.equal(p.objectiveFunction, "sharpe");
    assert.equal(p.method, "bayesian");
});

test("carries the data source", () => {
    assert.equal(TuneThis.buildPrefill(RUN()).source, "db");
});

// ------------------------------------------------------------------ engine
test("a canonical run carries engine=driver", () => {
    assert.equal(TuneThis.buildPrefill(RUN()).engine, "driver");
});

test("a Quick-Screen run carries engine=quick_screen", () => {
    /**
     * The §1.1 bug, one hop downstream. Drop the engine and a result screened
     * on approximate fills gets tuned on the canonical driver — two engines,
     * one apparent lineage.
     */
    const run = RUN();
    run.result.config.engine = "quick_screen";
    assert.equal(TuneThis.buildPrefill(run).engine, "quick_screen");
});

test("the engine travels with a human label", () => {
    const run = RUN();
    run.result.config.engine = "quick_screen";
    assert.match(TuneThis.buildPrefill(run).engineLabel, /Quick-Screen/);
});

// ------------------------------------------------------------- walk-forward
test("walk-forward is on by default", () => {
    assert.equal(TuneThis.buildPrefill(RUN()).walkForward.enabled, true);
});

test("the split is two thirds train to one third test", () => {
    const wf = TuneThis.walkForwardFor("2023-01-01", "2024-01-01");   // 365 days
    assert.equal(wf.trainPeriodDays, 243);
    assert.equal(wf.testPeriodDays, 122);
    assert.equal(wf.trainPeriodDays + wf.testPeriodDays, 365);
});

test("the step matches the test period", () => {
    // A longer step skips bars; a shorter one re-tests the same ones.
    const wf = TuneThis.walkForwardFor("2023-01-01", "2024-01-01");
    assert.equal(wf.stepDays, wf.testPeriodDays);
});

test("a window too short to split is refused with a reason", () => {
    /**
     * Not a split the server rejects at submit time — after the user has
     * already filled the form in.
     */
    const wf = TuneThis.walkForwardFor("2023-01-01", "2023-01-07");
    assert.equal(wf.enabled, false);
    assert.match(wf.reason, /cannot be split/);
});

test("an inverted or empty date range is refused with a reason", () => {
    assert.equal(TuneThis.walkForwardFor("2024-01-01", "2023-01-01").enabled, false);
    assert.equal(TuneThis.walkForwardFor("", "").enabled, false);
});

// ------------------------------------------------------------- the result id
test("a result id is minted when the run carries none", () => {
    const run = RUN();
    delete run.resultId;
    const p = TuneThis.buildPrefill(run);
    assert.match(p.resultId, /^bt_[a-z0-9]+$/);
});

test("minted ids differ between results", () => {
    const ids = new Set(Array.from({ length: 50 }, () => TuneThis.mintResultId()));
    assert.equal(ids.size, 50);
});

test("the id names a SESSION handle, not a stored backtest record", () => {
    /**
     * Nothing about a completed backtest is persisted, so an id in an audit
     * log must not imply a record that cannot be opened. The panel says so in
     * words, and that wording is what this pins.
     */
    const run = RUN();
    delete run.resultId;
    const html = renderWith(run);
    assert.match(html, /Result ID \(this session\)/);
    assert.ok(TuneThis.buildPrefill(run).resultId);
});

// ------------------------------------------------------------------ render
function renderWith(run) {
    const el = { innerHTML: "" };
    sandbox.document.getElementById = (id) => (id === "tuneThis" ? el : null);
    TuneThis.render("tuneThis", run);
    return el.innerHTML;
}

test("an all-green result gets the primary button", () => {
    const html = renderWith(RUN());
    assert.match(html, /tuneThisBtn/);
    assert.match(html, /All eight readiness checks passed/);
    assert.match(html, /class="btn btn-primary" id="tuneThisBtn"/);
    assert.doesNotMatch(html, /tune-this--caution/);
});

test("a NOT-certifiable result still gets a clickable button", () => {
    /**
     * §5 gates the §6 button on `tune_this_available`, which reads like a
     * hard gate. It is not used as one: the ordinary reason to open Optimize
     * is that the result is weak, so disabling the button would block the
     * workflow it exists for.
     */
    const run = RUN();
    run.result.readiness = {
        all_green: false,
        tune_this_available: false,
        red_flags: ["Data source", "Trade count (8)"],
        unproven: ["Cost shock (2x)"],
    };
    const html = renderWith(run);
    assert.match(html, /tuneThisBtn/, "the button must still be there");
    assert.match(html, /not<\/strong> certifiable/);
});

test("a weak result is demoted, not hidden", () => {
    /**
     * The two states are what makes "not a gate" legible. A button styled
     * identically in both cases cannot warn that the result is not
     * certifiable; a disabled one cannot be clicked at all.
     */
    const run = RUN();
    run.result.readiness = {
        all_green: false, tune_this_available: false,
        red_flags: ["Data source"], unproven: [],
    };
    const html = renderWith(run);
    assert.match(html, /tune-this--caution/);
    assert.match(html, /class="btn btn-secondary tune-this-caution" id="tuneThisBtn"/);
    assert.doesNotMatch(html, /disabled/);
    assert.match(html, /tune-this-badge/);
});

test("the §5 flag decides the state, not the button's own opinion", () => {
    /** §5 publishes `tune_this_available`; §6 renders it. Disagreement is
     *  resolved in §5's favour so a future gate has a real switch to flip. */
    const run = RUN();
    run.result.readiness = { all_green: true, tune_this_available: false, red_flags: [] };
    assert.match(renderWith(run), /tune-this--caution/);
    run.result.readiness = { all_green: false, tune_this_available: true };
    assert.doesNotMatch(renderWith(run), /tune-this--caution/);
});

test("a missing readiness assessment is demoted, not assumed green", () => {
    /** No payload means nobody ran the checks. Claiming eight green checks
     *  that were never made is worse than a cautious button. */
    const run = RUN();
    delete run.result.readiness;
    const html = renderWith(run);
    assert.match(html, /tune-this--caution/);
    assert.match(html, /no readiness assessment/);
    assert.doesNotMatch(html, /All eight readiness checks passed/);
});

test("a weak result says Optimize will not repair it", () => {
    const run = RUN();
    run.result.readiness = {
        all_green: false, red_flags: ["Data source"], unproven: ["Cost shock (2x)"],
    };
    const html = renderWith(run);
    assert.match(html, /does not\s+fix/);
    assert.match(html, /Data source/);
});

test("the rendered panel states nothing runs until Start", () => {
    assert.match(renderWith(RUN()), /Nothing is run until you press Start/);
});

test("the rendered panel labels the id as this session", () => {
    assert.match(renderWith(RUN()), /Result ID \(this session\)/);
});

test("the rendered panel escapes strategy names", () => {
    const run = RUN();
    run.result.config.strategy = '<img src=x onerror=alert(1)>';
    const html = renderWith(run);
    assert.doesNotMatch(html, /<img/);
    assert.match(html, /&lt;img/);
});

test("an unusable result renders nothing rather than a broken shell", () => {
    assert.equal(renderWith(null), "");
    assert.equal(renderWith({}), "");
    assert.equal(TuneThis.buildPrefill({ result: { config: {} } }), null);
});

console.log(`\n${passed} tests passed`);
