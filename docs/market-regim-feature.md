# Market Regime Panel — Discussion Document

**Status:** Brainstorm / Design Discussion (NOT a PRD, NOT an implementation plan)
**Purpose:** Think through what this actually is, how big firms do it, what it means for our architecture, and what questions we need to answer before writing a single line of code.

---

## 0. First: A Terminology Correction That Changes Everything

You said "market sentiment." In quant trading, **sentiment** and **regime** are two different things, and conflating them will lead to the wrong design.

| | **Regime** (what firms actually use) | **Sentiment** (what retail/prop uses) |
|---|---|---|
| **What it is** | Statistical state of the market: trend vs. mean-reversion, high-vol vs. low-vol, risk-on vs. risk-off | Aggregate "mood" of participants: bullish vs. bearish, fear vs. greed |
| **How it's measured** | Realized vol percentile, Hurst exponent, 200DMA slope, ADX, VIX term structure, breadth | Put/Call Ratio, FII/DII flows, advance/decline ratio, NLP on news, social media |
| **Timescale** | Days to weeks (slow) | Hours to days (faster) |
| **Use in algo** | **Position sizing, strategy selection, risk overlay** | **Secondary filter, entry timing, conviction scaling** |
| **Deterministic?** | Yes (given the data, the regime is computed) | Partially (NLP is stochastic, social data is noisy) |
| **Backtestable?** | Yes (vol, trend, breadth are all in historical data) | Partially (FII/DII is historical; NLP is not) |
| **What big firms use** | **This.** It's the primary framework. | This is a **factor**, not the framework. |

**The critical insight:** Citadel, Two Sigma, Jane Street do NOT have a "sentiment dashboard." They have a **regime model** that is embedded in every strategy's logic. The regime is a **dimension of the strategy's state space**, not a separate "tab" you look at.

What you're describing — a panel that tells you "NIFTY is bullish, BANKNIFTY is bearish, here's what to do" — is closer to a **regime overlay** that sits **above** individual strategies and provides a **contextual signal** they can optionally consume.

**My recommendation:** Call it **"Market Regime"** (not "sentiment"). Build it as a **statistical regime classifier** with a few **sentiment indicators** as secondary inputs. The panel is the **display**. The regime model is the **signal**. The strategy's use of it is the **consumption contract**.

Let me explain how this actually works at firms, then map it to our codebase.

---

## 1. How Big Firms Actually Do Regime Detection

### 1.1 The Core: Multi-Dimensional Regime Classification

No firm uses a single indicator. They use a **vector of market state variables** and classify the market into discrete (or continuous) states.

**The standard state vector for an index (NIFTY/BANKNIFTY):**

| Dimension | Indicator | How it's computed | Regime meaning |
|-----------|-----------|-------------------|----------------|
| **Trend** | 200-day SMA slope | `(SMA_200[t] - SMA_200[t-20]) / SMA_200[t-20]` | Positive = uptrend, negative = downtrend, flat = range |
| **Trend strength** | ADX (14-period) | Wilder's ADX on index | >25 = trending, <20 = ranging, 20–25 = transition |
| **Volatility** | Realized vol percentile (1yr) | `RV_20[t]` ranked against last 252 days | <20th = low vol, 20–80th = normal, >80th = high vol |
| **Vol term structure** | VIX 1M vs. VIX 3M (or 2W vs. 1M) | `VIX_2W / VIX_1M` | >1 = inverted (fear), <1 = normal (contango) |
| **Breadth** | % stocks above 200DMA | Count NIFTY 200 constituents above their 200DMA | >60% = broad uptrend, <40% = broad downtrend |
| **Momentum** | 20-day return, 60-day return | Simple | Positive = momentum up, negative = momentum down |
| **Liquidity** | Average daily volume (20d) percentile | Ranked against 1yr | High = liquid, low = illiquid (wider spreads) |
| **Cross-asset** | USD/INR, 10Y G-Sec yield, crude oil | Level + 20d change | INR depreciation = risk-off for Indian equities |
| **Derivatives** | Put/Call Ratio (NSE), OI change | PCR > 1.2 = bearish, < 0.8 = bullish | Sentiment proxy |
| **Flows** | FII net buy/sell (daily, NSDL) | Cumulative 5-day, 20-day | Sustained FII selling = structural headwind |

**The classification:**

