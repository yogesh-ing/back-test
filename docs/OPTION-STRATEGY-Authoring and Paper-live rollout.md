# Strategy Builder Design & User Template — Options V1

**Status:** design artifact extending PRD v2 §2 · **not an implementation authorization** · Phase 2 scope

---

## 0. PRD amendment note (record this)

The PRD's decision 2 says V1 = "buy CE / buy PE, **ATM**". You're now asking for **ATM−1 / ATM / ATM+1**. I accept this as **Amendment A-1** because it does not change the failure matrix: still one leg, still long premium, still max loss = premium + fees. Same pre-trade checklist, same strike-validity checks — the selector just resolves a different strike.

**Constraints I'm attaching to A-1:** offsets limited to **±1** in V1 (no ATM−5 style deep strikes — liquidity and decay profiles diverge and the validity rules get noisy); strike is **locked at entry** (no re-anchoring to a new ATM mid-trade — that's a rolling strategy, not V1); offset is defined on the **strike ladder**, not moneyness (see §2 — this matters more than it looks).

---

## 1. What kind of strategy belongs in options V1

V1 is **directional trend events on the underlying, expressed as long premium**. That's the only archetype that fits all the PRD constraints: single leg, defined risk, maps cleanly onto `MarketView`, finite holding with defined exits.

| Archetype | V1 verdict | Reason |
|---|---|---|
| Trend-following (EMA cross / price vs EMA) | ✅ **V1** | Discrete events on bar close, no lookahead, flip semantics already defined in PRD |
| Underlying price-level break ("buy CE when NIFTY crosses 25,000") | 🔜 **V1.1, first candidate** | Event-based, trivially testable, very common in Indian retail flow — cheapest possible preset to add once golden vectors exist |
| Breakout (Donchian / range break) | 🔜 V1.1 candidate | Event semantics, needs signal-level stats first |
| Mean reversion (RSI extremes) | ⚠️ defer | Fights the instrument: you hold decaying premium while *waiting* for reversion, and MR signals fire often → churn, which the architecture doc already flags |
| Straddle / strangle / spreads | ❌ V2+ | Multi-leg = leg-level partial fills, combo exit semantics, margin logic — PRD decision 2 excludes |
| Premium selling / delta-neutral / IV plays | ❌ out of scope | Margin, tail risk, continuous hedging, or needs IV history we don't have |

**Honest framing for users (this goes in the builder):** trend-following with long premium pays when the move is *fast enough to beat theta*. Every whipsaw costs full decay + fees. The builder should show **signal statistics** — triggers per week, average bars between opposite signals, computed on historical underlying candles only. This needs no option chain data, so it doesn't violate the "no option backtest" rule — but it must be labeled *"signal statistics, not performance."*

---

## 2. Strike selector semantics (the part users will get wrong if you let them)

### 2.1 Convention: offset on the strike ladder

`ATM−1` = **one strike below** the ATM strike. `ATM+1` = one strike above. The offset is in strike *steps*, and — critically — **the same offset means opposite moneyness for CE and PE:**

| Selector | Long CE | Long PE | Premium | Character |
|---|---|---|---|---|
| **ATM−1** | **ITM** call | **OTM** put | CE high / PE low | CE tracks underlying closely, steadier, costlier. PE is the cheap, fast-decaying side |
| **ATM** | ATM call | ATM put | balanced | Default. ~0.5 delta behavior |
| **ATM+1** | **OTM** call | **ITM** put | CE low / PE high | CE is cheap/fast-decay. PE tracks underlying closely |

Users don't need to memorize this — the **live chain preview resolves it** ("ATM−1 CE = strike 24,950, ITM, last ₹182, bid 181 / ask 184"). But the form should show the moneyness label next to the offset so nobody buys an OTM put thinking they bought the "safe" one.

**Selection guidance shown in the form (no edge claims):** ITM side → higher delta, slower % decay, higher cost per lot — suits slower expected moves. OTM side → cheaper, larger % swings, faster % decay — suits fast expected moves. ATM → default.

