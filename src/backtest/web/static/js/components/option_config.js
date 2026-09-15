/**
 * Option-runner spawn config (options forward testing, task C1).
 *
 * The forward engine has accepted `instrument: {"type": "option", ...}` on
 * `POST /api/portfolio/runner/create` since the Gap remediation, but nothing in
 * the UI ever sent it — `portfolio.js` built its payload from the spawn form and
 * simply omitted `instrument`, so an option runner could only be created by
 * hand-rolled JSON. This module is the missing form ⇄ payload translation, kept
 * pure so the browser and the Node harness (tests/js/test_option_config.mjs)
 * exercise exactly the same code.
 *
 * Design notes:
 *
 * - **Explicit over implicit** (the same rule ticket #10 applied to bucket
 *   mode): every option the user can see is written into the payload, so a
 *   spawned runner never behaves differently from what the form showed.
 * - **Blank means "not set"**, never zero: a blank stop loss must not become
 *   `0`, which the exit policy would read as "disabled" at best and a
 *   hair-trigger at worst.
 * - `null` is meaningful: `min_days_to_expiry: null` sends the runner into
 *   settlement instead of squaring off a day early.
 */
const OptionConfig = (() => {
  "use strict";

  // Mirrors STRUCTURES in src/backtest/forward/options_bridge.py — keep in sync.
  const STRUCTURES = [
    { id: "direction_aware", label: "Direction-aware (bull → call spread, bear → put spread)" },
    { id: "bull_call_spread", label: "Bull call spread" },
    { id: "bear_put_spread", label: "Bear put spread" },
    { id: "long_call", label: "Long call" },
    { id: "long_put", label: "Long put" },
  ];

  const FIXED_STRUCTURES = STRUCTURES.filter((s) => s.id !== "direction_aware").map((s) => s.id);

  // What the synthetic chain can actually price, so the form can warn honestly.
  const SYNTHETIC_UNDERLYINGS = ["NIFTY", "BANKNIFTY"];

  function isOption(instrumentType) {
    return String(instrumentType || "equity").toLowerCase() === "option";
  }

  function toFloat(value) {
    if (value === null || value === undefined || value === "") return null;
    const num = Number(value);
    return Number.isFinite(num) ? num : null;
  }

  function toInt(value) {
    const num = toFloat(value);
    return num === null ? null : Math.trunc(num);
  }

  /**
   * Build the `expression` block the backend expects.
   * Only non-empty settings are emitted; `exit` is omitted when it is empty so
   * the runner keeps the engine defaults (flip-exit on, square off 1 day early).
   */
  function buildExpression(cfg) {
    cfg = cfg || {};
    const expression = {};

    if (cfg.structure && cfg.structure !== "direction_aware") {
      expression.type = cfg.structure;
    } else {
      // Direction-aware is also the backend default; sending it keeps the
      // payload self-describing for anyone reading the audit log.
      expression.type = { BULLISH: "bull_call_spread", BEARISH: "bear_put_spread" };
    }

    const selection = String(cfg.strikeSelection || "atm").toLowerCase();
    expression.strike_selection = selection === "delta" ? "delta" : "atm";
    if (selection === "delta") {
      const target = toFloat(cfg.deltaTarget);
      expression.delta_target = target === null ? 0.35 : target;
    }

    const quantity = toInt(cfg.quantity);
    expression.quantity = quantity && quantity > 0 ? quantity : 1;

    const exit = {};
    // The checkbox is the switch: unchecked means "close when the view flips",
    // which is the engine's own default, so it is only sent when turned off.
    if (cfg.signalFlip === false) exit.signal_flip = false;

    const neutralBars = toInt(cfg.neutralBars);
    if (neutralBars !== null && neutralBars > 0) exit.neutral_bars = neutralBars;

    const stopPct = toFloat(cfg.stopLossPct);
    if (stopPct !== null) exit.stop_loss_pct = stopPct / 100; // form shows percent

    const targetPct = toFloat(cfg.takeProfitPct);
    if (targetPct !== null) exit.take_profit_pct = targetPct / 100;

    const maxBars = toInt(cfg.maxBars);
    if (maxBars !== null) exit.max_bars = maxBars;

    if (cfg.rideToSettlement === true) {
      exit.min_days_to_expiry = null; // explicit null = settle, don't square off
    } else {
      const dte = toInt(cfg.minDaysToExpiry);
      exit.min_days_to_expiry = dte === null ? 1 : dte;
    }

    if (cfg.reenter === true) exit.reenter = true;

    expression.exit = exit;
    return expression;
  }

  /** Build the `instrument` block for the create-runner payload. */
  function buildInstrument(cfg) {
    cfg = cfg || {};
    if (!isOption(cfg.instrumentType)) return { type: "equity" };
    return { type: "option", expression: buildExpression(cfg) };
  }

  /**
   * Problems that should block a deploy, phrased for the person who typed them.
   * Returns [] when the form is deployable.
   */
  function validate(cfg) {
    cfg = cfg || {};
    const problems = [];

    if (!isOption(cfg.instrumentType)) return problems;

    if (cfg.targetType === "pool") {
      problems.push(
        "Option runners trade a single underlying — pool mode would never open a " +
        "position (only the risk exits would fire). Pick Single Symbol.",
      );
    }

    const underlying = String(cfg.underlying || "").toUpperCase();
    if (!underlying) {
      problems.push("Underlying is required for an option runner (e.g. NIFTY).");
    } else if (
      cfg.source !== "mstock" &&
      SYNTHETIC_UNDERLYINGS.indexOf(underlying) === -1
    ) {
      problems.push(
        `The synthetic option chain only prices ${SYNTHETIC_UNDERLYINGS.join(" / ")}. ` +
        `Use those, or spawn with the mstock source.`,
      );
    }

    if (cfg.structure && cfg.structure !== "direction_aware" &&
        FIXED_STRUCTURES.indexOf(cfg.structure) === -1) {
      problems.push(`Unknown structure "${cfg.structure}".`);
    }

    if (String(cfg.strikeSelection || "atm").toLowerCase() === "delta") {
      const target = toFloat(cfg.deltaTarget);
      if (target !== null && (target <= 0 || target > 1)) {
        problems.push("Delta target must be between 0 and 1 (0.35 ≈ slightly OTM).");
      }
    }

    const quantity = toInt(cfg.quantity);
    if (quantity !== null && quantity < 1) {
      problems.push("Lots per leg must be at least 1.");
    }

    const stopPct = toFloat(cfg.stopLossPct);
    if (stopPct !== null && stopPct <= 0) {
      problems.push("Stop loss must be a positive percent of the premium paid.");
    }
    const targetPct = toFloat(cfg.takeProfitPct);
    if (targetPct !== null && targetPct <= 0) {
      problems.push("Take profit must be a positive percent of the premium paid.");
    }

    const neutralBars = toInt(cfg.neutralBars);
    if (neutralBars !== null && neutralBars < 0) {
      problems.push("'Close after N flat bars' cannot be negative.");
    }

    const dte = toInt(cfg.minDaysToExpiry);
    if (dte !== null && dte < 0) {
      problems.push("Square off days before expiry cannot be negative.");
    }

    return problems;
  }

  /** One-line description of a spawn, for the audit line and the toast. */
  function summarize(cfg) {
    cfg = cfg || {};
    if (!isOption(cfg.instrumentType)) return "equity";

    const structure = (STRUCTURES.find((s) => s.id === cfg.structure) || STRUCTURES[0]).label;
    const short = structure.split(" (")[0];
    const parts = [
      `option · ${short}`,
      String(cfg.strikeSelection || "atm").toLowerCase() === "delta"
        ? `Δ${toFloat(cfg.deltaTarget) === null ? 0.35 : toFloat(cfg.deltaTarget)}`
        : "ATM",
      `${toInt(cfg.quantity) && toInt(cfg.quantity) > 0 ? toInt(cfg.quantity) : 1} lot(s)`,
    ];
    const exit = [];
    if (cfg.signalFlip === false) exit.push("hold through flips");
    if (cfg.stopLossPct) exit.push(`stop ${cfg.stopLossPct}%`);
    if (cfg.takeProfitPct) exit.push(`target ${cfg.takeProfitPct}%`);
    if (cfg.rideToSettlement === true) exit.push("ride to settlement");
    else exit.push(`square off ${toInt(cfg.minDaysToExpiry) === null ? 1 : toInt(cfg.minDaysToExpiry)}d early`);
    if (cfg.reenter === true) exit.push("reverse on flip");
    parts.push(exit.join(", "));
    return parts.join(" · ");
  }

  return {
    STRUCTURES,
    FIXED_STRUCTURES,
    SYNTHETIC_UNDERLYINGS,
    isOption,
    buildExpression,
    buildInstrument,
    validate,
    summarize,
  };
})();

// Browser global; the Node harness reads the same file and evaluates it.
if (typeof window !== "undefined") window.OptionConfig = OptionConfig;
if (typeof module !== "undefined" && module.exports) module.exports = OptionConfig;
