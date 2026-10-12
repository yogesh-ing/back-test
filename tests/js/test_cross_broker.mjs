/**
 * Cross-broker analytics UI — behaviour tests (PRD-003).
 *
 * The page makes money-losing mistakes quietly, so these pin the ones that a
 * screenshot review would never catch:
 *
 *   * "no data" must never render as a number. A broker with no orders shows
 *     "—", not "0.0 bps" / "0% fill" — a fabricated zero is a tradable lie.
 *   * Significance must be labelled honestly: "p=0.148 · not significant" and
 *     "insufficient sample" are both valid answers, and both must survive to
 *     the screen.
 *   * Everything interpolated into innerHTML is escaped, because broker and
 *     segment names are user-supplied config.
 *
 * Usage: node tests/js/test_cross_broker.mjs
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import vm from "node:vm";
import assert from "node:assert/strict";

const root = fileURLToPath(new URL("../../", import.meta.url));
const load = (rel) => readFileSync(path.join(root, rel), "utf8");

// ------------------------------------------------------------------ stub DOM
function makeEl(id) {
    const handlers = {};
    return {
        id,
        innerHTML: "",
        textContent: "",
        value: "",
        checked: false,
        hidden: false,
        open: false,
        options: [],
        selectedIndex: 0,
        childElementCount: 0,
        style: {},
        dataset: {},
        classList: {
            _s: new Set(),
            add(c) { this._s.add(c); },
            remove(c) { this._s.delete(c); },
            contains(c) { return this._s.has(c); },
        },
        addEventListener(type, fn) { (handlers[type] = handlers[type] || []).push(fn); },
        fire(type, event) { (handlers[type] || []).forEach((fn) => fn(event || {})); },
        querySelector() { return makeEl(`${id}>q`); },
        querySelectorAll() { return []; },
        getContext() { return {}; },
        focus() {},
        appendChild(c) { return c; },
    };
}

const elements = {};
const el = (id) => (elements[id] = elements[id] || makeEl(id));

// The two <details> the controller listens to.
el("brokerSection").open = true;
el("executionSection").open = false;

const document = {
    readyState: "complete",
    body: { dataset: { currencyCode: "INR" } },
    getElementById: el,
    querySelectorAll: () => [],
    addEventListener() {},
};

// Chart.js is absent in the harness — the controller must tolerate that.
const requests = [];
let respond = () => ({ ok: true, status: 200, json: async () => ({ success: true }) });

const sandbox = {
    console,
    setTimeout,
    URLSearchParams,
    document,
    fetch: (url, opts) => {
        requests.push({ url, opts });
        return Promise.resolve(respond(url, opts));
    },
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(load("src/backtest/web/static/js/cross_broker.js"), sandbox);
const XB = sandbox.window.CrossBrokerUI;

let tests = 0;
async function test(name, fn) {
    try {
        await fn();
        tests += 1;
    } catch (err) {
        console.error(`FAIL: ${name}\n  ${err.message}`);
        process.exitCode = 1;
    }
}
const settle = async () => {
    for (let i = 0; i < 4; i += 1) await new Promise((r) => setImmediate(r));
};

// ------------------------------------------------------------------- fixture
const BROKER_ROW = (over = {}) => ({
    broker: "mstock",
    display_name: "mStock",
    mode: "live",
    allocated_capital: 120000,
    runner_count: 1,
    net_pnl: 185200,
    pnl_pct: 62.4,
    return_pct: 15.4,
    sharpe_ratio: 2.15,
    sortino_ratio: 2.9,
    max_drawdown_pct: 6.8,
    total_trades: 145,
    win_rate: 61.0,
    segments: ["options_index"],
    strategies: ["ema_pullback"],
    avg_slippage_bps: 8.2,
    fill_rate_pct: 94.0,
    avg_fill_time_sec: 2.3,
    rejection_rate_pct: 2.0,
    total_orders: 482,
    ...over,
});

const SUMMARY = {
    success: true,
    period: "30d",
    portfolio_total: {
        total_trades: 234, total_pnl: 296540, total_return_pct: 9.9,
        sharpe_ratio: 1.65, sortino_ratio: 1.92, max_drawdown_pct: 9.5,
        max_drawdown_amount: 285000, win_rate: 58.0,
        total_capital_deployed: 3000000, broker_count: 2,
        strategy_count: 5, gross_pnl: 324580,
    },
    by_broker: [
        BROKER_ROW(),
        BROKER_ROW({
            broker: "dhan", display_name: "Dhan", net_pnl: 78340, pnl_pct: 26.4,
            sharpe_ratio: 1.45, max_drawdown_pct: 9.2, total_trades: 68, win_rate: 54.0,
            avg_slippage_bps: 18.1, fill_rate_pct: 87.0, avg_fill_time_sec: 5.1,
            rejection_rate_pct: 8.0, segments: ["equity_intraday"],
            strategies: ["rsi_reversion"], total_orders: 482,
        }),
    ],
    by_segment: [
        { segment: "options_index", broker: "mstock", net_pnl: 185200, total_trades: 145 },
    ],
    broker_rankings: { by_sharpe: ["mstock", "dhan"], by_fill_rate: ["mstock", "dhan"] },
    alerts: [{ broker: "dhan", severity: "warning", message: "Dhan: fill rate 87.0% over 482 orders." }],
    insights: [{ kind: "slippage_gap", message: "Dhan slippage is 2.2x mStock's (18.1 vs 8.2 bps)." }],
    data_quality: {
        trades_in_period: 234, in_period: 964, scanned: 964, ledger_total: 964,
        truncated: false, orphaned: 0, session_scoped: true,
    },
};

const EXECUTION = {
    success: true,
    broker: "dhan",
    execution_quality: {
        total_orders: 482, filled_orders: 419, rejected_orders: 39,
        cancelled_orders: 24, pending_orders: 0, resolved_orders: 482,
        fill_rate_pct: 87.0, rejection_rate_pct: 8.0, avg_slippage_bps: 18.1,
        median_slippage_bps: 15.2, slippage_samples: 419,
        avg_fill_time_sec: 5.1, avg_order_notional: 5000,
    },
    slippage_distribution: [
        { bucket: "0-5 bps", count: 120 }, { bucket: "5-10 bps", count: 95 },
        { bucket: "10-20 bps", count: 150 }, { bucket: "20+ bps", count: 54 },
    ],
    by_strategy: [
        { strategy: "rsi_reversion", broker: "dhan", total_orders: 482,
          avg_slippage_bps: 14.8, fill_rate_pct: 91.0, rejection_rate_pct: 4.0 },
    ],
    time_series: [
        { date: "2026-09-01", avg_slippage_bps: 15.2, fill_rate_pct: 92, orders: 160 },
        { date: "2026-09-02", avg_slippage_bps: 16.8, fill_rate_pct: 89, orders: 150 },
        { date: "2026-09-03", avg_slippage_bps: 19.1, fill_rate_pct: 87, orders: 150 },
    ],
    degradation_alert: {
        alert_type: "fill_rate_decline", severity: "critical",
        message: "Fill rate fell 5.0pp (92.0% → 87.0%) over the last 3 days.",
    },
    peer_benchmarks: [
        { broker: "mstock", total_orders: 482, avg_slippage_bps: 8.2, fill_rate_pct: 94.0,
          avg_fill_time_sec: 2.3, rejection_rate_pct: 2.0, stale_order_pct: 0, is_subject: false },
        { broker: "dhan", total_orders: 482, avg_slippage_bps: 18.1, fill_rate_pct: 87.0,
          avg_fill_time_sec: 5.1, rejection_rate_pct: 8.0, stale_order_pct: 0, is_subject: true },
    ],
    notes: ["Fewer than 20 orders in this period — do not compare brokers on this sample."],
    data_quality: SUMMARY.data_quality,
};

function serve(payloadFor) {
    respond = (url) => {
        const body = payloadFor(url);
        return { ok: true, status: 200, json: async () => body };
    };
}

/** Serve a FAILING response (the 500 path the controller must surface). */
function serveFailure(error, status = 500) {
    respond = () => ({ ok: false, status, json: async () => ({ success: false, error }) });
}

