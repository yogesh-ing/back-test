/**
 * Cost & Risk Settings — the broker cards must show the brokers you trade.
 *
 * The complaint this pins: the page rendered every built-in preset (including
 * foreign banks and a mock) as an equal rectangle, so the brokers actually
 * pricing runs were lost in the list. The contract now is:
 *   * brokers Grouped by adoption — "In use" first and expanded;
 *   * everything else behind ONE collapsed <details> that says how many;
 *   * each in-use card says WHY it is in use (segment / active broker / data);
 *   * the active-broker select keeps every option but groups them the same way.
 *
 * The page's JS is inline in the template (there is no settings.js), so the
 * harness extracts it from the HTML and drives it in a stub DOM.
 *
 * Usage: node tests/js/test_settings_profiles.mjs
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import vm from "node:vm";
import assert from "node:assert/strict";

const root = fileURLToPath(new URL("../../", import.meta.url));
const html = readFileSync(path.join(root, "src/backtest/web/templates/settings.html"), "utf8");

const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
const inline = scripts.find((s) => s.includes("function renderProfiles"));
assert.ok(inline, "the settings template must still carry its inline panel script");

// ---------------------------------------------------------------------------
// Stub DOM
// ---------------------------------------------------------------------------

function makeNode(tag) {
    const node = {
        tagName: tag,
        children: [],
        attrs: {},
        style: {},
        dataset: {},
        className: "",
        hidden: false,
        selected: false,
        value: "",
        _text: "",
        _html: null,
        setAttribute(k, v) {
            this.attrs[k] = String(v);
            // mirror the DOM's reflected properties the panel reads back
            if (k === "value") this.value = String(v);
            if (k === "class") this.className = String(v);
            if (k === "id") this.id = String(v);
            if (k.startsWith("data-")) this.dataset[k.slice(5)] = String(v);
        },
        getAttribute(k) {
            return Object.prototype.hasOwnProperty.call(this.attrs, k) ? this.attrs[k] : null;
        },
        appendChild(child) {
            this.children.push(child);
            return child;
        },
        set innerHTML(value) {
            this._html = String(value);
            this.children = [];
        },
        get innerHTML() {
            return this._html === null ? "" : this._html;
        },
        set textContent(value) {
            this._text = String(value);
        },
        get textContent() {
            if (this._text) return this._text;
            if (this._html !== null) return this._html;
            return this.children.map((c) => c.textContent).join("");
        },
        querySelectorAll() {
            return [];
        },
        querySelector() {
            return null;
        },
        scrollIntoView() {},
        classList: { add() {}, remove() {}, contains: () => false },
    };
    node.classList = { add() {}, remove() {}, contains: () => false };
    return node;
}

const elements = {};
const el = (id) => (elements[id] = elements[id] || makeNode("div"));

const document = {
    createElement: (tag) => makeNode(tag),
    getElementById: (id) => el(id),
    querySelectorAll: () => [],
    addEventListener() {},
    body: makeNode("body"),
};

// ---------------------------------------------------------------------------
// Stub fetch — the payloads the panel boots from
// ---------------------------------------------------------------------------

const PROFILES = [
    {
        profile_id: "mstock", profile_name: "mStock", group: "in_use", is_preset: true,
        currency: "INR", default_segment: "equity_delivery", validated_on: null,
        usage: ["segment: options_index", "data: primary"],
        statutory_rates: { stt_delivery: "0.001", gst_rate: "0.18" },
        commission_model: { default: { model: "flat", per_trade: "20" } },
    },
    {
        profile_id: "dhan", profile_name: "Dhan", group: "in_use", is_preset: false,
        currency: "INR", default_segment: "equity_delivery", validated_on: null,
        matches_file: true,
        usage: ["segment: equity_intraday", "data: fallback"],
        statutory_rates: { stt_delivery: "0.001", gst_rate: "0.18" },
        commission_model: { default: { model: "percentage", rate: "0.0003" } },
    },
    {
        profile_id: "zerodha", profile_name: "Zerodha", group: "in_use", is_preset: true,
        currency: "INR", default_segment: "equity_delivery", validated_on: "2026-09-20",
        matches_file: false,
        contract_note_ref: "CN-1",
        usage: ["active broker", "differs from config/brokers.yaml — this row wins"],
        statutory_rates: {}, commission_model: {},
    },
    {
        profile_id: "ibkr", profile_name: "Interactive Brokers", group: "configured",
        is_preset: true, currency: "USD", default_segment: "equity_delivery", origin: "yaml",
        validated_on: null, usage: [], statutory_rates: {}, commission_model: {},
    },
    {
        profile_id: "zero", profile_name: "Zero-cost preset", group: "configured",
        is_preset: true, currency: "INR", default_segment: "equity_delivery",
        validated_on: null, usage: [], statutory_rates: {}, commission_model: {},
    },
    {
        profile_id: "upstox", profile_name: "Upstox", group: "catalogue", is_preset: true,
        currency: "INR", default_segment: "equity_delivery", validated_on: null,
        usage: [], statutory_rates: {}, commission_model: {},
    },
    {
        profile_id: "td_ameritrade", profile_name: "TD Ameritrade", group: "catalogue",
        is_preset: true, currency: "USD", default_segment: "equity_delivery",
        validated_on: null, usage: [], statutory_rates: {}, commission_model: {},
    },
    {
        profile_id: "robinhood", profile_name: "Robinhood", group: "catalogue", is_preset: true,
        currency: "USD", default_segment: "equity_delivery", validated_on: null,
        usage: [], statutory_rates: {}, commission_model: {},
    },
];

const PAYLOADS = {
    "/api/settings/brokers": { profiles: PROFILES, counts: { in_use: 3, configured: 2, catalogue: 3 } },
    "/api/settings/active-broker": { active_broker: "zerodha" },
    "/api/settings/live-kill-switch": { enabled: false },
    "/api/settings/segments": { segments: [] },
};

const fetch = (url) => {
    const key = String(url).split("?")[0];
    return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve(PAYLOADS[key] || {}),
    });
};

const sandbox = { document, fetch, console, Promise, JSON, Object, Array, String, Number };
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(inline, sandbox);

// let the boot chain's promises settle
for (let i = 0; i < 10; i += 1) {
    await new Promise((resolve) => setImmediate(resolve));
}

// ---------------------------------------------------------------------------
// Assertions
// ---------------------------------------------------------------------------

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

const host = document.getElementById("settings-profiles");
const summary = document.getElementById("settings-profile-summary");
const details = host.children.find((c) => c.tagName === "details");
const sections = host.children.filter((c) => c.tagName === "section");

test("in-use brokers render first, expanded, with a count", () => {
    assert.equal(sections.length, 1, "exactly one top-level (expanded) section");
    assert.equal(sections[0].getAttribute("data-group"), "in_use");
    assert.match(sections[0].children[0].textContent, /^In use \(3\)/);
});

test("nothing but in-use brokers is expanded", () => {
    const heads = host.children.filter((c) => c.tagName !== "details");
    assert.equal(heads.length, 1, "one expanded group, everything else collapsed");
});

test("the catalogue collapses behind one summary line", () => {
    assert.ok(details, "a <details> holds the rest");
    const toggle = details.children.find((c) => c.tagName === "summary");
    assert.equal(toggle.textContent, "Show 5 other broker(s) — config file + built-in presets");
    assert.ok(!details.attrs.open, "collapsed by default");
});

test("the collapsed block still separates file-defined from built-in", () => {
    const groups = details.children
        .filter((c) => c.tagName === "section")
        .map((c) => c.getAttribute("data-group"));
    assert.deepEqual(JSON.stringify(groups), JSON.stringify(["configured", "catalogue"]));
    const configured = details.children.find((c) => c.getAttribute("data-group") === "configured");
    assert.match(configured.children[0].textContent, /^In config\/brokers\.yaml \(2\)/);
});

test("each in-use card says why it is in use", () => {
    const grid = sections[0].children.find((c) => c.getAttribute("data-grid") === "1");
    const cards = grid.children;
    assert.equal(cards.length, 3);
    const dhan = cards[1].children.map((c) => c.textContent).join(" | ");
    assert.ok(dhan.includes("• segment: equity_intraday"), `dhan card: ${dhan}`);
    assert.ok(dhan.includes("• data: fallback"), `dhan card: ${dhan}`);
    const zerodha = cards[2].children.map((c) => c.textContent).join(" | ");
    assert.ok(zerodha.includes("• active broker"));
    assert.ok(zerodha.includes("this row wins"), "an override is called out on the card");
    assert.ok(zerodha.includes("validated 2026-09-20"));
});

test("a card says where its numbers come from", () => {
    const grid = sections[0].children.find((c) => c.getAttribute("data-grid") === "1");
    const dhan = grid.children[1].children.map((c) => c.textContent).join(" | ");
    assert.ok(dhan.includes("config/brokers.yaml"), "a seeded row still speaks the file");
    const zerodha = grid.children[2].children.map((c) => c.textContent).join(" | ");
    assert.ok(zerodha.includes("preset"), "a preset whose row has since moved says preset");
    const configured = details.children.find((c) => c.getAttribute("data-group") === "configured");
    const ibkr = configured.children
        .find((c) => c.getAttribute("data-grid") === "1")
        .children[0].children.map((c) => c.textContent).join(" | ");
    assert.ok(ibkr.includes("config/brokers.yaml"), "a yaml-only broker says so");
});

test("the summary line states the split", () => {
    assert.equal(
        summary.textContent,
        '3 broker(s) in use · 5 more available (collapsed) — nothing outside "In use" prices a run.'
    );
});

test("no in-use broker leaks into the collapsed catalogue", () => {
    const collapsed = details.children
        .filter((c) => c.getAttribute("data-grid") === "1")
        .flatMap((grid) => grid.children)
        .map((card) => card.children.map((c) => c.textContent).join(" "));
    for (const id of ["mstock", "dhan", "zerodha"]) {
        assert.ok(!collapsed.some((text) => text.includes(id)), `${id} must stay in use only`);
    }
});

test("the active-broker select keeps every option, grouped the same way", () => {
    const select = document.getElementById("settings-active-broker");
    assert.equal(select.children[0].value, "", "an explicit empty choice exists");
    const groups = select.children.filter((c) => c.tagName === "optgroup");
    assert.deepEqual(
        JSON.stringify(groups.map((g) => g.getAttribute("label"))),
        JSON.stringify(["In use", "In config/brokers.yaml", "Built-in presets"])
    );
    assert.equal(groups[0].children.length, 3);
    assert.equal(groups[2].children.length, 3, "the whole catalogue stays selectable");
    const selected = groups[0].children.find((o) => o.selected);
    assert.equal(selected.value, "zerodha", "the active broker is preselected");
});

let failures = 0;
for (const [name, fn] of tests) {
    try {
        fn();
        console.log(`  ✓ ${name}`);
    } catch (error) {
        failures += 1;
        console.log(`  ✗ ${name}\n      ${error.message}`);
    }
}
console.log(`\n${tests.length - failures} tests passed`);
if (failures) process.exit(1);
