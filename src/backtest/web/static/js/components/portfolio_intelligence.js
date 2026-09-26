/* Portfolio Intelligence — Risk Board sections.
 *
 * Collapsible sections (state in localStorage) inside the Risk Board tab:
 *   #pi-greeks         net Δ/Γ/Vega/Θ, per-strategy breakdown, scenarios   (1 s)
 *   #pi-concentration  by underlying (vs 60% line) + strike clustering    (1 s)
 *   #pi-correlation    strategy × strategy heatmap (server-cached 5 min)  (30 s)
 *   #pi-regime         VIX regime, history, strategy fit, "Set VIX"        (30 s)
 *   #pi-activity       OI anomalies + spread dry-ups                        (30 s)
 *
 * Polls only while the Risk Board tab is open and the page is visible.
 * Also owns the deep link  /portfolio?tab=risk#pi-<section>  used by alerts.
 * Read-only: nothing here trades.
 */
(function () {
  "use strict";

  const FAST_MS = 1000;
  const SLOW_MS = 30000;
  const LS_PREFIX = "pi.section.";
  const $ = (id) => document.getElementById(id);
  const state = { greeks: null, concentration: null, correlation: null, regime: null,
                  activity: null, lastSlow: 0, busyFast: false, busySlow: false,
                  timer: null, chart: null, groupBy: "runner" };

  const REGIME_BADGE = { low_vol: "pi-regime-low", moderate_vol: "pi-regime-moderate",
                         high_vol: "pi-regime-high", unknown: "pi-regime-unknown" };

  function esc(v) {
    return String(v == null ? "" : v)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  function num(n, dp) {
    if (n == null || isNaN(n)) return "—";
    return Number(n).toLocaleString("en-IN", { maximumFractionDigits: dp == null ? 1 : dp });
  }
  function money(n, signed) {
    if (n == null || isNaN(n)) return "—";
    if (typeof Money !== "undefined") {
      if (signed && Money.signed) return Money.signed(n);
      if (!signed && Money.format) return Money.format(n);
    }
    const v = Math.round(Number(n));
    const c = (document.body && document.body.dataset.currencySymbol) || "₹";
    const sign = signed ? (v >= 0 ? "+" : "-") : (v < 0 ? "-" : "");
    return sign + c + Math.abs(v).toLocaleString("en-IN");
  }
  const pnlClass = (n) => (n > 0 ? "pnl-pos" : n < 0 ? "pnl-neg" : "pnl-flat");

  function mode() {
    const page = $("portfolio-page");
    const pageMode = page && page.dataset ? page.dataset.mode || "" : "";
    if (pageMode) return pageMode;
    const sel = $("risk-mode");
    return sel ? sel.value || "" : "";
  }
  const q = (extra) => {
    const m = mode();
    const parts = [];
    if (m) parts.push("mode=" + encodeURIComponent(m));
    if (extra) parts.push(extra);
    return parts.length ? "?" + parts.join("&") : "";
  };

  async function api(url, method, body) {
    const opts = { method: method || "GET", headers: { "Content-Type": "application/json" } };
    if (body) opts.body = JSON.stringify(body);
    const res = await fetch(url, opts);
    let data = {};
    try { data = await res.json(); } catch (_) { data = {}; }
    if (!res.ok || data.success === false) throw new Error(data.error || "HTTP " + res.status);
    return data;
  }
  function toast(msg, kind) {
    if (window.showToast) return window.showToast(msg, kind);
    if (console && console.log) console.log("[pi]", msg);
  }

  // ------------------------------------------------------------- rendering

  function greekCard(label, value, sub, cls, hint) {
    return `<div class="pi-card ${cls || ""}" title="${esc(hint || "")}">
      <div class="pi-card-label">${esc(label)}</div>
      <div class="pi-card-value">${value}</div>
      <div class="pi-card-sub muted">${sub}</div></div>`;
  }

  function renderGreeks(g, thresholds) {
    if (!g) return;
    const th = thresholds || {};
    const gammaLimit = th.gamma_critical != null ? th.gamma_critical : null;
    const deltaLimit = th.delta_warning_abs != null ? th.delta_warning_abs : null;
    const bias = g.bias || {};
    const gammaBreach = gammaLimit != null && g.net_gamma < gammaLimit;
    const deltaBreach = deltaLimit != null && Math.abs(g.net_delta) > deltaLimit;
    const cards = $("pi-greeks-cards");
    if (cards) {
      cards.innerHTML = [
        greekCard("Net Delta", num(g.net_delta, 0),
          `${esc(bias.delta || "")} · ${money(g.delta_rupees_1pct, true)} per 1%` +
          (deltaLimit != null ? ` · limit ±${num(deltaLimit, 0)}` : ""),
          deltaBreach ? "pi-card-warn" : "", g.units && g.units.net_delta),
        greekCard("Net Gamma", num(g.net_gamma, 1),
          `${esc(bias.gamma || "")} · ${money(g.gamma_rupees_1pct, true)} Γ-P&L per 1%` +
          (gammaLimit != null ? ` · alert < ${num(gammaLimit, 0)}` : ""),
          gammaBreach ? "pi-card-crit" : "", g.units && g.units.net_gamma),
        greekCard("Net Vega", money(g.net_vega, true),
          `${esc(bias.vega || "")} · per +1 IV pt`, "", g.units && g.units.net_vega),
        greekCard("Net Theta", money(g.net_theta, true),
          `${esc(bias.theta || "")} · per day`, "", g.units && g.units.net_theta),
      ].join("");
    }
    const warn = $("pi-greeks-warnings");
    if (warn) {
      const list = (g.warnings || []).map((w) => `<div class="pi-warning">⚠ ${esc(w)}</div>`);
      warn.innerHTML = list.join("");
    }
    const tbody = $("pi-greeks-breakdown");
    if (tbody) {
      const rows = g.breakdown_by_strategy || [];
      tbody.innerHTML = rows.length ? rows.map((r) => `
        <tr class="${r.stale ? "pi-row-stale" : ""}">
          <td>${esc(r.label)} <span class="muted">${esc(r.mode || "")}</span></td>
          <td>${esc(r.strategy)}</td>
          <td class="num">${num(r.delta, 0)}</td>
          <td class="num ${r.gamma < 0 ? "pnl-neg" : ""}">${num(r.gamma, 1)}</td>
          <td class="num">${money(r.vega, true)}</td>
          <td class="num ${pnlClass(r.theta)}">${money(r.theta, true)}</td>
          <td class="num">${esc(r.positions)}</td>
          <td class="num">${r.gamma_share != null ? num(r.gamma_share * 100, 0) + "%" : "—"}</td>
        </tr>`).join("")
        : `<tr><td colspan="8" class="muted">No open positions — nothing to aggregate.</td></tr>`;
    }
    const sc = $("pi-greeks-scenarios");
    if (sc) {
      const rows = g.scenario_list || [];
      sc.innerHTML = rows.length ? rows.map((r) =>
        `<tr><td>${esc(r.label)}</td><td class="num ${pnlClass(r.pnl)}">${money(r.pnl, true)}</td></tr>`).join("")
        : `<tr><td colspan="2" class="muted">No scenarios configured.</td></tr>`;
    }
    const summary = $("pi-greeks-summary");
    if (summary) {
      summary.textContent = g.positions
        ? `Δ ${num(g.net_delta, 0)} · Γ ${num(g.net_gamma, 1)} · Θ ${money(g.net_theta, true)}/day`
        : "flat";
    }
    const foot = $("pi-greeks-foot");
    if (foot) {
      foot.textContent = `${g.positions || 0} positions · ${g.legs_priced || 0}/${g.legs || 0} legs priced · ` +
        `model ${g.model || "—"} · computed in ${num(g.compute_ms, 1)} ms · refreshed ${new Date().toLocaleTimeString()}`;
    }
  }

  function renderConcentration(c) {
    if (!c) return;
    const th = c.thresholds || {};
    const thEl = $("pi-conc-threshold");
    if (thEl) thEl.textContent = th.max_pct != null ? `(alert above ${num(th.max_pct, 0)}%)` : "";
    const u = $("pi-conc-underlying");
    if (u) {
      const entries = Object.entries(c.by_underlying || {});
      u.innerHTML = entries.length ? entries.map(([name, r]) => `
        <div class="pi-bar-row ${r.high ? "pi-bar-high" : ""}">
          <div class="pi-bar-label">${esc(name)} <span class="muted">${esc(r.positions)} pos</span></div>
          <div class="pi-bar"><div class="pi-bar-fill" style="width:${Math.min(100, r.pct || 0).toFixed(1)}%"></div>
            <div class="pi-bar-limit" style="left:${Math.min(100, th.max_pct || 60)}%"></div></div>
          <div class="pi-bar-value">${num(r.pct, 1)}%${r.high ? " ⚠" : ""}</div>
        </div>`).join("")
        : `<div class="muted">No exposure.</div>`;
    }
    const s = $("pi-conc-strikes");
    if (s) {
      const entries = Object.values(c.by_strike || {});
      s.innerHTML = entries.length ? entries.slice(0, 50).map((r) => `
        <tr class="${r.clustered ? "pi-row-flag" : ""}">
          <td>${esc(r.underlying)} ${num(r.strike, 0)}${r.clustered ? " ⚠" : ""}</td>
          <td class="num">${esc(r.positions)}</td>
          <td>${esc((r.option_types || []).join("/"))}</td>
          <td>${esc((r.sources || []).join(", "))}</td>
          <td class="num">${money(r.exposure)}</td></tr>`).join("")
        : `<tr><td colspan="5" class="muted">No option strikes held.</td></tr>`;
    }
    const summary = $("pi-concentration-summary");
    if (summary) {
      const top = Object.entries(c.by_underlying || {})[0];
      summary.textContent = top ? `${top[0]} ${num(top[1].pct, 0)}% of gross` : "flat";
    }
    const foot = $("pi-concentration-foot");
    if (foot) foot.textContent = `Measure: ${c.measure || "gross notional"} · total ${money(c.total_exposure)}`;
  }

  function corrColor(v) {
    if (v == null) return "transparent";
    const a = Math.min(1, Math.abs(v));
    return v >= 0 ? `rgba(239,68,68,${(a * 0.75).toFixed(2)})` : `rgba(59,130,246,${(a * 0.75).toFixed(2)})`;
  }

  function renderCorrelation(c) {
    if (!c) return;
    const el = $("pi-corr-heatmap");
    const labels = c.labels || [];
    const values = c.values || [];
    if (el) {
      if (labels.length < 2) {
        el.innerHTML = `<div class="muted">Correlation needs at least two strategy instances.</div>`;
      } else {
        const head = `<tr><th></th>${labels.map((l) => `<th class="pi-corr-h">${esc(l)}</th>`).join("")}</tr>`;
        const body = labels.map((l, i) => `<tr><th>${esc(l)}</th>${labels.map((_, j) => {
          const v = values[i] ? values[i][j] : null;
          const hot = i !== j && v != null && Math.abs(v) >= (c.threshold || 0.8);
          return `<td class="pi-corr-cell ${hot ? "pi-corr-hot" : ""}" style="background:${corrColor(v)}"
                    title="${esc(l)} vs ${esc(labels[j])}">${v == null ? "·" : num(v, 2)}</td>`;
        }).join("")}</tr>`).join("");
        el.innerHTML = `<table class="pi-corr-table"><thead>${head}</thead><tbody>${body}</tbody></table>`;
      }
    }
    const summary = $("pi-correlation-summary");
    if (summary) {
      const n = (c.alerts || []).length;
      summary.textContent = n ? `${n} pair${n === 1 ? "" : "s"} above ${num(c.threshold, 2)}` : "no hot pairs";
    }
    const foot = $("pi-correlation-foot");
    if (foot) {
      foot.textContent = `${c.method || ""} · needs ≥${c.min_samples} aligned samples (window ${c.window}) · ` +
        `"·" = not enough data yet · ${c.cached ? "cached" : "fresh"}`;
    }
  }

  function renderRegime(r) {
    if (!r) return;
    const cur = $("pi-regime-current");
    if (cur) {
      const cls = REGIME_BADGE[r.regime] || "pi-regime-unknown";
      const change = r.vix_change_pct != null
        ? ` <span class="${r.vix_change_pct > 0 ? "pnl-neg" : "pnl-pos"}">${r.vix_change_pct > 0 ? "+" : ""}${num(r.vix_change_pct, 1)}% vs 24h</span>`
        : "";
      const proxy = r.is_proxy
        ? `<div class="pi-note">ℹ️ No India VIX reading — showing the <strong>realized volatility of ${esc((r.source || "").split(":")[1] || "the benchmark")}</strong>
             as a proxy.${r.feed_synthetic ? " On the synthetic feed this number is illustrative only (synthetic bars are far noisier than NIFTY)." : ""}
             Enter a VIX value below or feed an <code>INDIAVIX</code> symbol for the real regime.</div>`
        : "";
      const bands = r.bands || {};
      cur.innerHTML = `
        <div class="pi-regime-row">
          <span class="pi-regime-badge ${cls}">${esc(r.label || r.regime)}</span>
          <span class="pi-regime-vix">${r.vix != null && r.regime !== "unknown" ? "VIX " + num(r.vix, 2) : "VIX —"}</span>${change}
          ${r.transitioning ? `<span class="pi-regime-trans">⚡ transitioning</span>` : ""}
        </div>
        <div class="muted">Source: ${esc(r.source || "none")} · bands: low &lt; ${num(bands.low_max, 0)} ≤ moderate &lt; ${num(bands.high_min, 0)} ≤ high
          ${r.regime_since ? " · since " + esc(new Date(r.regime_since).toLocaleString()) : ""}</div>
        ${proxy}`;
    }
    const fit = $("pi-regime-fit");
    if (fit) {
      const rows = r.strategy_fit || [];
      const icon = { favorable: "✅", unfavorable: "⚠️", any: "➖", unknown: "❔" };
      fit.innerHTML = rows.length ? rows.map((f) => `
        <tr class="${f.status === "unfavorable" ? "pi-row-flag" : ""}">
          <td>${esc(f.runner)}</td><td>${esc(f.strategy)}</td>
          <td>${f.range ? "VIX " + num(f.range[0], 0) + "–" + num(f.range[1], 0) : "any"}</td>
          <td>${icon[f.status] || ""} ${esc(f.status)}</td></tr>`).join("")
        : `<tr><td colspan="4" class="muted">No runners.</td></tr>`;
    }
    const summary = $("pi-regime-summary");
    if (summary) summary.textContent = `${r.label || r.regime}${r.vix != null && r.regime !== "unknown" ? " · " + num(r.vix, 1) : ""}${r.is_proxy ? " (proxy)" : ""}`;
    const foot = $("pi-regime-foot");
    if (foot) foot.textContent = `${(r.history || []).length} samples (every 5 min) · strategies declare their range via regime_vix_range`;
    renderRegimeChart(r);
  }

  function renderRegimeChart(r) {
    const canvas = $("pi-regime-chart");
    if (!canvas || typeof Chart === "undefined") return;
    const hist = (r.history || []).slice(-288);
    const labels = hist.map((h) => (h.ts || "").slice(11, 16));
    const data = hist.map((h) => h.value);
    if (state.chart) {
      state.chart.data.labels = labels;
      state.chart.data.datasets[0].data = data;
      state.chart.update("none");
      return;
    }
    try {
      state.chart = new Chart(canvas.getContext("2d"), {
        type: "line",
        data: { labels, datasets: [{ label: "VIX / vol", data, borderColor: "#8b5cf6", pointRadius: 0, borderWidth: 1.5, tension: 0.2 }] },
        options: { animation: false, responsive: true, plugins: { legend: { display: false } },
                   scales: { x: { ticks: { color: "#94a3b8", maxTicksLimit: 6 } }, y: { ticks: { color: "#94a3b8" } } } },
      });
    } catch (_) { /* chart is decoration */ }
  }

  function renderActivity(a) {
    if (!a) return;
    const el = $("pi-activity-content");
    if (el) {
      const an = a.anomalies || [];
      const lq = a.liquidity || [];
      const note = a.note ? `<div class="pi-note">ℹ️ ${esc(a.note)}</div>` : "";
      const anRows = an.length ? an.slice(0, 20).map((x) => `<tr><td>${esc(x.symbol)}</td><td class="num">${num(x.oi_change, 0)}</td>
          <td class="num">${num(x.multiplier, 1)}×</td><td class="muted">${esc((x.timestamp || "").slice(0, 16))}</td></tr>`).join("")
        : `<tr><td colspan="4" class="muted">No OI anomalies.</td></tr>`;
      const lqRows = lq.length ? lq.slice(0, 20).map((x) => `<tr><td>${esc(x.symbol)}</td><td class="num">${num(x.spread, 2)}</td>
          <td class="num">${num(x.multiplier, 1)}×</td><td class="muted">${esc((x.timestamp || "").slice(0, 16))}</td></tr>`).join("")
        : `<tr><td colspan="4" class="muted">No spread dry-ups.</td></tr>`;
      el.innerHTML = `${note}<div class="pi-two-col">
        <div><h5 class="pi-subhead">OI anomalies (&gt; ${num((a.thresholds || {}).oi_multiplier, 0)}× normal)</h5>
          <table class="matrix-table pi-table"><thead><tr><th>Contract</th><th class="num">ΔOI</th><th class="num">×</th><th>When</th></tr></thead><tbody>${anRows}</tbody></table></div>
        <div><h5 class="pi-subhead">Liquidity (spread &gt; ${num((a.thresholds || {}).spread_multiplier, 0)}× avg)</h5>
          <table class="matrix-table pi-table"><thead><tr><th>Contract</th><th class="num">Spread</th><th class="num">×</th><th>When</th></tr></thead><tbody>${lqRows}</tbody></table></div>
      </div>`;
    }
    const summary = $("pi-activity-summary");
    if (summary) summary.textContent = a.has_oi_data ? `${(a.anomalies || []).length} OI · ${(a.liquidity || []).length} spread events` : "no chain data";
  }

  // --------------------------------------------------------------- polling

  function riskTabOpen() {
    const panel = $("tab-risk");
    if (!panel || panel.hidden) return false;
    return !(typeof document.hidden === "boolean" && document.hidden);
  }

  async function refreshFast() {
    if (state.busyFast) return;
    state.busyFast = true;
    try {
      const [g, c] = await Promise.all([
        api("/api/portfolio/greeks" + q()),
        api("/api/portfolio/concentration" + q()),
      ]);
      state.greeks = g;
      state.concentration = c;
      renderGreeks(g, state.thresholds);
      renderConcentration(c);
    } catch (e) {
      const w = $("pi-greeks-warnings");
      if (w) w.innerHTML = `<div class="pi-warning">⚠ Greeks unavailable: ${esc(e.message)}</div>`;
    } finally {
      state.busyFast = false;
    }
  }

  async function refreshSlow(force) {
    if (state.busySlow) return;
    state.busySlow = true;
    try {
      const [overview, corr] = await Promise.all([
        api("/api/portfolio/intelligence" + q()),
        api("/api/portfolio/correlation" + q("group_by=" + state.groupBy + (force ? "&refresh=1" : ""))),
      ]);
      state.thresholds = overview.thresholds;
      state.regime = overview.regime;
      state.activity = overview.market_activity;
      state.correlation = corr;
      renderRegime(overview.regime);
      renderActivity(overview.market_activity);
      renderCorrelation(corr);
      if (!state.greeks) { renderGreeks(overview.greeks, overview.thresholds); renderConcentration(overview.concentration); }
      state.lastSlow = Date.now();
    } catch (e) {
      toast("Portfolio intelligence failed: " + e.message, "error");
    } finally {
      state.busySlow = false;
    }
  }

  function tick() {
    if (!riskTabOpen()) return;
    refreshFast();
    if (Date.now() - state.lastSlow >= SLOW_MS) refreshSlow(false);
  }

  function refreshAll() {
    state.lastSlow = 0;
    tick();
  }

  // -------------------------------------------------------------- sections

  function setSection(name, open) {
    const section = document.querySelector ? document.querySelector(`[data-pi-section="${name}"]`) : null;
    if (!section) return;
    section.classList[open ? "remove" : "add"]("pi-collapsed");
    const btn = section.querySelector("[data-pi-toggle]");
    if (btn && btn.setAttribute) btn.setAttribute("aria-expanded", open ? "true" : "false");
    try { localStorage.setItem(LS_PREFIX + name, open ? "1" : "0"); } catch (_) { /* private mode */ }
  }

  function restoreSections() {
    if (!document.querySelectorAll) return;
    document.querySelectorAll("[data-pi-section]").forEach((s) => {
      const name = s.dataset.piSection;
      let saved = null;
      try { saved = localStorage.getItem(LS_PREFIX + name); } catch (_) { saved = null; }
      const btn = s.querySelector("[data-pi-toggle]");
      const dflt = !btn || btn.getAttribute("aria-expanded") !== "false";
      setSection(name, saved == null ? dflt : saved === "1");
    });
  }

  function openFromHash() {
    const params = new URLSearchParams(window.location.search || "");
    const tab = params.get("tab");
    if (tab && document.querySelector) {
      const btn = document.querySelector(`.tab[data-tab="${tab}"]`);
      if (btn && btn.click) btn.click();
    }
    const hash = (window.location.hash || "").replace(/^#/, "");
    if (hash && hash.indexOf("pi-") === 0) {
      const el = $(hash);
      if (el) {
        setSection(hash.slice(3), true);
        if (el.classList) el.classList.add("pi-flash");
        setTimeout(() => {
          if (el.scrollIntoView) el.scrollIntoView({ behavior: "smooth", block: "start" });
          setTimeout(() => el.classList && el.classList.remove("pi-flash"), 2500);
        }, 50);
      }
    }
  }

  async function submitVix() {
    const input = $("pi-vix-value");
    const value = input ? parseFloat(input.value) : NaN;
    if (!(value > 0)) { toast("Enter a VIX level, e.g. 18.4", "warning"); return; }
    try {
      const snap = await api("/api/market/vix", "POST", { value, source: "manual" });
      renderRegime(Object.assign({}, state.regime || {}, snap));
      toast(`VIX ${value} applied — regime ${snap.label || snap.regime}`, "success");
      refreshSlow(false);
    } catch (e) {
      toast("VIX update failed: " + e.message, "error");
    }
  }

  function bind() {
    if (document.querySelectorAll) {
      document.querySelectorAll("[data-pi-toggle]").forEach((btn) =>
        btn.addEventListener("click", () => {
          const name = btn.dataset.piToggle;
          const section = btn.closest ? btn.closest("[data-pi-section]") : null;
          const open = section ? section.classList.contains("pi-collapsed") : true;
          setSection(name, open);
        }));
      document.querySelectorAll('.tab[data-tab="risk"]').forEach((t) =>
        t.addEventListener("click", () => setTimeout(refreshAll, 0)));
    }
    const grp = $("pi-corr-group");
    if (grp) grp.addEventListener("change", (e) => { state.groupBy = e.target.value; refreshSlow(false); });
    const corrBtn = $("pi-corr-refresh");
    if (corrBtn) corrBtn.addEventListener("click", () => refreshSlow(true));
    const vixBtn = $("pi-vix-submit");
    if (vixBtn) vixBtn.addEventListener("click", submitVix);
    const modeSel = $("risk-mode");
    if (modeSel) modeSel.addEventListener("change", refreshAll);
    const refreshBtn = $("btn-risk-refresh");
    if (refreshBtn) refreshBtn.addEventListener("click", refreshAll);
    if (window.addEventListener) window.addEventListener("hashchange", openFromHash);
  }

  function init() {
    if (!$("pi-root")) return;
    restoreSections();
    bind();
    openFromHash();
    if (state.timer) clearInterval(state.timer);
    state.timer = setInterval(tick, FAST_MS);
    tick();
  }

  window.PortfolioIntelligence = {
    init, refreshAll, refreshFast, refreshSlow, tick, setSection,
    renderGreeks, renderConcentration, renderCorrelation, renderRegime, renderActivity,
    corrColor, getState: () => state,
  };

  if (!window.__PI_NO_AUTOINIT__) {
    if (document.readyState === "loading" && document.addEventListener) {
      document.addEventListener("DOMContentLoaded", init);
    } else {
      init();
    }
  }
})();
