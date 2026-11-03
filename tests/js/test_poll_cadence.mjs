/**
 * Tests for PollCadence ("Option 1+") & chrome poller integration.
 *
 * Verifies:
 *   1. Fail-open when __lastMarketState is missing (never throttles in the dark).
 *   2. Paper / Live / Synthetic active-runner override: activeRunners > 0 keeps
 *      every poller at full live speed even on POST_CLOSE / WEEKEND / HOLIDAY.
 *   3. Quiet after-hours, weekend, holiday, logged-out, and hidden-tab cadences.
 *   4. Multi-broker safety: secondary authenticated/expiring_soon broker keeps
 *      session expiry checks active even when primary broker is unauthenticated.
 *   5. Data backfill independence: dataStatusDelay(true) stays 3s/6s post-close,
 *      and notifyDataJobStarted() fires instant wake listeners.
 *   6. SSE deduplication & market_chip.js market:state transition event.
 */

import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import vm from "node:vm";

const root = fileURLToPath(new URL("../../", import.meta.url));
const pollCadenceSrc = readFileSync(
  path.join(root, "src/backtest/web/static/js/components/poll_cadence.js"),
  "utf8",
);
const marketChipSrc = readFileSync(
  path.join(root, "src/backtest/web/static/js/components/market_chip.js"),
  "utf8",
);