### 2.2 Resolution algorithm (executed by the bridge at order time, never at draft time)

1. Take one **timestamped snapshot**: underlying spot LTP + option chain, same freshness SLA as PRD §3.3.
2. `reference = spot LTP`; reject if stale.
3. **ATM strike** = chain strike minimizing |strike − reference|. **Tie → lower strike** (deterministic, documented).
4. **Strike step** = modal adjacent-strike spacing within ±3 strikes of ATM, **computed from the chain** — never hardcoded (NIFTY/BANKNIFTY strike intervals have changed before and will again; this is the same discipline as the lot-size rule).
5. `target strike = ATM + offset × step`. Must exist in the chain **with a fresh quote** — else reject with `STRIKE_NO_FRESH_QUOTE`. **Never substitute** (PRD invariant).
6. `quantity = lots × lot_size`, lot size resolved from instrument metadata at execution time.

**Lock at entry:** the strike, expiry and lot size are resolved once at entry and held to exit. No re-anchoring.

---

## 3. The builder form (4 sections, per PRD §2)

### Section 1 — Signal (trigger composer)

```
┌─ TRIGGER ─────────────────────────────────────────────────────┐
│  WHEN   [ EMA(9)  ] [ crosses ABOVE ▾ ] [ EMA(21) ]          │
│         …or compose: [ Close ] [ crosses BELOW ▾ ] [ EMA(50) ]│
│  CONFIRMED BY  [ 1 ▾ ] additional closed bar(s) in direction │
│  DIRECTION MAP:  ◉ Both (bullish→CE, bearish→PE)             │
│                  ○ CE only   ○ PE only                        │
│  TIMEFRAME: [ 5m ▾ ]   UNDERLYING: [ NIFTY ▾ ]               │
└───────────────────────────────────────────────────────────────┘
```

**Builder rule that makes everything else well-defined:** all V1 triggers are **discrete events evaluated on bar close, never continuous states**. "EMA(9) above EMA(21)" as a *state* would re-fire every bar; as a *cross event* it fires once and only re-arms after the opposite cross. This is what makes the PRD's "entry only on next confirmed signal bar" and the no-re-entry rule unambiguous.

- **Neutral = hold** existing position (PRD default).
- **Flip = exit current leg at market on opposite confirmed signal; stay flat; new entry only on a fresh event.** For one-sided runners (CE-only/PE-only), an opposite signal still *exits* — the direction map only gates *entries*.
- **Warm-up:** no signals for the first `slow` (×3 policy) bars; the preview states it in wall-clock terms.

### Section 2 — Instrument & expression

```
┌─ EXPRESSION ──────────────────────────────────────────────────┐
│  UNDERLYING: NIFTY      LOTS: [ 1 ▾ ]                         │
│  EXPIRY: nearest with min DTE [ 1 ▾ ]                         │
│  STRIKE: [ ATM−1 ▾ ]  → resolved as: ITM call / OTM put       │
│  ── LIVE PREVIEW (sample real chain, read-only) ─────────────  │
│  ATM strike 25,000 (step 50) → selected 24,950 CE             │
│  bid 181.00 / ask 184.35 · lot size 75 · quote 12:31:04 IST   │
└───────────────────────────────────────────────────────────────┘
```

### Section 3 — Risk & exits (with resolved units)

```
┌─ EXITS ───────────────────────────────────────────────────────┐
│  STOP LOSS:   [ 30 ▾ ] % of entry premium  → shown as ₹ too   │
│  TARGET:      [ 1.5 × ▾ ] entry premium   (or underlying pts) │
│  TIME EXIT:   expiry day 15:15 IST (configurable days-before) │
│  FLIP on opposite signal: [ on ▾ ]                            │
│  RE-ENTRY:    ✗ (default false)                               │
│  TRAILING SL: 🔒 coming in V1.1 (visible, disabled)           │
│  PER-TRADE RISK CAP: [ 2 ]% of segment   MAX CONCURRENT: [ 1 ]│
└───────────────────────────────────────────────────────────────┘
```

