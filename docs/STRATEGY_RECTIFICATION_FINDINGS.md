# Strategy Rectification Findings — Reference Document

**Purpose:** Consolidated findings from reviewing the three short-structure plugins
(`immediate_strangle`, `strangle_wing_points`, `straddle_short_atm`) against the
`StructureStrategy` template contract (`src/backtest/strategy/templates/__init__.py`).

Use this as a checklist when rectifying **any** strategy, and when updating the
templates so new strategies inherit these fixes by default.

---

## 1. Findings per strategy

### immediate_strangle (`plugins/strategies/immediate_strangle.py`)

| Area | Finding | Severity | Fix |
|------|---------|----------|-----|
| Exit units | `stop_loss_points: 13`, `take_profit_points: 7` — comments explain 1 pt ≈ ₹150 (1 lot × 150 units). Easy to accidentally enter rupees (e.g. 2000) which the risk gate would refuse. | High | Add explicit `unit: "points"` field; parameterize all exit values. |
| Hardcoded values | Exit bracket values are hardcoded in `execution.exit`. | High | Convert to `params` with min/max/tooltip. |
| No trailing stop | Nothing tightens the stop as premium decays. | Medium | Add `trailing_stop_points` + `trailing_activation_points`. |
| No time stop | Runs to expiry; no EOD squareoff before market close (expiry-day last-hour risk). | High | Add `squareoff_minutes_before_close` (e.g. 30 min before 15:30). |
| No IV entry filter | Enters unconditionally regardless of IV environment. | Medium | Add `min_iv_percentile` / `max_iv_percentile`. |
| Alert hooks | Has `on_alert`/`on_alert_resolved`/`subscribed_alerts` (good — keep as reference). | — | Replicate to the other two. |
| `build_opening_intent` | Custom override with `distance_pct` — correct. | — | Keep. |

### strangle_wing_points (`plugins/strategies/strangle_wing_points.py`)

| Area | Finding | Severity | Fix |
|------|---------|----------|-----|
| Exit units | Same `stop_loss_points: 13` / `take_profit_points: 7` footgun. | High | Add `unit: "points"`; parameterize. |
| Hardcoded values | Exit bracket values hardcoded. | High | Convert to `params`. |
| No trailing stop | — | Medium | Add `trailing_stop_points` + `trailing_activation_points`. |
| No time stop | — | High | Add `squareoff_minutes_before_close`. |
| No IV entry filter | — | Medium | Add `min_iv_percentile` / `max_iv_percentile`. |
| No alert hooks | Missing `on_alert`/`on_alert_resolved`/`subscribed_alerts` despite being short-gamma. | High | Add (copy pattern from `immediate_strangle`). |
| `build_opening_intent` | Uses base `StructureStrategy.build_opening_intent` — correct but lacks `distance_pct` injection (wing is parameterized at runtime). | Medium | Override to inject `distance_pct` from `wing_points`/spot. |

### straddle_short_atm (`plugins/strategies/straddle_short_atm.py`)

| Area | Finding | Severity | Fix |
|------|---------|----------|-----|
| Exit units | Same footgun (`stop_loss_points: 13`, `take_profit_points: 7`). | High | Add `unit: "points"`; parameterize. |
| Hardcoded values | Exit bracket values hardcoded. | High | Convert to `params`. |
| No trailing stop | — | Medium | Add `trailing_stop_points` + `trailing_activation_points`. |
| No time stop | — | High | Add `squareoff_minutes_before_close`. |
| No IV entry filter | — | Medium | Add `min_iv_percentile` / `max_iv_percentile`. |
| No alert hooks | Missing `on_alert`/`on_alert_resolved`/`subscribed_alerts`. | High | Add. |
| `parameters` is a string | Line 59: `parameters = "underlying=NIFTY."` — should be a description string like the others. | Low | Fix to string description. |
| `build_opening_intent` | Uses base — lacks `distance_pct` (not needed for ATM, but consistent to override). | Low | Override for consistency. |

---

## 2. Cross-cutting template gaps (update `templates/`)

The `StructureStrategy` template (`src/backtest/strategy/templates/__init__.py`) does not enforce or provide:

1. **Exit unit field** — no `unit` key in `execution.exit`; strategies hardcode "points" or rupees inconsistently.
2. **Trailing stop** — not part of the standard exit schema.
3. **Time-based squareoff** — not part of the standard exit schema.
4. **IV entry filter** — not part of params schema.
5. **Alert hooks** — `on_alert`/`on_alert_resolved` not required by template.

**Template update plan (do this after rectifying the three plugins):**
- Add `unit` to `execution.exit` schema (values: `"points"` \| `"rupee"` \| `"percent"`).
- Add `trailing_stop_points` / `trailing_stop_rupee` / `trailing_activation_points` to exit schema.
- Add `squareoff_minutes_before_close` to exit schema.
- Add `min_iv_percentile` / `max_iv_percentile` to params schema.
- Add `on_alert` / `on_alert_resolved` stubs to `StructureStrategy` base.

