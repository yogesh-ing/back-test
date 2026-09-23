/* Tier 2: Landing Global Risk Header + Bucket Mini-Risk Row */
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

  let lastPortfolio = null;
  let expanded = localStorage.getItem("grh_expanded") === "1";

  function gaugeVisual(pctVal) {
    if (pctVal >= 0.8) return { bar: "▓▓▓", cls: "danger" };
    if (pctVal >= 0.5) return { bar: "▓▓░", cls: "warn" };
    return { bar: "▓░░", cls: "safe" };
  }

  function getState(p) {
    if (!p) return "safe";
    if (p.halted) return "halted";
    const max = Math.max(p.daily_loss_pct || 0, p.drawdown_pct || 0, p.deployed_pct || 0);
    if (max >= 0.8) return "danger";
    if (max >= 0.5) return "warning";
    return "safe";
  }

  function updateGlobalHeader(p) {
    const header = $("global-risk-header");
    if (!header || !p) return;
    header.hidden = false;

    const state = getState(p);
    header.className = "global-risk-header " + (expanded ? "" : "collapsed ") + state;

    const statusText = $("grh-status-text");
    if (statusText) {
      if (state === "halted") statusText.textContent = "🔴 HALTED — " + (p.halt_reason || "Risk breach");
      else if (state === "danger") statusText.textContent = "🔴 High Risk — " + pct(Math.max(p.daily_loss_pct, p.drawdown_pct));
      else if (state === "warning") statusText.textContent = "🟡 Warning — approaching limits";
      else statusText.textContent = "🟢 Safe — all limits nominal";
    }

    const dlossText = $("grh-dloss-text");
    const ddText = $("grh-dd-text");
    const depText = $("grh-dep-text");
    const dlossBar = $("grh-dloss-bar");
    const ddBar = $("grh-dd-bar");
    if (dlossText) dlossText.textContent = `Daily Loss: ${pct(p.daily_loss_pct)}`;
    if (ddText) ddText.textContent = `DD: ${pct(p.drawdown_pct)}`;
    if (depText) depText.textContent = `Deployed: ${pct(p.deployed_pct)}`;
    if (dlossBar) {
      dlossBar.style.width = Math.min(100, (p.daily_loss_pct || 0) * 100) + "%";
      dlossBar.className = "risk-strip-bar-fill " + (p.daily_loss_pct >= 0.8 ? "bar-danger" : p.daily_loss_pct >= 0.5 ? "bar-warning" : "bar-safe");
    }
    if (ddBar) {
      ddBar.style.width = Math.min(100, (p.drawdown_pct || 0) * 100 / 0.25 * 100) + "%";
      ddBar.className = "risk-strip-bar-fill " + (p.drawdown_pct >= 0.2 ? "bar-danger" : p.drawdown_pct >= 0.125 ? "bar-warning" : "bar-safe");
    }

    const dailyPnlEl = $("grh-daily-pnl");
    if (dailyPnlEl) {
      dailyPnlEl.textContent = fmtSigned(p.daily_pnl || 0);
      dailyPnlEl.className = "metric-value " + ((p.daily_pnl || 0) >= 0 ? "pnl-pos" : "pnl-neg");
    }
    const sub = $("grh-daily-pnl-sub");
    if (sub) sub.textContent = `Used ${fmtMoney(p.daily_loss_used || 0)} / ${fmtMoney(p.daily_loss_limit || 0)}`;

    const lossUsage = $("grh-loss-usage");
    const lossBar = $("grh-loss-bar");
    if (lossUsage) lossUsage.textContent = pct(p.daily_loss_pct);
    if (lossBar) lossBar.style.width = Math.min(100, (p.daily_loss_pct || 0) * 100) + "%";

    const ddEl = $("grh-dd");
    const ddSub = $("grh-dd-sub");
    if (ddEl) ddEl.textContent = pct(p.drawdown_pct);
    if (ddSub) ddSub.textContent = `Limit ${pct(p.max_drawdown_limit_pct || 0.25)} — Peak ${fmtMoney(p.peak_equity || 0)}`;

    const losersEl = $("grh-losers");
    if (losersEl && p.runners) {
      const losers = [...p.runners].filter(r => r.daily_pnl < 0).sort((a, b) => a.daily_pnl - b.daily_pnl).slice(0, 3);
      if (!losers.length) losersEl.textContent = "No losers — all green.";
      else losersEl.innerHTML = losers.map(r => `${r.name}: ${fmtSigned(r.daily_pnl)}`).join("<br>");
    }

    // Auto-expand if any limit >=50%
    if (!localStorage.getItem("grh_expanded")) {
      if ((p.daily_loss_pct || 0) >= 0.5 || (p.drawdown_pct || 0) >= 0.125) {
        expanded = true;
        header.classList.remove("collapsed");
        const btn = $("grh-expand-btn");
        if (btn) btn.textContent = "▲ Less";
      }
    }
  }

  function updateBucketMiniRisk(mode, summary) {
    const card = $("bucket-card-" + mode);
    const dlossBar = $("bmr-" + mode + "-dloss-bar");
    const dlossEl = $("bmr-" + mode + "-dloss");
    const ddEl = $("bmr-" + mode + "-dd");
    const posEl = $("bmr-" + mode + "-pos");
    if (!summary) return;

    const dlPct = summary.daily_loss_pct || 0;
    const ddPct = summary.drawdown_pct || 0;
    const g = gaugeVisual(dlPct);

    if (dlossBar) {
      dlossBar.textContent = g.bar;
      dlossBar.className = "mini-gauge-bar " + g.cls;
    }
    if (dlossEl) dlossEl.textContent = pct(dlPct);
    if (ddEl) ddEl.textContent = pct(ddPct);
    if (posEl) posEl.textContent = `${summary.open_positions || 0} pos`;

    // 2026-09-22: live Running / Open-positions rows on the overview card.
    const runEl = $("bucket-running-" + mode);
    const openEl = $("bucket-openpos-" + mode);
    if (runEl) runEl.textContent = summary.running || 0;
    if (openEl) openEl.textContent = summary.open_positions || 0;

    if (card) {
      card.classList.remove("bucket-card-risk-danger", "bucket-card-risk-warning");
      if (dlPct >= 0.8 || ddPct >= 0.2) card.classList.add("bucket-card-risk-danger");
      else if (dlPct >= 0.5 || ddPct >= 0.125) card.classList.add("bucket-card-risk-warning");
    }
  }

  async function fetchBucketRisk(mode) {
    try {
      const res = await fetch(`/api/portfolio/summary?mode=${mode}`);
      const data = await res.json();
      if (data.success && data.portfolio) {
        updateBucketMiniRisk(mode, data.portfolio);
      }
    } catch (_) {}
  }

  async function fetchAll() {
    try {
      const res = await fetch("/api/portfolio/summary");
      const data = await res.json();
      if (data.success && data.portfolio) {
        lastPortfolio = data.portfolio;
        updateGlobalHeader(data.portfolio);
      }
    } catch (_) {}
    // Per-bucket
    fetchBucketRisk("paper");
    fetchBucketRisk("live");
  }

  function bind() {
    const toggle = $("grh-toggle");
    const btn = $("grh-expand-btn");
    const header = $("global-risk-header");
    if (toggle && header) {
      const handler = () => {
        expanded = !expanded;
        header.classList.toggle("collapsed", !expanded);
        if (btn) btn.textContent = expanded ? "▲ Less" : "▼ Details";
        localStorage.setItem("grh_expanded", expanded ? "1" : "0");
      };
      toggle.addEventListener("click", handler);
      if (btn) btn.addEventListener("click", (e) => { e.stopPropagation(); handler(); });
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    if (!$("global-risk-header")) return;
    bind();
    fetchAll();
    setInterval(fetchAll, 3000);

    // Hook into RiskStrip if available
    const orig = window.RiskStrip?.updateStrip;
    if (orig) {
      const prev = window.RiskStrip.updateStrip;
      window.RiskStrip.updateStrip = function (p) {
        prev(p);
        updateGlobalHeader(p);
      };
    }
  });

  window.LandingRisk = { updateGlobalHeader, updateBucketMiniRisk, fetchAll };
})();
