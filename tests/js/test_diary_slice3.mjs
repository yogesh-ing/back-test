import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const PnLDiary = require('../../src/backtest/web/static/js/diary.js');

test('computeMatrixCellHeat scales positive/negative cells and buildMatrixHtml renders reconciled 2dp totals', () => {
  assert.equal(PnLDiary.computeMatrixCellHeat(0, 1000, 0), '');
  assert.equal(PnLDiary.computeMatrixCellHeat(-900, 1000, 4), 'mheat-neg-4');
  assert.equal(PnLDiary.computeMatrixCellHeat(200, 1000, 2), 'mheat-pos-1');

  const matrix = {
    max_abs_cell_net: 1800,
    columns: [
      { key: 'take_profit', label: 'Take Profit' },
      { key: 'stop_loss', label: 'Stop Loss' },
    ],
    rows: [
      {
        row_key: 'scalp_strangle',
        row_label: 'scalp_strangle',
        totals: { trade_count: 6, win_rate_pct: 0, commission: 300, net_pnl: -1840 },
        cells: {
          take_profit: { trade_count: 2, win_rate_pct: 0, commission: 100, net_pnl: -40 },
          stop_loss: { trade_count: 4, win_rate_pct: 0, commission: 200, net_pnl: -1800 },
        },
      },
    ],
    column_totals: {
      take_profit: { trade_count: 2, win_rate_pct: 0, commission: 100, net_pnl: -40 },
      stop_loss: { trade_count: 4, win_rate_pct: 0, commission: 200, net_pnl: -1800 },
    },
    grand_totals: { trade_count: 6, win_rate_pct: 0, commission: 300, net_pnl: -1840 },
  };

  const html = PnLDiary.buildMatrixHtml(matrix, 'Strategy', 'Paper');
  assert.ok(html.includes('scalp_strangle'));
  assert.ok(html.includes('−₹1,840.00'));
  assert.ok(html.includes('Cost ₹300.00'));
  assert.ok(html.includes('mheat-neg-4'));
});
