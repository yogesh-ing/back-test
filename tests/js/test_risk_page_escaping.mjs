#!/usr/bin/env node
/**
 * F14 — every string that arrives from the server on /risk is operator-typed
 * (a runner name, a halt reason, an audit action) and lands in an innerHTML
 * template. This harness drives the real component in a stub DOM with hostile
 * payloads and asserts that what reaches the screen is text, never markup.
 *
 * Usage: node tests/js/test_risk_page_escaping.mjs
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import vm from "node:vm";
import assert from "node:assert/strict";

const root = fileURLToPath(new URL("../../", import.meta.url));
const load = (rel) => readFileSync(path.join(root, rel), "utf8");

const ELEMENT_IDS = [
    "risk-page", "risk-page-mode", "risk-page-refresh", "risk-page-export",
    "risk-dashboard-grid", "risk-broker-cards",
    "risk-cb-banner", "risk-warnings-banner", "risk-limits-body",
    "risk-global-config-form", "risk-bucket-config-form", "risk-correlation-config",
    "audit-tbody", "audit-pagination", "audit-scope", "audit-type", "audit-search",
    "btn-audit-refresh", "btn-audit-export", "exp-symbol-tbody", "exp-corr-tbody",
    "exposure-concentration-warnings", "perf-summary", "trades-grouped",
    "trades-kind", "trades-limit", "btn-trades-refresh", "btn-trades-export",
    "btn-save-global", "btn-save-buckets", "btn-live-mode", "cfg-breach-mode",
];

// The one thing a stub DOM cannot fake is what a browser does with innerHTML:
// it parses. So every assignment is recorded here and scanned — a payload that
// survives unescaped would have become a node.
const markup = [];

function makeEl(id) {
    const handlers = {};
    const el = {
        id,
        textContent: "",
        value: "",
        hidden: false,
        className: "",
        dataset: {},
        style: {},
        classList: {
            _s: new Set(),
            add(c) { this._s.add(c); },
            remove(c) { this._s.delete(c); },
            contains(c) { return this._s.has(c); },
        },
        addEventListener(type, fn) { (handlers[type] = handlers[type] || []).push(fn); },
        fire(type, event) { (handlers[type] || []).forEach((fn) => fn(event || {})); },
        querySelector() { return null; },
        querySelectorAll() { return []; },
        setAttribute() {},
        getAttribute() { return null; },
        appendChild(c) { elements[c.id] = c; return c; },
    };
    let html = "";
    Object.defineProperty(el, "innerHTML", {
        get: () => html,
        set(v) {
            html = String(v);
            markup.push({ id, html });
        },
    });
    return el;
}

const elements = {};
const el = (id) => (elements[id] = elements[id] || makeEl(id));
ELEMENT_IDS.forEach(el);

// Hostile values on the fields the page interpolates: a halt reason, a runner
// name, an audit action/detail, a symbol, a stored limit, a bucket key.
const PAYLOADS = {
    "/api/portfolio/risk": {
        risk: {
            current: {
                daily_loss_pct: 0.1, drawdown_pct: 0.05, deployed_pct: 0.2,
                daily_loss_used: 5000, deployed_capital: 20000, open_positions: 3,
                halted: true,
                halt_reason: "breach <img src=x onerror=alert(1)>",
                halt_mode: 'flattening"><script>alert(2)</script>',
                warnings: ["<b>bold</b>"],
            },
            buckets: {},
            by_broker: [
                { broker: "<i>ICICI</i>", runner_count: 1, equity: 100, daily_pnl: 0, open_positions: 0 },
                { broker: "<i>AXIS</i>", runner_count: 2, equity: 50, daily_pnl: -10, open_positions: 1 },
            ],
            segments: {
                "<u>opt</u>": { display_name: "<s>Options</s>", broker: "ICICI", mode: "paper", equity: 1, halted: true, halt_reason: "<b>halt</b>", runner_count: 1 },
            },
            exposure: {
                gross_exposure_pct: 0.3, total_gross_notional: 2,
                by_symbol: [{ symbol: "<em>NIFTY</em>", notional: 1, runners: "<b>2</b>" }],
                by_correlation: [{ label: "<em>Bank</em>", notional: 1, count: 2 }],
            },
        },
    },
    "/api/portfolio/risk/limits": {
        limits: {
            per_trade_max_loss_inr: 1000, per_runner_max_lots: 2,
            per_runner_max_order_value_inr: 5000, kill_switch_engaged_on_boot: false,
            sha256: "abcdef1234567890",
            entitlement: { "<i>paper</i>": ["<b>strangle</b>"] },
        },
    },
    "/api/portfolio/risk/config": {
        global: { daily_loss_limit: "<x>50000</x>", max_drawdown_pct: 0.25, max_leverage: 1, breach_mode: "PAUSE_AND_HOLD", correlation_warning_threshold: 3 },
        buckets: { "<i>paper</i>": { max_position_pct: 0.1, max_position_value: "<q>10000</q>", max_open_positions: "<w>5</w>" } },
        correlation_groups: { "<i>grp</i>": { label: "<b>Bank</b>", threshold: 3, symbols: ["<s>HDFC</s>", "REL"] } },
    },
    "/api/portfolio/audit": {
        audit: [{
            ts: "2026-11-04T09:15:00", scope: "<i>live</i>", action: "<b>SPAWN</b>",
            detail: 'reason="x"><img src=y onerror=alert(3)>', runner_id: "<u>r-1</u>",
        }],
    },
    "/api/portfolio/trades/aggregated": {
        summary: { total: "<i>12</i>", win_rate: 0.5 },
        trades: [{
            exit_ts: "2026-11-04T09:20:00", symbol: "<em>NIFTY</em>", runner_name: "<b>QA13</b>",
            side: "<i>BUY</i>", qty: "<s>10</s>", net_pnl: 100, kind: "equity",
        }],
    },
};

const requests = [];
const domReady = [];
const ENDPOINTS = Object.keys(PAYLOADS).sort((a, b) => b.length - a.length);
const sandbox = {
    console, Math, Number, String, Object, Array, JSON, Date, Promise, RegExp, Error,
    setTimeout: () => 0, clearTimeout: () => {}, setInterval: () => 0, clearInterval: () => {},
    encodeURIComponent,
    document: {
        readyState: "complete", hidden: false,
        body: Object.assign(makeEl("body"), { dataset: { currencySymbol: "₹" } }),
        getElementById: (id) => elements[id] || null,
        createElement: () => makeEl(""),
        querySelector: () => null,
        querySelectorAll: () => [],
        addEventListener(type, fn) { if (type === "DOMContentLoaded") domReady.push(fn); },
    },
    fetch: async (url) => {
        requests.push(url);
        const key = ENDPOINTS.find((p) => url.startsWith(p));
        return { ok: true, status: 200, json: async () => (key ? PAYLOADS[key] : { success: true }) };
    },
};
sandbox.window = sandbox;
sandbox.window.location = { search: "", hash: "" };
sandbox.window.addEventListener = () => {};
sandbox.window.showToast = () => {};
vm.createContext(sandbox);

// labels.js first: the trades and limits tables resolve keys through it (F11).
vm.runInContext(
    load("src/backtest/web/static/js/components/labels.js") + "\n;globalThis.Labels = Labels;",
    sandbox);
vm.runInContext(load("src/backtest/web/static/js/components/risk_page.js"), sandbox);
const RP = sandbox.window.RiskPage;
assert.ok(RP, "risk_page.js must publish window.RiskPage");

const tests = [];
const test = (name, fn) => tests.push({ name, fn });

/** Run a loader and hand back every markup string it wrote. */
async function capture(fn) {
    markup.length = 0;
    await fn();
    // the loaders fire their fetches without awaiting; a macrotask boundary
    // lets every promise chain in the component settle before we scan.
    await new Promise((r) => setImmediate(r));
    return markup.map((m) => m.html).join("\n");
}

