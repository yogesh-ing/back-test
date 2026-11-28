# Multi-Leg Option Strategy Engine — Implementation-Ready Document (V1.1)

**Supersedes:** `docs/iron_condor_multileg_spec.md` (architect draft) + Master Document (engineer status) + V1.0 of this doc
**Author:** QUANT-ARCH
**Date:** 2026-11-28
**Status:** Revised. All gaps from quant-director review resolved against `.kilo/agent/quant-options-engineer.md`.
**Target:** 4-leg defined-risk structures (iron condor, iron butterfly) on NIFTY/BANKNIFTY. Paper + backtest. Live in Phase 2.

---

## 0. What This Document Is and Is Not

**Is:** The complete, unambiguous spec for building multi-leg option support into the existing engine. Every file, every function signature, every data model change, every test, every edge case. A developer should be able to implement Phases 1–4 without asking a single clarification question.

**Is not:** A PRD. The PRD was the previous document. This is the build spec.

**Scope (V1):**
- 4-leg structures: iron condor (sell OTM strangle + buy further OTM wings), iron butterfly (buy ATM straddle + sell OTM wings)
- Paper + backtest (model-priced, quarantined from live)
- NIFTY + BANKNIFTY
- 1D timeframe (backtest), 15-min (paper) — VWAP settlement requires intraday bars; see section 6.1 caveat
- Single underlying per structure
- 1–3 lots (lot size read from broker instrument master, never hardcoded)
- Defined risk (wings always present)
- Exit collar with off/shadow/enforce modes
- ltp<=0 guard on every exit quote
- Feed-subscription pre-check before entry

**Out of scope (V2+):**
- 3-leg structures (straddle, strangle, naked spreads)
- 5+ leg structures
- Multi-expiry structures (calendar, diagonal)
- Live execution (paper only in V1)
- Tick-level delta hedging
- Shorting equity
- Multi-DTE IV comparison

---

## 1. What Exists Today (Verified — Do Not Re-verify)

| Component | Location | Status |
|-----------|----------|--------|
| `OptionBacktestDriver` | `src/backtest/engine/option_backtest_driver.py:467` | ✅ Handles 2-leg structures. Bar-by-bar BS pricing. Decimal trade records. |
| `SignalViewStrategy` | `src/backtest/options/signal_view.py` | ✅ Bridges `Strategy.generate_market_view(candles) → MarketView`. Per-bar calling convention. |
| `OptionPricingFeed` / `ModelBSFeed` | `src/backtest/options/` | ✅ BS on realized vol. `pricing_feed: model_bs`. Quarantined. |
| `OptionPricingFeed` interface | `src/backtest/options/` | ✅ `quote(underlying, strike, expiry, side, spot, bar_time) → LegQuote`. |
| `bs_price` | `src/backtest/options/quote_providers.py:76` | ✅ Returns price, delta, gamma, theta, vega. Decimal. |
| `options/greeks.py` | `src/backtest/options/greeks.py` | ✅ Per-leg Greeks. |
| `options/margin.py` | `src/backtest/options/margin.py` | ✅ Per-leg SPAN + Exposure. No combined multi-leg margin. |
| `options/structures.py` | `src/backtest/options/structures.py` | ✅ `LongCall`, `LongPut`, `BullCallSpread`, `BearPutSpread`. No `IronCondor` / `IronButterfly`. |
| `options/selector.py` | `src/backtest/options/selector.py` | ✅ `ATMSelector`, `DeltaSelector`. No `OffsetSelector`. |
| `options/expiry.py` | `src/backtest/options/expiry.py` | ✅ Nearest expiry, DTE filter. |
| `options/playbook.py` | `src/backtest/options/playbook.py` | ✅ `Playbook` dataclass. `to_expression()`. `to_runner_config()`. |
| `forward/options_bridge.py` | `src/backtest/forward/options_bridge.py` | ✅ Paper execution. Atomic multi-leg (2-leg). No 4-leg support. |
| `forward/execution_engine.py` | `src/backtest/forward/execution_engine.py:52` | ✅ `EXIT_PRECEDENCE`. Applies to net premium of 2-leg. Not tested with 4-leg. |
| `simulator/` fee stack | `src/backtest/simulator/` | ✅ Per-leg: STT, exchange, SEBI, stamp, GST, brokerage. 4-leg = 4× calls. Verify no double-counting. |
| `trade_structures` table | Migration (existing) | ✅ Schema assumes max 2 legs. Needs migration for 4. |
| `instruments` table | DB | ✅ 154K instruments. NIFTY/BANKNIFTY option rows. Lot size, strike interval. |
| `plugins/strategies/iron_condor.py` | `plugins/strategies/iron_condor.py` | ⚠️ Approximation. Short straddle proxy. Must be rewritten. |
| `plugins/strategies/theta_harvest.py` | `plugins/strategies/theta_harvest.py` | ⚠️ S1+S2 merged. Likely 2-leg short straddle. |
| 4,849-test suite | `tests/` | ✅ All passing. Must remain passing. |
| Migration head | `migrations/versions/` | **024.** New migrations start at **025.** |

---

## 2. Architectural Decisions (Final — Do Not Re-litigate)

