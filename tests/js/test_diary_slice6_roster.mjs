/**
 * Node tests for Slice 6 — Constrained Live Roster Optimizer & Backtest-vs-Paper
 * Reality Matrix UI builders (`PnLDiary.buildRosterAllocStripHtml` &
 * `PnLDiary.buildRosterTableHtml`).
 */

import test from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import path from "node:path";

const require = createRequire(import.meta.url);
const root = fileURLToPath(new URL("../../", import.meta.url));
const PnLDiary = require(path.join(root, "src/backtest/web/static/js/diary.js"));

test("buildRosterAllocStripHtml renders deployable budget, margin reserve, and underlying mix", () => {
  const html = PnLDiary.buildRosterAllocStripHtml({
    constraints: {
      live_capital: 300000,
      deployable_budget: 240000,
      reserve_pct: 20,
    },
    summary: {
      allocated_capital_inr: 170000,
      reserve_inr: 60000,
      backtest_runs_indexed: 4,
      underlying_mix: [
        { underlying: "NIFTY", allocated_inr: 90000, share_of_live_pct: 30.0 },
        { underlying: "BANKNIFTY", allocated_inr: 80000, share_of_live_pct: 26.7 },
      ],
    },
  });

  assert.match(html, /Deployable Used:/);
  assert.match(html, /1,70,000\.00/);
  assert.match(html, /2,40,000\.00/);
  assert.match(html, /Margin Reserve:/);
  assert.match(html, /NIFTY: ₹90,000\.00 \(30\.0%\)/);
  assert.match(html, /Saved Backtests Indexed: 4/);
});

test("buildRosterTableHtml renders Live Roster winners, pruned clones, Backtest->Paper retention, and single Commission sum", () => {
  const html = PnLDiary.buildRosterTableHtml(
    {
      candidates: [
        {
          rank: 1,
          roster_slot: 1,
          decision: "SELECTED_LIVE_ROSTER",
          decision_label: "#1 Live Roster",
          decision_tone: "positive",
          decision_reason: "Wave 1 · Deploy Week 1 (1× min lot)",
          instance_label: "ORB NIFTY <5m>",
          strategy: "orb_breakout",
          primary_underlying: "NIFTY",
          timeframe: "5min",
          required_capital: 90000,
          capital_is_estimated: false,
          composite_score: 86.4,
          score_breakdown: { return_over_dd: 4.8 },
          sim_to_real: {
            has_backtest: true,
            status_label: "Retained (84% PF)",
            status_tone: "positive",
            win_rate_drift_pp: -2.1,
            benchmark: {
              profit_factor: 2.1,
              monte_carlo_profit_prob_pct: 85,
            },
          },
          max_corr_with_selected: null,
          gates_passed: 6,
          gates_total: 6,
          profit_factor: 1.76,
          unbounded_pf: false,
          net_pnl: 14250.5,
          commission: 840.25,
          max_drawdown_inr: 2950.0,
          max_drawdown_pct: 3.3,
        },
        {
          rank: 2,
          roster_slot: null,
          decision: "PRUNED_CORRELATED_CLONE",
          decision_label: "Pruned · Corr ρ=+0.84",
          decision_tone: "caution",
          decision_reason: "Daily IST Net P&L correlation exceeds 0.65 cap",
          instance_label: "Momentum Clone",
          strategy: "ema_momentum",
          primary_underlying: "FINNIFTY",
          timeframe: "5min",
          required_capital: 60000,
          capital_is_estimated: true,
          composite_score: 74.2,
          score_breakdown: { return_over_dd: 3.1 },
          sim_to_real: {
            has_backtest: false,
            status_label: "Paper-Only (No Saved BT)",
            status_tone: "neutral",
          },
          max_corr_with_selected: 0.84,
          max_corr_peer: "#1 ORB NIFTY <5m>",
          gates_passed: 6,
          gates_total: 6,
          profit_factor: 1.52,
          unbounded_pf: false,
          net_pnl: 8100.0,
          commission: 620.0,
          max_drawdown_inr: 2600.0,
          max_drawdown_pct: 4.3,
        },
      ],
    },
    "Paper",
  );

  assert.match(html, /#1 Live Roster/);
  assert.match(html, /ORB NIFTY &lt;5m&gt;/, "Instance labels must be HTML-escaped");
  assert.match(html, /BT PF 2\.10&times; &rarr; 1\.76&times;/);
  assert.match(html, /MC 85%/);
  assert.match(html, /Anchor #1/);
  assert.match(html, /\+0\.84/);
  assert.match(html, /Commission/);
  // E2 Single-Sum Commission guard: never emit split tax columns
  assert.doesNotMatch(html, /\b(STT|GST|SEBI|Stamp Duty)\b/i);
});
