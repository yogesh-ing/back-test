import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const PnLDiary = require('../../src/backtest/web/static/js/diary.js');

test('buildPayoffQuadrantSvg renders hyperbolic breakeven curve and interactive entity bubbles', () => {
  const entities = [
    {
      entity_key: 'orb_v2::22222222',
      entity_label: 'Trend Winner [22222222]',
      trade_count: 12,
      win_rate_pct: 66.7,
      payoff_ratio: 2.5,
      plot_payoff_ratio: 2.5,
      unbounded_payoff: false,
      net_pnl: 3200,
      prescription_code: 'POSITIVE_EDGE',
      prescription_severity: 'positive',
    },
  ];
  const curve = [
    { win_rate_pct: 20, payoff_ratio: 4.0 },
    { win_rate_pct: 50, payoff_ratio: 1.0 },
    { win_rate_pct: 100, payoff_ratio: 0.0 },
  ];

  const svg = PnLDiary.buildPayoffQuadrantSvg(entities, curve, 'orb_v2::22222222', 4.0);
  assert.ok(svg.includes('<svg'));
  assert.ok(svg.includes('diary-be-curve'));
  assert.ok(svg.includes('diary-qbubble is-selected'));
  assert.ok(svg.includes('Trend Winner [22222222]'));
  assert.ok(svg.includes('+₹3,200.00'));
});

test('buildHoldDurationTableHtml renders single commission sum, fee hurdle, and drag meter', () => {
  const buckets = [
    {
      key: 'lt_1m',
      label: '< 1m',
      subtitle: 'Churn Watch',
      trade_count: 5,
      trade_share_pct: 100,
      win_rate_pct: 0,
      gross_pnl: 200,
      commission: 350,
      avg_commission_per_trade: 70,
      profit_drag_pct: 175,
      drag_band: 'critical',
      net_pnl: -150,
      expectancy_net: -30,
    },
  ];
  const totals = {
    trade_count: 5,
    win_rate_pct: 0,
    gross_pnl: 200,
    commission: 350,
    avg_commission_per_trade: 70,
    profit_drag_pct: 175,
    drag_band: 'critical',
    net_pnl: -150,
    expectancy_net: -30,
  };

  const html = PnLDiary.buildHoldDurationTableHtml(buckets, totals, 'Paper');
  assert.ok(html.includes('Churn Watch'));
  assert.ok(html.includes('₹350.00'));
  assert.ok(html.includes('₹70.00'));
  assert.ok(html.includes('data-band="critical"'));
  assert.ok(html.includes('−₹150.00'));
});