| # | Decision | Rationale |
|---|----------|-----------|
| D1 | **`MultiLegOptionStrategy` emits `OpeningIntent`, never `MarketView`.** | The strategy says "open this specific 4-leg structure," not "market goes up/down." No MarketView adapter wraps the intent back (removed the misleading 6.4 adapter from V1.0). The driver checks `isinstance(strategy, MultiLegOptionStrategy)` and routes directly. |
| D2 | **Iron condor sells OTM, never ATM.** | Sell CE at ATM + short_offset_points, sell PE at ATM − short_offset_points, buy wings at ATM ± wing_width_points. Selling ATM repeats the short-straddle mistake and leaves undefined tail risk between ATM and the wing. |
| D3 | **Iron butterfly: buy ATM straddle + sell OTM wings.** | Defined-risk long-volatility structure. Net debit at entry. |
| D4 | **`max_loss_per_lot` clamped to ≥ 0.** | If net credit ≥ wing width, max loss is 0 (credit exceeds width). Never negative. |
| D5 | **Exit pricing uses bid/ask side.** | Closing a LONG → hit BID. Closing a SHORT → hit ASK. Single mid/LTP price is unacceptable for exit pricing. |
| D6 | **`ltp <= 0` is a failed quote.** | Raises `ProtectedExitRefusedError`. Structure stays open. Zero-price exit bug refused. |
| D7 | **Exit collar with off/shadow/enforce modes.** | In "enforce" mode, refuse fills deviating from mark by > max(₹25, 5% × mark). "Shadow" journals only. "Off" no check. |
| D8 | **Feed-subscription pre-check before entry.** | Leg tokens must be subscribed on the broker feed before any entry order is placed. Re-subscribe open-leg tokens on broker re-login from DB. |
| D9 | **Expiry settlement = last 30-min VWAP, not close.** | Cash settlement for index options uses last 30-min VWAP of the underlying index. **1D backtest cannot compute intraday VWAP** — VWAP settlement is scoped to 15-min paper mode only. 1D backtest settles at expiry-bar close with explicit provenance label. |
| D10 | **Lot sizes, strike intervals, expiry days read from broker instrument master.** | Never hardcoded. NIFTY=25, BANKNIFTY=15 (Nov 2024) are reference values only; the master is the source of truth. |
| D11 | **All monetary values in `Decimal`.** | No `float` for money. |
| D12 | **Backtest deterministic.** | Same input → byte-identical output. No `datetime.now()` in backtest path; use bar timestamp. |
| D13 | **Model-priced outputs quarantined.** | `certifiable: false` on every output. Structural quarantine prevents model results from reaching a live runner. |
| D14 | **Combined margin = max(short − long) + base, not sum of sells.** | Defined-risk offset reduces margin vs. naked. V1 conservative: `max(0, sum(sell_margins) − sum(buy_margins))`. |

---

## 3. Data Model Changes

### 3.1 New Table: `multi_leg_structures` (Migration 025)

```sql
CREATE TABLE multi_leg_structures (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    runner_id       UUID NOT NULL REFERENCES portfolios(id),
    strategy_name   VARCHAR(100) NOT NULL,
    underlying      VARCHAR(20) NOT NULL,
    structure_type  VARCHAR(30) NOT NULL,
    status          VARCHAR(20) NOT NULL DEFAULT 'open',
    legs            JSONB NOT NULL,
    net_credit      NUMERIC(12,2) NOT NULL,
    net_debit       NUMERIC(12,2),
    lot_count       INT NOT NULL DEFAULT 1,
    net_delta       NUMERIC(10,4),
    net_gamma       NUMERIC(12,6),
    net_theta       NUMERIC(10,4),
    net_vega        NUMERIC(10,4),
    entry_bar_time  TIMESTAMPTZ NOT NULL,
    exit_bar_time   TIMESTAMPTZ,
    exit_reason     VARCHAR(30),
    exit_premium    NUMERIC(12,2),
    realised_pnl    NUMERIC(12,2),
    fees            JSONB NOT NULL,
    max_loss        NUMERIC(12,2) NOT NULL,
    margin_required NUMERIC(12,2) NOT NULL,
    pricing_feed    VARCHAR(20) NOT NULL,
    data_source     VARCHAR(20) NOT NULL,
    certifiable     BOOLEAN NOT NULL DEFAULT FALSE,
    settlement_price NUMERIC(12,4),
    settlement_source VARCHAR(30),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_mls_runner ON multi_leg_structures (runner_id, status);
CREATE INDEX idx_mls_underlying ON multi_leg_structures (underlying, status);
CREATE INDEX idx_mls_entry_time ON multi_leg_structures (entry_bar_time DESC);
```

Leg JSON schema (each leg):
```json
{
  "leg_id": 1,
  "side": "sell" | "buy",
  "type": "call" | "put",
  "strike": 25000.0,
  "expiry": "2026-11-26",
  "lot_count": 1,
  "entry_premium": 1250.0,
  "entry_bid": 1248.0,
  "entry_ask": 1252.0,
  "entry_delta": -0.15,
  "entry_theta": 8.2,
  "instrument_id": "NFO:25000CE26NOV26",
  "order_id": "uuid",
  "exit_side": "bid" | "ask",
  "exit_premium": null,
  "exit_reason": null
}
```

### 3.2 Alter `trade_structures` (Migration 026)

```sql
ALTER TABLE trade_structures
    ADD COLUMN structure_type VARCHAR(30) NOT NULL DEFAULT 'two_leg';
-- Values: 'two_leg' (existing), 'multi_leg' (new, FK to multi_leg_structures.id)
```

### 3.3 New Table: `option_open_legs` (Migration 027)

Feed-subscription tracking for re-login re-subscription:

```sql
CREATE TABLE option_open_legs (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    runner_id      UUID NOT NULL REFERENCES portfolios(id),
    instrument_id  VARCHAR(60) NOT NULL,
    underlying     VARCHAR(20) NOT NULL,
    strike         NUMERIC(12,4) NOT NULL,
    option_type    VARCHAR(10) NOT NULL,
    expiry         DATE NOT NULL,
    side           VARCHAR(10) NOT NULL,
    subscribed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    feed_token     VARCHAR(60)
);
```

---

## 4. New / Modified Files (Complete List)

