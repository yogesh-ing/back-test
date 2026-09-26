/**
 * Portfolio Intelligence page (/monitor, /portfolio/greeks).
 *
 * Two layers:
 *   MonitorView — PURE render functions: data in, HTML string out. No DOM, no
 *                 fetch — pinned by tests/js/test_monitor_page.mjs.
 *   controller  — polls GET /api/monitor/snapshot, writes the HTML into the
 *                 page, draws the two charts, acknowledges alerts.
 *
 * Every user-controlled string (runner names, symbols) goes through esc():
 * runner names are free text typed into the spawn form.
 */
(function () {
    "use strict";

    // ------------------------------------------------------------ helpers
    function esc(value) {
        return String(value == null ? "" : value)
            .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
    }
    const isNum = (v) => typeof v === "number" && Number.isFinite(v);
    function money(v, dp = 0) {
        if (!isNum(v)) return "—";
        if (typeof Money !== "undefined") return Money.format(v, dp);
        const s = Math.abs(v).toLocaleString("en-IN", { maximumFractionDigits: dp });
        return `${v < 0 ? "-" : ""}₹${s}`;
    }
    function signedMoney(v, dp = 0) {
        if (!isNum(v)) return "—";
        if (typeof Money !== "undefined") return Money.signed(v, dp);
        return `${v < 0 ? "-" : "+"}₹${Math.abs(v).toLocaleString("en-IN", { maximumFractionDigits: dp })}`;
    }
    function pct(v, dp = 1) { return isNum(v) ? `${(v * 100).toFixed(dp)}%` : "—"; }
    function num(v, dp = 2) { return isNum(v) ? v.toLocaleString("en-IN", { maximumFractionDigits: dp }) : "—"; }
    const pnlCls = (v) => (!isNum(v) || v === 0 ? "" : v > 0 ? "pos" : "neg");
    const SEV_ICON = { critical: "🔴", warning: "⚠️", info: "ℹ️" };
    const SEV_RANK = { critical: 2, warning: 1, info: 0 };

    function table(head, rows, empty) {
        if (!rows.length) return `<div class="muted mon-empty">${esc(empty || "Nothing to show.")}</div>`;
        return `<table class="matrix-table"><thead><tr>${head.map(
            (h) => `<th class="${h.num ? "num" : ""}">${h.label}</th>`).join("")}</tr></thead>` +
            `<tbody>${rows.join("")}</tbody></table>`;
    }

    // ------------------------------------------------------------ summary
    function renderSummary(snap) {
        const s = (snap && snap.summary) || {};
        const c = (snap && snap.alert_counts) || {};
        const pill = (label, value, cls) =>
            `<div class="mon-pill ${cls || ""}"><span class="mon-pill-label">${label}</span>` +
            `<span class="mon-pill-value">${value}</span></div>`;
        return [
            pill("Scope", esc((snap && snap.mode) || "all")),
            pill("Strategies", num(s.strategies, 0)),
            pill("Open positions", `${num(s.positions, 0)} <span class="muted">(${num(s.option_legs, 0)} option legs)</span>`),
            pill("Equity", money(s.equity)),
            pill("Session P&amp;L", signedMoney(s.daily_pnl), pnlCls(s.daily_pnl)),
            pill("Alerts", `${c.critical || 0} 🔴 · ${c.warning || 0} ⚠️`, c.critical ? "mon-pill-crit" : c.warning ? "mon-pill-warn" : ""),
        ].join("");
    }

    // ------------------------------------------------------------ alerts
    function renderAlerts(alerts, opts) {
        const showInfo = !opts || opts.showInfo !== false;
        const list = (alerts || []).filter((a) => showInfo || a.severity !== "info");
        if (!list.length) {
            return `<div class="mon-all-clear">✅ No active portfolio-level alerts.</div>`;
        }
        list.sort((a, b) => (SEV_RANK[b.severity] || 0) - (SEV_RANK[a.severity] || 0));
        return list.map((a) => {
            const clearing = a.misses > 0 ? `<span class="mon-tag">clearing</span>` : "";
            const acked = a.acknowledged ? `<span class="mon-tag mon-tag-ack">acknowledged</span>` : "";
            const ackBtn = !a.acknowledged && a.severity !== "info"
                ? `<button class="btn btn-small mon-ack" type="button" data-alert-id="${esc(a.id)}">Acknowledge</button>`
                : "";
            return `<div class="mon-alert mon-alert-${esc(a.severity)}${a.acknowledged ? " mon-alert-acked" : ""}" data-alert-key="${esc(a.key)}">
                <div class="mon-alert-head">
                    <span class="mon-alert-icon">${SEV_ICON[a.severity] || "•"}</span>
                    <span class="mon-alert-sev">${esc(String(a.severity || "").toUpperCase())}</span>
                    <span class="mon-alert-cat">${esc(a.category)}</span>
                    <span class="mon-alert-title">${esc(a.title)}</span>
                    ${clearing}${acked}
                    <span class="mon-alert-age muted">×${num(a.occurrences, 0)}</span>
                    ${ackBtn}
                </div>
                <div class="mon-alert-msg">${esc(a.message)}</div>
                ${a.recommendation ? `<div class="mon-alert-rec">→ ${esc(a.recommendation)}</div>` : ""}
            </div>`;
        }).join("");
    }

    function renderCounts(c) {
        c = c || {};
        return `${c.critical || 0} critical · ${c.warning || 0} warning · ${c.info || 0} info` +
            (c.unacknowledged ? ` · <strong>${c.unacknowledged} unacknowledged</strong>` : "");
    }

    // ------------------------------------------------------------ greeks
    function greekCard(label, value, sub, sev, hint) {
        return `<div class="mon-card ${sev ? `mon-card-${sev}` : ""}">
            <div class="mon-card-label">${label}</div>
            <div class="mon-card-value">${value}</div>
            <div class="mon-card-sub">${sub || ""}</div>
            ${hint ? `<div class="mon-card-hint">${hint}</div>` : ""}
        </div>`;
    }

    /** Worst severity among the alerts matching any of `keys` ("" = none). */
    function severityFor(alerts, ...keys) {
        let best = "";
        (alerts || []).forEach((a) => {
            if (keys.includes(a.key) && (SEV_RANK[a.severity] || 0) >= (SEV_RANK[best] ?? -1)) best = a.severity;
        });
        return best;
    }

    function renderGreekCards(g) {
        if (!g) return "";
        const t = g.totals || {};
        const r = g.ratios || {};
        const al = g.alerts || [];
        const d = t.delta_1pct;
        const bias = !isNum(d) || Math.abs(d) < 1 ? "flat" : d > 0 ? "LONG bias" : "SHORT bias";
        const gam = t.gamma_2pct_pnl;
        const gSide = !isNum(gam) || gam === 0 ? "flat" : gam < 0 ? "short convexity" : "long convexity";
        const vega = t.vega;
        const theta = t.theta_day;
        return [
            greekCard("Delta · per 1% move", signedMoney(d), `${bias} · ${pct(r.delta_1pct, 2)} of equity`,
                severityFor(al, "greeks:delta:portfolio"), "P&L if every underlying rises 1%"),
            greekCard("Gamma · ±2% convexity", signedMoney(gam), `${gSide} · ${pct(r.gamma_2pct, 2)} of equity`,
                severityFor(al, "greeks:gamma:portfolio"), "extra P&L on a 2% move, on top of delta"),
            greekCard("Vega · per vol pt", signedMoney(vega),
                !isNum(vega) || vega === 0 ? "flat" : vega > 0 ? "long vol" : "short vol",
                severityFor(al, "greeks:vega:portfolio"), "P&L if IV rises 1 point"),
            greekCard("Theta · per day", signedMoney(theta),
                !isNum(theta) || theta === 0 ? "flat" : theta > 0 ? "collecting decay" : "paying decay",
                severityFor(al, "greeks:theta:portfolio", "greeks:theta_income:portfolio"),
                "P&L from one calendar day passing"),
            greekCard("Net premium", signedMoney(t.net_premium),
                !isNum(t.net_premium) || t.net_premium === 0 ? "—" : t.net_premium > 0 ? "credit (received)" : "debit (paid)"),
            greekCard("Margin used", money(t.margin_used),
                `${money(t.margin_capital)} capital · ${pct(t.margin_pct)}`,
                isNum(t.margin_pct) && t.margin_pct > 0.8 ? "critical" : isNum(t.margin_pct) && t.margin_pct > 0.6 ? "warning" : ""),
        ].join("");
    }

    function renderScenarios(sc) {
        if (!sc) return "";
        const row = (label, r, extra) => `<tr>
            <td>${esc(label)}</td>
            <td class="num ${pnlCls(r.pnl)}">${signedMoney(r.pnl)}</td>
            <td class="num ${pnlCls(r.pnl_pct)}">${pct(r.pnl_pct, 2)}</td>
            <td class="num muted">${extra == null ? "" : signedMoney(extra)}</td>
        </tr>`;
        const worst = sc.worst;
        const rows = [];
        rows.push(`<tr class="mon-sub"><td colspan="4">Underlying move</td></tr>`);
        (sc.spot || []).forEach((r) => rows.push(row(r.label, r, r.delta_gamma_pnl)));
        rows.push(`<tr class="mon-sub"><td colspan="4">Implied vol (underlying fixed)</td></tr>`);
        (sc.iv || []).forEach((r) => rows.push(row(r.label, r)));
        rows.push(row("Time decay · 1 day", sc.time_decay_1d || {}));
        if ((sc.stress || []).length) {
            rows.push(`<tr class="mon-sub"><td colspan="4">Combined stress</td></tr>`);
            sc.stress.forEach((r) => rows.push(row(r.label, r)));
        }
        const head = `<table class="matrix-table mon-scen"><thead><tr><th>Scenario</th>
            <th class="num">P&amp;L (full reval)</th><th class="num">% equity</th>
            <th class="num" title="Taylor estimate: Δ·dS + ½Γ·dS²">Δ-Γ estimate</th></tr></thead>`;
        const worstLine = worst && isNum(worst.pnl) && worst.pnl < 0
            ? `<div class="mon-worst">Worst modelled: <strong>${esc(worst.label)}</strong> → <span class="neg">${signedMoney(worst.pnl)}</span> (${pct(worst.pnl_pct, 2)})</div>`
            : "";
        return worstLine + head + `<tbody>${rows.join("")}</tbody></table>`;
    }

    function renderByStrategy(g) {
        const rows = ((g && g.by_strategy) || []).map((s) => {
            const units = Object.entries(s.delta_units || {})
                .map(([u, v]) => `${esc(u)} ${num(v, 0)}`).join(" · ");
            return `<tr>
                <td><strong>${esc(s.strategy_name)}</strong><div class="muted mon-small">${esc(s.strategy_kind)} · ${esc(s.mode)}</div></td>
                <td class="num ${pnlCls(s.delta_1pct)}">${signedMoney(s.delta_1pct)}</td>
                <td class="num muted">${units || "—"}</td>
                <td class="num ${pnlCls(s.gamma_pnl_1pct)}">${signedMoney(isNum(s.gamma_pnl_1pct) ? s.gamma_pnl_1pct * 4 : null)}</td>
                <td class="num ${pnlCls(s.vega)}">${signedMoney(s.vega)}</td>
                <td class="num ${pnlCls(s.theta_day)}">${signedMoney(s.theta_day)}</td>
                <td class="num">${signedMoney(s.net_premium)}</td>
                <td class="num">${num(s.positions, 0)} <span class="muted">(${num(s.long_legs, 0)}L/${num(s.short_legs, 0)}S)</span></td>
            </tr>`;
        });
        const t = (g && g.totals) || {};
        if (rows.length) {
            rows.push(`<tr class="mon-total">
                <td>TOTAL</td>
                <td class="num ${pnlCls(t.delta_1pct)}">${signedMoney(t.delta_1pct)}</td>
                <td class="num muted">per underlying ↓</td>
                <td class="num ${pnlCls(t.gamma_2pct_pnl)}">${signedMoney(t.gamma_2pct_pnl)}</td>
                <td class="num ${pnlCls(t.vega)}">${signedMoney(t.vega)}</td>
                <td class="num ${pnlCls(t.theta_day)}">${signedMoney(t.theta_day)}</td>
                <td class="num">${signedMoney(t.net_premium)}</td>
                <td class="num">${num(g.position_count, 0)}</td>
            </tr>`);
        }
        return table([
            { label: "Strategy" }, { label: "Δ ₹/1%", num: true }, { label: "Δ units", num: true },
            { label: "Γ ₹ (±2%)", num: true }, { label: "Vega ₹/pt", num: true },
            { label: "Θ ₹/day", num: true }, { label: "Net premium", num: true },
            { label: "Positions", num: true },
        ], rows, "No open positions in scope.");
    }

    function renderByUnderlying(g) {
        const rows = ((g && g.by_underlying) || []).map((u) => `<tr>
            <td><strong>${esc(u.underlying)}</strong></td>
            <td class="num">${num(u.spot, 2)}</td>
            <td class="num ${pnlCls(u.delta_units)}">${num(u.delta_units, 1)}</td>
            <td class="num ${pnlCls(u.delta_1pct)}">${signedMoney(u.delta_1pct)}</td>
            <td class="num ${pnlCls(u.gamma_1pct)}">${num(u.gamma_1pct, 1)}</td>
            <td class="num ${pnlCls(u.vega)}">${signedMoney(u.vega)}</td>
            <td class="num">${num(u.positions, 0)}</td>
        </tr>`);
        return table([
            { label: "Underlying" }, { label: "Spot", num: true }, { label: "Net Δ units", num: true },
            { label: "Δ ₹/1%", num: true }, { label: "Γ Δ-chg/1%", num: true },
            { label: "Vega ₹/pt", num: true }, { label: "Pos.", num: true },
        ], rows, "No exposure.");
    }

    function renderLegs(g) {
        const rows = ((g && g.legs) || []).map((l) => {
            const opt = l.instrument_type === "option";
            const ivCls = l.iv_source === "default" ? "mon-iv-default" : "";
            return `<tr>
                <td>${esc(l.strategy_name)}</td>
                <td>${esc(l.symbol)}${opt && l.structure_type ? `<div class="muted mon-small">${esc(l.structure_type)}</div>` : ""}</td>
                <td class="${l.side === "SHORT" ? "neg" : "pos"}">${esc(l.side)}</td>
                <td class="num">${num(l.lots, 2)}</td>
                <td class="num">${opt ? num(l.dte_days, 1) : "—"}</td>
                <td class="num ${ivCls}" title="${esc(l.iv_source || "")}">${opt && isNum(l.iv) ? pct(l.iv, 1) : "—"}</td>
                <td class="num">${num(l.delta, 3)}</td>
                <td class="num ${pnlCls(l.delta_1pct)}">${signedMoney(l.delta_1pct)}</td>
                <td class="num ${pnlCls(l.position_theta_day)}">${opt ? signedMoney(l.position_theta_day) : "—"}</td>
            </tr>`;
        });
        return table([
            { label: "Strategy" }, { label: "Instrument" }, { label: "Side" }, { label: "Lots", num: true },
            { label: "DTE", num: true }, { label: "IV", num: true }, { label: "Δ/unit", num: true },
            { label: "Δ ₹/1%", num: true }, { label: "Θ ₹/day", num: true },
        ], rows, "No open legs.");
    }

    function renderIvSources(g) {
        const src = (g && g.iv_sources) || {};
        const parts = Object.entries(src).map(([k, v]) => `${v} ${esc(k)}`);
        return parts.length ? `IV source: ${parts.join(" · ")}` : "";
    }

    // ------------------------------------------------------------ concentration
    function bar(label, pctValue, sub, sev) {
        const w = Math.max(0, Math.min(100, (pctValue || 0) * 100));
        return `<div class="mon-bar-row">
            <div class="mon-bar-label">${label}</div>
            <div class="mon-bar"><div class="mon-bar-fill ${sev ? `mon-bar-${sev}` : ""}" style="width:${w.toFixed(1)}%"></div></div>
            <div class="mon-bar-pct">${pct(pctValue, 0)}</div>
            <div class="mon-bar-sub muted">${sub || ""}</div>
        </div>`;
    }

    function renderConcentration(c) {
        if (!c) return { stats: "", underlying: "", groups: "", strikes: "" };
        const al = c.alerts || [];
        const stats = [
            greekCard("Gross notional", money(c.total_notional), `${num(c.gross_leverage, 2)}× equity`),
            greekCard("Effective underlyings", num(c.effective_underlyings, 2),
                `Herfindahl ${num(c.herfindahl, 3)} · ${num((c.by_underlying || []).length, 0)} held`),
            greekCard("Exposures", num(c.exposure_count, 0), `across ${num(c.strategy_count, 0)} strategies`),
        ].join("");
        const underlying = (c.by_underlying || []).map((u) => {
            const sev = severityFor(al, `concentration:underlying:${u.underlying}`);
            const strat = u.strategy_count > 1 ? ` · ${u.strategy_count} strategies` : "";
            return bar(`<strong>${esc(u.underlying)}</strong>`, u.pct,
                `${money(u.notional)} · Δ-adj ${signedMoney(u.delta_notional)}${strat}`, sev);
        }).join("") || `<div class="muted mon-empty">No exposure.</div>`;
        const groups = (c.by_group || []).map((g) => {
            const sev = severityFor(al, `concentration:group:${g.group}`);
            return bar(`<strong>${esc(g.group)}</strong>`, g.pct, (g.underlyings || []).map(esc).join(", "), sev);
        }).join("") || `<div class="muted mon-empty">No exposure.</div>`;
        const strikes = table([
            { label: "Underlying" }, { label: "Strike", num: true }, { label: "Expiry" },
            { label: "Positions", num: true }, { label: "Lots", num: true },
            { label: "Types" }, { label: "Strategies" },
        ], (c.strike_clusters || []).map((k) => `<tr class="${severityFor(al, `concentration:strike:${k.key}`) ? "mon-row-warn" : ""}">
            <td>${esc(k.underlying)}</td><td class="num">${num(k.strike, 0)}</td><td>${esc(k.expiry)}</td>
            <td class="num">${num(k.positions, 0)}</td><td class="num">${num(k.lots, 1)}</td>
            <td>${(k.option_types || []).map(esc).join("/")}</td>
            <td>${(k.strategies || []).map(esc).join(", ")}</td>
        </tr>`), "No option legs.");
        return { stats, underlying, groups, strikes };
    }

    // ------------------------------------------------------------ correlation
    function corrColor(v) {
        if (!isNum(v)) return "transparent";
        const a = Math.min(1, Math.abs(v));
        return v >= 0 ? `rgba(239,68,68,${(a * 0.75).toFixed(2)})` : `rgba(34,197,94,${(a * 0.6).toFixed(2)})`;
    }

    function renderCorrelation(c) {
        if (!c) return { stats: "", matrix: "", meta: "" };
        const names = c.strategies || [];
        const statusMsg = {
            need_two_strategies: "Correlation needs at least two running strategies.",
            insufficient_data: `Not enough overlapping P&L yet (need ${c.min_observations} observations, have ${c.observations}).`,
        }[c.status];
        const stats = [
            greekCard("Strategies", num(names.length, 0), `${num(c.strategies_with_variance || 0, 0)} with P&L variance`),
            greekCard("Effective strategies", num(c.effective_strategies, 2),
                "≈ independent bets the book behaves like",
                severityFor(c.alerts, "correlation:diversification:portfolio")),
            greekCard("Diversification ratio", num(c.diversification_ratio, 2), "Σσ strategies ÷ σ portfolio · 1.0 = none"),
        ].join("");
        let matrix;
        if (names.length < 2 || !(c.matrix || []).length) {
            matrix = `<div class="muted mon-empty">${esc(statusMsg || "No data.")}</div>`;
        } else {
            const head = `<tr><th></th>${names.map((n) => `<th class="mon-corr-h" title="${esc(n)}">${esc(n)}</th>`).join("")}</tr>`;
            const body = names.map((n, i) => `<tr><th class="mon-corr-rowh">${esc(n)}</th>${(c.matrix[i] || []).map((v, j) =>
                `<td class="num mon-corr-cell" style="background:${i === j ? "transparent" : corrColor(v)}">${i === j ? "—" : isNum(v) ? v.toFixed(2) : "·"}</td>`).join("")}</tr>`).join("");
            matrix = (statusMsg ? `<div class="muted mon-empty">${esc(statusMsg)}</div>` : "") +
                `<table class="matrix-table mon-corr"><thead>${head}</thead><tbody>${body}</tbody></table>
                <div class="mon-legend muted"><span class="mon-sw" style="background:${corrColor(0.9)}"></span> move together (losses cluster)
                <span class="mon-sw" style="background:${corrColor(-0.9)}"></span> offset (hedge) · “·” = insufficient data / no variance</div>`;
        }
        const src = c.series_source === "tick_samples" ? "same-tick samples" : "runner equity curves";
        const meta = `${num(c.observations, 0)} obs · ${esc(src)} · ${esc(c.method || "")}`;
        return { stats, matrix, meta };
    }

    // ------------------------------------------------------------ regime
    const REGIME_CLS = { low_vol: "mon-regime-low", moderate_vol: "mon-regime-mod", high_vol: "mon-regime-high" };
    const SOURCE_LABEL = {
        vix: "vol index (VIX)", option_iv: "book's option IV", realized: "realized vol (proxy)",
    };
    const FIT_ICON = { optimal: "✓ optimal", acceptable: "✓ acceptable", mismatch: "⚠️ mismatch", unknown: "… unknown", unprofiled: "— unprofiled" };

    function renderRegime(r) {
        if (!r) return { badge: "—", stats: "", fit: "", source: "" };
        const flags = [r.transitioning ? "transitioning" : "", r.range_expanding ? "range expanding" : ""].filter(Boolean);
        const badge = `<span class="mon-regime ${REGIME_CLS[r.regime] || ""}">${esc(r.label || r.regime)}</span>` +
            `<span class="muted"> · ${esc(r.symbol || "")}${flags.length ? ` · ${flags.map(esc).join(" · ")}` : ""}</span>`;
        const stats = [
            greekCard("Vol index", num(r.vol_index, 2),
                `${esc(SOURCE_LABEL[r.vol_index_source] || "n/a")}` +
                (isNum(r.vol_index_change_pct)
                    ? ` · ${r.change_basis && r.change_basis !== r.vol_index_source ? `${esc(r.change_basis)} ` : ""}` +
                      `${r.vol_index_change_pct > 0 ? "+" : ""}${r.vol_index_change_pct.toFixed(1)}%`
                    : ""),
                r.transitioning ? "warning" : ""),
            greekCard("Realized vol", num(r.realized_vol, 2), isNum(r.realized_vol_prev) ? `was ${num(r.realized_vol_prev, 2)}` : "annualised"),
            greekCard("Bar range", isNum(r.current_range_pct) ? `${r.current_range_pct.toFixed(2)}%` : "—",
                isNum(r.avg_range_pct) ? `avg ${r.avg_range_pct.toFixed(2)}%` : "", r.range_expanding ? "warning" : ""),
            greekCard("Bars", num(r.bars, 0), esc(r.data_source || "")),
        ].join("");
        const fit = table([
            { label: "Strategy" }, { label: "Profile" }, { label: "Optimal vol", num: true },
            { label: "Fit" }, { label: "Recommendation" },
        ], (r.strategy_fit || []).map((f) => {
            const p = f.profile || {};
            const range = (p.optimal_vol_range || []).length === 2 ? `${num(p.optimal_vol_range[0], 0)}–${num(p.optimal_vol_range[1], 0)}` : "—";
            return `<tr class="${f.fit === "mismatch" ? "mon-row-warn" : ""}">
                <td><strong>${esc(f.strategy_name)}</strong><div class="muted mon-small">${esc(f.structure_type || f.strategy_kind || "")}</div></td>
                <td>${p.optimal_regime ? esc(p.optimal_regime) : "—"}<div class="muted mon-small">${esc((p.tags || []).join(", "))}${p.source ? ` · ${esc(p.source)}` : ""}</div></td>
                <td class="num">${range}</td>
                <td>${esc(FIT_ICON[f.fit] || f.fit)}</td>
                <td class="mon-small">${esc(f.recommendation || "")}</td>
            </tr>`;
        }), "No strategies running.");
        const plotted = SOURCE_LABEL[r.history_source || r.vol_index_source] || "no data";
        const source = `chart: ${esc(plotted)} · ${num(r.periods_per_year, 0)} bars/yr`;
        return { badge, stats, fit, source };
    }

    const MonitorView = {
        esc, renderSummary, renderAlerts, renderCounts, renderGreekCards, renderScenarios,
        renderByStrategy, renderByUnderlying, renderLegs, renderIvSources,
        renderConcentration, renderCorrelation, renderRegime, corrColor,
    };
    if (typeof globalThis !== "undefined") globalThis.MonitorView = MonitorView;

    // ================================================================ controller
    if (typeof document === "undefined" || !document.getElementById("mon-page")) return;

    const $ = (id) => document.getElementById(id);
    const setHTML = (id, html) => { const el = $(id); if (el) el.innerHTML = html; };
    const state = { timer: null, snap: null, tab: $("mon-page").dataset.section || "greeks", charts: {}, busy: false };

    function activateTab(name) {
        state.tab = name;
        document.querySelectorAll(".mon-tabs .tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
        document.querySelectorAll(".mon-page .tab-pane").forEach((p) => p.classList.toggle("active", p.id === `mon-tab-${name}`));
        if (history.replaceState) history.replaceState(null, "", `#${name}`);
        if (state.snap) drawCharts(state.snap);
    }

    function chart(id, config) {
        if (typeof Chart === "undefined") return;
        const canvas = $(id);
        if (!canvas || !canvas.offsetParent) return; // hidden tab — draw when shown
        const existing = state.charts[id];
        if (existing) {
            existing.data = config.data;
            existing.update("none");
            return;
        }
        state.charts[id] = new Chart(canvas.getContext("2d"), config);
    }

    const AXIS = { ticks: { color: "#94a3b8" }, grid: { color: "rgba(51,65,85,.5)" } };

    function drawCharts(snap) {
        const sc = snap.greeks && snap.greeks.scenarios;
        if (sc && state.tab === "greeks") {
            const spot = [...(sc.spot || [])];
            spot.push({ spot_pct: 0, pnl: 0, delta_gamma_pnl: 0 });
            spot.sort((a, b) => a.spot_pct - b.spot_pct);
            chart("mon-profile-chart", {
                type: "line",
                data: {
                    labels: spot.map((r) => `${r.spot_pct > 0 ? "+" : ""}${r.spot_pct}%`),
                    datasets: [
                        { label: "Full revaluation", data: spot.map((r) => r.pnl), borderColor: "#3b82f6", backgroundColor: "rgba(59,130,246,.15)", fill: true, tension: 0.3 },
                        { label: "Δ-Γ estimate", data: spot.map((r) => r.delta_gamma_pnl), borderColor: "#94a3b8", borderDash: [5, 4], pointRadius: 0, tension: 0.3 },
                    ],
                },
                options: {
                    responsive: true, maintainAspectRatio: false, animation: false,
                    plugins: { legend: { labels: { color: "#e2e8f0" } },
                        tooltip: { callbacks: { label: (ctx) => `${ctx.dataset.label}: ${money(ctx.parsed.y)}` } } },
                    scales: { x: AXIS, y: { ...AXIS, ticks: { color: "#94a3b8", callback: (v) => money(v) } } },
                },
            });
        }
        const rg = snap.regime;
        if (rg && state.tab === "regime") {
            const hist = rg.history || [];
            chart("mon-vol-chart", {
                type: "line",
                data: {
                    labels: hist.map((p) => String(p.ts).slice(5, 16)),
                    datasets: [{ label: SOURCE_LABEL[rg.history_source || rg.vol_index_source] || "vol", data: hist.map((p) => p.value), borderColor: "#f59e0b", pointRadius: 0, tension: 0.25 }],
                },
                options: {
                    responsive: true, maintainAspectRatio: false, animation: false,
                    plugins: { legend: { labels: { color: "#e2e8f0" } } },
                    scales: { x: { ...AXIS, ticks: { color: "#94a3b8", maxTicksLimit: 8 } }, y: AXIS },
                },
            });
        }
    }

    function render(snap) {
        state.snap = snap;
        setHTML("mon-summary", renderSummary(snap));
        setHTML("mon-alert-counts", `(${renderCounts(snap.alert_counts)})`);
        setHTML("mon-alerts", renderAlerts(snap.alerts, { showInfo: $("mon-show-info").checked }));

        const g = snap.greeks;
        setHTML("mon-greek-cards", renderGreekCards(g));
        setHTML("mon-scenarios", renderScenarios(g && g.scenarios));
        setHTML("mon-by-strategy", renderByStrategy(g));
        setHTML("mon-by-underlying", renderByUnderlying(g));
        setHTML("mon-legs", renderLegs(g));
        setHTML("mon-iv-sources", renderIvSources(g));

        const c = renderConcentration(snap.concentration);
        setHTML("mon-conc-stats", c.stats);
        setHTML("mon-conc-underlying", c.underlying);
        setHTML("mon-conc-groups", c.groups);
        setHTML("mon-conc-strikes", c.strikes);

        const k = renderCorrelation(snap.correlation);
        setHTML("mon-corr-stats", k.stats);
        setHTML("mon-corr-matrix", k.matrix);
        setHTML("mon-corr-meta", k.meta);

        const r = renderRegime(snap.regime);
        setHTML("mon-regime-badge", r.badge);
        setHTML("mon-regime-stats", r.stats);
        setHTML("mon-regime-fit", r.fit);
        setHTML("mon-regime-source", r.source);
        const sel = $("mon-regime-symbol");
        const syms = (snap.regime && snap.regime.available_symbols) || [];
        const want = ["", ...syms];
        if (sel && sel.options.length !== want.length) {
            const cur = sel.value;
            sel.innerHTML = want.map((s) => `<option value="${esc(s)}">${s ? esc(s) : "auto"}</option>`).join("");
            sel.value = want.includes(cur) ? cur : "";
        }
        setHTML("mon-updated", `updated ${new Date().toLocaleTimeString()}`);
        drawCharts(snap);
    }

    async function load() {
        if (state.busy) return;
        state.busy = true;
        const params = new URLSearchParams();
        const mode = $("mon-mode").value;
        const sym = $("mon-regime-symbol").value;
        if (mode) params.set("mode", mode);
        if (sym) params.set("symbol", sym);
        try {
            const res = await fetch(`/api/monitor/snapshot?${params}`);
            const body = await res.json();
            if (!res.ok || !body.success) throw new Error(body.error || `HTTP ${res.status}`);
            render(body.snapshot);
        } catch (err) {
            setHTML("mon-updated", `<span class="neg">update failed: ${esc(err.message)}</span>`);
        } finally {
            state.busy = false;
        }
    }

    function schedule() {
        if (state.timer) clearInterval(state.timer);
        const ms = Number($("mon-refresh").value);
        state.timer = ms > 0 ? setInterval(load, ms) : null;
    }

    document.addEventListener("click", async (ev) => {
        const tab = ev.target.closest(".mon-tabs .tab");
        if (tab) { activateTab(tab.dataset.tab); return; }
        const ack = ev.target.closest(".mon-ack");
        if (ack) {
            ack.disabled = true;
            try {
                const res = await fetch(`/api/monitor/alerts/${encodeURIComponent(ack.dataset.alertId)}/ack`, { method: "POST" });
                const body = await res.json();
                if (!res.ok || !body.success) throw new Error(body.error || `HTTP ${res.status}`);
                if (typeof showToast === "function") showToast("Alert acknowledged", "success");
                load();
            } catch (err) {
                ack.disabled = false;
                if (typeof showToast === "function") showToast(`Acknowledge failed: ${err.message}`, "error");
            }
        }
    });
    $("mon-mode").addEventListener("change", load);
    $("mon-regime-symbol").addEventListener("change", load);
    $("mon-refresh").addEventListener("change", schedule);
    $("mon-refresh-now").addEventListener("click", load);
    $("mon-show-info").addEventListener("change", () => state.snap && render(state.snap));
    document.addEventListener("visibilitychange", () => {
        if (document.hidden) { if (state.timer) clearInterval(state.timer); state.timer = null; }
        else { load(); schedule(); }
    });

    const fromHash = (location.hash || "").replace("#", "");
    activateTab(["greeks", "concentration", "correlation", "regime"].includes(fromHash) ? fromHash : state.tab);
    load();
    schedule();
})();
