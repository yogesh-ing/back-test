/**
 * Data-tab row labels — what the symbol list actually prints.
 *
 * A row reads "RELIANCE · 1min · 4,817 bars". The timeframe shown is what was
 * DOWNLOADED, not the derived set the Backtest picker offers. Both come from
 * /api/data/coverage and both are on the same row object, so the two are easy
 * to swap — and swapping them prints the same nine-item list under every
 * instrument holding 1min data, which is the bug this file exists to catch.
 *
 * dmDetail() mirrors SymbolPicker.detailOf(); tests/js/test_symbol_picker.mjs
 * covers the picker's copy of the rule.
 *
 * The second half covers the one-payload rule (2026-10-06): the symbol list and
 * the inventory table are two views of ONE response, so the page must issue one
 * request and never fall back to /api/data/inventory — whose scan was the whole
 * reason the tab counted every bar twice per load.
 *
 * data_manager.js is a plain script with top-level init side effects (it binds
 * the refresh button, loads symbols, starts polling), so it runs against a
 * permissive DOM stub. Its top-level function declarations land on the sandbox
 * global, which is how dmDetail is reachable without exporting it.
 *
 * Usage: node tests/js/test_data_manager_labels.mjs
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import vm from "node:vm";
import assert from "node:assert/strict";

const root = fileURLToPath(new URL("../../", import.meta.url));
const src = readFileSync(path.join(root, "src/backtest/web/static/js/data_manager.js"), "utf8");

/** Any property read returns something callable, truthy, or empty — never throw. */
function stubNode() {
    const target = {};
    return new Proxy(target, {
        get(t, k) {
            if (k in t) return t[k];
            if (k === "innerHTML" || k === "textContent" || k === "value") return "";
            if (k === "options" || k === "children") return [];
            if (k === "dataset" || k === "style") return {};
            if (k === "classList") {
                return { add() {}, remove() {}, toggle() {}, contains: () => false };
            }
            if (k === "hidden" || k === "disabled" || k === "checked") return false;
            if (k === "length") return 0;
            if (k === "addEventListener" || k === "removeEventListener") return () => {};
            if (typeof k === "string" && /^(querySelector|querySelectorAll|closest|clonedNode)$/.test(k)) {
                return () => (k.endsWith("All") ? [] : null);
            }
            return () => undefined;
        },
        set(t, k, v) {
            t[k] = v;
            return true;
        },
    });
}

const nodes = {};
const sandbox = {
    console,
    document: {
        getElementById: (id) => (nodes[id] = nodes[id] || stubNode()),
        querySelector: () => null,
        querySelectorAll: () => [],
        addEventListener() {},
        createElement: () => stubNode(),
        body: stubNode(),
    },
    fetch: () => Promise.resolve({
        ok: true, status: 200,
        json: () => Promise.resolve({ instruments: [], total: 0 }),
    }),
    setTimeout: () => 0,
    clearTimeout() {},
    setInterval: () => 0,
    clearInterval() {},
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    URLSearchParams, Object, Array, JSON, Number, String, Promise, Boolean,
    Math, Date, RegExp, Error, isNaN, parseInt, parseFloat,
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(src, sandbox, { filename: "data_manager.js" });

let passed = 0;
async function test(name, fn) {
    await fn();
    passed += 1;
    console.log(`  ✓ ${name}`);
}

const tick = () => new Promise((r) => setImmediate(r));

/** A fresh Data-tab page whose coverage payload is canned, with its request
 *  URLs and the nodes it builds recorded. Mirrors SymbolPicker's harness. */
function mountWith(payload) {
    const seen = [];
    const built = [];
    // A node that can actually hold children and answer querySelector, so the
    // table path (`$('dm-inv-table').querySelector('tbody')`) is exercised
    // rather than silently skipped by a null stub.
    const makeNode = () => {
        const node = stubNode();
        const kids = {};
        node.appendChild = (child) => {
            node._appended.push(child);
            return child;
        };
        node.querySelector = (sel) => (kids[sel] = kids[sel] || makeNode());
        node.querySelectorAll = () => [];
        node._appended = [];
        return node;
    };
    const nodes = {};
    const document = {
        getElementById: (id) => (nodes[id] = nodes[id] || makeNode()),
        querySelector: () => null,
        querySelectorAll: () => [],
        addEventListener() {},
        createElement: () => {
            const el = makeNode();
            built.push(el);
            return el;
        },
        body: makeNode(),
    };
    const box = Object.assign({}, sandbox, {
        document,
        fetch: (url) => {
            seen.push(String(url));
            return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(payload) });
        },
        setTimeout: () => 0,
        clearTimeout() {},
    });
    box.globalThis = box;
    vm.createContext(box);
    vm.runInContext(src, box, { filename: "data_manager.js" });
    return {
        seen, built, nodes,
        settle: async () => { await tick(); await tick(); },
        // Everything the page rendered: the list is one big innerHTML string on
        // a looked-up node, the table is <tr> nodes appended to its tbody.
        rowHtml: () => built.concat(Object.values(nodes))
            .flatMap((el) => [String(el.innerHTML || "")]
                .concat((el._appended || []).map((c) => String(c.innerHTML || ""))))
            .join("\n"),
        /** How many <tr> the table actually appended. */
        tableRows: () => {
            const table = nodes["dm-inv-table"];
            const tbody = table && table.querySelector("tbody");
            return tbody ? (tbody._appended || []).length : 0;
        },
        /** Just the inventory table's body — the list and the table contain
         *  different things on purpose, so assertions must not be shared. */
        tableHtml: () => {
            const table = nodes["dm-inv-table"];
            const tbody = table && table.querySelector("tbody");
            if (!tbody) return "";
            return [String(tbody.innerHTML || "")]
                .concat((tbody._appended || []).map((c) => String(c.innerHTML || "")))
                .join("\n");
        },
    };
}