| # | File | Action | Phase |
|---|------|--------|-------|
| 1 | `src/backtest/strategy/multi_leg_base.py` | NEW — `MultiLegOptionStrategy`, `LegSpec`, `OpeningIntent`, `ProtectedExitRefusedError` | 1 |
| 2 | `src/backtest/options/structures.py` | MODIFY — add `IronCondor`, `IronButterfly` classes | 1 |
| 3 | `src/backtest/options/selector.py` | MODIFY — add `OffsetSelector` | 1 |
| 4 | `src/backtest/options/margin.py` | MODIFY — add `combined_margin()` for multi-leg | 1 |
| 5 | `src/backtest/options/multi_leg_greeks.py` | NEW — net Greeks for 4-leg structure | 1 |
| 6 | `src/backtest/options/multi_leg_exit.py` | NEW — exit logic, collar, ltp<=0 guard, bid/ask side | 2 |
| 7 | `src/backtest/options/feed_subscription.py` | NEW — pre-check + re-subscribe on re-login | 2 |
| 8 | `src/backtest/forward/options_bridge.py` | MODIFY — 4-leg atomic execution | 2 |
| 9 | `src/backtest/engine/option_backtest_driver.py` | MODIFY — accept `OpeningIntent`, handle 4-leg | 2 |
| 10 | `src/backtest/db/models.py` | MODIFY — add `MultiLegStructure`, `OptionOpenLeg` models | 1 |
| 11 | `src/backtest/db/migrations/025_multi_leg_structures.py` | NEW | 1 |
| 12 | `src/backtest/db/migrations/026_trade_structures_type.py` | NEW | 1 |
| 13 | `src/backtest/db/migrations/027_option_open_legs.py` | NEW | 2 |
| 14 | `plugins/strategies/iron_condor.py` | REWRITE — subclass `MultiLegOptionStrategy` | 3 |
| 15 | `plugins/strategies/theta_harvest.py` | REWRITE — add wings (4-leg) | 3 |
| 16 | `tests/test_multi_leg_base.py` | NEW | 1 |
| 17 | `tests/test_iron_condor_structure.py` | NEW | 1 |
| 18 | `tests/test_multi_leg_greeks.py` | NEW | 1 |
| 19 | `tests/test_multi_leg_margin.py` | NEW | 1 |
| 20 | `tests/test_multi_leg_exit.py` | NEW | 2 |
| 21 | `tests/test_multi_leg_feed_subscription.py` | NEW | 2 |
| 22 | `tests/test_multi_leg_bridge.py` | NEW | 2 |
| 23 | `tests/test_multi_leg_backtest.py` | NEW | 2 |
| 24 | `tests/test_api_multi_leg.py` | NEW | 3 |
| 25 | `tests/test_multi_leg_e2e.py` | NEW | 3 |

**Total: 7 new files, 7 modified files, 3 migrations.**

---

## 5. Phase 1 — Core Data Structures

### 5.1 `MultiLegOptionStrategy` Base Class

**File:** `src/backtest/strategy/multi_leg_base.py`

```python
"""
Base class for multi-leg option strategies (4-leg: iron condor, iron butterfly).

Key difference from DirectionalOptionStrategy:
- Emits OpeningIntent (specific structure), NOT MarketView (directional signal).
- Regime gate is internal.
- No directional bias. The structure IS the signal.

The driver calls:
    strategy.generate_opening_intent(candles_window, context) -> OpeningIntent | None
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional, List
import pandas as pd


class ProtectedExitRefusedError(Exception):
    """Raised when an exit quote fails the ltp<=0 guard or collar check."""


@dataclass(frozen=True)
class LegSpec:
    """Specification for one leg of a multi-leg structure."""
    side: str          # 'sell' or 'buy'
    type: str          # 'call' or 'put'
    strike_offset_points: int  # 0 = ATM, +N = N points above, -N = N points below
    # All offsets in POINTS. Engine converts to strikes using strike_interval.


@dataclass(frozen=True)
class OpeningIntent:
    """The strategy's instruction to open a multi-leg structure."""
    structure_type: str       # 'iron_condor' | 'iron_butterfly'
    legs: List[LegSpec]       # 4 legs
    lot_count: int            # 1-3
    wing_width_points: int    # distance from short strike to wing strike
    short_offset_points: int  # distance from ATM to short strike (0 = ATM, >0 = OTM)
    entry_context: dict       # Snapshot of indicators at entry (for audit)


class MultiLegOptionStrategy(ABC):
    name: str = "multi_leg_base"
    structure_type: str = "iron_condor"
    eligible_instruments: List[str] = ["NIFTY", "BANKNIFTY"]

    params: dict = {
        "wing_width_points": {"default": 500, "type": "int"},
        "short_offset_points": {"default": 0, "type": "int"},
        "lot_count": {"default": 1, "type": "int", "min": 1, "max": 3},
        "iv_rv_threshold": {"default": 3.0, "type": "float"},
        "vix_min": {"default": 15.0, "type": "float"},
        "vix_max": {"default": 30.0, "type": "float"},
        "adx_max": {"default": 20.0, "type": "float"},
        "stop_loss_pct": {"default": 0.50, "type": "float"},
        "take_profit_pct": {"default": 0.60, "type": "float"},
        "min_dte": {"default": 5, "type": "int"},
        "max_dte": {"default": 14, "type": "int"},
        "squareoff_dte": {"default": 2, "type": "int"},
        "collar_mode": {"default": "enforce", "type": "str"},
        "collar_max_dev": {"default": 25.0, "type": "float"},
    }

    def __init__(self, params: Optional[dict] = None):
        self._params = dict(self.params)
        if params:
            self._params.update(params)

    def generate_opening_intent(self, candles: pd.DataFrame, context: dict) -> Optional[OpeningIntent]:
        if not self._regime_gate(candles, context):
            return None
        if not self._entry_condition(candles, context):
            return None
        dte = context['dte']
        if dte < self.min_dte or dte > self.max_dte:
            return None
        atm = context['atm_strike']
        wing = self.wing_width_points
        short_off = self.short_offset_points
        interval = context['strike_interval']
        legs = self._build_legs(atm, wing, short_off, interval)
        return OpeningIntent(
            structure_type=self.structure_type,
            legs=legs,
            lot_count=self.lot_count,
            wing_width_points=wing,
            short_offset_points=short_off,
            entry_context={
                'spot': context['spot'],
                'iv_7dte': context['iv_7dte'],
                'rv_20d': context['rv_20d'],
                'adx': context['adx_14'],
                'vix': context['vix'],
                'bar_time': context['bar_time'],
            },
        )

    @abstractmethod
    def _build_legs(self, atm_strike: Decimal, wing_width_points: int,
                    short_offset_points: int, strike_interval: int) -> List[LegSpec]:
        ...

    @abstractmethod
    def _regime_gate(self, candles: pd.DataFrame, context: dict) -> bool:
        ...

    @abstractmethod
    def _entry_condition(self, candles: pd.DataFrame, context: dict) -> bool:
        ...

    @property
    def wing_width_points(self) -> int:
        return int(self._params.get('wing_width_points', 500))

    @property
    def short_offset_points(self) -> int:
        return int(self._params.get('short_offset_points', 0))

    @property
    def lot_count(self) -> int:
        return int(self._params.get('lot_count', 1))

    @property
    def min_dte(self) -> int:
        return int(self._params.get('min_dte', 5))

    @property
    def max_dte(self) -> int:
        return int(self._params.get('max_dte', 14))

    @property
    def squareoff_dte(self) -> int:
        return int(self._params.get('squareoff_dte', 2))

    @property
    def collar_mode(self) -> str:
        return str(self._params.get('collar_mode', 'enforce'))

    @property
    def collar_max_dev(self) -> Decimal:
        return Decimal(str(self._params.get('collar_max_dev', 25.0)))
```

