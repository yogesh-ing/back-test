/**
 * Risk strip feed line — the strip may not claim a feed that does not exist.
 *
 * `p.tick` is the *persisted* session counter (forward/portfolio_manager.py saves and
 * restores tick_index), so keying liveness off it made a restarted, broker-less app
 * read "Feed active · 0 running · 6145 ticks". docs/MARKET-STATUS-SPEC-2026-10-02.md
 * is the rule this pins: only server truth decides whether a feed is live, and a
 * counter never stands in for one.
 *
 * Usage: node tests/js/test_risk_strip_feed_state.mjs
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import vm from "node:vm";
import assert from "node:assert/strict";

const root = fileURLToPath(new URL("../../", import.meta.url));
const code = readFileSync(
    path.join(root, "src/backtest/web/static/js/components/risk_strip.js"), "utf8",
);

function makeEl(id) {
    return {
        id, textContent: "", innerHTML: "", className: "", hidden: false, title: "",
        style: {}, value: "", checked: false,
        classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
        querySelector() { return makeEl(`${id}::child`); },
        querySelectorAll() { return []; },
        getBoundingClientRect() { return { width: 10, height: 10, top: 0, left: 0 }; },
        addEventListener() {},
        appendChild() {},
    };
}

// Load the component and return the render entry point it publishes on window.
function load() {
    const elements = {};
    const el = (id) => (elements[id] = elements[id] || makeEl(id));
    const window = { addEventListener() {}, location: { pathname: "/" }, scrollTo() {} };
    const document = {
        getElementById: (id) => el(id),
        querySelector: () => makeEl("q"),
        querySelectorAll: () => [],
        addEventListener() {},
        createElement: () => makeEl("created"),
        body: { dataset: { currencySymbol: "₹" }, classList: { add() {}, remove() {} } },
        documentElement: { dataset: {}, classList: { add() {}, remove() {} }, style: {} },
    };
    const sandbox = {
        console, window, document,
        fetch: () => Promise.resolve({ ok: true, json: () => Promise.resolve({}) }),
        setInterval: () => 0, clearInterval: () => {}, setTimeout: () => 0,
        Math, Date, JSON, Number, String, Boolean, Array, Object, Promise, RegExp,
        Error, TypeError, isNaN, parseInt, parseFloat, Intl, Set, Map, Symbol,
        EventSource: function () {},
    };
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(code, sandbox);
    assert.ok(window.RiskStrip, "component must publish window.RiskStrip");
    return {
        update: (p) => window.RiskStrip.updateStrip(p),
        feed: () => el("risk-strip-feed").textContent,
    };
}

const OPEN = { day_state: "TRADING_DAY", session_state: "OPEN" };
const CLOSED = { day_state: "TRADING_DAY", session_state: "POST_CLOSE" };
const snapshot = (capability, market) => ({
    tick: 6145, running: 0, halted: false,
    daily_loss_pct: 0.019, daily_loss_used: 4683, daily_loss_limit: 250000,
    drawdown_pct: 0, max_drawdown_limit_pct: 0.8,
    deployed_capital: 0, deployed_pct: 0,
    positions: 0, runners: [], capability, market,
});

let pass = 0;
const check = (name, fn) => { fn(); console.log(`  ✓ ${name}`); pass += 1; };

const cases = [
    {
        name: "disconnected + restored ticks → no broker feed, never 'Feed active'",
        p: snapshot({ broker_connected: false }, CLOSED),
        want: "No broker feed",
        forbid: "Feed active",
    },
    {
        name: "connected + market open → Feed active",
        p: snapshot({ broker_connected: true }, OPEN),
        want: "Feed active",
    },
    {
        name: "connected + market closed → paused, not active",
        p: snapshot({ broker_connected: true }, CLOSED),
        want: "Feed paused (market closed)",
        forbid: "Feed active",
    },
    {
        name: "no capability block at all → no broker feed (fail closed)",
        p: (() => { const s = snapshot(undefined, OPEN); delete s.capability; return s; })(),
        want: "No broker feed",
        forbid: "Feed active",
    },
];

for (const c of cases) {
    const handle = load();
    handle.update(c.p);
    const rendered = handle.feed();
    assert.ok(rendered.includes(c.want), `${c.name}: expected "${c.want}" in "${rendered}"`);
    if (c.forbid) assert.ok(!rendered.includes(c.forbid), `${c.name}: must not say "${c.forbid}"`);
    assert.ok(/this session/.test(rendered), `${c.name}: the tick counter must be labelled as a session total`);
    check(`${c.name} — "${rendered}"`, () => {});
}

// The counter still shows — just never as the liveness signal.
check("the session tick counter is still surfaced", () => {
    const handle = load();
    handle.update(snapshot({ broker_connected: false }, CLOSED));
    assert.match(handle.feed(), /6145 ticks this session/);
});

console.log(`\n${pass} tests passed`);
