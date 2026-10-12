/**
 * Single-run checks — behaviour tests (PRD backTest-enhance §3).
 *
 * The Python side owns the arithmetic; what is pinned HERE is the three ways
 * a diagnostics panel can lie while looking perfectly healthy:
 *
 *   1. an absent check rendering as an absent panel, which reads as a pass;
 *   2. a "Base (0.05%)" column implying the run was costed when it was
 *      frictionless;
 *   3. the Monte Carlo presenting the reorder block's invariant final equity
 *      as if it were a distribution.
 *
 * Usage: node tests/js/test_run_checks.mjs
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import vm from "node:vm";
import assert from "node:assert/strict";

const root = fileURLToPath(new URL("../../", import.meta.url));
const code = readFileSync(
    path.join(root, "src/backtest/web/static/js/components/run_checks.js"), "utf8",
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

const sandbox = { console, document: { getElementById: el }, String, Number, Array, Math, isFinite };
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(code, sandbox, { filename: "run_checks.js" });

const { RunChecks } = sandbox;

// ------------------------------------------------------------- fixtures
function benchmark(over = {}) {
    return {
        label: "Buy & Hold", available: true,
        total_return: 0.2663, cagr: 0.079, sharpe: 0.57, max_drawdown: -0.2206,
        final_equity: 126634.9, alpha: 0.0138, beta: 0.5547, aligned_bars: 782,
        ...over,
    };
}

function costShock(over = {}) {
    return {
        available: true, base_bps: 5.0, base_bps_source: "default",
        base_bps_note: "Run was frictionless, so the stress base is the 5 bps NSE default.",
        status: "robust",
        warning: { level: "info", message: "Edge survives 3x slippage." },
        scenarios: [
            { multiple: 1, label: "Base", slippage_pct: 0.05, total_return_pct: 24.81, sharpe: 0.68, profitable: true, trades_dropped: 0, closed_trades: 13 },
            { multiple: 2, label: "2x Slippage", slippage_pct: 0.1, total_return_pct: 21.67, sharpe: 0.6, profitable: true, trades_dropped: 0, closed_trades: 13 },
            { multiple: 3, label: "3x Slippage", slippage_pct: 0.15, total_return_pct: 18.62, sharpe: 0.53, profitable: true, trades_dropped: 0, closed_trades: 13 },
        ],
        actual: { slippage_bps: 0.0, total_return_pct: 28.02, sharpe: 0.75, closed_trades: 13 },
        ...over,
    };
}

function monteCarlo(over = {}) {
    return {
        available: true, simulations: 1000, trades: 13, starting_capital: 100000.0,
        actual_final_equity: 128018.61,
        drawdown_note: "Drawdowns here are measured between trade boundaries.",
        concentration: { gross_profit: 1320, gross_loss: 520, best_trade: 200, best_trade_share_pct: 15.2, net: 800 },
        reorder: {
            median_final_equity: 128018.61, p5_final_equity: 128018.61, p95_final_equity: 128018.61,
            median_max_drawdown_pct: 0.11, p95_max_drawdown_pct: 0.18,
            profit_probability_pct: 100.0, final_equity_is_invariant: true, actual_percentile: 50.0,
        },
        bootstrap: {
            median_final_equity: 129347.29, p5_final_equity: 87491.95, p95_final_equity: 168927.04,
            median_max_drawdown_pct: 0.1, p95_max_drawdown_pct: 0.26,
            profit_probability_pct: 87.6, final_equity_is_invariant: false, actual_percentile: 48.0,
        },
        warnings: [],
        ...over,
    };
}

function payload(over = {}) {
    return {
        metrics: {
            total_return_pct: 28.02, sharpe: 0.75, max_drawdown_pct: -11.7,
            trade_count_flag: "ok",
        },
        benchmark: benchmark(),
        cost_shock: costShock(),
        monte_carlo: monteCarlo(),
        ...over,
    };
}

let passed = 0;
function test(name, fn) {
    fn();
    passed += 1;
    console.log(`  ✓ ${name}`);
}

const render = (id, p) => { RunChecks.renderInto(id, p); return el(id).innerHTML; };

// ------------------------------------------------------------------ panels
test("renders all three panels the PRD names", () => {
    const html = render("a", payload());
    assert.match(html, /Benchmark — vs Buy &amp; Hold/);
    assert.match(html, /Cost Shock/);
    assert.match(html, /Monte Carlo/);
    assert.equal((html.match(/<details/g) || []).length, 3);
});

test("panels start collapsed — they qualify the numbers, they do not bury them", () => {
    const html = render("b", payload());
    assert.ok(!/<details[^>]*\bopen\b/.test(html), "no panel may start open");
});

test("does not touch the metric cards or the §2 sections", () => {
    render("runChecks", payload());
    assert.equal(elements["metricsCards"], undefined);
    assert.equal(elements["metricSections"], undefined);
});

// ------------------------------------------------------------- benchmark
test("shows strategy and benchmark side by side, plus alpha and beta", () => {
    const html = render("c", payload());
    assert.match(html, /Strategy return/);
    assert.match(html, /Buy &amp; Hold return/);
    assert.match(html, /Alpha/);
    assert.match(html, /Beta/);
    assert.match(html, /26\.63%/, "benchmark return");
    assert.match(html, /1\.38%/, "alpha as 1.38%");
    assert.match(html, /0\.55/, "beta");
});

test("says plainly when the strategy LOST to buy-and-hold", () => {
    const html = render("d", payload({ benchmark: benchmark({ alpha: -0.08 }) }));
    assert.match(html, /Buy-and-hold matched or beat the strategy/);
    assert.match(html, /runcheck-warn/);
});

test("labels alpha as simple excess return, not the regression intercept", () => {
    const html = render("e", payload());
    assert.match(html, /not the regression intercept/);
});

test("an unavailable benchmark states why instead of vanishing", () => {
    const html = render("f", payload({
        benchmark: { available: false, reason: "no candles for this symbol" },
    }));
    assert.match(html, /no candles for this symbol/);
    assert.match(html, /runcheck-unavailable/);
});

// ------------------------------------------------------------- cost shock
test("renders the 1x / 2x / 3x table with return, Sharpe and profitability", () => {
    const html = render("g", payload());
    assert.match(html, /Cost Scenario/);
    assert.match(html, /2x Slippage/);
    assert.match(html, /3x Slippage/);
    assert.match(html, /24\.81%/);
    assert.match(html, /Total Return/);
    assert.match(html, /Profitable\?/);
});

test("a frictionless run does NOT render as a plain 'Base (0.05%)' column", () => {
    // The single most misleading thing this panel could do: imply the run
    // above was costed when it was frictionless.
    const html = render("h", payload());
    assert.match(html, /frictionless/);
    assert.match(html, /5 bps NSE default/);
    assert.match(html, /0\.00% slippage/, "and says what the run above actually used");
});

test("a configured slippage base is not labelled frictionless", () => {
    const html = render("i", payload({
        cost_shock: costShock({ base_bps_source: "configured", base_bps: 7.0 }),
    }));
    assert.ok(!/frictionless/.test(html));
});

test("a broken edge renders red and states the 2x rule", () => {
    const html = render("j", payload({
        cost_shock: costShock({
            status: "broken",
            warning: { level: "error", message: "Edge disappears at 2x slippage." },
        }),
    }));
    assert.match(html, /runcheck-error/);
    assert.match(html, /Edge disappears at 2x slippage/);
    assert.match(html, /role="alert"/);
});

test("a fragile edge renders yellow, a robust one green", () => {
    assert.match(render("k", payload({ cost_shock: costShock({ status: "fragile" }) })), /runcheck-warn/);
    assert.match(render("k", payload({ cost_shock: costShock({ status: "robust" }) })), /runcheck-ok/);
});

test("a scenario that could no longer fund its trades says so", () => {
    // Trades vanishing is a SIZING limit, not a thinner edge — reading it as
    // "the edge died" is the wrong lesson.
    const cs = costShock();
    cs.scenarios[2].trades_dropped = 6;
    const html = render("l", payload({ cost_shock: cs }));
    assert.match(html, /6 fewer trades/);
});

test("an unprofitable scenario is visibly marked, not left to the reader", () => {
    const cs = costShock();
    cs.scenarios[2].total_return_pct = -4.2;
    cs.scenarios[2].profitable = false;
    const html = render("m", payload({ cost_shock: cs }));
    assert.match(html, /-4\.20%/);
    assert.match(html, /runcheck-no/);
    assert.match(html, /class="num neg"/);
});

test("quick-screen's absent cost shock is stated, not silently dropped", () => {
    const html = render("n", payload({
        cost_shock: { available: false, reason: "cost shock runs on the canonical engine, not Fast Preview" },
    }));
    assert.match(html, /canonical engine, not Fast Preview/);
});

test("an empty scenario list degrades to a reason, not an empty table", () => {
    const html = render("o", payload({ cost_shock: { available: true, scenarios: [] } }));
    assert.ok(!html.includes("<table"));
    assert.match(html, /runcheck-unavailable/);
});

// ------------------------------------------------------------- monte carlo
test("the final-equity spread comes from the bootstrap, not the reorder", () => {
    // Under a reorder the final equity is invariant, so showing its 5th/95th
    // percentiles would put three identical numbers in a "distribution".
    const html = render("p", payload());
    assert.match(html, /129,?347|129347/, "bootstrap median");
    assert.match(html, /87,?491|87491/, "bootstrap 5th percentile");
    assert.match(html, /168,?927|168927/, "bootstrap 95th percentile");
});

test("the reorder block is presented as path-only, and says why", () => {
    const html = render("q", payload());
    assert.match(html, /Same trades, different order/);
    assert.match(html, /a shuffle does not change a sum/);
});

test("the drawdown rows come from the reorder block", () => {
    const html = render("r", payload());
    assert.match(html, /Drawdown, median ordering/);
    assert.match(html, /Drawdown, worst 5% of orderings/);
    assert.match(html, /0\.18%/);
});

test("shows the share of profit resting on the single best trade", () => {
    const html = render("s", payload());
    // The apostrophe is HTML-escaped by esc(), so match the escaped form.
    assert.match(html, /Best trade&#39;s share of profit/);
    assert.match(html, /15\.20%/);
});

test("carries the server's Monte Carlo warnings through", () => {
    const html = render("t", payload({
        monte_carlo: monteCarlo({
            concentration: { best_trade_share_pct: 92.4 },
            warnings: [{ level: "warning", code: "profit_concentrated",
                         message: "One trade produced 92% of gross profit." }],
        }),
    }));
    assert.match(html, /One trade produced 92% of gross profit/);
    assert.match(html, /92\.40%/);
});

test("the insufficient-trade banner appears across the whole panel", () => {
    const html = render("u", payload({
        metrics: { total_return_pct: 3.1, sharpe: 0.4, max_drawdown_pct: -2, trade_count_flag: "insufficient" },
    }));
    assert.match(html, /Fewer than 20 closed trades/);
    assert.match(html, /role="alert"/);
});

test("ok and warn raise no banner", () => {
    assert.ok(!render("v", payload()).includes("Fewer than 20 closed trades"));
    assert.ok(!render("v", payload({
        metrics: { trade_count_flag: "warn" },
    })).includes("Fewer than 20 closed trades"));
});

test("an unavailable Monte Carlo states why", () => {
    const html = render("w", payload({
        monte_carlo: { available: false, reason: "only 1 closed trade — there is no sequence to resample" },
    }));
    assert.match(html, /no sequence to resample/);
});

// ------------------------------------------------------------------ honesty
test("no metrics at all renders an empty container (page has not run yet)", () => {
    assert.equal(el("x").innerHTML, "", "");
    RunChecks.renderInto("x", null);
    assert.equal(el("x").innerHTML, "");
    RunChecks.renderInto("x", undefined);
    assert.equal(el("x").innerHTML, "");
});

test("all three checks missing still renders the panels, each saying why", () => {
    const html = render("y", payload({
        benchmark: { available: false, reason: "no benchmark" },
        cost_shock: { available: false, reason: "no cost shock" },
        monte_carlo: { available: false, reason: "no monte carlo" },
    }));
    assert.equal((html.match(/runcheck-unavailable/g) || []).length, 3);
    // A silent panel would read as a panel that passed.
    assert.equal((html.match(/<details/g) || []).length, 3);
});

test("server strings carrying markup are escaped, never injected", () => {
    const html = render("z", payload({
        cost_shock: costShock({
            warning: { level: "error", message: "<img src=x onerror=alert(1)>" },
        }),
    }));
    assert.ok(!html.includes("<img"), "raw markup must not survive into the DOM");
    assert.match(html, /&lt;img/);
});

test("renderInto resolves the container by id; a missing one is a no-op", () => {
    RunChecks.renderInto("does-not-exist", payload());  // must not throw
    assert.ok(RunChecks.STATUS_TEXT.robust);
});

console.log(`\nsingle-run checks: ${passed} tests passed`);
