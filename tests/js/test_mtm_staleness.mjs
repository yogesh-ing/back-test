/**
 * MTM Staleness Observability — JS unit & DOM rendering tests (R2, R7, AC #2, #7, #8, #10).
 *
 * Usage: node --test tests/js/test_mtm_staleness.mjs
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import assert from "node:assert/strict";

const root = fileURLToPath(new URL("../../", import.meta.url));
const loadText = (rel) => readFileSync(path.join(root, rel), "utf8");

const OptionView = new Function(
  loadText("src/backtest/web/static/js/components/option_config.js") + "\n" +
  loadText("src/backtest/web/static/js/components/option_view.js") + "\n" +
  "return OptionView;",
)();

let passed = 0;
function check(name, fn) {
  try {
    fn();
    passed += 1;
    console.log(`  ✓ ${name}`);
  } catch (err) {
    console.error(`  ✗ ${name}\n    ${err.message}`);
    process.exitCode = 1;
  }
}

check("staleTooltip formats mark_ts and quote_error per R2", () => {
  const tip = OptionView.staleTooltip({
    mark_stale: true,
    mark_ts: "2026-10-01T09:16:00+00:00",
    quote_error: "unknown option contract '49228'",
  });
  assert.match(tip, /^last good quote .+ — unknown option contract '49228'$/);
});

check("staleBadgeHtml renders nothing when mark_stale is false and badge when true", () => {
  assert.equal(OptionView.staleBadgeHtml({ mark_stale: false }), "");
  const html = OptionView.staleBadgeHtml({
    mark_stale: true,
    mark_ts: "2026-10-01T09:16:00+00:00",
    quote_error: "session expired",
  });
  assert.match(html, /badge-stale-mark/);
  assert.match(html, /⚠ stale mark/);
  assert.match(html, /last good quote .+ — session expired/);
});

check("structureRows propagates markStale, markTs, and quoteError to structure and legs", () => {
  const runner = {
    instrument: { type: "option" },
    options: {
      mark_stale: true,
      open_structures_detail: [
        {
          symbol: "NIFTY long_call",
          structure_type: "long_call",
          side: "LONG",
          qty: 1,
          units: 25,
          lot_size: 25,
          entry_price: 100,
          current_price: 100,
          unrealized_pnl: 0,
          entry_cost: 2500,
          open_pnl_pct: 0,
          strikes: [24800],
          expiry: "2026-10-29",
          bars_held: 3,
          mark_ts: "2026-10-01T09:15:00+00:00",
          mark_stale: true,
          quote_error: "broker timeout",
          legs_detail: [
            {
              side: "LONG",
              option_type: "CE",
              strike: 24800,
              trading_symbol: "NIFTY261024800CE",
              qty: 25,
              entry_price: 100,
              current_price: 100,
              pnl: 0,
              mark_ts: "2026-10-01T09:15:00+00:00",
              mark_stale: true,
              quote_error: "broker timeout",
            },
          ],
        },
      ],
    },
  };
  assert.equal(OptionView.isMarkStale(runner), true);
  assert.equal(OptionView.staleCount(runner), 1);
  const rows = OptionView.structureRows(runner);
  assert.equal(rows[0].markStale, true);
  assert.equal(rows[0].quoteError, "broker timeout");
  assert.equal(rows[0].legs[0].markStale, true);
  assert.equal(rows[0].legs[0].quoteError, "broker timeout");
});

check("portfolio.js fires toast once per healthy → stale episode and renders Refresh marks button", async () => {
  const store = {};
  const listeners = {};
  const toasts = [];
  let sseCallback = null;

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
      classList: { add() {}, remove() {}, contains() { return false; } },
      addEventListener(type, fn) {
        (listeners[id + ":" + type] = listeners[id + ":" + type] || []).push(fn);
      },
      querySelectorAll() { return []; },
      querySelector() { return null; },
      getContext() { return {}; },
      appendChild() {},
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
  globalThis.showToast = (msg, kind) => toasts.push({ msg, kind });
  globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
  globalThis.requestAnimationFrame = (fn) => fn();
  globalThis.setInterval = () => 0;
  globalThis.EventSource = class {
    addEventListener(ev, fn) {
      if (ev === "portfolio") sseCallback = fn;
    }
    close() {}
  };
  globalThis.Money = {
    symbol: "₹",
    format: (v) => "₹" + Math.round(Number(v) || 0).toLocaleString("en-IN"),
    signed: (v) => (Number(v) < 0 ? "−" : "+") + "₹" + Math.round(Math.abs(Number(v) || 0)).toLocaleString("en-IN"),
  };
  globalThis.fetch = () => Promise.resolve({
    ok: true,
    status: 200,
    json: () => Promise.resolve({ success: true, portfolio: { runners: [], positions: [], buckets: {} } }),
  });

  new Function(loadText("src/backtest/web/static/js/components/option_config.js"))();
  new Function(loadText("src/backtest/web/static/js/components/option_view.js"))();
  new Function(loadText("src/backtest/web/static/js/deep_dive.js"))();
  new Function(loadText("src/backtest/web/static/js/portfolio.js"))();

  (listeners["DOMContentLoaded"] || []).forEach((fn) => fn());
  assert.ok(sseCallback, "SSE listener registered");

  const makePayload = (isStale, epId) => ({
    total_capital: 500000,
    total_equity: 500000,
    deployed_capital: 25000,
    deployed_pct: 0.05,
    daily_pnl: 0,
    daily_pnl_pct: 0,
    realized_pnl: 0,
    open_positions: 1,
    stale_marks_count: isStale ? 1 : 0,
    runner_count: 1,
    running: 1,
    paused: 0,
    daily_loss_pct: 0,
    daily_loss_used: 0,
    daily_loss_limit: 50000,
    buckets: {},
    runners: [
      {
        instance_id: "r1",
        name: "NIFTY-OPT",
        strategy_name: "directional_options",
        target_type: "SINGLE_SYMBOL",
        target_label: "NIFTY",
        symbols: ["NIFTY"],
        symbol_count: 1,
        timeframe: "1min",
        allocated_capital: 500000,
        open_pnl: 0,
        daily_pnl: 0,
        open_positions: 1,
        status: "RUNNING",
        mode: "paper",
        source: "mstock",
        mark_stale: isStale,
        mark_ts: "2026-10-01T09:15:00+00:00",
        quote_error: isStale ? "unknown option contract '49228'" : null,
        stale_positions: isStale ? 1 : 0,
        instrument: { type: "option", expression: { type: "long_call" } },
        options: {
          open_positions: 1,
          open_structures: 1,
          mark_stale: isStale,
          mark_ts: "2026-10-01T09:15:00+00:00",
          quote_error: isStale ? "unknown option contract '49228'" : null,
          stale_positions: isStale ? 1 : 0,
          stale_episode_id: isStale ? epId : 0,
          open_structures_detail: [],
        },
      },
    ],
    positions: [
      {
        instance_id: "r1",
        runner: "NIFTY-OPT",
        status: "RUNNING",
        stale: false,
        kind: "option",
        symbol: "NIFTY long_call",
        label: "NIFTY long_call 24800",
        side: "LONG",
        qty: 1,
        entry_price: 120,
        current_price: 120,
        unrealized_pnl: 0,
        mark_stale: isStale,
        mark_ts: "2026-10-01T09:15:00+00:00",
        quote_error: isStale ? "unknown option contract '49228'" : null,
        legs: [],
      },
    ],
  });

  // 1. Healthy tick → 0 toasts
  sseCallback({ data: JSON.stringify(makePayload(false, 0)) });
  assert.equal(toasts.length, 0);
  assert.equal(store["m-positions"].textContent, "1 active");

  // 2. 10 consecutive stale ticks in episode #1 → exactly 1 toast (AC #8)
  for (let i = 0; i < 10; i++) {
    sseCallback({ data: JSON.stringify(makePayload(true, 1)) });
  }
  assert.equal(toasts.length, 1);
  assert.match(toasts[0].msg, /Stale mark on NIFTY-OPT/);
  assert.equal(store["m-positions"].textContent, "1 pos · 1 stale mark");
  assert.match(store["pos-summary"].innerHTML, /1 stale mark/);
  assert.match(store["matrix-body"].innerHTML, /↻ Refresh marks/);

  // 3. Recovery tick → clears episode; next stale episode #2 fires toast #2
  sseCallback({ data: JSON.stringify(makePayload(false, 0)) });
  sseCallback({ data: JSON.stringify(makePayload(true, 2)) });
  assert.equal(toasts.length, 2);
});

console.log(`${passed} tests passed`);
