/**
 * Labels — the one identifier→human-name resolver (finding F11).
 *
 * Every surface that used to print a strategy key, a segment code or an option
 * structure code now prints what this file returns and keeps the raw key in a
 * `title`. If a label ever drifts back to the identifier, the page reads like a
 * database dump again, so the vocabulary is pinned here rather than left to
 * whatever each renderer happened to do.
 *
 * Usage: node tests/js/test_labels.mjs
 */

import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import assert from "node:assert/strict";

const root = fileURLToPath(new URL("../../", import.meta.url));
const components = path.join(root, "src/backtest/web/static/js/components");

let passed = 0;
async function test(name, fn) {
    try {
        await fn();
        passed += 1;
        console.log(`  ✓ ${name}`);
    } catch (err) {
        console.error(`  ✗ ${name}\n    ${err.message}`);
        process.exitCode = 1;
    }
}

/** labels.js reads OptionConfig when it is there, so the real file goes in first. */
function boot(fetchImpl) {
    const sandbox = {
        console,
        fetch: fetchImpl,
        document: { body: { dataset: {} } },
    };
    sandbox.globalThis = sandbox;
    sandbox.window = sandbox;   // both components publish themselves on window
    vm.createContext(sandbox);
    for (const file of ["option_config.js", "labels.js"]) {
        vm.runInContext(readFileSync(path.join(components, file), "utf8"), sandbox, { filename: file });
    }
    return sandbox.Labels;
}

await test("strategy keys read as names, not identifiers", () => {
    const Labels = boot();
    assert.equal(Labels.strategy("bollinger_reversion"), "Bollinger reversion");
    assert.equal(Labels.strategy("sma_crossover"), "SMA crossover");
    assert.equal(Labels.strategy("banknifty_ema_momentum"), "Banknifty EMA momentum");
    assert.equal(Labels.strategy("swing_donchian_breakout"), "Swing Donchian breakout");
    assert.equal(Labels.strategy("buy_and_hold"), "Buy and hold");
    assert.equal(Labels.strategy("hf_equity_scalp"), "HF equity scalp");
});

await test("no label still carries an underscore", () => {
    const Labels = boot();
    for (const key of [
        "ema_reversion_pob", "volume_oi_surge", "swing_rsi2_meanrev", "one_min_scalp",
        "immediate_strangle", "straddle_short_atm", "directional_options",
    ]) {
        assert.doesNotMatch(Labels.strategy(key), /_/, `${key} kept its underscores`);
        assert.match(Labels.strategy(key), /^[A-Z]/, `${key} does not start with a capital`);
    }
});

await test("an operator's own capitals survive, so a name is never re-cased into a word", () => {
    const Labels = boot();
    assert.equal(Labels.strategy("CE_instant_buy"), "CE instant buy");
    assert.equal(Labels.strategy("PE_instant_buy"), "PE instant buy");
    assert.equal(Labels.humanise("swing_rsi2_meanrev"), "Swing RSI2 meanrev");
});

await test("option structures use the vocabulary the spawn form already uses", () => {
    const Labels = boot();
    assert.equal(Labels.structure("long_call"), "Long call");
    assert.equal(Labels.structure("bear_put_spread"), "Bear put spread");
    // OptionConfig deliberately labels the bare code as the short-premium form
    assert.equal(Labels.structure("strangle"), "Short strangle");
    assert.equal(Labels.structure("short_straddle"), "Short straddle");
});

await test("an unknown structure degrades to words instead of printing the code", () => {
    const Labels = boot();
    assert.equal(Labels.structure("iron_condor"), "Iron condor");
});

await test("a segment shows the name the operator configured", async () => {
    const payload = {
        segments: [
            { name: "options_index", display_name: "Index Options" },
            { name: "equity_intraday", display_name: "Equity Intraday" },
        ],
    };
    const Labels = boot(() => Promise.resolve({ ok: true, json: () => Promise.resolve(payload) }));
    await Labels.warm();
    assert.equal(Labels.segment("options_index"), "Index Options");
    assert.equal(Labels.segment("equity_intraday"), "Equity Intraday");
});

await test("a caller that already holds the display name wins over the cache", () => {
    const Labels = boot();
    assert.equal(Labels.segment("equity_delivery", "Delivery book"), "Delivery book");
});

await test("without the segments read a code still becomes words, and a dead endpoint does not throw", async () => {
    const Labels = boot(() => Promise.reject(new Error("offline")));
    assert.equal(Labels.segment("equity_delivery"), "Equity delivery");
    await Labels.warm();
    assert.equal(Labels.segment("equity_delivery"), "Equity delivery");
});

await test("a stored trade symbol keeps its underlying and labels its structure", () => {
    const Labels = boot();
    // the option books persist "<UNDERLYING> <structure_type>" (paper_runner.py:1872)
    assert.equal(Labels.symbol("NIFTY short_straddle"), "NIFTY Short straddle");
    assert.equal(Labels.symbol("BANKNIFTY long_put"), "BANKNIFTY Long put");
    assert.equal(Labels.symbol("NIFTY bull_call_spread"), "NIFTY Bull call spread");
    // exchange symbols and operator-chosen names are not structures
    assert.equal(Labels.symbol("TCS"), "TCS");
    assert.equal(Labels.symbol("NIFTY_50"), "NIFTY_50");
    assert.equal(Labels.symbol("NIFTY 25000 CE"), "NIFTY 25000 CE");
    assert.equal(Labels.symbol(""), "");
    assert.equal(Labels.symbol(null), "");
});

console.log(`\n${passed} tests passed`);
