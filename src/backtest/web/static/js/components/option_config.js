/**
 * Option structure vocabulary (display only).
 *
 * This module used to be the spawn form's form ⇄ payload translation: it
 * BUILT `instrument.expression` blocks (hardcoded direction-aware spreads
 * included) for POST /api/portfolio/runner/create. Phase 3 of the
 * execution refactor deleted that path — the spawn form now sends field
 * selections only and the strategy's declared contract resolves
 * server-side, so no client code may construct an expression. What
 * remains is the read-only structure vocabulary the option book views
 * label positions with.
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
    { id: "long_straddle", label: "Long straddle" },
    { id: "short_straddle", label: "Short straddle" },
    { id: "strangle", label: "Short strangle" },
  ];

  // What the synthetic chain can actually price, so views can warn honestly.
  const SYNTHETIC_UNDERLYINGS = ["NIFTY", "BANKNIFTY"];

  function isOption(instrumentType) {
    return String(instrumentType || "equity").toLowerCase() === "option";
  }

  return {
    STRUCTURES,
    SYNTHETIC_UNDERLYINGS,
    isOption,
  };
})();

// Browser global; the Node harness reads the same file and evaluates it.
if (typeof window !== "undefined") window.OptionConfig = OptionConfig;
if (typeof module !== "undefined" && module.exports) module.exports = OptionConfig;
