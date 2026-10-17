/**
 * Option structure vocabulary — behaviour tests.
 *
 * Phase 3 of the execution refactor deleted this module's spawn-payload
 * builders (`buildExpression`/`buildInstrument` hardcoded direction-aware
 * spreads into POST /api/portfolio/runner/create). The spawn form now
 * sends field selections only. These tests pin what remains — the
 * read-only vocabulary — and that the builders stay deleted.
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

test("vocabulary covers the bridge's structures", () => {
    const ids = OptionConfig.STRUCTURES.map((s) => s.id);
    for (const id of ["long_call", "long_put", "bull_call_spread", "bear_put_spread", "strangle"]) {
        assert.ok(ids.includes(id), `missing ${id}`);
    }
    assert.deepEqual(OptionConfig.SYNTHETIC_UNDERLYINGS, ["NIFTY", "BANKNIFTY"]);
    assert.equal(OptionConfig.isOption("option"), true);
    assert.equal(OptionConfig.isOption("equity"), false);
});

test("expression builders are deleted (Phase 3)", () => {
    assert.equal(OptionConfig.buildExpression, undefined);
    assert.equal(OptionConfig.buildInstrument, undefined);
    assert.equal(code.includes("bull_call_spread\", BEARISH"), false);
});

if (!process.exitCode) console.log(`test_option_config: ${tests} passed`);
