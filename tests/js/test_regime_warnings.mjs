/**
 * The regime table and the sticky warning panel — PRD backTest-enhance
 * Part 2 §6.1 and §6.2.
 *
 * The backend is pinned in tests/optimization/test_regimes.py. What is pinned
 * here is the two promises the PRD makes about the UI: that a period too short
 * for a Sharpe does not print one, and that a warning cannot be scrolled past
 * or clicked away.
 */
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const src = fs.readFileSync(
    path.join(ROOT, "src/backtest/web/static/js/optimize_run.js"), "utf8");

function lift(name) {
    const start = src.indexOf(`function ${name}`);
    assert.ok(start > 0, `${name} must exist in optimize_run.js`);
    const end = src.indexOf("\n    function ", start + 10);
    return src.slice(start, end > start ? end : undefined);
}
const body = lift("renderRegimes") + "\n" + lift("renderWarningPanel");

const nodes = {};
const $ = (id) => nodes[id];
const C = {
    isNum: (v) => typeof v === "number" && Number.isFinite(v),
    fmtNum: (v, d) => (v === null || v === undefined || v === "" ? "—" : Number(v).toFixed(d)),
    escapeHtml: (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;"),
};
const sandbox = { C, console, $ };
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
const api = vm.runInContext(
    `(() => { ${body}\nreturn { renderRegimes, renderWarningPanel }; })()`, sandbox);

function node() {
    return { textContent: "", innerHTML: "", className: "", hidden: true };
}
function mount() {
    for (const id of ["regimeBox", "warningPanel"]) nodes[id] = node();
    return nodes;
}

const REGIMES = {
    available: true, total_bars: 781, named_bars: 781, named_coverage_pct: 100.0, min_bars: 20,
    periods: [
        { label: "COVID crash", from: "2020-01-01", to: "2020-03-31", named: true, bars: 91,
          return_pct: -12.5, sharpe: -0.8, max_drawdown_pct: 18.2, trades: 4, sufficient: true },
        { label: "Recovery bull", from: "2020-04-01", to: "2021-12-31", named: true, bars: 261,
          return_pct: 15.1, sharpe: 1.68, max_drawdown_pct: 6.1, trades: 1, sufficient: true },
    ],
};

let passed = 0;
function test(name, fn) {
    try { fn(); passed += 1; process.stdout.write(`  ✓ ${name}\n`); }
    catch (e) { process.stdout.write(`  ✗ ${name}\n    ${e.message}\n`); process.exitCode = 1; }
}

// ------------------------------------------------------------ regime table
test("every period is shown with its four numbers", () => {
    const n = mount();
    api.renderRegimes(REGIMES);
    assert.match(n.regimeBox.innerHTML, /COVID crash/);
    assert.match(n.regimeBox.innerHTML, /Recovery bull/);
    assert.match(n.regimeBox.innerHTML, /-12\.50%/);
    assert.match(n.regimeBox.innerHTML, /1\.68/);
    assert.match(n.regimeBox.innerHTML, /18\.20%/);
    assert.match(n.regimeBox.innerHTML, />4</);
});

test("a period too short for a Sharpe prints a dash, not a number", () => {
    /** The number would be there and it would be large, which reads as "this
     *  period was better" when the truth is "too short to say". */
    const n = mount();
    const short = { ...REGIMES, periods: [{ ...REGIMES.periods[0], bars: 6, sufficient: false, sharpe: 9.1 }] };
    api.renderRegimes(short);
    assert.doesNotMatch(n.regimeBox.innerHTML, /9\.1/);
    assert.match(n.regimeBox.innerHTML, /too few bars/);
});

test("an untraded period shows a dash rather than zero trades", () => {
    const n = mount();
    api.renderRegimes({ ...REGIMES, periods: [{ ...REGIMES.periods[0], trades: null }] });
    assert.match(n.regimeBox.innerHTML, /—/);
});

test("partial coverage is stated in the table", () => {
    /** A run outside 2020-2024 must not look fully covered. */
    const n = mount();
    api.renderRegimes({ ...REGIMES, named_coverage_pct: 12.5,
        periods: [{ ...REGIMES.periods[0], named: false, from: null, to: null }] });
    assert.match(n.regimeBox.innerHTML, /only\s+12\.5% of this run's bars/);
    assert.match(n.regimeBox.innerHTML, /outside the named bands/);
});

test("full coverage does not claim partial coverage", () => {
    const n = mount();
    api.renderRegimes(REGIMES);
    assert.doesNotMatch(n.regimeBox.innerHTML, /cover only/);
});

test("a positive return is marked differently from a negative one", () => {
    const n = mount();
    api.renderRegimes(REGIMES);
    const html = n.regimeBox.innerHTML;
    assert.match(html, /class="neg">-12\.50%/);
    assert.match(html, /class="pos">15\.10%/);
});

test("an absent breakdown hides the card rather than showing an empty table", () => {
    for (const empty of [null, undefined, { available: false }, { available: true, periods: [] }]) {
        const n = mount();
        api.renderRegimes(empty);
        assert.equal(n.regimeBox.hidden, true, JSON.stringify(empty));
        assert.equal(n.regimeBox.innerHTML, "");
    }
});

// ------------------------------------------------------------ warning panel
test("warnings are counted in the title", () => {
    const n = mount();
    api.renderWarningPanel([{ level: "warning", code: "a", message: "A" },
                            { level: "warning", code: "b", message: "B" }]);
    assert.match(n.warningPanel.innerHTML, /2 warning signs/);
});

test("a single warning is not pluralised", () => {
    const n = mount();
    api.renderWarningPanel([{ level: "warning", code: "a", message: "A" }]);
    assert.match(n.warningPanel.innerHTML, /1 warning sign</);
    assert.doesNotMatch(n.warningPanel.innerHTML, /signs</);
});

test("a danger among warnings is counted out and escalates the panel", () => {
    const n = mount();
    api.renderWarningPanel([{ level: "warning", code: "a", message: "A" },
                            { level: "danger", code: "b", message: "B" }]);
    assert.match(n.warningPanel.className, /danger/);
    assert.match(n.warningPanel.innerHTML, /1 of 2 warning signs need attention/);
});

test("the panel cannot be dismissed", () => {
    /** §6.2's whole complaint is that the warnings were easy to miss. A close
     *  button would be the layout's answer to that. */
    const n = mount();
    api.renderWarningPanel([{ level: "danger", code: "a", message: "A" }]);
    assert.doesNotMatch(n.warningPanel.innerHTML, /data-close|✕|dismiss/i);
});

test("no warnings hides the panel instead of saying 'none'", () => {
    const n = mount();
    api.renderWarningPanel([]);
    assert.equal(n.warningPanel.hidden, true);
    api.renderWarningPanel(null);
    assert.equal(n.warningPanel.hidden, true);
});

test("messages are escaped", () => {
    const n = mount();
    api.renderWarningPanel([{ level: "warning", code: "a", message: "<img src=x onerror=1>" }]);
    assert.doesNotMatch(n.warningPanel.innerHTML, /<img/);
    assert.match(n.warningPanel.innerHTML, /&lt;img/);
});

console.log(`\n${passed} tests passed`);
