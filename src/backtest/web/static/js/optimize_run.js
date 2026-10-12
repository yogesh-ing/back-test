/* Optimization run page (/optimize/runs/<id>).
 *
 * While the run is live it polls GET /api/optimize/runs/<id> every second
 * and renders progress (phase, counts, ETA, best-so-far, recent results,
 * walk-forward splits). Once finished it renders the results dashboard:
 * overview (current vs optimized, robustness, warnings, equity curves),
 * heatmaps, walk-forward, sensitivity, all results and the audit trail.
 */
(() => {
    'use strict';
    const C = globalThis.OptCommon;
    const $ = (id) => document.getElementById(id);
    const root = $('optRun');
    if (!root) return;
    const runId = root.dataset.runId;
    const base = `/api/optimize/runs/${encodeURIComponent(runId)}`;

    const state = {
        run: null, timer: null, charts: {}, resultsLoaded: false,
        page: { offset: 0, limit: 50, sort: 'objective_score', order: 'desc', total: 0 },
        applyParams: null, runners: [],
    };
    const LIVE = new Set(['pending', 'running', 'paused']);

    // ---------------------------------------------------------------- polling

    async function refresh() {
        let res;
        try {
            res = await C.api(base);
        } catch (e) {
            showError(e.status === 404 ? 'Run not found.' : e.message);
            stopPolling();
            return;
        }
        const wasLive = state.run && LIVE.has(state.run.status);
        state.run = res.run;
        renderHeader();
        if (LIVE.has(state.run.status)) {
            renderProgress();
            schedule(1000);
        } else {
            $('progressCard').hidden = true;
            stopPolling();
            if (!state.resultsLoaded || wasLive) renderResults();
        }
    }

    function schedule(ms) { stopPolling(); state.timer = setTimeout(refresh, ms); }
    function stopPolling() { if (state.timer) clearTimeout(state.timer); state.timer = null; }

    function showError(msg) {
        const el = $('runError');
        el.hidden = !msg;
        el.textContent = msg || '';
    }

    // ---------------------------------------------------------------- header

    function renderHeader() {
        const r = state.run;
        const bt = r.backtest_config || {};
        $('runTitle').innerHTML = `${C.escapeHtml(r.strategy_id)} <span class="muted">· ${C.escapeHtml(r.method)}</span> ${C.statusBadge(r.status)}`;
        document.title = `${r.strategy_id} optimization · Trading Bot`;
        $('runSubtitle').textContent = `Maximize ${C.OBJECTIVE_LABELS[r.objective_function] || r.objective_function} · `
            + `${bt.symbol || ''} ${bt.timeframe || ''} ${bt.startDate || ''} → ${bt.endDate || ''} · `
            + `${bt.engine || 'driver'} engine · started ${C.fmtDate(r.started_at || r.created_at)}`;
        const live = LIVE.has(r.status);
        const paused = r.status === 'paused';
        const done = r.status === 'completed' || r.status === 'cancelled';
        $('btnPause').hidden = !(live && !paused && r.live);
        $('btnResume').hidden = !(paused && r.live);
        $('btnCancel').hidden = !live;
        $('btnStartDraft').hidden = r.status !== 'draft';
        $('btnRerun').hidden = live || r.status === 'draft';
        $('btnExport').hidden = !done;
        $('btnExport').href = `${base}/export.csv`;
        $('btnPreset').hidden = !(done && r.best_params);
        $('btnApply').hidden = !(done && r.best_params);
        $('btnMonteCarlo').hidden = !(done && r.best_params);
        // Engine + data this run was produced from, read back from the run
        // record itself (PRD backTest-enhance §1.2) — never from whatever the
        // app happens to be configured with now.
        if (typeof Provenance !== 'undefined') Provenance.render($('runProvenance'), r.provenance);
        if (r.status === 'failed') showError(`Run failed: ${r.error_message || 'unknown error'}`);
        else if (r.status === 'cancelled' && r.error_message) showError(`${r.error_message} — partial results below.`);
        else showError('');
    }

    // --------------------------------------------------------------- progress

    function renderProgress() {
        const p = state.run.progress;
        $('progressCard').hidden = false;
        if (!p) {
            $('progPhase').textContent = state.run.status === 'pending' ? 'queued' : state.run.status;
            return;
        }
        $('progPhase').textContent = `${p.phase}${p.phase_detail ? ' · ' + p.phase_detail : ''}${p.paused ? ' (paused)' : ''}`;
        $('progFill').style.width = `${p.percent}%`;
        $('progPct').textContent = `${p.percent.toFixed(1)}%`;
        $('progTested').textContent = p.tested.toLocaleString();
        $('progTestedSub').textContent = `of ~${(state.run.total_combinations || 0).toLocaleString()} planned`;
        $('progValid').textContent = p.valid.toLocaleString();
        $('progValidSub').textContent = p.tested ? `${((p.valid / p.tested) * 100).toFixed(0)}% meet constraints` : '';
        $('progElapsed').textContent = C.fmtDuration(p.elapsed_seconds);
        $('progEta').textContent = p.paused ? 'paused' : C.fmtDuration(p.eta_seconds);
        $('progRate').textContent = p.rate_per_second ? `${p.rate_per_second} backtests/s` : '';
        if (p.best) {
            const m = p.best.metrics || {};
            $('progBest').classList.remove('muted');
            $('progBest').innerHTML = `
                <div class="opt-best-score">${C.fmtNum(p.best.score, 3)}</div>
                <div class="opt-best-params">${C.escapeHtml(C.fmtParams(p.best.params))}</div>
                <div class="small">Return ${C.fmtMetric('total_return', m.total_return)} · DD ${C.fmtMetric('max_drawdown', m.max_drawdown)}
                    · ${m.total_trades ?? 0} trades · win ${C.fmtMetric('win_rate', m.win_rate)}</div>`;
        }
        $('progRecent').innerHTML = (p.recent || []).map((r) => `
            <tr class="${r.constraints_met ? '' : 'opt-row-off'}">
                <td>${r.seq}</td><td class="opt-params-cell">${C.escapeHtml(C.fmtParams(r.params))}</td>
                <td>${C.fmtNum(r.score, 3)}</td><td>${C.fmtMetric('total_return', r.total_return)}</td>
                <td>${C.fmtMetric('max_drawdown', r.max_drawdown)}</td><td>${r.total_trades ?? '—'}</td>
                <td>${r.constraints_met ? '<span class="pos">✓</span>' : `<span class="neg" title="violates">✗ ${C.escapeHtml(r.violation || '')}</span>`}</td>
            </tr>`).join('');
        const splits = p.walk_forward_splits || [];
        $('progWfBox').hidden = !splits.length;
        $('progWf').innerHTML = splits.map(wfRow).join('');
    }

    function wfRow(s) {
        const deg = s.degradation;
        return `<tr><td>${s.split}</td><td class="small">${s.train_start} → ${s.train_end}</td>
            <td class="small">${s.test_start} → ${s.test_end}</td>
            <td class="opt-params-cell">${C.escapeHtml(s.params ? C.fmtParams(s.params) : (s.note || '—'))}</td>
            <td>${C.fmtNum(s.train_score, 3)}</td><td class="${C.isNum(s.test_score) && s.test_score < 0 ? 'neg' : ''}">${C.fmtNum(s.test_score, 3)}</td>
            ${deg !== undefined ? `<td class="${C.isNum(deg) && deg > 0.3 ? 'neg' : ''}">${C.fmtNum(deg, 3)}</td><td>${s.test_trades ?? '—'}</td>` : ''}</tr>`;
    }

    // ---------------------------------------------------------------- results

    function renderResults() {
        state.resultsLoaded = true;
        const r = state.run;
        if (!['completed', 'cancelled', 'failed'].includes(r.status)) { $('resultsBox').hidden = true; return; }
        $('resultsBox').hidden = false;
        renderOverview();
        setupHeatmap();
        renderWalkForward();
        renderSensitivity();
        loadResults();
        loadAudit();
    }

    const COMPARE_METRICS = ['sharpe', 'sortino', 'calmar', 'total_return', 'cagr', 'max_drawdown',
        'win_rate', 'profit_factor', 'total_trades', 'expectancy', 'drawdown_duration_days'];

    /**
     * PRD Part 2 §3 — the deflated Sharpe.
     *
     * Two numbers on two scales, and the wording keeps them apart on purpose:
     * the bar is a Sharpe (what the best-of-N had to beat), the probability is
     * a probability (how likely the edge is real). Rendering them as one
     * "Deflated Sharpe" figure would be the easy mistake and the wrong one.
     */
    function renderDeflatedSharpe(dsr, observedMetrics) {
        const observed = observedMetrics && observedMetrics.sharpe;
        $('dsrObserved').textContent = C.isNum(observed) ? C.fmtNum(observed, 2) : '—';
        const reason = $('dsrReason');
        if (!dsr || dsr.status !== 'ok') {
            // An absent statistic is a normal outcome (a 2-combination search
            // has no selection to correct for). Say why rather than showing a
            // confident dash that reads like a zero.
            $('dsrBar').textContent = '—';
            $('dsrVerdict').textContent = '';
            $('dsrVerdict').className = 'opt-dsr-verdict';
            $('dsrFacts').innerHTML = '';
            reason.textContent = (dsr && dsr.reason)
                || 'Not enough combinations were tried for a multiple-testing correction.';
            return;
        }
        reason.textContent = '';
        const bar = Number(dsr.deflated_sharpe);
        $('dsrBar').textContent = C.fmtNum(bar, 2);

        const verdict = $('dsrVerdict');
        if (C.isNum(observed)) {
            const clears = Number(observed) >= bar;
            verdict.className = `opt-dsr-verdict ${clears ? 'pos' : 'neg'}`;
            verdict.textContent = clears
                ? `Clears the bar by ${C.fmtNum(Number(observed) - bar, 2)}.`
                : `Does not clear the bar — short by ${C.fmtNum(bar - Number(observed), 2)}.`;
        } else {
            verdict.className = 'opt-dsr-verdict';
            verdict.textContent = '';
        }

        const pct = C.isNum(dsr.probability) ? `${(dsr.probability * 100).toFixed(0)}%` : '—';
        $('dsrFacts').innerHTML = [
            ['Chances taken', C.isNum(dsr.trials) ? dsr.trials.toLocaleString() : '—'],
            ['Likely genuinely positive', pct],
            ['Return observations', C.isNum(dsr.observations) ? dsr.observations.toLocaleString() : '—'],
        ].map(([k, v]) => `<li><span>${C.escapeHtml(k)}</span><span>${C.escapeHtml(v)}</span></li>`).join('');
    }

    function renderOverview() {
        const r = state.run;
        const a = r.analysis || {};
        const rob = a.robustness || {};
        $('robustScore').textContent = C.isNum(rob.score) ? Number(rob.score).toFixed(1) : '—';
        const lab = C.robustnessLabel(rob.score);
        $('robustLabel').className = `opt-robust-label ${lab.cls}`;
        $('robustLabel').textContent = lab.text;
        const compNames = { plateaus: 'Parameter plateaus', walk_forward: 'Walk-forward efficiency',
            clustering: 'Top-result clustering', sample_size: 'Trade sample size' };
        $('robustComponents').innerHTML = Object.entries(rob.components || {}).map(([k, v]) =>
            `<li><span>${compNames[k] || k} <small class="muted">×${v.weight}</small></span><span>${(v.value * 100).toFixed(0)}%</span></li>`).join('')
            || '<li class="muted">Not enough evidence.</li>';

        const cmp = a.comparison;
        const baseM = (cmp && cmp.baseline.metrics) || r.baseline_metrics || {};
        const optM = (cmp && cmp.optimized.metrics) || r.best_metrics || {};
        renderDeflatedSharpe(a.deflated_sharpe, optM);
        $('compareBody').innerHTML = [['score', r.baseline_score, r.best_score]].concat(
            COMPARE_METRICS.map((k) => [k, baseM[k], optM[k]])).map(([k, b, o]) => {
            const better = C.isImprovement(k, b, o);
            let change = '—';
            if (C.isNum(b) && C.isNum(o)) change = C.fmtDelta(k, Number(o) - Number(b));
            const label = k === 'score' ? `Objective (${C.OBJECTIVE_LABELS[r.objective_function] || r.objective_function})` : (C.METRIC_LABELS[k] || k);
            const fmt = (v) => (k === 'score' ? C.fmtNum(v, 3) : C.fmtMetric(k, v));
            return `<tr><td>${label}</td><td>${fmt(b)}</td><td><strong>${fmt(o)}</strong></td>
                <td class="${better === null ? '' : (better ? 'pos' : 'neg')}">${change}</td></tr>`;
        }).join('');

        const baseP = r.baseline_params || {};
        const optP = r.best_params || {};
        const names = Array.from(new Set([...Object.keys(baseP), ...Object.keys(optP)]));
        $('paramsBody').innerHTML = names.map((n) => {
            const changed = String(baseP[n]) !== String(optP[n]);
            return `<tr class="${changed ? 'opt-changed' : ''}"><td>${C.escapeHtml(n)}</td><td>${C.escapeHtml(baseP[n] ?? '—')}</td>
                <td><strong>${C.escapeHtml(optP[n] ?? '—')}</strong></td></tr>`;
        }).join('') || '<tr><td colspan="3" class="muted">No valid result.</td></tr>';

        $('complianceList').innerHTML = (a.compliance || []).map((c) => `
            <li><span>${C.escapeHtml(c.metric)} ${C.escapeHtml(c.operator)} ${c.limit}</span>
            <span class="${c.passed ? 'pos' : 'neg'}">${c.passed ? '✓' : '✗'} ${C.fmtNum(c.actual, 2)}</span></li>`).join('')
            || '<li class="muted">No constraints.</li>';

        const levelIcon = { danger: '⛔', warning: '⚠️', info: 'ℹ️' };
        renderWarningPanel(a.warnings);
        renderRegimes(a.regimes);
        $('warningsList').innerHTML = (a.warnings || []).map((w) =>
            `<li class="opt-warn-${w.level}">${levelIcon[w.level] || '•'} ${C.escapeHtml(w.message)}</li>`).join('')
            || '<li class="pos">No overfitting warning signs detected.</li>';
        const st = a.stats || {};
        $('runStats').textContent = st.evaluations
            ? `${st.evaluations.toLocaleString()} backtests (${st.unique_results} unique, ${st.errors} errors) in ${C.fmtDuration(st.elapsed_seconds)} on ${st.workers} worker(s) · ${st.bars} bars ${st.data_from} → ${st.data_to}`
            : '';

        drawLineChart('equityChart', [
            { label: 'Current params', points: (a.curves || {}).baseline || [], color: '#94a3b8' },
            { label: 'Optimized', points: (a.curves || {}).optimized || [], color: '#3b82f6' },
        ]);
    }

    /**
     * PRD Part 2 §6.2 — warning signs, made impossible to miss.
     *
     * Sticky at the top of the results, not dismissable. The warnings were
     * already generated; the PRD's complaint is that they were easy to miss in
     * the layout, and a dismissable banner is the layout's answer to that. So:
     * no close button, and it stays put while the page scrolls.
     */
    function renderWarningPanel(warnings) {
        const el = $('warningPanel');
        if (!el) return;
        const list = (warnings || []).filter((w) => w.level !== 'info' || true);
        if (!list.length) { el.hidden = true; el.innerHTML = ''; return; }
        const danger = list.filter((w) => w.level === 'danger').length;
        const level = danger ? 'opt-warning-panel--danger' : 'opt-warning-panel--warning';
        const title = danger
            ? `⛔ ${danger} of ${list.length} warning sign${list.length === 1 ? '' : 's'} need attention`
            : `⚠️ ${list.length} warning sign${list.length === 1 ? '' : 's'}`;
        el.hidden = false;
        el.className = `opt-warning-panel ${level}`;
        el.innerHTML = `<div class="opt-warning-title">${C.escapeHtml(title)}</div>
            <ul class="opt-warning-list">${list.map((w) => `<li class="opt-warning-${w.level}">
                ${C.escapeHtml(w.message)}</li>`).join('')}</ul>`;
    }

    /**
     * PRD Part 2 §6.1 — how the winner behaved in each named period.
     *
     * A period with too few bars prints no Sharpe. The number would be there
     * and it would be larger than the number next to it, which reads as "this
     * period was better" when the truth is "this period is too short to say".
     */
    function renderRegimes(regimes) {
        const box = $('regimeBox');
        if (!box) return;
        if (!regimes || !regimes.available || !(regimes.periods || []).length) {
            box.hidden = true;
            box.innerHTML = '';
            return;
        }
        const uncovered = regimes.named_coverage_pct < 99.9;
        const rows = regimes.periods.map((p) => `<tr class="${p.named ? '' : 'opt-regime-unnamed'}">
            <td>${C.escapeHtml(p.label)}<small class="muted"> ${C.escapeHtml(p.from ? `${p.from} → ${p.to}` : 'outside the named bands')}</small></td>
            <td class="${p.return_pct >= 0 ? 'pos' : 'neg'}">${C.fmtNum(p.return_pct, 2)}%</td>
            <td>${p.sufficient ? C.fmtNum(p.sharpe, 2) : '<span class="muted" title="too few bars for a meaningful Sharpe">—</span>'}</td>
            <td>${C.fmtNum(p.max_drawdown_pct, 2)}%</td>
            <td>${p.trades === null ? '—' : p.trades}</td>
        </tr>`).join('');

        box.hidden = false;
        box.innerHTML = `<h2 class="card-title">Regime breakdown</h2>
            <p class="muted small">How the winning parameters behaved in each named period.
                ${uncovered ? `<strong class="neg">These bands cover only
                    ${C.fmtNum(regimes.named_coverage_pct, 1)}% of this run's bars</strong> — the
                    rest is shown as other.` : ''}</p>
            <div class="table-scroll"><table class="data-table opt-compact">
                <thead><tr><th>Period</th><th>Return</th><th>Sharpe</th><th>Max DD</th><th>Trades</th></tr></thead>
                <tbody>${rows}</tbody></table></div>
            <p class="muted small opt-regime-note">A period marked — has too few bars for a
                meaningful Sharpe (${regimes.min_bars} is the floor). The name describes what
                happened; nothing here predicts the next one.</p>`;
    }

    /**
     * PRD Part 2 §4 — Monte Carlo on the winning result.
     *
     * Re-runs the best parameters once (the engine's doing, server-side) and
     * resamples that result's trade sequence. The button is beside Apply to
     * Paper on purpose: this is the last check before those parameters reach a
     * runner, and the two answer different questions — walk-forward asks
     * whether the parameters generalise across time, this asks whether the
     * order of the trades was luck.
     */
    function renderMonteCarlo(mc) {
        const box = $('monteCarloBox');
        if (!box) return;
        if (!mc) { box.hidden = true; box.innerHTML = ''; return; }
        if (!mc.available) {
            box.hidden = false;
            box.innerHTML = `<h2 class="card-title">Monte Carlo on best result</h2>
                <p class="muted small">Not available: ${C.escapeHtml(mc.reason || 'unknown reason')}.</p>`;
            state.monteCarlo = null;
            return;
        }
        state.monteCarlo = mc;
        const boot = mc.bootstrap || {};
        const reorder = mc.reorder || {};
        const params = Object.entries(mc.params || {})
            .map(([k, v]) => `${C.escapeHtml(k)}=${C.escapeHtml(v)}`).join(', ');
        const lvl = { error: 'opt-mc-warn-error', warning: 'opt-mc-warn', info: 'opt-mc-info' };

        box.hidden = false;
        box.innerHTML = `<h2 class="card-title">Monte Carlo on best result</h2>
            <p class="muted small">${mc.simulations.toLocaleString()} resamples of
                <strong>${mc.trades}</strong> closed trades from
                <code>${params || '—'}</code>. The same trades in different orders, and a fresh
                draw from the same distribution.</p>
            <div class="opt-mc-grid">
                <div class="opt-mc-stat">
                    <span class="opt-mc-num ${boot.profit_probability_pct >= 60 ? 'pos' : 'neg'}">
                        ${C.fmtNum(boot.profit_probability_pct, 0)}%</span>
                    <span class="opt-mc-label">of sequences finished profitable</span>
                </div>
                <div class="opt-mc-stat">
                    <span class="opt-mc-num">${C.fmtNum(boot.p5_final_equity, 0)}</span>
                    <span class="opt-mc-label">5th percentile final equity</span>
                </div>
                <div class="opt-mc-stat">
                    <span class="opt-mc-num">${C.fmtNum(boot.p95_final_equity, 0)}</span>
                    <span class="opt-mc-label">95th percentile final equity</span>
                </div>
                <div class="opt-mc-stat">
                    <span class="opt-mc-num">${C.fmtNum(reorder.p95_max_drawdown_pct, 2)}%</span>
                    <span class="opt-mc-label">worst drawdown from reordering</span>
                </div>
            </div>
            <canvas id="mcFanChart" height="150"></canvas>
            <p class="muted small opt-mc-note">${C.escapeHtml(mc.drawdown_note || '')}</p>
            ${(mc.warnings || []).map((w) => `<div class="opt-mc-banner ${lvl[w.level] || 'opt-mc-info'}">
                ${C.escapeHtml(w.message)}</div>`).join('')}`;
        drawFanChart('mcFanChart', mc);
    }

    /** The 5–95% envelope of the bootstrap resamples. */
    function drawFanChart(canvasId, mc) {
        if (typeof Chart === 'undefined') return;
        const fan = mc.fan || {};
        const bands = fan.bands || {};
        if (!bands['50'] || !bands['50'].length) return;
        const ctx = document.getElementById(canvasId);
        if (!ctx) return;
        const canvas = Chart.getChart(ctx);
        if (canvas) canvas.destroy();
        const labels = Array.from({ length: fan.points }, (_, i) => i);
        new Chart(ctx, {
            type: 'line',
            data: {
                labels,
                datasets: [
                    { label: 'p95', data: bands['95'], borderColor: 'rgba(148,163,184,.55)',
                      backgroundColor: 'rgba(148,163,184,.16)', fill: '+1', pointRadius: 0,
                      borderWidth: 1, tension: 0.1 },
                    { label: 'p50', data: bands['50'], borderColor: '#3b82f6',
                      backgroundColor: 'rgba(59,130,246,.18)', fill: '+1', pointRadius: 0,
                      borderWidth: 2, tension: 0.1 },
                    { label: 'p5', data: bands['5'], borderColor: 'rgba(148,163,184,.55)',
                      backgroundColor: 'transparent', fill: false, pointRadius: 0,
                      borderWidth: 1, tension: 0.1 },
                ],
            },
            options: {
                responsive: true, maintainAspectRatio: false, animation: false,
                interaction: { mode: 'index', intersect: false },
                plugins: {
                    legend: { labels: { color: '#e2e8f0' } },
                    title: { display: true, color: '#94a3b8',
                        text: `Equity by trade — 5th to 95th percentile of ${mc.simulations} resamples` },
                },
                scales: {
                    x: { title: { display: true, color: '#94a3b8', text: 'trade' },
                         ticks: { color: '#94a3b8', maxTicksLimit: 8 }, grid: { color: 'rgba(51,65,85,.4)' } },
                    y: { ticks: { color: '#94a3b8' }, grid: { color: 'rgba(51,65,85,.4)' } },
                },
            },
        });
    }

    async function runMonteCarlo() {
        const btn = $('btnMonteCarlo');
        const previous = btn.textContent;
        btn.disabled = true;
        btn.textContent = '🎲 Resampling…';
        try {
            const res = await C.api(`${base}/monte-carlo`, { method: 'POST', body: {} });
            renderMonteCarlo(res.monte_carlo);
        } catch (e) {
            renderMonteCarlo({ available: false, reason: e.message });
            C.toast(e.message, 'error');
        } finally {
            btn.disabled = false;
            btn.textContent = previous;
        }
    }

    function drawLineChart(canvasId, series) {
        if (typeof Chart === 'undefined') return;
        if (state.charts[canvasId]) state.charts[canvasId].destroy();
        const labels = (series.find((s) => s.points.length) || { points: [] }).points.map((p) => p[0]);
        state.charts[canvasId] = new Chart($(canvasId), {
            type: 'line',
            data: {
                labels,
                datasets: series.map((s) => ({
                    label: s.label, data: s.points.map((p) => p[1]), borderColor: s.color,
                    backgroundColor: s.color, pointRadius: 0, borderWidth: 2, tension: 0.1,
                })),
            },
            options: {
                responsive: true, maintainAspectRatio: false, animation: false,
                interaction: { mode: 'index', intersect: false },
                plugins: { legend: { labels: { color: '#e2e8f0' } } },
                scales: {
                    x: { ticks: { color: '#94a3b8', maxTicksLimit: 10 }, grid: { color: 'rgba(51,65,85,.4)' } },
                    y: { ticks: { color: '#94a3b8' }, grid: { color: 'rgba(51,65,85,.4)' } },
                },
            },
        });
    }

    // ---------------------------------------------------------------- heatmap

    function setupHeatmap() {
        const names = (state.run.analysis && state.run.analysis.optimized_params)
            || (state.run.param_space || []).filter((p) => p.optimize).map((p) => p.name);
        const pane = document.querySelector('[data-pane="heatmaps"]');
        const tabBtn = document.querySelector('.tab[data-tab="heatmaps"]');
        if (names.length < 2) {
            tabBtn.hidden = true;
            pane.innerHTML = '<div class="card muted">Heatmaps need at least two optimized parameters.</div>';
            return;
        }
        const opts = names.map((n) => `<option value="${C.escapeHtml(n)}">${C.escapeHtml(n)}</option>`).join('');
        $('heatX').innerHTML = opts; $('heatY').innerHTML = opts;
        $('heatX').value = names[0]; $('heatY').value = names[1];
        ['heatX', 'heatY', 'heatMetric', 'heatAgg', 'heatCompliant'].forEach((id) =>
            $(id).addEventListener('change', loadHeatmap));
        loadHeatmap();
    }

    async function loadHeatmap() {
        const x = $('heatX').value, y = $('heatY').value;
        if (x === y) { $('heatmap').innerHTML = '<div class="muted small">Pick two different parameters.</div>'; return; }
        const q = new URLSearchParams({ x, y, metric: $('heatMetric').value, agg: $('heatAgg').value,
            compliant: $('heatCompliant').checked ? '1' : '' });
        try {
            const res = await C.api(`${base}/heatmap?${q}`);
            renderHeatmap(res.heatmap);
        } catch (e) {
            $('heatmap').innerHTML = `<div class="muted small">${C.escapeHtml(e.message)}</div>`;
        }
    }

    function renderHeatmap(h) {
        const metric = h.metric;
        const norm = C.normalise(h.z);
        const best = state.run.best_params || {};
        const fmt = (v) => (metric === 'score' ? C.fmtNum(v, 2) : C.fmtMetric(metric, v));
        let html = `<table class="opt-heat-table"><thead><tr><th class="opt-heat-corner">${C.escapeHtml(h.y_param)} ↓ / ${C.escapeHtml(h.x_param)} →</th>`
            + h.x_values.map((x) => `<th>${x}</th>`).join('') + '</tr></thead><tbody>';
        // highest Y at the top, like a chart
        for (let yi = h.y_values.length - 1; yi >= 0; yi -= 1) {
            const yv = h.y_values[yi];
            html += `<tr><th>${yv}</th>`;
            h.x_values.forEach((xv, xi) => {
                const v = h.z[yi][xi];
                const t = norm.scale(v);
                const isBest = String(best[h.x_param]) === String(xv) && String(best[h.y_param]) === String(yv);
                html += v === null
                    ? '<td class="opt-heat-empty">·</td>'
                    : `<td class="${isBest ? 'opt-heat-best' : ''}" style="background:${C.heatColor(t)}"
                        title="${C.escapeHtml(h.x_param)}=${xv}, ${C.escapeHtml(h.y_param)}=${yv}: ${fmt(v)}">${fmt(v)}</td>`;
            });
            html += '</tr>';
        }
        html += '</tbody></table>';
        $('heatmap').innerHTML = html;
        $('heatLegend').innerHTML = norm.min === null ? '' :
            `<span>${fmt(norm.min)}</span><span class="opt-heat-bar"></span><span>${fmt(norm.max)}</span><span class="muted small">★ outlined = chosen optimum</span>`;
        const aggText = { max: 'Each cell shows the best value over the other parameters.',
            mean: 'Each cell averages over the other parameters (robustness view).',
            slice: 'Other parameters are fixed at the optimum.' }[h.agg];
        $('heatNote').textContent = `${aggText} Coverage ${(h.coverage * 100).toFixed(0)}% of cells${h.coverage < 1 ? ' (sampling methods leave gaps)' : ''}.`;
    }

    // ------------------------------------------------------------ walk-forward

    function renderWalkForward() {
        const r = state.run;
        const wf = r.walk_forward_results;
        $('wfEmpty').hidden = !!r.walk_forward_enabled;
        $('wfBox').hidden = !wf;
        if (!wf) return;
        const v = $('wfVerdict');
        v.className = `opt-verdict ${wf.overfitted ? 'opt-verdict-bad' : (wf.overfitted === false ? 'opt-verdict-ok' : '')}`;
        v.textContent = `${wf.overfitted ? '⛔ Likely overfitted — ' : (wf.overfitted === false ? '✅ Holds up out-of-sample — ' : '')}${wf.verdict}`;
        $('wfTrain').textContent = C.fmtNum(wf.avg_train_score, 3);
        $('wfTest').textContent = C.fmtNum(wf.avg_test_score, 3);
        $('wfDeg').textContent = C.fmtNum(wf.avg_degradation, 3);
        $('wfEff').textContent = C.isNum(wf.efficiency) ? C.fmtNum(wf.efficiency, 2) : '—';
        $('wfPos').textContent = `${wf.positive_test_splits}/${wf.n_scored}`;
        $('wfSplits').innerHTML = wf.splits.map(wfRow).join('');
        $('wfStability').innerHTML = Object.entries(wf.param_stability || {}).map(([k, s]) => `
            <tr><td>${C.escapeHtml(k)}</td><td class="small">${s.values.join(', ')}</td><td>${C.fmtNum(s.mean, 2)}</td>
            <td>${C.fmtNum(s.std, 2)}</td><td class="${C.isNum(s.cv) && s.cv > 0.5 ? 'neg' : ''}">${C.fmtNum(s.cv, 2)}</td></tr>`).join('');
        if (typeof Chart !== 'undefined') {
            if (state.charts.wfChart) state.charts.wfChart.destroy();
            state.charts.wfChart = new Chart($('wfChart'), {
                type: 'bar',
                data: {
                    labels: wf.splits.map((s) => `#${s.split}`),
                    datasets: [
                        { label: 'Train', data: wf.splits.map((s) => s.train_score), backgroundColor: 'rgba(148,163,184,.7)' },
                        { label: 'Test', data: wf.splits.map((s) => s.test_score), backgroundColor: 'rgba(59,130,246,.85)' },
                    ],
                },
                options: {
                    responsive: true, maintainAspectRatio: false, animation: false,
                    plugins: { legend: { labels: { color: '#e2e8f0' } } },
                    scales: { x: { ticks: { color: '#94a3b8' } }, y: { ticks: { color: '#94a3b8' }, grid: { color: 'rgba(51,65,85,.4)' } } },
                },
            });
        }
        drawLineChart('wfEquityChart', [{ label: 'Out-of-sample (stitched test windows)', points: wf.oos_curve || [], color: '#7fc8a0' }]);
    }

    // ------------------------------------------------------------ sensitivity

    function renderSensitivity() {
        const sens = (state.run.analysis || {}).sensitivity || {};
        const grid = $('sensGrid');
        const names = Object.keys(sens);
        if (!names.length) { grid.innerHTML = '<div class="card muted">No sensitivity data (no valid optimum).</div>'; return; }
        const labelCls = { stable: 'pos', moderate: 'opt-warn-text', sensitive: 'neg' };
        grid.innerHTML = names.map((n, i) => {
            const s = sens[n];
            return `<div class="card opt-sens-card"><div class="card-head"><h3 class="card-title">${C.escapeHtml(n)}</h3>
                <span class="${labelCls[s.label] || 'muted'}">${C.escapeHtml(s.label)}</span></div>
                <div class="small muted">optimum ${s.best_value}${s.plateau ? ` · plateau ${s.plateau[0]} … ${s.plateau[1]}` : ''}
                ${C.isNum(s.stability) ? ` · stability ${(s.stability * 100).toFixed(0)}%` : ''}</div>
                <div class="chart-wrap opt-chart-xs"><canvas id="sens_${i}"></canvas></div></div>`;
        }).join('');
        if (typeof Chart === 'undefined') return;
        names.forEach((n, i) => {
            const s = sens[n];
            const inPlateau = (v) => s.plateau && Number(v) >= Number(s.plateau[0]) && Number(v) <= Number(s.plateau[1]);
            new Chart($(`sens_${i}`), {
                type: 'line',
                data: {
                    labels: s.values,
                    datasets: [{
                        label: 'score', data: s.scores, borderColor: '#8b5cf6', borderWidth: 2, tension: 0.15,
                        pointRadius: s.values.map((v) => (String(v) === String(s.best_value) ? 6 : 3)),
                        pointBackgroundColor: s.values.map((v) => (String(v) === String(s.best_value) ? '#d4b26a' : (inPlateau(v) ? '#7fc8a0' : '#64748b'))),
                    }],
                },
                options: {
                    responsive: true, maintainAspectRatio: false, animation: false,
                    plugins: { legend: { display: false } },
                    scales: { x: { ticks: { color: '#94a3b8' } }, y: { ticks: { color: '#94a3b8' }, grid: { color: 'rgba(51,65,85,.4)' } } },
                },
            });
        });
    }

    // ------------------------------------------------------------ all results

    const RESULT_COLS = [
        ['rank', 'Rank'], ['objective_score', 'Score'], ['sharpe', 'Sharpe'], ['total_return', 'Return'],
        ['max_drawdown', 'Max DD'], ['win_rate', 'Win %'], ['total_trades', 'Trades'],
        ['profit_factor', 'PF'], ['calmar', 'Calmar'], ['expectancy', 'Expectancy'],
    ];

    async function loadResults() {
        const pg = state.page;
        const q = new URLSearchParams({ sort: pg.sort, order: pg.order, limit: pg.limit, offset: pg.offset,
            compliant: $('resCompliant').checked ? '1' : '' });
        let res;
        try { res = await C.api(`${base}/results?${q}`); } catch (e) {
            $('resBody').innerHTML = `<tr><td class="muted">${C.escapeHtml(e.message)}</td></tr>`; return;
        }
        pg.total = res.total;
        const pnames = Array.from(new Set(res.results.flatMap((r) => Object.keys(r.params || {}))));
        $('resTotal').textContent = `(${res.total.toLocaleString()})`;
        $('resHead').innerHTML = `<tr>${pnames.map((n) => `<th class="opt-nosort">${C.escapeHtml(n)}</th>`).join('')}${RESULT_COLS.map(([k, l]) =>
            `<th data-sort="${k}" class="${pg.sort === k ? 'opt-sorted' : ''}">${l}${pg.sort === k ? (pg.order === 'desc' ? ' ▼' : ' ▲') : ''}</th>`).join('')}<th class="opt-nosort"></th></tr>`;
        $('resBody').innerHTML = res.results.map((r, i) => `
            <tr class="${r.constraints_met ? '' : 'opt-row-off'}" data-i="${i}">
                ${pnames.map((n) => `<td>${C.escapeHtml((r.params || {})[n] ?? '')}</td>`).join('')}
                <td>${r.rank ?? '—'}</td><td><strong>${C.fmtNum(r.objective_score, 3)}</strong></td>
                ${RESULT_COLS.slice(2).map(([k]) => `<td>${C.fmtMetric(k, r[k])}</td>`).join('')}
                <td class="opt-row-actions">${r.constraints_met ? `<button class="btn btn-sm" data-act="apply">Apply</button><button class="btn btn-sm" data-act="preset">★</button>` : `<span class="neg small" title="${C.escapeHtml(violationText(r))}">✗</span>`}</td>
            </tr>`).join('');
        $('resHead').querySelectorAll('th[data-sort]').forEach((th) => th.addEventListener('click', () => {
            const k = th.dataset.sort;
            if (pg.sort === k) pg.order = pg.order === 'desc' ? 'asc' : 'desc';
            else { pg.sort = k; pg.order = k === 'rank' ? 'asc' : 'desc'; }
            pg.offset = 0; loadResults();
        }));
        $('resBody').querySelectorAll('button[data-act]').forEach((b) => b.addEventListener('click', () => {
            const r = res.results[Number(b.closest('tr').dataset.i)];
            if (b.dataset.act === 'apply') openApply(r.params); else openPreset(r.params);
        }));
        const pageNo = Math.floor(pg.offset / pg.limit) + 1;
        const pages = Math.max(1, Math.ceil(pg.total / pg.limit));
        $('resPage').textContent = `page ${pageNo} / ${pages}`;
        $('resPrev').disabled = pg.offset === 0;
        $('resNext').disabled = pg.offset + pg.limit >= pg.total;
    }

    function violationText(r) {
        return (r.constraint_violations || []).map((v) => (v.metric
            ? `${v.metric} ${v.operator} ${v.limit} (got ${v.actual})` : String(v.error || ''))).join('; ');
    }

    // ------------------------------------------------------------------ audit

    async function loadAudit() {
        try {
            const res = await C.api(`/api/optimize/audit?run_id=${encodeURIComponent(runId)}`);
            $('auditBody').innerHTML = res.audit.map((a) => {
                const d = a.action_details || {};
                const diff = Object.entries(a.params_diff || {}).map(([k, v]) => `${k}: ${v.old ?? '—'} → ${v.new ?? '—'}`).join(', ');
                const canRollback = a.action === 'apply' && !d.rolled_back;
                // PRD Part 2 §2 — the audit row is the last place a run's data
                // can be checked, long after the results page has scrolled away.
                const att = d.data_attestation;
                const attLine = att
                    ? ` <span class="${att.data_source_real ? 'muted' : 'neg'}" title="${C.escapeHtml(att.date_from || '')} → ${C.escapeHtml(att.date_to || '')}">${C.escapeHtml(att.data_source_label || att.data_source || '')}</span>`
                    : '';
                // PRD Part 2 §1: the audit row is where the full chain is
                // readable years later — the origin handle is shown as a
                // session handle (it is one), not as a record id.
                const originLine = d.originated_from_backtest_id
                    ? ` <span class="muted small" title="Session handle from the Backtest page">← BT ${C.escapeHtml(d.originated_from_backtest_id)}</span>`
                    : '';
                return `<tr><td class="small">${C.fmtDate(a.timestamp)}</td><td>${C.escapeHtml(a.action)}${d.rolled_back ? ' <span class="muted small">(rolled back)</span>' : ''}${attLine}${originLine}</td>
                    <td class="small">${C.escapeHtml(d.target || a.applied_to_mode || '—')}${d.new_instance_id ? ` · ${C.escapeHtml(String(d.new_instance_id).slice(0, 8))}` : ''}</td>
                    <td class="small">${C.escapeHtml(diff || '—')}</td><td class="small">${C.escapeHtml(a.user_id || '—')}</td>
                    <td>${canRollback ? `<button class="btn btn-sm" data-rollback="${a.audit_id}">↶ Rollback</button>` : ''}</td></tr>`;
            }).join('') || '<tr><td colspan="6" class="muted">Nothing applied from this run yet.</td></tr>';
            $('auditBody').querySelectorAll('[data-rollback]').forEach((b) => b.addEventListener('click', () => rollback(b.dataset.rollback)));
        } catch (e) {
            $('auditBody').innerHTML = `<tr><td colspan="6" class="muted">${C.escapeHtml(e.message)}</td></tr>`;
        }
    }

    async function rollback(auditId) {
        if (!window.confirm('Roll back this change? A restarted runner gets its previous parameters; a spawned runner is removed.')) return;
        try {
            await C.api(`/api/optimize/audit/${auditId}/rollback`, { method: 'POST', body: {} });
            C.toast('Rolled back');
            loadAudit();
        } catch (e) { C.toast(e.message, 'error'); }
    }

    // ------------------------------------------------------------ apply modal

    function paramsPreview(params) {
        const baseP = state.run.baseline_params || {};
        return `<table class="data-table opt-compact"><thead><tr><th>Parameter</th><th>Current</th><th>New</th></tr></thead><tbody>${
            Object.entries(params).map(([k, v]) => `<tr class="${String(baseP[k]) !== String(v) ? 'opt-changed' : ''}"><td>${C.escapeHtml(k)}</td><td>${C.escapeHtml(baseP[k] ?? '—')}</td><td><strong>${C.escapeHtml(v)}</strong></td></tr>`).join('')
        }</tbody></table>`;
    }

    /**
     * PRD §6 reverse flow: the three-step chain, shown BEFORE applying so the
     * user sees where the parameters came from and where they are going.
     *
     * The first link is a SESSION handle minted by the Backtest page, not a
     * stored backtest record — nothing about a completed backtest is
     * persisted. It is labelled as such rather than dressed up as an id that
     * resolves to something.
     */
    function renderChain() {
        const el = $('applyChain');
        if (!el) return;
        const origin = (state.run && state.run.backtest_config || {}).sourceBacktestId;
        const t = applyTarget();
        const runnerId = ($('applyRunner') || {}).value || '';
        const spawning = (t === 'paper' || t === 'ab_test') && !runnerId;
        const links = [
            {
                id: origin || null,
                label: 'Backtest result',
                note: origin ? `session handle ${origin}` : 'not started from a backtest',
                missing: !origin,
            },
            {
                id: (state.run && state.run.run_id) || null,
                label: 'Optimize run',
                note: (state.run && state.run.run_id) || '—',
                missing: false,
            },
            {
                id: t === 'none' ? null : (spawning ? 'new runner' : runnerId),
                label: t === 'live' ? 'Live runner' : 'Paper runner',
                note: t === 'none' ? 'record only — no runner changes'
                    : spawning ? 'a new runner will be created'
                        : (runnerId || '—'),
                missing: false,
            },
        ];
        el.innerHTML = `<span class="opt-chain-title">Applied from</span>` + links.map((l, i) => {
            const cls = l.missing ? 'opt-chain-missing' : '';
            const arrow = i < links.length - 1 ? '<span class="opt-chain-arrow">→</span>' : '';
            return `<span class="opt-chain-link ${cls}">`
                + `<span class="opt-chain-label">${C.escapeHtml(l.label)}</span>`
                + `<code>${C.escapeHtml(l.id || '—')}</code>`
                + `<span class="opt-chain-note">${C.escapeHtml(l.note)}</span></span>${arrow}`;
        }).join('');
    }

    async function openApply(params) {
        state.applyParams = params || state.run.best_params;
        state.applyStep = 1;
        renderApplyChecks();
        renderChain();
        renderMonteCarloGate();
        $('applyParams').innerHTML = paramsPreview(state.applyParams);
        $('applyError').innerHTML = '';
        $('applyConfirmLive').checked = false;
        $('applyAllowUnvalidated').checked = false;
        $('applyConfirmText').value = '';
        $('applyNotes').value = '';
        // PRD §5 step 2: default to the recommended first step (new paper
        // runner), not "record only" — the modal exists to get a validated
        // result onto paper, and pre-selecting the do-nothing option made
        // most applies two clicks of pure ceremony away from that.
        document.querySelector('input[name="applyTarget"][value="paper"]').checked = true;
        $('applyModal').hidden = false;
        try {
            const res = await C.api(`/api/optimize/runners?strategy=${encodeURIComponent(state.run.strategy_id)}`);
            state.runners = res.runners || [];
        } catch (_) { state.runners = []; }
        syncApplyTarget();
        syncApplyStep();
    }

    // ------------------------------------------------------------------------
    // PRD backTest-enhance §5 — the 3-step wizard.
    // Step 1 REVIEW: the checks table. Step 2 TARGET: where, with the live
    // 30-day paper-history gate as an inline explanation. Step 3 CONFIRM:
    // chain + diff + type-"CONFIRM" handshake. The Apply button is never
    // disabled; unreadiness is a visible table row, not a dead control.
    // ------------------------------------------------------------------------

    const APPLY_STEPS = 3;

    function syncApplyStep() {
        const step = state.applyStep || 1;
        document.querySelectorAll('#applyModal .opt-apply-step').forEach((el) => {
            el.hidden = Number(el.dataset.step) !== step;
        });
        $('applyBack').hidden = step === 1;
        $('applyNext').hidden = step === APPLY_STEPS;
        $('applyConfirm').hidden = step !== APPLY_STEPS;
        $('applyNext').textContent = step === 1 ? 'Continue to Step 2 →' : 'Continue to Step 3 →';
        if (step === 3) {
            renderChain();
            // Confirm-step chain + diff render into their own nodes so going
            // Back to Step 2 and changing the target re-renders them.
            const chain3 = $('applyChainConfirm');
            if (chain3) chain3.innerHTML = $('applyChain').innerHTML;
            $('applyParamsConfirm').innerHTML = paramsPreview(state.applyParams);
            $('applyConfirmText').focus();
        }
        $('applyError').innerHTML = '';
    }

    function applyStepNext() {
        const step = state.applyStep || 1;
        if (step === 2) {
            const t = applyTarget();
            if (t === 'live' && !$('applyConfirmLive').checked) {
                $('applyError').innerHTML = '<div>Tick the live acknowledgement in Step 2 first — these parameters would trade real money.</div>';
                return;
            }
            if ((t === 'paper' || t === 'ab_test') && $('applyRunnerRow').hidden === false && !$('applyRunner').value && t === 'live') {
                $('applyError').innerHTML = '<div>Select a live runner.</div>';
                return;
            }
        }
        state.applyStep = Math.min(APPLY_STEPS, step + 1);
        syncApplyStep();
    }

    function applyStepBack() {
        state.applyStep = Math.max(1, (state.applyStep || 1) - 1);
        syncApplyStep();
    }

    /**
     * §5 step 1: the checks table. Rows are assembled from what this run
     * actually carries — walk-forward, robustness, deflated Sharpe, Monte
     * Carlo, data source, trade count — with ✅/⚠/❌ per row. A missing check
     * renders as ⚠ "not run", never as a pass.
     */
    function renderApplyChecks() {
        const body = $('applyChecksBody');
        const summary = $('applyReviewSummary');
        if (!body) return;
        const r = state.run || {};
        const wf = r.walk_forward_results || {};
        const att = r.data_attestation || {};
        const metrics = r.best_metrics || {};
        const mc = (state.monteCarlo && state.monteCarlo.available) ? state.monteCarlo : null;
        const rows = [
            {
                label: 'Walk-forward run',
                value: r.walk_forward_enabled ? 'Yes' : 'No',
                cls: r.walk_forward_enabled ? 'ok' : 'warn',
            },
            {
                label: 'Walk-forward pass',
                value: r.walk_forward_enabled && C.isNum(wf.efficiency) ? `${C.fmtNum(wf.efficiency, 2)} eff` : 'not run',
                cls: !r.walk_forward_enabled ? 'warn'
                    : (wf.overfitted === true ? 'bad' : (wf.overfitted === false && C.isNum(wf.efficiency) && wf.efficiency >= 0.5 ? 'ok' : 'warn')),
            },
            {
                label: 'Robustness score',
                value: C.isNum(r.robustness_score) ? `${C.fmtNum(r.robustness_score, 1)}/10` : 'not scored',
                cls: C.isNum(r.robustness_score) ? (r.robustness_score >= 6 ? 'ok' : 'warn') : 'warn',
            },
            {
                label: 'Deflated Sharpe',
                value: C.isNum(r.deflated_sharpe) ? C.fmtNum(r.deflated_sharpe, 2) : 'not computed',
                cls: C.isNum(r.deflated_sharpe) ? (r.deflated_sharpe >= 0.5 ? 'ok' : 'warn') : 'warn',
            },
            {
                label: 'Monte Carlo P(profit)',
                value: mc ? `${C.fmtNum((mc.bootstrap || {}).profit_probability_pct, 0)}%` : 'not run',
                cls: mc ? (((mc.bootstrap || {}).profit_probability_pct >= 60) ? 'ok' : 'warn') : 'warn',
            },
            {
                label: 'Data source',
                value: att.data_source_label || att.data_source || r.data_source || 'unknown',
                cls: att.data_source_real || r.data_source_real ? 'ok' : 'warn',
            },
            {
                label: 'Trade count',
                value: String(C.isNum(metrics.total_trades) ? metrics.total_trades : (r.best_score != null && metrics.total_trades === undefined ? '—' : '—')),
                cls: (metrics.total_trades >= 30) ? 'ok' : 'warn',
            },
        ];
        const bad = rows.filter((x) => x.cls === 'bad').length;
        const warn = rows.filter((x) => x.cls === 'warn').length;
        body.innerHTML = rows.map((x) => `
            <tr><td>${C.escapeHtml(x.label)}</td><td>${C.escapeHtml(x.value)}</td>
            <td class="opt-check-${x.cls}">${x.cls === 'ok' ? '✅' : x.cls === 'bad' ? '❌' : '⚠️'}</td></tr>`).join('');
        if (summary) {
            summary.innerHTML = bad
                ? `<span class="neg">${bad} check${bad > 1 ? 's' : ''} failed — review before applying anywhere, and do not apply to live.</span>`
                : warn
                    ? `<span class="opt-warn-text">${warn} check${warn > 1 ? 's' : ''} incomplete or weak — readable as “not certifiable yet”, not as a block.</span>`
                    : `<span class="pos">All checks passed.</span>`;
        }
    }

    /**
     * PRD Part 2 §4's gate behaviour: low P(profit) is a **visible flag that
     * must be acknowledged**, not a block.
     *
     * A block would be wrong here. Monte Carlo is opt-in — the user has to
     * press the button to get a result — so a hard gate on it would mean the
     * apply path silently depends on a check that may never have been run. A
     * flag that says "this has not been tested, or tested badly" is the honest
     * version of the same idea.
     */
    function renderMonteCarloGate() {
        const box = $('applyMcGate');
        if (!box) return;
        const mc = state.monteCarlo;
        if (!mc || !mc.available) {
            box.hidden = true;
            box.innerHTML = '';
            return;
        }
        const pct = (mc.bootstrap || {}).profit_probability_pct;
        const low = C.isNum(pct) && pct < 60;
        box.hidden = false;
        box.innerHTML = low
            ? `<label class="small"><input type="checkbox" id="applyMcAck"> Monte Carlo on the best
                 result found only <strong class="neg">${C.fmtNum(pct, 0)}%</strong> of resampled
                 trade sequences profitable. I have read that.</label>`
            : `<p class="small pos">Monte Carlo: ${C.fmtNum(pct, 0)}% of resampled trade sequences
                 finished profitable.</p>`;
    }

    function applyTarget() {
        const raw = (document.querySelector('input[name="applyTarget"]:checked') || {}).value || 'none';
        // §5 step 2 splits "paper" into new vs replace for presentation; the
        // API vocabulary is still (none | paper | ab_test | live).
        return raw === 'paper_replace' ? 'paper' : raw;
    }

    function syncApplyTarget() {
        const raw = (document.querySelector('input[name="applyTarget"]:checked') || {}).value || 'none';
        const t = applyTarget();
        const row = $('applyRunnerRow');
        const sel = $('applyRunner');
        renderChain();
        const wantMode = t === 'live' ? 'live' : 'paper';
        const runners = state.runners.filter((r) => r.mode === wantMode);
        // §5 step 2: the replace option carries its own runner picker inline;
        // new-paper and A/B never show one.
        const replaceMode = raw === 'paper_replace';
        row.hidden = !(replaceMode || t === 'live');
        let opts = runners.map((r) => `<option value="${C.escapeHtml(r.instance_id)}">${C.escapeHtml(r.name)} · ${C.escapeHtml(r.status)} · ${C.escapeHtml((r.symbols || []).join(','))}</option>`).join('');
        if (t === 'paper' && !replaceMode) opts = '';
        if (t === 'live') opts = opts || '<option value="">No matching live runner</option>';
        sel.innerHTML = opts || '<option value="">No matching paper runner</option>';
        $('applyRunnerHint').textContent = {
            paper: replaceMode
                ? 'Selecting a runner flattens it and restarts it with the new parameters.'
                : 'A new paper runner is created with these parameters.',
            ab_test: 'A new paper runner (B) is spawned; the existing runner (A) is left untouched as the control.',
            live: 'The live runner is flattened and restarted with the new parameters.',
        }[t] || '';
        renderLiveHistoryNote();
        const r = state.run;
        $('applyLiveBox').hidden = t !== 'live';
        if (t === 'live') {
            const gates = [];
            if (r.overfitted) gates.push('<span class="neg">⛔ Walk-forward flagged this run as overfitted — live apply is blocked.</span>');
            if (!r.walk_forward_enabled) gates.push('<span class="opt-warn-text">⚠ Not walk-forward validated.</span>');
            if (!gates.length) gates.push('<span class="pos">✓ Walk-forward validated, not overfitted.</span>');
            $('applyLiveGate').innerHTML = gates.join('<br>');
            $('applyAllowUnvalidated').parentElement.hidden = !!r.walk_forward_enabled;
        }
    }

    /**
     * §5 step 2's live gate, as an INLINE EXPLANATION rather than a dead
     * control: the LIVE radio is never disabled, but the note under it says
     * exactly why the result is not live-ready — no paper runner for this
     * strategy with ≥30 days of history. The server still enforces the real
     * gate; this is the part the operator reads.
     */
    function renderLiveHistoryNote() {
        const el = $('applyLiveHistoryNote');
        if (!el) return;
        const paperRunners = state.runners.filter((r) => r.mode === 'paper');
        const qualified = paperRunners.filter((r) => {
            const started = r.created_ts ? Date.parse(r.created_ts) : NaN;
            return isFinite(started) && (Date.now() - started) >= 30 * 86400000;
        });
        if (qualified.length) {
            el.innerHTML = `✓ Paper history available: ${qualified.length} paper runner${qualified.length > 1 ? 's' : ''} with ≥ 30 days of history for this strategy.`;
            el.classList.remove('neg');
        } else if (paperRunners.length) {
            el.innerHTML = `⚠ Not ready: no paper runner for this strategy has ≥ 30 days of history yet. Test on paper first — the server will refuse a live apply without it.`;
            el.classList.add('opt-warn-text');
        } else {
            el.innerHTML = `⚠ Not ready: this strategy has no paper runner at all. “New Paper Runner” above is the recommended first step.`;
            el.classList.add('opt-warn-text');
        }
    }

    async function confirmApply() {
        // §5 step 3: the type-"CONFIRM" handshake replaces the old checkbox.
        // A checkbox is one reflexive click; typing a word is a decision.
        // The comparison is case-insensitive on purpose: what matters is that
        // the operator typed the word, not their shift-key discipline.
        if (String($('applyConfirmText').value || '').trim().toUpperCase() !== 'CONFIRM') {
            $('applyError').innerHTML = '<div>Type CONFIRM to apply these parameters.</div>';
            return;
        }
        const t = applyTarget();
        // The Monte Carlo acknowledgement is required only when it is on
        // screen and unchecked. A checkbox nobody was shown must not become a
        // gate that silently blocks a run which never had the test run on it.
        const ack = $('applyMcAck');
        if (ack && !ack.checked) {
            $('applyError').innerHTML = `<div>Tick the Monte Carlo acknowledgement to apply
                these parameters.</div>`;
            return;
        }
        const body = {
            target: t, params: state.applyParams, instance_id: $('applyRunner').value || null,
            confirm_live: $('applyConfirmLive').checked, allow_unvalidated: $('applyAllowUnvalidated').checked,
            monte_carlo_acknowledged: !!ack,
            monte_carlo_profit_probability: state.monteCarlo && state.monteCarlo.available
                ? (state.monteCarlo.bootstrap || {}).profit_probability_pct : null,
            notes: $('applyNotes').value || null,
        };
        $('applyConfirm').disabled = true;
        try {
            const res = await C.api(`${base}/apply`, { method: 'POST', body });
            $('applyModal').hidden = true;
            C.toast(res.instance_id ? `Applied — runner ${String(res.instance_id).slice(0, 8)}` : 'Recorded in the audit trail');
            loadAudit();
        } catch (e) {
            $('applyError').innerHTML = `<div>${C.escapeHtml(e.message)}</div>`;
        } finally { $('applyConfirm').disabled = false; }
    }

    function openPreset(params) {
        state.presetParams = params || null;
        $('presetParams').innerHTML = paramsPreview(params || state.run.best_params || {});
        $('presetName').value = `${state.run.strategy_id} ${state.run.objective_function} ${new Date().toISOString().slice(0, 10)}`;
        $('presetDesc').value = '';
        $('presetModal').hidden = false;
    }

    async function confirmPreset() {
        try {
            const res = await C.api(`${base}/presets`, { method: 'POST', body: {
                name: $('presetName').value, description: $('presetDesc').value || null,
                params: state.presetParams,
            } });
            $('presetModal').hidden = true;
            C.toast(`Preset "${res.preset.name}" saved`);
        } catch (e) { C.toast(e.message, 'error'); }
    }

    // ---------------------------------------------------------------- actions

    async function action(name, body) {
        try {
            const res = await C.api(`${base}/${name}`, { method: 'POST', body: body || {} });
            if (name === 'rerun') { window.location.href = `/optimize/runs/${res.run_id}`; return; }
            state.run = res.run;
            refresh();
        } catch (e) { C.toast(e.message, 'error'); }
    }

    function bind() {
        $('btnPause').addEventListener('click', () => action('pause'));
        $('btnResume').addEventListener('click', () => action('resume'));
        $('btnCancel').addEventListener('click', () => {
            if (window.confirm('Cancel this optimization? Results computed so far are kept.')) action('cancel');
        });
        $('btnStartDraft').addEventListener('click', () => action('start'));
        $('btnRerun').addEventListener('click', () => action('rerun'));
        $('btnRerunWf').addEventListener('click', () => action('rerun', { overrides: { walkForward: { enabled: true } } }));
        $('btnApply').addEventListener('click', () => openApply());
        $('btnMonteCarlo').addEventListener('click', () => runMonteCarlo());
        $('btnPreset').addEventListener('click', () => openPreset());
        $('applyConfirm').addEventListener('click', confirmApply);
        $('applyNext').addEventListener('click', applyStepNext);
        $('applyBack').addEventListener('click', applyStepBack);
        $('applyConfirmText').addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && state.applyStep === APPLY_STEPS) confirmApply();
        });
        $('presetConfirm').addEventListener('click', confirmPreset);
        document.querySelectorAll('input[name="applyTarget"]').forEach((r) => r.addEventListener('change', syncApplyTarget));
        $('applyRunner').addEventListener('change', renderChain);
        document.querySelectorAll('.modal-overlay [data-close]').forEach((b) => b.addEventListener('click', () => {
            b.closest('.modal-overlay').hidden = true;
        }));
        document.querySelectorAll('.opt-tabs .tab').forEach((t) => t.addEventListener('click', () => {
            document.querySelectorAll('.opt-tabs .tab').forEach((x) => x.classList.toggle('active', x === t));
            document.querySelectorAll('#resultsBox .tab-pane').forEach((p) => p.classList.toggle('active', p.dataset.pane === t.dataset.tab));
            const url = new URL(window.location.href); url.hash = t.dataset.tab; window.history.replaceState(null, '', url);
        }));
        $('resCompliant').addEventListener('change', () => { state.page.offset = 0; loadResults(); });
        $('resPrev').addEventListener('click', () => { state.page.offset = Math.max(0, state.page.offset - state.page.limit); loadResults(); });
        $('resNext').addEventListener('click', () => { state.page.offset += state.page.limit; loadResults(); });
        const hash = window.location.hash.replace('#', '');
        if (hash) {
            const t = document.querySelector(`.opt-tabs .tab[data-tab="${hash}"]`);
            if (t) t.click();
        }
    }

    bind();
    refresh();
})();