Exit precedence stays exactly per PRD: **emergency flatten > risk caps > DTE/session square-off > stop > target > flip.**

### Section 4 — Review

Shows the immutable versioned config + playbook snapshot, the live-chain preview, signal statistics, and the **mandatory disclosures** (theta/IV can beat a correct direction call; exits fill at market and can gap; paper fills are simulated at ask/bid). Save as draft. No performance gate.

---

## 4. Declarative config schema (what the form compiles to)

```json
{
  "schema_version": "1.0",
  "name": "NIFTY 5m EMA 9/21 trend",
  "signal": {
    "type": "ema_cross",
    "params": { "fast": 9, "slow": 21 },
    "confirmation_bars": 1,
    "direction_map": { "bullish": "CE", "bearish": "PE" }
  },
  "instrument": {
    "underlying": "NIFTY",
    "timeframe": "5m",
    "expiry_policy": { "mode": "nearest", "min_dte": 1 },
    "strike_selector": { "base": "ATM", "offset": 0 },
    "lots": 1
  },
  "exits": {
    "stop_loss":   { "type": "premium_pct", "value": 30 },
    "target":      { "type": "premium_multiple", "value": 1.5 },
    "dte_exit":    { "mode": "expiry_day", "time": "15:15" },
    "flip":        { "enabled": true },
    "re_entry":    false,
    "trailing_stop": { "enabled": false }
  },
  "risk": {
    "per_trade_cap_pct": 2.0,
    "segment": "OPTIONS_PAPER",
    "max_concurrent_exposure": 1
  }
}
```

`direction_map` values `{"bullish": "CE", "bearish": null}` produce the one-sided runners. `trailing_stop` exists in the schema now (disabled) so V1.1 is an additive migration, not a schema break.

### Validation rules (form-time)

| Field | Range / values | Default | Rejection code |
|---|---|---|---|
| `signal.type` | `ema_cross`, `price_cross_ema` | `ema_cross` | `SIG_TYPE_UNSUPPORTED` |
| `fast` / `slow` | 2–50 / 3–200, fast < slow | 9 / 21 | `SIG_PARAM_RANGE` |
| `confirmation_bars` | 0–3 | 1 | `SIG_CONFIRM_RANGE` |
| `strike offset` | **{−1, 0, +1}** | 0 | `STRIKE_OFFSET_UNSUPPORTED` |
| `min_dte` | 0–7 | 1 | `EXPIRY_POLICY_INVALID` |
| `lots` | ≥ 1, ≤ segment max | 1 | `LOTS_RANGE` |
| SL `premium_pct` | 5–90 | 30 | `EXIT_RANGE` |
| Target multiple | 1.1–5.0 | 1.5 | `EXIT_RANGE` |
| `trailing_stop.enabled` | must be `false` | false | `TRAILING_NOT_IN_V1` |

Execution-time rejections (not form-time) reuse the PRD's ordered pre-trade checklist: `STRIKE_NO_FRESH_QUOTE`, `CHAIN_MISSING`, `EXPIRY_UNAVAILABLE`, `FUNDS_INSUFFICIENT`, `RISK_CAP`, `EXPOSURE_CAP` — each distinct, logged, testable.

---

## 5. Compiled strategy contract (fixed implementations, no codegen)

The builder **never generates code**. Config selects from an audited registry:

