/**
 * Render an option runner through the *real* view code, with no browser.
 *
 * The matrix (`portfolio.js`) and the deep-dive drawer (`deep_dive.js`) only
 * make sense as HTML, so rather than trusting string greps this harness loads
 * the actual files against a minimal DOM, feeds them the payload a live API
 * returns (task C2) and prints the markup they produce.
 *
 * Usage: node tests/js/render_option_views.mjs <snapshot.json> <detail.json>
 *   snapshot.json — GET /api/portfolio/summary  (portfolio.runners[])
 *   detail.json   — POST .../control {action: deep_dive} (runner object)
 *
 * Prints two sections to stdout:
 *   ---MATRIX---            innerHTML of #matrix-body
 *   ---POSITIONS---         innerHTML of #aggregate-positions
 *   ---DEEPDIVE---          innerHTML of #dd-body
 *   ---DEEPDIVE-CONFIG---   the Config tab markup
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const root = fileURLToPath(new URL("../../", import.meta.url));
const [snapshotPath, detailPath] = process.argv.slice(2);
if (!snapshotPath || !detailPath) {
    console.error("usage: node render_option_views.mjs <snapshot.json> <detail.json>");
    process.exit(2);
}
const snapshot = JSON.parse(readFileSync(snapshotPath, "utf8"));
const detail = JSON.parse(readFileSync(detailPath, "utf8"));

// ---------------------------------------------------------------------------
// Minimal DOM: enough for the two view files, no dependencies.
// ---------------------------------------------------------------------------

const store = {};      // id -> {innerHTML, textContent, ...}
const listeners = {};  // event name -> [fn]

function makeElement(id) {
    if (store[id]) return store[id];
    const el = {
        id,
        innerHTML: "",
        textContent: "",
        hidden: false,
        value: "",
        checked: false,
        dataset: {},
        style: {},
        chart: null,
        classList: {
            add() {}, remove() {}, contains() { return false; },
        },
        addEventListener(type, fn) {
            (listeners[id + ":" + type] = listeners[id + ":" + type] || []).push(fn);
        },
        querySelectorAll() { return []; },
        querySelector() { return null; },
        getContext() { return {}; },
        appendChild() {},
        focus() {},
        remove() {},
        closest() { return null; },
    };
    store[id] = el;
    return el;
}

globalThis.document = {
    getElementById: (id) => makeElement(id),
    querySelectorAll: () => [],
    querySelector: () => null,
    addEventListener: (type, fn) => {
        (listeners[type] = listeners[type] || []).push(fn);
    },
    createElement: (tag) => makeElement("created-" + tag),
    body: makeElement("body"),
};
globalThis.window = globalThis;
globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
globalThis.requestAnimationFrame = (fn) => fn();

// The runner pages open an SSE stream on boot; a stub keeps it quiet.
globalThis.EventSource = class {
    constructor() { this.readyState = 0; }
    addEventListener() {}
    close() {}
};

// Money is a base.html component; the views call it for every amount.
globalThis.Money = {
    symbol: "₹",
    format: (v) => "₹" + Math.round(Number(v) || 0).toLocaleString("en-IN"),
    signed: (v) => (Number(v) < 0 ? "−" : "+") + "₹" +
        Math.round(Math.abs(Number(v) || 0)).toLocaleString("en-IN"),
};

// fetch: serve the two payloads the views ask for, in the shape the API uses.
globalThis.fetch = (url) => {
    const body = String(url).includes("/api/portfolio/runner/")
        ? { success: true, runner: detail }
        : snapshot;
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });
};

function load(rel) {
    const code = readFileSync(path.join(root, rel), "utf8");
    // eslint-disable-next-line no-new-func
    new Function(code)();
}

load("src/backtest/web/static/js/components/option_config.js");
load("src/backtest/web/static/js/components/option_view.js");
load("src/backtest/web/static/js/deep_dive.js");
load("src/backtest/web/static/js/portfolio.js");

// ---------------------------------------------------------------------------
// Drive the views
// ---------------------------------------------------------------------------

// portfolio.js boots on DOMContentLoaded, then fetches the summary.
(listeners["DOMContentLoaded"] || []).forEach((fn) => fn());

const portfolio = snapshot.portfolio || snapshot;

// deep_dive.js renders on demand: DeepDive.open(id) → fetch(runner) → render.
globalThis.DeepDive.open(detail.instance_id, portfolio);

const flush = () => new Promise((r) => setTimeout(r, 60));
await flush();

const body = store["dd-body"] ? store["dd-body"].innerHTML : "";
const configTab = body.includes('data-ddpanel="params"')
    ? body.slice(body.indexOf('data-ddpanel="params"'))
    : "";

console.log("---MATRIX---");
console.log(store["matrix-body"] ? store["matrix-body"].innerHTML : "");
console.log("---POSITIONS---");
console.log(store["aggregate-positions"] ? store["aggregate-positions"].innerHTML : "");
console.log("---DEEPDIVE---");
console.log(body);
console.log("---DEEPDIVE-CONFIG---");
console.log(configTab);
