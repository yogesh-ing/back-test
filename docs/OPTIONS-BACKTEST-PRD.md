# PRD: Options Backtesting Engine — Phase A (MVP)

**Version:** 4.2
**Status:** 🟢 Phase 0 + A2 (export) + A3 (seam) + A4 (loop) done — A1/A5–A7 open (A6 partially: ID/alert determinism landed)
**Owner:** Strategy Platform
**Supersedes:** PRD v1 / v2 / v3 (see [Corrections log](#corrections-log))
**Task tracker:** [`docs/OPTIONS-BACKTEST-TASKS.md`](OPTIONS-BACKTEST-TASKS.md)

> **This document is the source of truth for the options backtest.**
> Every signature in [Appendix A](#appendix-a--verified-signatures) was read
> from the code, not reconstructed. If a claim here disagrees with the code,
> the code wins — fix this document in the same commit.

---

## 1. Objective

Build a deterministic, model-driven backtesting environment for options
strategies: the **first production caller** of the options expression layer
(`MarketView → selector → structure builder → TradeIntent → broker`).

The backtest validates **mechanics** — structure construction, cost
accounting, margin checks, expiry settlement, exit discipline — against
synthetic Black-Scholes pricing. It does **not** validate edge against
realised market premiums.

## 2. Scope

### In scope (Phase A)

| Area | Decision |
|---|---|
| Structures | **4**: `long_call`, `long_put`, `bull_call_spread`, `bear_put_spread` |
| Underlying | `NIFTY` (synthetic scenarios); `BANKNIFTY` works but is untested |
| Bar interval | Daily |
| Pricing | Synthetic Black-Scholes, flat IV per underlying |
| Fills | LTP + the broker's existing per-leg `slippage_pct` |
| Expiry | `ExpiryManager` — DTE square-off **and** cash settlement |
| Metrics | Equity curve, trade log, drawdown, Sharpe |
| Loop | **New dedicated options loop** (does not touch `run_engine_loop`) |

### Out of scope (Phase B)

- `straddle`, `strangle`, `iron_condor`, `calendar_spread` — **blocked on a
  chain-shape change**, not merely unwritten (see §5)
- Real historical option chains / real underlying bars
- Bid/ask fills (the *data* already exists — see §6)
- Intraday bars, liquidity gates
- Greek P&L decomposition (delta/theta/vega attribution)
- Walk-forward optimisation

## 3. Why 4 structures, not 8

`TradeIntent._VALID_STRUCTURES` accepts 8 names, but
`create_structure()` implements 4 and raises `ValueError` for the rest.

The other four are not simply unimplemented — the **chain shape forecloses
them**. A chain is `dict[Decimal, OptionContract]`: keyed by strike and
**single-sided** (`generate_chain` materialises CE *or* PE), and
`OptionStructure.build()` takes exactly **one** `expiry`.

| Structure | Why it cannot be built today |
|---|---|
| `straddle`, `strangle` | Needs CE **and** PE — a `dict[strike]` cannot hold both |
| `iron_condor` | Needs CE + PE across four strikes — same collision |
| `calendar_spread` | Needs two expiries; `build()` accepts one |

Promising "all 8 structures" in Phase A would fail in week two. Phase A is
scoped to 4; widening requires the chain refactor in §5.

## 4. Architecture

```
Synthetic daily bars (trending / range / volatile-drop scenarios)
        │
        ├─ SyntheticChainGenerator.set_spot(underlying, close)      ← per bar
        ├─ generator.generate_chain(underlying, expiry, option_type="CE")
        ├─ SyntheticQuoteProvider.register_chain(chain)             ← REQUIRED
        │
        ▼
DirectionalOptions.generate_market_view(candles) → MarketView | None
        │
        ▼
create_selector(type).pick_strikes(...)  → list[Decimal]
        │
        ▼
create_structure(type).build(view, strikes, chain, expiry, strategy_name)
        → TradeIntent   (estimated_premium populated — T0.1)
        │
        ▼
OptionPaperBroker.execute_structure(intent, quote_provider, timestamp)
        │
        ├─ update_mtm(quote_provider, timestamp)                    ← per bar
        └─ ExpiryManager(broker).process_expiries(
               quote_provider, settlement_provider, as_of=timestamp)
        │
        ▼
compute_metrics(result) + trade log (exit_reason per structure — T0.2)
```

### Components reused unchanged

| Component | Path | Notes |
|---|---|---|
| `OptionPaperBroker` | `src/backtest/options/paper_trading.py` | Accounting, slippage, atomic multi-leg fills already correct |
| `SyntheticChainGenerator` | `src/backtest/options/quote_providers.py` | `set_spot()` + `generate_chain()` |
| `SyntheticQuoteProvider` | same | BS pricing, and **already returns bid/ask** |
| `bs_price` | same | The pricing kernel |
| `ExpiryManager` | `src/backtest/options/expiry.py` | `process_expiries(as_of=...)` |
| `create_selector` / `create_structure` | `src/backtest/options/{selector,structures}.py` | The expression layer |
| `DirectionalOptions` | `src/backtest/strategies/option_directional.py` | View producer |
| `compute_metrics` | `src/backtest/engine/metrics.py` | Portfolio metrics only — see the reuse caveat below. **Not** a `MetricsCalculator` class |
| Options trade log | `src/backtest/engine/option_backtest_driver.py` | `build_trade_log()` — per-structure, because `walk_trades` cannot model a multi-structure book |
| `SyntheticSource` | `src/backtest/data/synthetic.py` | Scenario bars |
| `DataSource` protocol | `src/backtest/data/base.py` | As-is; no new protocol needed |

### New code (deliberately minimal)

| File | Status | Purpose |
|---|---|---|
| `src/backtest/engine/option_backtest_driver.py` | 🟢 seam (A3) + export (A2) + **loop (A4)** | `build_intent_from_view()` converts a view + chain into an intent; `build_trade_log()` / `capture_equity()` produce the run's outputs; `OptionBacktestDriver` runs the bar-by-bar loop and returns a `BacktestResult` (trade log, equity curve, alerts, metrics) |
| `config/backtest_options.yaml` | ⬜ | Backtest configuration |
| Scenario helpers | ⬜ | Added to `data/synthetic.py` (shape TBD — §7) |

### The seam (A3), as implemented

```python
build_intent_from_view(
    view,                    # MarketView | None — None/NEUTRAL means no trade
    chain,                   # dict[Decimal, OptionContract], one side
    expiry,                  # date
    *,
    strategy_name: str = "",
    selector_type: str = "atm",
    selector_kwargs: Mapping[str, Any] | None = None,
    decider: Callable[[MarketView], str | None] | None = None,
) -> TradeIntent | None
```

Three properties this deliberately holds:

1. **The structure decision is injected.** `decider` defaults to
   `default_structure_decider` (documented as *policy, not mechanism*) so
   the engine carries no strategy opinion.
2. **Direction is compared against the `Direction` enum**, never a string —
   a string comparison is always `False` and yields a silently empty
   backtest.
3. **Out-of-scope structures raise `UnsupportedStructureError`** (a
   `ValueError`) naming the chain-shape reason, rather than returning
   `None`. Choosing a straddle is configuration, not a market condition.

## 5. Known blocker feeding Phase B

The chain representation:

```python
dict[Decimal, OptionContract]   # today: one side, one expiry
```

must become multi-dimensional — e.g.
`dict[tuple[date, Decimal, OptionType], OptionContract]` — before two-sided
or multi-expiry structures can exist. **Migration cost is not yet
estimated**, and it touches `generate_chain`, `_get_contract`,
`OptionStructure.build()` and every call site.

## 6. Slippage and fills

Two mechanisms exist; Phase A uses the first.

1. **`OptionPaperBroker(slippage_pct=0.001)`** — applied **per leg,
   side-aware** as `price × (1 ± slippage_pct)`. This is already built and
   is what the PRD means by "per-leg bps".
2. **`config/slippage.yaml`** — a `SlippageCalculator` (hybrid
   spread + volume + ATR, in bps, with a `backtest` profile). It is
   **equity-candle-shaped and not read by the options broker.**

Bid/ask fills are closer than they look: `SyntheticQuoteProvider.get_quote`
already returns `bid`/`ask` around LTP (`spread = 0.5`). Making filling
bid/ask-aware is a broker change, not a data-sourcing project.

## 7. Open design questions

1. **Scenario helpers' shape.** `SyntheticSource` is an instance
   `DataSource` (`get_candles(symbol, start, end, interval)`), so
   `@staticmethod` scenario generators do not fit it cleanly. Options:
   module-level functions in `synthetic.py`, a scenario parameter on
   `SyntheticSource`, or a separate `scenarios.py`. **Decide before A1.**
2. **IV skew injection point.** Flat `VOL = {"NIFTY": 0.12, ...}` lives on
   `SyntheticChainGenerator` and is consumed by
   `generator.price_contract(contract, option_type)`. A smile means
   per-strike vol at *that* seam — not a change to the chain generator's
   contract emission.
3. **`estimated_margin`.** Left at `0`; no policy consumes it. Phase B (B3).

## 8. Acceptance criteria

- [ ] 4 structures execute end-to-end through selector → builder → broker
- [ ] **Determinism:** 3 runs → identical trade log and equity curve,
      including IDs. Requires T0.1/T0.2 **plus** A6 (see §9)
- [ ] 3 hand-calculated trades reconcile within **₹1** (per trade)
- [ ] DTE square-off fires at the configured threshold; settlement charges
      intrinsic at expiry
- [ ] Report carries the disclaimer
      *"⚠️ Synthetic Black-Scholes Pricing — Not Historical Market Data"*
- [ ] `run_engine_loop` and the equity backtest are untouched
- [ ] Expiry exits are labelled: `auto_square_off` vs `expiry_settlement`

## 9. Determinism — the real requirement

Clock injection alone is **not** sufficient. Four sources must be addressed:

| Source | Location | Status |
|---|---|---|
| `structure_id = str(uuid.uuid4())` | `paper_trading.py` | ✅ done (2026-09-15) — `struct_{ts}_{seq}`, per-broker monotonic counter |
| `position_id = uuid.uuid4()` | `paper_trading.py` | ✅ done — `pos_{ts}_{seq}` at the broker's construction site (the dataclass uuid4 default remains for ad-hoc construction) |
| `last_updated = datetime.utcnow()` — **unconditional** | `paper_trading.py` | ❌ open (A6 remainder) — historical path now passes timestamps explicitly; verify non-historical callers first |
| `date.today()` in `available_expiries` / `next_monthly_expiry` fallback | `quote_providers.py` | ⚠️ avoided by the driver (explicit `expiry` / `set_reference` per bar); audit-and-default hardening left in A6 |
| `alert_id = uuid4()` (found by A4, not in the original list) | `expiry.py` | ✅ done — sha256 digest of type + timestamp + targets |

**Warning:** `hash(intent)` is **not** a valid ID source. `TradeIntent` is a
`@dataclass(frozen=True)` with a `metadata: dict` field, so `hash()` raises
`TypeError: unhashable type: 'dict'`. Use a monotonic counter or a hash over
explicit scalar fields.

**Also new with A4:** `SyntheticQuoteProvider.set_reference()` — quotes can
be pinned to bar time. Without it, `price_contract`'s `datetime.now()`
fallback gives negative time-to-expiry over historical bars and every
premium collapses to intrinsic value. This was the loop's one
silently-wrong-numbers trap; there is a regression test.

## 10. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Chain shape blocks 4 structures | 🔴 High | Explicit 4-structure scope; §5 refactor in Phase B |
| Driver is net-new — interface friction at the seam | 🔴 High | A3 spiked and implemented first; it is the critical path |
| Determinism blocked by uuid4/utcnow | 🟡 Medium | Dedicated task A6, named sources in §9 |
| Model IV ≠ market IV | 🟡 Medium | Disclaimer; never present as historical performance |
| Demo data mislabelled as real NIFTY (`close ≈ 19.56`) | 🟡 Medium | T0.3 — **pending owner decision**, not yet actioned |

## 11. Corrections log

Each revision claimed its signatures were verified. These were found wrong.

### v3 → v4

| v3 claim | Reality |
|---|---|
| `create_selector` at `backtest/strategy/selectors.py:236` | `backtest/options/selector.py:319` |
| `create_structure` at `backtest/strategy/structures.py:153` | `backtest/options/structures.py:330` |
| `OptionPaperBroker(initial_capital=…)` | Parameter is `capital` |
| `ExpiryManager()` | Requires `broker` positionally |
| `process_expiries(broker=…)` | `process_expiries(quote_provider, settlement_provider, as_of=None)` — no `broker`; `settlement_provider` required |
| `broker.closed_structures` / `open_structures` | `get_open_structures()`; no closed-structure accessor |
| `s.total_margin` | Not a thing; the broker has `total_margin_used` |
| `MetricsCalculator.calculate(…)` | `compute_metrics(result)` — no such class |
| `StructureBuilder` / `BullCallSpreadBuilder` | `OptionStructure` ABC; `LongCall`, `LongPut`, `BullCallSpread`, `BearPutSpread` |
| `view.direction == "NEUTRAL"` | `direction` is a `Direction` **enum** — string compare is always `False`, silently producing a zero-trade backtest |
| `hash(intent) % 10000` for IDs | Raises `TypeError` (see §9) |
| `StructurePosition` in `strategy/intent.py` | `options/paper_trading.py` |
| (silent) — no `register_chain()` call | `SyntheticQuoteProvider` returns `ltp: 0.0` for unregistered tokens; every structure would price at zero |

### Reuse claims that did not survive contact

| Claim | Reality |
|---|---|
| "Reuse `MetricsCalculator.calculate`" | No such class; only `compute_metrics(result)` |
| "Reuse `compute_metrics` for the options backtest" | Only its **portfolio-level** half. It sources trade stats from `walk_trades(equity, position)`, which defines a trade as a run of bars holding one sign — that cannot represent simultaneous multi-leg structures, and would discard `structure_type` and `exit_reason`. Options trade stats come from the structure log instead |
| "Trade log export from `broker.closed_structures`" | No such accessor existed; `get_closed_structures()` was added in A2 |
| "`s.total_margin`" | Not a thing; the broker exposes `total_margin_used` |

### Earlier revisions

All ten v1 module paths were wrong (no `src/backtest/` prefix, five files
that do not exist); v2 repeated the `generate_market_view(chain, market_data)`
signature error. Both are fixed in Appendix A.

---

## Appendix A — Verified signatures

Read from source. Line numbers move; re-verify when they matter.

### `src/backtest/options/paper_trading.py`

```python
class PositionStatus(Enum):        # :45
    OPEN = "open"; CLOSED = "closed"; EXPIRED = "expired"

@dataclass
class OptionPosition:              # :64 — ONE LEG, not a structure
    position_id: str; structure_id: str; strategy_name: str
    instrument_token: str; trading_symbol: str; underlying: str
    option_type: str; strike: Decimal; expiry: date | None; lot_size: int
    side: str; quantity: int; entry_price: Decimal; current_price: Decimal
    status: PositionStatus
    realized_pnl: Decimal; unrealized_pnl: Decimal; commission: Decimal
    opened_at: datetime; closed_at: datetime | None; last_updated: datetime
    metadata: dict
    # properties: total_quantity, is_long, is_short

@dataclass
class StructurePosition:           # :170 — the STRUCTURE (use this for trade logs)
    structure_id: str; structure_type: str; strategy_name: str
    underlying: str; expiry: date
    legs: list[OptionPosition]
    opened_at: datetime; closed_at: datetime | None
    exit_reason: str | None = None         # T0.2
    # properties (NOT fields): is_open, total_entry_cost,
    # total_unrealized_pnl, total_realized_pnl, total_commission

class QuoteProvider(Protocol):      # :219
    def get_quote(self, instrument_token: str) -> dict[str, Any]: ...
    # returns {"ltp": float, "bid": float, "ask": float, ...} — no timestamp

class FakeQuoteProvider:            # :227
    def __init__(self, default_price: float = 100.0) -> None: ...
    def set_price(self, instrument_token: str, price: float) -> None: ...

class OptionPaperBroker:            # :246
    def __init__(self, capital: float = 1_000_000.0,
                 slippage_pct: float = 0.001,
                 commission_per_lot: float = 20.0,
                 fee_calculator: Any | None = None) -> None: ...

    def execute_structure(self, intent: TradeIntent,
                          quote_provider: QuoteProvider,
                          timestamp: datetime | None = None
                          ) -> list[OptionPosition]: ...

    def close_structure(self, structure_id: str,
                        quote_provider: QuoteProvider,
                        timestamp: datetime | None = None,
                        reason: str = "manual",
                        ) -> Decimal: ...        # T0.2 added `reason`

    def update_mtm(self, quote_provider: QuoteProvider,
                   timestamp: datetime | None = None) -> Decimal: ...

    # queries — there is NO closed_structures accessor
    def get_open_positions(self) -> list[OptionPosition]: ...
    def get_positions_by_structure(self, structure_id: str) -> list[OptionPosition]: ...
    def get_open_structures(self) -> list[StructurePosition]: ...
    def get_structure(self, structure_id: str) -> StructurePosition | None: ...

    # equity / cost properties
    #   total_equity, total_commission_paid, total_statutory_fees_paid,
    #   total_costs_paid, total_margin_used
```

### `src/backtest/options/quote_providers.py`

```python
def bs_price(spot: float, strike: float, years_to_expiry: float,
             vol: float, option_type: str,   # "CE" | "PE"
             risk_free: float = 0.065) -> float: ...        # :53

class SyntheticChainGenerator:      # :81
    LOT_SIZES    = {"NIFTY": 75, "BANKNIFTY": 35}
    STRIKE_STEPS = {"NIFTY": 50, "BANKNIFTY": 100}
    VOL          = {"NIFTY": 0.12, "BANKNIFTY": 0.15}       # FLAT IV lives here

    def __init__(self, spot: float | None = None,
                 strikes_each_side: int = 10) -> None: ...
    def set_spot(self, underlying: str, spot: float) -> None: ...
    def get_spot(self, underlying: str) -> float: ...
    def next_monthly_expiry(self, reference: date | None = None) -> date: ...
    def generate_chain(self, underlying: str, expiry: date | None = None,
                       strikes_each_side: int | None = None,
                       option_type: str = "CE",
                       ) -> dict[Decimal, OptionContract]: ...
        # NOTE: no `reference` param; single-sided; contracts are UNPRICED
    def available_expiries(self, underlying: str, count: int = 3) -> list[date]: ...
    def price_contract(self, contract, option_type) -> float: ...
        # the IV-skew injection point (see §7)

class SyntheticQuoteProvider:       # :198
    spread = 0.5                    # half-spread in ₹ → bid/ask available
    def __init__(self, chain_generator: SyntheticChainGenerator | None = None) -> None: ...
    def register_chain(self, chain: dict[Decimal, OptionContract]) -> None: ...  # REQUIRED
    def register_contract(self, contract: OptionContract) -> None: ...
    def set_spot(self, underlying: str, spot: float) -> None: ...
    def get_quote(self, instrument_token: str) -> dict[str, Any]: ...
    @property
    def source_name(self) -> str: ...   # "synthetic:bs"

class LiveQuoteProvider: ...        # :261  (mStock LTP, TTL cache)
class CachedQuoteProvider: ...      # :320
```

### `src/backtest/instruments/option.py`

```python
class OptionContract(BaseInstrument):   # :32 — METADATA ONLY, carries no price
    # instrument_token, trading_symbol, underlying, exchange, segment,
    # expiry, strike, option_type (OptionType.CE/PE), lot_size, tick_size,
    # contract_type, metadata
    # Synthetic chains set metadata = {"synthetic": True, "vol": float,
    #                                  "spot_at_gen": float}
```

### `src/backtest/strategy/intent.py`

```python
class Direction(Enum):              # :35
    BULLISH = "bullish"; BEARISH = "bearish"; NEUTRAL = "neutral"

@dataclass(frozen=True)
class MarketView:                   # :44
    direction: Direction
    confidence: float = 0.5
    underlying: str = "NIFTY"
    spot_price: Decimal = Decimal("0")
    target_price: Decimal | None = None
    stop_price: Decimal | None = None
    bar_timestamp: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class OptionLeg:                    # :94  (class is OptionLeg, NOT Leg)
    instrument_token: str; trading_symbol: str
    side: str                       # "BUY" | "SELL" (uppercase)
    quantity: int                   # lots
    lot_size: int                   # also here, in addition to the contract
    # property: total_quantity == quantity * lot_size

@dataclass(frozen=True)
class TradeIntent:                  # :132
    view: MarketView                # required
    structure_type: str
    legs: tuple[OptionLeg, ...]     # TUPLE, max 4
    expiry: date                    # required
    estimated_premium: Decimal = Decimal("0")   # populated by builders (T0.1)
    estimated_margin: Decimal = Decimal("0")    # still 0
    strategy_name: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    # property: is_multi_leg, is_debit
    # net_debit was DELETED in T0.1 — it summed lot_size. Do not reintroduce.
```

### `src/backtest/options/{selector,structures}.py`

```python
def create_selector(selector_type: str = "atm", **kwargs) -> StrikeSelector: ...   # :319
    # "atm" | "delta" | "fixed_distance" | "target_price"

class StrikeSelector(Protocol):     # :40
    def pick_strike(self, ...) -> Decimal | None: ...
    def pick_strikes(self, ...) -> list[Decimal]: ...
    # selectors choose STRIKES — they do not choose structures

def create_structure(structure_type: str) -> OptionStructure: ...                  # :330
    # implements ONLY: long_call, long_put, bull_call_spread, bear_put_spread
    # raises ValueError for: straddle, strangle, iron_condor, calendar_spread

class OptionStructure(ABC):         # :43
    def build(self, view: MarketView, strikes: list[Decimal],
              chain: dict[Decimal, OptionContract], expiry: date,
              strategy_name: str = "") -> TradeIntent: ...
    # helpers: _get_contract(strike, option_type, chain), _build_leg(contract, side, quantity=1)
    # T0.1 helper: _estimate_net_premium(priced_legs, view, expiry)
```

### `src/backtest/options/expiry.py`

```python
class ExpiryManager:                # :154
    def __init__(self, broker: OptionPaperBroker,
                 squareoff_minutes_before: int = 30) -> None: ...   # broker REQUIRED
    def detect_expiring(self, as_of: datetime | None = None) -> list[OptionPosition]: ...
    def auto_square_off(self, quote_provider: Any,
                        as_of: datetime | None = None) -> list[SettlementResult]: ...
        # closes via broker.close_structure(..., reason="auto_square_off")   # T0.2
    def settle_expired(self, settlement_provider: SettlementPriceProvider,
                       as_of: datetime | None = None) -> list[SettlementResult]: ...
        # sets pos.status = PositionStatus.EXPIRED and stamps the parent
        # structure exit_reason = "expiry_settlement"                        # T0.2
    def process_expiries(self, quote_provider: Any,
                         settlement_provider: SettlementPriceProvider,
                         as_of: datetime | None = None) -> dict[str, Any]: ...

class StaticSettlementProvider: ...          # :93
class LtpFallbackSettlementProvider: ...     # :112
```

### `src/backtest/strategies/option_directional.py`

```python
class DirectionalOptions(Strategy):     # :28  (name = "directional_options")
    def generate_market_view(self, candles: pd.DataFrame) -> MarketView | None: ...   # :80
        # ONE DataFrame argument. Not (chain, market_data).
```

### `src/backtest/{simulator/engine_loop,engine/metrics,data}.py`

```python
def run_engine_loop(*, source: Any, strategy: Any, portfolio, executor,
                    order_queue, symbols: list[str], start=None, end=None,
                    interval: str = "day", quantity: int = 100,
                    size_fn=None) -> ...: ...                       # engine_loop.py:102
    # calls strategy.generate_signals(candles) only (:181) — EQUITY-ONLY.
    # Do not extend this for options; the options driver is separate.

def compute_metrics(result) -> dict: ...                            # engine/metrics.py:16
    # there is NO MetricsCalculator class

class DataSource(Protocol):                                         # data/base.py:28
    def get_candles(self, symbol: str, start: str, end: str,
                    interval: str = "1day") -> pd.DataFrame: ...

class CsvSource: ...        # data/csv_source.py:14
class FrameSource: ...      # data/frame_source.py:15
class SyntheticSource:      # data/synthetic.py:15
    SUPPORTED_INTERVALS = ("1day",)
    def __init__(self, replay_speed: float = 1.0) -> None: ...
    def get_candles(self, symbol, start, end, interval="1day") -> pd.DataFrame: ...

def load_config(...): ...   # db/config.py:264
```

### Config

`config/` holds 14 YAML files including `execution.yaml`, `slippage.yaml`,
`risk.yaml`. `slippage.yaml` is structured as `active_profile` + `default` +
`profiles` (`backtest`, `simple`, `realistic`, `pessimistic`, `optimistic`).
