import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const PnLDiary = require('../../src/backtest/web/static/js/diary.js');

const SAMPLE_STRATEGIES = [
  {
    strategy_name: 'immediate_strangle',
    net_pnl: -8000,
    commission: 500,
    trade_count: 10,
    win_rate_pct: 20,
    profit_factor: 0.25,
    unbounded_pf: false,
    instances: [
      { instance_key: 'immediate_strangle::a', net_pnl: -3000, commission: 200, trade_count: 4, win_rate_pct: 25, profit_factor: 0.4 },
      { instance_key: 'immediate_strangle::b', net_pnl: -5000, commission: 300, trade_count: 6, win_rate_pct: 16.7, profit_factor: 0.15 },
    ],
  },
  {
    strategy_name: 'vwap_reversion',
    net_pnl: 1450,
    commission: 120,
    trade_count: 5,
    win_rate_pct: 100,
    profit_factor: null,
    unbounded_pf: true,
    instances: [
      { instance_key: 'vwap_reversion::c', net_pnl: 1450, commission: 120, trade_count: 5, win_rate_pct: 100, profit_factor: null, unbounded_pf: true },
    ],
  },
  {
    strategy_name: 'ema_pullback',
    net_pnl: -1200,
    commission: 750,
    trade_count: 15,
    win_rate_pct: 46.7,
    profit_factor: 0.85,
    unbounded_pf: false,
    instances: [],
  },
];

test('sortAttributionStrategies sorts both strategies and nested instances without mutating input', () => {
  const worstFirst = PnLDiary.sortAttributionStrategies(SAMPLE_STRATEGIES, 'net_asc');
  assert.deepEqual(
    worstFirst.map((s) => s.strategy_name),
    ['immediate_strangle', 'ema_pullback', 'vwap_reversion']
  );
  // Nested instances under immediate_strangle are also sorted worst-first (-5000 before -3000)
  assert.deepEqual(
    worstFirst[0].instances.map((i) => i.instance_key),
    ['immediate_strangle::b', 'immediate_strangle::a']
  );
  // Original array order untouched
  assert.equal(SAMPLE_STRATEGIES[0].instances[0].instance_key, 'immediate_strangle::a');

  const byComm = PnLDiary.sortAttributionStrategies(SAMPLE_STRATEGIES, 'comm_desc');
  assert.deepEqual(
    byComm.map((s) => s.strategy_name),
    ['ema_pullback', 'immediate_strangle', 'vwap_reversion']
  );

  const byPf = PnLDiary.sortAttributionStrategies(SAMPLE_STRATEGIES, 'pf_desc');
  // unbounded_pf (all winners) ranks highest, followed by 0.85, then 0.25
  assert.deepEqual(
    byPf.map((s) => s.strategy_name),
    ['vwap_reversion', 'ema_pullback', 'immediate_strangle']
  );
});