### 5.2 `IronCondor` and `IronButterfly` Structure Classes

**File:** `src/backtest/options/structures.py`

```python
@dataclass(frozen=True)
class IronCondor:
    """
    4-leg defined-risk structure.

    Legs (for NIFTY ATM 25000, short_offset 100, wing_width 500, interval 50):
      1. SELL 25100 CE (ATM + 100, OTM call)
      2. SELL 24900 PE (ATM - 100, OTM put)
      3. BUY  25600 CE (ATM + 100 + 500 = wing, further OTM call)
      4. BUY  24400 PE (ATM - 100 - 500 = wing, further OTM put)

    Short legs collect credit. Long wings cap the loss.
    Max loss = (wing_width - net_credit) * lot_count, clamped >= 0.
    """
    underlying: str
    atm_strike: Decimal
    short_strike_call: Decimal   # ATM + short_offset
    short_strike_put: Decimal    # ATM - short_offset
    wing_strike_call: Decimal    # ATM + short_offset + wing_width
    wing_strike_put: Decimal     # ATM - short_offset - wing_width
    expiry: str
    lot_count: int
    leg_premiums: tuple  # (sell_ce, sell_pe, buy_ce, buy_pe)
    short_offset_points: int
    wing_width_points: int

    @property
    def net_credit(self) -> Decimal:
        return self.leg_premiums[0] + self.leg_premiums[1] - self.leg_premiums[2] - self.leg_premiums[3]

    @property
    def wing_width(self) -> Decimal:
        return self.wing_strike_call - self.short_strike_call

    @property
    def max_loss_per_lot(self) -> Decimal:
        raw = self.wing_width * Decimal(self.lot_count) - self.net_credit * Decimal(self.lot_count)
        return max(raw, Decimal('0'))

    @property
    def max_profit_per_lot(self) -> Decimal:
        return self.net_credit * Decimal(self.lot_count)


@dataclass(frozen=True)
class IronButterfly:
    """
    4-leg defined-risk structure.

    Legs (for NIFTY ATM 25000, wing_width 500, interval 50):
      1. BUY  25000 CE (ATM call)
      2. BUY  25000 PE (ATM put)
      3. SELL 25500 CE (ATM + 500, OTM call)
      4. SELL 24500 PE (ATM - 500, OTM put)

    Net debit = (CE_buy + PE_buy) - (CE_sell + PE_sell)
    Max profit = wing_width - net_debit
    Max loss = net_debit
    """
    underlying: str
    atm_strike: Decimal
    wing_strike_call: Decimal
    wing_strike_put: Decimal
    expiry: str
    lot_count: int
    leg_premiums: tuple  # (buy_ce, buy_pe, sell_ce, sell_pe)
    wing_width_points: int

    @property
    def net_debit(self) -> Decimal:
        return self.leg_premiums[0] + self.leg_premiums[1] - self.leg_premiums[2] - self.leg_premiums[3]

    @property
    def wing_width(self) -> Decimal:
        return self.wing_strike_call - self.atm_strike

    @property
    def max_loss_per_lot(self) -> Decimal:
        return self.net_debit * Decimal(self.lot_count)

    @property
    def max_profit_per_lot(self) -> Decimal:
        raw = self.wing_width * Decimal(self.lot_count) - self.net_debit * Decimal(self.lot_count)
        return max(raw, Decimal('0'))
```

### 5.3 `OffsetSelector`

**File:** `src/backtest/options/selector.py`

```python
class OffsetSelector:
    """
    Select a strike at a fixed offset (in points) from ATM.

    Args:
        offset_points: int (e.g., 100 for short leg, 500 for wing)
        direction: 'above' or 'below'
        strike_interval: int (from instrument master, e.g., 50 for NIFTY)

    Returns:
        The nearest valid strike to (ATM ± offset_points).
    """

    def __init__(self, offset_points: int, direction: str, strike_interval: int):
        assert direction in ('above', 'below')
        assert offset_points >= 0, "offset_points must be non-negative"
        assert strike_interval > 0
        self.offset_points = offset_points
        self.direction = direction
        self.strike_interval = strike_interval

    def select(self, atm_strike: Decimal) -> Decimal:
        sign = 1 if self.direction == 'above' else -1
        target = atm_strike + Decimal(sign * self.offset_points)
        strikes_below = (target // Decimal(self.strike_interval)) * Decimal(self.strike_interval)
        strikes_above = strikes_below + Decimal(self.strike_interval)
        if abs(target - strikes_below) <= abs(strikes_above - target):
            return strikes_below
        return strikes_above
```

### 5.4 Combined Margin

**File:** `src/backtest/options/margin.py`

```python
def combined_margin(legs: list[dict], underlying: str) -> Decimal:
    """
    Combined SPAN + Exposure margin for a multi-leg structure.

    Defined-risk formula (V1 conservative):
        margin = max(0, sum(sell_leg_margins) - sum(buy_leg_margins)) * lot_count

    V2: use NSE's actual SPAN + Exposure formula with inter-spread benefits.
    """
    sell_margins = []
    buy_margins = []
    for leg in legs:
        m = margin(leg, underlying)
        if leg['side'] == 'sell':
            sell_margins.append(m)
        else:
            buy_margins.append(m)

    if not sell_margins:
        return Decimal('0')

    total_sell = sum(sell_margins)
    total_buy = sum(buy_margins)
    combined = max(Decimal('0'), total_sell - total_buy)
    return combined
```

