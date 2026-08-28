/**
 * Metrics cards + trade table components — behaviour tests (gaps G1/G2).
 *
 * These two components are where "Trades" and "Win Rate" become visible, so they
 * are pinned here: a run with nothing closed must show "—" (not a misleading
 * 0.00%), an open trade must be labelled Open rather than ✅/❌, and the card
 * count must equal the number of rows it is summarising.
 *
 * Usage: node tests/js/test_metrics_cards.mjs
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import vm from "node:vm";
import assert from "node:assert/strict";

const root = fileURLToPath(new URL("../../", import.meta.url));
const cardsCode = readFileSync(
    path.join(root, "src/backtest/web/static/js/components/metrics_cards.js"), "utf8",
);
const tableCode = readFileSync(
    path.join(root, "src/backtest/web/static/js/components/trade_table.js"), "utf8",
);
const currencyCode = readFileSync(
    path.join(root, "src/backtest/web/static/js/components/currency.js"), "utf8",
);

// ------------------------------------------------------------------ tiny DOM
function makeEl(id) {
    const el = {
        id, innerHTML: "", className: "", hidden: false,
        children: [], style: {}, dataset: {},
        querySelector(sel) {
            const key = `${id}::${sel}`;
            this.children = this.children || [];
            if (!this._q) this._q = {};
            if (!this._q[sel]) this._q[sel] = makeEl(key);
            return this._q[sel];
        },
        querySelectorAll() { return []; },
        addEventListener() {},
    };
    return el;
}

const elements = {};
function el(id) {
    if (!elements[id]) elements[id] = makeEl(id);
    return elements[id];
}

const sandbox = {
    console,
    // body.dataset carries what the server rendered (₹ by default — gap G3 was
    // the Forward page hard-coding ₹ while every other page hard-coded $).
    document: {
        getElementById: (id) => el(id),
        createElement: () => makeEl("created"),
        body: { dataset: { currencySymbol: "₹", currencyCode: "INR", currencyLocale: "en-IN" } },
    },
    Number, String, Math,
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(currencyCode, sandbox, { filename: "currency.js" });
vm.runInContext(cardsCode, sandbox, { filename: "metrics_cards.js" });
// trade_table.js declares TradeTable with a top-level `const`, which never lands
// on the sandbox object — re-export it explicitly so we can drive it.
vm.runInContext(`${tableCode}\n;globalThis.TradeTable = TradeTable;`, sandbox,
                { filename: "trade_table.js" });

const { renderMetricsCards, TradeTable, Money } = sandbox;
const $ = (id) => el(id);

let passed = 0;
function test(name, fn) {
    fn();
    passed += 1;
    console.log(`  ✓ ${name}`);
}

// ------------------------------------------------------------- metrics cards
test("renders the five PRD cards", () => {
    renderMetricsCards("m1", {
        total_pnl: 2847, win_rate_pct: 64.2, max_drawdown_pct: -8.3, sharpe: 1.42,
        total_trades: 203, closed_trades: 202, open_trades: 1,
    });
    const html = $("m1").innerHTML;
    for (const label of ["Total P&L", "Win Rate", "Max Drawdown", "Sharpe", "Trades"]) {
        assert.ok(html.includes(label), `missing card ${label}`);
    }
    assert.equal((html.match(/metric-card/g) || []).length, 5);
});

test("P&L sign drives the colour class", () => {
    renderMetricsCards("m3", { total_pnl: -100, win_rate_pct: 50, max_drawdown_pct: -5,
                              sharpe: 1, total_trades: 4, closed_trades: 4, open_trades: 0 });
    assert.match($("m3").innerHTML, /value neg/);
    renderMetricsCards("m4", { total_pnl: 10, win_rate_pct: 50, max_drawdown_pct: -5,
                              sharpe: 1, total_trades: 4, closed_trades: 4, open_trades: 0 });
    assert.match($("m4").innerHTML, /value pos/);
});

test("drawdown severity scales with magnitude", () => {
    const cases = [[-3, "dd-sev-1"], [-10, "dd-sev-2"], [-40, "dd-sev-3"]];
    cases.forEach(([dd, cls], i) => {
        renderMetricsCards(`s${i}`, { total_pnl: 0, win_rate_pct: 0, max_drawdown_pct: dd,
                                     sharpe: 0, total_trades: 0, closed_trades: 0, open_trades: 0 });
        assert.match($(`s${i}`).innerHTML, new RegExp(cls));
    });
});

test("win rate is '—' when nothing has closed, never a fake 0.00%", () => {
    renderMetricsCards("w1", { total_pnl: 1318, win_rate_pct: 0, max_drawdown_pct: -1,
                              sharpe: 1, total_trades: 1, closed_trades: 0, open_trades: 1 });
    const html = $("w1").innerHTML;
    assert.ok(html.includes("—"), "must show a dash for an un-closeable verdict");
    assert.ok(!html.includes("0.00%"), "0.00% would read as 'loses every trade'");
    assert.match(html, /nothing closed yet/);
});

test("win rate is shown normally once trades have closed", () => {
    renderMetricsCards("w2", { total_pnl: 5, win_rate_pct: 37.5, max_drawdown_pct: -2,
                              sharpe: 0.5, total_trades: 9, closed_trades: 8, open_trades: 1 });
    assert.match($("w2").innerHTML, /37\.50%/);
    assert.match($("w2").innerHTML, /8 closed · 1 open/);
});

test("trades card flags an open position", () => {
    renderMetricsCards("w3", { total_pnl: 5, win_rate_pct: 50, max_drawdown_pct: -2,
                              sharpe: 0.5, total_trades: 3, closed_trades: 3, open_trades: 0 });
    assert.ok(!$("w3").innerHTML.includes("still open"));
    renderMetricsCards("w4", { total_pnl: 5, win_rate_pct: 50, max_drawdown_pct: -2,
                              sharpe: 0.5, total_trades: 4, closed_trades: 3, open_trades: 1 });
    assert.match($("w4").innerHTML, /1 still open/);
});

test("one currency formatter drives every page (₹ from the server)", () => {
    assert.equal(Money.symbol(), "₹");
    assert.equal(Money.signed(-1234.5), "-₹1,234.50");
    // en-IN grouping is lakh/crore style — that is the point of carrying a locale.
    assert.equal(Money.format(100000, 0), "₹1,00,000");
    // The card renderer must use it, not a hard-coded "$" (old behaviour).
    renderMetricsCards("cur", { total_pnl: 500, win_rate_pct: 50, max_drawdown_pct: -2,
                               sharpe: 1, total_trades: 2, closed_trades: 2, open_trades: 0 });
    assert.match($("cur").innerHTML, /\+₹500\.00/);
    assert.ok(!$("cur").innerHTML.includes("$"), "no stray $ on a ₹ deployment");
});

// ---------------------------------------------------------------- trade table
test("open trades are labelled Open, wins/losses keep their icons", () => {
    TradeTable.render("tradeTable-wrap", [
        { id: 1, date: "2024-01-02", side: "LONG", entry: 100, exit: 90, pnl: -10, result: "Loss", is_open: false },
        { id: 2, date: "2024-02-02", side: "LONG", entry: 100, exit: 110, pnl: 10, result: "Win", is_open: false },
        { id: 3, date: "2024-03-02", side: "LONG", entry: 100, exit: 105, pnl: 5, result: "Win", is_open: true },
    ]);
    const html = $("tradeTable-wrap").querySelector("tbody").innerHTML;
    assert.match(html, /✅ Win/);
    assert.match(html, /❌ Loss/);
    assert.match(html, /⏳ Open/, "an open trade must not be scored as a win");
    assert.match(html, /tag-open/);
    assert.equal((html.match(/<tr>/g) || []).length, 3);
});

test("pnl cells are coloured by sign", () => {
    TradeTable.render("tradeTable-wrap2", [
        { id: 1, date: "2024-01-02", side: "LONG", entry: 1, exit: 2, pnl: 5, result: "Win", is_open: false },
        { id: 2, date: "2024-01-04", side: "LONG", entry: 2, exit: 1, pnl: -5, result: "Loss", is_open: false },
    ]);
    const html = $("tradeTable-wrap2").querySelector("tbody").innerHTML;
    assert.match(html, /class="pos"/);
    assert.match(html, /class="neg"/);
});

test("empty trade list renders the placeholder row", () => {
    TradeTable.render("tradeTable-wrap3", []);
    assert.match($("tradeTable-wrap3").querySelector("tbody").innerHTML, /No trades/);
});

console.log(`\nmetrics cards + trade table: ${passed} tests passed`);
