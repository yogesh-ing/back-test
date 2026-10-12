/* Options reporting component (options forward testing, task C2).
 *
 * The portfolio matrix and the deep-dive drawer are equity-shaped: they read
 * `symbol`, `qty`, `entry_price`, `unrealized_pnl`. An option runner's
 * structures live in the bridge, not in the runner's equity positions, so
 * before this component an option runner rendered as an equity runner with
 * nothing open — "Positions 0", no structure, no premium, no expiry.
 *
 * Everything here is **pure**: it turns the runner row the API already sends
 * (`row.options.open_structures_detail`, task C2) into numbers and labels.
 * The views (`portfolio.js`, `deep_dive.js`) only add markup, and node can
 * test this file directly with no DOM.
 *
 * Depends on components/option_config.js for the structure vocabulary — one
 * source of truth for structure ids and labels.
 */
const OptionView = (function () {
  "use strict";

  const Config = (typeof OptionConfig !== "undefined") ? OptionConfig
    : (typeof window !== "undefined" && window.OptionConfig) || null;
  if (!Config) throw new Error("option_view.js requires option_config.js");

  // Plain-language exit reasons (mirrors options/exit_policy.py + expiry.py).
  const EXIT_LABELS = {
    manual: "Manual",
    signal_flip: "View flipped",
    signal_neutral: "View went neutral",
    risk_cap: "Risk cap",
    stop_loss: "Stop loss",
    take_profit: "Take profit",
    time_stop: "Time stop",
    auto_square_off: "Squared off pre-expiry",
    expiry_settlement: "Expiry settlement",
    // Operator-set levels (Live Order Management) — distinct from the
    // playbook's own stop/target so the trade log says WHO closed it.
    manual_stop_loss: "Manual stop-loss",
    manual_target: "Manual target",
  };

  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

  function isOption(row) {
    return !!(row && row.instrument && String(row.instrument.type).toLowerCase() === "option");
  }

  function book(row) {
    return (row && row.options) || null;
  }

  /** Open structures, each with legs/strikes/premium/expiry (see the bridge). */
  function openStructures(row) {
    const b = book(row);
    return (b && b.open_structures_detail) || [];
  }

  function openLegs(row) {
    return openStructures(row).reduce((n, s) => n + (s.legs || 0), 0);
  }

  /** Capital genuinely committed: net premium paid for what is open. */
  function premiumAtRisk(row) {
    return openStructures(row).reduce((n, s) => n + (s.entry_cost || 0), 0);
  }

  /** Mark-to-market P&L of the open book. */
  function bookPnl(row) {
    const b = book(row);
    if (!b) return 0;
    if (typeof b.unrealized_pnl === "number") return b.unrealized_pnl;
    return openStructures(row).reduce((n, s) => n + (s.unrealized_pnl || 0), 0);
  }

  function num(v) {
    const n = Number(v);
    return Number.isFinite(n) ? n : 0;
  }

  /** "29 Oct" — short, sortable-looking, unambiguous about the month. */
  function expiryLabel(iso) {
    if (!iso) return "—";
    const m = String(iso).match(/^(\d{4})-(\d{2})-(\d{2})/);
    if (!m) return String(iso);
    const day = parseInt(m[3], 10);
    return day + " " + MONTHS[parseInt(m[2], 10) - 1];
  }

  function structureLabel(type) {
    const found = Config.STRUCTURES.find((s) => s.id === type);
    if (found) return found.label.split(" (")[0];
    return String(type || "structure").replace(/_/g, " ");
  }

  /** The option-type side of a structure: "CE", "PE" or "CE/PE" for mixed. */
  function optionTypes(structure) {
    const seen = [];
    ((structure && structure.legs_detail) || []).forEach((leg) => {
      if (leg.option_type && seen.indexOf(leg.option_type) === -1) seen.push(leg.option_type);
    });
    return seen.join("/");
  }

  function strikesText(structure) {
    const strikes = (structure && structure.strikes) || [];
    if (!strikes.length) return "—";
    // 10,150 rather than 10150 — these are index levels.
    return strikes.map((s) => Number(s).toLocaleString("en-IN")).join("/");
  }

  /** One-line description of an open structure, for a matrix sub-row. */
  function structureLine(structure) {
    if (!structure) return "";
    const parts = [
      structureLabel(structure.structure_type),
      strikesText(structure) + (optionTypes(structure) ? " " + optionTypes(structure) : ""),
      num(structure.qty) + " lot" + (num(structure.qty) === 1 ? "" : "s"),
      "exp " + expiryLabel(structure.expiry),
    ];
    if (num(structure.units)) {
      parts.push(
        "premium " + signText(structure.entry_price) + " → " + signText(structure.current_price)
      );
    }
    return parts.join(" · ");
  }

  function signText(v) {
    const n = num(v);
    return (n < 0 ? "−" : "") + Math.abs(n).toFixed(2);
  }

  function escAttr(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/"/g, "&quot;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  /** Format an ISO-8601 UTC mark_ts into a compact local timestamp for R2 tooltips. */
  function formatMarkTs(iso) {
    if (!iso) return "never";
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return String(iso);
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
  }

  /** R2 hover tooltip: `last good quote <mark_ts local> — <quote_error>`. */
  function staleTooltip(item) {
    if (!item) return "";
    const ts = formatMarkTs(item.mark_ts != null ? item.mark_ts : item.markTs);
    const err = (item.quote_error != null ? item.quote_error : item.quoteError) || "quote failed";
    return "last good quote " + ts + " — " + err;
  }

  /** R2 badge HTML rendered next to a stale mark price. */
  function staleBadgeHtml(item) {
    const isStale = !!(item && (item.mark_stale || item.markStale));
    if (!isStale) return "";
    return ' <span class="badge badge-stale-mark" title="' + escAttr(staleTooltip(item)) + '">⚠ stale mark</span>';
  }

  /** True when a runner or its option book has any stale mark (R1/R2/R7). */
  function isMarkStale(row) {
    if (!row) return false;
    if (row.mark_stale) return true;
    const b = book(row);
    if (b && b.mark_stale) return true;
    return openStructures(row).some((s) => !!(s && s.mark_stale));
  }

  /** Count of stale option structures on a runner row. */
  function staleCount(row) {
    if (!row) return 0;
    if (typeof row.stale_positions === "number") return row.stale_positions;
    const b = book(row);
    if (b && typeof b.stale_positions === "number") return b.stale_positions;
    return openStructures(row).filter((s) => !!(s && s.mark_stale)).length;
  }

  /** Flat rows for the deep-dive open-position table. */
  function structureRows(row) {
    return openStructures(row).map((s) => ({
      kind: "option",
      symbol: s.symbol,
      label: s.label,
      structure: structureLabel(s.structure_type),
      structureType: s.structure_type,
      strikes: strikesText(s),
      optionType: optionTypes(s),
      side: s.side,
      lots: num(s.qty),
      units: num(s.units),
      lotSize: num(s.lot_size),
      entryPrice: num(s.entry_price),
      currentPrice: num(s.current_price),
      entryCost: num(s.entry_cost),
      pnl: num(s.unrealized_pnl),
      pnlPct: num(s.open_pnl_pct),
      expiry: s.expiry,
      expiryText: expiryLabel(s.expiry),
      barsHeld: num(s.bars_held),
      markTs: s.mark_ts || null,
      markStale: !!s.mark_stale,
      quoteError: s.quote_error || null,
      legs: (s.legs_detail || []).map((leg) => ({
        side: leg.side,
        optionType: leg.option_type,
        strike: num(leg.strike),
        tradingSymbol: leg.trading_symbol,
        qty: num(leg.qty),
        entryPrice: num(leg.entry_price),
        currentPrice: num(leg.current_price),
        pnl: num(leg.pnl),
        markTs: leg.mark_ts || null,
        markStale: !!leg.mark_stale,
        quoteError: leg.quote_error || null,
      })),
    }));
  }

  /** Whether a (possibly closed) trade record is an option structure. */
  function isOptionTrade(trade) {
    return !!(trade && String(trade.kind || "").toLowerCase() === "option");
  }

  function exitReasonLabel(reason) {
    if (!reason) return "—";
    return EXIT_LABELS[reason] || String(reason).replace(/_/g, " ");
  }

  function tradeLine(trade) {
    if (!isOptionTrade(trade)) return trade ? trade.symbol : "";
    return (trade.label || trade.symbol || "") + " · " + exitReasonLabel(trade.exit_reason);
  }

  /**
   * Book-level stats for the drawer strip: what is open, what it cost, what it
   * is worth, and how the closed book has done. Values are numbers/strings —
   * the view formats the money.
   */
  function bookStats(row) {
    const b = book(row);
    const structures = openStructures(row);
    if (!b) return [];
    const closed = num(b.closed_structures);
    return [
      { key: "open_structures", label: "Open Structures", value: num(b.open_structures) },
      { key: "open_legs", label: "Open Legs", value: openLegs(row) },
      { key: "premium_at_risk", label: "Premium at Risk", value: premiumAtRisk(row), money: true },
      { key: "book_pnl", label: "Book P&L (Open)", value: bookPnl(row), money: true, pnl: true },
      {
        key: "next_expiry",
        label: "Next Expiry",
        value: structures.length ? expiryLabel(structures[0].expiry) : "—",
      },
      { key: "closed", label: "Closed Structures", value: closed },
      {
        key: "book_realized",
        label: "Book P&L (Closed)",
        value: num(b.total_pnl),
        money: true,
        pnl: true,
        sub: closed ? Math.round(num(b.win_rate) * 100) + "% won · settled " + num(b.settled_count) : "",
      },
    ];
  }

  /** Matrix "Positions" cell: structures, with legs as the sub-line. */
  function positionsCell(row) {
    if (!isOption(row)) {
      return { primary: num(row && row.open_positions), sub: "" };
    }
    const structures = openStructures(row);
    const count = structures.length || num(book(row) && book(row).open_structures);
    return {
      primary: count,
      sub: count ? openLegs(row) + " leg" + (openLegs(row) === 1 ? "" : "s") : "",
    };
  }

  /**
   * Matrix sub-lines for an option runner: the open structure(s) and the
   * premium they tie up. Empty for equity runners (their cell is unchanged).
   */
  function matrixNotes(row) {
    if (!isOption(row)) return [];
    const structures = openStructures(row);
    if (!structures.length) {
      const b = book(row);
      const closed = num(b && b.closed_structures);
      return [closed ? "flat · " + closed + " closed" : "flat · waiting for a view"];
    }
    const notes = structures.map(structureLine);
    const risk = premiumAtRisk(row);
    if (risk) {
      notes.push("premium at risk " + risk.toFixed(0) + " · " + openLegs(row) + " legs");
    }
    return notes;
  }

  /** Type column text: the structure for option runners, "" for equity. */
  function instrumentLabel(row) {
    if (!isOption(row)) return "";
    const structures = openStructures(row);
    if (structures.length) return structureLabel(structures[0].structure_type);
    const cfg = (row.instrument && row.instrument.expression) || {};
    const type = cfg.type;
    if (type && typeof type === "object") {
      // Direction-aware: the structure is chosen per view at entry time.
      return "Direction-aware";
    }
    return structureLabel(type);
  }

  return {
    EXIT_LABELS,
    isOption,
    book,
    openStructures,
    openLegs,
    premiumAtRisk,
    bookPnl,
    expiryLabel,
    structureLabel,
    optionTypes,
    strikesText,
    structureLine,
    structureRows,
    isOptionTrade,
    exitReasonLabel,
    tradeLine,
    bookStats,
    positionsCell,
    matrixNotes,
    instrumentLabel,
    formatMarkTs,
    staleTooltip,
    staleBadgeHtml,
    isMarkStale,
    staleCount,
  };
})();

// Browser global; the Node harness reads the same file and evaluates it.
if (typeof window !== "undefined") window.OptionView = OptionView;
if (typeof module !== "undefined" && module.exports) module.exports = OptionView;