---

## 3. Parameterization principle (user direction)

> "Give parameter where ever possible and avoid hardcoding the value. I feel we can have unit also as parameter as some strategy will have points-based profit booking mostly option buying strategies but I agree in selling it must be in rupee terms that we can calculate upfront as we are selling."

**Applied convention in the rectified plugins:**

| Param | Type | Default | Unit | Notes |
|-------|------|---------|------|-------|
| `stop_loss_points` | float | 13.0 | points of premium | Selling side — points of premium, engine prices as points × Σ(lot size × lots) |
| `take_profit_points` | float | 7.0 | points of premium | Same |
| `stop_loss_rupee` | float | 0.0 | rupees | 0 = disabled; alternative to points for selling strategies |
| `take_profit_rupee` | float | 0.0 | rupees | 0 = disabled |
| `exit_unit` | str | `"points"` | — | `"points"` \| `"rupee"` — explicit unit to prevent confusion |
| `trailing_stop_points` | float | 5.0 | points | 0 = disabled |
| `trailing_activation_points` | float | 3.0 | points | start trailing after this much profit |
| `squareoff_minutes_before_close` | int | 30 | minutes | 0 = disabled; cut trade before 3pm on expiry day |
| `min_iv_percentile` | float | 0.0 | percentile | 0 = disabled |
| `max_iv_percentile` | float | 100.0 | percentile | 100 = disabled |

**Option-buying strategies** (e.g. `directional_options`, `banknifty_ema_momentum`) use percent-based exits:
- `stop_loss_pct: 0.50` (−50% of premium paid)
- `take_profit_pct: 1.0` (+100% of premium paid)

**Option-selling strategies** (strangle/straddle) should use points or rupee because the premium base is negative/short, and percent of a negative credit is meaningless.

---

## 4. Time-based stop rationale

User requirement: "time base stop so we can cut the trade before 3 pm to avoid last hour trouble on expiry day."

**Implementation:** `squareoff_minutes_before_close` (default 30). The bridge/executor checks the clock and squares off open short structures `N` minutes before market close (15:30 IST → 15:00 IST by default). This avoids:
- Last-hour liquidity crunch on expiry day
- Wide bid-ask spreads into the close
- Unintended overnight assignment risk on short options

**Not added yet:** The rectified plugins below declare the parameter and include it in the `execution.exit` dict, but the actual enforcement logic lives in the bridge/risk gate (Phase 1/MVP wiring). The strategy declares the intent; the engine enforces it.

---

## 5. How to apply this document

1. **Rectify the three strangle plugins** (done in this session) — apply the parameterization, `unit`, `trailing_stop`, `squareoff_minutes_before_close`, `IV filter`, and alert hooks.
2. **Update `templates/option_strategy_template.py` and `templates/strategy_template.py`** — bake the new params into the template skeletons.
3. **Update `templates/strategy_test_template.py`** — add `ALLOWS_NEUTRAL_VIEW = True` handling for structure strategies (NEUTRAL + `opening_structure` is the legitimate exception to "NEUTRAL = silence").
4. **Add per-strategy test files** — `tests/strategies/test_immediate_strangle.py`, `test_strangle_wing_points.py`, `test_straddle_short_atm.py`.
5. **Review other strategies** — run the same checklist against `directional_options`, `banknifty_ema_momentum`, `vwap_ema_rsi`, etc.

---

## 6. Files changed in this session

- `plugins/strategies/immediate_strangle.py` — rectified
- `plugins/strategies/strangle_wing_points.py` — rectified
- `plugins/strategies/straddle_short_atm.py` — rectified
- `src/backtest/strategy/templates/__init__.py` — added `on_alert`/`on_alert_resolved` no-op stubs to `StructureStrategy`, added `AlertType` import
- `templates/option_strategy_template.py` — added selling-strategy params (exit_unit, stop_loss_points, take_profit_points, stop_loss_rupee, take_profit_rupee, trailing_stop_points, trailing_activation_points, squareoff_minutes_before_close, min_iv_percentile, max_iv_percentile, alert_response)
- `templates/strategy_template.py` — added same selling-strategy params
- `templates/strategy_test_template.py` — no change needed (ALLOWS_NEUTRAL_VIEW already documented for structure strategies)
- `docs/STRATEGY_RECTIFICATION_FINDINGS.md` — this document

**Verification:** `discover_plugins(force=True)` registers all three plugins; `tests/test_strategy_conformance.py` (22), `tests/test_strategy_execution.py` + `test_strategy_base.py` + `test_strategy_bundle.py` + `test_strategy_adapter.py` (86) all pass.