### 5.5 Multi-Leg Greeks

**File:** `src/backtest/options/multi_leg_greeks.py`

```python
def compute_net_greeks(legs: list[dict]) -> dict:
    sign_map = {'sell': -1, 'buy': 1}
    net = {'delta': Decimal('0'), 'gamma': Decimal('0'),
           'theta': Decimal('0'), 'vega': Decimal('0')}
    for leg in legs:
        s = sign_map[leg['side']] * Decimal(leg['lot_count'])
        net['delta'] += s * leg['delta']
        net['gamma'] += s * leg['gamma']
        net['theta'] += s * leg['theta']
        net['vega'] += s * leg['vega']
    return net
```

### 5.6 Phase 1 Tests

| # | Test | Assertion |
|---|------|-----------|
| T1.1 | `LegSpec` frozen | Modifying raises `FrozenInstanceError` |
| T1.2 | `OpeningIntent` frozen | Same |
| T1.3 | Regime gate fails → `None` | Mock `_regime_gate` → False |
| T1.4 | DTE < min_dte → `None` | `dte=3`, `min_dte=5` |
| T1.5 | DTE > max_dte → `None` | `dte=20`, `max_dte=14` |
| T1.6 | All gates pass → `OpeningIntent` with 4 legs | Assert `len(legs) == 4` |
| T1.7 | `IronCondor.net_credit` correct | Sell CE 500, Sell PE 450, Buy CE 100, Buy PE 80 → 770 |
| T1.8 | `IronCondor.max_loss_per_lot` clamped ≥ 0 | Wing 500, credit 770 → max(0, 500-770) = 0 |
| T1.9 | `IronCondor.max_loss_per_lot` positive when credit < wing | Wing 500, credit 300 → 200 |
| T1.10 | `IronButterfly.net_debit` correct | Buy CE 500, Buy PE 450, Sell CE 100, Sell PE 80 → 770 |
| T1.11 | `OffsetSelector` above | ATM=25000, offset=500, interval=50 → 25500 |
| T1.12 | `OffsetSelector` below | ATM=25000, offset=500, interval=50 → 24500 |
| T1.13 | `OffsetSelector` zero offset | ATM=25000, offset=0 → 25000 |
| T1.14 | `compute_net_greeks` delta-neutral | Iron condor net delta ≈ 0 (within 0.05) |
| T1.15 | `compute_net_greeks` short gamma | Net gamma < 0 for iron condor |
| T1.16 | `compute_net_greeks` long theta | Net theta > 0 for iron condor |
| T1.17 | `combined_margin` positive | 4 legs, margin > 0 |
| T1.18 | `combined_margin` less than sum of sells | Defined-risk offset |

---

## 6. Phase 2 — Execution Engine

### 6.1 Multi-Leg Exit Logic

**File:** `src/backtest/options/multi_leg_exit.py`

Exit is applied to NET structure P&L, not per-leg.

```
Net P&L at bar t = (net_premium_at_t - net_premium_at_entry) * lot_count - fees

net_premium_at_t = sum(side_sign * current_premium_leg_i)
side_sign = -1 for sell legs, +1 for buy legs

For iron condor:
    net_premium_at_t = (-ce_sell_t - pe_sell_t + ce_buy_t + pe_buy_t)
```

Exit precedence:
1. Emergency (engine-level) → close all 4 legs at market
2. Stop loss: net_PnL < -(stop_loss_pct × net_credit_at_entry) × lot_count
3. Take profit: net_PnL > +(take_profit_pct × net_credit_at_entry) × lot_count
4. DTE: dte <= squareoff_dte → close all 4 legs

All exits checked at BAR CLOSE. Intrabar path NOT modelled.

**VWAP settlement caveat:** Last-30-min VWAP requires intraday bars. 1D backtest settles at expiry-bar close with `settlement_source: "expiry_bar_close"` and explicit provenance. 15-min paper mode computes last-30-min VWAP with `settlement_source: "vwap_30min"`.

```python
class ExitReason(Enum):
    EMERGENCY = "emergency"
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    DTE_EXPIRY = "dte_expiry"
    SQUARE_OFF = "square_off"


class MultiLegExitState:
    def __init__(self, entry_net_premium, entry_bar_time, stop_loss_pct,
                 take_profit_pct, squareoff_dte, lot_count, collar_mode='enforce',
                 collar_max_dev=Decimal('25')):
        self.entry_net_premium = entry_net_premium
        self.entry_bar_time = entry_bar_time
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct
        self.squareoff_dte = squareoff_dte
        self.lot_count = lot_count
        self.collar_mode = collar_mode
        self.collar_max_dev = Decimal(str(collar_max_dev))

    def check_exit(self, current_net_premium, dte, bar_time,
                   mark_price: Optional[Decimal] = None,
                   fill_price: Optional[Decimal] = None):
        net_pnl = (current_net_premium - self.entry_net_premium) * Decimal(self.lot_count)

        # Collar check (before P&L checks)
        if fill_price is not None and mark_price is not None:
            dev = abs(fill_price - mark_price)
            bound = max(self.collar_max_dev, Decimal('0.05') * mark_price)
            if dev > bound:
                if self.collar_mode == 'enforce':
                    return True, ExitReason.EMERGENCY, 'collar_violation_enforce'
                if self.collar_mode == 'shadow':
                    # journal only, take fill
                    pass

        sl_threshold = -(self.stop_loss_pct * self.entry_net_premium) * Decimal(self.lot_count)
        if net_pnl <= sl_threshold:
            return True, ExitReason.STOP_LOSS, None

        tp_threshold = self.take_profit_pct * self.entry_net_premium * Decimal(self.lot_count)
        if net_pnl >= tp_threshold:
            return True, ExitReason.TAKE_PROFIT, None

        if dte <= self.squareoff_dte:
            return True, ExitReason.SQUARE_OFF, None

        return False, None, None
```

