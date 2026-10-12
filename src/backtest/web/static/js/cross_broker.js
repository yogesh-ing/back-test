/**
 * Cross-broker analytics controller (PRD-003).
 *
 * Renders the two new collapsible sections on /analytics — "By Broker" and
 * "Execution Quality" — plus three modals (Compare / Migration / Recommend).
 * No new nav item: the page is the existing one, this is the broker dimension
 * the per-strategy table structurally cannot express.
 *
 * API:
 *   GET  /api/analytics/cross-broker/summary?period=&mode=
 *   GET  /api/analytics/cross-broker/execution?broker=&strategy=&period=&mode=
 *   POST /api/analytics/cross-broker/compare
 *   POST /api/analytics/cross-broker/migration-impact
 *   POST /api/analytics/cross-broker/recommend-broker
 *
 * Two rules this file follows everywhere, because the analytics gap review
 * (docs/archive/ANALYTICS-TAB-GAPS.md) was written about exactly this class
 * of page:
 *
 *  1. **Nothing user-supplied reaches innerHTML unescaped.** Broker names come
 *     from config, runner names do not — but rejection reasons and segment
 *     labels do, and they are all `esc()`-ed.
 *  2. **"No data" is never rendered as a number.** The API returns `null` for
 *     an undefined metric; `fmt*` renders that as "—" (or "n/a" where a
 *     number was expected), never 0.00. A fill rate of 0% and "we have never
 *     seen an order here" are different facts.
 */

