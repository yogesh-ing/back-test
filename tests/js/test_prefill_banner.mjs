/**
 * The setup page's prefill banner quotes the originating backtest's baseline
 * return. tune_this.js carries `total_return_pct` ALREADY in percent (the
 * adapter multiplies the engine fraction by 100); fmtPct's default
 * fraction=true would multiply it again — a −4.8% baseline read as −478%.
 * This pins the banner against the real OptCommon formatter.
 */
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const setupSrc = fs.readFileSync(
    path.join(ROOT, "src/backtest/web/static/js/optimize_setup.js"), "utf8");
const commonSrc = fs.readFileSync(
    path.join(ROOT, "src/backtest/web/static/js/optimize_common.js"), "utf8");

function lift(name) {
    const start = setupSrc.indexOf(`function ${name}`);
    assert.ok(start > 0, `${name} must exist in optimize_setup.js`);
    let depth = 0;
    let seen = false;
    for (let i = setupSrc.indexOf("{", start); i < setupSrc.length; i += 1) {
        if (setupSrc[i] === "{") { depth += 1; seen = true; }
        else if (setupSrc[i] === "}") {
            depth -= 1;
            if (seen && depth === 0) return setupSrc.slice(start, i + 1);
        }
    }
    throw new Error(`unbalanced braces lifting ${name}`);
}

const nodes = {
    optPrefillNotice: { innerHTML: "", className: "", hidden: true },
    optWfEnabled: { checked: true },
};
const sandbox = {
    console,
    $: (id) => nodes[id],
    root: { dataset: { source: "db" } },
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(commonSrc, sandbox);
assert.ok(sandbox.OptCommon, "OptCommon must be exported on globalThis");
const render = vm.runInContext(
    `(() => { ${lift("renderPrefillNotice")}\nreturn renderPrefillNotice; })()`, sandbox);

let passed = 0;
function test(name, fn) {
    try {
        fn();
        passed += 1;
    } catch (err) {
        console.error(`FAIL ${name}\n${err.stack}`);
        process.exitCode = 1;
    }
}

function banner(baselineMetrics) {
    render({
        resultId: "bt_test", engine: "driver", engineLabel: "Fill-Exact (Canonical)",
        source: "db", baselineMetrics,
        baselineFillExact: true, baselineRealData: true,
    });
    return nodes.optPrefillNotice.innerHTML;
}

test("an already-percent baseline return is shown once, not ×100", () => {
    const html = banner({ sharpe: -0.08, total_return_pct: -4.78, total_trades: 227 });
    assert.ok(html.includes("-4.8%"), `expected -4.8% in: ${html}`);
    assert.ok(!html.includes("-478"), `double-multiplied return in: ${html}`);
});

test("a positive percent return keeps its magnitude", () => {
    const html = banner({ sharpe: 1.2, total_return_pct: 12.34, total_trades: 30 });
    assert.ok(html.includes("12.3%"), `expected 12.3% in: ${html}`);
    assert.ok(!html.includes("1234"), `double-multiplied return in: ${html}`);
});

test("missing return omits the Return clause, Sharpe and trades still quote", () => {
    const html = banner({ sharpe: 0.5, total_trades: 10 });
    assert.ok(!html.includes("Return"), html);
    assert.ok(html.includes("Sharpe"), html);
    assert.ok(html.includes("10"), html);
});

console.log(`${passed} tests passed`);