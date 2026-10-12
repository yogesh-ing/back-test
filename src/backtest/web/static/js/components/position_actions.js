/* Position actions — manual intervention on a live position (Live Order Management).
 *
 * The positions table used to be read-only: an operator could watch a losing
 * position but not do anything about it short of flattening the whole bucket.
 * This component adds the four verbs that matter on ONE position — modify stop,
 * modify target, scale out, close — and owns the three things a money-moving
 * control must get right:
 *
 *   1. **The right row.** Every modal is opened from a row and shows that row's
 *      instance / symbol / mark before it can submit; the payload carries the
 *      row's own instance_id + position_key. There is no "currently selected"
 *      global that a re-render can invalidate.
 *   2. **The server decides validity.** Levels are validated server-side against
 *      the live mark (a stop through the market is refused, not armed); this
 *      component renders the refusal verbatim instead of inventing its own rule
 *      set that would drift from the engine's.
 *   3. **Honest feedback.** Success says what actually happened (units closed,
 *      fill price, remaining size); failure says why. Nothing is assumed to have
 *      filled because a click happened.
 *
 * Pure DOM + fetch; no build step. The API calls are relative URLs so the page
 * works behind the platform proxy.
 */
const PositionActions = (function () {
  "use strict";

  const ACTION_URL = "/api/portfolio/position/action";

  // Which modal answers which button, and how its submit is phrased.
  const VERBS = {
    modify_sl: { modal: "pos-sl-modal", label: "Stop" },
    modify_target: { modal: "pos-target-modal", label: "Target" },
    close_fraction: { modal: "pos-partial-modal", label: "Scale out" },
    close_all: { modal: "pos-closeall-modal", label: "Close" },
  };

  const state = {
    row: null, // the position row the open modal is acting on
    busy: false,
    onDone: null, // host hook: refresh the orders tab / audit trail
    toast: null,
  };

  const $ = (id) => document.getElementById(id);

  function toast(message, kind) {
    if (state.toast) return state.toast(message, kind);
    if (typeof window !== "undefined" && window.showToast) return window.showToast(message, kind);
    console.log("[position-actions]", kind || "info", message);
  }

  const money = (v) =>
    typeof Money !== "undefined" ? Money.format(v) : "₹" + Number(v || 0).toFixed(2);
  const price = (v) =>
    v === null || v === undefined || v === "" ? "—" : Number(v).toLocaleString("en-IN", {
      minimumFractionDigits: 2, maximumFractionDigits: 2,
    });

  function esc(text) {
    return String(text === null || text === undefined ? "" : text)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  async function post(body) {
    const res = await fetch(ACTION_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    let data = null;
    try { data = await res.json(); } catch (_e) { data = {}; }
    if (!res.ok || data.success === false) {
      throw new Error((data && data.error) || ("HTTP " + res.status));
    }
    return data;
  }

  /** Button cell for one position row (used by the positions table renderer). */
  function buttonCell(row) {
    if (!row) return "";
    const id = esc(row.instance_id || "");
    const key = esc(row.position_key || row.symbol || "");
    const set = 'data-id="' + id + '" data-key="' + key + '"';
    const partial = row.can_partial_close
      ? '<button class="row-btn" data-pos-act="close_fraction" ' + set +
        ' title="Scale out — close part of this position">◐ 50%</button>'
      : '<button class="row-btn" data-pos-act="close_fraction" ' + set +
        ' title="Multi-leg structures close atomically — 100% only" disabled>◐</button>';
    return (
      '<div class="pos-actions">' +
      '<button class="row-btn" data-pos-act="modify_sl" ' + set + ' title="Modify stop-loss">🛑 SL</button>' +
      '<button class="row-btn" data-pos-act="modify_target" ' + set + ' title="Modify target">🎯 TP</button>' +
      partial +
      '<button class="row-btn row-btn-danger" data-pos-act="close_all" ' + set +
      ' title="Close the whole position now">✕ All</button>' +
      "</div>"
    );
  }

  // ------------------------------------------------------------- modal flow

  function contextHtml(row) {
    const kind = row.kind === "option" ? "Option structure" : "Equity position";
    return (
      '<div class="pos-modal-line"><strong>' + esc(row.runner || row.instance_id || "runner") +
      "</strong> · " + esc(kind) + "</div>" +
      '<div class="pos-modal-line">' + esc(row.label || row.symbol) +
      (row.side ? " · " + esc(row.side) : "") +
      (row.qty ? " · " + Number(row.qty).toLocaleString("en-IN") + " units" : "") + "</div>" +
      '<div class="pos-modal-line muted">Mark ' + price(row.current_price) +
      " · entry " + price(row.entry_price) +
      (row.expiry ? " · exp " + esc(String(row.expiry).slice(0, 10)) : "") + "</div>"
    );
  }

  function open(row, verb) {
    const spec = VERBS[verb];
    if (!spec) return;
    state.row = row;
    const modal = $(spec.modal);
    if (!modal) return;

    if (verb === "modify_sl") {
      $("pos-sl-context").innerHTML = contextHtml(row);
      $("pos-sl-value").value = row.stop_loss !== null && row.stop_loss !== undefined
        ? row.stop_loss : "";
      $("pos-sl-hint").textContent =
        "Must sit below the current mark (" + price(row.current_price) + ").";
    } else if (verb === "modify_target") {
      $("pos-target-context").innerHTML = contextHtml(row);
      $("pos-target-value").value = row.target !== null && row.target !== undefined
        ? row.target : "";
      $("pos-target-hint").textContent =
        "Must sit above the current mark (" + price(row.current_price) + ").";
    } else if (verb === "close_fraction") {
      $("pos-partial-context").innerHTML = contextHtml(row);
      const qty = Number(row.qty) || 0;
      const frac = Number($("pos-partial-value").value) || 0.5;
      $("pos-partial-hint").textContent = qty
        ? "≈ " + Math.max(1, Math.floor(qty * frac)).toLocaleString("en-IN") +
          " of " + qty.toLocaleString("en-IN") + " units"
        : "";
      if (row.kind === "option") {
        $("pos-partial-value").value = "1";
        $("pos-partial-note").innerHTML =
          "This is a multi-leg option structure: the legs close together, so only 100% is accepted. " +
          "Use <strong>Close All</strong> for a full exit.";
      }
    } else if (verb === "close_all") {
      $("pos-closeall-context").innerHTML = contextHtml(row);
    }
    modal.hidden = false;
    const input = modal.querySelector("input[type=number]");
    if (input) { try { input.focus(); input.select(); } catch (_e) { /* stub DOM */ } }
  }

  function close(modalId) {
    const modal = $(modalId);
    if (modal) modal.hidden = true;
    state.row = null;
  }

  async function submit(verb, body, successMessage) {
    if (state.busy) return null;
    state.busy = true;
    try {
      const data = await post(body);
      toast(successMessage(data), "success");
      if (typeof state.onDone === "function") state.onDone(data);
      return data;
    } catch (err) {
      // The server's own words: it validated against the live mark.
      toast(err.message, "error");
      return null;
    } finally {
      state.busy = false;
    }
  }

  function submitStop() {
    const row = state.row;
    if (!row) return;
    const raw = $("pos-sl-value").value;
    if (raw === "" || raw === null) { toast("Enter a stop level, or use Clear level.", "error"); return; }
    submit("modify_sl", {
      instance_id: row.instance_id,
      position_key: row.position_key || row.symbol,
      action: "modify_stop_loss",
      value: Number(raw),
    }, () => "Stop armed at " + price(Number(raw)) + " on " + (row.label || row.symbol))
      .then((ok) => { if (ok) close("pos-sl-modal"); });
  }

  function clearStop() {
    const row = state.row;
    if (!row) return;
    submit("modify_sl", {
      instance_id: row.instance_id,
      position_key: row.position_key || row.symbol,
      action: "clear_stop_loss",
    }, () => "Stop cleared on " + (row.label || row.symbol))
      .then((ok) => { if (ok) close("pos-sl-modal"); });
  }

  function submitTarget() {
    const row = state.row;
    if (!row) return;
    const raw = $("pos-target-value").value;
    if (raw === "" || raw === null) { toast("Enter a target level, or use Clear level.", "error"); return; }
    submit("modify_target", {
      instance_id: row.instance_id,
      position_key: row.position_key || row.symbol,
      action: "modify_target",
      value: Number(raw),
    }, () => "Target armed at " + price(Number(raw)) + " on " + (row.label || row.symbol))
      .then((ok) => { if (ok) close("pos-target-modal"); });
  }

  function clearTarget() {
    const row = state.row;
    if (!row) return;
    submit("modify_target", {
      instance_id: row.instance_id,
      position_key: row.position_key || row.symbol,
      action: "clear_target",
    }, () => "Target cleared on " + (row.label || row.symbol))
      .then((ok) => { if (ok) close("pos-target-modal"); });
  }

  function submitPartial() {
    const row = state.row;
    if (!row) return;
    const frac = Number($("pos-partial-value").value);
    if (!(frac > 0 && frac <= 1)) { toast("Fraction must be between 0 and 1 (e.g. 0.5).", "error"); return; }
    submit("close_fraction", {
      instance_id: row.instance_id,
      position_key: row.position_key || row.symbol,
      action: "close_fraction",
      fraction: frac,
    }, (data) => {
      const units = data.qty_closed !== null && data.qty_closed !== undefined
        ? Number(data.qty_closed).toLocaleString("en-IN") + " units @ " + price(data.price)
        : "the full structure";
      const left = Number(data.remaining_qty || 0);
      return "Closed " + Math.round(frac * 100) + "% (" + units + ")" +
        (left > 0 ? " — " + left.toLocaleString("en-IN") + " still working" : " — position flat");
    }).then((ok) => { if (ok) close("pos-partial-modal"); });
  }

  function submitCloseAll() {
    const row = state.row;
    if (!row) return;
    submit("close_all", {
      instance_id: row.instance_id,
      position_key: row.position_key || row.symbol,
      action: "close_all",
    }, (data) => {
      if (data.status === "placed") {
        return "Close order PLACED at the venue (coid " + String(data.coid).slice(0, 18) +
          ") — not filled yet";
      }
      const pnl = Number(row.unrealized_pnl || 0);
      return "Closed " + (row.label || row.symbol) + " @ " + price(data.price) +
        " (marked P&L " + (pnl >= 0 ? "+" : "") + money(pnl) + ")";
    }).then((ok) => { if (ok) close("pos-closeall-modal"); });
  }

  // ---------------------------------------------------------------- wiring

  function onTableClick(event) {
    const btn = event.target && event.target.closest
      ? event.target.closest("[data-pos-act]") : null;
    if (!btn || btn.disabled) return;
    const row = (state.rows || []).find(
      (r) => String(r.instance_id) === btn.dataset.id &&
        String(r.position_key || r.symbol) === btn.dataset.key
    );
    if (!row) { toast("That position is no longer in the book — refresh.", "error"); return; }
    open(row, btn.dataset.posAct);
  }

  /**
   * init({ rows, tableId, toast, onDone })
   *
   * `rows` is the flat positions array from the snapshot; it is replaced on
   * every render so a click always resolves against the LATEST book, not the
   * one that happened to be on screen when the page loaded.
   */
  function init(opts) {
    const options = opts || {};
    state.toast = options.toast || null;
    state.onDone = options.onDone || null;
    state.rows = options.rows || [];

    const table = $(options.tableId || "aggregate-positions");
    // Delegated once: rows are re-rendered every second, so per-button
    // listeners would be re-created (and leak) on every tick.
    if (table && !table._posActionsBound) {
      table.addEventListener("click", onTableClick);
      table._posActionsBound = true;
    }

    const bind = (id, fn) => {
      const el = $(id);
      if (el && !el._posActionsBound) {
        el.addEventListener("click", fn);
        el._posActionsBound = true;
      }
    };
    bind("pos-sl-submit", submitStop);
    bind("pos-sl-clear", clearStop);
    bind("pos-target-submit", submitTarget);
    bind("pos-target-clear", clearTarget);
    bind("pos-partial-submit", submitPartial);
    bind("pos-closeall-confirm", submitCloseAll);

    const fracInput = $("pos-partial-value");
    if (fracInput && !fracInput._posActionsBound) {
      fracInput.addEventListener("input", () => {
        if (state.row) open(state.row, "close_fraction");
      });
      fracInput._posActionsBound = true;
    }
    document.querySelectorAll(".pos-fraction").forEach((b) => {
      if (b._posActionsBound) return;
      b.addEventListener("click", () => {
        const input = $("pos-partial-value");
        if (input) input.value = b.dataset.fraction;
        if (state.row) open(state.row, "close_fraction");
      });
      b._posActionsBound = true;
    });

    // Escape closes whichever modal is open (keyboard parity with the ✕).
    if (!document._posActionsEsc) {
      document.addEventListener("keydown", (e) => {
        if (e.key !== "Escape") return;
        Object.values(VERBS).forEach((spec) => {
          const modal = $(spec.modal);
          if (modal && !modal.hidden) close(spec.modal);
        });
      });
      document._posActionsEsc = true;
    }
    return PositionActions;
  }

  return {
    init,
    open,
    close,
    buttonCell,
    rows: (rows) => { state.rows = rows || []; },
    _state: state,
  };
})();

if (typeof globalThis !== "undefined") globalThis.PositionActions = PositionActions;
