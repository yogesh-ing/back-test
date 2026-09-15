/**
 * Option-runner spawn payload — behaviour tests (options forward testing, C1).
 *
 * The forward engine has accepted `instrument: {"type": "option", ...}` since
 * the Gap remediation, but the spawn form never sent it, so option runners
 * could only be created with hand-written JSON. These tests drive the real
 * components/option_config.js so the form ⇄ payload translation cannot drift
 * away from what the backend expects.
 *
 * Usage: node tests/js/test_option_config.mjs
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import assert from "node:assert/strict";

const root = fileURLToPath(new URL("../../", import.meta.url));
const code = readFileSync(
    path.join(root, "src/backtest/web/static/js/components/option_config.js"), "utf8",
);

// Evaluate in *this* realm (not a vm context): the module builds plain objects
// and arrays, and cross-realm objects fail assert.deepEqual even when they are
// structurally identical. The browser global is skipped automatically because
// `window` does not exist here.
const OptionConfig = new Function(`${code}\nreturn OptionConfig;`)();

let tests = 0;
function test(name, fn) {
    try {
        fn();
        tests += 1;
    } catch (err) {
        console.error(`FAIL: ${name}\n  ${err.message}`);
        process.exitCode = 1;
    }
}

// ---------------------------------------------------------------------------
// Equity is untouched
// ---------------------------------------------------------------------------

test("equity instrument is the classic payload", () => {
    assert.deepEqual(OptionConfig.buildInstrument({ instrumentType: "equity" }),
                     { type: "equity" });
    assert.deepEqual(OptionConfig.buildInstrument({}), { type: "equity" });
});

test("equity spawns have no option validation problems", () => {
    assert.deepEqual(OptionConfig.validate({ instrumentType: "equity" }), []);
});

test("unknown instrument types fall back to equity, not to options", () => {
    assert.deepEqual(OptionConfig.buildInstrument({ instrumentType: "wat" }),
                     { type: "equity" });
});

// ---------------------------------------------------------------------------
// Option payload shape
// ---------------------------------------------------------------------------

test("direction-aware is the default structure", () => {
    const inst = OptionConfig.buildInstrument({ instrumentType: "option" });
    assert.equal(inst.type, "option");
    assert.deepEqual(inst.expression.type,
                     { BULLISH: "bull_call_spread", BEARISH: "bear_put_spread" });
    assert.equal(inst.expression.strike_selection, "atm");
    assert.equal(inst.expression.quantity, 1);
});

test("a fixed structure is sent as a plain string", () => {
    const inst = OptionConfig.buildInstrument({
        instrumentType: "option", structure: "long_put",
    });
    assert.equal(inst.expression.type, "long_put");
});

test("delta selection carries the target; atm does not", () => {
    const delta = OptionConfig.buildInstrument({
        instrumentType: "option", strikeSelection: "delta", deltaTarget: "0.25",
    });
    assert.equal(delta.expression.strike_selection, "delta");
    assert.equal(delta.expression.delta_target, 0.25);

    const atm = OptionConfig.buildInstrument({
        instrumentType: "option", strikeSelection: "atm", deltaTarget: "0.25",
    });
    assert.equal(atm.expression.strike_selection, "atm");
    assert.equal("delta_target" in atm.expression, false);
});

test("delta selection defaults the target to 0.35 when blank", () => {
    const inst = OptionConfig.buildInstrument({
        instrumentType: "option", strikeSelection: "delta", deltaTarget: "",
    });
    assert.equal(inst.expression.delta_target, 0.35);
});

test("lots per leg is an integer >= 1", () => {
    assert.equal(
        OptionConfig.buildInstrument({ instrumentType: "option", quantity: "3" })
            .expression.quantity, 3);
    assert.equal(
        OptionConfig.buildInstrument({ instrumentType: "option", quantity: "0" })
            .expression.quantity, 1);
    assert.equal(
        OptionConfig.buildInstrument({ instrumentType: "option", quantity: "" })
            .expression.quantity, 1);
});

// ---------------------------------------------------------------------------
// Exit rules
// ---------------------------------------------------------------------------

test("default exit block squares off one day early, like the engine", () => {
    const exit = OptionConfig.buildInstrument({ instrumentType: "option" })
        .expression.exit;
    assert.equal(exit.min_days_to_expiry, 1);
    assert.equal("signal_flip" in exit, false);  // engine default is flip-exit on
    assert.equal("stop_loss_pct" in exit, false);
    assert.equal("take_profit_pct" in exit, false);
});

test("percentages are converted to fractions", () => {
    const exit = OptionConfig.buildInstrument({
        instrumentType: "option", stopLossPct: "40", takeProfitPct: "150",
    }).expression.exit;
    assert.equal(exit.stop_loss_pct, 0.4);
    assert.equal(exit.take_profit_pct, 1.5);
});

test("blank stops and targets are omitted, never sent as zero", () => {
    // A zero would arm a hair-trigger stop on the first bar.
    const exit = OptionConfig.buildInstrument({
        instrumentType: "option", stopLossPct: "", takeProfitPct: "",
        neutralBars: "", maxBars: "",
    }).expression.exit;
    assert.equal("stop_loss_pct" in exit, false);
    assert.equal("take_profit_pct" in exit, false);
    assert.equal("neutral_bars" in exit, false);
    assert.equal("max_bars" in exit, false);
});

test("zero flat bars means 'hold' and is omitted", () => {
    const exit = OptionConfig.buildInstrument({
        instrumentType: "option", neutralBars: "0",
    }).expression.exit;
    assert.equal("neutral_bars" in exit, false);
});

test("unchecking flip-exit sends signal_flip: false", () => {
    const exit = OptionConfig.buildInstrument({
        instrumentType: "option", signalFlip: false,
    }).expression.exit;
    assert.equal(exit.signal_flip, false);
});

test("ride-into-settlement sends an explicit null", () => {
    const exit = OptionConfig.buildInstrument({
        instrumentType: "option", rideToSettlement: true, minDaysToExpiry: "5",
    }).expression.exit;
    assert.equal(exit.min_days_to_expiry, null);  // null = settle, not square off
});

test("days-to-expiry accepts 0 (square off on expiry day)", () => {
    const exit = OptionConfig.buildInstrument({
        instrumentType: "option", minDaysToExpiry: "0",
    }).expression.exit;
    assert.equal(exit.min_days_to_expiry, 0);
});

test("reenter is only sent when requested", () => {
    assert.equal("reenter" in OptionConfig.buildInstrument({ instrumentType: "option" })
        .expression.exit, false);
    assert.equal(OptionConfig.buildInstrument({ instrumentType: "option", reenter: true })
        .expression.exit.reenter, true);
});

test("a full exit configuration round-trips", () => {
    const exit = OptionConfig.buildInstrument({
        instrumentType: "option",
        structure: "bear_put_spread",
        strikeSelection: "delta", deltaTarget: "0.3",
        quantity: "2",
        stopLossPct: "50", takeProfitPct: "100",
        neutralBars: "3", maxBars: "10",
        minDaysToExpiry: "2", reenter: true, signalFlip: true,
    }).expression.exit;

    assert.deepEqual(exit, {
        neutral_bars: 3,
        stop_loss_pct: 0.5,
        take_profit_pct: 1,
        max_bars: 10,
        min_days_to_expiry: 2,
        reenter: true,
    });
});

// ---------------------------------------------------------------------------
// Validation
// ---------------------------------------------------------------------------

test("pool mode is rejected for options, with the reason", () => {
    const problems = OptionConfig.validate({
        instrumentType: "option", targetType: "pool", underlying: "NIFTY",
    });
    assert.deepEqual(problems.length, 1);
    assert.match(problems[0], /single underlying/);
});

test("a missing underlying is rejected", () => {
    const problems = OptionConfig.validate({ instrumentType: "option", underlying: "" });
    assert.match(problems[0], /Underlying is required/);
});

test("synthetic source only accepts the index underlyings it can price", () => {
    const problems = OptionConfig.validate({
        instrumentType: "option", underlying: "RELIANCE", source: "synthetic",
    });
    assert.equal(problems.length, 1);
    assert.match(problems[0], /NIFTY/);

    // ...but a live source is allowed through (real chains exist for more names).
    assert.deepEqual(
        OptionConfig.validate({ instrumentType: "option", underlying: "RELIANCE", source: "mstock" }),
        [],
    );
});

test("index underlyings pass", () => {
    for (const underlying of ["nifty", "BANKNIFTY"]) {
        assert.deepEqual(
            OptionConfig.validate({ instrumentType: "option", underlying }),
            [], `${underlying} should validate`,
        );
    }
});

test("a bad delta target is rejected", () => {
    assert.match(
        OptionConfig.validate({
            instrumentType: "option", underlying: "NIFTY",
            strikeSelection: "delta", deltaTarget: "1.5",
        })[0],
        /Delta target/,
    );
});

test("non-positive stops and targets are rejected", () => {
    assert.match(
        OptionConfig.validate({
            instrumentType: "option", underlying: "NIFTY", stopLossPct: "0",
        })[0],
        /Stop loss/,
    );
    assert.match(
        OptionConfig.validate({
            instrumentType: "option", underlying: "NIFTY", takeProfitPct: "-5",
        })[0],
        /Take profit/,
    );
});

test("zero lots is rejected", () => {
    assert.match(
        OptionConfig.validate({
            instrumentType: "option", underlying: "NIFTY", quantity: "0",
        })[0],
        /Lots per leg/,
    );
});

test("a clean option form validates", () => {
    assert.deepEqual(
        OptionConfig.validate({
            instrumentType: "option", underlying: "NIFTY", source: "synthetic",
            targetType: "single", structure: "bull_call_spread",
            strikeSelection: "delta", deltaTarget: "0.35", quantity: "1",
            stopLossPct: "40", takeProfitPct: "80", neutralBars: "2",
            minDaysToExpiry: "1",
        }),
        [],
    );
});

test("every structure the UI offers is a real bridge structure", () => {
    // Mirrors STRUCTURES in src/backtest/forward/options_bridge.py.
    const real = ["long_call", "long_put", "bull_call_spread", "bear_put_spread"];
    for (const id of OptionConfig.FIXED_STRUCTURES) {
        assert.ok(real.indexOf(id) !== -1, `${id} is not a bridge structure`);
    }
    assert.equal(OptionConfig.FIXED_STRUCTURES.length, real.length);
});

// ---------------------------------------------------------------------------
// Summary line
// ---------------------------------------------------------------------------

test("the summary names the structure, sizing and exit plan", () => {
    const line = OptionConfig.summarize({
        instrumentType: "option", structure: "bull_call_spread",
        strikeSelection: "atm", quantity: "2",
        stopLossPct: "40", takeProfitPct: "80", minDaysToExpiry: "2",
    });
    assert.match(line, /option · Bull call spread/);
    assert.match(line, /ATM/);
    assert.match(line, /2 lot/);
    assert.match(line, /stop 40%/);
    assert.match(line, /target 80%/);
    assert.match(line, /square off 2d early/);
});

test("the summary says 'ride to settlement' when that is chosen", () => {
    const line = OptionConfig.summarize({
        instrumentType: "option", rideToSettlement: true,
    });
    assert.match(line, /ride to settlement/);
});

test("equity summaries stay short", () => {
    assert.equal(OptionConfig.summarize({ instrumentType: "equity" }), "equity");
});

// ---------------------------------------------------------------------------

if (process.exitCode) {
    console.error(`\n${tests} passed, with failures above (see FAIL lines)`);
} else {
    console.log(`${tests} tests passed`);
}
