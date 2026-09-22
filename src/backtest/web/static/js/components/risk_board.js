/* Risk Management Board — aggregated risk view for self-sufficient trading.
 * Enhanced per PRD FR-2.3: last breach timestamp + Export CSV + breach highlights.
 */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const state = { risk: null, mode: "", loading: false };

  const fmtMoney = (n) => {
    if (typeof Money !== "undefined" && Money.format) return Money.format(n);
    const c = document.body.dataset.currencySymbol || "₹";
    const sign = n < 0 ? "-" : "";
    return sign + c + Math.abs(Math.round(n || 0)).toLocaleString("en-IN");
  };
  const fmtSigned = (n) => {
    if (typeof Money !== "undefined" && Money.signed) return Money.signed(n);
    const v = Math.round(n || 0);
    return (v >= 0 ? "+₹" : "-₹") + Math.abs(v).toLocaleString("en-IN");
  };
  const pct = (x) => ((x || 0) * 100).toFixed(2) + "%";
  const pnlClass = (n) => (n > 0 ? "pnl-pos" : n < 0 ? "pnl-neg" : "pnl-flat");

  function toast(msg, kind) {
    if (window.showToast) return window.showToast(msg, kind);
    console.log("[risk]", msg);
  }
  async function api(url, method, body) {
    const opts = { method: method || "GET", headers: { "Content-Type": "application/json" } };
    if (body) opts.body = JSON.stringify(body);
    const res = await fetch(url, opts);
    let data = null;
    try { data = await res.json(); } catch (_) { data = {}; }
    if (!res.ok || data.success === false) throw new Error(data.error || "HTTP " + res.status);
    return data;
  }

  function riskGauge(label, valuePct, valueText, dangerThreshold = 80) {
    const pctVal = Math.min(100, Math.max(0, (valuePct || 0) * 100));
    const danger = pctVal >= dangerThreshold;
    return `
      <div class="risk-gauge">
        <div class="risk-gauge-head">
          <span class="risk-gauge-label">${label}</span>
          <span class="risk-gauge-value ${danger ? 'pnl-neg' : ''}">${valueText}</span>
        </div>
        <div class="pbar ${danger ? 'pbar-danger' : ''}"><div class="pbar-fill ${danger ? 'pbar-fill-danger' : ''}" style="width:${pctVal.toFixed(1)}%"></div></div>
        <div class="risk-gauge-sub muted">${pctVal.toFixed(1)}% used</div>
      </div>`;
  }

  function exportCSV(rows, filename) {
    if (!rows || !rows.length) { toast("Nothing to export", "warning"); return; }
    const headers = Object.keys(rows[0]);
    const csv = [headers.join(",")].concat(rows.map(r => headers.map(h => `"${String(r[h] ?? "").replace(/"/g,'""')}"`).join(","))).join("\n");
    const blob = new Blob([csv], { type: "text/csv" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a"); a.href = url; a.download = filename; a.click();
    URL.revokeObjectURL(url);
  }

  function lastBreachTimestamp(audit) {
    if (!audit || !audit.length) return null;
    const breach = audit.find(a => /HALT|BREACH|BREAKER|EMERGENCY/i.test(a.action||"") || /HALT|BREACH/i.test(a.reason||"") || /HALT|BREACH/i.test(a.detail||""));
    return breach ? (breach.ts || breach.timestamp || breach.time || "") : null;
  }

  function renderRisk(r) {
    if (!r) return;
    const cur = r.current || {};
    const buckets = r.buckets || {};
    const exposure = r.exposure || {};
    const tradeStats = r.trade_stats || {};
    const globalCfg = r.global_config || {};
    const bucketLimits = r.bucket_limits || {};
    const warnings = cur.warnings || [];
    const audit = r.risk_audit || [];

    // Gauges
    const gaugesEl = $("risk-gauges");
    if (gaugesEl) {
      const dailyLossPct = cur.daily_loss_pct || 0;
      const drawdownPct = cur.drawdown_pct || 0;
      const deployedPct = cur.deployed_pct || 0;
      const grossPct = exposure.gross_exposure_pct || 0;
      gaugesEl.innerHTML = [
        riskGauge("Daily Loss Used", dailyLossPct, `${fmtMoney(cur.daily_loss_used || 0)} / ${fmtMoney(globalCfg.daily_loss_limit || 0)}`, 75),
        riskGauge("Drawdown", drawdownPct, pct(drawdownPct) + " / " + pct(globalCfg.max_drawdown_pct || 0.25), 70),
        riskGauge("Deployed Capital", deployedPct, pct(deployedPct), 90),
        riskGauge("Gross Exposure", grossPct, pct(grossPct) + " of equity", 85),
      ].join("");
    }

    // CB status + last breach
    const cbEl = $("risk-cb-status");
    if (cbEl) {
      const lastBreach = lastBreachTimestamp(audit);
      if (cur.halted) {
        cbEl.innerHTML = `<div class="cb-banner" style="display:flex"><span>🚨 HALTED</span><span class="cb-reason">${cur.halt_reason || ""} (${cur.halt_mode || ""})</span></div>
          ${lastBreach ? `<div class="last-breach">Last breach: ${lastBreach.slice(0,19).replace("T"," ")} <span class="muted">— ${audit.length} risk events</span></div>` : ""}
          <div style="margin-top:8px;"><button class="btn btn-ghost btn-small" id="risk-export-csv" type="button">Export CSV</button></div>`;
      } else {
        cbEl.innerHTML = `<div class="risk-ok">✅ Normal — no breaker tripped</div>
          ${lastBreach ? `<div class="last-breach">Last breach: ${lastBreach.slice(0,19).replace("T"," ")} — system recovered</div>` : `<div class="last-breach">No breaches recorded</div>`}
          <div style="margin-top:8px; display:flex; gap:8px;"><button class="btn btn-ghost btn-small" id="risk-export-csv" type="button">Export CSV</button><a href="/risk" class="btn btn-ghost btn-small">Full Risk Page →</a></div>`;
      }
      const exportBtn = document.getElementById("risk-export-csv");
      if (exportBtn) exportBtn.addEventListener("click", () => {
        const rows = audit.map(a => ({ ts: a.ts || a.timestamp || "", scope: a.scope||"", action: a.action||"", detail: a.detail||a.reason||"", runner: a.runner_id||a.instance_id||"" }));
        exportCSV(rows.length?rows:[{note:"no risk events"}], "risk_audit.csv");
      });
    }

    // Warnings with breach highlight
    const warnEl = $("risk-warnings");
    if (warnEl) {
      if (!warnings.length) {
        warnEl.innerHTML = `<div class="muted">No concentration warnings.</div>`;
      } else {
        warnEl.innerHTML = warnings.map(w => {
          const msg = w.message || JSON.stringify(w);
          const isBreach = /BREACH|HALT|EXCEED/i.test(msg);
          return `<div class="warning-line ${isBreach ? 'breach-highlight' : ''}">⚠️ ${msg}</div>`;
        }).join("");
      }
    }

    // Bucket table with breach highlight
    const bucketEl = $("risk-bucket-table");
    if (bucketEl) {
      const rows = Object.keys(buckets).map(mode => {
        const b = buckets[mode];
        const lim = bucketLimits[mode] || {};
        const breach = b.halted || (b.daily_loss_pct||0) >= 0.8 || (b.drawdown_pct||0) >= 0.2;
        return `<tr class="${breach ? 'audit-breach' : ''}">
          <td><span class="badge ${mode === 'live' ? 'badge-live' : 'badge-paper'}">${mode.toUpperCase()}</span></td>
          <td class="num">${b.count || 0}</td>
          <td class="num">${fmtMoney(b.equity || 0)}</td>
          <td class="num ${pnlClass(b.daily_pnl)}">${fmtSigned(b.daily_pnl || 0)}</td>
          <td class="num">${pct(b.drawdown_pct)}</td>
          <td class="num">${b.open_positions || 0} / ${lim.max_open_positions ?? lim.max_positions ?? "∞"}</td>
          <td class="num">${lim.max_position_pct ? pct(parseFloat(lim.max_position_pct)) : "∞"}</td>
          <td class="num">${lim.max_position_value || "∞"}</td>
          <td class="num">${lim.allowed_sources ? lim.allowed_sources.join(",") : "all"}</td>
          <td>${b.halted ? "🚨 HALTED" : "✅"}</td>
        </tr>`;
      }).join("");
      bucketEl.innerHTML = rows || `<tr><td colspan="10" class="muted">No buckets</td></tr>`;
    }

    // Global config
    const cfgEl = $("risk-global-config");
    if (cfgEl) {
      cfgEl.innerHTML = `
        <div class="risk-kv"><span>Daily Loss Limit</span><strong>${fmtMoney(globalCfg.daily_loss_limit || 0)}</strong></div>
        <div class="risk-kv"><span>Max Drawdown</span><strong>${pct(globalCfg.max_drawdown_pct)}</strong></div>
        <div class="risk-kv"><span>Max Leverage</span><strong>${globalCfg.max_leverage || 1}x</strong></div>
        <div class="risk-kv"><span>Breach Mode</span><strong>${globalCfg.breach_mode || ""}</strong></div>
        <div class="risk-kv"><span>Correlation Threshold</span><strong>${globalCfg.correlation_warning_threshold || 3}</strong></div>
      `;
    }

    // Exposure by symbol
    const symEl = $("risk-exposure-symbol");
    if (symEl) {
      const bySym = exposure.by_symbol || [];
      if (!bySym.length) symEl.innerHTML = `<tr><td colspan="5" class="muted">No open exposure</td></tr>`;
      else symEl.innerHTML = bySym.slice(0, 20).map(e => `<tr><td>${e.symbol}</td><td class="num">${fmtMoney(e.notional)}</td><td class="num">${e.qty}</td><td class="num">${e.runners}</td><td>${(e.sides || []).join(", ")}</td></tr>`).join("");
    }

    // Correlation
    const corrEl = $("risk-exposure-correlation");
    if (corrEl) {
      const byCorr = exposure.by_correlation || [];
      if (!byCorr.length) corrEl.innerHTML = `<tr><td colspan="5" class="muted">No correlation exposure</td></tr>`;
      else corrEl.innerHTML = byCorr.map(c => `<tr class="${c.count >= (c.threshold||3) ? 'audit-breach' : ''}"><td>${c.label || c.group}</td><td class="num">${fmtMoney(c.notional)}</td><td class="num">${c.count}</td><td>${(c.symbols || []).join(", ")}</td><td>${c.count >= (c.threshold || 3) ? "⚠️ HIGH" : "OK"}</td></tr>`).join("");
    }

    // Trade stats
    const tradeEl = $("risk-trade-stats");
    if (tradeEl) {
      tradeEl.innerHTML = `<div class="risk-stats-grid">
          <div class="risk-stat"><span>Total Trades</span><strong>${tradeStats.total_trades || 0}</strong></div>
          <div class="risk-stat"><span>Win Rate</span><strong>${pct(tradeStats.win_rate)}</strong></div>
          <div class="risk-stat"><span>Profit Factor</span><strong>${tradeStats.profit_factor || 0}</strong></div>
          <div class="risk-stat"><span>Total PnL</span><strong class="${pnlClass(tradeStats.total_pnl)}">${fmtSigned(tradeStats.total_pnl || 0)}</strong></div>
          <div class="risk-stat"><span>Avg Win</span><strong class="pnl-pos">${fmtMoney(tradeStats.avg_win || 0)}</strong></div>
          <div class="risk-stat"><span>Avg Loss</span><strong class="pnl-neg">${fmtMoney(tradeStats.avg_loss || 0)}</strong></div>
          <div class="risk-stat"><span>Best / Worst</span><strong>${fmtMoney(tradeStats.best_trade || 0)} / ${fmtMoney(tradeStats.worst_trade || 0)}</strong></div>
          <div class="risk-stat"><span>Max Consec Losses</span><strong>${tradeStats.max_consecutive_losses || 0}</strong></div>
        </div>`;
    }

    // Audit with breach highlights
    const auditEl = $("risk-audit");
    if (auditEl) {
      if (!audit.length) auditEl.innerHTML = `<div class="muted">No risk events yet.</div>`;
      else auditEl.innerHTML = audit.map(a => {
        const isBreach = /HALT|BREACH|EMERGENCY|BREAKER/i.test(a.action||"") || /HALT|BREACH/i.test(a.detail||"") || /HALT|BREACH/i.test(a.reason||"");
        return `<div class="audit-line ${isBreach ? 'audit-danger breach-highlight' : (a.action||'').includes('HALT') || (a.action||'').includes('EMERGENCY') ? 'audit-danger' : 'audit-action'}">
            <span class="audit-ts">${(a.ts || a.timestamp || "").slice(0,19).replace("T"," ")}</span>
            <span>[${(a.scope || "").toUpperCase()}] ${a.action || ""} ${a.detail ? "— " + a.detail : a.reason ? "— " + a.reason : ""}</span>
          </div>`;
      }).join("");
    }

    const expSumEl = $("risk-exposure-summary");
    if (expSumEl) expSumEl.textContent = `Total gross ${fmtMoney(exposure.total_gross_notional || 0)} · ${pct(exposure.gross_exposure_pct)} of equity`;
  }

  async function refresh() {
    if (state.loading) return;
    state.loading = true;
    const btn = $("btn-risk-refresh");
    if (btn) btn.disabled = true;
    try {
      const mode = state.mode || ($("risk-mode") ? $("risk-mode").value : "");
      const url = "/api/portfolio/risk" + (mode ? "?mode=" + mode : "");
      const data = await api(url);
      state.risk = data.risk;
      renderRisk(state.risk);
    } catch (e) {
      toast("Risk board failed: " + e.message, "error");
    } finally {
      state.loading = false;
      if (btn) btn.disabled = false;
    }
  }

  async function refreshAggregatedTrades() {
    try {
      const mode = state.mode || ($("risk-mode") ? $("risk-mode").value : "");
      const kind = $("agg-trade-kind") ? $("agg-trade-kind").value : "all";
      const limit = $("agg-trade-limit") ? $("agg-trade-limit").value : "100";
      const url = `/api/portfolio/trades/aggregated?mode=${mode}&kind=${kind}&limit=${limit}`;
      const data = await api(url);
      const tbody = $("agg-trades-body");
      const summaryEl = $("agg-trades-summary");
      if (summaryEl) summaryEl.textContent = `${data.summary.total} total · ${data.summary.returned} shown · PnL ${fmtSigned(data.summary.total_pnl)} · Win rate ${pct(data.summary.win_rate)}`;
      if (tbody) {
        if (!data.trades.length) tbody.innerHTML = `<tr><td colspan="9" class="muted">No closed trades yet.</td></tr>`;
        else tbody.innerHTML = data.trades.map(t => `<tr><td>${t.symbol || ""}</td><td>${t.runner_name || ""}</td><td>${t.strategy || ""}</td><td>${t.kind || "equity"}</td><td>${t.side || ""}</td><td class="num">${t.qty ?? ""}</td><td class="num">${t.entry_price ?? ""} → ${t.exit_price ?? ""}</td><td class="num ${pnlClass(t.pnl)}">${fmtSigned(t.pnl || 0)}</td><td>${t.exit_reason || ""} <span class="muted">${(t.exit_ts || "").slice(0,16)}</span></td></tr>`).join("");
      }
    } catch (e) {
      toast("Aggregated trades failed: " + e.message, "error");
    }
  }

  function bindEvents() {
    const refreshBtn = $("btn-risk-refresh");
    if (refreshBtn) refreshBtn.addEventListener("click", refresh);
    const modeSel = $("risk-mode");
    if (modeSel) modeSel.addEventListener("change", (e) => { state.mode = e.target.value; refresh(); refreshAggregatedTrades(); });
    const aggRefreshBtn = $("btn-agg-trades-refresh");
    if (aggRefreshBtn) aggRefreshBtn.addEventListener("click", refreshAggregatedTrades);
    const kindSel = $("agg-trade-kind");
    if (kindSel) kindSel.addEventListener("change", refreshAggregatedTrades);
    const limitSel = $("agg-trade-limit");
    if (limitSel) limitSel.addEventListener("change", refreshAggregatedTrades);
    const stressBtn = $("btn-risk-stress");
    if (stressBtn) stressBtn.addEventListener("click", async () => {
      if (!confirm("Inject simulated crash (-25%) to test circuit breaker?")) return;
      try {
        const data = await api("/api/portfolio/test/breach", "POST", { crash_pct: 0.25, tighten_limits: true });
        toast("Crash injected — breaker: " + (data.portfolio.halted ? "HALTED" : "NORMAL"), data.portfolio.halted ? "error" : "success");
        refresh();
      } catch (e) { toast("Stress test failed: " + e.message, "error"); }
    });
    const resetBtn = $("btn-risk-reset-breaker");
    if (resetBtn) resetBtn.addEventListener("click", async () => {
      try {
        const mode = state.mode || (modeSel ? modeSel.value : "");
        const url = "/api/portfolio/control/reset_breaker" + (mode ? "?mode=" + mode : "");
        await api(url, "POST", {});
        toast("Breaker reset", "success"); refresh();
      } catch (e) { toast("Reset failed: " + e.message, "error"); }
    });
  }

  window.RiskBoard = { refresh, refreshAggregatedTrades, bindEvents, getState: () => state };
  document.addEventListener("DOMContentLoaded", () => { bindEvents(); });
})();
