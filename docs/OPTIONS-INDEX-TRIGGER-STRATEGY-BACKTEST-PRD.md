# PRD — Index-Triggered Option Strategy Backtest (CE/PE on NIFTY/BANKNIFTY)

**Version:** 1.0 · **Status:** Draft for architect review · **Author:** Kilo (quant-engineer-sonet mode)

---

## Decisions requested

**A. Scope** — Add index-triggered long CE / long PE option backtesting through the existing options-model sandbox (`/api/backtest/options-model`, `pricing_feed: model_bs`), with configurable TP/SL and index as the trigger. Approval of this document approves this V1 contract.

**B. Implementation path** — Strategy-plugin-first (no UI builder yet): the operator/developer writes a strategy plugin that emits a `MarketView` from an index condition; the sandbox prices it via Black-Scholes off NIFTY index candles. UI form comes later via Phase 1 of the option strategy builder.

**C. Not in this PRD** — chain_history replay (real premiums), spreads/shorts/multi-leg, Pine import, live/paper deployment. These remain gated per `OPTIONS-MODEL-BACKTEST-PLAN-V3.md` and `OPTION-STRATEGY-BUILDER-ARCHITECT-PRD.md`.

---

## 1. Why this fits the current code

- The options-model sandbox already accepts **any registered strategy** (built-in + plugin) that emits a directional view, and expresses it as an option structure priced by BS off the underlying candles (`src/backtest/api/options_model.py`, `src/backtest/options/signal_view.py`).
- `SignalViewStrategy` bridges `Strategy.generate_market_view(candles) -> MarketView | None` into the `OptionBacktestDriver`. Option-native strategies (override `generate_market_view`) are priced straight from their own view — a put strategy is not fed bullish signals.
- The sandbox is **quarantined**: `certifiable: false`, `certification_reason: model_pricing`, refused by certify/deploy/optimize doors. Research-only, not a live-P&L measurement. This is intentional and documented in `OPTIONS-MODEL-BACKTEST-PLAN-V3.md`.
- Exits are already configurable at the API level: `stop_loss_pct`, `take_profit_pct`, `exit_on_signal_flip`, applied per structure via `BacktestConfig` → `ExitPolicy`.
- Migration 026 (`option_chain_history`) is applied to the dev DB, but no chain rows exist yet — `chain_history` feed is refused with a coverage message, never silently falls back to BS.

**Current limitation:** There is no UI form to configure "index hits X → buy CE/PE, exit at TP Y / SL Z". The user must write a strategy plugin (Python) today. This PRD is the plan to close that gap.

---

## 2. What works today (verified)

Verified over HTTP against the running app (`http://127.0.0.1:5000`, source=db, real NIFTY 1D data, 2025-11-03 → 2026-10-30, ₹1,00,000 capital):

| Strategy | Structure | Trades | Return | MDD | Win rate |
|----------|-----------|--------|--------|-----|----------|
| `CE_instant_buy` | long ATM call | 17 | −38.54% | −38.8% | 6.25% |
| `PE_instant_buy` | long ATM put | works | — | — | — |
| `directional_options` | EMA momentum → long call/put | works | — | — | — |

All results carry `pricing_feed: model_bs`, `certifiable: false`, and the model-priced disclaimer. The `chain_history` feed returns 400 with coverage info (no 503 — migration table exists).

## 3. The gap

| Capability | Today | Needed |
|------------|-------|--------|
| Index trigger (NIFTY hits X) | Write a `generate_market_view` plugin | UI form or strategy factory |
| Configurable TP/SL | API `exits` field works; plugin `execution.exit` works | UI form binding to `exits` |
| Option structure (CE/PE) | `selector_type: atm`, `long_call`/`long_put` | UI dropdown |
| Backtest & view results | `/api/backtest/options-model` works | None — works |

The engine gap is **zero**: index-triggered option backtesting works today via plugins. The product gap is **UI/authoring**: no no-code path to declare an index condition + TP/SL and run it.

---

## 4. Proposed implementation

### Phase A (this PRD) — Strategy-plugin path (immediate, no UI)

The operator writes a plugin in `plugins/strategies/<name>.py`:

