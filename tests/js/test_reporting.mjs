/**
 * Consolidated P&L page (reporting.js) — view-model behaviour.
 *
 * The page's reason to exist is that a trader can tell *what is left* apart
 * from *what was made*, and can see which caveats apply to the number. So the
 * things pinned here are the ones that would silently mislead:
 *
 *   • an estimated fee stack is marked as estimated in the ledger and counted
 *     in the ladder, never presented as observed;
 *   • paper money is labelled as excluded rather than quietly added to income;
 *   • a loss carries forward instead of showing as a negative tax;
 *   • the reconciliation verdict uses words, not just a number;
 *   • the demo book is announced before any figure is read.
 *
 * Usage: node tests/js/test_reporting.mjs
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import vm from "node:vm";
import assert from "node:assert/strict";

const root = fileURLToPath(new URL("../../", import.meta.url));
const code = readFileSync(
    path.join(root, "src/backtest/web/static/js/reporting.js"), "utf8",
);

// ------------------------------------------------------------------ tiny DOM
function makeEl(id) {
    return {
        id, innerHTML: "", value: "", checked: false,
        children: [],
        querySelector() { return makeEl(`${id}::child`); },
        querySelectorAll() { return []; },
        addEventListener() {},
    };
}
const elements = {};
const el = (id) => (elements[id] = elements[id] || makeEl(id));

const sandbox = {
    console,
    document: {
        getElementById: (id) => el(id),
        createElement: () => makeEl("created"),
        querySelectorAll: () => [],
        body: { appendChild() {}, removeChild() {} },
    },
    fetch: () => Promise.resolve({ ok: false, status: 500, json: () => Promise.resolve({}) }),
    URL: { createObjectURL: () => "blob:x", revokeObjectURL() {} },
    URLSearchParams, JSON, Number, String, Math, Object, Boolean,
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(`${code}\n;globalThis.Reporting = Reporting;`, sandbox,
                { filename: "reporting.js" });

const Reporting = sandbox.Reporting;

let passed = 0;
function test(name, fn) {
    fn();
    passed += 1;
    console.log(`  ✓ ${name}`);
}

// ------------------------------------------------------------------- fixtures
const report = {
    demo: true,
    period: { start: "2026-04-01", end: "2026-09-30", label: "01-Apr-2026 → 30-Sep-2026" },
    summary: {
        gross_pnl: 7619.5, fees: 480.25, slippage: 0, total_costs: 480.25,
        net_pnl: 7139.25, tax: 357.85, net_after_tax: 6781.4, cost_drag_pct: 11.0,
    },
    fee_rows: [
        { key: "brokerage", label: "Brokerage", amount: 300.0 },
        { key: "stt", label: "STT / CTT", amount: 180.25 },
    ],
    stats: { trade_count: 9, win_rate: 0.6667, winners: 6, losers: 3 },
    cost_basis: { recorded: 3, estimated: 6, none: 0 },
    by_broker: [
        { broker: "broker_c", trades: 2, net_pnl: 3962.0, share_pct: 55.5, modes: ["paper"], estimated_fee_trades: 0 },
        { broker: "dhan", trades: 3, net_pnl: 1581.22, share_pct: 22.2, modes: ["live"], estimated_fee_trades: 2 },
    ],
    by_category: [
        {
            category: "PAPER_EXCLUDED", label: "Paper / simulated (not taxable)",
            treatment: "Simulated money — never enters a return", loss_rule: "n/a",
            schedule: "— (excluded)", net_pnl: 5582.0, rate: 0.0, estimated_tax: 0.0, trades: 3,
        },
        {
            category: "EQUITY_STCG", label: "Equity delivery ≤12m (STCG)",
            treatment: "Capital gains", loss_rule: "Carry forward 8 years",
            schedule: "Schedule CG — A3", net_pnl: 1463.9, rate: 0.2, estimated_tax: 308.89, trades: 1,
        },
    ],
    tax: {
        total: 357.85, subtotal: 344.09, cess: 13.76, surcharge: 0.0,
        ltcg_exemption_used: 0.0,
        carry_forward: [
            { category: "FNO_NON_SPECULATIVE", label: "F&O (non-speculative business)",
              amount: 23.97, rule: "Set off against any income except salary; carry forward 8 years",
              its_schedule: "ITR-3 Schedule BP" },
        ],
        notes: ["Business-income tax is a slab estimate, not a rule."],
        rules: { stcg_rate: "0.2", ltcg_rate: "0.125", ltcg_exemption: "125000",
                 business_slab_rate: "0.3", disclaimer: "Estimate only — verify with a chartered accountant." },
    },
    warnings: ["6 of 9 trades carry an ESTIMATED fee stack — the cost line is modelled, not observed"],
    data_notes: ["merged 1 duplicate trade"],
    sources: [{ kind: "demo", name: "demo", note: "9 demo trade(s)" }],
    trades: [
        { trade_id: "t1", exit_date: "2026-09-30", broker: "dhan", mode: "live",
          symbol: "INFY", quantity: 25, gross_pnl: 1568.75, fees: 104.85, net_pnl: 1463.9,
          tax_category_label: "Equity delivery ≤12m (STCG)", fees_basis: "estimated", tag: "SIMULATED [demo]" },
        { trade_id: "t2", exit_date: "2026-09-30", broker: "mstock", mode: "live",
          symbol: "NIFTY26OCT24800CE", quantity: 75, gross_pnl: -1672.5, fees: 64.97,
          net_pnl: -1737.47, tax_category_label: "F&O (non-speculative business)",
          fees_basis: "recorded", tag: "" },
    ],
};

const reconcile = {
    status: "WARNING", difference: 42.5, difference_pct: 0.4,
    platform_pnl: 1581.22, broker_pnl: 1538.72, trades_compared: 3,
    component_breakdown: {
        stt: { platform: 60.0, broker: 20.0, difference: 40.0 },
    },
    likely_causes: ["stt: platform ₹60.00 vs note ₹20.00 (+40.00)"],
    warnings: [],
};

// ---------------------------------------------------------------------- tests
test("money() formats Indian digit grouping and keeps the sign", () => {
    assert.equal(Reporting.money(7139.25), "₹7,139.25");
    assert.equal(Reporting.money(-1737.47), "-₹1,737.47");
    assert.equal(Reporting.money(125000), "₹1,25,000.00");
    assert.equal(Reporting.money(undefined), "₹0.00");
});

test("the ladder subtracts every fee row from gross", () => {
    const model = Reporting.summaryModel(report);
    const labels = model.rows.map((row) => row.label);
    // (vm realms have their own Array prototype, so compare contents, not identity)
    assert.equal(JSON.stringify(labels), JSON.stringify(["Gross P&L", "Brokerage", "STT / CTT"]));
    assert.equal(model.rows[1].amount, -300.0);
    assert.equal(model.net, 7139.25);
    assert.equal(model.netAfterTax, 6781.4);
});

test("estimated fee stacks are counted, not hidden", () => {
    const model = Reporting.summaryModel(report);
    assert.equal(model.estimated, 6);
    const html = Reporting.summaryHtml(report);
    assert.match(html, /ESTIMATED fee stacks/);
    assert.match(html, /9 closed trades/);
});

test("paper trades are labelled as excluded from tax", () => {
    const rows = Reporting.taxModel(report);
    const paper = rows.find((row) => row.key === "PAPER_EXCLUDED");
    assert.equal(paper.tax, 0);
    assert.match(paper.treatment, /never enters a return/);
    const html = Reporting.taxHtml(report);
    assert.match(html, /Paper \/ simulated/);
    assert.match(html, /Schedule CG — A3/);
});

test("a loss is shown as a carry-forward, never as negative tax", () => {
    const html = Reporting.taxHtml(report);
    assert.match(html, /Losses carried forward/);
    assert.match(html, /₹23.97/);
    assert.ok(!/\-₹/.test(html.split("Losses carried forward")[1].slice(0, 200)));
});

test("the disclaimer reaches the page", () => {
    const html = Reporting.caveatsHtml(report);
    assert.match(html, /chartered accountant/);
});

test("warnings and source provenance are both visible", () => {
    const html = Reporting.caveatsHtml(report);
    assert.match(html, /ESTIMATED fee stack/);
    assert.match(html, /merged 1 duplicate trade/);
    assert.match(html, /demo — 9 demo trade\(s\)/);
});

test("broker rows carry their share of the profit", () => {
    const rows = Reporting.brokerModel(report);
    assert.equal(rows[0].broker, "broker_c");
    assert.equal(rows[0].share, 55.5);
    const html = Reporting.brokerHtml(report);
    assert.match(html, /55\.5%/);
    assert.match(html, /paper/);
});

test("the ledger marks estimated rows with an asterisk", () => {
    const model = Reporting.ledgerModel(report);
    assert.equal(model.total, 2);
    assert.equal(model.shown, 2);
    const html = Reporting.ledgerHtml(report);
    assert.match(html, /estimated fees/);
    assert.match(html, /SIMULATED \[demo\]/);
    assert.match(html, /NIFTY26OCT24800CE/);
});

test("an empty book renders an explanation, not an empty table", () => {
    const html = Reporting.ledgerHtml({ trades: [] });
    assert.match(html, /No closed trades in this period/);
});

test("a reconciliation verdict is stated in words", () => {
    const pass = Reporting.reconcileModel({ status: "PASS", difference: 0 });
    assert.match(pass.headline, /rounding tolerance/);
    const fail = Reporting.reconcileModel({ status: "FAIL", difference: 900 });
    assert.match(fail.headline, /do not file/);
    const warn = Reporting.reconcileModel(reconcile);
    assert.equal(warn.components[0].key, "stt");
    assert.equal(warn.components[0].difference, 40.0);
    const html = Reporting.reconcileHtml(reconcile);
    assert.match(html, /WARNING/);
    assert.match(html, /stt: platform/);
});

test("the demo banner is rendered before the numbers", () => {
    Reporting.render(report);
    assert.match(el("rp-banner").innerHTML, /DEMO BOOK/);
    assert.match(el("rp-summary").innerHTML, /Net after tax/);
    assert.match(el("rp-tax-block").innerHTML, /Tax categorisation/);
});

test("html is escaped — a symbol from the market is still untrusted input", () => {
    const nasty = {
        trades: [{ exit_date: "2026-09-30", broker: "x", symbol: "<img src=x onerror=alert(1)>",
                   quantity: 1, gross_pnl: 0, fees: 0, net_pnl: 0,
                   tax_category_label: "<script>", fees_basis: "recorded", tag: "" }],
    };
    const html = Reporting.ledgerHtml(nasty);
    assert.ok(!html.includes("<img"), "raw tag leaked into the page");
    assert.match(html, /&lt;img/);
});

console.log(`\n${passed} tests passed`);