### 6.2 Exit Pricing — Bid/Ask Side + ltp<=0 Guard

**File:** `src/backtest/options/multi_leg_exit.py` (continued)

```python
def price_exit_quote(leg: dict, quote: LegQuote) -> Decimal:
    """
    Price an exit quote using the correct side of the spread.

    Closing a LONG → you SELL → hit the BID.
    Closing a SHORT → you BUY → hit the ASK.

    Raises ProtectedExitRefusedError if ltp <= 0.
    """
    side = leg['side']  # 'sell' or 'buy' (the position side)

    if side == 'buy':
        # We are long; to exit we sell → hit BID
        price = quote.bid
    else:
        # We are short; to exit we buy → hit ASK
        price = quote.ask

    if price is None or price <= 0:
        # Fall back to LTP only if bid/ask unavailable
        price = quote.ltp

    if price is None or price <= 0:
        raise ProtectedExitRefusedError(
            f"Exit quote failed: ltp={quote.ltp}, bid={quote.bid}, ask={quote.ask} for leg {leg}"
        )

    return price
```

### 6.3 Feed Subscription — Pre-Check Before Entry

**File:** `src/backtest/options/feed_subscription.py`

```python
class FeedSubscriptionManager:
    """
    Ensures leg tokens are subscribed on the broker feed before entry.
    Re-subscribes open-leg tokens on broker re-login from DB.
    """

    def __init__(self, broker_feed, db):
        self.feed = broker_feed
        self.db = db

    def pre_check_entry(self, legs: list[dict], runner_id: str) -> None:
        """
        Verify all leg tokens are subscribed. Refuse entry if any missing.
        """
        for leg in legs:
            token = leg.get('feed_token')
            if not token:
                raise ProtectedExitRefusedError(
                    f"Leg {leg['instrument_id']} has no feed token — not subscribed"
                )
            if not self.feed.is_subscribed(token):
                raise ProtectedExitRefusedError(
                    f"Leg {leg['instrument_id']} (token {token}) not subscribed on feed"
                )

    def subscribe_entry_legs(self, legs: list[dict], runner_id: str) -> None:
        """Subscribe all leg tokens before placing entry order."""
        for leg in legs:
            self.feed.subscribe(leg['feed_token'])
            self.db.upsert_option_open_leg(runner_id, leg)

    def resubscribe_on_relogin(self, runner_id: str) -> None:
        """Re-subscribe open-leg tokens from DB after broker re-login."""
        open_legs = self.db.get_option_open_legs(runner_id, status='open')
        for leg in open_legs:
            self.feed.subscribe(leg['feed_token'])
```

### 6.4 Bridge: 4-Leg Atomic Execution

**File:** `src/backtest/forward/options_bridge.py`

```python
def execute_multi_leg(intent: OpeningIntent, context: dict,
                      feed_subscription: FeedSubscriptionManager) -> MultiLegResult:
    """
    Place 4 orders atomically. All fill or none.
    Paper mode: all 4 fill immediately at model price.

    Steps:
    1. Resolve strikes for all 4 legs via OffsetSelector
    2. Feed-subscription pre-check (refuse entry if any token unsubscribed)
    3. Price all 4 legs (bid/ask side for exit readiness)
    4. Compute net credit/debit, margin, net Greeks, max loss
    5. Paper: all 4 fill immediately
    6. Book position in multi_leg_structures
    7. Register open legs in option_open_legs for re-login re-subscription
    """
    # 1. Resolve strikes
    legs_resolved = []
    for leg_spec in intent.legs:
        selector = OffsetSelector(
            leg_spec.strike_offset_points,
            'above' if leg_spec.strike_offset_points >= 0 else 'below',
            context['strike_interval'],
        )
        strike = selector.select(context['atm_strike'])
        legs_resolved.append({**leg_spec.__dict__, 'strike': strike, 'abs_offset': abs(leg_spec.strike_offset_points)})

    # 2. Feed subscription pre-check
    feed_subscription.pre_check_entry(legs_resolved, context['runner_id'])

    # 3. Price all 4 legs
    premiums, greeks = [], []
    for leg in legs_resolved:
        quote = context['pricing_feed'].quote(
            underlying=context['underlying'],
            strike=leg['strike'],
            expiry=context['nearest_expiry'],
            side=leg['type'],
            spot=context['spot'],
            bar_time=context['bar_time'],
        )
        premiums.append(quote.price)
        greeks.append(quote)

    # 4. Compute net, margin, Greeks, max loss
    net_premium = compute_net_premium(legs_resolved, premiums)
    margin = combined_margin(legs_resolved, context['underlying'])
    net_greeks = compute_net_greeks([
        {'side': leg['side'], 'delta': g.delta, 'gamma': g.gamma,
         'theta': g.theta, 'vega': g.vega, 'lot_count': intent.lot_count}
        for leg, g in zip(legs_resolved, greeks)
    ])
    max_loss = compute_max_loss(intent.structure_type, net_premium,
                                intent.wing_width_points, intent.lot_count)

    # 5. Paper fill
    fills = [LegFill(order_id=str(uuid.uuid4()), fill_price=p,
                     fill_time=context['bar_time']) for p in premiums]

    # 6. Book position
    structure = IronCondor(...)  # or IronButterfly
    db.insert_multi_leg_structure(structure, fills, net_greeks, margin, max_loss)

    # 7. Register open legs
    feed_subscription.subscribe_entry_legs(legs_resolved, context['runner_id'])

    return MultiLegResult(status='filled', legs=fills, net_credit=net_premium,
                          margin_required=margin, net_greeks=net_greeks, max_loss=max_loss)
```

### 6.5 Driver: Accept `OpeningIntent`

**File:** `src/backtest/engine/option_backtest_driver.py`