(function () {
    'use strict';

    const API = {
        summary: '/api/analytics/cross-broker/summary',
        execution: '/api/analytics/cross-broker/execution',
        compare: '/api/analytics/cross-broker/compare',
        migration: '/api/analytics/cross-broker/migration-impact',
        recommend: '/api/analytics/cross-broker/recommend-broker',
    };

    // ------------------------------------------------------------------
    // Formatting
    // ------------------------------------------------------------------

    function esc(value) {
        return String(value == null ? '' : value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    const DASH = '—';

    function isNum(v) {
        return v !== null && v !== undefined && v !== '' && Number.isFinite(Number(v));
    }

    function fmtNum(v, digits) {
        return isNum(v) ? Number(v).toFixed(digits == null ? 2 : digits) : DASH;
    }

    function fmtPct(v, digits) {
        return isNum(v) ? `${Number(v).toFixed(digits == null ? 1 : digits)}%` : DASH;
    }

    function fmtInr(v) {
        if (!isNum(v)) return DASH;
        const n = Number(v);
        return `${n < 0 ? '-' : ''}₹${Math.abs(Math.round(n)).toLocaleString('en-IN')}`;
    }

    function fmtBps(v) {
        return isNum(v) ? `${Number(v).toFixed(1)} bps` : DASH;
    }

    function fmtSec(v) {
        return isNum(v) ? `${Number(v).toFixed(1)}s` : DASH;
    }

    function fmtInt(v) {
        return isNum(v) ? Number(v).toLocaleString('en-IN') : DASH;
    }

    function labelFor(name) {
        const key = String(name == null ? '' : name);
        if (!key) return 'Unassigned';
        const known = { mstock: 'mStock', dhan: 'Dhan', paper: 'Paper (sim)' };
        if (known[key.toLowerCase()]) return known[key.toLowerCase()];
        return key.charAt(0).toUpperCase() + key.slice(1);
    }

    /** "statistically significant" / "needs more data" / "not tested" — in words. */
    function sigText(sig) {
        if (!sig) return 'not tested';
        if (sig.testable && sig.significant) {
            const p = Number(sig.p_value);
            const shown = p < 0.0001 ? 'p<0.0001' : `p=${p.toFixed(4)}`;
            return `${shown} · significant`;
        }
        if (sig.testable) {
            const p = Number(sig.p_value);
            return `${Number.isFinite(p) ? `p=${p.toFixed(3)}` : 'p n/a'} · not significant`;
        }
        return sig.reason || 'not tested';
    }

    // ------------------------------------------------------------------
    // State
    // ------------------------------------------------------------------

    const state = {
        period: '30d',
        mode: 'all',
        summary: null,
        execution: null,
        brokers: [],
        strategies: [],
        loadedSummary: false,
        loadedExecution: false,
    };

    const el = (id) => document.getElementById(id);

    // ------------------------------------------------------------------
    // Networking
    // ------------------------------------------------------------------

    async function getJson(url) {
        const res = await fetch(url);
        const data = await res.json().catch(() => ({}));
        if (!res.ok || data.success === false) {
            throw new Error(data.error || `Request failed (${res.status})`);
        }
        return data;
    }

    async function postJson(url, body) {
        const res = await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok || data.success === false) {
            throw new Error(data.error || `Request failed (${res.status})`);
        }
        return data;
    }

    function query(base) {
        const params = new URLSearchParams({ period: state.period });
        if (state.mode && state.mode !== 'all') params.set('mode', state.mode);
        return `${base}?${params.toString()}`;
    }

    // ------------------------------------------------------------------
    // Section 1 — cross-broker portfolio + By Broker
    // ------------------------------------------------------------------

    function renderPortfolioTotals(payload) {
        const total = payload.portfolio_total || {};
        const pnlEl = el('crossBrokerTotalPnl');
        const pnlSub = el('crossBrokerTotalPnlSub');
        if (pnlEl) {
            pnlEl.textContent = fmtInr(total.total_pnl);
            pnlEl.style.color = (total.total_pnl || 0) >= 0 ? 'var(--success)' : 'var(--danger)';
        }
        if (pnlSub) {
            const sign = (total.total_return_pct || 0) >= 0 ? '+' : '';
            pnlSub.textContent = isNum(total.total_return_pct)
                ? `${sign}${Number(total.total_return_pct).toFixed(2)}% on ${fmtInr(total.total_capital_deployed)}`
                : '—';
        }

        const sharpeEl = el('crossBrokerSharpe');
        if (sharpeEl) sharpeEl.textContent = fmtNum(total.sharpe_ratio);
        const sharpeSub = el('crossBrokerSharpeSub');
        if (sharpeSub) {
            sharpeSub.textContent = isNum(total.sortino_ratio)
                ? `Sortino ${fmtNum(total.sortino_ratio)}`
                : 'Sortino n/a (no downside deviation)';
        }

        const ddEl = el('crossBrokerMaxDd');
        if (ddEl) {
            ddEl.textContent = isNum(total.max_drawdown_pct)
                ? `-${Number(total.max_drawdown_pct).toFixed(2)}%`
                : DASH;
        }
        const ddSub = el('crossBrokerMaxDdSub');
        if (ddSub) ddSub.textContent = fmtInr(total.max_drawdown_amount);

        const tradesEl = el('crossBrokerTrades');
        if (tradesEl) tradesEl.textContent = fmtInt(total.total_trades);
        const tradesSub = el('crossBrokerTradesSub');
        if (tradesSub) {
            tradesSub.textContent = isNum(total.win_rate)
                ? `${Number(total.win_rate).toFixed(1)}% win rate`
                : 'no closed trades';
        }

        const pill = el('crossBrokerScopePill');
        if (pill) {
            pill.textContent = `${total.broker_count || 0} broker${total.broker_count === 1 ? '' : 's'} · `
                + `${total.strategy_count || 0} strateg${total.strategy_count === 1 ? 'y' : 'ies'}`;
        }

        renderDataQuality(el('crossBrokerDataQuality'), payload.data_quality);
    }

    function renderDataQuality(node, dq) {
        if (!node) return;
        if (!dq) {
            node.textContent = '';
            return;
        }
        const bits = [];
        if (dq.trades_in_period != null) bits.push(`${dq.trades_in_period} trades in period`);
        if (dq.in_period != null) bits.push(`${dq.in_period} orders scanned`);
        if (dq.truncated) bits.push(`ledger scan truncated at ${fmtInt(dq.scanned)} of ${fmtInt(dq.ledger_total)}`);
        if (dq.orphaned) bits.push(`${dq.orphaned} order(s) from removed runners excluded`);
        if (dq.session_scoped) bits.push('order history is session-scoped (a restart clears it)');
        node.textContent = bits.length ? `ℹ️ ${bits.join(' · ')}` : '';
    }

    function renderBrokerTable(payload) {
        const body = el('brokerTableBody');
        if (!body) return;
        const rows = payload.by_broker || [];
        if (!rows.length) {
            body.innerHTML = '<tr><td colspan="12" class="text-center muted">'
                + 'No brokers have a runner in this period.</td></tr>';
            return;
        }
        const bestSharpe = Math.max(...rows.map((r) => (isNum(r.sharpe_ratio) ? r.sharpe_ratio : -Infinity)));
        const bestSlip = Math.min(...rows.map((r) => (isNum(r.avg_slippage_bps) ? r.avg_slippage_bps : Infinity)));

        body.innerHTML = rows.map((r) => {
            const pnlColor = (r.net_pnl || 0) >= 0 ? 'var(--success)' : 'var(--danger)';
            const sharpeCell = isNum(r.sharpe_ratio) && r.sharpe_ratio === bestSharpe
                ? `${fmtNum(r.sharpe_ratio)} <span title="Best">🥇</span>` : fmtNum(r.sharpe_ratio);
            const slipCell = isNum(r.avg_slippage_bps) && rows.length > 1 && r.avg_slippage_bps === bestSlip
                ? `${fmtBps(r.avg_slippage_bps)} <span title="Lowest">✅</span>` : fmtBps(r.avg_slippage_bps);
            const segments = (r.segments || []).map((s) => `<div class="small">${esc(s)}</div>`).join('') || DASH;
            return `<tr>
                <td><strong>${esc(r.display_name || labelFor(r.broker))}</strong>
                    <div class="small muted">${esc((r.strategies || []).join(', '))}</div></td>
                <td class="text-right" style="color:${pnlColor};">${fmtInr(r.net_pnl)}</td>
                <td class="text-right">${fmtPct(r.pnl_pct)}</td>
                <td class="text-right">${sharpeCell}</td>
                <td class="text-right">${isNum(r.max_drawdown_pct) ? `-${Number(r.max_drawdown_pct).toFixed(2)}%` : DASH}</td>
                <td class="text-right">${fmtInt(r.total_trades)}</td>
                <td class="text-right">${fmtPct(r.win_rate)}</td>
                <td class="text-right">${slipCell}</td>
                <td class="text-right">${fmtPct(r.fill_rate_pct)}</td>
                <td class="text-right">${fmtSec(r.avg_fill_time_sec)}</td>
                <td class="text-right">${fmtPct(r.rejection_rate_pct)}</td>
                <td>${segments}</td>
            </tr>`;
        }).join('');
    }

    function renderBrokerHeadline(payload) {
        const node = el('brokerHeadline');
        if (!node) return;
        const chips = [];
        const bySharpe = (payload.broker_rankings || {}).by_sharpe || [];
        if (bySharpe.length) {
            const top = payload.by_broker.find((b) => b.broker === bySharpe[0]);
            if (top) {
                chips.push(`<span class="xb-chip xb-chip-good">🏆 Best performer: ${esc(top.display_name)}`
                    + ` (Sharpe ${fmtNum(top.sharpe_ratio)})</span>`);
            }
        }
        (payload.alerts || []).slice(0, 3).forEach((a) => {
            const cls = a.severity === 'critical' ? 'xb-chip-bad'
                : a.severity === 'info' ? 'xb-chip' : 'xb-chip-warn';
            chips.push(`<span class="xb-chip ${cls}">${esc(a.message)}</span>`);
        });
        node.innerHTML = chips.join('');
    }

    function renderBrokerAlerts(payload) {
        const node = el('brokerAlerts');
        if (!node) return;
        const alerts = payload.alerts || [];
        node.innerHTML = alerts.length
            ? alerts.map((a) => `<div class="xb-alert is-${esc(a.severity || 'warning')}">`
                + `${esc(a.message)}</div>`).join('')
            : '<div class="xb-note">No execution alerts in this period.</div>';
    }

    function renderBrokerInsights(payload) {
        const node = el('brokerInsights');
        if (!node) return;
        const insights = payload.insights || [];
        node.innerHTML = insights.length
            ? insights.map((i) => `<div class="xb-insight">💡 ${esc(i.message)}</div>`).join('')
            : '';
    }

    async function loadSummary(force) {
        if (state.loadedSummary && !force) return;
        const body = el('brokerTableBody');
        try {
            const data = await getJson(query(API.summary));
            state.summary = data;
            state.loadedSummary = true;
            state.brokers = (data.by_broker || []).map((b) => b.broker);
            renderPortfolioTotals(data);
            renderBrokerHeadline(data);
            renderBrokerTable(data);
            renderBrokerAlerts(data);
            renderBrokerInsights(data);
            refreshBrokerOptions();
        } catch (err) {
            state.loadedSummary = false;
            if (body) {
                body.innerHTML = `<tr><td colspan="12" class="text-center" style="color:var(--danger);">`
                    + `${esc(err.message)}</td></tr>`;
            }
        }
    }

    // ------------------------------------------------------------------
    // Section 2 — execution quality
    // ------------------------------------------------------------------

    let slippageChart = null;
    let distributionChart = null;

    function renderSlippageChart(series, brokers) {
        const canvas = el('execSlippageChart');
        if (!canvas || typeof Chart === 'undefined') return;
        if (slippageChart) slippageChart.destroy();

        const points = (series || []).filter((p) => isNum(p.avg_slippage_bps));
        if (!points.length) {
            const ctx0 = canvas.getContext('2d');
            ctx0.clearRect(0, 0, canvas.width, canvas.height);
            return;
        }
        const palette = ['#7fc8a0', '#d4b26a', '#e0938f', '#5b8dbe', '#b58cd6'];
        slippageChart = new Chart(canvas.getContext('2d'), {
            type: 'line',
            data: {
                labels: points.map((p) => p.date),
                datasets: [{
                    label: 'Avg slippage (bps)',
                    data: points.map((p) => p.avg_slippage_bps),
                    borderColor: palette[0],
                    backgroundColor: 'rgba(127,200,160,0.12)',
                    fill: true,
                    tension: 0.2,
                    pointRadius: points.length > 20 ? 0 : 3,
                }],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: { legend: { display: false } },
                scales: {
                    x: { grid: { display: false } },
                    y: { title: { display: true, text: 'bps' } },
                },
            },
        });
    }

    function renderDistributionChart(buckets) {
        const canvas = el('execDistributionChart');
        if (!canvas || typeof Chart === 'undefined') return;
        if (distributionChart) distributionChart.destroy();
        const data = (buckets || []).map((b) => b.count || 0);
        distributionChart = new Chart(canvas.getContext('2d'), {
            type: 'bar',
            data: {
                labels: (buckets || []).map((b) => b.bucket),
                datasets: [{
                    label: 'Orders',
                    data,
                    backgroundColor: 'rgba(91,141,190,0.65)',
                    borderColor: '#5b8dbe',
                    borderWidth: 1,
                }],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: { legend: { display: false } },
                scales: {
                    x: { grid: { display: false } },
                    y: { beginAtZero: true, ticks: { precision: 0 } },
                },
            },
        });
    }

    function renderExecution(payload) {
        const q = payload.execution_quality || {};

        const degradation = el('execDegradation');
        if (degradation) {
            const alert = payload.degradation_alert;
            if (!alert) {
                degradation.innerHTML = '';
            } else if (alert.alert_type === 'fill_rate_decline') {
                degradation.innerHTML = `<span class="xb-chip xb-chip-bad">⚠️ ${esc(alert.message)}</span>`;
            } else {
                degradation.innerHTML = `<span class="xb-chip">ℹ️ ${esc(alert.message)}</span>`;
            }
        }

        renderSlippageChart(payload.time_series, payload.brokers);
        renderDistributionChart(payload.slippage_distribution);

        const median = el('execSlippageMedian');
        if (median) {
            const peers = payload.peer_benchmarks || [];
            const detail = peers.length
                ? `Medians — ${peers.map((p) => `${esc(labelFor(p.broker))} ${fmtBps(p.avg_slippage_bps)}`).join(' · ')}`
                : 'No peer brokers in this period.';
            median.textContent = detail;
        }

        const distNote = el('execDistributionNote');
        if (distNote) {
            const top = (payload.slippage_distribution || []).slice().sort((a, b) => b.count - a.count)[0];
            distNote.textContent = (q.total_orders && top && top.count)
                ? `${top.count} of ${q.slippage_samples || 0} sampled fills fall in ${top.bucket}.`
                : 'No sampled fills in this period.';
        }

        const peers = payload.peer_benchmarks || [];
        const peerBody = el('execPeerBody');
        if (peerBody) {
            const bestFill = peers.length
                ? Math.max(...peers.map((p) => (isNum(p.fill_rate_pct) ? p.fill_rate_pct : -Infinity)))
                : null;
            peerBody.innerHTML = peers.length
                ? peers.map((p) => {
                    const isBest = isNum(p.fill_rate_pct) && p.fill_rate_pct === bestFill;
                    return `<tr>
                        <td><strong>${esc(labelFor(p.broker))}</strong></td>
                        <td class="text-right">${fmtPct(p.fill_rate_pct)} ${isBest ? '✅' : ''}</td>
                        <td class="text-right">${fmtSec(p.avg_fill_time_sec)}</td>
                        <td class="text-right">${fmtPct(p.rejection_rate_pct)}</td>
                        <td class="text-right">${fmtPct(p.stale_order_pct)}</td>
                        <td class="text-right">${fmtInt(p.total_orders)}</td>
                    </tr>`;
                }).join('')
                : '<tr><td colspan="6" class="text-center muted">No execution data in this period.</td></tr>';
        }

        const strategyBody = el('execStrategyBody');
        if (strategyBody) {
            const rows = payload.by_strategy || [];
            strategyBody.innerHTML = rows.length
                ? rows.map((r) => `<tr>
                    <td>${esc(r.strategy)}</td>
                    <td>${esc(labelFor(r.broker))}</td>
                    <td class="text-right">${fmtBps(r.avg_slippage_bps)}</td>
                    <td class="text-right">${fmtPct(r.fill_rate_pct)}</td>
                    <td class="text-right">${fmtPct(r.rejection_rate_pct)}</td>
                    <td class="text-right">${fmtInt(r.total_orders)}</td>
                </tr>`).join('')
                : '<tr><td colspan="6" class="text-center muted">No execution data in this period.</td></tr>';
        }

        const notes = el('execNotes');
        if (notes) {
            notes.innerHTML = (payload.notes || [])
                .map((n) => `<div class="xb-note">ℹ️ ${esc(n)}</div>`).join('');
        }
    }

    async function loadExecution(force) {
        if (state.loadedExecution && !force) return;
        const params = new URLSearchParams({ period: state.period });
        const broker = el('execBrokerFilter') ? el('execBrokerFilter').value : '';
        const strategy = el('execStrategyFilter') ? el('execStrategyFilter').value : '';
        if (broker) params.set('broker', broker);
        if (strategy) params.set('strategy', strategy);
        if (state.mode && state.mode !== 'all') params.set('mode', state.mode);
        try {
            const data = await getJson(`${API.execution}?${params.toString()}`);
            state.execution = data;
            state.loadedExecution = true;
            renderExecution(data);
        } catch (err) {
            state.loadedExecution = false;
            const body = el('execPeerBody');
            if (body) {
                body.innerHTML = '<tr><td colspan="6" class="text-center" style="color:var(--danger);">'
                    + `${esc(err.message)}</td></tr>`;
            }
        }
    }

    // ------------------------------------------------------------------
    // Modals
    // ------------------------------------------------------------------

    function openOverlay(id) {
        const node = el(id);
        if (node) node.classList.add('open');
    }

    function closeOverlay(id) {
        const node = el(id);
        if (node) node.classList.remove('open');
    }

    function bindOverlay(overlayId, closeId) {
        const overlay = el(overlayId);
        const close = el(closeId);
        if (close && overlay) close.addEventListener('click', () => closeOverlay(overlayId));
        if (overlay) {
            overlay.addEventListener('click', (e) => {
                if (e.target === overlay) closeOverlay(overlayId);
            });
        }
    }

    // ---- Compare -------------------------------------------------------

    /** Refresh the broker list, then show the modal. */
    async function openCompareModal() {
        await loadSummary(true);
        openOverlay('crossBrokerCompareOverlay');
    }

    async function openMigrationModal() {
        await loadSummary(true);
        renderMigrationOptions();
        openOverlay('crossBrokerMigrationOverlay');
    }

    async function openRecommendModal() {
        await loadSummary(true);
        openOverlay('crossBrokerRecommendOverlay');
    }

    function renderCompareOptions() {
        const host = el('compareBrokerChecks');
        if (!host) return;
        const names = state.brokers;
        if (host.childElementCount === names.length && names.length) return;
        host.innerHTML = names.length
            ? names.map((name, i) => `<label><input type="checkbox" class="xb-compare-broker" `
                + `value="${esc(name)}" ${i < 2 ? 'checked' : ''} /> ${esc(labelFor(name))}</label>`).join('')
            : '<div class="xb-empty">No brokers have a runner yet.</div>';
    }

    function selectedCompareBrokers() {
        return Array.from(document.querySelectorAll('.xb-compare-broker'))
            .filter((cb) => cb.checked)
            .map((cb) => cb.value);
    }

    async function runCompare() {
        const errorNode = el('compareError');
        const result = el('compareResult');
        if (errorNode) errorNode.textContent = '';
        const brokers = selectedCompareBrokers();
        if (brokers.length < 2) {
            if (errorNode) errorNode.textContent = 'Pick at least two brokers to compare.';
            return;
        }
        const metricSelect = el('compareMetricSelect');
        const metrics = metricSelect && metricSelect.value !== 'all' ? [metricSelect.value] : null;
        try {
            const data = await postJson(API.compare, {
                brokers,
                metrics,
                period: state.period,
                statistical_test: !!(el('compareStatTest') && el('compareStatTest').checked),
            });
            if (result) result.innerHTML = renderComparison(data);
        } catch (err) {
            if (result) result.innerHTML = '';
            if (errorNode) errorNode.textContent = err.message;
        }
    }

    function renderComparison(data) {
        const [a, b] = data.brokers || [];
        if (!a || !b) return '<div class="xb-empty">Nothing to compare yet.</div>';

        const header = `<div class="xb-metric-row is-header">
            <span>Metric</span><span>${esc(labelFor(a))}</span>
            <span>${esc(labelFor(b))}</span><span>Difference</span></div>`;

        const rows = (data.metrics || []).map((m) => {
            const va = m[a];
            const vb = m[b];
            const scale = Math.max(Math.abs(Number(va) || 0), Math.abs(Number(vb) || 0)) || 1;
            const sig = m.statistical_significance || {};
            const winnerIsA = m.better === a;
            const cls = (k) => (m.better ? (m.better === k ? 'xb-metric-winner' : 'xb-metric-loser') : '');
            const bar = (v) => {
                const pct = Math.min(100, (Math.abs(Number(v) || 0) / scale) * 100);
                const good = m.better === a ? (v === va) : (v === vb);
                return `<div class="xb-bar ${m.better ? (good ? 'is-good' : 'is-bad') : ''}">`
                    + `<span style="width:${pct.toFixed(1)}%"></span></div>`;
            };
            const unit = m.unit === '₹' ? '' : (m.unit === '%' ? '%' : (m.unit ? ` ${m.unit}` : ''));
            return `<div class="xb-metric-row">
                <div><div>${esc(m.label)}</div>
                    <div class="xb-sig ${sig.testable && sig.significant ? 'is-significant' : ''}">${esc(sigText(sig))}</div></div>
                <div class="${cls(a)}">${esc(fmtMetric(m, va))}${esc(unit)}${bar(va)}</div>
                <div class="${cls(b)}">${esc(fmtMetric(m, vb))}${esc(unit)}${bar(vb)}</div>
                <div class="small">${esc(fmtDelta(m, va, vb))}</div>
            </div>`;
        }).join('');

        const rec = data.recommendation || {};
        const recBlock = rec.preferred_broker
            ? `<div class="xb-insight" style="margin-top:12px;">🎯 <strong>${esc(labelFor(rec.preferred_broker))}</strong>
                 — ${esc(rec.summary || '')} <span class="xb-sig">confidence: ${esc(rec.confidence)}</span>
               <ul class="small" style="margin:6px 0 0 16px;">`
                + (rec.reasoning || []).map((r) => `<li>${esc(r)}</li>`).join('') + '</ul></div>'
            : `<div class="xb-insight" style="margin-top:12px;">🎯 ${esc(rec.summary || 'No broker is measurably better.')}</div>`;

        const note = m => (m && m.note) ? `<div class="xb-note">ℹ️ ${esc(m.note)}</div>` : '';

        return header + rows + recBlock + note(data.data_quality);
    }

    function fmtMetric(row, value) {
        if (value == null) return DASH;
        if (row.unit === '₹') return fmtInr(value);
        if (row.metric === 'total_trades') return fmtInt(value);
        if (row.metric === 'max_drawdown_pct') return `-${Number(value).toFixed(2)}%`;
        return fmtNum(value);
    }

    function fmtDelta(row, va, vb) {
        if (row.difference == null) return DASH;
        const sign = row.difference > 0 ? '+' : '';
        const body = row.unit === '₹' ? fmtInr(row.difference) : fmtNum(row.difference);
        const pct = row.difference_pct == null
            ? '' : ` (${sign}${Number(row.difference_pct).toFixed(0)}%)`;
        return `${sign}${body}${pct}`;
    }

    // ---- Migration -----------------------------------------------------

    function renderMigrationOptions() {
        const summary = state.summary;
        if (!summary) return;
        const rows = summary.by_broker || [];
        const options = rows.map((r) => `<option value="${esc(r.broker)}">${esc(r.display_name || labelFor(r.broker))}</option>`).join('');

        ['migrationFrom', 'migrationTo'].forEach((id) => {
            const node = el(id);
            if (node && !node.childElementCount) node.innerHTML = options;
        });
        const to = el('migrationTo');
        if (to && to.options.length > 1) to.selectedIndex = 1;

        const strategySelect = el('migrationStrategy');
        if (strategySelect && !strategySelect.childElementCount) {
            const names = new Set();
            rows.forEach((r) => (r.strategies || []).forEach((s) => names.add(s)));
            strategySelect.innerHTML = Array.from(names)
                .map((s) => `<option value="${esc(s)}">${esc(s)}</option>`).join('');
        }
    }

    async function runMigration() {
        const errorNode = el('migrationError');
        const result = el('migrationResult');
        if (errorNode) errorNode.textContent = '';
        const strategy = el('migrationStrategy') ? el('migrationStrategy').value : '';
        const from = el('migrationFrom') ? el('migrationFrom').value : '';
        const to = el('migrationTo') ? el('migrationTo').value : '';
        if (!strategy || !from || !to || from === to) {
            if (errorNode) {
                errorNode.textContent = 'Pick a strategy and two different brokers.';
            }
            return;
        }
        try {
            const data = await postJson(API.migration, {
                strategy, from_broker: from, to_broker: to, period: state.period,
            });
            if (result) result.innerHTML = renderMigration(data);
        } catch (err) {
            if (result) result.innerHTML = '';
            if (errorNode) errorNode.textContent = err.message;
        }
    }

    function perfBlock(title, perf, delta) {
        const rows = [
            ['Sharpe', fmtNum(perf.sharpe), delta ? delta.sharpe : null],
            ['Net P&L', fmtInr(perf.net_pnl), delta ? fmtInr(delta.pnl_change) : null],
            ['Avg slippage', fmtBps(perf.avg_slippage_bps), null],
            ['Fill rate', fmtPct(perf.fill_rate_pct), null],
            ['Orders', fmtInt(perf.orders != null ? perf.orders : perf.total_trades), null],
        ];
        return `<div class="xb-panel" style="margin-top:10px;">
            <h4 class="xb-panel-title">${esc(title)}</h4>
            ${rows.map(([k, v, d]) => `<div class="xb-metric-row" style="grid-template-columns:1.4fr 1fr 1fr;">
                <span>${esc(k)}</span><span>${esc(v)}</span>
                <span class="small muted">${d == null ? '' : esc(d)}</span></div>`).join('')}
        </div>`;
    }

    function renderMigration(data) {
        const impact = data.impact || {};
        const rec = data.recommendation || {};
        const history = data.historical_data || {};
        const current = data.current_performance || {};
        const estimated = data.estimated_performance || {};

        const actionChip = {
            KEEP: 'xb-chip-good', MOVE: 'xb-chip-good', HOLD: 'xb-chip', 'INSUFFICIENT DATA': 'xb-chip-warn',
        }[rec.action] || 'xb-chip';

        const parts = [];
        parts.push(`<div class="xb-headline">
            <span class="xb-chip ${actionChip}">${esc(rec.action || '?')} — ${esc(data.strategy)}</span>
            <span class="xb-chip">${esc(labelFor(data.from_broker))} → ${esc(labelFor(data.to_broker))}</span>
            <span class="xb-chip">confidence: ${esc(rec.confidence || 'n/a')}</span>
        </div>`);

        parts.push(perfBlock(`📊 Current (${labelFor(data.from_broker)})`, current, null));
        parts.push(perfBlock(`🔮 Estimated (${labelFor(data.to_broker)})`, {
            sharpe: estimated.estimated_sharpe,
            net_pnl: estimated.estimated_net_pnl,
            avg_slippage_bps: estimated.avg_slippage_bps,
            fill_rate_pct: estimated.fill_rate_pct,
            orders: null,
            total_trades: estimated.estimated_trades,
        }, {
            sharpe: isNum(impact.sharpe_change) ? `${impact.sharpe_change > 0 ? '+' : ''}${Number(impact.sharpe_change).toFixed(2)}` : null,
            pnl_change: impact.pnl_change,
        }));

        if ((impact.reasons || []).length) {
            parts.push('<div class="xb-panel" style="margin-top:10px;"><h4 class="xb-panel-title">🔍 Why</h4><ul class="small" style="margin:0 0 0 16px;">'
                + impact.reasons.map((r) => `<li>${esc(r)}</li>`).join('') + '</ul></div>');
        }

        // Trade retention is the assumption most likely to be wrong: if the
        // target venue rejects a third of the orders, the P&L delta below is
        // not a like-for-like comparison. Show it next to the numbers.
        if (isNum(impact.trade_retention)) {
            parts.push(`<div class="xb-note" style="margin-top:10px;">📉 Trade retention assumption:
                <strong>${Number(impact.trade_retention).toFixed(1)}%</strong> of submitted orders are
                assumed to reach the target broker (${fmtNum(impact.trade_count_change, 0)} trades).
                The P&L estimate scales with it.</div>`);
        }

        // The API states which model produced the estimate. Never render an
        // estimate without it.
        if (estimated.methodology) {
            parts.push(`<div class="xb-panel" style="margin-top:10px;">
                <h4 class="xb-panel-title">📐 Model</h4>
                <div class="small">${esc(estimated.methodology)}</div></div>`);
        }
        if (history.note) {
            parts.push(`<div class="xb-note" style="margin-top:10px;">📚 ${esc(history.note)}</div>`);
        }
        if (rec.note) {
            parts.push(`<div class="xb-note" style="margin-top:6px;">⚠️ ${esc(rec.note)}</div>`);
        }
        return parts.join('');
    }

    // ---- Recommend -----------------------------------------------------

    async function runRecommend() {
        const errorNode = el('recommendError');
        const result = el('recommendResult');
        if (errorNode) errorNode.textContent = '';
        const sizeRaw = el('recommendSize') ? el('recommendSize').value : '';
        const body = {
            strategy_type: el('recommendType') ? el('recommendType').value : 'scalper',
            trade_frequency: el('recommendFrequency') ? el('recommendFrequency').value : 'high',
            segment: el('recommendSegment') ? el('recommendSegment').value || null : null,
            period: state.period,
        };
        if (sizeRaw !== '') body.avg_trade_size = Number(sizeRaw);
        try {
            const data = await postJson(API.recommend, body);
            if (result) result.innerHTML = renderRecommend(data);
        } catch (err) {
            if (result) result.innerHTML = '';
            if (errorNode) errorNode.textContent = err.message;
        }
    }

    function renderRecommend(data) {
        if (!data.recommended_broker) {
            const lines = (data.reasoning || []).map((r) => `<li>${esc(r)}</li>`).join('');
            return `<div class="xb-headline"><span class="xb-chip xb-chip-warn">No recommendation</span></div>`
                + `<ul class="small" style="margin-left:16px;">${lines}</ul>`;
        }
        const savings = data.estimated_monthly_savings || {};
        // score_basis_pct is how much of the scoring rubric the broker had
        // measured data for. A 9.0 score off 20% of the weights is a different
        // claim from a 9.0 off 100%, so the basis rides next to every score.
        const rankings = (data.rankings || []).map((r) => `<tr>
            <td><strong>${esc(r.display_name || labelFor(r.broker))}</strong></td>
            <td class="text-right">${fmtNum(r.score, 1)}
                <div class="small muted">basis ${fmtPct(r.score_basis_pct)}</div></td>
            <td class="text-right">${fmtPct(r.metrics.fill_rate_pct)}</td>
            <td class="text-right">${fmtSec(r.metrics.avg_fill_time_sec)}</td>
            <td class="text-right">${fmtBps(r.metrics.avg_slippage_bps)}</td>
            <td>${esc((r.strengths || []).join(', ')) || DASH}</td>
        </tr>`).join('');

        return `<div class="xb-headline">
            <span class="xb-chip xb-chip-good">🎯 ${esc(data.recommended_broker_label || labelFor(data.recommended_broker))}</span>
            <span class="xb-chip">confidence: ${esc(data.confidence)}</span>
            ${savings.amount ? `<span class="xb-chip xb-chip-good">Saves ${esc(fmtInr(savings.amount))}/month</span>` : ''}
        </div>
        <ul class="small" style="margin:0 0 10px 16px;">${(data.reasoning || []).map((r) => `<li>${esc(r)}</li>`).join('')}</ul>
        ${savings.calculation ? `<div class="xb-note">${esc(savings.calculation)}</div>` : ''}
        ${data.segment_note ? `<div class="xb-note">ℹ️ ${esc(data.segment_note)}</div>` : ''}
        <div class="table-scroll" style="margin-top:10px;">
            <table class="data-table">
                <thead><tr><th>Broker</th><th class="text-right">Score<br><span class="small muted">basis</span></th>
                    <th class="text-right">Fill</th><th class="text-right">Time</th>
                    <th class="text-right">Slip</th><th>Strengths</th></tr></thead>
                <tbody>${rankings}</tbody>
            </table>
        </div>`;
    }

    // ------------------------------------------------------------------
    // Shared option refresh
    // ------------------------------------------------------------------

    function refreshBrokerOptions() {
        renderCompareOptions();
        renderMigrationOptions();

        const execBroker = el('execBrokerFilter');
        if (execBroker && execBroker.childElementCount - 1 !== state.brokers.length) {
            const current = execBroker.value;
            execBroker.innerHTML = '<option value="">All brokers</option>'
                + state.brokers.map((b) => `<option value="${esc(b)}">${esc(labelFor(b))}</option>`).join('');
            execBroker.value = current;
        }

        const segSelect = el('recommendSegment');
        if (segSelect && segSelect.childElementCount === 1 && state.summary) {
            const segments = Array.from(new Set(
                (state.summary.by_segment || []).map((s) => s.segment).filter(Boolean)
            ));
            segSelect.innerHTML = '<option value="">All segments</option>'
                + segments.map((s) => `<option value="${esc(s)}">${esc(s)}</option>`).join('');
        }
    }

    function refreshStrategyOptions() {
        const select = el('execStrategyFilter');
        if (!select || !state.summary) return;
        const names = new Set();
        (state.summary.by_broker || []).forEach((b) => (b.strategies || []).forEach((s) => names.add(s)));
        const current = select.value;
        select.innerHTML = '<option value="">All strategies</option>'
            + Array.from(names).map((s) => `<option value="${esc(s)}">${esc(s)}</option>`).join('');
        select.value = current;
    }

    // ------------------------------------------------------------------
    // Wiring
    // ------------------------------------------------------------------

    function onToggle() {
        if (el('brokerSection') && el('brokerSection').open) loadSummary(false);
    }

    function bind() {
        const brokerSection = el('brokerSection');
        if (brokerSection) brokerSection.addEventListener('toggle', onToggle);

        const execSection = el('executionSection');
        if (execSection) {
            execSection.addEventListener('toggle', () => {
                if (!execSection.open) return;
                loadSummary(false).then(() => refreshStrategyOptions()).then(() => loadExecution(false));
            });
        }

        ['execBrokerFilter', 'execStrategyFilter'].forEach((id) => {
            const node = el(id);
            if (node) node.addEventListener('change', () => loadExecution(true));
        });

        const openCompare = el('openCompareBtn');
        if (openCompare) openCompare.addEventListener('click', openCompareModal);
        const openMigration = el('openMigrationBtn');
        if (openMigration) openMigration.addEventListener('click', openMigrationModal);
        const openRecommend = el('openRecommendBtn');
        if (openRecommend) openRecommend.addEventListener('click', openRecommendModal);

        bindOverlay('crossBrokerCompareOverlay', 'crossBrokerCompareClose');
        bindOverlay('crossBrokerMigrationOverlay', 'crossBrokerMigrationClose');
        bindOverlay('crossBrokerRecommendOverlay', 'crossBrokerRecommendClose');

        const runCompareBtn = el('compareRunBtn');
        if (runCompareBtn) runCompareBtn.addEventListener('click', runCompare);
        const runMigrationBtn = el('migrationRunBtn');
        if (runMigrationBtn) runMigrationBtn.addEventListener('click', runMigration);
        const runRecommendBtn = el('recommendRunBtn');
        if (runRecommendBtn) runRecommendBtn.addEventListener('click', runRecommend);
    }

    /** Called by analytics.js when the page period/mode filter changes. */
    function invalidate() {
        state.loadedSummary = false;
        state.loadedExecution = false;
    }

    function setPeriod(period) {
        state.period = period;
        invalidate();
    }

    function setMode(mode) {
        state.mode = mode;
        invalidate();
    }

    window.CrossBrokerUI = {
        invalidate,
        setPeriod,
        setMode,
        loadSummary,
        loadExecution,
        // Exposed so the PRD-003 harness can drive a modal end-to-end without
        // reaching into the closure.
        openCompareModal,
        openMigrationModal,
        openRecommendModal,
    };

    function init() {
        bind();
        // The portfolio strip is the one thing worth showing without expanding
        // anything, so it loads immediately; the heavy sections stay lazy.
        loadSummary(false);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