function createCadenceSandbox({ pathname = "/", hidden = false, marketState = null } = {}) {
  const dispatched = [];
  const document = {
    hidden,
    dispatchEvent(ev) {
      dispatched.push(ev);
    },
    addEventListener() {},
  };
  const sandbox = {
    console,
    Date,
    Math,
    Number,
    String,
    Boolean,
    Object,
    Array,
    RegExp,
    CustomEvent: class CustomEvent {
      constructor(type, opts) {
        this.type = type;
        this.detail = opts && opts.detail;
      }
    },
    document,
    location: { pathname },
    __lastMarketState: marketState,
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(pollCadenceSrc, sandbox, { filename: "poll_cadence.js" });
  return { sandbox, PollCadence: sandbox.PollCadence, document, dispatched };
}

// Helper: build a UTC Date corresponding to an exact IST wall-clock time on 2026-10-30 (Friday)
function istTime(hh, mm, ss = 0) {
  const pad = (n) => String(n).padStart(2, "0");
  return new Date(`2026-10-30T${pad(hh)}:${pad(mm)}:${pad(ss)}+05:30`);
}

test("PollCadence fails open when __lastMarketState is missing (never throttles blindly)", () => {
  const { PollCadence } = createCadenceSandbox({ pathname: "/portfolio/paper", marketState: null });
  const snap = PollCadence.resolveMarketSnapshot();
  assert.equal(snap.known, false);
  assert.equal(snap.isOpenOrPreOpen, true);
  assert.equal(PollCadence.marketStatusDelay(), 30000);
  assert.equal(PollCadence.riskStripDelay({ sseActive: false, pathname: "/portfolio/paper" }), 5000);
  assert.equal(PollCadence.alertWidgetDelay({ pathname: "/portfolio/paper" }), 10000);
  assert.equal(PollCadence.brokerStatusDelay({ status: "authenticated" }), 60000);
});

test("PollCadence throttles idle POST_CLOSE / WEEKEND / HOLIDAY tabs with 0 active runners", () => {
  const postCloseAt = istTime(20, 15, 0); // 20:15 IST after market close
  const { sandbox, PollCadence } = createCadenceSandbox({
    pathname: "/diary",
    marketState: { day_state: "TRADING_DAY", session_state: "POST_CLOSE" },
  });

  assert.equal(PollCadence.isSessionLiveOrActive({ now: postCloseAt }), false);
  assert.equal(PollCadence.marketStatusDelay({ now: postCloseAt }), 5 * 60 * 1000);
  assert.equal(PollCadence.riskStripDelay({ sseActive: false, pathname: "/diary", now: postCloseAt }), 5 * 60 * 1000);
  assert.equal(PollCadence.alertWidgetDelay({ pathname: "/diary", now: postCloseAt }), 5 * 60 * 1000);
  assert.equal(PollCadence.landingRiskDelay({ now: postCloseAt }), 5 * 60 * 1000);
  assert.equal(PollCadence.brokerStatusDelay({ status: "authenticated" }, { now: postCloseAt }), 5 * 60 * 1000);

  // Weekend extends market-status sync to 30m
  sandbox.__lastMarketState = { day_state: "WEEKEND", session_state: "POST_CLOSE" };
  assert.equal(PollCadence.marketStatusDelay({ now: istTime(11, 0, 0) }), 30 * 60 * 1000);
});

test("Active Paper/Live runner override keeps live cadence even after market close or on weekends", () => {
  const weekendAt = istTime(14, 0, 0);
  const { PollCadence } = createCadenceSandbox({
    pathname: "/portfolio/paper",
    marketState: { day_state: "WEEKEND", session_state: "POST_CLOSE" },
  });

  // Simulate an active after-hours Paper/Synthetic runner from risk/SSE snapshot
  PollCadence.noteRiskSnapshot({ runners: [{ instance_id: "p1", state: "running" }] });
  assert.equal(PollCadence.getActiveRunners(), 1);
  assert.equal(PollCadence.isSessionLiveOrActive({ now: weekendAt }), true);

  assert.equal(PollCadence.riskStripDelay({ sseActive: false, pathname: "/portfolio/paper", now: weekendAt }), 5000);
  assert.equal(PollCadence.alertWidgetDelay({ pathname: "/portfolio/paper", now: weekendAt }), 10000);
  assert.equal(PollCadence.brokerStatusDelay({ status: "authenticated" }, { now: weekendAt }), 60000);
  assert.equal(PollCadence.landingRiskDelay({ now: weekendAt }), 10000);

  // Backgrounded trading tab with an active runner keeps 30s safety heartbeat instead of pausing
  const hiddenTrading = createCadenceSandbox({
    pathname: "/portfolio/live",
    hidden: true,
    marketState: { day_state: "TRADING_DAY", session_state: "OPEN" },
  });
  hiddenTrading.PollCadence.noteSseFrame({ running: 2 }, 1000);
  assert.equal(hiddenTrading.PollCadence.riskStripDelay({ sseActive: false, pathname: "/portfolio/live" }), 30000);
  assert.equal(hiddenTrading.PollCadence.alertWidgetDelay({ pathname: "/portfolio/live" }), 30000);
});

test("Multi-broker brokerStatusDelay pauses only when all brokers are logged out and keeps 60s on expiring_soon", () => {
  const postCloseAt = istTime(21, 0, 0);
  const { PollCadence } = createCadenceSandbox({
    pathname: "/",
    marketState: { day_state: "TRADING_DAY", session_state: "POST_CLOSE" },
  });

  // Logged out across all brokers -> null (0 background polls)
  assert.equal(
    PollCadence.brokerStatusDelay(
      {
        status: "unauthenticated",
        sessions: {
          mstock: { status: "unauthenticated", authenticated: false },
          dhan: { status: "expired", authenticated: false },
        },
      },
      { now: postCloseAt },
    ),
    null,
  );

  // Primary unauthenticated, secondary Dhan authenticated -> stays active (5m post-close, 60s open)
  const secondaryAuth = {
    status: "unauthenticated",
    sessions: {
      mstock: { status: "unauthenticated", authenticated: false },
      dhan: { status: "authenticated", authenticated: true },
    },
  };
  assert.equal(PollCadence.brokerStatusDelay(secondaryAuth, { now: postCloseAt }), 5 * 60 * 1000);
  assert.equal(PollCadence.brokerStatusDelay(secondaryAuth, { now: istTime(10, 30, 0) }), 60000);

  // Secondary broker expiring_soon -> stays at 60s even post-close so expiry toast is never missed
  const secondaryExpiring = {
    status: "unauthenticated",
    sessions: {
      mstock: { status: "unauthenticated", authenticated: false },
      dhan: { status: "expiring_soon", authenticated: true },
    },
  };
  assert.equal(PollCadence.brokerStatusDelay(secondaryExpiring, { now: postCloseAt }), 60000);
});

test("Data fetch status is never gated on market hours and wakes immediately on job start", () => {
  const { PollCadence, dispatched } = createCadenceSandbox({
    pathname: "/data",
    marketState: { day_state: "WEEKEND", session_state: "POST_CLOSE" },
  });

  // Running backfill on a weekend stays fast (3s visible)
  assert.equal(PollCadence.dataStatusDelay(true), 3000);
  // Idle visible tab polls every 120s
  assert.equal(PollCadence.dataStatusDelay(false), 120000);

  let woke = 0;
  PollCadence.onDataJobWake(() => { woke += 1; });
  PollCadence.notifyDataJobStarted();
  assert.equal(woke, 1);
  assert.equal(dispatched.some((ev) => ev.type === "data:job-started"), true);
});

test("Hidden idle tab pauses all 6 background chrome pollers (0 requests/min)", () => {
  const { PollCadence } = createCadenceSandbox({
    pathname: "/diary",
    hidden: true,
    marketState: { day_state: "TRADING_DAY", session_state: "POST_CLOSE" },
  });

  assert.equal(PollCadence.marketStatusDelay({ now: istTime(22, 0, 0) }), null);
  assert.equal(PollCadence.brokerStatusDelay({ status: "authenticated" }, { now: istTime(22, 0, 0) }), null);
  assert.equal(PollCadence.dataStatusDelay(false), null);
  assert.equal(PollCadence.dataStatusDelay(true), 6000); // active download still tracks every 6s
  assert.equal(PollCadence.riskStripDelay({ sseActive: false, pathname: "/diary", now: istTime(22, 0, 0) }), null);
  assert.equal(PollCadence.alertWidgetDelay({ pathname: "/diary", now: istTime(22, 0, 0) }), null);
  assert.equal(PollCadence.landingRiskDelay({ now: istTime(22, 0, 0) }), null);
});

test("SSE freshness suppresses duplicate /api/market/status polls and market_chip emits market:state transitions", () => {
  const { sandbox, PollCadence, dispatched } = createCadenceSandbox({
    pathname: "/portfolio/paper",
    marketState: { day_state: "TRADING_DAY", session_state: "OPEN" },
  });

  PollCadence.noteSseFrame({ running: 1 }, 50000);
  assert.equal(PollCadence.marketStatusDelay({ nowMs: 55000, now: istTime(11, 0, 0) }), null);
  assert.equal(PollCadence.marketStatusDelay({ nowMs: 70000, now: istTime(11, 0, 0) }), 5 * 60 * 1000);

  // Verify market_chip.js dispatches market:state on state transitions
  const els = {
    "market-status-chip": { id: "market-status-chip", dataset: {}, setAttribute() {}, getAttribute() {} },
    "market-status-text": { id: "market-status-text", textContent: "" },
  };
  sandbox.document.getElementById = (id) => els[id] || null;
  sandbox.setTimeout = () => 0;
  sandbox.clearTimeout = () => {};
  sandbox.setInterval = () => 0;
  sandbox.clearInterval = () => {};
  sandbox.JSON = JSON;
  vm.runInContext(marketChipSrc, sandbox, { filename: "market_chip.js" });

  dispatched.length = 0;
  sandbox.window.updateMarketChip({ day_state: "WEEKEND", session_state: "POST_CLOSE", is_open: false });
  assert.equal(dispatched.filter((e) => e.type === "market:state").length, 1);
  // Repeating identical state must not spam duplicate transition events
  sandbox.window.updateMarketChip({ day_state: "WEEKEND", session_state: "POST_CLOSE", is_open: false });
  assert.equal(dispatched.filter((e) => e.type === "market:state").length, 1);
});
