import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const PnLDiary = require('../../src/backtest/web/static/js/diary.js');

test('buildGraduationTableHtml renders 6 gate chips, single commission sum, verdict badge, and option advisory', () => {
  const instances = [
    {
      instance_key: 'theta_strangle::22222222',
      strategy: 'theta_strangle',
      instance_label: 'Index Strangle Alpha [22222222]',
      portfolio_id: '22222222-0000-0000-0000-000000000000',
      lifecycle_status: 'RUNNING',
      portfolio_deep_link: '/portfolio/paper?instance=22222222-0000-0000-0000-000000000000',
      config_badges: ['5min', 'strangle'],
      trade_count: 21,
      expectancy_net: 421.67,
      profit_factor: 2.38,
      unbounded_pf: false,
      commission_drag_pct: 9.5,
      max_drawdown_pct: 3.12,
      max_drawdown_inr: 9360,
      commission: 945,
      net_pnl: 8855,
      gates_passed: 6,
      gates_total: 6,
      gates: {
        sample_size: { passed: true, target: '>= 20 trades' },
        net_expectancy: { passed: true, target: '> ₹0.00 / trade' },
        profit_factor: { passed: true, target: '>= 1.35' },
        commission_drag: { passed: true, target: '<= 35%' },
        max_drawdown: { passed: true, target: '<= 10.0% cap & OK' },
        ledger_integrity: { passed: true, target: '0 mismatches' },
      },
      verdict: 'PROMOTE_CANDIDATE',
      verdict_label: 'Promote Candidate',
      verdict_tone: 'positive',
      rationale: 'All 6 institutional gates passed.',
      entitlement_advisory: {
        requires_live_entitlement_review: true,
        message: 'Uses [strangle]; config/risk_limits.yaml live entitlement currently permits [long_call, long_put] only.',
      },
    },
  ];

  const html = PnLDiary.buildGraduationTableHtml(instances, 'Paper');
  assert.ok(html.includes('Index Strangle Alpha [22222222]'));
  assert.ok(html.includes('6/6 passed'));
  assert.ok(html.includes('₹945.00'));
  assert.ok(html.includes('+₹8,855.00'));
  assert.ok(html.includes('Promote Candidate'));
  assert.ok(html.includes('config/risk_limits.yaml live entitlement'));
  assert.ok(html.includes('data-journal-prefill-instance="Index Strangle Alpha [22222222]"'));
});

test('buildJournalFeedHtml renders IST session entries, regime tags, and escapes untrusted HTML', () => {
  const notes = [
    {
      note_id: 'aaaa1111-2222-3333-4444-555566667777',
      mode: 'paper',
      session_date: '2026-10-30',
      strategy_name: 'orb_options',
      instance_label: 'NIFTY Runner [11111111]',
      regime_tags: ['Trend', 'Expiry'],
      note_text: 'Clean breakout <script>alert(1)</script> & zero slippage',
      updated_ist_label: '30-Oct 15:40 IST',
    },
  ];

  const html = PnLDiary.buildJournalFeedHtml(notes, 'Paper');
  assert.ok(html.includes('2026-10-30 IST'));
  assert.ok(html.includes('orb_options'));
  assert.ok(html.includes('NIFTY Runner [11111111]'));
  assert.ok(html.includes('Trend'));
  assert.ok(html.includes('Expiry'));
  assert.ok(!html.includes('<script>'));
  assert.ok(html.includes('&lt;script&gt;alert(1)&lt;/script&gt;'));
  assert.ok(html.includes('data-edit-note="aaaa1111-2222-3333-4444-555566667777"'));
  assert.ok(html.includes('data-delete-note="aaaa1111-2222-3333-4444-555566667777"'));
});