Most firms use one of three approaches:

| Approach | How | Pros | Cons |
|----------|-----|------|------|
| **Rule-based** | `if RV > 80th AND ADX < 20: regime = "high_vol_range"` | Transparent, backtestable, explainable | Rigid, boundary effects (80th vs. 81st) |
| **HMM (Hidden Markov Model)** | 3–5 latent states, trained on the state vector | Captures transitions, probabilistic | Black-box, needs retraining, non-deterministic if not seeded |
| **Clustering (K-Means / GMM)** | Cluster the state vector into K regimes | Unsupervised, adapts | K is arbitrary, clusters can be unstable |

**What I've seen at firms:**

- **Citadel / Two Sigma:** HMM with 4 states (low-vol uptrend, low-vol downtrend, high-vol uptrend, high-vol downtrend). The HMM is retrained monthly on 5 years of data. The state is a **probability vector** (e.g., [0.7, 0.1, 0.1, 0.1] = 70% likely "low-vol uptrend"). Strategies consume the probability, not the hard label.
- **Jane Street / HRT:** No explicit "regime model." Regime is **embedded in the microstructure model.** The order flow imbalance, spread, and queue position ARE the regime. For index options, they look at the IV surface shape (skew, term structure) as the regime.
- **Indian prop firms (smaller):** Rule-based. "VIX > 20 AND NIFTY < 200DMA = risk-off. Reduce position size by 50%." Simple, transparent, works.

**For our system:** Rule-based is the right starting point. It's deterministic, backtestable, explainable, and the operator can see exactly why the regime is what it is. HMM is a Phase 2 enhancement if the rule-based version proves insufficient.

### 1.2 The Regime → Strategy Mapping

This is the part most people miss. The regime doesn't just "tell you the market is bullish." It **changes how strategies behave.**

| Regime | What it means for strategies | Concrete actions |
|--------|------------------------------|-----------------|
| **Low-vol uptrend** (RV < 20th, ADX > 25, NIFTY > 200DMA) | Trend-following works. Mean-reversion is dangerous (you're catching falling knives in a rising market). Theta works (low vol = slow decay). | ↑ Trend strategy position size. ↓ Mean-reversion position size. Long calls > long puts (directional bias). |
| **High-vol uptrend** (RV > 80th, ADX > 25, NIFTY > 200DMA) | Trend-following works but with wider stops. Vol strategies (long straddles, long vol) work. Theta is expensive. | ↑ Vol strategy allocation. ↑ Stop-loss width (2× normal). Short puts > long calls (better risk/reward in high vol). |
| **Low-vol range** (RV < 20th, ADX < 20, NIFTY ≈ 200DMA) | Mean-reversion works. Trend-following gets whipsawed. Theta is cheap (sell options). | ↑ Mean-reversion position size. ↓ Trend position size. Short straddles / iron condors (if we support them). |
| **High-vol range / crash** (RV > 80th, ADX < 20, NIFTY < 200DMA) | **Everything is dangerous.** Mean-reversion catches knives. Trend-following gets stopped out. Vol is expensive to buy. | ↓ ALL position sizes (50% or less). ↑ Stop-loss tightness. Flat or very small positions. "Cash is a position." |
| **Transition** (ADX 20–25, regime changing) | **Uncertainty is highest.** False signals are most common. | ↓ Position sizes. Wait for confirmation. No new entries. |

**The key principle:** The regime doesn't tell a strategy "buy" or "sell." It tells it **"how much to bet" and "which type of strategy has edge right now."** The strategy's signal is still the signal. The regime is the **context multiplier.**

### 1.3 What Firms Do NOT Do

- **They don't use NLP sentiment as a primary signal.** News sentiment is a slow, noisy, easily-arbitraged factor. It's a 5th or 6th input in a 10-factor model, not the headline.
- **They don't have a "sentiment score" that goes from 0 to 100.** That's a retail app. Firms use **probability distributions over latent states.**
- **They don't change strategy logic based on regime.** They change **position size, risk limits, and strategy allocation.** The SMA crossover still crosses. The RSI still reverts. But the position size is 50% in a high-vol regime vs. 100% in a low-vol regime.
- **They don't use a single indicator.** VIX alone is not a regime. ADX alone is not a regime. It's the **combination** that matters.
- **They don't let the regime model override the strategy.** The regime is a **filter / scaler**, not a **veto.** (Unless it's a global circuit breaker, which is a separate risk tier.)

