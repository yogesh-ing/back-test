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
        attestation: null,     // §2 data confirmation for the current selection
        syntheticAck: false,   // the operator's explicit tick, per selection
        estimating: 0,
    };

    let symbolPicker = null;   // components/symbol_picker.js handle
    // §6: the engine the search will run on. The Optimize form has no engine
    // control, so the value has to travel with the request; carrying it from
    // the backtest is the only way a quick-screen result cannot silently be
    // tuned on the canonical engine (§1.1 was entirely about that mismatch).
    let engine = 'driver';
    let sourceBacktestId = null;   // §6 reverse-flow audit chain, first link
    let prefillBaseline = null;    // §1: origin's engine/data flags + baseline metrics
    let prefill = null;            // the §6 hand-off, consumed once

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
        // Same gate as Backtest: a disabled source must not offer to start a
        // search. optStart manages its own disabled state, so the gate's
        // save/restore has to run before anything else touches it.
        if (window.DataSourceGate) {
            DataSourceGate.mount('dataSourceGate', {
                status: $('dataSourceGate').dataset.status,
                blockIds: ['optStart'],
            });
        }

        $('optFrom').value = isoDaysAgo(3 * 365);
        $('optTo').value = isoDaysAgo(1);

        // §1.3/§1.4 — the same picker and timeframe rules as Backtest/Compare:
        // a symbol with no cached bars is listed but not selectable, and the
        // timeframe list is only what the chosen symbol really has.
        symbolPicker = SymbolPicker.mount({
            select: 'optSymbol', search: 'optSymbolSearch', tabs: 'optSymbolTabs',
            summary: 'optSymbolStatus',
            onChange: () => Timeframes.applyTo($('optTimeframe'), symbolPicker.timeframesFor($('optSymbol').value)),
        });
        Timeframes.applyTo($('optTimeframe'), null);
        try {
            const meta = await C.api('/api/optimize/meta');
            $('optObjective').innerHTML = meta.objectives
                .map((o) => `<option value="${o.id}">${C.escapeHtml(o.label)}</option>`).join('');
        } catch (e) { C.toast(`Could not load options: ${e.message}`, 'error'); }
        try {
            // venue=backtest: optimization drives the same DB-bar backtest
            // engine, so option strategies are not offered here either.
            const res = await fetch('/api/strategies?venue=backtest');
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
        // §6: applied AFTER loadStrategy, because loadStrategy fetches the
        // parameter space and renders the rows the prefill has to write into.
        prefill = typeof SessionState !== 'undefined' ? SessionState.takeOptimizePrefill() : null;
        if (prefill && prefill.strategyId && state.strategies.some((x) => x.name === prefill.strategyId)) {
            sel.value = prefill.strategyId;
        }
        await loadStrategy(sel.value);
        if (prefill) applyPrefill(prefill);
        loadHistory();
    }

    // ------------------------------------------------------------ §6 prefill

    /** Write the §6 hand-off into the form, leaving Start to the user. */
    function applyPrefill(pf) {
        if (symbolPicker) symbolPicker.setValue(pf.symbol);
        $('optFrom').value = pf.startDate;
        $('optTo').value = pf.endDate;
        $('optCapital').value = pf.initialCapital;
        if (pf.timeframe) {
            Timeframes.applyTo($('optTimeframe'), symbolPicker ? symbolPicker.timeframesFor(pf.symbol) : null);
            const want = Timeframes.toCanonical(pf.timeframe);
            const sel = $('optTimeframe');
            if (want && [...sel.options].some((o) => o.value === want)) sel.value = want;
        }
        if (pf.objectiveFunction) $('optObjective').value = pf.objectiveFunction;
        if (pf.method) {
            const radio = document.querySelector(`input[name="optMethod"][value="${pf.method}"]`);
            if (radio) radio.checked = true;
            syncMethod();
        }
        const wf = pf.walkForward || {};
        $('optWfEnabled').checked = !!wf.enabled;
        if (wf.enabled) {
            $('optWfTrain').value = wf.trainPeriodDays;
            $('optWfTest').value = wf.testPeriodDays;
            $('optWfStep').value = wf.stepDays;
        }
        syncWf();
        // §6 "parameters pre-filled with the current values" — the baseline
        // every optimised value is then shown as a delta against.
        applyParamOverrides(pf.params || {});
        engine = pf.engine || 'driver';
        sourceBacktestId = pf.resultId || null;
        prefillBaseline = {
            baselineFillExact: !!pf.baselineFillExact,
            baselineRealData: !!pf.baselineRealData,
            baselineMetrics: pf.baselineMetrics || null,
        };
        renderPrefillNotice(pf);
        onChange();
    }

    /** Copy a backtest's parameter values into the rendered `current` column. */
    function applyParamOverrides(params) {
        const names = Object.keys(params || {});
        if (!names.length) return;
        state.params.forEach((p) => {
            if (names.includes(p.name) && params[p.name] !== null && params[p.name] !== undefined) {
                p.current = params[p.name];
            }
        });
        renderParams();
    }

    function renderPrefillNotice(pf) {
        const el = $('optPrefillNotice');
        if (!el) return;
        const same = !pf.source || pf.source === root.dataset.source;
        // PRD Part 2 §1: the banner quotes the originating backtest's baseline
        // performance, so the user can judge every optimizer result against
        // the number they started from.
        const bm = pf.baselineMetrics || {};
        const C = globalThis.OptCommon;
        const baselineLine = C && C.isNum(bm.sharpe)
            ? `<br><span class="small">Baseline performance: <strong>Sharpe ${C.fmtNum(bm.sharpe, 2)}</strong>`
              + (C.isNum(bm.total_return_pct) ? ` · Return <strong>${C.fmtPct(bm.total_return_pct)}</strong>` : '')
              + (C.isNum(bm.total_trades) ? ` · Trades <strong>${bm.total_trades}</strong>` : '')
              + `</span>`
            : '';
        // §1 skip-baseline: redundant only when the origin already ran the
        // canonical fill-exact engine on real data over the same range. The
        // run is told via baseline_imported; the search itself does not skip
        // anything until the server agrees the origin qualifies.
        const skipBaseline = pf.baselineFillExact && pf.baselineRealData && same;
        const skipLine = skipBaseline
            ? `<br><span class="pos small">✓ Origin ran the fill-exact engine on real data — the optimizer imports this result as the baseline instead of re-running it.</span>`
            : '';
        el.hidden = false;
        el.className = 'opt-prefill-notice' + (same ? '' : ' opt-prefill-warn');
        el.innerHTML = `📎 Pre-filled from backtest result <code>${C.escapeHtml(pf.resultId || '—')}</code>`
            + ` · engine <strong>${C.escapeHtml(pf.engineLabel || pf.engine)}</strong>`
            + ` · data source <strong>${C.escapeHtml(pf.source || root.dataset.source || 'default')}</strong>`
            + baselineLine
            + (same ? ''
                : ` — <span class="neg">this is a different source from the one this page uses, `
                  + `so the search will not be comparable.</span>`)
            + skipLine
            + `<br><span class="muted small">Nothing has been run. Review the form, then press Start. `
            + `Walk-forward is ${$('optWfEnabled').checked ? 'on' : 'off'}.</span>`;
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
        // Option strategies need an index; equity strategies need a share. The
        // suggestion is only applied when the picker actually offers it, so a
        // missing symbol never leaves the form pointing at nothing.
        const sym = $('optSymbol');
        const setSymbol = (value) => { if (symbolPicker) symbolPicker.setValue(value); else sym.value = value; };
        if (state.space.is_option && !['NIFTY', 'BANKNIFTY'].includes(sym.value.toUpperCase())) setSymbol('NIFTY');
        if (!state.space.is_option && ['NIFTY', 'BANKNIFTY'].includes(sym.value.toUpperCase())) setSymbol(state.space.default_symbol);
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
                <td><input class="input opt-num" data-f="current" aria-label="${C.escapeHtml(p.label || p.name)} — current value" type="number" ${numAttrs} value="${p.current}"></td>
                <td><input class="input opt-num" data-f="min" aria-label="${C.escapeHtml(p.label || p.name)} — search minimum" type="number" ${numAttrs} value="${p.min}" ${p.optimize ? '' : 'disabled'}></td>
                <td><input class="input opt-num" data-f="max" aria-label="${C.escapeHtml(p.label || p.name)} — search maximum" type="number" ${numAttrs} value="${p.max}" ${p.optimize ? '' : 'disabled'}></td>
                <td><input class="input opt-num" data-f="step" aria-label="${C.escapeHtml(p.label || p.name)} — search step size" type="number" step="any" min="0" value="${p.step}" ${p.optimize ? '' : 'disabled'}></td>
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
                <select class="input" data-f="metric" aria-label="Constraint ${i + 1} metric">${Object.entries(CONSTRAINT_LABELS)
                    .map(([k, v]) => `<option value="${k}" ${k === c.metric ? 'selected' : ''}>${v}</option>`).join('')}</select>
                <select class="input opt-op" data-f="operator" aria-label="Constraint ${i + 1} comparison">${['<', '<=', '>', '>=']
                    .map((o) => `<option ${o === c.operator ? 'selected' : ''}>${o}</option>`).join('')}</select>
                <input class="input opt-num" type="number" step="any" data-f="value" aria-label="Constraint ${i + 1} threshold" value="${c.value}">
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
                // §6: the engine the backtest used. Emitted so a quick-screen
                // result cannot be tuned on the canonical driver without
                // saying so.
                engine,
                selectorType: $('optSelector').value,
                source: root.dataset.source || undefined,
                // §6 reverse flow: link 1 of backtest -> optimize -> runner.
                sourceBacktestId: sourceBacktestId || undefined,
                // §1: recorded on the run so the audit row can name the
                // originating backtest, and so the optimizer can skip its own
                // baseline pass when the origin already ran fill-exact on
                // real data (baselineImported flags that at run time).
                baselineImported: !!(prefillBaseline && prefillBaseline.baselineFillExact
                    && prefillBaseline.baselineRealData),
                baselineMetrics: prefillBaseline ? prefillBaseline.baselineMetrics || null : null,
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

    // ------------------------------------------------------- §2 attestation

    /**
     * PRD Part 2 §2 — the data confirmation box.
     *
     * Two things are deliberate. First, this is a PREVIEW: bar count and the
     * real coverage are not knowable until the candles are fetched, so they
     * read "—" rather than a number the box would have had to invent. The run
     * page shows what was actually loaded. Second, staleness warns and allows.
     * Re-fetching is the operator's decision, and a box that refuses work for
     * reasons they cannot act on teaches them to ignore it.
     *
     * Synthetic is the exception the PRD calls for, and the one hard gate in
     * the Optimize flow. It is enforced again on the server — a gate only the
     * browser enforces is a suggestion.
     */
    function renderAttestation(res) {
        const el = $('optAttestation');
        if (!el) return;
        const a = res.attestation || {};
        const real = !!a.data_source_real;
        const day = (v) => (v ? C.escapeHtml(v) : '<span class="muted">—</span>');

        el.hidden = false;
        el.className = `opt-attestation ${real ? '' : 'opt-attestation--synthetic'}`;
        el.innerHTML = `
            <div class="opt-attestation-head">
                <span class="opt-attestation-title">DATA CONFIRMATION</span>
                <span class="opt-attestation-source ${real ? 'pos' : 'neg'}">
                    ${C.escapeHtml(a.data_source_label || 'Unknown source')}
                </span>
            </div>
            <dl class="opt-attestation-grid">
                <dt>Source</dt><dd>${C.escapeHtml(a.data_source_label || '—')}</dd>
                <dt>Symbol</dt><dd>${C.escapeHtml(a.symbol || '—')}</dd>
                <dt>Timeframe</dt><dd>${C.escapeHtml(a.timeframe || '—')}</dd>
                <dt>Bars</dt><dd>${a.bars_count === null || a.bars_count === undefined
                    ? '<span class="muted">— (shown after the run)</span>'
                    : a.bars_count.toLocaleString()}</dd>
                <dt>Range</dt><dd>${day(a.date_from)} → ${day(a.date_to)}</dd>
                <dt>Fetched</dt><dd>${a.data_fetch_date ? day(a.data_fetch_date)
                    : (real ? '<span class="muted">unknown</span>' : 'n/a — generated')}</dd>
            </dl>
            ${(res.warnings || []).map((w) => `<p class="opt-attestation-note opt-attestation-note--${w.level === 'error' ? 'error' : 'warn'}">
                ${w.level === 'error' ? '⛔' : '⚠️'} ${C.escapeHtml(w.message)}</p>`).join('')}
            ${a.requires_acknowledgement ? `
                <label class="opt-attestation-ack">
                    <input type="checkbox" id="optSyntheticAck">
                    <span>${C.escapeHtml(res.acknowledgement || '')}</span>
                </label>
                <p class="opt-attestation-note opt-attestation-note--warn">
                    Tick to start. This run's results will be labelled synthetic
                    everywhere they appear, including the audit trail.</p>` : ''}`;

        // The tick is the only thing standing between the operator and a run
        // whose numbers look like every other run's and are not.
        const ack = $('optSyntheticAck');
        if (ack) {
            ack.checked = state.syntheticAck;
            ack.addEventListener('change', () => {
                state.syntheticAck = ack.checked;
                renderEstimate(state.lastEstimate, null);
            });
        }
    }

    async function refreshAttestation(cfg) {
        try {
            const res = await C.api('/api/optimize/attestation', { method: 'POST', body: cfg });
            renderAttestation(res);
            state.attestation = res;
        } catch (e) {
            // A box that cannot load must not look like a box that says the
            // data is fine. Say so instead of showing the last good answer.
            const el = $('optAttestation');
            el.hidden = false;
            el.className = 'opt-attestation opt-attestation--synthetic';
            el.innerHTML = `<p class="opt-attestation-note opt-attestation-note--error">
                ⛔ Could not confirm the data source: ${C.escapeHtml(e.message)}</p>`;
            state.attestation = null;
        }
        renderEstimate(state.lastEstimate, null);
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
            refreshAttestation(cfg);
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

    /** May this run start? Real data always; synthetic only once ticked. */
    function attestationCleared() {
        const res = state.attestation;
        if (!res) return false;   // unknown: the box failed to load
        return res.satisfied || state.syntheticAck;
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
        $('optStart').disabled = !est || !attestationCleared();
        $('optDraft').disabled = !est || !attestationCleared();
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
        // The server re-checks this. Sending it is not for the server's sake —
        // it is so the stored run records what the operator actually agreed to.
        if (state.syntheticAck) {
            cfg.dataAttestation = {
                acknowledged: true,
                acknowledged_at: new Date().toISOString(),
            };
        }
        $('optStart').disabled = true;
        try {
            const res = await C.api('/api/optimize/runs', { method: 'POST', body: cfg });
            C.toast(start ? 'Optimization started' : 'Draft saved');
            window.location.href = `/optimize/runs/${res.run_id}`;
        } catch (e) {
            renderEstimate(state.lastEstimate, e.errors || { error: e.message });
            C.toast(e.message, 'error');
            // Matched on the machine-readable code, never on the wording: a
            // refusal whose meaning lives in a sentence breaks on the next
            // reword, and this one is the only thing pointing at the tick.
            if (e.code === 'synthetic_data_not_acknowledged') {
                const ack = $('optSyntheticAck');
                if (ack) { ack.checked = false; state.syntheticAck = false; ack.scrollIntoView({ block: 'center' }); }
            }
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
