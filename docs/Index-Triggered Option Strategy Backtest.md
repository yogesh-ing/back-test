# Index-Triggered Option Strategy Backtest

**Version:** 1.0 (Architect Review Draft) · **Status:** Awaiting architect review
**Source:** `docs/OPTIONS-INDEX-TRIGGER-STRATEGY-BACKTEST-PRD.md` (working draft)
**Target:** Backtest index-triggered CE/PE option strategies on NIFTY/BANKNIFTY via the existing options-model sandbox.

---

## 1. Decision requested

Approve **index-triggered option strategy backtesting** through the existing `/api/backtest/options-model` endpoint (`pricing_feed: model_bs`), with:

- Index as trigger (e.g. NIFTY ≥ 25000 → buy ATM CE)
- Configurable TP/SL via `exits` block (`stop_loss_pct`, `take_profit_pct`, `exit_on_signal_flip`)
- Strategy-plugin authoring path (`plugins/strategies/*.py`) — no UI builder in this phase

**Quarantine contract:** results are `certifiable: false`, `certification_reason: model_pricing` — research-only, not live-P&L evidence. Refused by certify/deploy/optimize doors.

---

## 2. What works today (verified over HTTP, real NIFTY 1D data, 2025-11-03 → 2026-10-30)

| Strategy | Structure | Trades | Return | MDD | Win rate |
|----------|-----------|--------|--------|-----|----------|
| `CE_instant_buy` | long ATM call | 17 | −38.54% | −38.8% | 6.25% |
| `PE_instant_buy` | long ATM put | works | — | — | — |
| `directional_options` | EMA momentum → long call/put | works | — | — | — |

All results carry `pricing_feed: model_bs`, `certifiable: false`, model-priced disclaimer. `chain_history` feed returns 400 with coverage message (no silent BS fallback — migration 026 applied, no chain rows yet).

---

## 3. The gap

| Capability | Today | Needed |
|------------|-------|--------|
| Index trigger (NIFTY ≥ X) | Write `generate_market_view` plugin | Plugin authoring (works) — UI form deferred |
| Configurable TP/SL | API `exits` field + plugin `execution.exit` | Works via API; UI binding deferred |
| Option structure CE/PE | `selector_type: atm`, `long_call`/`long_put` | Works |
| Backtest & view results | `/api/backtest/options-model` | Works |

**Engine gap: zero.** Index-triggered option backtesting works today via plugins. **Product gap: UI/authoring** — no no-code form to declare an index condition + TP/SL.

---

## 4. Implementation path (Phase A — immediate)

Strategy plugin in `plugins/strategies/nifty_trigger_long_ce.py`:

```python
from decimal import Decimal
import pandas as pd
from backtest.strategy.intent import Direction, MarketView
from backtest.strategy.templates import EquityStrategy

class NiftyTriggerLongCE(EquityStrategy):
    name = "nifty_trigger_long_ce"
    eligible_instruments = ["NIFTY"]
    params = {
        "trigger_level": {"default": 25000.0, "type": "float"},
        "sl_pct": {"default": 30.0, "type": "float"},
        "tp_pct": {"default": 50.0, "type": "float"},
    }
    def generate_market_view(self, candles):
        close = float(candles["close"].iloc[-1])
        if close >= float(self.trigger_level):
            return MarketView(direction=Direction.BULLISH, confidence=1.0,
                              underlying="NIFTY", spot_price=Decimal(str(close)))
        return None
```

Run via `POST /api/backtest/options-model` with `pricing_feed: model_bs` and `exits: {stop_loss_pct, take_profit_pct, exit_on_signal_flip}`.

Phase B (UI strategy builder) deferred to `OPTION-STRATEGY-BUILDER-ARCHITECT-PRD.md`.

---

## 5. Architecture decisions

| # | Decision | Resolution |
|---|----------|------------|
| 1 | Authoring layer | Strategy plugins first; UI builder deferred |
| 2 | Trigger mechanism | `Strategy.generate_market_view(candles)` → `MarketView`, bridged by `SignalViewStrategy` |
| 3 | Pricing | Black-Scholes off NIFTY/BANKNIFTY index candles, `pricing_feed: model_bs`, quarantined |
| 4 | Exits | API `exits` block + plugin `execution.exit` |
| 5 | Structure whitelist | Long CE / long PE only |
| 6 | Data source | Real index candles from PostgreSQL `market_data_cache` |

---

## 6. Test matrix

- [ ] `CE_instant_buy` / `PE_instant_buy` full-year NIFTY run returns 200 (verified)
- [ ] Custom index-trigger plugin runs via API, emits correct BULLISH/BEARISH views
- [ ] TP/SL exits respected (bar-close evaluation; intrabar gap not modelled — disclosed)
- [ ] Quarantine doors hold: `certifiable: false`, refused by optimize/portfolio/certify
- [ ] `chain_history` refused with coverage message (no BS fallback)
- [ ] flake8 clean; 89 options/chain tests pass

---

## 7. Non-goals

- No historical options performance claim (model-priced = screen, not live-P&L evidence)
- No spreads/shorts/multi-leg (long CE/PE only)
- No chain_history replay yet (requires captured chain rows)
- No Pine import / no-code UI in this phase

---

## 8. Open questions for architect

1. Approve Phase A (plugin path) as the immediate way to backtest index-triggered option strategies?
2. Is the `model_bs` quarantine contract acceptable for research backtests?
3. Any objection to `plugins/strategies/` as the authoring surface until the UI builder lands?

---

*Architect comments go below this line.*
