/**
 * F6 — the diary scroll cue.
 *
 * The attribution tree is wider than its card, and until this slice the extra
 * 80px simply sat off-screen: "Running MTM" was cut mid-glyph with nothing to
 * say so. markScrollCues() names the columns that are still out of view and
 * makes the box focusable, and hides the cue again once they are not.
 *
 * diary.js is required directly (the module exports itself under Node), so the
 * DOM is stubbed here only with the handful of members the function touches.
 *
 * Usage: node tests/js/test_diary_scroll_cue.mjs
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
// diary.js prints strategy keys through the shared resolver (finding F11)
globalThis.Labels = require('../../src/backtest/web/static/js/components/labels.js');
const PnLDiary = require('../../src/backtest/web/static/js/diary.js');

function makeTh(label, left, right) {
    return {
        _rect: { left, right, width: right - left, top: 0, bottom: 24, height: 24 },
        textContent: label,
        getBoundingClientRect() { return this._rect; },
    };
}

/** A .diary-table-wrap: a scroll box of `clientWidth` holding `columns`. */
function makeWrap({ id, boxLeft, boxRight, scrollLeft = 0, columns, visible = true }) {
    const wrap = {
        id,
        attrs: {},
        hidden: false,
        // In the real DOM tabIndex and the tabindex attribute are the same thing,
        // so the stub keeps them together — otherwise "the focus stop goes away"
        // would pass for the wrong reason.
        set tabIndex(v) { this.attrs.tabindex = String(v); },
        get tabIndex() { return this.attrs.tabindex === undefined ? -1 : Number(this.attrs.tabindex); },
        parentNode: { inserted: [], insertBefore(node, ref) { node.parentNode = this; this.inserted.push(node); } },
        _scrollLeft: scrollLeft,
        get clientWidth() { return boxRight - boxLeft; },
        getBoundingClientRect() {
            return visible
                ? { left: boxLeft, right: boxRight, width: boxRight - boxLeft, top: 0, bottom: 400, height: 400 }
                : { left: 0, right: 0, width: 0, top: 0, bottom: 0, height: 0 };
        },
        querySelectorAll(selector) {
            assert.equal(selector, 'thead th');
            // Columns sit at their natural place in the content box; scrolling the
            // box moves them left relative to the viewport.
            return columns.map((c) => makeTh(c.label, c.left - this._scrollLeft, c.right - this._scrollLeft));
        },
        setAttribute(key, value) { this.attrs[key] = String(value); },
        getAttribute(key) {
            return Object.prototype.hasOwnProperty.call(this.attrs, key) ? this.attrs[key] : null;
        },
        removeAttribute(key) { delete this.attrs[key]; },
    };
    return wrap;
}

function withDocument(wraps, run) {
    const created = [];
    const previous = globalThis.document;
    globalThis.document = {
        createElement(tag) {
            assert.equal(tag, 'p');
            const node = { tagName: tag, className: '', textContent: '', hidden: false, parentNode: null };
            created.push(node);
            return node;
        },
        querySelectorAll(selector) {
            assert.equal(selector, '.diary-table-wrap');
            return wraps;
        },
    };
    try {
        run(created);
    } finally {
        if (previous === undefined) delete globalThis.document;
        else globalThis.document = previous;
    }
}

test('a column cut off by the card is named, and the box becomes focusable', () => {
    const wrap = makeWrap({
        id: 'diary-attr-tree-wrap', boxLeft: 263, boxRight: 985,
        columns: [
            { label: 'Strategy / Spawned Instance', left: 263, right: 500 },
            { label: 'Net P&L', left: 900, right: 1024 },      // 39px past the box
            { label: 'Running MTM', left: 1024, right: 1121 },  // fully out of view
        ],
    });
    withDocument([wrap], (created) => {
        PnLDiary.markScrollCues();
        assert.equal(created.length, 1, 'one cue is inserted, not one per column');
        const cue = created[0];
        assert.equal(cue.className, 'diary-scroll-cue');
        assert.equal(cue.hidden, false);
        assert.match(cue.textContent, /Net P&L/);
        assert.match(cue.textContent, /Running MTM/);
        assert.doesNotMatch(cue.textContent, /Strategy/, 'a column that fits is not reported');
        assert.equal(wrap.parentNode.inserted[0], cue, 'the cue sits above the table, not inside the scroller');
        assert.equal(wrap.tabIndex, 0, 'keyboard users must be able to scroll it');
        assert.equal(wrap.attrs.role, 'region');
        assert.ok(wrap.attrs['aria-label']);
    });
});

test('scrolling to the end clears the cue', () => {
    const wrap = makeWrap({
        id: 'diary-attr-tree-wrap', boxLeft: 263, boxRight: 985, scrollLeft: 136,
        columns: [
            { label: 'Strategy / Spawned Instance', left: 263, right: 500 },
            { label: 'Net P&L', left: 900, right: 1024 },
            { label: 'Running MTM', left: 1024, right: 1121 },
        ],
    });
    withDocument([wrap], () => {
        PnLDiary.markScrollCues();
        assert.equal(wrap._diaryScrollCue.hidden, true, 'nothing left to warn about');
        assert.equal(wrap._diaryScrollCue.textContent, '');
        assert.equal(wrap.attrs.tabindex, undefined, 'the focus stop goes away with the reason for it');
    });
});

test('a table in a hidden tab is not measured, so no cue is invented for it', () => {
    const wrap = makeWrap({
        id: 'diary-payoff-table-wrap', boxLeft: 0, boxRight: 0, visible: false,
        columns: [{ label: 'Payoff R (BE)', left: 0, right: 900 }],
    });
    withDocument([wrap], (created) => {
        PnLDiary.markScrollCues();
        assert.equal(created.length, 0);
        assert.equal(wrap._diaryScrollCue, undefined);
    });
});

test('the cue is reused across renders instead of stacking', () => {
    const wrap = makeWrap({
        id: 'diary-attr-tree-wrap', boxLeft: 263, boxRight: 985,
        columns: [{ label: 'Running MTM', left: 900, right: 1121 }],
    });
    withDocument([wrap], (created) => {
        PnLDiary.markScrollCues();
        PnLDiary.markScrollCues();
        PnLDiary.markScrollCues();
        assert.equal(created.length, 1, 'one cue element for three passes');
    });
});