---

## 2. What We Already Have (And What's Missing)

### 2.1 Existing in the Codebase

| Component | Location | What it does | Gap |
|-----------|----------|-------------|-----|
| VIX regime | `src/backtest/intelligence/` | Computes VIX level and regime classification | Single indicator. No trend, no breadth, no cross-asset. |
| Portfolio Greeks | `src/backtest/intelligence/` | Net Δ, Γ, Θ, V in ₹ | Portfolio-level, not market-level. |
| Concentration | `src/backtest/intelligence/` | Per-underlying, per-group, per-strike | Portfolio-level. |
| Strategy P&L correlation | `src/backtest/intelligence/` | Correlation matrix across runners | Portfolio-level. |
| Alert system | `src/backtest/alerts/` | Pub/sub, dedup, escalation | Can deliver regime change alerts, but no regime model to alert on. |
| Strategy hooks | `src/backtest/alerts/` | Strategies can subscribe to alerts | The consumption mechanism exists. The signal doesn't. |
| Risk tiers | `src/backtest/forward/` | Global, per-bucket, per-order breakers | Risk limits exist. No regime-based dynamic adjustment. |
| Data: NIFTY/BANKNIFTY 1-min | `market_data_cache` | OHLCV candles | Sufficient for RV, trend, momentum. |
| Data: NIFTY 200 constituents | `stock-list/nse_ind_nifty200list.csv` | Universe list | Sufficient for breadth (need 200DMA per constituent). |
| Data: VIX | ? | Need to verify | Is NSE VIX in the DB? Or fetched from broker? |
| Data: FII/DII flows | ? | Need to verify | Probably not in the DB. External source. |
| Data: Put/Call Ratio | ? | Need to verify | From NSE option chain. We have the chain (if recording). |
| Data: USD/INR, G-Sec, Crude | ? | Need to verify | Cross-asset. Probably not in the DB. |

### 2.2 What's Missing (The Gap)

1. **A market-level regime model.** Not portfolio-level. A function that takes NIFTY/BANKNIFTY candles + VIX + (optionally) breadth/flows and returns a **regime state** with confidence.

2. **A regime → strategy mapping.** A config that says: "In regime X, strategy Y gets position size multiplier Z." This is the **consumption contract.**

3. **A display surface.** The panel/banner/side-panel the user is asking for. This is the **human-readable** view of the regime model.

4. **A signal delivery mechanism.** How does a running strategy **know** the regime changed? The alert system exists. But it's for discrete events ("VIX crossed 25"). A regime change is a **state transition** ("market moved from low-vol uptrend to high-vol range"). Different semantics.

5. **Backtest integration.** Can I backtest a strategy **with** and **without** regime-based position sizing? This is critical for validation. If the regime model doesn't improve the backtest (walk-forward), it's not worth deploying.

---

## 3. What the Panel Would Actually Show

Let me sketch what I think the **Market Regime Panel** should look like. This is a **side panel** (always visible, collapsible) on the Portfolio Command Center, NOT a separate tab. A separate tab means you have to navigate to it. A side panel means it's **always in your peripheral vision** while you're managing positions.

