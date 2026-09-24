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
        drawLineChart('wfEquityChart', [{ label: 'Out-of-sample (stitched test windows)', points: wf.oos_curve || [], color: '#22c55e' }]);
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
                        pointBackgroundColor: s.values.map((v) => (String(v) === String(s.best_value) ? '#f59e0b' : (inPlateau(v) ? '#22c55e' : '#64748b'))),
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
                return `<tr><td class="small">${C.fmtDate(a.timestamp)}</td><td>${C.escapeHtml(a.action)}${d.rolled_back ? ' <span class="muted small">(rolled back)</span>' : ''}</td>
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

    async function openApply(params) {
        state.applyParams = params || state.run.best_params;
        $('applyParams').innerHTML = paramsPreview(state.applyParams);
        $('applyError').innerHTML = '';
        $('applyConfirmLive').checked = false;
        $('applyAllowUnvalidated').checked = false;
        document.querySelector('input[name="applyTarget"][value="none"]').checked = true;
        $('applyModal').hidden = false;
        try {
            const res = await C.api(`/api/optimize/runners?strategy=${encodeURIComponent(state.run.strategy_id)}`);
            state.runners = res.runners || [];
        } catch (_) { state.runners = []; }
        syncApplyTarget();
    }

    function applyTarget() {
        return (document.querySelector('input[name="applyTarget"]:checked') || {}).value || 'none';
    }

    function syncApplyTarget() {
        const t = applyTarget();
        const row = $('applyRunnerRow');
        const sel = $('applyRunner');
        const wantMode = t === 'live' ? 'live' : 'paper';
        const runners = state.runners.filter((r) => r.mode === wantMode);
        row.hidden = t === 'none';
        let opts = runners.map((r) => `<option value="${C.escapeHtml(r.instance_id)}">${C.escapeHtml(r.name)} · ${C.escapeHtml(r.status)} · ${C.escapeHtml((r.symbols || []).join(','))}</option>`).join('');
        if (t === 'paper' || t === 'ab_test') opts = `<option value="">${t === 'ab_test' ? '— no control runner —' : 'Spawn a NEW paper runner'}</option>${opts}`;
        sel.innerHTML = opts || '<option value="">No matching live runner</option>';
        $('applyRunnerHint').textContent = {
            paper: 'Selecting a runner flattens it and restarts it with the new parameters.',
            ab_test: 'A new paper runner (B) is spawned; the selected runner (A) is left untouched as the control.',
            live: 'The live runner is flattened and restarted with the new parameters.',
        }[t] || '';
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

    async function confirmApply() {
        const t = applyTarget();
        const body = {
            target: t, params: state.applyParams, instance_id: $('applyRunner').value || null,
            confirm_live: $('applyConfirmLive').checked, allow_unvalidated: $('applyAllowUnvalidated').checked,
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
        $('btnPreset').addEventListener('click', () => openPreset());
        $('applyConfirm').addEventListener('click', confirmApply);
        $('presetConfirm').addEventListener('click', confirmPreset);
        document.querySelectorAll('input[name="applyTarget"]').forEach((r) => r.addEventListener('change', syncApplyTarget));
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
