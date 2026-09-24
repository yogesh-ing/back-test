/* Shared helpers for the optimization pages (setup + run).
 *
 * Pure functions (formatting, grid maths, colour scales) are exported on
 * globalThis.OptCommon so tests/js/test_optimize_common.mjs can exercise
 * them without a browser. DOM/fetch helpers degrade gracefully in Node.
 */
const OptCommon = (() => {
    'use strict';

    const OBJECTIVE_LABELS = {
        sharpe: 'Sharpe', sortino: 'Sortino', calmar: 'Calmar',
        total_return: 'Total return', profit_factor: 'Profit factor', expectancy: 'Expectancy',
    };
    const METRIC_LABELS = {
        score: 'Objective score', sharpe: 'Sharpe', sortino: 'Sortino', calmar: 'Calmar',
        total_return: 'Total return', cagr: 'CAGR', max_drawdown: 'Max drawdown',
        drawdown_duration_days: 'DD duration (d)', profit_factor: 'Profit factor',
        win_rate: 'Win rate', total_trades: 'Trades', winning_trades: 'Winners',
        losing_trades: 'Losers', expectancy: 'Expectancy', avg_win: 'Avg win',
        avg_loss: 'Avg loss', largest_win: 'Largest win', largest_loss: 'Largest loss',
        volatility: 'Volatility', downside_deviation: 'Downside dev.',
        avg_holding_time_minutes: 'Avg hold (min)', final_equity: 'Final equity',
    };
    /** Metrics stored as decimal fractions → shown as %. */
    const FRACTION_METRICS = new Set(['total_return', 'cagr', 'max_drawdown', 'volatility',
        'downside_deviation']);
    /** Metrics where a LOWER value is better (for colouring deltas). */
    const LOWER_IS_BETTER = new Set(['max_drawdown_mag', 'drawdown_duration_days', 'volatility',
        'downside_deviation']);
    const INTEGER_METRICS = new Set(['total_trades', 'winning_trades', 'losing_trades',
        'drawdown_duration_days', 'avg_holding_time_minutes']);
    const MONEY_METRICS = new Set(['expectancy', 'avg_win', 'avg_loss', 'largest_win',
        'largest_loss', 'final_equity']);

    function isNum(v) { return v !== null && v !== undefined && v !== '' && Number.isFinite(Number(v)); }

    function escapeHtml(value) {
        return String(value ?? '').replace(/[&<>"']/g, (c) => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
        }[c]));
    }

    function fmtNum(v, dp = 2) {
        if (!isNum(v)) return '—';
        return Number(v).toLocaleString(undefined, { minimumFractionDigits: dp, maximumFractionDigits: dp });
    }

    function fmtPct(v, dp = 1, { fraction = true, signed = false } = {}) {
        if (!isNum(v)) return '—';
        const n = Number(v) * (fraction ? 100 : 1);
        const s = signed && n > 0 ? '+' : '';
        return `${s}${n.toFixed(dp)}%`;
    }

    function fmtMoney(v, dp = 0) {
        if (!isNum(v)) return '—';
        if (globalThis.Money && typeof globalThis.Money.format === 'function') {
            return globalThis.Money.format(v, dp);
        }
        return Number(v).toFixed(dp);
    }

    /** Format a metric by name, using the right unit. */
    function fmtMetric(name, v) {
        if (!isNum(v)) return '—';
        if (name === 'win_rate') return fmtPct(v, 1, { fraction: false });
        if (FRACTION_METRICS.has(name)) return fmtPct(v, name === 'max_drawdown' ? 1 : 2);
        if (MONEY_METRICS.has(name)) return fmtMoney(v, 0);
        if (INTEGER_METRICS.has(name)) return String(Math.round(Number(v)));
        return fmtNum(v, name === 'profit_factor' ? 2 : 3);
    }

    /** Signed difference of a metric, in the metric's own unit. */
    function fmtDelta(name, d) {
        if (!isNum(d)) return '—';
        const n = Number(d);
        const sign = n > 0 ? '+' : (n < 0 ? '-' : '');
        if (FRACTION_METRICS.has(name)) return fmtPct(n, 2, { signed: true });
        if (name === 'win_rate') return `${sign}${Math.abs(n).toFixed(1)} pp`;
        if (MONEY_METRICS.has(name)) return `${sign}${fmtMoney(Math.abs(n), 0)}`;
        if (INTEGER_METRICS.has(name)) return `${sign}${Math.abs(Math.round(n)).toLocaleString()}`;
        return `${sign}${fmtNum(Math.abs(n), name === 'profit_factor' ? 2 : 3)}`;
    }

    function fmtDuration(seconds) {
        if (!isNum(seconds)) return '—';
        let s = Math.max(0, Math.round(Number(seconds)));
        if (s < 60) return `${s}s`;
        const h = Math.floor(s / 3600); s -= h * 3600;
        const m = Math.floor(s / 60); s -= m * 60;
        if (h) return `${h}h ${String(m).padStart(2, '0')}m`;
        return `${m}m ${String(s).padStart(2, '0')}s`;
    }

    function fmtDate(iso) {
        if (!iso) return '—';
        const d = new Date(iso);
        if (Number.isNaN(d.getTime())) return String(iso);
        return d.toLocaleString(undefined, { year: 'numeric', month: 'short', day: '2-digit',
            hour: '2-digit', minute: '2-digit' });
    }

    function fmtParams(params) {
        if (!params) return '—';
        return Object.entries(params).map(([k, v]) => `${k.replace(/^engine\./, '⚙')}=${v}`).join(', ');
    }

    /** Decimal-safe count of values in [min, max] by step (mirrors ParameterSpec.size). */
    function gridCount(min, max, step) {
        const lo = Number(min), hi = Number(max), st = Number(step);
        if (![lo, hi, st].every(Number.isFinite) || st <= 0 || hi < lo) return 0;
        const dec = Math.max(decimals(lo), decimals(st), decimals(hi));
        const f = 10 ** Math.min(dec, 10);
        return Math.floor((Math.round(hi * f) - Math.round(lo * f)) / Math.round(st * f)) + 1;
    }

    function decimals(v) {
        const s = String(v);
        if (s.includes('e-')) return Number(s.split('e-')[1]);
        const i = s.indexOf('.');
        return i < 0 ? 0 : s.length - i - 1;
    }

    /** Product of per-param grid sizes (checked params only). */
    function totalCombinations(rows) {
        return rows.filter((r) => r.optimize).reduce((acc, r) => acc * Math.max(gridCount(r.min, r.max, r.step), 0), 1);
    }

    const STATUS_CLASS = {
        completed: 'opt-status-ok', running: 'opt-status-run', pending: 'opt-status-wait',
        paused: 'opt-status-wait', draft: 'opt-status-muted', cancelled: 'opt-status-muted',
        failed: 'opt-status-bad',
    };

    function statusBadge(status) {
        const cls = STATUS_CLASS[status] || 'opt-status-muted';
        return `<span class="opt-status ${cls}">${escapeHtml(status || '—')}</span>`;
    }

    /** Red → amber → green colour for t ∈ [0, 1] (heatmaps). */
    function heatColor(t) {
        if (!Number.isFinite(t)) return 'transparent';
        const x = Math.min(1, Math.max(0, t));
        // hsl hue 0 (red) → 50 (amber) → 140 (green)
        const hue = x < 0.5 ? x * 2 * 50 : 50 + (x - 0.5) * 2 * 90;
        return `hsl(${hue.toFixed(0)}, 70%, ${(32 + x * 8).toFixed(0)}%)`;
    }

    /** Normalise a 2-D array (nulls allowed) into [0, 1]. */
    function normalise(z) {
        const vals = z.flat().filter((v) => isNum(v)).map(Number);
        if (!vals.length) return { min: null, max: null, scale: () => NaN };
        const min = Math.min(...vals), max = Math.max(...vals);
        const span = max - min || 1;
        return { min, max, scale: (v) => (isNum(v) ? (Number(v) - min) / span : NaN) };
    }

    function robustnessLabel(score) {
        if (!isNum(score)) return { text: 'n/a', cls: 'muted' };
        const s = Number(score);
        if (s >= 7) return { text: 'Robust', cls: 'pos' };
        if (s >= 4) return { text: 'Moderate', cls: 'opt-warn-text' };
        return { text: 'Fragile', cls: 'neg' };
    }

    /** Improvement direction for a metric delta (true = better). */
    function isImprovement(name, before, after) {
        if (!isNum(before) || !isNum(after)) return null;
        if (name === 'max_drawdown') return Number(after) > Number(before); // less negative
        if (LOWER_IS_BETTER.has(name)) return Number(after) < Number(before);
        return Number(after) > Number(before);
    }

    // ---------------------------------------------------------------- DOM / fetch

    async function api(url, opts = {}) {
        const init = { headers: { 'Content-Type': 'application/json' }, ...opts };
        if (init.body && typeof init.body !== 'string') init.body = JSON.stringify(init.body);
        const res = await fetch(url, init);
        let data = null;
        try { data = await res.json(); } catch (_) { data = null; }
        if (!res.ok || (data && data.success === false)) {
            const err = new Error((data && data.error) || `HTTP ${res.status}`);
            err.status = res.status;
            err.errors = data && data.errors;
            throw err;
        }
        return data;
    }

    function toast(msg, type = 'success', ms) {
        if (typeof globalThis.showToast === 'function') globalThis.showToast(msg, type, ms);
        else if (typeof console !== 'undefined') console.log(`[${type}] ${msg}`);
    }

    function debounce(fn, ms = 300) {
        let t = null;
        return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
    }

    return {
        OBJECTIVE_LABELS, METRIC_LABELS, FRACTION_METRICS,
        isNum, escapeHtml, fmtNum, fmtPct, fmtMoney, fmtMetric, fmtDelta, fmtDuration, fmtDate, fmtParams,
        gridCount, totalCombinations, statusBadge, heatColor, normalise, robustnessLabel,
        isImprovement, api, toast, debounce,
    };
})();

if (typeof globalThis !== 'undefined') globalThis.OptCommon = OptCommon;
