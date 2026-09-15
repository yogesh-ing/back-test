/**
 * Option book rendering data — behaviour tests (options forward testing, C2).
 *
 * The matrix and the deep-dive drawer render an option runner from
 * `row.options.open_structures_detail` (the bridge snapshot). Everything the
 * views need to turn that into cells, sub-lines, tables and labels lives in
 * components/option_view.js; these tests pin the numbers and the wording so a
 * renderer change cannot quietly mis-report premium, legs or expiry.
 *
 * Usage: node tests/js/test_option_view.mjs
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import assert from "node:assert/strict";

const root = fileURLToPath(new URL("../../", import.meta.url));
const load = (rel) => readFileSync(path.join(root, rel), "utf8");

const OptionView = new Function(
    load("src/backtest/web/static/js/components/option_config.js") + "\n" +
    load("src/backtest/web/static/js/components/option_view.js") + "\n" +
    "return OptionView;",
)();

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

/** A closed bull call spread, as the live deep-dive probe returned it. */
const STRUCTURE = {
    symbol: "NIFTY bull_call_spread",
    label: "NIFTY bull_call_spread 24950 25000",
    side: "LONG",
    qty: 12,                       // lots
    units: 900,                    // lots × 75
    lot_size: 75,
    entry_price: 29.63,            // net premium per unit
    current_price: 38.05,
    unrealized_pnl: 7578.0,
    entry_cost: 26667.0,
    open_pnl_pct: 0.2842,
    kind: "option",
    structure_type: "bull_call_spread",
    underlying: "NIFTY",
    strikes: [24950.0, 25000.0],
    legs: 2,
    legs_detail: [
        { option_type: "CE", side: "LONG", strike: 24950, trading_symbol: "NIFTY261024950CE",
          qty: 900, entry_price: 545.65, current_price: 625.85, pnl: 72180.0 },
        { option_type: "CE", side: "SHORT", strike: 25000, trading_symbol: "NIFTY261025000CE",
          qty: 900, entry_price: 516.02, current_price: 587.8, pnl: -64602.0 },
    ],
    expiry: "2026-10-29",
    next_expiry: "2026-10-29",
    bars_held: 7,
};

function optionRunner(overrides = {}) {
    return Object.assign({
        instance_id: "abc12345",
        name: "NIFTY-BCS",
        strategy_name: "directional_options",
        target_type: "SINGLE_SYMBOL",
        target_label: "NIFTY",
        symbol_count: 1,
        timeframe: "1day",
        allocated_capital: 500000,
        equity: 500000,
        open_pnl: 7578.0,
        daily_pnl: 7578.0,
        open_positions: 1,              // C2: structures, not legs
        status: "RUNNING",
        mode: "paper",
        source: "synthetic",
        instrument: { type: "option", expression: { type: "bull_call_spread" } },
        options: {
            open_positions: 2,          // legs, from the bridge
            open_structures: 1,
            executed_count: 1,
            closed_count: 0,
            closed_structures: 0,
            wins: 0, losses: 0, win_rate: 0,
            total_pnl: 0, costs_paid: 80,
            settled_count: 0,
            unrealized_pnl: 7578.0,
            open_structures_detail: [STRUCTURE],
            exit_policy: { min_days_to_expiry: 1 },
        },
    }, overrides);
}

const EQUITY_RUNNER = {
    instance_id: "eq1",
    name: "RELIANCE-SMA",
    target_label: "RELIANCE",
    open_positions: 2,
    open_pnl: 100,
    instrument: { type: "equity" },
    options: null,
};

// ---------------------------------------------------------------------------
// Detection
// ---------------------------------------------------------------------------

test("only option runners are treated as options", () => {
    assert.equal(OptionView.isOption(optionRunner()), true);
    assert.equal(OptionView.isOption(EQUITY_RUNNER), false);
    assert.equal(OptionView.isOption(undefined), false);
    assert.equal(OptionView.isOption({}), false);
});

test("an equity runner has no option notes and keeps its positions count", () => {
    assert.deepEqual(OptionView.matrixNotes(EQUITY_RUNNER), []);
    assert.deepEqual(OptionView.positionsCell(EQUITY_RUNNER), { primary: 2, sub: "" });
    assert.equal(OptionView.instrumentLabel(EQUITY_RUNNER), "");
});

// ---------------------------------------------------------------------------
// Book-level numbers
// ---------------------------------------------------------------------------

test("premium at risk is the net premium actually paid", () => {
    assert.equal(OptionView.premiumAtRisk(optionRunner()), 26667.0);
});