```
┌─────────────────────────────────────────────────────────┐
│  MARKET REGIME                              [collapse ▸] │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  NIFTY 50                    BANKNIFTY                  │
│  ┌─────────────────────┐    ┌─────────────────────┐     │
│  │ 25,432.15  +0.82%   │    │ 54,218.40  -0.34%   │     │
│  │                     │    │                     │     │
│  │ REGIME:             │    │ REGIME:             │     │
│  │ 🟢 LOW-VOL UPTREND  │    │ 🟡 HIGH-VOL RANGE   │     │
│  │ Confidence: 78%     │    │ Confidence: 54%     │     │
│  │                     │    │                     │     │
│  │ Trend: ↑ (ADX 32)  │    │ Trend: → (ADX 18)  │     │
│  │ Vol: 14.2% (22nd)  │    │ Vol: 22.8% (71st)  │     │
│  │ Breadth: 67% > 200D│    │ Breadth: 48% > 200D│     │
│  │ VIX: 13.4 (contango)│   │ VIX: 18.9 (flat)   │     │
│  │ PCR: 0.92 (neutral) │    │ PCR: 1.08 (slight  │     │
│  │                     │    │      bearish)      │     │
│  │ FII 5d: +₹8,200 Cr │    │ FII 5d: +₹8,200 Cr │     │
│  │                     │    │                     │     │
│  │ Strategy overlay:   │    │ Strategy overlay:   │     │
│  │ ↑ Trend (×1.2)     │    │ ↓ All (×0.5)       │     │
│  │ ↓ MeanRev (×0.7)   │    │ ↑ Vol (×1.3)       │     │
│  │ Long calls favored  │    │ Short puts favored  │     │
│  └─────────────────────┘    └─────────────────────┘     │
│                                                         │
│  Cross-Asset:                                           │
│  USD/INR: 85.42 (+0.1%)  10Y G-Sec: 7.12% (-2bp)       │
│  Crude: $78.3 (-1.2%)  VIX Term: Contango (13.4/14.1) │
│                                                         │
│  Regime History (last 20 bars):                         │
│  [🟢][🟢][🟢][🟢][🟡][🟡][🟢][🟢][🟢][🟢]...           │
│                                                         │
│  Last regime change: 2 bars ago (🟡 → )             │
│  Duration in current regime: 14 bars                    │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

**Key design decisions in this sketch:**

1. **Two columns (NIFTY + BANKNIFTY).** They can be in different regimes. NIFTY can be in a low-vol uptrend while BANKNIFTY is in a high-vol range (bank stocks are more volatile, more sensitive to rate expectations).

2. **Regime is a label + confidence.** Not a 0–100 score. "LOW-VOL UPTREND, 78% confidence." The confidence tells you how sure the model is. Low confidence = transition = reduce size.

3. **The indicators are visible, not hidden.** The operator can see WHY the regime is what it is. "ADX 32" means strong trend. "Vol 14.2% (22nd percentile)" means low vol. "Breadth 67%" means most stocks are in uptrend. This is **explainability.** A black-box "sentiment: 72/100" is useless for debugging.

4. **Strategy overlay is explicit.** "↑ Trend (×1.2)" means: trend-following strategies get 1.2× their normal position size. "↓ All (×0.5)" means: all strategies get 50% size. This is the **consumption contract** made visible.

5. **Regime history strip.** The last 20 bars as colored dots. You can see if the regime is stable or whipsawing. If it's flipping every 2–3 bars, the model is too sensitive.

6. **Cross-asset row.** USD/INR, G-Sec, Crude. These are **context**, not primary signals. But they explain WHY the regime is what it is. (INR depreciating + G-Sec yield rising = risk-off for Indian equities.)

7. **It's a side panel, not a tab.** Always visible. Collapsible. On the Portfolio Command Center, the Backtest page, the Risk page. It's **ambient information**, like a stock ticker on a TV.

---

## 4. The Architecture Question: Display vs. Signal vs. Both

This is the **most important design decision** and it changes everything.

### Option A: Display Only (Informational)

The panel shows the regime. The **human** reads it and makes decisions. Strategies don't change.

- **Pros:** Simple. No strategy code changes. No backtest integration. Low risk.
- **Cons:** The operator has to manually adjust position sizes. Inconsistent. Forgettable. Doesn't scale (what if you have 10 strategies?).
- **Effort:** 2–3 days (compute regime + render panel).
- **Who it's for:** A single operator who actively watches the screen.

### Option B: Signal (Strategies Consume It)

The regime model produces a **state** that strategies can **subscribe to** via the existing alert/hook system. When the regime changes, strategies can:
- Adjust position size (×0.5, ×1.0, ×1.2)
- Adjust stop-loss width (tighter in high vol, wider in low vol)
- Skip entries (no new entries in "high-vol crash" regime)
- Change structure preference (long calls in uptrend, long puts in downtrend)

- **Pros:** Automated. Consistent. Scales to N strategies. Backtestable (run the same strategy with and without regime overlay).
- **Cons:** Complex. The regime model must be **deterministic** (for backtesting). The consumption contract must be **explicit** (which strategies use it, how, what multiplier). Risk of over-engineering.
- **Effort:** 5–8 days (regime model + consumption contract + backtest integration + tests).
- **Who it's for:** A system that runs strategies autonomously with minimal human intervention.

### Option C: Both (Display + Signal, Opt-In)

The panel is always visible (display). **Each strategy can opt in** to regime-based position sizing via a config flag:

```python
class MyStrategy(EquityStrategy):
    # ...
    regime_aware = True  # Opt in to regime-based sizing
    regime_multipliers = {
        "low_vol_uptrend": 1.2,
        "low_vol_range": 0.8,
        "high_vol_uptrend": 0.9,
        "high_vol_range": 0.5,
        "crash": 0.0,  # Flat
    }
