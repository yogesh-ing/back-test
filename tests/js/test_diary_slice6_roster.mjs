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

test("Slice 6 Safeguards: renders Session Breadth, H2 Trajectory, Tail Co-Crash & builds Journal Audit Snapshot", () => {
  const rosterPayload = {
    mode: "paper",
    from_date: "2026-10-01",
    to_date: "2026-10-30",
    constraints: {
      live_capital: 300000,
      reserve_pct: 20,
      deployable_budget: 240000,
      max_underlying_pct: 40,
      max_correlation: 0.65,
      max_live_runners: 5,
      min_session_days: 5,
      block_decaying_edge: true,
      max_tail_overlap: 3,
    },
    summary: {
      selected_count: 1,
      warm_bench_count: 1,
      held_session_breadth_count: 1,
      held_recent_decay_count: 1,
      pruned_duplicate_or_corr_count: 1,
      pruned_tail_crash_count: 1,
      allocated_capital_inr: 90000,
      reserve_inr: 60000,
    },
    selected_roster: [
      {
        roster_slot: 1,
        rollout_wave: 1,
        decision: "SELECTED_LIVE_ROSTER",
        decision_label: "#1 Live Roster · Wave 1",
        decision_tone: "positive",
        instance_label: "Anchor NIFTY 5m",
        strategy: "orb_breakout",
        primary_underlying: "NIFTY",
        timeframe: "5min",
        required_capital: 90000,
        composite_score: 86.2,
        score_breakdown: { return_over_dd: 3.8, trajectory_penalty_pts: 0 },
        active_session_days: 16,
        trajectory: { status: "STEADY_OR_EXPANDING", status_label: "H2 Steady", status_tone: "positive" },
        sim_to_real: { status_label: "Strong Retention (84%)", status_tone: "positive", has_backtest: false },
        net_pnl: 14200,
        commission: 820,
        max_drawdown_inr: 3700,
        gates_passed: 6,
        gates_total: 6,
      },
    ],
    warm_bench: [
      {
        instance_label: "Backup TCS 15m",
        decision: "DEFERRED_SLOT_CAP",
        decision_label: "Warm Bench · Slot Cap",
      },
    ],
    candidates: [
      {
        roster_slot: 1,
        rollout_wave: 1,
        decision: "SELECTED_LIVE_ROSTER",
        decision_label: "#1 Live Roster · Wave 1",
        decision_tone: "positive",
        instance_label: "Anchor NIFTY 5m",
        strategy: "orb_breakout",
        primary_underlying: "NIFTY",
        timeframe: "5min",
        required_capital: 90000,
        composite_score: 86.2,
        score_breakdown: { return_over_dd: 3.8, trajectory_penalty_pts: 0 },
        active_session_days: 16,
        trajectory: { status: "STEADY_OR_EXPANDING", status_label: "H2 Steady", status_tone: "positive" },
        sim_to_real: { status_label: "Strong Retention (84%)", status_tone: "positive", has_backtest: false },
        net_pnl: 14200,
        commission: 820,
        max_drawdown_inr: 3700,
        gates_passed: 6,
        gates_total: 6,
      },
      {
        decision: "PRUNED_TAIL_CRASH_OVERLAP",
        decision_label: "Pruned · Tail Crash (3/3d)",
        decision_tone: "caution",
        decision_reason: "Shares 3/3 worst red IST sessions with Anchor NIFTY 5m.",
        instance_label: "Tail Peer HDFCBANK",
        strategy: "vwap_revert",
        primary_underlying: "HDFCBANK",
        timeframe: "15min",
        required_capital: 65000,
        composite_score: 71.0,
        score_breakdown: { return_over_dd: 2.4, trajectory_penalty_pts: -6 },
        active_session_days: 14,
        trajectory: { status: "COOLING_RECENT_EDGE", status_label: "H2 Cooling", status_tone: "caution" },
        sim_to_real: { status_label: "Paper-Only", status_tone: "neutral", has_backtest: false },
        max_corr_with_selected: 0.34,
        max_corr_peer: "Anchor NIFTY 5m",
        max_tail_overlap_days: 3,
        net_pnl: 8400,
        commission: 610,
        max_drawdown_inr: 3500,
        gates_passed: 6,
        gates_total: 6,
      },
    ],
  };

  const tableHtml = PnLDiary.buildRosterTableHtml(rosterPayload, "Paper");
  assert.match(tableHtml, /Sessions: 16d/);
  assert.match(tableHtml, /H2 Steady/);
  assert.match(tableHtml, /H2 Cooling/);
  assert.match(tableHtml, /Tail: 3\/3d/);
  assert.match(tableHtml, /H2 -6/);

  const noteBody = PnLDiary.buildRosterJournalNotePayload(rosterPayload, "2026-10-30", "paper");
  assert.equal(noteBody.mode, "paper");
  assert.equal(noteBody.session_date, "2026-10-30");
  assert.deepEqual(Array.from(noteBody.regime_tags), ["RosterPlan", "Graduation"]);
  assert.match(noteBody.instance_label, /Live Roster Plan \(1\/5 slots\)/);
  assert.match(noteBody.note_text, /LIVE ROSTER AUDIT SNAPSHOT/);
  assert.match(noteBody.note_text, /Anchor NIFTY 5m/);
  assert.match(noteBody.note_text, /Backup TCS 15m/);
  assert.match(noteBody.note_text, /Tail Co-Crash 1/);
  assert.doesNotMatch(noteBody.note_text, /\b(STT|GST|SEBI|Stamp Duty)\b/i);
});

