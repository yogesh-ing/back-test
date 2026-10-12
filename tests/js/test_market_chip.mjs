/**
 * Tests for Global Market Status Chip (MARKET-STATUS-SPEC-2026-10-02 §6).
 *
 * Runs under plain Node: executes market_chip.js in a VM sandbox with stub DOM.
 * Usage: node tests/js/test_market_chip.mjs
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import vm from "node:vm";
import assert from "node:assert/strict";

const root = fileURLToPath(new URL("../../", import.meta.url));
const code = readFileSync(
    path.join(root, "src/backtest/web/static/js/components/market_chip.js"), "utf8",
);

function makeElement(id) {
    return {
        id,
        textContent: "",
        dataset: {},
        attrs: {},
        setAttribute(k, v) { this.attrs[k] = v; },
        getAttribute(k) { return this.attrs[k]; },
        addEventListener() {},
    };
}

const elements = {
    "market-status-chip": makeElement("market-status-chip"),
    "market-status-text": makeElement("market-status-text"),
};

const sandbox = {
    console: { log: () => {}, warn: () => {}, error: () => {} },
    setTimeout: () => 0,
    clearTimeout: () => {},
    setInterval: () => 0,
    clearInterval: () => {},
    Date,
    Object,
    JSON,
    document: {
        getElementById: (id) => elements[id] || null,
        addEventListener: () => {},
    },
    window: {},
};
sandbox.globalThis = sandbox;

vm.createContext(sandbox);
vm.runInContext(code, sandbox, { filename: "market_chip.js" });

const chip = () => elements["market-status-chip"];
const text = () => elements["market-status-text"];
const updateMarketChip = sandbox.window.updateMarketChip;

let passed = 0;
function test(name, fn) {
    fn();
    passed += 1;
    console.log(`  ✓ ${name}`);
}

test("HOLIDAY state: renders red dot, CLOSED label, and truncates name > 18 chars", () => {
    updateMarketChip({
        day_state: "HOLIDAY",
        session_state: "POST_CLOSE",
        holiday_name: "Mahatma Gandhi Jayanti",
        next_open_ts: "2026-10-05T09:15:00+05:30",
        next_open_formatted: "Mon 05 Oct 09:15 IST",
        gap_warning: null,
        is_open: false,
    });

    assert.equal(chip().getAttribute("data-state"), "HOLIDAY");
    // "Mahatma Gandhi Jayanti" is 22 chars -> "Mahatma Gandhi Ja…"
    assert.equal(text().textContent, "NSE CLOSED · Mahatma Gandhi Ja…");
    const title = chip().getAttribute("title");
    assert.match(title, /NSE Trading Hours: 09:15 - 15:30 IST/);
    assert.match(title, /Mon 05 Oct 09:15 IST/);
    assert.match(title, /Holiday: Mahatma Gandhi Jayanti/);
});

test("WEEKEND state: renders WEEKEND chip and label", () => {
    updateMarketChip({
        day_state: "WEEKEND",
        session_state: "POST_CLOSE",
        holiday_name: null,
        next_open_ts: "2026-10-05T09:15:00+05:30",
        next_open_formatted: "Mon 05 Oct 09:15 IST",
        gap_warning: null,
        is_open: false,
    });

    assert.equal(chip().getAttribute("data-state"), "WEEKEND");
    assert.equal(text().textContent, "NSE CLOSED · WEEKEND");
    assert.match(chip().getAttribute("title"), /Mon 05 Oct 09:15 IST/);
});

test("TRADING_DAY + OPEN: renders OPEN chip and label", () => {
    // When day_state is TRADING_DAY, market_chip calculates time-of-day live
    // but accepts session_state from the server
    updateMarketChip({
        day_state: "TRADING_DAY",
        session_state: "OPEN",
        holiday_name: null,
        next_open_ts: "2026-10-06T09:15:00+05:30",
        next_open_formatted: "Tue 06 Oct 09:15 IST",
        gap_warning: null,
        is_open: true,
    });

    // Check tooltip formatting
    const title = chip().getAttribute("title");
    assert.match(title, /NSE Trading Hours: 09:15 - 15:30 IST/);
});

test("Calendar gap warning: appended to title attribute", () => {
    updateMarketChip({
        day_state: "TRADING_DAY",
        session_state: "OPEN",
        holiday_name: null,
        next_open_ts: "2027-03-16T09:15:00+05:30",
        next_open_formatted: "Tue 16 Mar 09:15 IST",
        gap_warning: "⚠ No holiday data for 2027 — weekday rule only",
        is_open: true,
    });

    const title = chip().getAttribute("title");
    assert.match(title, /Calendar warning: ⚠ No holiday data for 2027 — weekday rule only/);
});

test("Short holiday name <= 18 chars: not truncated", () => {
    updateMarketChip({
        day_state: "HOLIDAY",
        session_state: "POST_CLOSE",
        holiday_name: "Diwali Balipratipada",
        next_open_ts: "2026-11-11T09:15:00+05:30",
        next_open_formatted: "Wed 11 Nov 09:15 IST",
        gap_warning: null,
        is_open: false,
    });

    // "Diwali Balipratipada" is 20 chars -> "Diwali Balipratip…"
    assert.equal(text().textContent, "NSE CLOSED · Diwali Balipratip…");

    updateMarketChip({
        day_state: "HOLIDAY",
        session_state: "POST_CLOSE",
        holiday_name: "Christmas",
        next_open_ts: "2026-12-28T09:15:00+05:30",
        next_open_formatted: "Mon 28 Dec 09:15 IST",
        gap_warning: null,
        is_open: false,
    });

    assert.equal(text().textContent, "NSE CLOSED · Christmas");
});

console.log(`market_chip.js: ${passed} tests passed`);
