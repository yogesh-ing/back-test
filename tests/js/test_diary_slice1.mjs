import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const PnLDiary = require('../../src/backtest/web/static/js/diary.js');

test('PnLDiary formats 2dp signed and absolute INR without rounding away paisa', () => {
  assert.equal(PnLDiary.fmtInrAbs(670.51), '₹670.51');
  assert.equal(PnLDiary.fmtInrSigned(-10520.51), '−₹10,520.51');
  assert.equal(PnLDiary.fmtInrSigned(2212.6), '+₹2,212.60');
  assert.equal(PnLDiary.fmtInrSigned(0.001), '₹0.00');
});

test('PnLDiary heatmap intensity tiers scale cleanly across positive and negative days', () => {
  assert.equal(PnLDiary.computeHeatClass(0, 5000, 0), '');
  assert.equal(PnLDiary.computeHeatClass(500, 5000, 3), 'level-pos-1');
  assert.equal(PnLDiary.computeHeatClass(2000, 5000, 3), 'level-pos-2');
  assert.equal(PnLDiary.computeHeatClass(-3200, 5000, 4), 'level-neg-3');
  assert.equal(PnLDiary.computeHeatClass(-5000, 5000, 13), 'level-neg-4');
});

test('PnLDiary calendar month bounds and Monday week anchor match IST calendar dates', () => {
  assert.deepEqual(PnLDiary.monthBounds(2026, 10), {
    from: '2026-10-01',
    to: '2026-10-31',
  });
  // 2026-10-01 is Thursday -> Monday is 2026-09-28
  assert.equal(PnLDiary.mondayOfWeekIso('2026-10-01'), '2026-09-28');
  // 2026-10-30 is Friday -> Monday is 2026-10-26
  assert.equal(PnLDiary.mondayOfWeekIso('2026-10-30'), '2026-10-26');
});
