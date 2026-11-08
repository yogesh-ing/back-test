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
 * source of truth for structure ids and labels — and on components/labels.js
 * to turn a structure id into that vocabulary's label.
 */
const OptionView = (function () {
  "use strict";

  // option_config.js must load first: Labels.structure resolves a structure id
  // against its STRUCTURES vocabulary, and without it every id would degrade to
  // a guess. labels.js carries the resolver itself, so this file has no second
  // copy of the vocabulary or the casing rules.
  if (typeof OptionConfig === "undefined"
      && !(typeof window !== "undefined" && window.OptionConfig)) {
    throw new Error("option_view.js requires option_config.js");
  }
  const House = (typeof Labels !== "undefined") ? Labels
    : (typeof window !== "undefined" && window.Labels) || null;
  if (!House) throw new Error("option_view.js requires components/labels.js");

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
    return House.structureShort(type || "structure");
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

  /**
   * Sprint A1 — can this book see the market at all?
   *
   * `feed_state` is orthogonal to `status`: `status` is the operator's intent
   * (do I want this trading), `feed_state` is the pipe (can it see the market).
   * A book restored before its broker logged in is WAITING_FOR_FEED — on
   * screen, holding its last honest mark, refusing every new entry. Before A1
   * it was not on screen at all: construction failed and the manager filed it
   * under `_parked_snapshots`, so ten live paper books vanished from boot until
   * the next restart *after* a login.
   *
   * Distinct from a stale mark (the feed died mid-session) and from PAUSED (the
   * operator's choice) — and it has a different fix: log the broker in.
   */
  function isFeedWaiting(row) {
    if (!row) return false;
    if (row.feed_disconnected) return true;
    return String(row.feed_state || "").toUpperCase() === "WAITING_FOR_FEED";
  }

  const BROKER_LABELS = { mstock: "mStock", dhan: "Dhan", upstox: "Upstox" };

  /** "Waiting for mStock login" — names the broker the operator must log into. */
  function feedWaitLabel(row) {
    const source = String((row && row.source) || "").toLowerCase();
    return "Waiting for " + (BROKER_LABELS[source] || source || "broker") + " login";
  }

  /** Status-cell sub-line for a blind book; "" for a connected one. */
  function feedStateBadgeHtml(row) {
    if (!isFeedWaiting(row)) return "";
    const detail = (row && row.feed_refusal) || "";
    const title = "Marks are frozen at their last honest value and new entries are refused." +
      (detail ? " " + detail : "");
    return '<div class="cell-sub badge-waiting-feed" title="' + escAttr(title) + '">' +
      "🔌 " + escAttr(feedWaitLabel(row)) + " · frozen mark</div>";
  }

  /** "HH:MM:SS" in minutes-since-midnight, or the fallback when absent. */
  function clockMinutes(hhmmss, fallback) {
    if (typeof hhmmss !== "string") return fallback;
    const parts = hhmmss.split(":").map(Number);
    if (parts.length < 2 || parts.some(isNaN)) return fallback;
    return parts[0] * 60 + parts[1];
  }

  /**
   * Sprint D / Ticket D2: compute the IST auto-squareoff clock from
   * `intraday_squareoff_minutes_before` against that session's own close —
   * 15:30 IST normally, 15:40 on a declared abridged session (Muhurat).
   */
  function squareoffTimeLabel(minutesBefore, sessionCloseHhmmss) {
    const mins = Number.isFinite(Number(minutesBefore)) && Number(minutesBefore) > 0
      ? Math.round(Number(minutesBefore))
      : 20;
    const closeMin = clockMinutes(sessionCloseHhmmss, 15 * 60 + 30);
    const total = Math.max(9 * 60 + 15, closeMin - mins);
    const hh = String(Math.floor(total / 60)).padStart(2, "0");
    const mm = String(total % 60).padStart(2, "0");
    return hh + ":" + mm;
  }

  /** Resolve canonical carry_policy ("intraday" | "positional" | "") for a position or runner row. */
  function carryPolicyOf(item) {
    if (!item) return "";
    const direct = String(item.carry_policy || "").trim().toLowerCase();
    if (direct === "intraday" || direct === "positional") return direct;
    const b = book(item);
    const fromBook = b && String(b.carry_policy || "").trim().toLowerCase();
    if (fromBook === "intraday" || fromBook === "positional") return fromBook;
    if (isOption(item)) {
      const expr = (item.instrument && item.instrument.expression) || {};
      const fromExpr = String(expr.carry_policy || "").trim().toLowerCase();
      if (fromExpr === "intraday" || fromExpr === "positional") return fromExpr;
    }
    return "";
  }

  /**
   * Sprint D / Ticket D2 & D3: Carry-horizon badge HTML.
   * - `carry_policy="intraday"`   -> `⏱ INTRADAY 15:10`
   * - `carry_policy="positional"` -> `📅 POSITIONAL (HOLD TO EXPIRY)` (or `📅 POSITIONAL` when `opts.compact`)
   */
  function carryPolicyBadgeHtml(item, opts) {
    const policy = carryPolicyOf(item);
    if (!policy) return "";
    const compact = !!(opts && opts.compact);
    const b = book(item);
    const minsBefore = item.intraday_squareoff_minutes_before != null
      ? item.intraday_squareoff_minutes_before
      : (b && b.intraday_squareoff_minutes_before);
    if (policy === "positional") {
      const text = compact ? "📅 POSITIONAL" : "📅 POSITIONAL (HOLD TO EXPIRY)";
      const title = "Positional carry contract (carry_policy=positional) — held across sessions until exit rule or expiry settlement";
      return ' <span class="badge badge-carry-positional" title="' + escAttr(title) + '">' + text + "</span>";
    }
    const sessionClose = (typeof window !== "undefined" && window.__lastMarketState)
      ? window.__lastMarketState.session_close
      : null;
    const sqTime = squareoffTimeLabel(minsBefore, sessionClose);
    const title = "Intraday carry contract (carry_policy=intraday) — auto square-off at " + sqTime + " IST while runner is RUNNING";
    return ' <span class="badge badge-carry-intraday" title="' + escAttr(title) + '">⏱ INTRADAY ' + sqTime + "</span>";
  }

  function fmtSignedInr(v) {
    const n = num(v);
    const abs = Math.abs(n).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    return (n > 0 ? "+₹" : (n < 0 ? "−₹" : "₹")) + abs;
  }

  /**
   * Sprint D / Ticket D2: Today vs. Total Trade P&L + 15:30 prev_close_mark
   * sub-line for open option structure rows on the Positions blotter.
   */
  function carryPnlSubHtml(row) {
    if (!row || row.kind !== "option") return "";
    const hasToday = row.today_pnl !== null && row.today_pnl !== undefined;
    const hasNet = row.net_open_pnl !== null && row.net_open_pnl !== undefined;
    const hasPrev = row.prev_close_mark !== null && row.prev_close_mark !== undefined;
    if (!hasToday && !hasNet && !hasPrev) return "";
    if (row.carried_overnight || hasPrev) {
      const parts = [];
      if (hasToday) parts.push("Today " + fmtSignedInr(row.today_pnl));
      if (hasNet) parts.push("Total " + fmtSignedInr(row.net_open_pnl));
      const titleParts = ["Carried overnight" + (row.entry_session_date ? " from " + row.entry_session_date : "")];
      if (hasPrev) titleParts.push("15:30 prev close mark ₹" + num(row.prev_close_mark).toFixed(2));
      return '<div class="cell-sub pos-pnl-split" title="' + escAttr(titleParts.join(" · ")) + '">' +
        escAttr(parts.join(" · ")) +
        (hasPrev ? ' <span class="muted">(prev ' + escAttr(signText(row.prev_close_mark)) + ")</span>" : "") +
        "</div>";
    }
    if (hasNet) {
      return '<div class="cell-sub pos-pnl-split" title="Opened today — Today P&L equals Total Net Open P&L after entry commission">' +
        "Net " + escAttr(fmtSignedInr(row.net_open_pnl)) + "</div>";
    }
    return "";
  }

  /** True when a runner's frozen spawn config/template has drifted from disk (Ticket D3). */
  function hasConfigDrift(row) {
    if (!row) return false;
    return !!(row.config_drift || row.strategy_code_changed);
  }

  /**
   * Sprint D / Ticket D3: Amber config-drift pill for FROZEN_AT_SPAWN runners
   * whose strategy code or template expression/carry_policy defaults changed.
   */
  function configDriftBadgeHtml(row) {
    if (!hasConfigDrift(row)) return "";
    const reasons = Array.isArray(row.config_drift_reasons) && row.config_drift_reasons.length
      ? row.config_drift_reasons.join("; ")
      : "Strategy template on disk changed since this runner was spawned";
    const openPos = num(row.open_positions);
    const gateNote = openPos > 0
      ? " Close or wait for " + openPos + " open position(s) to exit before respawning."
      : " Runner is flat — click Respawn to rollover onto the latest rules.";
    return '<div class="cell-sub badge-config-drift" title="' + escAttr(reasons + "." + gateNote) + '">' +
      "⚠️ Outdated config — respawn when flat</div>";
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
        value: num(b.net_realized_pnl),
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
    isFeedWaiting,
    feedWaitLabel,
    feedStateBadgeHtml,
    squareoffTimeLabel,
    carryPolicyOf,
    carryPolicyBadgeHtml,
    carryPnlSubHtml,
    hasConfigDrift,
    configDriftBadgeHtml,
  };
})();

// Browser global; the Node harness reads the same file and evaluates it.
if (typeof window !== "undefined") window.OptionView = OptionView;
if (typeof module !== "undefined" && module.exports) module.exports = OptionView;