const COVERAGE = {
    instruments: [
        { symbol: "RELIANCE", name: "Reliance Industries Ltd.", instrument_type: "equity",
          curated: true, data_available: true, bars_count: 4817,
          from_date: "2026-09-02", to_date: "2026-09-30",
          timeframes_stored: ["1min"], timeframes_available: ["1min", "5min", "1day"],
          bars_by_timeframe: { "1min": 4817 },
          dates_by_timeframe: { "1min": { from: "2026-09-02", to: "2026-09-30" } } },
        { symbol: "NIFTY", name: "NIFTY 50", instrument_type: "index", curated: true,
          data_available: false, bars_count: 0, from_date: null, to_date: null,
          timeframes_stored: [], timeframes_available: [], bars_by_timeframe: {},
          dates_by_timeframe: {} },
        { symbol: "OBSCUREMIDCAP", name: "OBSCUREMIDCAP", instrument_type: "equity",
          curated: false, data_available: true, bars_count: 90,
          from_date: "2026-07-01", to_date: "2026-07-30",
          timeframes_stored: ["1day"], timeframes_available: ["1day", "1week"],
          bars_by_timeframe: { "1day": 90 },
          dates_by_timeframe: { "1day": { from: "2026-07-01", to: "2026-07-30" } } },
    ],
    total: 3, returned: 3, known_total: 203, available_total: 2, hidden_total: 0,
    db_available: true, catalogue_source: "universe+market_data_cache",
    instrument_types: ["equity", "index", "futures", "options"],
    hint: "No data loaded. Go to Data tab → fetch data for this symbol.",
    timeframes: ["1min", "5min", "10min", "15min", "30min", "1hour", "4hour", "1day", "1week"],
    generated_at: "2026-10-06",
};

const row = (over) => Object.assign({
    symbol: "RELIANCE", data_available: true, bars_count: 4817,
    timeframes_stored: ["1min"],
    timeframes_available: [
        "1min", "5min", "10min", "15min", "30min", "1hour", "4hour", "1day", "1week",
    ],
}, over);

test("the label prints the STORED timeframe and the bar count", () => {
    assert.equal(sandbox.dmDetail(row()), "1min · 4,817 bars");
});

test("the derived set never appears in the label", () => {
    // The regression: reading timeframes_available printed the same nine-item
    // list under every symbol, telling the user nothing about that symbol.
    const label = sandbox.dmDetail(row());
    for (const derived of ["5min", "15min", "1hour", "4hour", "1day", "1week"]) {
        assert.ok(!label.includes(derived), `${derived} leaked into "${label}"`);
    }
});

test("a daily-only symbol says 1day, not the two it can serve", () => {
    const label = sandbox.dmDetail(row({
        symbol: "NIFTY", bars_count: 1247,
        timeframes_stored: ["1day"], timeframes_available: ["1day", "1week"],
    }));
    assert.equal(label, "1day · 1,247 bars");
});

test("several stored timeframes are all listed, comma-free like the picker", () => {
    const label = sandbox.dmDetail(row({
        timeframes_stored: ["1min", "1day"],
        timeframes_available: ["1min", "1day", "1week"],
    }));
    assert.equal(label, "1min/1day · 4,817 bars");
});