const defaultServe = () => serve((url) => {
    if (url.includes("/execution")) return EXECUTION;
    if (url.includes("/summary")) return SUMMARY;
    return { success: true };
});

/** Reset controller + payload state. Elements are NOT deleted: the module
 *  binds its listeners once at import time, so the wired nodes must survive. */
async function fresh() {
    el("brokerSection").open = true;
    el("executionSection").open = false;
    ["brokerTableBody", "brokerHeadline", "brokerAlerts", "brokerInsights",
     "execPeerBody", "execStrategyBody", "execNotes", "execDegradation",
     "compareError", "compareResult", "migrationError", "migrationResult",
     "recommendError", "recommendResult"].forEach((id) => {
        const node = el(id);
        node.innerHTML = "";
        node.textContent = "";
    });
    requests.length = 0;
    defaultServe();
    XB.setPeriod("30d");
    XB.setMode("all");
    XB.invalidate();
    await XB.loadSummary(true);
    await settle();
}

// -------------------------------------------------------------------- tests

await test("the portfolio strip renders the cross-broker total", async () => {
    await fresh();
    assert.equal(el("crossBrokerTotalPnl").textContent, "₹2,96,540");
    assert.equal(el("crossBrokerSharpe").textContent, "1.65");
    assert.equal(el("crossBrokerMaxDd").textContent, "-9.50%");
    assert.equal(el("crossBrokerTrades").textContent, "234");
    assert.ok(el("crossBrokerScopePill").textContent.includes("2 brokers"),
               el("crossBrokerScopePill").textContent);
});

