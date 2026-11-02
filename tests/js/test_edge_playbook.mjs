/**
 * Tests for Institutional Edge & Performance Playbook UI Manual.
 *
 * Verifies:
 *   1. Zero trading/network blast radius: edge_playbook.js contains no fetch,
 *      XMLHttpRequest, EventSource, or background setInterval timers.
 *   2. E2 Single-Sum Commission compliance in edge_playbook_modal.html.
 *   3. Tab switching, ARIA state, keyboard navigation (ArrowRight/Left/Home/End/Escape),
 *      keyword filtering across tabs, and ?playbook=1&playbook_tab=... deep-linking.
 */

import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";
import vm from "node:vm";

const root = fileURLToPath(new URL("../../", import.meta.url));
const jsSrc = readFileSync(
  path.join(root, "src/backtest/web/static/js/components/edge_playbook.js"),
  "utf8",
);
const modalHtml = readFileSync(
  path.join(root, "src/backtest/web/templates/components/edge_playbook_modal.html"),
  "utf8",
);

function buildPlaybookDom({ search = "" } = {}) {
  const byIdMap = {};
  const openTriggers = [];
  const closeTriggers = [];
  const allSearchable = [];

  function makeClassList() {
    const set = new Set();
    return {
      add(c) { set.add(c); },
      remove(c) { set.delete(c); },
      contains(c) { return set.has(c); },
    };
  }

  function makeEl(id, extra = {}) {
    const attrs = Object.assign({}, extra.attrs || {});
    const handlers = {};
    const el = {
      id,
      hidden: Boolean(extra.hidden),
      value: "",
      tabIndex: extra.tabIndex ?? 0,
      textContent: extra.textContent || "",
      dataset: {},
      classList: makeClassList(),
      setAttribute(k, v) { attrs[k] = String(v); },
      getAttribute(k) { return k in attrs ? attrs[k] : null; },
      addEventListener(type, fn) { (handlers[type] = handlers[type] || []).push(fn); },
      fire(type, ev = {}) {
        (handlers[type] || []).forEach((fn) => fn(Object.assign({ preventDefault() {} }, ev)));
      },
      focus() { el._focused = true; },
      querySelectorAll(sel) {
        if (sel === "[data-epb-searchable]" && Array.isArray(el._items)) return el._items;
        if (sel === "[data-epb-tab]" && Array.isArray(el._tabs)) return el._tabs;
        if (sel === '[data-close="edge-playbook-modal"]') return closeTriggers;
        if (sel === "[data-epb-searchable]") return allSearchable;
        return [];
      },
    };
    if (id) byIdMap[id] = el;
    return el;
  }

  const modal = makeEl("edge-playbook-modal", { hidden: true });
  makeEl("epb-filter-input");
  makeEl("epb-search-banner", { hidden: true });

  const tabKeys = ["synergy", "params", "workflow", "prescriptions", "roster", "formulas"];
  const tabButtons = tabKeys.map((k, idx) =>
    makeEl("epb-tab-btn-" + k, {
      attrs: { "data-epb-tab": k, "aria-selected": idx === 0 ? "true" : "false" },
      tabIndex: idx === 0 ? 0 : -1,
    }),
  );
  const tablist = makeEl("epb-tablist");
  tablist._tabs = tabButtons;

  const panelSamples = {
    synergy: ["Macro Time-Series Analytics vs Micro Attribution PnL Diary"],
    params: ["Net Profit Factor >= 1.35x and Sample >= 20 trades"],
    workflow: ["11:30-13:30 Midday Chop entry blackout window"],
    prescriptions: ["CHURN_FRICTION_DRAG positive gross flipped by commission"],
    roster: ["Constrained Live Roster Optimizer daily correlation 0.65 cap"],
    formulas: ["Net P&L = Gross P&L - Commission single combined sum"],
  };

  tabKeys.forEach((k, idx) => {
    const panel = makeEl("epb-panel-" + k, { hidden: idx !== 0 });
    const item = makeEl("", { textContent: panelSamples[k][0] });
    panel._items = [item];
    allSearchable.push(item);
  });

  const openBtn = makeEl("diary-open-playbook", {
    attrs: { "data-open-edge-playbook": "params" },
  });
  openTriggers.push(openBtn);

  const closeBtn = makeEl("epb-close-btn", {
    attrs: { "data-close": "edge-playbook-modal" },
  });
  closeTriggers.push(closeBtn);

  const docHandlers = {};
  const document = {
    readyState: "complete",
    getElementById: (id) => byIdMap[id] || null,
    querySelectorAll(sel) {
      if (sel === "[data-open-edge-playbook]") return openTriggers;
      return [];
    },
    addEventListener(type, fn) { (docHandlers[type] = docHandlers[type] || []).push(fn); },
    fire(type, ev = {}) {
      (docHandlers[type] || []).forEach((fn) => fn(Object.assign({ preventDefault() {} }, ev)));
    },
  };

  const sandbox = {
    console,
    Array,
    String,
    Object,
    URLSearchParams,
    document,
    location: { search },
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(jsSrc, sandbox, { filename: "edge_playbook.js" });

  return { sandbox, EdgePlaybook: sandbox.EdgePlaybook, byIdMap, modal, tablist, openBtn, closeBtn, document };
}

test("edge_playbook.js is strictly read-only with zero network calls or background timers", () => {
  assert.doesNotMatch(jsSrc, /\bfetch\s*\(/);
  assert.doesNotMatch(jsSrc, /\bXMLHttpRequest\b/);
  assert.doesNotMatch(jsSrc, /\bEventSource\b/);
  assert.doesNotMatch(jsSrc, /\bsetInterval\s*\(/);
});

test("edge_playbook_modal.html honors single-h1 workspace rule and E2 single-sum commission contract", () => {
  assert.doesNotMatch(modalHtml, /<h1\b/i, "Modal must not introduce a second <h1> on any page");
  assert.match(modalHtml, /Gross P&amp;L &minus; Commission|Gross − Comm/);
  assert.match(modalHtml, /one single combined sum/i);
  assert.match(modalHtml, /1\.35×/);
  assert.match(modalHtml, /CHURN_FRICTION_DRAG/);
  assert.match(modalHtml, /\/analytics/);
  assert.match(modalHtml, /\/diary/);
});

test("EdgePlaybook opens targeted tab on trigger click, supports keyboard tab navigation, and closes on Escape", () => {
  const { byIdMap, modal, tablist, openBtn, document } = buildPlaybookDom();
  assert.equal(modal.hidden, true);

  openBtn.fire("click");
  assert.equal(modal.hidden, false);
  assert.equal(byIdMap["epb-tab-btn-params"].getAttribute("aria-selected"), "true");
  assert.equal(byIdMap["epb-panel-params"].hidden, false);
  assert.equal(byIdMap["epb-panel-synergy"].hidden, true);

  // ArrowRight moves params -> workflow
  tablist.fire("keydown", { key: "ArrowRight" });
  assert.equal(byIdMap["epb-tab-btn-workflow"].getAttribute("aria-selected"), "true");
  assert.equal(byIdMap["epb-panel-workflow"].hidden, false);

  // End jumps to formulas
  tablist.fire("keydown", { key: "End" });
  assert.equal(byIdMap["epb-tab-btn-formulas"].getAttribute("aria-selected"), "true");

  // Escape closes modal
  document.fire("keydown", { key: "Escape" });
  assert.equal(modal.hidden, true);
});

test("EdgePlaybook live filter narrows matching rules and auto-switches to matching tab", () => {
  const { EdgePlaybook, byIdMap } = buildPlaybookDom();
  EdgePlaybook.open("synergy");

  const res = EdgePlaybook.applyFilter("CHURN_FRICTION_DRAG");
  assert.equal(res.totalMatches, 1);
  assert.deepEqual(Array.from(res.matchedTabs), ["prescriptions"]);
  assert.equal(byIdMap["epb-tab-btn-prescriptions"].getAttribute("aria-selected"), "true");
  assert.equal(byIdMap["epb-search-banner"].hidden, false);

  EdgePlaybook.applyFilter("");
  assert.equal(byIdMap["epb-search-banner"].hidden, true);
});

test("EdgePlaybook auto-opens requested section when URL carries ?playbook=1&playbook_tab=workflow", () => {
  const { modal, byIdMap } = buildPlaybookDom({ search: "?playbook=1&playbook_tab=workflow" });
  assert.equal(modal.hidden, false);
  assert.equal(byIdMap["epb-tab-btn-workflow"].getAttribute("aria-selected"), "true");
  assert.equal(byIdMap["epb-panel-workflow"].hidden, false);
});
