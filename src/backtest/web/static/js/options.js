/**
 * Options dashboard — polls /api/options/summary and renders
 * summary cards, portfolio Greeks, expiry alerts, positions, structures.
 */
(function () {
  "use strict";

  const fmtMoney = (v) =>
    "₹" + Number(v).toLocaleString("en-IN", { maximumFractionDigits: 2 });

  const fmtPnl = (v) => {
    const n = Number(v);
    const cls = n > 0 ? "pnl-pos" : n < 0 ? "pnl-neg" : "";
    return `<span class="${cls}">${fmtMoney(n)}</span>`;
  };

  const fmtGreek = (v, digits) => {
    const n = Number(v);
    const cls = n > 0 ? "pnl-pos" : n < 0 ? "pnl-neg" : "";
    return `<span class="${cls}">${n.toFixed(digits)}</span>`;
  };

  function renderSummary(data) {
    document.getElementById("opt-equity").textContent = fmtMoney(data.total_equity);
    document.getElementById("opt-cash").textContent = fmtMoney(data.available_cash);
    document.getElementById("opt-positions").textContent = data.open_position_count;
    document.getElementById("opt-pnl").innerHTML = fmtPnl(data.realized_pnl);
  }

  function renderGreeks(greeks) {
    document.getElementById("gk-delta").innerHTML = fmtGreek(greeks.total_delta, 2);
    document.getElementById("gk-gamma").innerHTML = fmtGreek(greeks.total_gamma, 4);
    document.getElementById("gk-theta").innerHTML = fmtGreek(greeks.total_theta / 365, 2);
    document.getElementById("gk-vega").innerHTML = fmtGreek(greeks.total_vega, 2);
    document.getElementById("gk-rho").innerHTML = fmtGreek(greeks.total_rho, 3);
  }

  function renderAlerts(alerts) {
    const card = document.getElementById("expiry-alerts-card");
    const box = document.getElementById("expiry-alerts");
    if (!alerts || alerts.length === 0) {
      card.style.display = "none";
      return;
    }
    card.style.display = "";
    // Show the most recent 5
    const recent = alerts.slice(-5).reverse();
    box.innerHTML = recent
      .map((a) => {
        const icon =
          a.type === "error"
            ? "❌"
            : a.type === "auto_squared_off"
            ? "🛑"
            : a.type === "expired_itm"
            ? "✅"
            : a.type === "expired_otm"
            ? "⭕"
            : "🔔";
        const when = new Date(a.timestamp).toLocaleTimeString();
        return `<div class="alert-row">
          <span>${icon}</span>
          <span class="alert-msg">${a.message}</span>
          <span class="alert-time muted">${when}</span>
        </div>`;
      })
      .join("");
  }

  function renderPositions(positions) {
    const body = document.getElementById("opt-positions-body");
    if (!positions || positions.length === 0) {
      body.innerHTML =
        '<tr><td colspan="9" class="muted">No open positions.</td></tr>';
      return;
    }
    body.innerHTML = positions
      .map((p) => {
        const sideCls = p.side === "BUY" ? "side-buy" : "side-sell";
        return `<tr>
          <td><strong>${p.trading_symbol}</strong></td>
          <td><span class="${sideCls}">${p.side}</span></td>
          <td>${p.quantity} × ${p.lot_size}</td>
          <td>${p.strike}</td>
          <td>${p.option_type}</td>
          <td>${p.expiry || "—"}</td>
          <td>${fmtMoney(p.entry_price)}</td>
          <td>${fmtMoney(p.current_price)}</td>
          <td>${fmtPnl(p.unrealized_pnl)}</td>
        </tr>`;
      })
      .join("");
  }

  function renderStructures(structures) {
    const body = document.getElementById("opt-structures-body");
    if (!structures || structures.length === 0) {
      body.innerHTML =
        '<tr><td colspan="8" class="muted">No open structures.</td></tr>';
      return;
    }
    body.innerHTML = structures
      .map((s) => {
        return `<tr>
          <td><strong>${s.structure_id.slice(0, 8)}</strong></td>
          <td>${s.structure_type}</td>
          <td>${s.underlying}</td>
          <td>${s.expiry || "—"}</td>
          <td>${s.leg_count}</td>
          <td>${fmtMoney(s.total_entry_cost)}</td>
          <td>${fmtPnl(s.total_unrealized_pnl)}</td>
          <td><button class="btn btn-small btn-close-structure"
                data-id="${s.structure_id}">Close</button></td>
        </tr>`;
      })
      .join("");

    // Wire close buttons
    body.querySelectorAll(".btn-close-structure").forEach((btn) => {
      btn.addEventListener("click", async () => {
        btn.disabled = true;
        btn.textContent = "…";
        try {
          const resp = await fetch(
            `/api/options/structures/${btn.dataset.id}/close`,
            { method: "POST" }
          );
          const payload = await resp.json();
          if (!resp.ok) {
            window.toast && window.toast(payload.error || "Close failed", "error");
          } else {
            window.toast &&
              window.toast(`Closed — P&L ${fmtMoney(payload.realized_pnl)}`, "success");
          }
        } catch (e) {
          window.toast && window.toast("Network error", "error");
        }
        refresh();
      });
    });
  }

  async function refresh() {
    try {
      const resp = await fetch("/api/options/summary");
      if (!resp.ok) throw new Error("summary fetch failed");
      const data = await resp.json();
      renderSummary(data);
      renderGreeks(data.greeks || {});
      renderAlerts(data.alerts || []);
      renderPositions(data.positions || []);
      renderStructures(data.structures || []);
    } catch (e) {
      console.error("options dashboard refresh failed:", e);
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    refresh();
    setInterval(refresh, 5000); // poll every 5s
  });
})();