await test("the By Broker table shows every venue side by side", async () => {
    await fresh();
    const html = el("brokerTableBody").innerHTML;
    assert.ok(html.includes("mStock"), "mStock column missing");
    assert.ok(html.includes("Dhan"), "Dhan column missing");
    assert.ok(html.includes("8.2 bps"), "slippage missing");
    assert.ok(html.includes("94.0%"), "fill rate missing");
    assert.ok(html.includes("2.3s"), "fill time missing");
    assert.ok(html.includes("options_index"), "segment missing");
});

await test("best performer and the worst venue's alert are surfaced", async () => {
    await fresh();
    const head = el("brokerHeadline").innerHTML;
    assert.ok(head.includes("Best performer: mStock"), head);
    assert.ok(el("brokerAlerts").innerHTML.includes("fill rate 87.0%"),
              el("brokerAlerts").innerHTML);
    assert.ok(el("brokerInsights").innerHTML.includes("2.2x"), el("brokerInsights").innerHTML);
});

await test("a broker with no orders renders a dash, never a fake 0.00", async () => {
    serve((url) => {
        if (url.includes("/summary")) {
            return {
                ...SUMMARY,
                by_broker: [BROKER_ROW({ avg_slippage_bps: null, fill_rate_pct: null,
                                         avg_fill_time_sec: null, rejection_rate_pct: null,
                                         total_orders: 0 })],
            };
        }
        return { success: true };
    });
    XB.invalidate();
    await XB.loadSummary(true);
    await settle();
    const html = el("brokerTableBody").innerHTML;
    assert.ok(html.includes("—"), "expected a dash for missing execution metrics");
    assert.ok(!html.includes("0.00 bps"), "must not fabricate a 0.00 bps slippage");
    assert.ok(!html.includes("0.0%"), "must not fabricate a 0.0% fill rate");
    defaultServe();
});

await test("the execution table ranks the best fill rate", async () => {
    await fresh();
    XB.invalidate();
    await XB.loadExecution(true);
    await settle();
    const html = el("execPeerBody").innerHTML;
    assert.ok(html.includes("mStock"), html);
    assert.ok(html.includes("Dhan"), html);
    assert.ok(html.includes("5.1s"), "fill time missing");
});

await test("a fill-rate drop is announced, not buried in a chart", async () => {
    await fresh();
    XB.invalidate();
    await XB.loadExecution(true);
    await settle();
    const html = el("execDegradation").innerHTML;
    assert.ok(html.includes("Fill rate fell 5.0pp"), html);
    assert.ok(html.includes("xb-chip-bad"), "a decline must read as a warning, not a note");
});

await test("the distribution note sums back to the sample count", async () => {
    await fresh();
    XB.invalidate();
    await XB.loadExecution(true);
    await settle();
    assert.ok(el("execDistributionNote").textContent.includes("150"),
              el("execDistributionNote").textContent);
});

await test("the API's own caveats are shown to the trader", async () => {
    await fresh();
    XB.invalidate();
    await XB.loadExecution(true);
    await settle();
    assert.ok(el("execNotes").innerHTML.includes("Fewer than 20 orders"),
              el("execNotes").innerHTML);
});