const PROBE = [
    "<img src=x onerror=alert(1)>",
    '"><script>alert(2)</script>',
    "<b>SPAWN</b>",
    "<em>NIFTY</em>",
    "<b>QA13</b>",
    "<b>strangle</b>",
];

const assertClean = (html, where) => {
    for (const probe of PROBE) {
        assert.ok(!html.includes(probe), `unescaped payload in ${where}: ${probe}`);
    }
    assert.ok(!/<[a-z]+[^>]*\sonerror=/i.test(html), `an inline handler reached ${where}`);
};

test("dashboard: halt reason, warnings, broker and symbol all arrive escaped", async () => {
    const html = await capture(() => RP.loadRisk());
    assertClean(html, "dashboard");
    assert.match(html, /&lt;img src=x onerror=alert\(1\)&gt;/);
    assert.match(html, /&lt;b&gt;halt&lt;\/b&gt;/);
    assert.match(html, /&lt;em&gt;NIFTY&lt;\/em&gt;/);
    assert.match(html, /&lt;s&gt;Options&lt;\/s&gt;/);
    assert.match(html, /&lt;i&gt;ICICI&lt;\/i&gt;/);
});

test("circuit-breaker banner: the reason stays text inside its span", async () => {
    const html = await capture(() => RP.loadRisk());
    const banner = markup.find((m) => m.id === "risk-cb-banner");
    assert.ok(banner, "halted state must show the breaker banner");
    assert.match(banner.html, /CIRCUIT BREAKER HALTED/);
    assertClean(banner.html, "breaker banner");
});

