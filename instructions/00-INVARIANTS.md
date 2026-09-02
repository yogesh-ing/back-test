# Card 00 — Invariants (the contract that must never drift)

**Prerequisite:** none. **Applies to:** every other card.
These are the highest-risk rules. Encode them exactly; Card 06 tests pin them.

1. **No look-ahead.** Position held during bar *t* = strategy target from bar
   *t-1* (`target.shift(1)`, first bar = 0). A signal from bar *t*'s close may
   only be traded from *t+1*.
2. **Bar return** is close-to-close: `close.pct_change()`.
3. **Costs** per bar = `(commission_pct + slippage_pct) × |Δ held_position|`.
   Turnover 1.0 (full entry or exit) pays the full rate once.
4. **Equity** = `initial_capital × cumprod(1 + net)`, where
   `net = held × bar_return − costs`.
5. **Stop-loss / take-profit** (when set) are engine-enforced, **intrabar**,
   relative to **entry price** (= close of the bar where the position opened):
   - Long: stop `entry×(1−sl)` vs bar `low`; target `entry×(1+tp)` vs bar `high`.
   - Short: mirror (stop `entry×(1+sl)` vs `high`; target `entry×(1−tp)` vs `low`).
   - **Same-bar conservative rule:** if both could trigger, **stop-loss fills
     first**.
   - On a forced exit: pay exit turnover cost, go flat, and **block re-entry**
     until the target returns to flat.
   - Must hold: a pure stop-out at zero cost ⇒ whole-trade return **exactly
     −stop_loss**; a pure target hit ⇒ **+take_profit**.
6. **Signals** ∈ {−1, 0, +1}; NaN ⇒ flat (0). Shorts (−1) must work.
7. **Strategies are pure:** same candles + params ⇒ same signals.
8. **Canonical candle frame everywhere:** tz-naive, ascending, unique
   `DatetimeIndex`; lowercase columns `open, high, low, close, volume`; numeric.

**Verify:** nothing to run yet — but every later Verify step depends on these.