```python
SIGNAL_REGISTRY = {
    "ema_cross":      EMACrossSignal,        # fixed, golden-vector tested
    "price_cross_ema": PriceCrossEMASignal,  # fixed, golden-vector tested
}

class EMACrossSignal(AuditedSignal):
    """Parameterized by validated config only. Warm-up = 3 × slow bars.

    generate_market_view(candles) -> MarketView | None
      BULLISH: EMA(fast) crossed ABOVE EMA(slow) on the last closed bar,
               with `confirmation_bars` prior closed bars also fast > slow.
      BEARISH: symmetric.
      None otherwise  =>  neutral = hold existing position.

    Pure function of trailing candles. No clock, no chain, no broker access
    (docs/STRATEGY-AUTHORING.md). Event semantics: a cross fires once and
    re-arms only after the opposite cross.
    """
```

The bridge consumes `instrument` + `exits` blocks via the §2.2 resolution algorithm. Strike selection lives in the **bridge**, not the strategy — the strategy only ever says bullish/bearish, which is the existing `MarketView` contract, unchanged.

---

## 6. Worked example — the exact preview text a user sees at Review

> **Trigger:** When a 5-minute candle **closes** with EMA(9) crossing above EMA(21), confirmed by 1 additional closed candle with EMA(9) still above EMA(21) → **buy CE**. Symmetric cross below → **buy PE**. *(Direction: both.)*
>
> **Execution:** On the bar after the trigger, buy **1 lot** of NIFTY options — expiry = nearest with ≥ 1 day to expiry; strike = **ATM** (closest to spot, ties take the lower strike); at market (paper fills at the ask). Lot size and strike step are resolved from the live chain at execution — never assumed.
>
> **Exits** (checked on fresh quotes, in priority order): emergency flatten → risk caps → expiry-day 15:15 IST → **stop-loss** at ≤ 70% of entry fill *(entry ₹150 → exit if mark ≤ ₹105)* → **target** at ≥ 1.5× entry fill *(₹225)* → **flip**: opposite confirmed cross exits the position, runner stays flat until the next confirmed signal. No re-entry.
>
> **Warm-up:** first 63 bars (~5¼ hours) produce no signals. **Signal statistics:** ~11 triggers/week on this timeframe over the last 6 months *(signal count only — not performance)*.
>
> ⚠️ A correct direction call can still lose: premium decays with time and volatility. Exits fill at market and can gap past trigger prices. Paper fills are simulated; live fills will differ.

Second example, one line: *BANKNIFTY 15m, PE-only, ATM−1 (OTM put), SL 25%, target 2×, flip exits on bullish cross but never buys CE.*

---

## 7. Trailing stop — V1.1 placeholder (designed now, shipped later)

```json
"trailing_stop": {
  "enabled": false,
  "arm_after_pct": 20,      // activates once mark ≥ 1.2 × entry fill
  "trail_pct": 10,          // exits when mark falls 10% from peak mark
  "peak_reference": "mark"
}
```

Requires one engine addition (per-position peak-mark tracking) and its own exit-precedence tests — that's exactly why it's V1.1, not V1. Having the schema present now means enabling it later doesn't touch stored configs.

---

## 8. Preset roadmap (each entry passes the PRD's signal-level gate)

| Version | Addition | Gate |
|---|---|---|
| V1 | `ema_cross`, `price_cross_ema` | Golden vectors + forward paper comparison |
| V1.1 | `price_cross_level` (underlying level break) | Cheapest next preset — trivial golden vectors |
| V1.1 | Trailing stop | Peak-mark tracking + precedence property tests |
| V1.1+ | Donchian breakout; offset ±2 | Signal stats + liquidity check on ±2 before offering it |
| V2 | Second expiry policy, re-entry rules | After Phase 3 live-pilot reliability evidence |
| Beyond | Spreads / multi-leg | Explicit new PRD — not a builder toggle |

---

**Bottom line:** the template stays a **parameterized trigger → strike selector → exits** pipeline. Users compose an *event* on the underlying, pick a rung on the strike ladder (with moneyness made visible), and set premium-based exits. Everything they can express compiles to the existing `MarketView` + playbook + bridge contracts — nothing new in the execution path except the ±1 offset resolution, which rides the existing strike-validity checks.