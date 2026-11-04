/**
 * Money.count / Money.num — the client half of the one-formatting rule (F9).
 *
 * The server renders a portfolio row through the `money`/`count` Jinja filters
 * (backtest/web/formatting.py); these are the calls the same screen makes from
 * JavaScript. If they ever disagree about grouping, a number appears two ways
 * on one page — which is what tests/test_number_formatting.py forbids on the
 * Python side and this file pins on the JS side.
 *
 * Usage: node tests/js/test_currency_count.mjs
 */

import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import assert from "node:assert/strict";

const root = fileURLToPath(new URL("../../", import.meta.url));
const code = readFileSync(
    path.join(root, "src/backtest/web/static/js/components/currency.js"), "utf8",
);

let passed = 0;
function test(name, fn) {
    try {
        fn();
        passed += 1;
        console.log(`  ✓ ${name}`);
    } catch (err) {
        console.error(`  ✗ ${name}\n    ${err.message}`);
        process.exitCode = 1;
    }
}

/** currency.js reads the currency config off <body data-currency-*>, as base.html sets it. */
function boot(dataset) {
    const sandbox = { console, document: { body: { dataset: dataset || {} } } };
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(code, sandbox, { filename: "currency.js" });
    return sandbox.Money;
}

test("counts group lakhs, the same way the server's count filter does", () => {
    const Money = boot();
    assert.equal(Money.count(1234567), "12,34,567");
    assert.equal(Money.count(4817), "4,817");
    assert.equal(Money.count(42), "42");
    assert.equal(Money.count(0), "0");
});

test("counts never carry a currency symbol", () => {
    const Money = boot();
    assert.doesNotMatch(Money.count(1234), /[₹$]/);
});

test("num keeps decimals in the same grouping", () => {
    const Money = boot();
    assert.equal(Money.num(1234567.891, 2), "12,34,567.89");
    assert.equal(Money.num(1234567, 0), "12,34,567");
});

test("a non-numeric count prints zero, not NaN", () => {
    const Money = boot();
    for (const value of [null, undefined, NaN, "abc"]) {
        assert.equal(Money.count(value), "0", `${String(value)} must not reach the screen as NaN`);
    }
});

test("the server's locale wins, so a non-IN deployment groups western", () => {
    const Money = boot({ currencyLocale: "en-US", currencySymbol: "$" });
    assert.equal(Money.count(1234567), "1,234,567");
    assert.equal(Money.format(1234567, 0), "$1,234,567");
});

console.log(`\n${passed} tests passed`);