```

If `regime_aware = False` (default), the strategy ignores the regime. If `True`, the engine multiplies the strategy's position size by the regime multiplier before execution.

- **Pros:** Best of both. Human sees the regime. Strategies can use it. Opt-in means no strategy is affected unless it opts in. Backtestable.
- **Cons:** More complex. The `regime_multipliers` config is a new parameter space (overfitting risk). The operator needs to understand what the multipliers do.
- **Effort:** 7–10 days.
- **Who it's for:** A mature system where the operator has validated the regime model and wants to automate the overlay.

**My recommendation: Start with Option A. Plan for Option C.**

Build the regime model and the panel first (Option A). Validate it for 2–4 weeks. Watch:
- Is the regime stable or whipsawing?
- Does it actually correlate with strategy P&L? (In "low-vol uptrend," do trend strategies outperform?)
- Does the operator actually look at it? (If not, it's a useless dashboard.)

Then, if the data supports it, add Option C (opt-in consumption). The regime model is the same. You're just adding the consumption contract.

---

## 5. The Regime Model: What to Build (Option A)

### 5.1 The State Vector (V1 — Minimal)

For V1, use **5 indicators** that are computable from data we already have:

| # | Indicator | Data Source | Computation | Regime Meaning |
|---|-----------|-------------|-------------|----------------|
| 1 | **Trend** | NIFTY/BANKNIFTY 1D candles | `SMA_200` + 20-day slope | Above/below, rising/falling |
| 2 | **Trend strength** | Same | ADX(14) on 1D | >25 trending, <20 ranging |
| 3 | **Volatility** | Same | RV_20 (20-day realized vol), annualized, percentile over 252 days | <20th low, >80th high |
| 4 | **VIX level** | NSE VIX (verify source) | Current level + 5-day change | >20 elevated, >30 extreme |
| 5 | **Breadth** | NIFTY 200 constituents 1D | % of 200 stocks above their 200DMA | >60% broad up, <40% broad down |

**What we need to verify (before building):**
- Is NSE VIX in our DB? Or do we fetch it from the broker?
- Do we have 1D candles for all 200 NIFTY constituents? (For breadth.)
- If not, can we compute breadth from a smaller universe (NIFTY 50)?
- Is FII/DII data available? (Probably not in our DB. External source. **Defer to V2.**)
- Is Put/Call Ratio available? (From NSE option chain. We have the chain if we're recording. **Defer to V2.**)

### 5.2 The Classification (Rule-Based, V1)

```python
def classify_regime(trend: str, adx: float, rv_pctile: float, vix: float, breadth: float) -> RegimeState:
    """
    Returns: RegimeState(label, confidence, indicators)
    
    Regimes:
      - LOW_VOL_UPTREND
      - LOW_VOL_RANGE
      - HIGH_VOL_UPTREND
      - HIGH_VOL_RANGE
      - HIGH_VOL_DOWNTREND  (crash / severe correction)
    """
    vol_high = rv_pctile > 80
    vol_low = rv_pctile < 20
    trending = adx > 25
    ranging = adx < 20
    uptrend = trend == "above_200dma_rising"
    downtrend = trend == "below_200dma_falling"
    broad_up = breadth > 60
    broad_down = breadth < 40
    
    # Confidence: how many indicators agree
    # (e.g., if vol_high AND trending AND downtrend AND broad_down → 4/5 agree → 80% confidence)
    
    if vol_high and downtrend and broad_down:
        return RegimeState("HIGH_VOL_DOWNTREND", confidence=0.8)
    if vol_high and trending and uptrend:
        return RegimeState("HIGH_VOL_UPTREND", confidence=0.7)
    if vol_high and ranging:
        return RegimeState("HIGH_VOL_RANGE", confidence=0.6)
    if vol_low and trending and uptrend:
        return RegimeState("LOW_VOL_UPTREND", confidence=0.8)
    if vol_low and ranging:
        return RegimeState("LOW_VOL_RANGE", confidence=0.7)
    # Default / transition
    return RegimeState("TRANSITION", confidence=0.3)