test("open legs come from the leg detail, not the structure count", () => {
    assert.equal(OptionView.openLegs(optionRunner()), 2);
    assert.equal(OptionView.openStructures(optionRunner()).length, 1);
});

test("book P&L prefers the bridge's own mark", () => {
    assert.equal(OptionView.bookPnl(optionRunner()), 7578.0);
});

test("a flat book reports zero everywhere instead of throwing", () => {
    const flat = optionRunner({
        open_positions: 0,
        options: Object.assign({}, optionRunner().options,
            { open_structures: 0, open_positions: 0, open_structures_detail: [],
              unrealized_pnl: 0 }),
    });
    assert.equal(OptionView.premiumAtRisk(flat), 0);
    assert.equal(OptionView.bookPnl(flat), 0);
    assert.equal(OptionView.openLegs(flat), 0);
    assert.match(OptionView.matrixNotes(flat)[0], /flat/);
});

test("a runner with no options payload at all is safe", () => {
    const running = optionRunner({ options: null });
    assert.deepEqual(OptionView.openStructures(running), []);
    assert.equal(OptionView.bookPnl(running), 0);
    assert.equal(OptionView.positionsCell(running).primary, 0);
});

// ---------------------------------------------------------------------------
// Matrix cells
// ---------------------------------------------------------------------------

test("the matrix positions cell counts structures and shows legs underneath", () => {
    assert.deepEqual(OptionView.positionsCell(optionRunner()),
                     { primary: 1, sub: "2 legs" });
});

test("the matrix notes name the structure, strikes, lots, expiry and premium", () => {
    const notes = OptionView.matrixNotes(optionRunner());
    assert.match(notes[0], /Bull call spread/);
    assert.match(notes[0], /24,950\/25,000 CE/);
    assert.match(notes[0], /12 lots/);
    assert.match(notes[0], /exp 29 Oct/);
    assert.match(notes[0], /premium 29\.63 → 38\.05/);
    assert.match(notes[1], /premium at risk/);
});

test("a flat option runner says so rather than looking like an equity runner", () => {
    const flat = optionRunner({
        open_positions: 0,
        options: Object.assign({}, optionRunner().options,
            { open_structures: 0, open_structures_detail: [], unrealized_pnl: 0,
              closed_structures: 3 }),
    });
    assert.deepEqual(OptionView.matrixNotes(flat), ["flat · 3 closed"]);
    assert.deepEqual(OptionView.positionsCell(flat), { primary: 0, sub: "" });
});

test("the type column names the structure, or the configured one when flat", () => {
    assert.equal(OptionView.instrumentLabel(optionRunner()), "Bull call spread");
    const flat = optionRunner({
        options: Object.assign({}, optionRunner().options,
            { open_structures: 0, open_structures_detail: [] }),
    });
    assert.equal(OptionView.instrumentLabel(flat), "Bull call spread");
});

test("direction-aware runners say so (the structure is chosen per view)", () => {
    const aware = optionRunner({
        instrument: { type: "option", expression: {
            type: { BULLISH: "bull_call_spread", BEARISH: "bear_put_spread" },
        } },
        options: Object.assign({}, optionRunner().options,
            { open_structures: 0, open_structures_detail: [] }),
    });
    assert.equal(OptionView.instrumentLabel(aware), "Direction-aware");
});

// ---------------------------------------------------------------------------
// Expiry + structure labels
// ---------------------------------------------------------------------------

test("expiries render short and unambiguous", () => {
    assert.equal(OptionView.expiryLabel("2026-10-29"), "29 Oct");
    assert.equal(OptionView.expiryLabel("2026-01-01"), "1 Jan");
    assert.equal(OptionView.expiryLabel(null), "—");
    assert.equal(OptionView.expiryLabel(""), "—");
});

test("structure ids become the same labels the spawn form uses", () => {
    assert.equal(OptionView.structureLabel("bull_call_spread"), "Bull call spread");
    assert.equal(OptionView.structureLabel("long_put"), "Long put");
    assert.equal(OptionView.structureLabel("direction_aware"), "Direction-aware");
    assert.equal(OptionView.structureLabel("exotic_thing"), "exotic thing");
});

test("strikes are thousands-separated, index-style", () => {
    assert.equal(OptionView.strikesText(STRUCTURE), "24,950/25,000");
    assert.equal(OptionView.strikesText({ strikes: [25100] }), "25,100");
    assert.equal(OptionView.strikesText({}), "—");
});