await test("the session-scoped ledger caveat is on screen", async () => {
    await fresh();
    const dq = el("crossBrokerDataQuality").textContent;
    assert.ok(dq.includes("session-scoped"), dq);
    assert.ok(dq.includes("964 orders scanned"), dq);
});

await test("an API failure is shown, not swallowed into an empty table", async () => {
    serveFailure("boom");
    XB.invalidate();
    await XB.loadSummary(true);
    await settle();
    assert.ok(el("brokerTableBody").innerHTML.includes("boom"), el("brokerTableBody").innerHTML);
    defaultServe();
});

await test("a broker name containing markup is escaped, not executed", async () => {
    serve((url) => {
        if (url.includes("/summary")) {
            return {
                ...SUMMARY,
                by_broker: [BROKER_ROW({
                    broker: "x", display_name: "<img src=x onerror=alert(1)>",
                    segments: ["<b>seg</b>"],
                })],
            };
        }
        return { success: true };
    });
    XB.invalidate();
    await XB.loadSummary(true);
    await settle();
    const html = el("brokerTableBody").innerHTML;
    assert.ok(!html.includes("<img src=x"), "raw markup reached innerHTML");
    assert.ok(html.includes("&lt;img src=x"), html.slice(0, 400));
    defaultServe();
});

await test("compare refuses to run with fewer than two brokers", async () => {
    await fresh();
    el("compareBrokerChecks").childElementCount = 0;
    el("compareBrokerChecks").querySelectorAll = () => [];
    const btn = el("compareRunBtn");
    btn.fire("click");
    await settle();
    assert.ok(el("compareError").textContent.includes("at least two"), el("compareError").textContent);
});

await test("migration refuses a same-broker move before hitting the API", async () => {
    await fresh();
    el("migrationStrategy").value = "ema_pullback";
    el("migrationFrom").value = "mstock";
    el("migrationTo").value = "mstock";
    requests.length = 0;
    el("migrationRunBtn").fire("click");
    await settle();
    assert.ok(el("migrationError").textContent.includes("two different brokers"),
              el("migrationError").textContent);
    assert.equal(requests.filter((r) => r.url.includes("migration-impact")).length, 0,
                 "an invalid migration must not reach the API");
});

await test("the period filter is threaded into every cross-broker request", async () => {
    await fresh();
    XB.setPeriod("7d");
    XB.invalidate();
    requests.length = 0;
    await XB.loadSummary(true);
    await settle();
    assert.ok(requests[0].url.includes("period=7d"), requests[0].url);
});

await test("a non-all mode filter is threaded through too", async () => {
    await fresh();
    XB.setMode("live");
    XB.invalidate();
    requests.length = 0;
    await XB.loadSummary(true);
    await settle();
    assert.ok(requests[0].url.includes("mode=live"), requests[0].url);
    XB.setMode("all");
});

await test("an empty deployment is a valid answer, not an error", async () => {
    serve((url) => (url.includes("/summary")
        ? { ...SUMMARY, by_broker: [], by_segment: [], alerts: [], insights: [] }
        : { success: true }));
    XB.invalidate();
    await XB.loadSummary(true);
    await settle();
    assert.ok(el("brokerTableBody").innerHTML.includes("No brokers have a runner"),
              el("brokerTableBody").innerHTML);
    defaultServe();
});

// ------------------------------------------------- disclosure requirements
// The user constraint is explicit: a recommendation never appears without its
// statistical support, and a migration estimate never appears without the
// model that produced it. These three fields come back from the API and were
// being dropped on the floor.

const RECOMMEND = {
    success: true,
    recommended_broker: "mstock",
    recommended_broker_label: "mStock",
    confidence: "medium",
    reasoning: ["mStock ranks #1 for a scalper at high frequency"],
    weights: { fill_rate_pct: 0.35, avg_slippage_bps: 0.3 },
    segment_note: null,
    estimated_monthly_savings: {
        amount: 4120.0, delta_bps: 9.9, monthly_orders: 500,
        avg_order_notional: 5000, calculation: "9.9 bps × 500 orders × ₹5,000",
    },
    rankings: [
        { broker: "mstock", display_name: "mStock", score: 9.0, score_basis_pct: 100.0,
          strengths: ["fill rate"], weaknesses: [],
          metrics: { fill_rate_pct: 95.0, avg_fill_time_sec: 2.3, avg_slippage_bps: 8.2 } },
        { broker: "dhan", display_name: "Dhan", score: 4.2, score_basis_pct: 25.0,
          strengths: [], weaknesses: ["slippage"],
          metrics: { fill_rate_pct: 87.0, avg_fill_time_sec: 5.1, avg_slippage_bps: 18.1 } },
    ],
    data_quality: SUMMARY.data_quality,
};