test("audit row: action, scope, detail and runner id escaped, attribute closed", async () => {
    const html = await capture(() => RP.loadAudit());
    assertClean(html, "audit table");
    assert.match(html, /&lt;b&gt;SPAWN&lt;\/b&gt;/);
    assert.match(html, /&lt;i&gt;live&lt;\/i&gt;/);
    assert.match(html, /&lt;u&gt;r-1&lt;\/u&gt;/);
    assert.ok(!html.includes('reason="x">'), "the detail string broke out of its title attribute");
});

test("trades row: symbol, runner name, side and qty escaped", async () => {
    const html = await capture(() => RP.loadTrades());
    assertClean(html, "trades list");
    assert.match(html, /&lt;b&gt;QA13&lt;\/b&gt;/);
    assert.match(html, /&lt;em&gt;NIFTY&lt;\/em&gt;/);
    assert.match(html, /&lt;i&gt;12&lt;\/i&gt;/);
});

test("config form: bucket keys, group labels and stored limits escaped", async () => {
    const html = await capture(() => RP.loadConfig());
    assertClean(html, "config form");
    assert.match(html, /&lt;i&gt;paper&lt;\/i&gt;/);
    assert.match(html, /&lt;b&gt;Bank&lt;\/b&gt;/);
    assert.match(html, /&lt;q&gt;10000&lt;\/q&gt;/);
    assert.ok(!html.includes('value="<w>5</w>'), "a stored limit broke out of its value attribute");
});

test("rulebook table: mode, structure and error text escaped", async () => {
    // loadLimits is not on the published handle — it runs from the page's own
    // init, so the init is fired rather than a private function reached around.
    const html = await capture(async () => { domReady.forEach((fn) => fn()); });
    const body = markup.find((m) => m.id === "risk-limits-body");
    assert.ok(body, "the rulebook must render its table");
    assertClean(body.html, "rulebook table");
    assert.match(body.html, /&lt;i&gt;paper&lt;\/i&gt;/);
    assert.match(body.html, /&lt;b&gt;strangle&lt;\/b&gt;/);
});

test("escaping does not eat the chrome the page is built on", async () => {
    const html = await capture(() => RP.loadRisk());
    for (const kept of ["Daily Loss Used", "Gross Exposure", "Open Positions", "risk-gauge-large", "risk-broker-card", "Reset &amp; Resume"]) {
        assert.ok(html.includes(kept), `missing ${kept}`);
    }
});

test("a plain halt reason still reads as plain text", async () => {
    PAYLOADS["/api/portfolio/risk"].risk.current.halt_reason = "daily loss breached";
    const html = await capture(() => RP.loadRisk());
    assert.match(html, /daily loss breached/);
    PAYLOADS["/api/portfolio/risk"].risk.current.halt_reason = "breach <img src=x onerror=alert(1)>";
});

const run = async () => {
    let passed = 0;
    for (const { name, fn } of tests) {
        try {
            await fn();
            passed++;
            console.log(`ok  ${name}`);
        } catch (err) {
            console.error(`FAIL ${name}\n  ${err.message}`);
            process.exitCode = 1;
        }
    }
    console.log(`${passed} tests passed`);
};
run();