```

**This is deliberately simple.** No HMM. No ML. If the operator can explain the regime in one sentence ("NIFTY is above its 200DMA, ADX is 32, vol is in the 22nd percentile, 67% of stocks are in uptrend — so it's a low-vol uptrend"), it's a good regime model.

### 5.3 The Strategy Overlay (V2, Option C)

```python
REGIME_MULTIPLIERS = {
    #           low_vol_up  low_vol_rng  high_vol_up  high_vol_rng  high_vol_down
    "trend":    [1.2,       0.6,        0.9,         0.4,          0.0],
    "meanrev":  [0.7,       1.2,        0.5,         0.8,          0.0],
    "vol":      [0.8,       1.0,        1.3,         1.5,          1.0],  # long vol
    "options":  [1.1,       0.8,        0.9,         0.5,          0.0],  # directional options
}
```

These are **defaults.** Each strategy can override. The operator can tune them in the UI. **But they must be validated in backtest before being trusted.**

---

## 6. How Strategies Would Consume It (The Contract)

### 6.1 For Option A (Display Only)

No consumption. The panel is informational. The human reads it. Done.

### 6.2 For Option C (Opt-In Signal)

**The delivery mechanism:**

The regime model runs on a **schedule** (e.g., every 1D bar close, or every 15-min bar for intraday). When the regime **changes** (state transition), it publishes an event via the existing `AlertBroker`:

```python
# In the regime model:
if new_regime != current_regime:
    alert_broker.publish(
        channel=f"regime.{underlying}",
        event=RegimeChangeEvent(
            underlying="NIFTY",
            old_regime=current_regime,
            new_regime=new_regime,
            confidence=new_regime.confidence,
            indicators=new_regime.indicators,
            bar_time=bar_time,
        ),
        severity="info" if confidence > 0.6 else "warn",
    )
```

**The consumption:**

A strategy that opts in subscribes to the regime channel:

```python
class MyTrendStrategy(EquityStrategy):
    regime_aware = True
    
    def on_regime_change(self, event: RegimeChangeEvent):
        """Called when the regime changes. Adjust internal state."""
        self._position_multiplier = REGIME_MULTIPLIERS["trend"][event.new_regime.index]
        self._sl_width = 0.05 if event.new_regime.vol_high else 0.03
    
    def generate_signals(self, candles):
        signals = super().generate_signals(candles)
        # The engine reads self._position_multiplier and scales position size
        return signals
```

**The engine's role:**

The engine reads `strategy.regime_aware` and `strategy._position_multiplier` before sizing the position:

```python
# In the execution engine:
if strategy.regime_aware:
    position_size = base_size * strategy._position_multiplier
else:
    position_size = base_size