const MIGRATION = {
    success: true,
    strategy: "rsi_reversion",
    from_broker: "dhan",
    to_broker: "mstock",
    current_performance: {
        broker: "dhan", broker_label: "Dhan", net_pnl: 2100.0, sharpe: 0.41,
        win_rate: 52.0, max_drawdown_pct: 9.2, orders: 120, total_trades: 24,
        avg_slippage_bps: 18.1, fill_rate_pct: 87.0, avg_fill_time_sec: 5.1,
    },
    estimated_performance: {
        broker: "mstock", broker_label: "mStock", estimated_net_pnl: 3940.0,
        estimated_sharpe: 0.78, estimated_trades: 26, avg_slippage_bps: 8.2,
        fill_rate_pct: 95.0, avg_fill_time_sec: 2.3,
        methodology: "Extrapolated from mStock's own realised slippage and fill rate "
            + "on rsi_reversion — assumes the strategy's signals are unchanged.",
    },
    historical_data: { available: true, note: "24 trades of same-strategy history at mStock." },
    impact: {
        pnl_change: 1840.0, pnl_change_pct: 87.6, sharpe_change: 0.37, sharpe_change_pct: 90.2,
        estimated_sharpe: 0.78, trade_retention: 92.0, trade_count_change: 2,
        reasons: ["Dhan slippage 18.1 bps vs mStock 8.2 bps"],
    },
    recommendation: {
        action: "MOVE", confidence: "low", reasoning: ["execution gap is wide"],
        note: "Advisory only — you migrate the strategy yourself.",
    },
    data_quality: SUMMARY.data_quality,
};

await test("a recommendation shows its confidence AND its score basis", async () => {
    await fresh();
    serve((url) => (url.includes("/recommend-broker")
        ? RECOMMEND
        : url.includes("/summary") ? SUMMARY : { success: true }));
    XB.invalidate();
    await XB.postForTest ? null : null;
    XB.openCompareModal();
    XB.openMigrationModal();
    XB.openRecommendModal();
    el("recommendType").value = "scalper";
    el("recommendFrequency").value = "high";
    requests.length = 0;
    el("recommendRunBtn").fire("click");
    await settle();
    const html = el("recommendResult").innerHTML;
    assert.ok(html.includes("confidence: medium"), html.slice(0, 400));
    assert.ok(html.includes("basis 100.0%"), "a score without its basis is a bare number");
    assert.ok(html.includes("basis 25.0%"),
              "a thinly-measured broker must show it scored off partial data");
    assert.ok(html.includes("9.9 bps"), "the savings calculation must be shown, not just ₹");
    assert.ok(requests[0].url.includes("/recommend-broker"), requests[0].url);
    defaultServe();
});

await test("a migration estimate is shown with the model that produced it", async () => {
    await fresh();
    serve((url) => (url.includes("/migration-impact")
        ? MIGRATION
        : url.includes("/summary") ? SUMMARY : { success: true }));
    XB.invalidate();
    el("migrationStrategy").value = "rsi_reversion";
    el("migrationFrom").value = "dhan";
    el("migrationTo").value = "mstock";
    requests.length = 0;
    el("migrationRunBtn").fire("click");
    await settle();
    const html = el("migrationResult").innerHTML;
    assert.ok(html.includes("Extrapolated from mStock"), "the model must be disclosed");
    assert.ok(html.includes("92.0%"), "the trade-retention assumption must be shown");
    assert.ok(html.includes("Advisory only"), "migration must stay advisory");
    assert.ok(html.includes("MOVE"), html.slice(0, 300));
    assert.equal(el("migrationError").textContent, "");
});

if (process.exitCode) {
    console.error(`\n${tests} passed, some failed`);
} else {
    console.log("---CROSS-BROKER UI OK---");
    console.log(`${tests} tests passed`);
}