In the driver's main loop:
```python
for t in range(len(candles)):
    candles_window = candles.iloc[:t+1]
    context = build_context(candles_window, t)

    if isinstance(strategy, MultiLegOptionStrategy):
        intent = strategy.generate_opening_intent(candles_window, context)
        if intent is not None and no_open_position:
            result = execute_multi_leg(intent, context, feed_subscription)
            open_position = result
        elif open_position is not None:
            current_premiums = reprice_legs(open_position, context)
            current_net = compute_net_premium(open_position.legs, current_premiums)
            should_exit, reason, collar_flag = open_position.exit_state.check_exit(
                current_net, context['dte'], context['bar_time'],
                mark_price=context['mark_price'],
                fill_price=current_net)
            if should_exit:
                close_multi_leg(open_position, current_premiums, reason, context)
                open_position = None
    else:
        # Existing MarketView path — UNCHANGED
        view = strategy.generate_market_view(candles_window)
        ...
```

No MarketView adapter. The `isinstance` check routes directly. No regression to 2-leg path.

### 6.6 Phase 2 Tests

| # | Test | Assertion |
|---|------|-----------|
| T3.1 | Stop loss fires at 50% of credit | Entry credit ₹800/lot, SL 50% → net P&L = -₹400 → exit |
| T3.2 | Take profit fires at 60% of credit | Entry credit ₹800/lot, TP 60% → net P&L = +₹480 → exit |
| T3.3 | DTE exit at 2 DTE | dte=2 → exit |
| T3.4 | No exit within SL/TP | net P&L = +₹200 → no exit |
| T3.5 | Exit precedence: SL before TP | Both hit same bar → SL wins |
| T3.6 | Emergency exit overrides all | Emergency flag → exit regardless of P&L |
| T3.7 | `ltp <= 0` raises `ProtectedExitRefusedError` | Quote with ltp=0 → exception, structure stays open |
| T3.8 | Exit uses bid for long, ask for short | Long leg exit → bid price; short leg exit → ask price |
| T3.9 | Collar enforce refuses off-mark fill | dev > max(₹25, 5%×mark) → refuse |
| T3.10 | Collar shadow journals but takes fill | dev > bound → journal, proceed |
| T3.11 | Collar off no check | dev > bound → proceed, no journal |
| T4.1 | 4-leg atomic fill (paper) | All 4 fill, position booked |
| T4.2 | Feed pre-check refuses unsubscribed leg | Missing token → exception, no position |
| T4.3 | Strike resolution NIFTY | ATM 25000, short_off 100, wing 500, interval 50 → 25100/24900/25600/24400 |
| T4.4 | Strike resolution BANKNIFTY | ATM 54000, short_off 100, wing 500, interval 100 → 54100/53900/54600/53400 |
| T4.5 | Net credit positive | Realistic BS prices |
| T4.6 | Margin < sum of sell margins | Defined-risk offset |
| T4.7 | Net Greeks: delta≈0, gamma<0, theta>0, vega<0 | Iron condor |
| T4.8 | Max loss = max(0, wing×lot − credit×lot) | Correct clamped formula |
| T4.9 | Position persisted to `multi_leg_structures` | DB row with correct fields |
| T4.10 | `certifiable: false` | Quarantine |
| T4.11 | Open legs registered in `option_open_legs` | Re-subscription source populated |

---

## 7. Phase 3 — API + UI + Plugin Rewrite

### 7.1 Plugin Rewrite: `iron_condor.py`

**File:** `plugins/strategies/iron_condor.py`

```python
"""
Iron Condor: 4-leg defined-risk structure.

Sell OTM CE + Sell OTM PE + Buy further OTM CE + Buy further OTM PE.
Profits from theta decay in a range-bound market.

Regime gate: IV > RV + 3, ADX < 20, VIX 15-30.
Entry: regime gate passes + DTE in [min_dte, max_dte].
Exit: SL 50% of credit, TP 60% of credit, DTE squareoff.
Collar: enforce mode.
"""

from decimal import Decimal
from backtest.strategy.multi_leg_base import (
    MultiLegOptionStrategy, LegSpec, OpeningIntent, ProtectedExitRefusedError
)


class IronCondorStrategy(MultiLegOptionStrategy):
    name = "iron_condor"
    structure_type = "iron_condor"
    description = "4-leg defined-risk: sell OTM strangle, buy OTM wings"
    eligible_instruments = ["NIFTY", "BANKNIFTY"]

    params = {
        "wing_width_points": {"default": 500, "type": "int"},
        "short_offset_points": {"default": 100, "type": "int"},
        "lot_count": {"default": 1, "type": "int", "min": 1, "max": 3},
        "iv_rv_threshold": {"default": 3.0, "type": "float"},
        "vix_min": {"default": 15.0, "type": "float"},
        "vix_max": {"default": 30.0, "type": "float"},
        "adx_max": {"default": 20.0, "type": "float"},
        "stop_loss_pct": {"default": 0.50, "type": "float"},
        "take_profit_pct": {"default": 0.60, "type": "float"},
        "min_dte": {"default": 5, "type": "int"},
        "max_dte": {"default": 14, "type": "int"},
        "squareoff_dte": {"default": 2, "type": "int"},
        "collar_mode": {"default": "enforce", "type": "str"},
        "collar_max_dev": {"default": 25.0, "type": "float"},
    }

    def _build_legs(self, atm_strike: Decimal, wing_width_points: int,
                    short_offset_points: int, strike_interval: int):
        return [
            LegSpec(side="sell", type="call", strike_offset_points=short_offset_points),
            LegSpec(side="sell", type="put",  strike_offset_points=-short_offset_points),
            LegSpec(side="buy",  type="call", strike_offset_points=short_offset_points + wing_width_points),
            LegSpec(side="buy",  type="put",  strike_offset_points=-(short_offset_points + wing_width_points)),
        ]

    def _regime_gate(self, candles, context) -> bool:
        iv = context['iv_7dte']
        rv = context['rv_20d']
        adx = context['adx_14']
        vix = context['vix']
        if iv < rv + Decimal(str(self.params.get('iv_rv_threshold', 3.0))):
            return False
        if adx > Decimal(str(self.params.get('adx_max', 20.0))):
            return False
        if vix < Decimal(str(self.params.get('vix_min', 15.0))) or vix > Decimal(str(self.params.get('vix_max', 30.0))):
            return False
        return True

    def _entry_condition(self, candles, context) -> bool:
        return True
```

