/**
 * Portfolio Intelligence UI — alert widget, alert detail modal and the Risk
 * Board intelligence sections, driven in a stub DOM.
 *
 * What is pinned here is the product contract, not the markup:
 *   * the widget always shows a count + severity breakdown, most severe first;
 *   * every alert offers exactly "View Details" and "Dismiss";
 *   * the detail modal explains and deep-links but offers NO trading action
 *     (information layer: the platform never closes positions);
 *   * a page load does not replay open alerts as toasts — only new ones toast;
 *   * the Greeks cards flag a breach and the heatmap flags hot pairs;
 *   * a realized-vol proxy is labelled as such (never passed off as India VIX).
 *
 * Usage: node tests/js/test_alert_widget.mjs
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import vm from "node:vm";
import assert from "node:assert/strict";

const root = fileURLToPath(new URL("../../", import.meta.url));
const load = (rel) => readFileSync(path.join(root, rel), "utf8");

function makeEl(id) {
    const handlers = {};
    return {
        id,
        innerHTML: "",
        textContent: "",
        value: "",
        hidden: false,
        dataset: {},
        style: {},
        _handlers: handlers,
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
}

const elements = {};
const el = (id) => (elements[id] = elements[id] || makeEl(id));
const storage = {};
const document = {
    readyState: "complete",
    hidden: false,
    body: Object.assign(makeEl("body"), { dataset: { currencySymbol: "₹" } }),
    getElementById: (id) => elements[id] || null,
    createElement: () => makeEl(""),
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener() {},
};
document.body.appendChild = (c) => { elements[c.id] = c; return c; };

const requests = [];
let respond = () => ({ success: true, alerts: [], counts: { total: 0 }, version: 0 });
const toasts = [];
const sandbox = {
    console,
    setTimeout: () => 0,
    clearTimeout: () => {},
    setInterval: () => 0,
    clearInterval: () => {},
    URLSearchParams,
    Date,
    Math,
    Number,
    String,
    Object,
    Promise,
    encodeURIComponent,
    document,
    localStorage: {
        getItem: (k) => (k in storage ? storage[k] : null),
        setItem: (k, v) => { storage[k] = String(v); },
    },
    fetch: async (url, opts) => {
        requests.push({ url, method: (opts && opts.method) || "GET", body: opts && opts.body });
        const data = respond(url, opts);
        return { ok: true, status: 200, json: async () => data };
    },
};
sandbox.window = sandbox;
sandbox.window.location = { search: "", hash: "" };
sandbox.window.addEventListener = () => {};
sandbox.window.showToast = (msg, kind) => toasts.push({ msg, kind });
vm.createContext(sandbox);

const tests = [];
const test = (name, fn) => tests.push({ name, fn });
const flush = () => new Promise((r) => setImmediate(r));

// ------------------------------------------------------------------ fixtures
const gammaAlert = {
    alert_id: "a-gamma",
    alert_type: "portfolio_gamma_critical",
    severity: "critical",
    title: "Portfolio gamma critical",
    section: "pi-greeks",
    message: "Portfolio gamma -1,247 (threshold -1,000) <b>",
    created_at: new Date(Date.now() - 120000).toISOString(),
    occurrences: 3,
    status: "active",
    data: {
        net_gamma: -1247, threshold: -1000, breach_pct: 24.7, gamma_rupees_1pct: -58000,
        move_1pct_pnl: -45000, move_2pct_pnl: -180000, net_theta: 12000,
        summary: "Contributing: Strangle A (2)",
        contributors: [
            { instance_id: "r1", label: "Strangle A", strategy: "immediate_strangle", mode: "paper",
              gamma: -890, share: 0.71, positions: 2 },
            { instance_id: "r2", label: "Iron B", strategy: "iron_condor", mode: "paper",
              gamma: -357, share: 0.29, positions: 1 },
        ],
    },
    what_it_means: "The portfolio loses money on large moves in either direction.",
    typical_responses: ["Reduce short options", "Buy protective wings"],
    notified_strategies: [{ subscriber_id: "r1", ok: true }],
    subscriptions: {
        subscribed: [{ subscriber_id: "r1", runner: "Strangle A", strategy: "immediate_strangle" }],
        not_subscribed: [{ instance_id: "r2", runner: "Iron B", strategy: "iron_condor" }],
    },
};
const infoAlert = {
    alert_id: "a-strike", alert_type: "strike_clustering", severity: "info",
    title: "Strike clustering", section: "pi-concentration", message: "NIFTY 23500: 3 positions",
    created_at: new Date().toISOString(), data: {},
};
const warnAlert = {
    alert_id: "a-conc", alert_type: "concentration_high", severity: "warning",
    title: "Concentration high", section: "pi-concentration", message: "NIFTY concentration: 78%",
    created_at: new Date(Date.now() - 5000).toISOString(), data: { underlying: "NIFTY", pct: 78 },
};

// ------------------------------------------------------------ alert widget
sandbox.__ALERT_WIDGET_NO_AUTOINIT__ = true;
vm.runInContext(load("src/backtest/web/static/js/components/alert_widget.js"), sandbox);
const AW = sandbox.AlertWidget;

test("minimized: no alerts reads as clear", () => {
    const html = AW.renderMinimized({ total: 0 });
    assert.match(html, /No alerts/);
    assert.match(html, /aw-pill-clear/);
});

test("minimized: count + severity breakdown, colour of the worst", () => {
    const html = AW.renderMinimized({ total: 3, critical: 1, warning: 2, info: 0 });
    assert.match(html, /<strong class="aw-count">3<\/strong>/);
    assert.match(html, /🔴 1/);
    assert.match(html, /🟡 2/);
    assert.doesNotMatch(html, /🔵/);
    assert.match(html, /aw-pill-critical/);
});

test("expanded: most severe first, each with View Details + Dismiss, message escaped", () => {
    const html = AW.renderExpanded([infoAlert, gammaAlert, warnAlert], { total: 3 });
    const order = ["a-gamma", "a-conc", "a-strike"].map((id) => html.indexOf(`data-alert-id="${id}"`));
    assert.ok(order[0] < order[1] && order[1] < order[2], "critical → warning → info");
    assert.equal((html.match(/View Details/g) || []).length, 3);
    assert.equal((html.match(/>Dismiss</g) || []).length, 3);
    assert.match(html, /&lt;b&gt;/);
    assert.doesNotMatch(html, /-1,000\) <b>/);
});

test("detail modal: explains, attributes, deep-links — and has no trading action", () => {
    const html = AW.renderDetail(gammaAlert);
    assert.match(html, /Current state/);
    assert.match(html, /What this means/);
    assert.match(html, /Contributing strategies/);
    assert.match(html, /Typical responses/);
    assert.match(html, /Strategy subscriptions/);
    assert.match(html, /will <strong>not<\/strong> take automatic action/);
    assert.match(html, /href="\/portfolio\?tab=risk#pi-greeks"/);
    assert.match(html, /Strangle A/);
    assert.match(html, /71%/);
    assert.match(html, /✅ notified/);
    assert.match(html, /contributes but is not subscribed/);
    assert.doesNotMatch(html, /close position|close all|flatten|exit now/i);
});

test("gamma metrics include the worst 1% and 2% move P&L", () => {
    const rows = AW.metricRows(gammaAlert).map(([k]) => k);
    assert.ok(rows.includes("Worst P&L on a 1% move"));
    assert.ok(rows.includes("Worst P&L on a 2% move"));
    assert.ok(rows.includes("Net gamma (Δ per 1% move)"));
});

test("init polls /api/alerts/active and renders the count; toggle persists", async () => {
    el("alert-widget");
    respond = () => ({ success: true, alerts: [gammaAlert, warnAlert], counts: { total: 2, critical: 1, warning: 1, info: 0 }, version: 4 });
    AW.init();
    await flush();
    assert.ok(requests.some((r) => r.url === "/api/alerts/active"));
    assert.match(elements["alert-widget"].innerHTML, /aw-count">2</);
    AW.setExpanded(true);
    assert.equal(storage["pi.alertWidget.expanded"], "1");
    assert.equal(elements["alert-widget"].dataset.state, "expanded");
    assert.match(elements["alert-widget"].innerHTML, /Portfolio Alerts \(2\)/);
});

test("first load does not toast open alerts; a new critical one does", async () => {
    assert.equal(toasts.length, 0, "page load must not replay alerts as toasts");
    const fresh = Object.assign({}, gammaAlert, { alert_id: "a-new", message: "Data feed stale" , alert_type: "data_feed_stale", title: "Data feed stale" });
    AW.apply({ alerts: [gammaAlert, warnAlert, fresh], counts: { total: 3, critical: 2, warning: 1 }, version: 5 });
    assert.equal(toasts.length, 1);
    assert.equal(toasts[0].kind, "error");
    assert.match(toasts[0].msg, /Data feed stale/);
});

test("dismiss posts to the lifecycle endpoint", async () => {
    requests.length = 0;
    respond = (url) => url.endsWith("/dismiss")
        ? { success: true, alert: {}, counts: { total: 1 } }
        : { success: true, alerts: [warnAlert], counts: { total: 1, warning: 1 }, version: 6 };
    const btn = { dataset: { aw: "dismiss", id: "a-gamma" } };
    elements["alert-widget"].fire("click", { target: { closest: () => btn } });
    await flush(); await flush();
    const post = requests.find((r) => r.method === "POST");
    assert.ok(post, "a POST was made");
    assert.equal(post.url, "/api/alerts/a-gamma/dismiss");
});

// ------------------------------------------------------- Risk Board sections
sandbox.__PI_NO_AUTOINIT__ = true;
vm.runInContext(load("src/backtest/web/static/js/components/portfolio_intelligence.js"), sandbox);
const PI = sandbox.PortfolioIntelligence;

test("greeks: cards flag a gamma breach; breakdown + scenarios render", () => {
    ["pi-greeks-cards", "pi-greeks-warnings", "pi-greeks-breakdown", "pi-greeks-scenarios",
     "pi-greeks-summary", "pi-greeks-foot"].forEach(el);
    PI.renderGreeks({
        net_delta: 250, net_gamma: -1247, net_vega: -8200, net_theta: 12000,
        delta_rupees_1pct: 58000, gamma_rupees_1pct: -60000, positions: 3, legs: 6, legs_priced: 6,
        bias: { delta: "long bias", gamma: "short γ", vega: "short vol", theta: "collecting decay" },
        breakdown_by_strategy: [{ label: "Strangle A", strategy: "immediate_strangle", mode: "paper",
            delta: 120, gamma: -890, vega: -5000, theta: 8000, positions: 2, gamma_share: 0.71 }],
        scenario_list: [{ key: "spot_down_2pct", label: "Market -2%", pnl: -180000 }],
        warnings: ["IV unknown for 1 leg(s) — assumed 15%"], units: {},
    }, { gamma_critical: -1000, delta_warning_abs: 800 });
    assert.match(elements["pi-greeks-cards"].innerHTML, /pi-card-crit/);
    assert.match(elements["pi-greeks-cards"].innerHTML, /Net Gamma/);
    assert.match(elements["pi-greeks-breakdown"].innerHTML, /Strangle A/);
    assert.match(elements["pi-greeks-breakdown"].innerHTML, /71%/);
    assert.match(elements["pi-greeks-scenarios"].innerHTML, /Market -2%/);
    assert.match(elements["pi-greeks-scenarios"].innerHTML, /pnl-neg/);
    assert.match(elements["pi-greeks-warnings"].innerHTML, /IV unknown/);
});

test("correlation: hot pairs flagged; fewer than two runners explained", () => {
    el("pi-corr-heatmap"); el("pi-correlation-summary"); el("pi-correlation-foot");
    PI.renderCorrelation({ labels: ["A"], values: [[1]], alerts: [], threshold: 0.8 });
    assert.match(elements["pi-corr-heatmap"].innerHTML, /at least two/);
    PI.renderCorrelation({ labels: ["A", "B"], values: [[1, 0.91], [0.91, 1]],
        alerts: [{ id_a: "a", id_b: "b" }], threshold: 0.8, min_samples: 30, window: 240 });
    assert.match(elements["pi-corr-heatmap"].innerHTML, /pi-corr-hot/);
    assert.match(elements["pi-correlation-summary"].textContent, /1 pair above 0\.8/);
});

test("regime: a realized-vol proxy is labelled, never passed off as India VIX", () => {
    el("pi-regime-current"); el("pi-regime-fit"); el("pi-regime-summary"); el("pi-regime-foot");
    PI.renderRegime({ regime: "high_vol", label: "HIGH VOLATILITY", vix: 96.2,
        source: "realized_vol_proxy:NIFTY", is_proxy: true, feed_synthetic: true,
        bands: { low_max: 15, high_min: 22 }, history: [],
        strategy_fit: [{ runner: "Strangle A", strategy: "immediate_strangle", status: "unfavorable", range: [10, 15] }] });
    assert.match(elements["pi-regime-current"].innerHTML, /realized volatility/);
    assert.match(elements["pi-regime-current"].innerHTML, /illustrative/);
    assert.match(elements["pi-regime-summary"].textContent, /\(proxy\)/);
    assert.match(elements["pi-regime-fit"].innerHTML, /unfavorable/);
    assert.match(elements["pi-regime-fit"].innerHTML, /VIX 10–15/);
});

// ------------------------------------------------------------------ runner
let passed = 0;
for (const t of tests) {
    try {
        await t.fn();
        passed += 1;
        console.log("  ✓", t.name);
    } catch (e) {
        console.error("  ✗", t.name);
        console.error(e);
        process.exit(1);
    }
}
console.log(`${passed} tests passed`);