```

**Critical rule:** The regime multiplier **scales position size.** It does NOT change the signal. The strategy still says "buy." The regime says "but buy half as much." The signal is the strategy's. The size is the regime's.

### 6.3 For Backtesting

The backtest engine must be able to:
1. Compute the regime at each bar (using the same rule-based model).
2. Apply the regime multiplier to position size.
3. Report P&L **with** and **without** the regime overlay (side-by-side).
4. Show: "Regime overlay improved Sharpe from 0.8 to 1.2, reduced max DD from 18% to 12%."

This is the **validation gate.** If the regime overlay doesn't improve the backtest (walk-forward), it's not worth deploying.

---

## 7. Pitfalls and What NOT to Do

| Pitfall | Why it's dangerous | What to do instead |
|---------|-------------------|-------------------|
| **Regime whipsaw** | The model flips between "uptrend" and "range" every 2–3 bars. Strategies get whipsawed. P&L destroyed. | Add a **hysteresis / confirmation** rule: regime change requires 3 consecutive bars in the new state. Or: minimum duration in a regime (e.g., 5 bars). |
| **Overfitting the multipliers** | You tune `REGIME_MULTIPLIERS` until the backtest looks great. Live performance collapses. | Use **defaults** (the table in §5.3). Validate with walk-forward. If the default multipliers don't improve the backtest, **don't deploy the overlay.** |
| **Regime as a veto** | "If regime is HIGH_VOL_DOWNTREND, no strategy can trade." This is a **global circuit breaker**, not a regime overlay. It belongs in the risk tier, not the regime model. | The regime model **scales** position size. The risk tier **halts** trading. Keep them separate. |
| **Using VIX alone** | VIX is a single indicator. It doesn't capture trend, breadth, or momentum. "VIX is 18" doesn't tell you if the market is in an uptrend or a crash. | Use the **vector** of indicators. VIX is one input, not the model. |
| **NLP sentiment as a primary signal** | News sentiment is noisy, lagged, and easily arbitraged. It's a 5th-order factor, not a primary signal. | Use it as a **secondary indicator** in the panel (display only). Don't let it drive position sizing. |
| **Backtesting the regime model on the same data it was tuned on** | Classic overfitting. The regime looks great in-sample, terrible out-of-sample. | Walk-forward: tune on 2 years, validate on the next 6 months. Repeat. |
| **Making the regime model non-deterministic** | If the regime depends on `datetime.now()` or a random seed, the backtest is non-reproducible. | The regime model is a **pure function** of the data. Same candles → same regime. No wall-clock. |
| **One regime for all strategies** | A trend strategy and a mean-reversion strategy should respond **differently** to the same regime. | The multiplier table is **per strategy type**, not global. |
| **Updating the regime too frequently** | On 1-min bars, the regime changes 10× per day. Strategies can't react fast enough. | V1: compute on **1D bars** (daily close). V2: 15-min bars with hysteresis. Not 1-min. |
| **Ignoring BANKNIFTY** | NIFTY and BANKNIFTY can be in different regimes. A strategy on BANKNIFTY should use BANKNIFTY's regime, not NIFTY's. | Compute regime **per underlying.** The panel shows both. |

---

## 8. Data Requirements (What We Need to Verify)

| Data | Needed for | Source | In our DB? | Action |
|------|-----------|--------|------------|--------|
| NIFTY 1D OHLCV | Trend, RV, ADX, momentum | `market_data_cache` (resampled from 1-min) | ✅ (verify) | None |
| BANKNIFTY 1D OHLCV | Same | Same | ⚠️ (backfill stopped) | **Operator decision: resume backfill?** |
| NSE VIX 1D | VIX level, change | NSE website / broker API | ❓ (verify) | Add to data source if missing |
| NIFTY 200 constituents 1D | Breadth (% above 200DMA) | `market_data_cache` | ⚠️ (need to verify coverage) | Fetch if missing |
| FII/DII daily flows | Flow indicator | NSDL/CDSL / broker | ❌ (not in DB) | **V2.** External source. |
| Put/Call Ratio | Sentiment indicator | NSE option chain | ❌ (chain not recorded yet) | **V2.** After chain recording. |
| USD/INR, G-Sec, Crude | Cross-asset context | External | ❌ | **V2.** Nice to have. |

**V1 data requirement:** NIFTY + BANKNIFTY 1D candles + VIX + NIFTY 200 breadth. That's it. Everything else is V2.

---

## 9. Questions to Answer Before Building

These are the questions **you** (the operator/architect) need to decide. I'm not deciding them for you.

| # | Question | Why it matters | My suggestion |
|---|----------|---------------|---------------|
| 1 | **Display only (Option A) or signal (Option C)?** | Changes the scope by 2×. | Start with A. Add C after 2–4 weeks of validation. |
| 2 | **1D or intraday regime?** | 1D = 252 bars/year, simple. 15-min = 1,890 bars/year, more responsive but more whipsaw. | 1D for V1. The operator makes decisions at EOD. Intraday is V2. |
| 3 | **Which indicators in V1?** | More indicators = more data to fetch, more complexity, more overfitting risk. | 5 indicators: trend (200DMA), ADX, RV percentile, VIX, breadth. No FII, no PCR, no cross-asset. |
| 4 | **Rule-based or HMM?** | Rule-based = transparent, deterministic, backtestable. HMM = probabilistic, needs retraining, harder to debug. | Rule-based for V1. HMM is a Phase 2 enhancement if the rule-based version proves insufficient. |
| 5 | **Regime per underlying or global?** | NIFTY and BANKNIFTY can be in different regimes. | Per underlying. The panel shows both. Strategies use their own underlying's regime. |
| 6 | **Does the regime affect the options model backtest?** | If a strategy is in a "high-vol range" regime, should the model backtest use a different IV premium? | **No.** The regime overlay affects **position sizing**, not **pricing.** The BS model uses realized vol. The regime model uses realized vol. They're consistent. Don't double-count. |
| 7 | **BANKNIFTY backfill: resume or not?** | Without BANKNIFTY 1D data, we can't compute its regime. | **Operator decision.** If you trade BANKNIFTY options, resume the backfill. If not, NIFTY-only for V1. |
| 8 | **Where does the panel live?** | Side panel on Portfolio? Separate tab? Both? | **Side panel on Portfolio Command Center.** Collapsible. Always visible. NOT a separate tab (you'd have to navigate to it). |
| 9 | **Should the regime model be backtestable?** | If not, you can't validate that it improves P&L. | **Yes.** The backtest engine should be able to run a strategy with and without the regime overlay. This is the validation gate. |
| 10 | **What's the kill switch?** | If the regime model starts whipsawing, how do you turn it off? | `REGIME_OVERLAY_ENABLED` env var (default `false` for V1). The panel is always visible (display). The overlay (position sizing) is off until you explicitly enable it. |

---

## 10. What I Would NOT Build (And Why)

| Thing | Why not |
|-------|---------|
| **A 0–100 "sentiment score"** | It's a retail app feature. It's not actionable. "NIFTY sentiment: 72/100" doesn't tell you what to do. "NIFTY is in a low-vol uptrend, ADX 32, 67% breadth" tells you exactly what to do. |
| **NLP on news headlines** | Noisy, lagged, easily arbitraged. Not in our data pipeline. Not backtestable. Not deterministic. V3 at the earliest. |
| **Social media sentiment (Twitter/X, Moneycontrol)** | Same as above. Plus: scraping is fragile, rate-limited, and legally grey. |
| **A separate "Regime" tab** | You'd have to navigate to it. It should be **ambient** (side panel). Like a stock ticker. Always in your peripheral vision. |
| **Regime-based strategy selection** | "In this regime, run strategy A instead of strategy B." This is **strategy allocation**, not regime detection. It's a portfolio management decision, not a signal. Keep it separate. |
| **Real-time (tick-level) regime** | Overkill. The regime changes on the order of days, not seconds. 1D is sufficient. 15-min is the maximum. |
| **A regime model that overrides the strategy signal** | The regime **scales** the position. It doesn't **veto** the signal. If the strategy says "buy" and the regime says "high-vol crash," the position size is 0 (flat). But the strategy still says "buy." The regime just makes the size zero. The distinction matters for backtesting and for the operator's mental model. |

---

## 11. The One-Paragraph Summary

Build a **rule-based regime classifier** that computes 5 indicators (trend, ADX, RV percentile, VIX, breadth) on 1D candles for NIFTY and BANKNIFTY, classifies the market into 5 states (low-vol uptrend, low-vol range, high-vol uptrend, high-vol range, high-vol downtrend), and displays the result in a **collapsible side panel** on the Portfolio Command Center. The panel shows the indicators (so the operator can verify the classification), the regime label + confidence, and a "strategy overlay" hint (e.g., "↑ Trend ×1.2, ↓ MeanRev ×0.7"). In V1, this is **display only** — the human reads it and makes decisions. In V2 (after 2–4 weeks of validation), add **opt-in position sizing** where strategies can subscribe to regime changes and scale their position size. The regime model is a **pure function** of the data (deterministic, backtestable). It **scales** position size; it does not **veto** signals. It is **not** a sentiment score. It is **not** NLP. It is **not** a separate tab. It is a **statistical state classifier** with a **human-readable display** and an **optional programmatic consumption contract.**

---

## 12. Next Steps (If You Want to Proceed)

1. **You answer the 10 questions in §9.** Especially #1 (display vs. signal), #2 (1D vs. intraday), #3 (which indicators), and #7 (BANKNIFTY backfill).
2. **I verify the data:** Is VIX in the DB? Do we have 200 constituents? What's the exact symbol string?
3. **I write a 1-page design doc** (not a PRD) with the regime model spec, the panel layout, the API contract, and the test matrix.
4. **You review it.** We discuss. We refine.
5. **Then** I write the PRD and the Junior gets the build plan.

No code until step 4 is done. This is a discussion. The code comes later.