### 7.2 Plugin Rewrite: `theta_harvest.py`

Same as `IronCondorStrategy` with different defaults:
- `vix_max: 35`
- `adx_max: 25`
- `take_profit_pct: 0.50`
- `short_offset_points: 100`

### 7.3 API Changes

**File:** `src/backtest/api/options_model.py`

Add `structure: "iron_condor"` | `"iron_butterfly"` to request. Validation:
- `wing_width_points` must be a multiple of strike interval for the underlying
- `short_offset_points` must be a multiple of strike interval
- `lot_count × max_loss ≤ capital × 0.95`
- Response includes 4 legs per trade, net Greeks, itemised fees, `certifiable: false`, `pricing_feed`, `settlement_source`

Response trade example:
```json
{
  "id": 1,
  "structure_type": "iron_condor",
  "entry_date": "2025-12-15",
  "exit_date": "2025-12-19",
  "underlying": "NIFTY",
  "atm_strike": 25000,
  "short_strike_call": 25100,
  "short_strike_put": 24900,
  "wing_strike_call": 25600,
  "wing_strike_put": 24400,
  "expiry": "2025-12-18",
  "lot_count": 1,
  "legs": [...],
  "net_credit_at_entry": 785.0,
  "net_premium_at_exit": 525.0,
  "realised_pnl": 260.0,
  "fees": {"stt": 12.5, "exchange": 40.0, "sebi": 40.0, "stamp": 2.0, "gst": 18.0, "brokerage": 80.0, "exercise_stt": 0.0, "total": 192.5},
  "net_delta_at_entry": 0.02,
  "net_gamma_at_entry": -0.003,
  "net_theta_at_entry": 12.5,
  "net_vega_at_entry": -8.2,
  "max_loss": 215.0,
  "margin_required": 42000.0,
  "exit_reason": "take_profit",
  "settlement_source": "expiry_bar_close",
  "pricing_feed": "model_bs",
  "certifiable": false
}
```

### 7.4 Phase 3 Tests

| # | Test | Assertion |
|---|------|-----------|
| T6.1 | Valid iron condor request → 200 | 4-leg trades, Greeks, fees |
| T6.2 | `wing_width_points` not multiple of interval → 400 | Validation error |
| T6.3 | `lot_count × max_loss > capital × 0.95` → 400 | Position size error |
| T6.4 | Response includes `certifiable: false` | Present |
| T6.5 | Response includes `pricing_feed: "model_bs"` | Present |
| T6.6 | Response includes 4 legs per trade | `len(trade['legs']) == 4` |
| T6.7 | Response includes net Greeks | `net_delta_at_entry`, etc. |
| T6.8 | Response includes itemised fees for 4 legs | `fees.total` = sum |
| T7.1 | E2e: POST → 200 → readiness → `certifiable: false` | Quarantine |
| T7.2 | Existing 4,849-test suite passes | No regression |
| T7.3 | Existing 2-leg strategies still work | No regression |

---

## 8. Honest Assessment of Implemented Plugins

| Plugin | Claimed | Reality | Action |
|--------|---------|---------|--------|
| `opening_range_breakout` | Implemented | 2-leg/equity, works in existing engine | Keep |
| `mean_reversion_extreme` | Implemented | 2-leg, works in existing engine | Keep |
| `pullback_in_uptrend` | Implemented | 2-leg, works in existing engine | Keep |
| `pre_expiry_gamma` | Implemented | Short straddle approximation, no wings | Rewrite after multi-leg base |
| `theta_harvest` | Implemented | Short straddle approximation, no wings | Rewrite (this doc) |
| `iron_condor` | "S5 approx" | Short straddle (2-leg), undefined tail risk | Rewrite (this doc, priority) |

**Critical risk:** `iron_condor.py` and `theta_harvest.py` are currently naked short straddles with no wings. A 3% NIFTY move in 2 days produces effectively unbounded loss. These must not reach paper trading until the 4-leg rewrite is complete.

---

## 9. Self-Checklist (Before Delivering)

- [ ] Verified assumptions against actual code (not README)
- [ ] Every monetary calculation in `Decimal`
- [ ] Backtest deterministic (two runs → byte-identical)
- [ ] Every output carries provenance (source, feed, timestamp, run_id)
- [ ] Quarantine intact (model outputs cannot reach production)
- [ ] Fees complete (STT, exchange, SEBI, stamp, GST, brokerage, exercise STT)
- [ ] Exit collar: off/shadow/enforce implemented
- [ ] `ltp <= 0` → `ProtectedExitRefusedError`
- [ ] Exit uses bid (long close) / ask (short close)
- [ ] Feed-subscription pre-check before entry
- [ ] Re-subscribe on re-login from `option_open_legs`
- [ ] Lot sizes/strike intervals/expiry from broker master, not hardcoded
- [ ] Iron condor sells OTM, never ATM
- [ ] `IronButterfly` fully implemented (no empty body)
- [ ] `max_loss_per_lot` clamped ≥ 0
- [ ] `OpeningIntent` never wrapped back into `MarketView`
- [ ] VWAP settlement scoped to 15-min paper; 1D uses expiry-bar-close with provenance
- [ ] `strike_offset_points` unit consistent (points everywhere)
- [ ] Combined margin uses sell − buy offset, not sum of sells
- [ ] Edge cases tested (empty data, NaN, negative, zero)
- [ ] Failure cases tested (broker down, data gap, DB error)
- [ ] Test suite passes (4,849 + new tests)
- [ ] Rollback plan exists
- [ ] Kill switch exists
- [ ] Documentation complete
- [ ] Uncertainties flagged
- [ ] Data ownership boundary respected (strategy never calls broker API)
- [ ] Bar timestamp used, not `datetime.now()`
- [ ] Error handling fail-closed (unknown → refuse)
- [ ] Test written before code
- [ ] Code is boring (simple, predictable)