// ---------------------------------------------------------------------------
// Deep-dive rows
// ---------------------------------------------------------------------------

test("structure rows carry everything the open-book table prints", () => {
    const rows = OptionView.structureRows(optionRunner());
    assert.equal(rows.length, 1);
    const s = rows[0];
    assert.equal(s.structure, "Bull call spread");
    assert.equal(s.strikes, "24,950/25,000");
    assert.equal(s.optionType, "CE");
    assert.equal(s.lots, 12);
    assert.equal(s.units, 900);
    assert.equal(s.entryPrice, 29.63);
    assert.equal(s.currentPrice, 38.05);
    assert.equal(s.pnl, 7578.0);
    assert.equal(s.pnlPct, 0.2842);
    assert.equal(s.expiryText, "29 Oct");
    assert.equal(s.barsHeld, 7);
    assert.equal(s.legs.length, 2);
    assert.equal(s.legs[0].tradingSymbol, "NIFTY261024950CE");
});

test("leg rows keep their own side, strike and mark", () => {
    const legs = OptionView.structureRows(optionRunner())[0].legs;
    assert.deepEqual(legs.map((l) => [l.side, l.optionType, l.strike]),
                     [["LONG", "CE", 24950], ["SHORT", "CE", 25000]]);
    assert.equal(legs[0].entryPrice, 545.65);
    assert.equal(legs[1].currentPrice, 587.8);
});

test("book stats cover open, at-risk, marked and closed", () => {
    const stats = OptionView.bookStats(optionRunner());
    const byKey = Object.fromEntries(stats.map((s) => [s.key, s]));
    assert.equal(byKey.open_structures.value, 1);
    assert.equal(byKey.open_legs.value, 2);
    assert.equal(byKey.premium_at_risk.value, 26667.0);
    assert.equal(byKey.premium_at_risk.money, true);
    assert.equal(byKey.book_pnl.value, 7578.0);
    assert.equal(byKey.next_expiry.value, "29 Oct");
    assert.equal(byKey.closed.value, 0);
    assert.equal(byKey.book_realized.pnl, true);
});

test("closed-book stats summarise wins and settlements", () => {
    const traded = optionRunner({
        options: Object.assign({}, optionRunner().options, {
            closed_structures: 4, wins: 1, losses: 3, win_rate: 0.25,
            total_pnl: -8051.25, settled_count: 1,
        }),
    });
    const closed = OptionView.bookStats(traded).find((s) => s.key === "book_realized");
    assert.equal(closed.value, -8051.25);
    assert.match(closed.sub, /25% won/);
    assert.match(closed.sub, /settled 1/);
});

test("no options payload means no book strip at all", () => {
    assert.deepEqual(OptionView.bookStats(EQUITY_RUNNER), []);
});

// ---------------------------------------------------------------------------
// Closed trades
// ---------------------------------------------------------------------------

test("option trades are recognised by their kind tag", () => {
    assert.equal(OptionView.isOptionTrade({ kind: "option" }), true);
    assert.equal(OptionView.isOptionTrade({ kind: "equity" }), false);
    assert.equal(OptionView.isOptionTrade({}), false);
});

test("exit reasons read as English", () => {
    assert.equal(OptionView.exitReasonLabel("stop_loss"), "Stop loss");
    assert.equal(OptionView.exitReasonLabel("expiry_settlement"), "Expiry settlement");
    assert.equal(OptionView.exitReasonLabel("signal_flip"), "View flipped");
    assert.equal(OptionView.exitReasonLabel("auto_square_off"), "Squared off pre-expiry");
    assert.equal(OptionView.exitReasonLabel("who_knows"), "who knows");
    assert.equal(OptionView.exitReasonLabel(null), "—");
});

test("a closed option trade is labelled by structure and reason", () => {
    const line = OptionView.tradeLine({
        kind: "option", label: "NIFTY bull_call_spread 24950/25000 CE",
        exit_reason: "stop_loss",
    });
    assert.match(line, /bull_call_spread 24950\/25000 CE/);
    assert.match(line, /Stop loss/);
});

test("equity trades keep their plain symbol", () => {
    assert.equal(OptionView.tradeLine({ kind: "equity", symbol: "RELIANCE" }), "RELIANCE");
});

// ---------------------------------------------------------------------------

if (process.exitCode) {
    console.error(`\n${tests} passed, with failures above (see FAIL lines)`);
} else {
    console.log(`${tests} tests passed`);
}
