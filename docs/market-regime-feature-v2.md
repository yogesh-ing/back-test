# Market Regime Engine V2 — Implementation Plan

**Goal:** Production-ready regime detection + alerting so strategies can go paper → live trial (1–2 days) → green after 20–25 paper days.

---

## 1. Current State (What Exists)

| Component | Location | Status |
|-----------|----------|--------|
| Alert types | `src/backtest/alerts/types.py` | ✅ Defined (`VIX_REGIME_CHANGE`, `PORTFOLIO_GAMMA_CRITICAL`, etc.) |
| Alert subscription | `Strategy.subscribed_alerts` + `on_alert()` | ✅ Working (see `old_immediate_strangle`) |
| Portfolio manager | `src/backtest/forward/portfolio_manager.py` | ✅ Emits gamma alerts |
| VIX data source | mStock feed / NSE VIX | ❌ Not wired to regime engine |
| ADX/IV-RV/Session | Not implemented | ❌ Missing |

---

## 2. Architecture V2

```
┌─────────────────────────────────────────────────────────────────┐
│                    MARKET REGIME ENGINE                         │
│  (runs on evaluator thread, 1-min cadence, feeds portfolio)    │
├─────────────────────────────────────────────────────────────────┤
│  Inputs:                                                        │
│  • NIFTY/BANKNIFTY 1-min candles (from feed)                   │
│  • Option chain IV snapshot (from mStock)                      │
│  • Portfolio positions (for gamma)                             │
│                                                                 │
│  Detectors (pure functions, no state):                         │
│  • VIXRegimeDetector      → emits VIX_REGIME_CHANGE            │
│  • TrendRegimeDetector    → emits TREND_REGIME_CHANGE          │
│  • IVRVDislocationDetector → emits IV_RV_DISLOCATION           │
│  • SessionPhaseDetector   → emits SESSION_PHASE                │
│  • GammaRiskDetector      → emits PORTFOLIO_GAMMA_CRITICAL     │
│                                                                 │
│  Output: Alert bus → all subscribed strategies                 │
└─────────────────────────────────────────────────────────────────┘
```

---

## 3. Detector Specs (Implement These)

### 3.1 VIXRegimeDetector
```python
# Input: NSE VIX index (1-min or 5-min)
# Thresholds (configurable via config/regime.yaml):
LOW_VIX_THRESHOLD    = 15.0    # below = cheap vol, favor buying
NORMAL_VIX_MAX       = 25.0    # 15–25 = fair vol
HIGH_VIX_THRESHOLD   = 35.0    # above = expensive vol, favor selling

# Hysteresis: require 2 consecutive bars beyond threshold to flip
# Output: VIX_REGIME_CHANGE {new_regime, vix, prev_regime}
```

### 3.2 TrendRegimeDetector
```python
# Input: NIFTY/BANKNIFTY 1-min candles (resample to 15-min for ADX)
# ADX(14) on 15-min bars:
ADX_TREND_THRESHOLD     = 25.0   # >25 = trend
ADX_WEAK_THRESHOLD      = 20.0   # <20 = range
# Price vs 200DMA (daily close):
# Regimes: trend_up, trend_down, range, weak_trend_up, weak_trend_down
# Output: TREND_REGIME_CHANGE {regime, adx, price_vs_200dma}
```

### 3.3 IVRVDislocationDetector
```python
# Input: ATM IV (from chain, 5-min snapshot) + RV (20-day realized vol from 1-min)
# RV = std(log returns) * sqrt(252*375) * 100  [vol pts]
# Threshold:
IV_RV_BUFFER = 5.0  # vol pts

# States:
#   iv > rv + 5   → "sell_vol" (IV expensive)
#   iv < rv - 5   → "buy_vol"  (IV cheap)
#   else          → "fair"
# Output: IV_RV_DISLOCATION {state, iv, rv, edge}
```

### 3.4 SessionPhaseDetector
```python
# Input: Current IST time
# Phases (configurable):
PHASES = {
    "pre_open":      (9, 0)  - (9, 15),
    "opening_range": (9, 15) - (9, 30),
    "morning_trend": (9, 30) - (11, 30),
    "midday_chop":   (11, 30) - (13, 30),
    "afternoon_trend": (13, 30) - (14, 30),
    "pre_close":     (14, 30) - (15, 0),
    "close_auction": (15, 0) - (15, 30),
}
# Output: SESSION_PHASE {phase, minutes_to_next}
```

### 3.5 GammaRiskDetector (Already in PortfolioManager)
```python
# Input: All open option positions + greeks from chain
# Threshold: PORTFOLIO_GAMMA_THRESHOLD = -500000 (configurable)
# Per-strategy share: if strategy > 50% of short gamma → actionable
# Output: PORTFOLIO_GAMMA_CRITICAL {total_gamma, self_contribution: {share, gamma}}
```

---

## 4. Config File: `config/regime.yaml`

