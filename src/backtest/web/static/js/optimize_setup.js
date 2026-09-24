/* Optimization setup page (/optimize).
 *
 * Builds the PRD OptimizationConfig from the form, asks the server for a
 * validated estimate on every change (debounced), and starts the run.
 * Server-side validation is the source of truth: field errors come back as
 * {field: message} and are listed next to the Start button.
 */
(() => {
    'use strict';
    const C = globalThis.OptCommon;
    const $ = (id) => document.getElementById(id);
    const root = $('optSetup');
    if (!root) return;

    const state = {
        strategies: [],
        space: null,          // /space payload for the selected strategy
        params: [],           // editable rows
        constraints: [
            { enabled: true, metric: 'max_drawdown', operator: '<', value: 25 },
            { enabled: true, metric: 'min_trades', operator: '>=', value: 10 },
        ],
        lastEstimate: null,
        estimating: 0,
    };

    const CONSTRAINT_LABELS = {
        max_drawdown: 'Max drawdown (%)', min_trades: 'Trades', win_rate: 'Win rate (%)',
        sharpe: 'Sharpe', profit_factor: 'Profit factor', total_return: 'Total return (%)',
    };

    // ------------------------------------------------------------------ init

    function isoDaysAgo(days) {
        const d = new Date(Date.now() - days * 86400000);
        return d.toISOString().slice(0, 10);
    }

    async function init() {
        $('optFrom').value = isoDaysAgo(3 * 365);
        $('optTo').value = isoDaysAgo(1);
        try {
            const meta = await C.api('/api/optimize/meta');
            $('optObjective').innerHTML = meta.objectives
                .map((o) => `<option value="${o.id}">${C.escapeHtml(o.label)}</option>`).join('');
        } catch (e) { C.toast(`Could not load options: ${e.message}`, 'error'); }
        try {
            const res = await fetch('/api/strategies');
            state.strategies = await res.json();
        } catch (e) { state.strategies = []; }
        const sel = $('optStrategy');
        sel.innerHTML = state.strategies
            .map((s) => `<option value="${C.escapeHtml(s.name)}">${C.escapeHtml(s.name)}${s.signal_kind === 'option' ? ' · options' : ''}</option>`)
            .join('');
        const wanted = root.dataset.selectedStrategy;
        if (wanted && state.strategies.some((s) => s.name === wanted)) sel.value = wanted;
        else if (state.strategies.some((s) => s.name === 'sma_crossover')) sel.value = 'sma_crossover';
        bind();
        renderConstraints();
        await loadStrategy(sel.value);
        loadHistory();
    }

    function bind() {
        $('optStrategy').addEventListener('change', (e) => loadStrategy(e.target.value));
        $('optForm').addEventListener('input', onChange);
        $('optForm').addEventListener('change', onChange);
        $('optAddConstraint').addEventListener('click', () => {
            state.constraints.push({ enabled: true, metric: 'sharpe', operator: '>', value: 0.5 });
            renderConstraints(); onChange();
        });
        $('optWfEnabled').addEventListener('change', syncWf);
        document.querySelectorAll('input[name="optMethod"]').forEach((r) => r.addEventListener('change', syncMethod));
        $('optStart').addEventListener('click', () => submit(true));
        $('optDraft').addEventListener('click', () => submit(false));
        $('optPresetSelect').addEventListener('change', applyPreset);
        $('optHistoryFilter').addEventListener('change', loadHistory);
        syncMethod(); syncWf();
    }

    // ------------------------------------------------------------ strategy

    async function loadStrategy(name) {
        if (!name) return;
        const url = new URL(window.location.href);
        url.searchParams.set('strategy', name);
        window.history.replaceState(null, '', url);
        try {
            state.space = await C.api(`/api/optimize/strategies/${encodeURIComponent(name)}/space`);
        } catch (e) {
            C.toast(`Could not load ${name}: ${e.message}`, 'error');
            return;
        }
        state.params = state.space.parameters.map((p) => ({ ...p }));
        $('optStrategyDesc').textContent = state.space.description || '';
        const sym = $('optSymbol');
        if (state.space.is_option && !['NIFTY', 'BANKNIFTY'].includes(sym.value.toUpperCase())) sym.value = 'NIFTY';
        if (!state.space.is_option && ['NIFTY', 'BANKNIFTY'].includes(sym.value.toUpperCase())) sym.value = state.space.default_symbol;
        $('optSelectorRow').hidden = !state.space.is_option;
        const obj = $('optObjective');
        if (state.space.is_option && obj.value === 'sharpe') obj.value = 'total_return';
        renderParams();
        loadPresets(name);
        if ($('optHistoryFilter').checked) loadHistory();
        onChange();
    }

    function renderParams() {
        const body = $('optParamTable').querySelector('tbody');
        $('optParamEmpty').hidden = state.params.length > 0;
        let lastGroup = null;
        body.innerHTML = state.params.map((p, i) => {
            const group = p.engine_param ? 'engine' : 'strategy';
            let head = '';
            if (group !== lastGroup && group === 'engine') {
                head = '<tr class="opt-group-row"><td colspan="7">⚙ Option engine knobs</td></tr>';
            }
            lastGroup = group;
            const bounds = [p.bound_min, p.bound_max].every((b) => b === null || b === undefined)
                ? '' : `allowed ${p.bound_min ?? '−∞'} … ${p.bound_max ?? '∞'}`;
            const numAttrs = `step="any" ${p.bound_min !== null && p.bound_min !== undefined ? `min="${p.bound_min}"` : ''} ${p.bound_max !== null && p.bound_max !== undefined ? `max="${p.bound_max}"` : ''}`;
            return `${head}<tr data-i="${i}" class="${p.optimize ? '' : 'opt-row-off'}">
                <td><input type="checkbox" data-f="optimize" ${p.optimize ? 'checked' : ''} aria-label="optimize ${C.escapeHtml(p.name)}"></td>
                <td><div class="opt-pname" title="${C.escapeHtml(p.tooltip || '')}">${C.escapeHtml(p.label || p.name)}</div>
                    <div class="muted small">${C.escapeHtml(p.name)}${bounds ? ' · ' + bounds : ''}</div></td>
                <td><input class="input opt-num" data-f="current" type="number" ${numAttrs} value="${p.current}"></td>
                <td><input class="input opt-num" data-f="min" type="number" ${numAttrs} value="${p.min}" ${p.optimize ? '' : 'disabled'}></td>
                <td><input class="input opt-num" data-f="max" type="number" ${numAttrs} value="${p.max}" ${p.optimize ? '' : 'disabled'}></td>
                <td><input class="input opt-num" data-f="step" type="number" step="any" min="0" value="${p.step}" ${p.optimize ? '' : 'disabled'}></td>
                <td class="opt-count" data-count></td>
            </tr>`;
        }).join('');
        body.querySelectorAll('tr[data-i]').forEach((tr) => {
            tr.addEventListener('input', (e) => onParamEdit(tr, e));
            tr.addEventListener('change', (e) => onParamEdit(tr, e));
        });
        refreshCounts();
    }

    function onParamEdit(tr, e) {
        const p = state.params[Number(tr.dataset.i)];
        const f = e.target.dataset.f;
        if (!f) return;
        if (f === 'optimize') {
            p.optimize = e.target.checked;
            tr.classList.toggle('opt-row-off', !p.optimize);
            tr.querySelectorAll('input[data-f="min"],input[data-f="max"],input[data-f="step"]')
                .forEach((inp) => { inp.disabled = !p.optimize; });
        } else {
            p[f] = e.target.value === '' ? null : Number(e.target.value);
        }
        refreshCounts();
    }

    function refreshCounts() {
        const rows = $('optParamTable').querySelectorAll('tr[data-i]');
        rows.forEach((tr) => {
            const p = state.params[Number(tr.dataset.i)];
            const n = p.optimize ? C.gridCount(p.min, p.max, p.step) : 1;
            tr.querySelector('[data-count]').textContent = p.optimize ? (n || '!') : '—';
        });
        const k = state.params.filter((p) => p.optimize).length;
        $('optParamCount').textContent = `${k} selected · ${C.totalCombinations(state.params).toLocaleString()} combinations`;
    }

    // -------------------------------------------------------- constraints

    function renderConstraints() {
        const box = $('optConstraints');
        box.innerHTML = state.constraints.map((c, i) => `
            <div class="opt-constraint ${c.enabled ? '' : 'opt-row-off'}" data-i="${i}">
                <input type="checkbox" data-f="enabled" ${c.enabled ? 'checked' : ''} aria-label="enable constraint">
                <select class="input" data-f="metric">${Object.entries(CONSTRAINT_LABELS)
                    .map(([k, v]) => `<option value="${k}" ${k === c.metric ? 'selected' : ''}>${v}</option>`).join('')}</select>
                <select class="input opt-op" data-f="operator">${['<', '<=', '>', '>=']
                    .map((o) => `<option ${o === c.operator ? 'selected' : ''}>${o}</option>`).join('')}</select>
                <input class="input opt-num" type="number" step="any" data-f="value" value="${c.value}">
                <button type="button" class="btn-icon" data-remove title="Remove">✕</button>
            </div>`).join('') || '<div class="muted small">No constraints — every result will be ranked.</div>';
        box.querySelectorAll('.opt-constraint').forEach((row) => {
            const c = state.constraints[Number(row.dataset.i)];
            row.addEventListener('change', (e) => {
                const f = e.target.dataset.f;
                if (!f) return;
                c[f] = f === 'enabled' ? e.target.checked : (f === 'value' ? Number(e.target.value) : e.target.value);
                row.classList.toggle('opt-row-off', !c.enabled);
            });
            row.querySelector('[data-remove]').addEventListener('click', () => {
                state.constraints.splice(Number(row.dataset.i), 1);
                renderConstraints(); onChange();
            });
        });
    }

    // ----------------------------------------------------- method / WF UI

    function method() {
        const r = document.querySelector('input[name="optMethod"]:checked');
        return r ? r.value : 'grid';
    }

    function syncMethod() {
        const m = method();
        document.querySelectorAll('[data-method]').forEach((el) => { el.hidden = el.dataset.method !== m; });
        document.querySelectorAll('.opt-method').forEach((el) => {
            el.classList.toggle('active', el.querySelector('input').value === m);
        });
        onChange();
    }

    function syncWf() {
        const on = $('optWfEnabled').checked;
        $('optWfFields').classList.toggle('opt-disabled', !on);
        $('optWfFields').querySelectorAll('input').forEach((i) => { i.disabled = !on; });
        onChange();
    }

    // ------------------------------------------------------------- config

    function numOrNull(id) {
        const v = $(id).value;
        return v === '' ? null : Number(v);
    }

    function buildConfig() {
        return {
            strategyId: $('optStrategy').value,
            objectiveFunction: $('optObjective').value || 'sharpe',
            method: method(),
            parameters: state.params.map((p) => ({
                name: p.name, type: p.type, optimize: !!p.optimize,
                min: p.min, max: p.max, step: p.step, current: p.current,
            })),
            constraints: state.constraints.filter((c) => c.enabled)
                .map((c) => ({ metric: c.metric, operator: c.operator, value: Number(c.value) })),
            methodSettings: {
                nSamples: numOrNull('optNSamples'),
                nCalls: numOrNull('optNCalls'),
                population: numOrNull('optPopulation'),
                generations: numOrNull('optGenerations'),
            },
            backtestConfig: {
                symbol: $('optSymbol').value.trim().toUpperCase(),
                startDate: $('optFrom').value,
                endDate: $('optTo').value,
                initialCapital: Number($('optCapital').value),
                timeframe: $('optTimeframe').value,
                selectorType: $('optSelector').value,
                source: root.dataset.source || undefined,
            },
            walkForward: {
                enabled: $('optWfEnabled').checked,
                trainPeriodDays: numOrNull('optWfTrain'),
                testPeriodDays: numOrNull('optWfTest'),
                stepDays: numOrNull('optWfStep'),
                maxEvalsPerSplit: numOrNull('optWfBudget'),
            },
        };
    }

    const onChange = C.debounce(estimate, 350);

    async function estimate() {
        if (!state.space) return;
        const ticket = ++state.estimating;
        const cfg = buildConfig();
        $('optStart').disabled = true;
        $('optDraft').disabled = true;
        try {
            const res = await C.api('/api/optimize/estimate', { method: 'POST', body: cfg });
            if (ticket !== state.estimating) return;
            state.lastEstimate = res.estimate;
            renderEstimate(res.estimate, null);
        } catch (e) {
            if (ticket !== state.estimating) return;
            if (e.status === 503) {
                const banner = $('optDbBanner');
                banner.hidden = false;
                banner.textContent = e.message;
            }
            renderEstimate(null, e.errors || { error: e.message });
        }
        renderWfPreview(cfg);
    }

    function renderEstimate(est, errors) {
        $('estGrid').textContent = est ? est.grid_size.toLocaleString() : '—';
        $('estEvals').textContent = est ? est.total_evaluations.toLocaleString() : '—';
        $('estTime').textContent = est ? `~${C.fmtDuration(est.estimated_seconds)}` : '—';
        $('estWorkers').textContent = est ? est.workers : '—';
        $('estWarnings').innerHTML = (est ? est.warnings : [])
            .map((w) => `<li>⚠ ${C.escapeHtml(w)}</li>`).join('');
        $('estErrors').innerHTML = Object.entries(errors || {})
            .map(([k, v]) => `<li><strong>${C.escapeHtml(k)}</strong>: ${C.escapeHtml(v)}</li>`).join('');
        $('optStart').disabled = !est;
        $('optDraft').disabled = !est;
        document.querySelectorAll('#optParamTable tr[data-i]').forEach((tr) => {
            const p = state.params[Number(tr.dataset.i)];
            tr.classList.toggle('opt-row-error', !!(errors && errors[`parameters.${p.name}`]));
        });
    }

    function renderWfPreview(cfg) {
        const wf = cfg.walkForward;
        const el = $('optWfPreview');
        if (!wf.enabled) { el.textContent = 'Off — the results page will not be able to detect overfitting.'; return; }
        const start = new Date(cfg.backtestConfig.startDate);
        const end = new Date(cfg.backtestConfig.endDate);
        const days = Math.round((end - start) / 86400000) + 1;
        if (!(days > 0) || !wf.trainPeriodDays || !wf.testPeriodDays || !wf.stepDays) { el.textContent = ''; return; }
        const splits = Math.max(0, Math.floor((days - wf.trainPeriodDays - wf.testPeriodDays) / wf.stepDays) + 1);
        el.textContent = `${splits} split${splits === 1 ? '' : 's'}: optimize on ${wf.trainPeriodDays}d, test on the next ${wf.testPeriodDays}d, roll forward ${wf.stepDays}d.`;
    }

    async function submit(start) {
        const cfg = buildConfig();
        cfg.start = start;
        $('optStart').disabled = true;
        try {
            const res = await C.api('/api/optimize/runs', { method: 'POST', body: cfg });
            C.toast(start ? 'Optimization started' : 'Draft saved');
            window.location.href = `/optimize/runs/${res.run_id}`;
        } catch (e) {
            renderEstimate(state.lastEstimate, e.errors || { error: e.message });
            C.toast(e.message, 'error');
        }
    }

    // ------------------------------------------------------------ presets

    async function loadPresets(strategy) {
        const sel = $('optPresetSelect');
        sel.innerHTML = '<option value="">Load preset…</option>';
        try {
            const res = await C.api(`/api/optimize/presets?strategy=${encodeURIComponent(strategy)}`);
            const own = res.presets.filter((p) => p.strategy_id === strategy);
            sel.innerHTML += own.map((p) => `<option value="${p.preset_id}">${C.escapeHtml(p.name)} (${p.source})</option>`).join('');
            state.presets = own;
            sel.hidden = own.length === 0;
        } catch (_) { sel.hidden = true; }
    }

    function applyPreset(e) {
        const preset = (state.presets || []).find((p) => p.preset_id === e.target.value);
        if (!preset) return;
        let touched = 0;
        state.params.forEach((p) => {
            if (preset.params[p.name] !== undefined) { p.current = preset.params[p.name]; touched += 1; }
        });
        renderParams(); onChange();
        C.toast(`Loaded ${touched} value(s) from "${preset.name}" as current`, 'info');
        e.target.value = '';
    }

    // ------------------------------------------------------------ history

    async function loadHistory() {
        const box = $('optHistory');
        const only = $('optHistoryFilter').checked;
        const q = only ? `?strategy=${encodeURIComponent($('optStrategy').value)}&limit=15` : '?limit=15';
        try {
            const res = await C.api(`/api/optimize/runs${q}`);
            if (!res.runs.length) { box.innerHTML = '<div class="muted small">No runs yet.</div>'; return; }
            box.innerHTML = res.runs.map((r) => `
                <a class="opt-history-row" href="/optimize/runs/${r.run_id}">
                    <div class="opt-history-main">
                        <strong>${C.escapeHtml(r.strategy_id)}</strong>
                        <span class="muted small">${C.escapeHtml(r.method)} · ${C.escapeHtml(C.OBJECTIVE_LABELS[r.objective_function] || r.objective_function)}</span>
                    </div>
                    <div class="opt-history-meta">
                        ${C.statusBadge(r.status)}
                        <span class="small">${C.isNum(r.best_score) ? 'best ' + C.fmtNum(r.best_score, 3) : ''}</span>
                        ${r.overfitted ? '<span class="opt-status opt-status-bad" title="walk-forward flagged overfitting">overfit</span>' : ''}
                    </div>
                    <div class="muted small">${C.fmtDate(r.created_at)} · ${r.tested_combinations}/${r.total_combinations ?? '?'} tested</div>
                </a>`).join('');
        } catch (e) {
            box.innerHTML = `<div class="muted small">${C.escapeHtml(e.message)}</div>`;
            if (e.status === 503) {
                const banner = $('optDbBanner');
                banner.hidden = false;
                banner.textContent = e.message;
            }
        }
    }

    init();
})();