test("a row with no data renders nothing at all", () => {
    assert.equal(sandbox.dmDetail(row({ data_available: false, bars_count: 0 })), "");
});

test("a missing timeframes_stored degrades to the bar count, never to the noise", () => {
    // An older server payload would omit the field. Showing only the bar count
    // is honest; falling back to timeframes_available would restore the bug.
    const label = sandbox.dmDetail(row({ timeframes_stored: undefined }));
    assert.equal(label, "4,817 bars");
});

// ---------------------------------------------------------------------------
// The window in the label
// ---------------------------------------------------------------------------

test("the row label carries the fetched window", () => {
    assert.equal(
        sandbox.dmDetail(row({ from_date: "2026-09-02", to_date: "2026-09-30" })),
        "1min · 4,817 bars · 02 Sep → 30 Sep"
    );
});

test("a row with no dates keeps its old label — no dangling separator", () => {
    assert.equal(sandbox.dmDetail(row()), "1min · 4,817 bars");
});

test("a partially known window still reads honestly", () => {
    assert.equal(sandbox.dmDetail(row({ from_date: "2026-09-02", to_date: null })),
        "1min · 4,817 bars · 02 Sep → ?");
});

test("dmWindow formats a date without a timezone round-trip", () => {
    // new Date("2026-09-02") parses as UTC midnight and renders as 01 Sep in
    // any negative-offset browser. String slicing cannot drift like that.
    assert.equal(sandbox.dmWindow({ from_date: "2026-01-01", to_date: "2026-12-31" }),
        "01 Jan → 31 Dec");
    assert.equal(sandbox.dmWindow({ from_date: null, to_date: null }), "");
});

// ---------------------------------------------------------------------------
// One request, two views
// ---------------------------------------------------------------------------

/** The coverage requests only — /api/data/status polling is a different thing. */
const coverageCalls = (seen) => seen.filter((u) => u.includes("/api/data/coverage"));

await test("the Data tab asks for the universe + bars, never the catalogue", async () => {
    const h = mountWith(COVERAGE);
    await h.settle();
    const calls = coverageCalls(h.seen);
    assert.equal(calls.length, 1, `expected one coverage request, got ${calls.length}`);
    assert.match(calls[0], /include_catalogue=0/);
    assert.ok(!calls[0].includes("curated=1"),
        "curated=1 would hide a symbol fetched outside the universe");
});

await test("the table renders from that payload — no second request, no inventory scan", async () => {
    const h = mountWith(COVERAGE);
    await h.settle();
    for (const url of h.seen) {
        assert.ok(!url.includes("/api/data/inventory"),
            "the inventory endpoint ran its own full GROUP BY scan of the same rows");
    }
    assert.equal(coverageCalls(h.seen).length, 1, "one payload per page load");
});

await test("the table shows every stored symbol, one row each, with its dates", async () => {
    const h = mountWith(COVERAGE);
    await h.settle();
    const table = h.tableHtml();
    assert.ok(table.includes("RELIANCE"), "a stored curated symbol");
    assert.ok(table.includes("OBSCUREMIDCAP"), "a stored symbol outside the universe");
    assert.ok(!table.includes("NIFTY"), "NIFTY has no bars and does not belong in the table");
    assert.ok(table.includes("2026-09-02") && table.includes("2026-09-30"));
    assert.ok(table.includes("4,817"), "the bar count");
    assert.equal(h.tableRows(), 2, "one row per stored symbol");
});

await test("the summary counts only symbols that hold bars", async () => {
    const h = mountWith(COVERAGE);
    await h.settle();
    assert.equal(h.nodes["dm-inv-symbols"].textContent, "2 symbols");
    assert.equal(h.nodes["dm-inv-bars"].textContent, "4,907 bars");
    assert.equal(h.nodes["dm-inv-timeframes"].textContent, "1min, 1day");
});

await test("the summary owns up when the page is showing a subset", async () => {
    const h = mountWith(Object.assign({}, COVERAGE, { total: 700 }));
    await h.settle();
    assert.match(h.nodes["dm-inv-symbols"].textContent, /2 symbols \(of 700 shown\)/);
});

await test("a symbol with no bars is still LISTED, marked as not fetched", async () => {
    const h = mountWith(COVERAGE);
    await h.settle();
    const html = h.rowHtml();
    assert.ok(html.includes(">NIFTY<"), "the fetchable-but-unfetched row must still be listed");
    assert.ok(html.includes("not fetched yet"));
});

console.log(`\ndata manager labels: ${passed} tests passed`);
