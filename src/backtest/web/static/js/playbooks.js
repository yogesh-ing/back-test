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
    // C4: risk_envelope estimated flag — UI must show "estimated" badge next to ₹
    const riskCap = pb.max_loss_per_trade ? `${fmtMoney(pb.max_loss_per_trade)} <span class="chip" style="background:rgba(245,158,11,0.15); border-color:rgba(245,158,11,0.3); color:#fcd34d;">estimated</span>` : "";
    const versionBadge = pb.version != null ? `<span class="chip" title="Playbook version — runner snapshots expression at spawn">v${esc(pb.version)}</span>` : "";

    return `
      <div class="card playbook-card" data-id="${esc(pb.playbook_id)}">
        <div style="display:flex; justify-content:space-between; align-items:flex-start; gap:8px;">
          <div>
            <div style="font-weight:600; font-size:14px;">${esc(pb.name)} ${versionBadge}</div>
            <div class="muted" style="font-size:12px;">${esc(pb.underlying)} · ${esc(structLabel)} · ${esc(pb.strike_selection)}${pb.strike_selection === "delta" ? ` Δ${pb.delta_target}` : ""} · ${pb.quantity} lot(s)</div>
            <div class="muted" style="font-size:11px; margin-top:4px;">${esc(pb.description || "")}</div>
            <div class="muted" style="font-size:11px; margin-top:4px;">Exit: ${esc(exitBits.join(", ") || "flip only")}</div>
            ${riskCap ? `<div class="muted" style="font-size:11px;">Risk cap: ${riskCap} max loss / signal (per-signal, not per-day)</div>` : ""}
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
    // U6.1: the spawn form is routing-only now, so Deploying a playbook opens
    // the slim modal with strategy=option-kind and the playbook pre-selected.
    // Trading logic (structure/strikes/exits) is carried by the playbook itself.
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

        // Pre-fill routing fields only
        const nameEl = $("spawn-name");
        if (nameEl) nameEl.value = pb.name + " · " + new Date().toLocaleTimeString();

        // Strategy: prefer an option-kind strategy from the catalogue
        const stratSel = $("spawn-strategy");
        if (stratSel && stratSel._catalogue) {
          const optStrat = stratSel._catalogue.find((s) => s.signal_kind === "option");
          if (optStrat) stratSel.value = optStrat.name;
          stratSel.dispatchEvent(new Event("change"));
        }

        const symbolEl = $("spawn-symbol");
        if (symbolEl) symbolEl.value = pb.underlying;

        // Select the playbook in the picker
        const pbSel = $("spawn-playbook");
        if (pbSel) {
          pbSel.value = playbookId;
          pbSel.dispatchEvent(new Event("change"));
        }

        // Store playbook id for spawn
        modal.dataset.playbookId = playbookId;

        if (window.showToast) window.showToast(`Loaded playbook ${pb.name} — pick bucket/source and Deploy`, "info");
      } else {
        // Fallback: direct spawn via API — new spec: spawn returns config, caller creates (no side effects)
        const strategy = prompt("Strategy name for this playbook (e.g. directional_options):", "directional_options");
        if (!strategy) return;
        const capital = prompt("Allocated capital:", "100000");
        if (!capital) return;

        const spawnResult = await api(`/api/playbooks/${playbookId}/spawn`, "POST", {
          strategy: strategy,
          allocated_capital: parseFloat(capital) || 100000,
          mode: "paper",
          source: "synthetic",
        });

        // New API returns runner_config, old returned runner — handle both
        const runnerConfig = spawnResult.runner_config || spawnResult.config;
        if (!runnerConfig) {
          // Old API fallback
          if (spawnResult.runner) {
            if (window.showToast) window.showToast(`Deployed ${spawnResult.runner.name} from playbook`, "success");
            return;
          }
          throw new Error("Spawn did not return runner_config");
        }

        // One creation path: POST /api/portfolio/runner/create
        const createResult = await api("/api/portfolio/runner/create", "POST", runnerConfig);
        const runnerName = (createResult.runner && createResult.runner.name) || runnerConfig.name || playbookId;
        if (window.showToast) window.showToast(`Deployed ${runnerName} from playbook (v${spawnResult.runner_config ? "" : ""})`, "success");
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

    // Buttons
    const createBtn = $("btn-create-playbook");
    if (createBtn) createBtn.addEventListener("click", openCreatePlaybookModal);

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

    // (dashboard-book banner removed with the Manual Options Book tab)

  });

  window.Playbooks = { loadPlaybooks, spawnFromPlaybook };
})();
