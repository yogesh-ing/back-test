/* Tier 3: Dedicated /risk page — Global Dashboard, Config, Audit, Exposure, Trades */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const fmtMoney = (n) => {
    if (typeof Money !== "undefined" && Money.format) return Money.format(n);
    const c = document.body.dataset.currencySymbol || "₹";
    return c + Math.abs(Math.round(n || 0)).toLocaleString("en-IN");
  };
  const fmtSigned = (n) => {
    if (typeof Money !== "undefined" && Money.signed) return Money.signed(n);
    const v = Math.round(n || 0);
    return (v >= 0 ? "+₹" : "-₹") + Math.abs(v).toLocaleString("en-IN");
  };
  const pct = (x) => ((x || 0) * 100).toFixed(1) + "%";

  let currentRisk = null;
  let currentConfig = null;
  let auditData = [];
  let auditPage = 1;
  const auditPageSize = 20;
  let charts = {};

  async function fetchJSON(url, opts) {
    const res = await fetch(url, opts);
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    return data;
  }

  function gaugeClass(pctVal, thresholds) {
    if (pctVal >= (thresholds.danger || 0.8)) return "danger";
    if (pctVal >= (thresholds.warning || 0.5)) return "warning";
    return "safe";
  }

  async function loadRisk() {
    const mode = $("risk-page-mode")?.value || "";
    const url = mode ? `/api/portfolio/risk?mode=${mode}` : "/api/portfolio/risk";
    try {
      const data = await fetchJSON(url);
      currentRisk = data.risk;
      renderDashboard(data.risk);
      renderExposure(data.risk);
    } catch (e) {
      const grid = $("risk-dashboard-grid");
      if (grid) grid.innerHTML = `<div class="card-error">Failed: ${e.message}</div>`;
    }
  }

  function renderDashboard(risk) {
    const grid = $("risk-dashboard-grid");
    if (!grid) return;
    const cur = risk.current || {};
    const buckets = risk.buckets || {};
    const totalEquity = cur.total_equity || 0;

    // Calculate derived metrics
    const dailyLossPct = cur.daily_loss_pct || 0;
    const drawdownPct = cur.drawdown_pct || 0;
    const deployedPct = cur.deployed_pct || 0;
    const grossPct = risk.exposure?.gross_exposure_pct || deployedPct;
    const openPos = Object.values(buckets).reduce((s, b) => s + (b.open_positions || 0), 0) || cur.open_positions || 0;
    const halted = cur.halted ? "HALTED" : "LIVE";

    const gauges = [
      { label: "Daily Loss Used", value: pct(dailyLossPct), raw: dailyLossPct, sub: `${fmtMoney(cur.daily_loss_used || 0)} / limit`, bar: dailyLossPct * 100, thresh: { warning: 0.5, danger: 0.8 } },
      { label: "Drawdown", value: pct(drawdownPct), raw: drawdownPct, sub: `Peak protected`, bar: (drawdownPct / 0.25) * 100, thresh: { warning: 0.5, danger: 0.8 } },
      { label: "Capital Deployed", value: pct(deployedPct), raw: deployedPct, sub: fmtMoney(cur.deployed_capital || 0), bar: deployedPct * 100, thresh: { warning: 0.7, danger: 0.9 } },
      { label: "Gross Exposure", value: pct(grossPct), raw: grossPct, sub: fmtMoney(risk.exposure?.total_gross_notional || 0), bar: Math.min(100, grossPct * 100), thresh: { warning: 0.8, danger: 1.0 } },
      { label: "Open Positions", value: `${openPos}`, raw: openPos / 50, sub: `${Object.keys(buckets).length} buckets active`, bar: Math.min(100, (openPos / 30) * 100), thresh: { warning: 0.6, danger: 0.9 } },
      { label: "System Status", value: halted, raw: cur.halted ? 1 : 0, sub: cur.halt_reason || "All systems nominal", bar: cur.halted ? 100 : 10, thresh: { warning: 0.5, danger: 0.9 } },
    ];

    grid.innerHTML = gauges.map(g => {
      const cls = gaugeClass(g.raw, g.thresh);
      const barColor = cls === "danger" ? "var(--danger)" : cls === "warning" ? "var(--warning)" : "var(--success)";
      return `<div class="risk-gauge-large ${cls}">
        <div class="gauge-label">${g.label}</div>
        <div class="gauge-value">${g.value}</div>
        <div class="muted small">${g.sub}</div>
        <div class="gauge-bar"><div class="gauge-bar-fill" style="width:${Math.min(100, g.bar)}%; background:${barColor}"></div></div>
      </div>`;
    }).join("");

    // CB banner
    const cbBanner = $("risk-cb-banner");
    if (cbBanner) {
      if (cur.halted) {
        cbBanner.hidden = false;
        cbBanner.className = "cb-banner";
        cbBanner.innerHTML = `<span class="cb-icon">🚨</span><div class="cb-body"><strong>CIRCUIT BREAKER HALTED</strong><span class="cb-reason">${cur.halt_reason || ""} (${cur.halt_mode || ""})</span></div><button class="btn btn-ghost" id="risk-reset-breaker" type="button">Reset & Resume</button>`;
        cbBanner.querySelector("#risk-reset-breaker")?.addEventListener("click", async () => {
          try { await fetchJSON("/api/portfolio/control/reset_breaker", { method: "POST" }); loadRisk(); if (window.showToast) showToast("Breaker reset", "success"); } catch(e){ showToast(e.message,"error"); }
        });
      } else {
        cbBanner.hidden = true;
      }
    }

    const warnBanner = $("risk-warnings-banner");
    if (warnBanner) {
      const warns = cur.warnings || [];
      if (warns.length) {
        warnBanner.hidden = false;
        warnBanner.className = "warning-banner";
        warnBanner.innerHTML = warns.map(w => `<div class="warning-line">⚠️ ${w}</div>`).join("");
      } else {
        warnBanner.hidden = true;
      }
    }
  }

  async function loadConfig() {
    try {
      const data = await fetchJSON("/api/portfolio/risk/config");
      currentConfig = data;
      renderConfig(data);
    } catch (e) {
      const el = $("risk-global-config-form");
      if (el) el.innerHTML = `<div class="card-error">Failed: ${e.message}</div>`;
    }
  }

  function renderConfig(cfg) {
    const globalForm = $("risk-global-config-form");
    if (globalForm) {
      const g = cfg.global || {};
      globalForm.innerHTML = `
        <div class="risk-config-row"><label>Daily Loss Limit (${fmtMoney(1)})</label><input id="cfg-daily-loss" type="range" min="1000" max="100000" step="1000" value="${g.daily_loss_limit || 50000}" class="risk-slider"><span id="cfg-daily-loss-val">${fmtMoney(g.daily_loss_limit || 50000)}</span></div>
        <div class="risk-config-row"><label>Max Drawdown %</label><input id="cfg-drawdown" type="range" min="1" max="50" step="1" value="${(g.max_drawdown_pct || 0.25)*100}" class="risk-slider"><span id="cfg-drawdown-val">${pct(g.max_drawdown_pct || 0.25)}</span></div>
        <div class="risk-config-row"><label>Max Leverage</label><input id="cfg-leverage" type="range" min="1" max="5" step="0.1" value="${g.max_leverage || 1}" class="risk-slider"><span id="cfg-leverage-val">${g.max_leverage || 1}x</span></div>
        <div class="risk-config-row"><label>Breach Mode</label><select id="cfg-breach-mode" class="input input-select"><option value="PAUSE_AND_HOLD" ${g.breach_mode==="PAUSE_AND_HOLD"?"selected":""}>Pause & Hold</option>      <option value="EMERGENCY_FLATTEN" ${g.breach_mode==="EMERGENCY_FLATTEN"?"selected":""}>Flatten & Halt</option></select><span></span></div>
        <div class="risk-config-row"><label>Correlation Warn Threshold</label><input id="cfg-corr" type="range" min="1" max="10" step="1" value="${g.correlation_warning_threshold || 3}" class="risk-slider"><span id="cfg-corr-val">${g.correlation_warning_threshold || 3}</span></div>
      `;
      // Bind live updates
      $("cfg-daily-loss")?.addEventListener("input", e => { $("cfg-daily-loss-val").textContent = fmtMoney(parseFloat(e.target.value)); });
      $("cfg-drawdown")?.addEventListener("input", e => { $("cfg-drawdown-val").textContent = pct(parseFloat(e.target.value)/100); });
      $("cfg-leverage")?.addEventListener("input", e => { $("cfg-leverage-val").textContent = e.target.value + "x"; });
      $("cfg-corr")?.addEventListener("input", e => { $("cfg-corr-val").textContent = e.target.value; });
    }

    const bucketForm = $("risk-bucket-config-form");
    if (bucketForm) {
      const buckets = cfg.buckets || {};
      bucketForm.innerHTML = Object.entries(buckets).map(([mode, lim]) => `
        <div style="border:1px solid var(--border); border-radius:8px; padding:10px; margin-bottom:8px;">
          <strong>${mode.toUpperCase()}</strong>
          <div class="risk-config-row"><label>Max Position %</label><input data-bucket="${mode}" data-field="max_position_pct" type="range" min="1" max="100" step="1" value="${(lim.max_position_pct||0.1)*100}" class="risk-slider"><span>${pct(lim.max_position_pct||0.1)}</span></div>
          <div class="risk-config-row"><label>Max Position Value</label><input data-bucket="${mode}" data-field="max_position_value" type="number" value="${lim.max_position_value||10000}" class="input" style="max-width:140px;"><span>${fmtMoney(lim.max_position_value||0)}</span></div>
          <div class="risk-config-row"><label>Max Open Positions</label><input data-bucket="${mode}" data-field="max_open_positions" type="number" min="1" max="100" value="${lim.max_open_positions||''}" class="input" style="max-width:100px;"><span></span></div>
        </div>
      `).join("");
    }

    const corrEl = $("risk-correlation-config");
    if (corrEl) {
      const groups = cfg.correlation_groups || {};
      corrEl.innerHTML = `<div style="display:grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap:8px;">` +
        Object.entries(groups).map(([gid, meta]) => `
          <div style="background:var(--surface-2); border:1px solid var(--border); border-radius:6px; padding:8px;">
            <strong>${meta.label || gid}</strong> <span class="muted">thr ${meta.threshold}</span>
            <div class="muted small" style="margin-top:4px; word-break:break-all;">${(meta.symbols||[]).slice(0,8).join(", ")}${(meta.symbols||[]).length>8?"…":""}</div>
          </div>
        `).join("") + `</div>`;
    }
  }

  function applyPreset(preset) {
    if (!currentConfig) return;
    const presets = {
      conservative: { daily_loss_limit: 20000, max_drawdown_pct: 0.1, max_leverage: 1, correlation_warning_threshold: 2, bucket: { max_position_pct: 0.05, max_open_positions: 3 } },
      balanced: { daily_loss_limit: 50000, max_drawdown_pct: 0.25, max_leverage: 1.5, correlation_warning_threshold: 3, bucket: { max_position_pct: 0.1, max_open_positions: 5 } },
      aggressive: { daily_loss_limit: 100000, max_drawdown_pct: 0.4, max_leverage: 3, correlation_warning_threshold: 5, bucket: { max_position_pct: 0.2, max_open_positions: 10 } },
    };
    const p = presets[preset];
    if (!p) return;
    const dl = $("cfg-daily-loss");
    const dd = $("cfg-drawdown");
    const lev = $("cfg-leverage");
    const corr = $("cfg-corr");
    if (dl) { dl.value = p.daily_loss_limit; $("cfg-daily-loss-val").textContent = fmtMoney(p.daily_loss_limit); }
    if (dd) { dd.value = p.max_drawdown_pct * 100; $("cfg-drawdown-val").textContent = pct(p.max_drawdown_pct); }
    if (lev) { lev.value = p.max_leverage; $("cfg-leverage-val").textContent = p.max_leverage + "x"; }
    if (corr) { corr.value = p.correlation_warning_threshold; $("cfg-corr-val").textContent = p.correlation_warning_threshold; }
    // Highlight preset button
    document.querySelectorAll("#risk-presets button").forEach(b => b.classList.remove("risk-preset-active"));
    const activeBtn = document.querySelector(`#risk-presets [data-preset="${preset}"]`);
    if (activeBtn) activeBtn.classList.add("risk-preset-active");
  }

  async function saveGlobal() {
    const payload = {
      daily_loss_limit: parseFloat($("cfg-daily-loss")?.value || 50000),
      max_drawdown_pct: parseFloat($("cfg-drawdown")?.value || 25) / 100,
      max_leverage: parseFloat($("cfg-leverage")?.value || 1),
      breach_mode: $("cfg-breach-mode")?.value || "PAUSE_AND_HOLD",
      correlation_warning_threshold: parseInt($("cfg-corr")?.value || 3),
    };
    try {
      await fetchJSON("/api/portfolio/risk/config", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ global: payload }) });
      const msg = $("risk-save-msg");
      if (msg) { msg.textContent = "✓ Saved"; setTimeout(()=>msg.textContent="",2000); }
      if (window.showToast) window.showToast("Global risk config saved", "success");
      loadRisk();
    } catch (e) {
      if (window.showToast) window.showToast("Save failed: " + e.message, "error");
    }
  }

  async function saveBuckets() {
    const buckets = {};
    document.querySelectorAll("[data-bucket]").forEach(el => {
      const mode = el.dataset.bucket;
      const field = el.dataset.field;
      if (!buckets[mode]) buckets[mode] = {};
      buckets[mode][field] = el.type === "range" ? parseFloat(el.value)/100 : parseFloat(el.value);
      if (field === "max_position_value" || field === "max_open_positions") buckets[mode][field] = parseFloat(el.value);
    });
    try {
      await fetchJSON("/api/portfolio/risk/config", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ buckets }) });
      if (window.showToast) window.showToast("Bucket overrides saved", "success");
      loadRisk();
    } catch (e) {
      if (window.showToast) window.showToast("Save failed: " + e.message, "error");
    }
  }

  // Audit timeline
  async function loadAudit() {
    const scope = $("audit-scope")?.value || "all";
    const type = $("audit-type")?.value || "all";
    const search = $("audit-search")?.value || "";
    const url = `/api/portfolio/audit?scope=${scope}&limit=500`;
    try {
      const data = await fetchJSON(url);
      let entries = data.audit || [];
      // Filter by type
      if (type === "breach") entries = entries.filter(e => /BREACH|HALT/i.test(e.action || ""));
      else if (type === "flatten") entries = entries.filter(e => /FLATTEN/i.test(e.action || ""));
      else if (type === "halt") entries = entries.filter(e => /HALT|BREAKER/i.test(e.action || ""));
      if (search) {
        const s = search.toLowerCase();
        entries = entries.filter(e => (e.action||"").toLowerCase().includes(s) || (e.reason||"").toLowerCase().includes(s) || JSON.stringify(e).toLowerCase().includes(s));
      }
      auditData = entries;
      auditPage = 1;
      renderAudit();
    } catch (e) {
      const tbody = $("audit-tbody");
      if (tbody) tbody.innerHTML = `<tr><td colspan="5" class="card-error">Failed: ${e.message}</td></tr>`;
    }
  }

  function renderAudit() {
    const tbody = $("audit-tbody");
    if (!tbody) return;
    const start = (auditPage - 1) * auditPageSize;
    const pageEntries = auditData.slice(start, start + auditPageSize);
    if (!pageEntries.length) {
      tbody.innerHTML = `<tr><td colspan="5" class="muted">No audit entries match filter.</td></tr>`;
    } else {
      tbody.innerHTML = pageEntries.map(e => {
        const isBreach = /BREACH|HALT|BREAKER/i.test(e.action || "");
        const cls = isBreach ? "audit-breach" : /WARN|BLOCK/i.test(e.action||"") ? "audit-warning" : "";
        const ts = e.ts || e.timestamp || "";
        return `<tr class="${cls}">
          <td>${ts.slice(0,19).replace("T"," ")}</td>
          <td><span class="badge badge-${e.scope==='live'?'live':'paper'}">${e.scope||""}</span></td>
          <td><strong>${e.action||""}</strong></td>
          <td>${e.reason || e.details || JSON.stringify(e).slice(0,120)}</td>
          <td>${e.runner_id || e.instance_id || ""}</td>
        </tr>`;
      }).join("");
    }
    // Pagination
    const pag = $("audit-pagination");
    if (pag) {
      const totalPages = Math.ceil(auditData.length / auditPageSize) || 1;
      pag.innerHTML = `<button ${auditPage<=1?"disabled":""} data-p="prev">Prev</button><span class="muted">Page ${auditPage} / ${totalPages} — ${auditData.length} entries</span><button ${auditPage>=totalPages?"disabled":""} data-p="next">Next</button>`;
      pag.querySelectorAll("button").forEach(btn => {
        btn.addEventListener("click", () => {
          if (btn.dataset.p === "prev" && auditPage > 1) auditPage--;
          else if (btn.dataset.p === "next" && auditPage < totalPages) auditPage++;
          renderAudit();
        });
      });
    }
  }

  function exportCSV(rows, filename) {
    if (!rows.length) { if (window.showToast) showToast("Nothing to export", "warning"); return; }
    const headers = Object.keys(rows[0]);
    const csv = [headers.join(",")].concat(rows.map(r => headers.map(h => `"${String(r[h] ?? "").replace(/"/g,'""')}"`).join(","))).join("\n");
    const blob = new Blob([csv], { type: "text/csv" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = filename; a.click();
    URL.revokeObjectURL(url);
  }

  // Exposure
  function renderExposure(risk) {
    const exp = risk.exposure || {};
    const bySymbol = exp.by_symbol || [];
    const byCorr = exp.by_correlation || [];

    const symBody = $("exp-symbol-tbody");
    if (symBody) {
      symBody.innerHTML = bySymbol.slice(0,20).map(s => `<tr><td>${s.symbol}</td><td class="num">${fmtMoney(s.notional)}</td><td class="num">${s.runners}</td></tr>`).join("") || `<tr><td colspan="3" class="muted">No exposure</td></tr>`;
    }
    const corrBody = $("exp-corr-tbody");
    if (corrBody) {
      corrBody.innerHTML = byCorr.map(c => `<tr><td>${c.label||c.group}</td><td class="num">${fmtMoney(c.notional)}</td><td class="num">${c.count||0}</td></tr>`).join("") || `<tr><td colspan="3" class="muted">No correlation exposure</td></tr>`;
    }

    // Charts
    if (typeof Chart !== "undefined") {
      // Pie
      const pieCanvas = $("exposure-pie");
      if (pieCanvas && bySymbol.length) {
        if (charts.pie) charts.pie.destroy();
        charts.pie = new Chart(pieCanvas.getContext("2d"), {
          type: "pie",
          data: {
            labels: bySymbol.slice(0,8).map(s => s.symbol),
            datasets: [{ data: bySymbol.slice(0,8).map(s => s.notional), backgroundColor: ["#3b82f6","#8b5cf6","#22c55e","#f59e0b","#ef4444","#06b6d4","#ec4899","#84cc16"] }]
          },
          options: { responsive: true, plugins: { legend: { labels: { color: "#e2e8f0" } } } }
        });
      }
      // Bar
      const barCanvas = $("exposure-bar");
      if (barCanvas && byCorr.length) {
        if (charts.bar) charts.bar.destroy();
        charts.bar = new Chart(barCanvas.getContext("2d"), {
          type: "bar",
          data: {
            labels: byCorr.map(c => c.label || c.group),
            datasets: [{ label: "Notional", data: byCorr.map(c => c.notional), backgroundColor: "#3b82f6" }]
          },
          options: { responsive: true, plugins: { legend: { display: false } }, scales: { x: { ticks: { color: "#94a3b8" } }, y: { ticks: { color: "#94a3b8" } } } }
        });
      }
      // Line for risk history (from strip)
      const lineCanvas = $("exposure-line");
      if (lineCanvas) {
        const hist = window.RiskStrip?.getHistory?.() || [];
        if (hist.length > 2) {
          if (charts.line) charts.line.destroy();
          charts.line = new Chart(lineCanvas.getContext("2d"), {
            type: "line",
            data: {
              labels: hist.map(h => h.ts),
              datasets: [
                { label: "Daily Loss %", data: hist.map(h => (h.daily_loss_pct*100).toFixed(1)), borderColor: "#ef4444", tension: 0.3, pointRadius: 0 },
                { label: "Drawdown %", data: hist.map(h => (h.drawdown_pct*100).toFixed(1)), borderColor: "#f59e0b", tension: 0.3, pointRadius: 0 },
              ]
            },
            options: { responsive: true, animation: false, scales: { x: { display: false }, y: { ticks: { color: "#94a3b8" } } } }
          });
        }
      }
    }

    // Concentration warnings
    const warnEl = $("exposure-concentration-warnings");
    if (warnEl) {
      const warnings = [];
      bySymbol.forEach(s => {
        const pctExp = exp.total_gross_notional ? (s.notional / exp.total_gross_notional) : 0;
        if (pctExp > 0.3) warnings.push(`⚠️ High concentration: ${s.symbol} is ${(pctExp*100).toFixed(1)}% of gross`);
      });
      byCorr.forEach(c => {
        if (c.count >= (c.threshold || 3)) warnings.push(`⚠️ Correlation breach: ${c.label||c.group} has ${c.count} positions (threshold ${c.threshold||3})`);
      });
      warnEl.innerHTML = warnings.map(w => `<div class="concentration-warn">${w}</div>`).join("");
    }
  }

  // Trades tab
  async function loadTrades() {
    const kind = $("trades-kind")?.value || "all";
    const limit = $("trades-limit")?.value || "100";
    const mode = $("risk-page-mode")?.value || "";
    const url = `/api/portfolio/trades/aggregated?kind=${kind}&limit=${limit}${mode?`&mode=${mode}`:""}`;
    try {
      const data = await fetchJSON(url);
      renderTrades(data);
    } catch (e) {
      const el = $("trades-grouped");
      if (el) el.innerHTML = `<div class="card-error">Failed: ${e.message}</div>`;
    }
  }

  function renderTrades(data) {
    const summaryEl = $("perf-summary");
    if (summaryEl) {
      const s = data.summary || {};
      const trades = data.trades || [];
      const pnls = trades.map(t => t.pnl || 0);
      const wins = pnls.filter(p => p >= 0).length;
      const total = pnls.length;
      const best = total ? Math.max(...pnls) : 0;
      const worst = total ? Math.min(...pnls) : 0;
      const avg = total ? pnls.reduce((a,b)=>a+b,0)/total : 0;
      summaryEl.innerHTML = `
        <div class="perf-stat"><div class="label">Total Trades</div><div class="value">${s.total||total}</div></div>
        <div class="perf-stat"><div class="label">Total P&L</div><div class="value ${s.total_pnl>=0?"pnl-pos":"pnl-neg"}">${fmtSigned(s.total_pnl||0)}</div></div>
        <div class="perf-stat"><div class="label">Win Rate</div><div class="value">${((s.win_rate||0)*100).toFixed(1)}%</div></div>
        <div class="perf-stat"><div class="label">Wins / Losses</div><div class="value">${wins} / ${total-wins}</div></div>
        <div class="perf-stat"><div class="label">Avg P&L</div><div class="value ${avg>=0?"pnl-pos":"pnl-neg"}">${fmtSigned(avg)}</div></div>
        <div class="perf-stat"><div class="label">Best / Worst</div><div class="value"><span class="pnl-pos">${fmtSigned(best)}</span> / <span class="pnl-neg">${fmtSigned(worst)}</span></div></div>
      `;
    }

    const groupedEl = $("trades-grouped");
    if (!groupedEl) return;
    const trades = data.trades || [];
    if (!trades.length) {
      groupedEl.innerHTML = `<div class="muted">No trades found.</div>`;
      return;
    }
    // Group by day
    const groups = {};
    trades.forEach(t => {
      const day = (t.exit_ts || t.entry_ts || "").slice(0,10) || "Unknown";
      if (!groups[day]) groups[day] = [];
      groups[day].push(t);
    });
    groupedEl.innerHTML = Object.entries(groups).sort((a,b)=>b[0].localeCompare(a[0])).map(([day, dayTrades]) => {
      const dayPnl = dayTrades.reduce((s,t)=>s+(t.pnl||0),0);
      return `<div class="trade-group">
        <div class="trade-group-head">📅 ${day} — ${dayTrades.length} trades — <span class="${dayPnl>=0?"pnl-pos":"pnl-neg"}">${fmtSigned(dayPnl)}</span></div>
        <div class="trade-group-body">
          ${dayTrades.map(t => {
            const icon = t.pnl >= 0 ? "✅" : "❌";
            const kindIcon = t.kind === "option" ? "📝" : "📈";
            return `<div class="trade-row">
              <span>${icon} ${kindIcon}</span>
              <span><strong>${t.symbol||t.underlying||""}</strong> <span class="muted">${t.runner_name||""}</span></span>
              <span class="${t.side==="BUY"?"pnl-pos":"pnl-neg"}">${t.side||""}</span>
              <span class="num">${t.qty||""}</span>
              <span class="num ${t.pnl>=0?"pnl-pos":"pnl-neg"}">${fmtSigned(t.pnl||0)}</span>
            </div>`;
          }).join("")}
        </div>
      </div>`;
    }).join("");
  }

  function bind() {
    $("risk-page-mode")?.addEventListener("change", () => { loadRisk(); loadTrades(); });
    $("risk-page-refresh")?.addEventListener("click", () => { loadRisk(); loadConfig(); loadAudit(); loadTrades(); });
    $("risk-page-export")?.addEventListener("click", () => {
      if (currentRisk) exportCSV([currentRisk.current], "risk_dashboard.csv");
    });

    // Tabs
    document.querySelectorAll("#risk-page .tab").forEach(tab => {
      tab.addEventListener("click", () => {
        document.querySelectorAll("#risk-page .tab").forEach(t => t.classList.remove("active"));
        tab.classList.add("active");
        document.querySelectorAll("#risk-page .tab-panel").forEach(p => p.hidden = true);
        const target = $("tab-" + tab.dataset.tab);
        if (target) target.hidden = false;
        if (tab.dataset.tab === "audit") loadAudit();
        if (tab.dataset.tab === "exposure") { if (currentRisk) renderExposure(currentRisk); }
        if (tab.dataset.tab === "trades") loadTrades();
        if (tab.dataset.tab === "config") loadConfig();
      });
    });

    // Config presets
    document.querySelectorAll("#risk-presets [data-preset]").forEach(btn => {
      btn.addEventListener("click", () => applyPreset(btn.dataset.preset));
    });
    $("btn-live-mode")?.addEventListener("click", () => {
      document.querySelectorAll("#risk-presets button").forEach(b => b.classList.remove("risk-preset-active"));
      $("btn-live-mode").classList.add("risk-preset-active");
      // Live mode = conservative + flatten-and-halt
      applyPreset("conservative");
      const bm = $("cfg-breach-mode");
      if (bm) bm.value = "FLATTEN_AND_HALT";
    });
    $("btn-save-global")?.addEventListener("click", saveGlobal);
    $("btn-save-buckets")?.addEventListener("click", saveBuckets);

    // Audit
    $("btn-audit-refresh")?.addEventListener("click", loadAudit);
    $("audit-scope")?.addEventListener("change", loadAudit);
    $("audit-type")?.addEventListener("change", loadAudit);
    $("audit-search")?.addEventListener("input", () => { clearTimeout(window._auditSearchT); window._auditSearchT = setTimeout(loadAudit, 400); });
    $("btn-audit-export")?.addEventListener("click", () => exportCSV(auditData, "audit_timeline.csv"));

    // Trades
    $("btn-trades-refresh")?.addEventListener("click", loadTrades);
    $("trades-kind")?.addEventListener("change", loadTrades);
    $("trades-limit")?.addEventListener("change", loadTrades);
    $("btn-trades-export")?.addEventListener("click", async () => {
      const kind = $("trades-kind")?.value || "all";
      const limit = $("trades-limit")?.value || "100";
      try {
        const data = await fetchJSON(`/api/portfolio/trades/aggregated?kind=${kind}&limit=${limit}`);
        exportCSV(data.trades || [], "trade_history.csv");
      } catch(e) { if (window.showToast) showToast(e.message,"error"); }
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    if (!$("risk-page")) return;
    bind();
    loadRisk();
    loadConfig();
    // Auto-refresh dashboard every 2s
    setInterval(() => {
      const activeTab = document.querySelector("#risk-page .tab.active")?.dataset.tab;
      if (activeTab === "config" || !activeTab) loadRisk();
    }, 2000);
  });

  window.RiskPage = { loadRisk, loadConfig, loadAudit, loadTrades };
})();