```python
from decimal import Decimal
import pandas as pd
from backtest.strategy.intent import Direction, MarketView
from backtest.strategy.templates import EquityStrategy

class NiftyTriggerLongCE(EquityStrategy):
    name = "nifty_trigger_long_ce"
    eligible_instruments = ["NIFTY"]
    params = {
        "trigger_level": {"default": 25000.0, "type": "float", "label": "Index trigger level"},
        "sl_pct": {"default": 30.0, "type": "float", "label": "Stop loss % of premium"},
        "tp_pct": {"default": 50.0, "type": "float", "label": "Take profit % of premium"},
    }
    def generate_market_view(self, candles):
        close = float(candles["close"].iloc[-1])
        if close >= float(self.trigger_level):
            return MarketView(direction=Direction.BULLISH, confidence=1.0,
                              underlying="NIFTY", spot_price=Decimal(str(close)))
        return None
```

Run via:
```
POST /api/backtest/options-model
{
  "strategy": "nifty_trigger_long_ce",
  "symbol": "NIFTY", "timeframe": "1D",
  "from_date": "...", "to_date": "...", "capital": 1000000,
  "params": {"trigger_level": 25000, "sl_pct": 30, "tp_pct": 50},
  "pricing_feed": "model_bs",
  "exits": {"stop_loss_pct": 0.30, "take_profit_pct": 0.50, "exit_on_signal_flip": false},
  "selector_type": "atm", "max_open_structures": 1
}
```

### Phase B (follow-up, not this PRD) — UI strategy builder

A form on the backtest page: underlying (NIFTY/BANKNIFTY), index condition (level/cross), structure (CE/PE), TP/SL, expiry/DTE. Serializes to a strategy + exit plan and calls the same `/api/backtest/options-model` endpoint. Design lives in `OPTION-STRATEGY-BUILDER-ARCHITECT-PRD.md` Phase 2.

---

## 5. Architecture decisions

| # | Decision | Resolution |
|---|----------|------------|
| 1 | Authoring layer | Strategy plugins first; UI builder deferred to the option-strategy-builder PRD Phase 2 |
| 2 | Trigger mechanism | `Strategy.generate_market_view(candles)` → `MarketView` (BULLISH/BEARISH/NEUTRAL), bridged by `SignalViewStrategy` |
| 3 | Pricing | Black-Scholes off NIFTY/BANKNIFTY index candles, `pricing_feed: model_bs`, quarantined (research only) |
| 4 | Exits | API `exits` block (`stop_loss_pct`, `take_profit_pct`, `exit_on_signal_flip`) + plugin `execution.exit` |
| 5 | Structure whitelist | Long CE / long PE only (`RESEARCH_STRUCTURES` permits wider only in the research driver) |
| 6 | Data source | Real NIFTY index candles from PostgreSQL (`market_data_cache`); no synthetic substitution |

---

## 6. Test matrix

- [ ] `CE_instant_buy` / `PE_instant_buy` full-year NIFTY run returns 200 with metrics (verified)
- [ ] Custom index-trigger plugin runs via API and emits correct BULLISH/BEARISH views
- [ ] TP/SL exits respected (stop/target at bar close; intrabar gap not modelled — disclosed)
- [ ] Quarantine doors hold: `certifiable: false`, refused by optimize/portfolio/certify
- [ ] `chain_history` refused with coverage message (no fallback to BS)
- [ ] flake8 clean on changed files; existing options test suite (89 tests) passes

---

## 7. Non-goals / trade-offs

- **No historical options performance claim:** model-priced results are a structure/strike/exit comparison screen, not live-P&L evidence.
- **No spreads/shorts/multi-leg in V1:** execution gate accepts long CE / long PE only.
- **No chain_history replay yet:** requires captured chain rows (migration 026 applied; recorder not yet populated).
- **No Pine import / no-code UI in this PRD:** deferred to the option strategy builder.

---

## 8. Open questions for architect

1. Approve Phase A (plugin path) as the immediate way to backtest index-triggered option strategies?
2. Confirm `model_bs` quarantine contract is acceptable for research backtests, or should index-trigger results carry an additional banner?
3. Any objection to `plugins/strategies/` as the authoring surface until the UI builder (Phase B) lands?

---

## 9. Sign-off

Approve **A** (scope) and **B** (implementation path) as one action. Phase A requires no new engine work — only a strategy plugin and API calls through the existing endpoint.
