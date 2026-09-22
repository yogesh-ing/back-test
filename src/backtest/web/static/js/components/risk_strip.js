/* Tier 1 Survival Layer — Sticky Risk Strip + Halt Pill
 * Always-visible critical metrics, 0-click awareness.
 * Fetches /api/portfolio/summary or uses SSE portfolio snapshot if available.
 * Implements states: safe (<50%), warning (50-80%), danger (>80%), halted.
 */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  let expanded = false;
  let lastPortfolio = null;
  let riskHistory = []; // last 6 hours of daily_loss_pct for mini chart
  let chart = null;

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
  const pct = (x) => ((x || 0) * 100).toFixed(1) + "%";

  function getState(p) {
    if (!p) return "safe";
    if (p.halted) return "halted";
    const dl = p.daily_loss_pct || 0;
    const dd = p.drawdown_pct || 0;
    const dep = p.deployed_pct || 0;
    const maxPct = Math.max(dl, dd, dep);
    if (maxPct >= 0.8) return "danger";
    if (maxPct >= 0.5) return "warning";
    return "safe";
  }

  function canDismiss(p) {
    if (!p) return true;
    if (p.halted) return false;
    return (p.daily_loss_pct || 0) < 0.5 && (p.drawdown_pct || 0) < 0.5 && (p.deployed_pct || 0) < 0.8;
  }

  function updateHaltPill(p) {
    const pill = $("halt-pill");
    const dot = $("halt-pill-dot");
    const txt = $("halt-pill-text");
    if (!pill || !dot || !txt) return;

    const state = getState(p);
    pill.className = "halt-pill halt-pill-" + state;

    if (state === "halted") {
      dot.textContent = "🔴";
      txt.textContent = "HALTED";
      pill.title = p.halt_reason ? `Halted: ${p.halt_reason}` : "Trading halted — click for Risk Board";
      pill.style.animation = "pulse-red 1.2s ease-in-out infinite";
    } else if (state === "danger") {
      dot.textContent = "🟠";
      txt.textContent = "RISK";
      pill.title = `High risk: Daily Loss ${pct(p.daily_loss_pct)} | Drawdown ${pct(p.drawdown_pct)}`;
      pill.style.animation = "";
    } else if (state === "warning") {
      dot.textContent = "🟡";
      txt.textContent = "WARN";
      pill.title = `Warning: Daily Loss ${pct(p.daily_loss_pct)}`;
      pill.style.animation = "";
    } else {
      dot.textContent = "🟢";
      txt.textContent = "LIVE";
      pill.title = "All runners active — click to view Risk Board";
      pill.style.animation = "";
    }
  }

  function updateStrip(p) {
    const strip = $("risk-strip");
    const feedEl = $("risk-strip-feed");
    const dlossEl = $("risk-strip-dloss");
    const ddEl = $("risk-strip-dd");
    const deployedEl = $("risk-strip-deployed");
    const posEl = $("risk-strip-positions");
    const dlossBar = $("risk-strip-dloss-bar");
    const ddBar = $("risk-strip-dd-bar");
    const deployedBar = $("risk-strip-deployed-bar");

    if (!strip) return;

    // Show strip only when we have data or when halted
    if (!p) {
      strip.hidden = true;
      return;
    }

    strip.hidden = false;

    const state = getState(p);
    strip.className = "risk-strip risk-strip-" + state;

    // Feed status
    if (feedEl) {
      const tick = p.tick || 0;
      const running = p.running || 0;
      feedEl.textContent = tick > 0 ? `🟢 Feed OK · ${running} running · ${tick} ticks` : "🟢 Feed OK";
    }

    // Daily loss
    if (dlossEl) {
      dlossEl.textContent = `Daily Loss: ${fmtMoney(p.daily_loss_used || 0)} / ${fmtMoney(p.daily_loss_limit || 0)} (${pct(p.daily_loss_pct)})`;
    }
    if (dlossBar) {
      dlossBar.style.width = Math.min(100, (p.daily_loss_pct || 0) * 100) + "%";
      dlossBar.className = "risk-strip-bar-fill " + (p.daily_loss_pct >= 0.8 ? "bar-danger" : p.daily_loss_pct >= 0.5 ? "bar-warning" : "bar-safe");
    }

    // Drawdown
    if (ddEl) {
      ddEl.textContent = `DD: ${pct(p.drawdown_pct)} / ${pct(p.max_drawdown_limit_pct || 0.25)}`;
    }
    if (ddBar) {
      ddBar.style.width = Math.min(100, (p.drawdown_pct || 0) * 100 / (p.max_drawdown_limit_pct || 0.25) * 100) + "%";
      const ddPctOfLimit = p.max_drawdown_limit_pct ? (p.drawdown_pct / p.max_drawdown_limit_pct) : p.drawdown_pct;
      ddBar.className = "risk-strip-bar-fill " + (ddPctOfLimit >= 0.8 ? "bar-danger" : ddPctOfLimit >= 0.5 ? "bar-warning" : "bar-safe");
    }

    // Deployed
    if (deployedEl) {
      deployedEl.textContent = `Deployed: ${fmtMoney(p.deployed_capital || 0)} (${pct(p.deployed_pct)})`;
    }
    if (deployedBar) {
      deployedBar.style.width = Math.min(100, (p.deployed_pct || 0) * 100) + "%";
      deployedBar.className = "risk-strip-bar-fill " + (p.deployed_pct >= 0.9 ? "bar-danger" : p.deployed_pct >= 0.7 ? "bar-warning" : "bar-safe");
    }

    // Positions
    if (posEl) {
      posEl.textContent = `Pos: ${p.open_positions || 0} / ${p.runner_count || 0} runners`;
    }

    // Handle auto-hide logic for safe state
    if (state === "safe" && !expanded) {
      // Allow hiding on scroll down - for now keep visible but subtle
      strip.style.opacity = "0.95";
    } else {
      strip.style.opacity = "1";
    }

    // Update history for expanded chart
    riskHistory.push({
      ts: new Date().toLocaleTimeString(),
      daily_loss_pct: p.daily_loss_pct || 0,
      drawdown_pct: p.drawdown_pct || 0,
    });
    if (riskHistory.length > 60) riskHistory = riskHistory.slice(-60); // last 60 points ~ 1 min at 1Hz

    if (expanded) renderExpanded(p);

    // Browser notification for danger/halted (once per breach)
    if (state === "halted" && !strip._notifiedHalted) {
      notifyHalt(p);
      strip._notifiedHalted = true;
    } else if (state !== "halted") {
      strip._notifiedHalted = false;
    }

    if (state === "danger" && !strip._notifiedDanger) {
      notifyDanger(p);
      strip._notifiedDanger = true;
    } else if (state === "safe") {
      strip._notifiedDanger = false;
    }
  }

  function renderExpanded(p) {
    const statsEl = $("risk-strip-detail-stats");
    const losersEl = $("risk-strip-top-losers");
    const canvas = $("risk-strip-chart");

    if (statsEl) {
      statsEl.innerHTML = `
        <div>Current: ${fmtMoney(p.daily_pnl || 0)} | Used: ${fmtMoney(p.daily_loss_used || 0)} / ${fmtMoney(p.daily_loss_limit || 0)}</div>
        <div>Peak Equity: ${fmtMoney(p.peak_equity || 0)} | Drawdown: ${pct(p.drawdown_pct)} (limit ${pct(p.max_drawdown_limit_pct || 0.25)})</div>
        <div>Deployed: ${fmtMoney(p.deployed_capital || 0)} (${pct(p.deployed_pct)}) | Gross: ${p.open_positions || 0} positions</div>
        <div class="muted">Last update: ${p.timestamp ? p.timestamp.slice(11,19) : new Date().toLocaleTimeString()}</div>
      `;
    }

    if (losersEl && p.runners) {
      const losers = [...p.runners].filter(r => r.daily_pnl < 0).sort((a,b) => a.daily_pnl - b.daily_pnl).slice(0, 3);
      if (!losers.length) {
        losersEl.innerHTML = `<div class="muted">No losing runners — all green.</div>`;
      } else {
        losersEl.innerHTML = `<strong>Top Losers Today:</strong>` + losers.map(r => `
          <div class="risk-strip-loser">• ${r.name} (${r.mode}): ${fmtSigned(r.daily_pnl)} (${r.target_label}) <button class="btn btn-ghost btn-small" onclick="window.location.href='/portfolio/${r.mode}'">View →</button></div>
        `).join("");
      }
    }

    // Mini chart
    if (canvas && typeof Chart !== "undefined" && riskHistory.length > 2) {
      if (chart) chart.destroy();
      const labels = riskHistory.map(h => h.ts);
      const dlData = riskHistory.map(h => (h.daily_loss_pct * 100).toFixed(2));
      chart = new Chart(canvas.getContext("2d"), {
        type: "line",
        data: {
          labels,
          datasets: [
            { label: "Daily Loss %", data: dlData, borderColor: "#ef4444", backgroundColor: "rgba(239,68,68,.1)", fill: true, tension: 0.3, pointRadius: 0, borderWidth: 2 },
          ]
        },
        options: {
          responsive: true,
          animation: false,
          plugins: { legend: { display: false } },
          scales: {
            x: { display: false },
            y: { ticks: { color: "#94a3b8", callback: v => v + "%" }, grid: { color: "rgba(148,163,184,.1)" } }
          }
        }
      });
    }
  }

  function notifyHalt(p) {
    if ("Notification" in window && Notification.permission === "granted") {
      new Notification("🔴 Trading HALTED", { body: p.halt_reason || "Risk limit breached", requireInteraction: true, tag: "halt" });
    }
    // Sound alert
    try {
      const audio = new Audio("data:audio/wav;base64,UklGRigAAABXQVZFZm10IBAAAAABAAEARKwAAIhYAQACABAAZGF0YQQAAAAAAA==");
      audio.volume = 0.3;
      audio.play().catch(()=>{});
    } catch(_) {}
    if (window.showToast) window.showToast("🔴 HALTED: " + (p.halt_reason || "Risk limit breached"), "error");
  }

  function notifyDanger(p) {
    if (window.showToast) window.showToast(`⚠️ High Risk: Daily Loss ${pct(p.daily_loss_pct)} — close to limit`, "warning");
  }

  async function fetchRisk() {
    try {
      const res = await fetch("/api/portfolio/summary");
      const data = await res.json();
      if (data.success && data.portfolio) {
        lastPortfolio = data.portfolio;
        updateStrip(data.portfolio);
        updateHaltPill(data.portfolio);
      }
    } catch (e) {
      // Silent — SSE will recover
    }
  }

  function bindEvents() {
    const expandBtn = $("risk-strip-expand");
    const collapseBtn = $("risk-strip-collapse");
    const collapsed = $("risk-strip-collapsed");
    const expandedEl = $("risk-strip-expanded");
    const haltPill = $("halt-pill");
    const flattenBtn = $("risk-strip-flatten");
    const flattenModal = $("risk-flatten-modal");
    const flattenConfirm = $("risk-flatten-confirm");
    const flattenStats = $("risk-flatten-stats");

    if (expandBtn && expandedEl) {
      expandBtn.addEventListener("click", () => {
        expanded = !expanded;
        expandedEl.hidden = !expanded;
        expandBtn.textContent = expanded ? "▲ Less" : "▼ Details";
        if (expanded && lastPortfolio) renderExpanded(lastPortfolio);
      });
    }

    if (collapseBtn && expandedEl && expandBtn) {
      collapseBtn.addEventListener("click", () => {
        expanded = false;
        expandedEl.hidden = true;
        expandBtn.textContent = "▼ Details";
      });
    }

    // Toggle on gauge click
    ["risk-strip-dloss", "risk-strip-dd", "risk-strip-deployed"].forEach(id => {
      const el = $(id);
      if (el) el.addEventListener("click", () => {
        if (expandBtn) expandBtn.click();
      });
    });

    if (haltPill) {
      haltPill.addEventListener("click", () => {
        const active = document.body.dataset.active;
        if (active === "portfolio") {
          // Focus Risk Board tab
          const riskTab = document.querySelector('[data-tab="risk"]');
          if (riskTab) riskTab.click();
          window.scrollTo({ top: 0, behavior: "smooth" });
        } else {
          window.location.href = "/risk";
        }
      });
    }

    if (flattenBtn && flattenModal && flattenStats) {
      flattenBtn.addEventListener("click", () => {
        const p = lastPortfolio;
        if (p) {
          flattenStats.innerHTML = `
            <div>Open positions: <strong>${p.open_positions || 0}</strong> across ${p.runner_count || 0} runners</div>
            <div>Total exposure: <strong>${fmtMoney(p.deployed_capital || 0)}</strong></div>
            <div>Current P&L: <strong>${fmtSigned(p.daily_pnl || 0)}</strong></div>
            <div>Daily loss used: <strong>${pct(p.daily_loss_pct)} / ${fmtMoney(p.daily_loss_limit || 0)}</strong></div>
          `;
        } else {
          flattenStats.textContent = "This will close all open positions.";
        }
        flattenModal.hidden = false;
      });
    }

    if (flattenConfirm) {
      flattenConfirm.addEventListener("click", async () => {
        flattenConfirm.disabled = true;
        flattenConfirm.textContent = "Flattening...";
        try {
          const res = await fetch("/api/portfolio/emergency_stop", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ reason: "sticky_strip_emergency" })
          });
          const data = await res.json();
          if (window.showToast) window.showToast(`Flattened ${data.flattened_positions || 0} positions`, "success");
          $("risk-flatten-modal").hidden = true;
        } catch (e) {
          if (window.showToast) window.showToast("Flatten failed: " + e.message, "error");
        } finally {
          flattenConfirm.disabled = false;
          flattenConfirm.textContent = "🔴 YES, FLATTEN EVERYTHING";
        }
      });
    }

    // Close modals
    document.querySelectorAll("[data-close='risk-flatten-modal']").forEach(b => {
      b.addEventListener("click", () => { $("risk-flatten-modal").hidden = true; });
    });

    // Auto-hide strip on scroll down for safe state
    let lastScroll = 0;
    window.addEventListener("scroll", () => {
      const strip = $("risk-strip");
      if (!strip || expanded) return;
      const p = lastPortfolio;
      if (!p || !canDismiss(p)) return; // locked when warning/danger/halted
      const cur = window.scrollY;
      if (cur > lastScroll && cur > 100) {
        strip.style.transform = "translateY(-100%)";
      } else {
        strip.style.transform = "translateY(0)";
      }
      lastScroll = cur;
    });
  }

  // SSE integration — portfolio.js already opens SSE, we hook into it
  function hookSSE() {
    // If portfolio stream exists, use its data
    // Fallback to polling every 2s for Tier 1
    setInterval(fetchRisk, 2000);
    fetchRisk();

    // Try to listen to portfolio events if EventSource is used
    const originalAddEventListener = EventSource.prototype.addEventListener;
    EventSource.prototype.addEventListener = function (type, handler) {
      if (type === "portfolio") {
        const wrapped = function (ev) {
          try {
            const p = JSON.parse(ev.data);
            lastPortfolio = p;
            updateStrip(p);
            updateHaltPill(p);
          } catch(_) {}
          return handler.call(this, ev);
        };
        return originalAddEventListener.call(this, type, wrapped);
      }
      return originalAddEventListener.call(this, type, handler);
    };
  }

  // Request notification permission
  if ("Notification" in window && Notification.permission === "default") {
    Notification.requestPermission().catch(()=>{});
  }

  document.addEventListener("DOMContentLoaded", () => {
    bindEvents();
    hookSSE();
  });

  window.RiskStrip = {
    updateStrip,
    updateHaltPill,
    fetchRisk,
    getHistory: () => riskHistory,
  };
})();
