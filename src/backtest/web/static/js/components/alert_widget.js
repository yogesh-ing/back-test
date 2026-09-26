/* Portfolio Intelligence — persistent alert widget + alert detail modal.
 *
 * Bottom-right on every page. Minimized: count + severity breakdown.
 * Expanded: the open alerts, newest-most-severe first, each with
 * "View Details" and "Dismiss". The modal explains the alert (current state,
 * what it means, contributing strategies, typical responses, who is
 * subscribed) and deep-links to the Risk Board section.
 *
 * Information only: there is deliberately no "close position" button here.
 * The platform informs; strategies decide; the trader overrides from the
 * normal position controls.
 *
 * Data: GET /api/alerts/active (polled; re-rendered only when the broker's
 * version changes), GET /api/alerts/<id>, POST /api/alerts/<id>/dismiss|review.
 */
(function () {
  "use strict";

  const POLL_MS = 3000;
  const HIDDEN_POLL_MS = 15000;
  const LS_EXPANDED = "pi.alertWidget.expanded";
  const ICON = { critical: "🔴", warning: "🟡", info: "🔵" };
  const RANK = { critical: 2, warning: 1, info: 0 };
  const state = { alerts: [], counts: { total: 0, critical: 0, warning: 0, info: 0 },
                  version: -1, expanded: false, seen: new Set(), primed: false,
                  timer: null, detail: null, error: null };

  const $ = (id) => document.getElementById(id);

  function esc(v) {
    return String(v == null ? "" : v)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function money(n) {
    if (n == null || isNaN(n)) return "—";
    if (typeof Money !== "undefined" && Money.signed) return Money.signed(n);
    const v = Math.round(Number(n));
    const c = (document.body && document.body.dataset.currencySymbol) || "₹";
    return (v >= 0 ? "+" : "-") + c + Math.abs(v).toLocaleString("en-IN");
  }

  function num(n, dp) {
    if (n == null || isNaN(n)) return "—";
    return Number(n).toLocaleString("en-IN", { maximumFractionDigits: dp == null ? 1 : dp,
                                               minimumFractionDigits: 0 });
  }

  function ago(iso, now) {
    if (!iso) return "";
    const t = Date.parse(iso);
    if (isNaN(t)) return "";
    const s = Math.max(0, Math.round(((now || Date.now()) - t) / 1000));
    if (s < 60) return s + "s ago";
    if (s < 3600) return Math.floor(s / 60) + " min ago";
    if (s < 86400) return Math.floor(s / 3600) + " h ago";
    return Math.floor(s / 86400) + " d ago";
  }

  function sortAlerts(list) {
    return (list || []).slice().sort((a, b) =>
      (RANK[b.severity] || 0) - (RANK[a.severity] || 0) ||
      String(b.created_at || "").localeCompare(String(a.created_at || "")));
  }

  function deepLink(alert) {
    const section = (alert && alert.section) || "pi-greeks";
    return "/portfolio?tab=risk#" + encodeURIComponent(section);
  }

  // ------------------------------------------------------------------ render

  function renderMinimized(counts) {
    const c = counts || {};
    if (!c.total) {
      return `<button class="aw-pill aw-pill-clear" type="button" data-aw="toggle"
                title="No open portfolio alerts">🔔 <span class="aw-pill-text">No alerts</span></button>`;
    }
    const parts = ["critical", "warning", "info"]
      .filter((k) => c[k])
      .map((k) => `<span class="aw-sev aw-sev-${k}">${ICON[k]} ${c[k]}</span>`)
      .join(" ");
    const worst = c.critical ? "critical" : c.warning ? "warning" : "info";
    return `<button class="aw-pill aw-pill-${worst}" type="button" data-aw="toggle"
              title="Open the alert list">🔔 <strong class="aw-count">${c.total}</strong>
              <span class="aw-pill-text">alert${c.total === 1 ? "" : "s"}</span> ${parts}</button>`;
  }

  function renderItem(a, now) {
    const sev = a.severity || "info";
    const summary = a.data && a.data.summary ? `<div class="aw-item-summary muted">${esc(a.data.summary)}</div>` : "";
    return `
      <li class="aw-item aw-item-${esc(sev)}" data-alert-id="${esc(a.alert_id)}">
        <div class="aw-item-head">
          <span class="aw-item-icon">${ICON[sev] || "•"}</span>
          <span class="aw-item-title">${esc(a.title || a.alert_type)}</span>
          <span class="aw-item-time muted">${esc(ago(a.created_at, now))}</span>
        </div>
        <div class="aw-item-msg">${esc(a.message)}</div>
        ${summary}
        <div class="aw-item-actions">
          <button class="btn btn-ghost btn-small" type="button" data-aw="details" data-id="${esc(a.alert_id)}">View Details</button>
          <button class="btn btn-ghost btn-small" type="button" data-aw="dismiss" data-id="${esc(a.alert_id)}">Dismiss</button>
        </div>
      </li>`;
  }

  function renderExpanded(alerts, counts, now) {
    const list = sortAlerts(alerts);
    const body = list.length
      ? `<ul class="aw-list">${list.map((a) => renderItem(a, now)).join("")}</ul>`
      : `<div class="aw-empty muted">No open alerts. The platform is watching Greeks, concentration,
           correlation, regime and feed health.</div>`;
    return `
      <div class="aw-panel" role="dialog" aria-label="Portfolio alerts">
        <div class="aw-panel-head">
          <strong>🔔 Portfolio Alerts (${(counts && counts.total) || 0})</strong>
          <span class="aw-panel-links">
            <a href="/portfolio?tab=risk#pi-greeks" class="aw-link">Risk Board</a>
            <button class="aw-min" type="button" data-aw="toggle" title="Minimize">▁</button>
          </span>
        </div>
        ${state.error ? `<div class="aw-error">⚠ ${esc(state.error)}</div>` : ""}
        ${body}
        <div class="aw-panel-foot muted">Information only — strategies decide, you override.</div>
      </div>`;
  }

  function render() {
    const root = $("alert-widget");
    if (!root) return;
    root.dataset.state = state.expanded ? "expanded" : "minimized";
    root.innerHTML = state.expanded
      ? renderExpanded(state.alerts, state.counts)
      : renderMinimized(state.counts);
  }

  // ------------------------------------------------------------- detail modal

  function metricRows(a) {
    const d = (a && a.data) || {};
    const rows = [];
    const add = (k, v) => { if (v !== undefined && v !== null && v !== "") rows.push([k, v]); };
    switch (a.alert_type) {
      case "portfolio_gamma_critical":
        add("Net gamma (Δ per 1% move)", num(d.net_gamma, 1));
        add("Threshold", num(d.threshold, 0));
        add("Breach", d.breach_pct != null ? num(d.breach_pct, 0) + "% beyond threshold" : null);
        add("Gamma in ₹ (1% move)", money(d.gamma_rupees_1pct));
        add("Worst P&L on a 1% move", money(d.move_1pct_pnl));
        add("Worst P&L on a 2% move", money(d.move_2pct_pnl));
        add("Theta collected / day", money(d.net_theta));
        break;
      case "portfolio_delta_warning":
        add("Net delta (share-equivalent)", num(d.net_delta, 0));
        add("Limit", "±" + num(d.threshold, 0));
        add("P&L per 1% move", money(d.delta_rupees_1pct));
        break;
      case "concentration_high":
        add("Underlying", d.underlying);
        add("Share of gross exposure", num(d.pct, 1) + "%");
        add("Max recommended", num(d.threshold_pct, 0) + "%");
        add("Exposure", money(d.exposure).replace("+", ""));
        add("Portfolio gross", money(d.total_exposure).replace("+", ""));
        break;
      case "strike_clustering":
        add("Strike", `${d.underlying || ""} ${d.strike != null ? num(d.strike, 0) : ""}`);
        add("Positions at this strike", d.positions);
        add("Clustering threshold", d.threshold);
        add("Held by", (d.sources || []).join(", "));
        break;
      case "vix_regime_change":
        add("Transition", `${d.old_regime || "?"} → ${d.new_regime || "?"}`);
        add("VIX", num(d.vix, 2));
        add("Source", d.is_proxy ? `${d.source} (realized-vol proxy — not India VIX)` : d.source);
        add("Unfavorable for", (d.affected || []).map((x) => x.label).join(", ") || "none");
        break;
      case "oi_anomaly":
        add("Contract", d.symbol);
        add("OI change", num(d.oi_change, 0));
        add("Multiple of normal", num(d.multiplier, 1) + "×");
        add("Average |ΔOI|", num(d.avg_change, 0));
        add("You hold this strike", d.held ? "yes" : "no");
        break;
      case "correlation_spike":
        add("Pair", `${d.strategy_a} ↔ ${d.strategy_b}`);
        add("Correlation", num(d.correlation, 2));
        add("Threshold", num(d.threshold, 2));
        add("Aligned samples", d.samples);
        break;
      case "liquidity_dry_up":
        add("Contract", d.symbol);
        add("Spread", num(d.spread, 2));
        add("Average spread", num(d.avg_spread, 2));
        add("Multiple", num(d.multiplier, 1) + "×");
        add("You hold this strike", d.held ? "yes" : "no");
        break;
      case "data_feed_stale":
        add("Feed", d.source);
        add("Last bar", d.age_s != null ? num(d.age_s, 0) + " s ago" : null);
        add("Limit", d.threshold_s != null ? num(d.threshold_s, 0) + " s" : null);
        add("Symbols", (d.symbols || []).join(", "));
        add("Runners affected", (d.runners || []).join(", "));
        break;
      default:
        Object.keys(d).forEach((k) => {
          const v = d[k];
          if (v == null || typeof v === "object") return;
          add(k, v);
        });
    }
    return rows;
  }

  function contributorsTable(a) {
    const list = (a.data && (a.data.contributors || a.data.affected)) || [];
    if (!list.length) return `<div class="muted">No single strategy attribution for this alert.</div>`;
    const field = a.alert_type === "portfolio_delta_warning" ? "delta" : "gamma";
    const hasMetric = list.some((c) => c[field] != null);
    const head = hasMetric
      ? `<tr><th>Runner</th><th>Strategy</th><th>Bucket</th><th class="num">${field === "delta" ? "Δ" : "Γ"}</th><th class="num">Share</th><th class="num">Pos</th></tr>`
      : `<tr><th>Runner</th><th>Strategy</th><th>Bucket</th><th>Note</th></tr>`;
    const rows = list.map((c) => hasMetric
      ? `<tr><td>${esc(c.label)}</td><td>${esc(c.strategy)}</td><td>${esc(c.mode)}</td>
           <td class="num">${num(c[field], 1)}</td><td class="num">${c.share != null ? num(c.share * 100, 0) + "%" : "—"}</td>
           <td class="num">${esc(c.positions != null ? c.positions : "")}</td></tr>`
      : `<tr><td>${esc(c.label)}</td><td>${esc(c.strategy)}</td><td>${esc(c.mode)}</td><td>${esc(c.fit || "")}</td></tr>`).join("");
    return `<table class="matrix-table pi-table"><thead>${head}</thead><tbody>${rows}</tbody></table>`;
  }

  function subscriptionsBlock(a) {
    const subs = (a.subscriptions && a.subscriptions.subscribed) || [];
    const notSubs = (a.subscriptions && a.subscriptions.not_subscribed) || [];
    const notified = {};
    (a.notified_strategies || []).forEach((n) => { notified[n.subscriber_id] = n; });
    const subRows = subs.length
      ? subs.map((s) => {
          const n = notified[s.subscriber_id];
          const status = n ? (n.ok ? "✅ notified" : `⚠️ callback failed: ${esc(n.error || "")}`) : "⏳ not notified (cooldown / subscribed later)";
          return `<li>${esc(s.runner || s.subscriber_id)} <span class="muted">(${esc(s.strategy || "")})</span> — ${status}</li>`;
        }).join("")
      : `<li class="muted">No strategy subscribes to <code>${esc(a.alert_type)}</code>.</li>`;
    const notRows = notSubs.map((s) =>
      `<li>${esc(s.runner || s.instance_id)} <span class="muted">(${esc(s.strategy || "")})</span> — contributes but is not subscribed</li>`).join("");
    return `<ul class="pi-list">${subRows}${notRows}</ul>
      <div class="muted pi-note">Subscribed strategies decide for themselves (pause entries, request an exit, or
      ignore) — see <code>docs/STRATEGY-ALERTS.md</code>.</div>`;
  }

  function renderDetail(a) {
    const sev = a.severity || "info";
    const metrics = metricRows(a).map(([k, v]) =>
      `<tr><th>${esc(k)}</th><td>${esc(v)}</td></tr>`).join("");
    const responses = (a.typical_responses || []).map((r) => `<li>${esc(r)}</li>`).join("");
    const status = a.status && a.status !== "active" ? ` <span class="aw-status">${esc(a.status)}</span>` : "";
    return `
      <div class="modal pi-modal">
        <div class="modal-head">
          <h3>${ICON[sev] || ""} ${esc(a.title || a.alert_type)}${status}</h3>
          <button class="modal-close" type="button" data-aw="close-modal">✕</button>
        </div>
        <div class="modal-body pi-modal-body">
          <div class="aw-detail-msg aw-item-${esc(sev)}">${esc(a.message)}
            <div class="muted">Raised ${esc(ago(a.created_at))} · seen ${esc(a.occurrences || 1)}× · ${esc(sev.toUpperCase())}</div>
          </div>
          <h4>Current state</h4>
          <table class="pi-kv">${metrics || `<tr><td class="muted">No metrics recorded.</td></tr>`}</table>
          <h4>What this means</h4>
          <p>${esc(a.what_it_means || "")}</p>
          <h4>Contributing strategies</h4>
          ${contributorsTable(a)}
          <h4>Typical responses</h4>
          <ul class="pi-list">${responses}</ul>
          <div class="pi-note">ℹ️ The platform will <strong>not</strong> take automatic action. Adjust positions
          from the normal controls, or let subscribed strategies respond.</div>
          <h4>Strategy subscriptions</h4>
          ${subscriptionsBlock(a)}
        </div>
        <div class="modal-foot">
          <a class="btn btn-ghost" href="${deepLink(a)}" data-aw="deeplink">View in Risk Board →</a>
          <button class="btn btn-ghost" type="button" data-aw="review" data-id="${esc(a.alert_id)}">Mark as reviewed</button>
          <button class="btn btn-ghost" type="button" data-aw="dismiss" data-id="${esc(a.alert_id)}">Dismiss</button>
          <button class="btn btn-primary" type="button" data-aw="close-modal">Close</button>
        </div>
      </div>`;
  }

  function modalEl() {
    let m = $("alert-detail-modal");
    if (!m && document.createElement) {
      m = document.createElement("div");
      m.id = "alert-detail-modal";
      m.className = "modal-overlay";
      m.hidden = true;
      m.addEventListener("click", onClick);
      document.body.appendChild(m);
    }
    return m;
  }

  async function openDetail(id) {
    const m = modalEl();
    if (!m) return;
    try {
      const data = await api("/api/alerts/" + encodeURIComponent(id));
      state.detail = data.alert;
      m.innerHTML = renderDetail(data.alert);
      m.hidden = false;
    } catch (e) {
      toast("Alert details failed: " + e.message, "error");
    }
  }

  function closeDetail() {
    const m = $("alert-detail-modal");
    if (m) m.hidden = true;
    state.detail = null;
  }

  // --------------------------------------------------------------- network

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
    if (console && console.log) console.log("[alerts]", msg);
  }

  function announceNew(alerts) {
    // Toast new critical/warning alerts once (not on the first load — a page
    // navigation must not replay every open alert as a toast).
    alerts.forEach((a) => {
      if (state.seen.has(a.alert_id)) return;
      state.seen.add(a.alert_id);
      if (state.primed && (a.severity === "critical" || a.severity === "warning")) {
        toast(`${ICON[a.severity]} ${a.title || a.alert_type}: ${a.message}`,
              a.severity === "critical" ? "error" : "warning");
      }
    });
    state.primed = true;
  }

  function apply(data) {
    state.error = null;
    const changed = data.version !== state.version;
    state.alerts = data.alerts || [];
    state.counts = data.counts || state.counts;
    state.version = data.version;
    announceNew(state.alerts);
    if (changed) render();
    return changed;
  }

  async function poll() {
    try {
      apply(await api("/api/alerts/active"));
    } catch (e) {
      if (state.error !== e.message) {
        state.error = e.message;
        render();
      }
    }
  }

  function schedule() {
    if (state.timer) clearTimeout(state.timer);
    const hidden = typeof document.hidden === "boolean" && document.hidden;
    state.timer = setTimeout(async () => { await poll(); schedule(); }, hidden ? HIDDEN_POLL_MS : POLL_MS);
  }

  async function lifecycle(id, action) {
    try {
      await api(`/api/alerts/${encodeURIComponent(id)}/${action}`, "POST", { by: "trader" });
      state.alerts = state.alerts.filter((a) => a.alert_id !== id);
      closeDetail();
      await poll();
      render();
    } catch (e) {
      toast(`Could not ${action} alert: ${e.message}`, "error");
    }
  }

  // ---------------------------------------------------------------- events

  function setExpanded(v) {
    state.expanded = !!v;
    try { localStorage.setItem(LS_EXPANDED, state.expanded ? "1" : "0"); } catch (_) { /* private mode */ }
    render();
  }

  function onClick(ev) {
    const t = ev && ev.target && ev.target.closest ? ev.target.closest("[data-aw]") : null;
    if (!t) {
      // Click on the modal backdrop closes it.
      if (ev && ev.target && ev.target.id === "alert-detail-modal") closeDetail();
      return;
    }
    const action = t.dataset.aw;
    const id = t.dataset.id;
    if (action === "toggle") setExpanded(!state.expanded);
    else if (action === "details") openDetail(id);
    else if (action === "dismiss") lifecycle(id, "dismiss");
    else if (action === "review") lifecycle(id, "review");
    else if (action === "close-modal") closeDetail();
    else if (action === "deeplink") closeDetail();
  }

  function init() {
    const root = $("alert-widget");
    if (!root) return;
    try { state.expanded = localStorage.getItem(LS_EXPANDED) === "1"; } catch (_) { state.expanded = false; }
    if (document.body && document.body.classList) document.body.classList.add("has-alert-widget");
    root.addEventListener("click", onClick);
    if (document.addEventListener) {
      document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDetail(); });
      document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); schedule(); });
    }
    render();
    poll().then(schedule);
  }

  window.AlertWidget = {
    init, poll, render, apply, openDetail, closeDetail, setExpanded,
    renderMinimized, renderExpanded, renderDetail, metricRows, sortAlerts, deepLink, ago, esc,
    getState: () => state,
  };

  if (!window.__ALERT_WIDGET_NO_AUTOINIT__) {
    if (document.readyState === "loading" && document.addEventListener) {
      document.addEventListener("DOMContentLoaded", init);
    } else {
      init();
    }
  }
})();