```yaml
vix:
  low_threshold: 15.0
  normal_max: 25.0
  high_threshold: 35.0
  hysteresis_bars: 2
  source: "nse_vix"  # or "mstock_vix"

trend:
  adx_period: 14
  adx_trend_threshold: 25.0
  adx_weak_threshold: 20.0
  resample: "15min"
  use_200dma: true

iv_rv:
  rv_period_days: 20
  rv_resample: "1min"
  buffer_vol_pts: 5.0
  iv_source: "atm_chain_snapshot"
  snapshot_interval_min: 5

session:
  phases:
    pre_open: "09:00-09:15"
    opening_range: "09:15-09:30"
    morning_trend: "09:30-11:30"
    midday_chop: "11:30-13:30"
    afternoon_trend: "13:30-14:30"
    pre_close: "14:30-15:00"
    close_auction: "15:00-15:30"

gamma:
  portfolio_threshold: -500000
  strategy_share_threshold: 0.50
```

---

## 5. Integration Points

| File | Change |
|------|--------|
| `src/backtest/alerts/engine.py` (new) | Main regime engine class, runs detectors, publishes alerts |
| `src/backtest/forward/portfolio_manager.py` | Register regime engine; forward alerts to strategies |
| `src/backtest/web/app.py` | Start regime engine in `create_app()` after `discover_plugins()` |
| `config/regime.yaml` | New config (load via existing config loader) |
| `src/backtest/strategy/registry.py` | No change — strategies already subscribe via `subscribed_alerts` |

---

## 6. Strategy Opt-In Pattern (V2 Ready)

```python
# In any strategy that wants regime awareness:
from backtest.alerts.types import AlertType

class MyStrategy(Strategy):
    subscribed_alerts = (
        AlertType.VIX_REGIME_CHANGE,
        AlertType.TREND_REGIME_CHANGE,
        AlertType.IV_RV_DISLOCATION,
        AlertType.SESSION_PHASE,
        AlertType.PORTFOLIO_GAMMA_CRITICAL,
    )

    def on_alert(self, alert_type, alert_data):
        if alert_type == AlertType.VIX_REGIME_CHANGE:
            if alert_data["new_regime"] == "high_vol":
                self.pause_new_entries = True
            elif alert_data["new_regime"] == "low_vol":
                self.pause_new_entries = False

        elif alert_type == AlertType.TREND_REGIME_CHANGE:
            if alert_data["regime"] in ("trend_up", "trend_down"):
                # Short straddle: exit in trend
                self.request_exit(1.0, reason=f"trend_{alert_data['regime']}")

        elif alert_type == AlertType.IV_RV_DISLOCATION:
            if alert_data["edge"] == "buy_vol" and self.is_short_vol:
                self.request_exit(1.0, reason="iv_cheap_exit")

        elif alert_type == AlertType.SESSION_PHASE:
            if alert_data["phase"] in ("midday_chop", "close_auction"):
                self.pause_new_entries = True
            else:
                self.pause_new_entries = False

        elif alert_type == AlertType.PORTFOLIO_GAMMA_CRITICAL:
            if alert_data["self_contribution"]["share"] > 0.5:
                self.request_exit(1.0, reason="gamma_critical")
```

---

## 7. Paper → Live Trial Gates

### Phase 1: Paper (20–25 trading days)
| Metric | Gate |
|--------|------|
| Regime alerts fire correctly | 100% (verified in logs) |
| Strategy `on_alert` reactions | Logged + reviewed weekly |
| No missed regime flips | VIX/ADX/IV-RV transitions match manual check |
| False positive rate | < 5% (pause when shouldn't) |

### Phase 2: Live Trial (1–2 days, small size)
| Metric | Gate |
|--------|------|
| Real broker fills on regime exits | Latency < 500ms from alert to order |
| Margin validation on gamma exits | No rejected orders |
| VIX/IV data latency | < 2s from mStock to alert |

### Phase 3: Green Signal (Full Size)
- 20–25 paper days clean
- 1–2 live trial days clean
- Operator sign-off

---

## 8. Implementation Order (2–3 Days)

| Day | Task |
|-----|------|
| 1 | Create `config/regime.yaml` + `src/backtest/alerts/engine.py` with 5 detectors |
| 1 | Wire into `portfolio_manager.py` + `app.py` startup |
| 2 | Add unit tests for each detector (deterministic, fixture-based) |
| 2 | Update `immediate_short_straddle`, `zero_dte_short_straddle` with full `on_alert` |
| 3 | Paper-run validation: replay 20 days, verify alerts + strategy reactions |
| 3 | Document live trial checklist |

---

## 9. Files to Create/Modify

| File | Action |
|------|--------|
| `config/regime.yaml` | Create |
| `src/backtest/alerts/engine.py` | Create (regime engine) |
| `src/backtest/alerts/detectors.py` | Create (5 detector classes) |
| `src/backtest/forward/portfolio_manager.py` | Modify (register engine) |
| `src/backtest/web/app.py` | Modify (start engine) |
| `plugins/strategies/immediate_short_straddle.py` | Add full `on_alert` |
| `plugins/strategies/zero_dte_short_straddle.py` | Add full `on_alert` |
| `tests/alerts/test_regime_engine.py` | Create |

---

## 10. Risk Mitigation

| Risk | Mitigation |
|------|------------|
| VIX data gap (mStock down) | Fallback to NSE VIX HTTP; cache last known |
| False regime flip | Hysteresis (2 bars) + minimum dwell time (30 min) |
| Alert storm | Rate-limit: max 1 alert per type per 5 min |
| Strategy ignores alert | Conformance test: every subscribed alert must have `on_alert` branch |

---

**Next Step:** Create `config/regime.yaml` and `src/backtest/alerts/engine.py` — the core engine. Detectors are pure functions → easy to unit test in isolation.