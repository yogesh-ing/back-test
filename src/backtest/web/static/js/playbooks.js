/**
 * Playbooks — Plug-and-Play Option Configs (Fix for Options Tab Redesign)
 *
 * Playbook = declarative config (structure, strikes policy, exits, sizing).
 * Portfolio spawns Runners FROM Playbooks. One concept, embedded, no new page.
 *
 * This is the UI for the Playbook entity that makes option trading plug-and-play.
 * The runner-spawn config already IS a playbook; it just couldn't be saved/named/reused.
 * Now it can.
 */

(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  const fmtMoney = (n) => {
    const v = Math.round(n || 0);
    return "₹" + Math.abs(v).toLocaleString("en-IN");
  };

  async function api(url, method, body) {
    const opts = { method: method || "GET", headers: { "Content-Type": "application/json" } };
    if (body) opts.body = JSON.stringify(body);
    const res = await fetch(url, opts);
    let data = null;
    try { data = await res.json(); } catch (e) { data = {}; }
    if (!res.ok || data.success === false) {
      throw new Error(data.error || ("HTTP " + res.status));
    }
    return data;
  }

  function playbookCardHtml(pb) {
    const structLabel = (() => {
      if (typeof pb.structure_type === "string") return pb.structure_type;
      if (typeof pb.structure_type === "object") {
        const bull = pb.structure_type.BULLISH || "bull_call_spread";
        const bear = pb.structure_type.BEARISH || "bear_put_spread";
        return `${bull} / ${bear}`;
      }
      return "direction-aware";
    })();

    const exit = pb.exit_config || {};
    const exitBits = [];
    if (exit.signal_flip === false) exitBits.push("hold through flips");
    if (exit.stop_loss_pct != null) exitBits.push(`stop ${Math.round(exit.stop_loss_pct * 100)}%`);
    if (exit.take_profit_pct != null) exitBits.push(`target ${Math.round(exit.take_profit_pct * 100)}%`);
    if (exit.min_days_to_expiry === null) exitBits.push("ride to settlement");
    else exitBits.push(`square off ${exit.min_days_to_expiry ?? 1}d early`);
    if (exit.reenter) exitBits.push("re-enter on flip");

    const tags = (pb.tags || []).map(t => `<span class="chip">${esc(t)}</span>`).join("");

    return `
      <div class="card playbook-card" data-id="${esc(pb.playbook_id)}">
        <div style="display:flex; justify-content:space-between; align-items:flex-start; gap:8px;">
          <div>
            <div style="font-weight:600; font-size:14px;">${esc(pb.name)}</div>
            <div class="muted" style="font-size:12px;">${esc(pb.underlying)} · ${esc(structLabel)} · ${esc(pb.strike_selection)}${pb.strike_selection === "delta" ? ` Δ${pb.delta_target}` : ""} · ${pb.quantity} lot(s)</div>
            <div class="muted" style="font-size:11px; margin-top:4px;">${esc(pb.description || "")}</div>
            <div class="muted" style="font-size:11px; margin-top:4px;">Exit: ${esc(exitBits.join(", ") || "flip only")}</div>
            ${pb.max_loss_per_trade ? `<div class="muted" style="font-size:11px;">Risk cap: ${fmtMoney(pb.max_loss_per_trade)} max loss / signal</div>` : ""}
          </div>
          <div style="display:flex; flex-direction:column; gap:4px;">
            <button class="btn btn-primary btn-small btn-spawn-playbook" data-id="${esc(pb.playbook_id)}" type="button">Deploy</button>
            <button class="btn btn-ghost btn-small btn-edit-playbook" data-id="${esc(pb.playbook_id)}" type="button">Edit</button>
            ${pb.playbook_id.startsWith("pb_default_") ? "" : `<button class="btn btn-ghost btn-small btn-delete-playbook" data-id="${esc(pb.playbook_id)}" type="button">Delete</button>`}
          </div>
        </div>
        <div style="margin-top:8px; display:flex; gap:4px; flex-wrap:wrap;">${tags}</div>
      </div>
    `;
  }

  async function loadPlaybooks() {
    const container = $("playbooks-list");
    if (!container) return;

    try {
      const data = await api("/api/playbooks");
      const pbs = data.playbooks || [];

      if (!pbs.length) {
        container.innerHTML = '<p class="muted">No playbooks yet. Click ＋ New Playbook to create one.</p>';
        return;
      }

      container.innerHTML = pbs.map(playbookCardHtml).join("");

      // Wire buttons
      container.querySelectorAll(".btn-spawn-playbook").forEach(btn => {
        btn.addEventListener("click", () => spawnFromPlaybook(btn.dataset.id));
      });
      container.querySelectorAll(".btn-delete-playbook").forEach(btn => {
        btn.addEventListener("click", async () => {
          if (!confirm(`Delete playbook ${btn.dataset.id}?`)) return;
          try {
            await api(`/api/playbooks/${btn.dataset.id}`, "DELETE");
            loadPlaybooks();
            if (window.showToast) window.showToast("Playbook deleted", "success");
          } catch (e) {
            if (window.showToast) window.showToast(e.message, "error");
          }
        });
      });
      container.querySelectorAll(".btn-edit-playbook").forEach(btn => {
        btn.addEventListener("click", () => editPlaybook(btn.dataset.id));
      });

    } catch (e) {
      container.innerHTML = `<div class="card-error">Failed to load playbooks: ${esc(e.message)}</div>`;
    }
  }

  async function spawnFromPlaybook(playbookId) {
    // Reuse the spawn modal but pre-fill from playbook
    try {
      const data = await api(`/api/playbooks/${playbookId}`);
      const pb = data.playbook;

      // Open spawn modal
      const modal = $("spawn-modal");
      if (modal) {
        modal.hidden = false;
        // Trigger form load
        if (typeof loadSpawnForm === "function") {
          await loadSpawnForm();
        }

        // Pre-fill
        const nameEl = $("spawn-name");
        if (nameEl) nameEl.value = pb.name + " · " + new Date().toLocaleTimeString();

        const instrumentEl = $("spawn-instrument-type");
        if (instrumentEl) {
          instrumentEl.value = "option";
          instrumentEl.dispatchEvent(new Event("change"));
        }

        const symbolEl = $("spawn-symbol");
        if (symbolEl) symbolEl.value = pb.underlying;

        const structEl = $("spawn-opt-structure");
        if (structEl) {
          // Map playbook structure to form
          const st = pb.structure_type;
          if (typeof st === "string") structEl.value = st;
          else structEl.value = "direction_aware";
          structEl.dispatchEvent(new Event("change"));
        }

        const strikeEl = $("spawn-opt-strike");
        if (strikeEl) {
          strikeEl.value = pb.strike_selection;
          strikeEl.dispatchEvent(new Event("change"));
        }

        const deltaEl = $("spawn-opt-delta");
        if (deltaEl && pb.strike_selection === "delta") deltaEl.value = pb.delta_target;

        const qtyEl = $("spawn-opt-qty");
        if (qtyEl) qtyEl.value = pb.quantity;

        // Exit config
        const exit = pb.exit_config || {};
        const stopEl = $("spawn-opt-stop");
        if (stopEl && exit.stop_loss_pct != null) stopEl.value = Math.round(exit.stop_loss_pct * 100);

        const targetEl = $("spawn-opt-target");
        if (targetEl && exit.take_profit_pct != null) targetEl.value = Math.round(exit.take_profit_pct * 100);

        const dteEl = $("spawn-opt-dte");
        if (dteEl) {
          if (exit.min_days_to_expiry === null) {
            const settleEl = $("spawn-opt-settle");
            if (settleEl) settleEl.checked = true;
          } else {
            dteEl.value = exit.min_days_to_expiry ?? 1;
          }
        }

        const flipEl = $("spawn-opt-flip");
        if (flipEl) flipEl.checked = exit.signal_flip !== false;

        const reenterEl = $("spawn-opt-reenter");
        if (reenterEl) reenterEl.checked = !!exit.reenter;

        // Trigger summary update
        const syncFn = window.OptionConfig ? null : null;
        // The portfolio.js syncOptionForm is not global, so we dispatch events
        if (instrumentEl) instrumentEl.dispatchEvent(new Event("change"));

        // Store playbook id for spawn
        modal.dataset.playbookId = playbookId;

        if (window.showToast) window.showToast(`Loaded playbook ${pb.name} — adjust and Deploy`, "info");
      } else {
        // Fallback: direct spawn via API
        const strategy = prompt("Strategy name for this playbook (e.g. directional_options):", "directional_options");
        if (!strategy) return;
        const capital = prompt("Allocated capital:", "100000");
        if (!capital) return;

        const result = await api(`/api/playbooks/${playbookId}/spawn`, "POST", {
          strategy: strategy,
          allocated_capital: parseFloat(capital) || 100000,
          mode: "paper",
          source: "synthetic",
        });

        if (window.showToast) window.showToast(`Deployed ${result.runner.name} from playbook`, "success");
      }
    } catch (e) {
      if (window.showToast) window.showToast(e.message, "error");
      else alert(e.message);
    }
  }

  async function editPlaybook(playbookId) {
    try {
      const data = await api(`/api/playbooks/${playbookId}`);
      const pb = data.playbook;

      const newName = prompt("Playbook name:", pb.name);
      if (newName === null) return;

      const newDesc = prompt("Description:", pb.description || "");
      if (newDesc === null) return;

      const newQty = prompt("Lots per leg:", String(pb.quantity));
      if (newQty === null) return;

      const newMaxLoss = prompt("Max loss per signal (₹, blank = no cap):", pb.max_loss_per_trade ? String(pb.max_loss_per_trade) : "");

      const updated = {
        ...pb,
        name: newName || pb.name,
        description: newDesc,
        quantity: parseInt(newQty, 10) || pb.quantity,
        max_loss_per_trade: newMaxLoss ? parseFloat(newMaxLoss) : null,
      };

      await api(`/api/playbooks/${playbookId}`, "PUT", updated);
      loadPlaybooks();
      if (window.showToast) window.showToast("Playbook updated", "success");

    } catch (e) {
      if (window.showToast) window.showToast(e.message, "error");
    }
  }

  // Create playbook modal (simple)
  function openCreatePlaybookModal() {
    const name = prompt("Playbook name (e.g. NIFTY ATM Bull Spread):");
    if (!name) return;

    const underlying = prompt("Underlying (NIFTY, BANKNIFTY):", "NIFTY");
    if (!underlying) return;

    const structure = prompt("Structure (direction_aware, bull_call_spread, bear_put_spread, long_call, long_put):", "direction_aware");
    if (!structure) return;

    const strikeSel = prompt("Strike selection (atm, delta):", "atm");
    if (!strikeSel) return;

    const qty = prompt("Lots per leg:", "1");
    if (!qty) return;

    const pbData = {
      name: name,
      underlying: underlying.toUpperCase(),
      structure_type: structure === "direction_aware" ? {"BULLISH": "bull_call_spread", "BEARISH": "bear_put_spread"} : structure,
      strike_selection: strikeSel.toLowerCase(),
      quantity: parseInt(qty, 10) || 1,
      exit_config: {
        signal_flip: true,
        stop_loss_pct: 0.5,
        take_profit_pct: 1.0,
        min_days_to_expiry: 1,
      },
      description: `Custom playbook for ${underlying}`,
      tags: [underlying.toLowerCase(), "custom"],
    };

    api("/api/playbooks", "POST", pbData)
      .then(() => {
        loadPlaybooks();
        if (window.showToast) window.showToast("Playbook created", "success");
      })
      .catch(e => {
        if (window.showToast) window.showToast(e.message, "error");
      });
  }

  // Dashboard book rendering (merged second book)
  async function loadDashboardBook() {
    const container = $("dashboard-book-content");
    const banner = $("dashboard-book-banner");
    const countEl = $("db-book-count");

    if (!container) return;

    try {
      // Get from portfolio summary (already merged)
      const summaryRes = await fetch("/api/portfolio/summary");
      const summaryData = await summaryRes.json();
      const portfolio = summaryData.portfolio || summaryData;
      const dbBook = portfolio.dashboard_book;

      if (!dbBook || !dbBook.exists) {
        container.innerHTML = '<p class="muted">No manual options book — the legacy Options tab book is empty or never created. All option trading now happens via Portfolio runners (playbooks).</p>';
        if (banner) banner.hidden = true;
        return;
      }

      // Show banner if book has positions
      if (banner && countEl) {
        if (dbBook.open_structures > 0) {
          banner.hidden = false;
          countEl.textContent = `${dbBook.open_structures} structure(s), ${dbBook.open_positions} leg(s) — Equity ₹${Math.round(dbBook.equity).toLocaleString("en-IN")}`;
        } else {
          banner.hidden = true;
        }
      }

      const structures = dbBook.structures || [];
      const positions = dbBook.positions || [];

      if (!structures.length) {
        container.innerHTML = `
          <p class="muted">Manual book is flat — no open structures.</p>
          <div class="muted" style="font-size:12px;">
            Capital: ₹${Math.round(dbBook.capital).toLocaleString("en-IN")} ·
            Equity: ₹${Math.round(dbBook.equity).toLocaleString("en-IN")} ·
            Realized: ₹${Math.round(dbBook.realized_pnl).toLocaleString("en-IN")} ·
            Unrealized: ₹${Math.round(dbBook.unrealized_pnl).toLocaleString("en-IN")}
          </div>
        `;
        return;
      }

      container.innerHTML = `
        <div style="margin-bottom:12px;" class="muted">
          Capital: ₹${Math.round(dbBook.capital).toLocaleString("en-IN")} ·
          Equity: ₹${Math.round(dbBook.equity).toLocaleString("en-IN")} ·
          Realized: ₹${Math.round(dbBook.realized_pnl).toLocaleString("en-IN")} ·
          Unrealized: ₹${Math.round(dbBook.unrealized_pnl).toLocaleString("en-IN")} ·
          Quote source: ${dbBook.quote_source || "unknown"}
        </div>
        <table class="matrix-table">
          <thead>
            <tr><th>Structure</th><th>Type</th><th>Underlying</th><th>Expiry</th><th>Legs</th><th>Entry Cost</th><th>Unrealized</th><th>Actions</th></tr>
          </thead>
          <tbody>
            ${structures.map(s => `
              <tr>
                <td><strong>${s.structure_id.slice(0,8)}</strong></td>
                <td>${esc(s.structure_type)}</td>
                <td>${esc(s.underlying)}</td>
                <td>${esc(s.expiry || "—")}</td>
                <td>${s.leg_count}</td>
                <td>₹${Math.round(s.total_entry_cost).toLocaleString("en-IN")}</td>
                <td style="color:${s.total_unrealized_pnl >= 0 ? "#10b981" : "#ef4444"}">₹${Math.round(s.total_unrealized_pnl).toLocaleString("en-IN")}</td>
                <td><button class="btn btn-small btn-close-db-structure" data-id="${esc(s.structure_id)}" type="button">Close</button></td>
              </tr>
            `).join("")}
          </tbody>
        </table>
        <h4 style="margin-top:16px;">Legs</h4>
        <table class="matrix-table">
          <thead><tr><th>Symbol</th><th>Side</th><th>Strike</th><th>Type</th><th>Qty</th><th>Entry</th><th>LTP</th><th>P&L</th></tr></thead>
          <tbody>
            ${positions.map(p => `
              <tr>
                <td><strong>${esc(p.trading_symbol)}</strong></td>
                <td>${esc(p.side)}</td>
                <td>${esc(p.strike)}</td>
                <td>${esc(p.option_type)}</td>
                <td>${p.quantity} × ${p.lot_size}</td>
                <td>₹${p.entry_price.toFixed(2)}</td>
                <td>₹${p.current_price.toFixed(2)}</td>
                <td style="color:${p.unrealized_pnl >= 0 ? "#10b981" : "#ef4444"}">₹${Math.round(p.unrealized_pnl).toLocaleString("en-IN")}</td>
              </tr>
            `).join("")}
          </tbody>
        </table>
      `;

      // Wire close buttons
      container.querySelectorAll(".btn-close-db-structure").forEach(btn => {
        btn.addEventListener("click", async () => {
          if (!confirm(`Close structure ${btn.dataset.id.slice(0,8)}?`)) return;
          btn.disabled = true;
          try {
            const resp = await fetch(`/api/options/structures/${btn.dataset.id}/close`, { method: "POST" });
            const out = await resp.json();
            if (!resp.ok) throw new Error(out.error || "Close failed");
            if (window.showToast) window.showToast(`Closed — P&L ₹${Math.round(out.realized_pnl).toLocaleString("en-IN")}`, "success");
            loadDashboardBook();
          } catch (e) {
            if (window.showToast) window.showToast(e.message, "error");
            btn.disabled = false;
          }
        });
      });

    } catch (e) {
      container.innerHTML = `<div class="card-error">Failed to load dashboard book: ${esc(e.message)}</div>`;
    }
  }

  // Boot
  document.addEventListener("DOMContentLoaded", () => {
    // Playbooks tab
    const playbooksTab = document.querySelector('[data-tab="playbooks"]');
    if (playbooksTab) {
      playbooksTab.addEventListener("click", () => {
        // Small delay so tab switch happens first
        setTimeout(loadPlaybooks, 50);
      });
    }

    // Dashboard book tab
    const dbTab = document.querySelector('[data-tab="dashboard-book"]');
    if (dbTab) {
      dbTab.addEventListener("click", () => {
        setTimeout(loadDashboardBook, 50);
      });
    }

    // Buttons
    const createBtn = $("btn-create-playbook");
    if (createBtn) createBtn.addEventListener("click", openCreatePlaybookModal);

    const refreshDbBtn = $("btn-refresh-dashboard-book");
    if (refreshDbBtn) refreshDbBtn.addEventListener("click", loadDashboardBook);

    const viewDbBtn = $("btn-view-dashboard-book");
    if (viewDbBtn) {
      viewDbBtn.addEventListener("click", () => {
        // Switch to dashboard-book tab
        document.querySelectorAll(".tab").forEach(t => t.classList.remove("active"));
        const target = document.querySelector('[data-tab="dashboard-book"]');
        if (target) target.classList.add("active");
        document.querySelectorAll(".tab-panel").forEach(p => p.hidden = true);
        const panel = $("tab-dashboard-book");
        if (panel) panel.hidden = false;
        loadDashboardBook();
      });
    }

    const flattenDbBtn = $("btn-flatten-dashboard-book");
    if (flattenDbBtn) {
      flattenDbBtn.addEventListener("click", async () => {
        if (!confirm("Flatten ALL manual options book structures? This closes everything from Options tab.")) return;
        try {
          // Use portfolio emergency flatten which now also flattens dashboard book
          const res = await fetch("/api/portfolio/emergency_stop", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ reason: "manual_dashboard_flatten", mode: "paper" }),
          });
          const out = await res.json();
          if (!res.ok || out.success === false) throw new Error(out.error || "Flatten failed");
          if (window.showToast) window.showToast(`Flattened ${out.flattened_positions} positions (including manual book)`, "success");
          loadDashboardBook();
        } catch (e) {
          if (window.showToast) window.showToast(e.message, "error");
        }
      });
    }

    const dismissUnifiedBtn = $("btn-dismiss-unified");
    if (dismissUnifiedBtn) {
      dismissUnifiedBtn.addEventListener("click", () => {
        const banner = $("unified-banner");
        if (banner) banner.hidden = true;
        localStorage.setItem("unified-banner-dismissed", "1");
      });
      if (localStorage.getItem("unified-banner-dismissed") === "1") {
        const banner = $("unified-banner");
        if (banner) banner.hidden = true;
      }
    }

    // Initial load if playbooks tab is active or on portfolio landing
    // Load dashboard book summary for banner
    fetch("/api/portfolio/summary")
      .then(r => r.json())
      .then(data => {
        const portfolio = data.portfolio || data;
        const dbBook = portfolio.dashboard_book;
        const banner = $("dashboard-book-banner");
        const countEl = $("db-book-count");
        if (banner && countEl && dbBook && dbBook.exists && dbBook.open_structures > 0) {
          banner.hidden = false;
          countEl.textContent = `${dbBook.open_structures} structure(s), ${dbBook.open_positions} leg(s) — Equity ₹${Math.round(dbBook.equity).toLocaleString("en-IN")}`;
        }
      })
      .catch(() => {});

  });

  window.Playbooks = { loadPlaybooks, loadDashboardBook, spawnFromPlaybook };
})();
