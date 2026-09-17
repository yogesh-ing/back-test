# Options Trading — Paper & Live

How to trade NIFTY/BANKNIFTY options through this platform: first on the
paper book, then (when you choose to) through the live mStock order path.

> V1 scope: **European cash-settled index options** on NIFTY and BANKNIFTY,
> NSE/NFO segment, up to 4 legs per structure. Stock options, American
> exercise and physical settlement are rejected by `OptionContract.validate()`.

---

## 1. The pipeline

Every option trade — paper or live — flows through the same layers:

```
Strategy                Expression layer                  Execution
────────────────        ──────────────────────────        ─────────────────────
generate_market_view()  StrikeSelector → strikes          OptionPaperBroker
  → MarketView          ExpiryPolicy  → expiry              (paper, atomic)
  (bullish/bearish  →   OptionStructure.build()             or
   + spot + confidence)  → TradeIntent (legs)              LiveOptionTrader
                        create_selector /                  (mStock, rollback)
                        create_structure /
                        create_expiry_policy factories
```

Key modules (`src/backtest/`):

| Module | Role |
|---|---|
| `instruments/option.py` | `OptionContract` — validated V1 contract model |
| `instruments/registry.py` | `InstrumentRegistry` — in-memory chain cache |
| `instruments/expiry_calendar.py` | `ExpiryCalendar` — last-Thursday monthly expiries |
| `strategy/intent.py` | `MarketView`, `TradeIntent`, `OptionLeg` |
| `options/selector.py` | Strike selection: ATM, Delta, FixedDistance, TargetPrice |
| `options/expiry_policy.py` | Expiry selection: nearest, weekly, fixed-days, minimum-days |
| `options/structures.py` | LongCall, LongPut, BullCallSpread, BearPutSpread |
| `options/paper_trading.py` | `OptionPaperBroker` — atomic multi-leg paper execution |
| `options/live_trading.py` | `LiveOptionTrader` — mStock multi-leg orchestration |
| `options/greeks.py` | Black-Scholes price + Greeks + IV solver |
| `options/portfolio_greeks.py` | Portfolio-level Greeks aggregation |
| `options/margin.py` | Margin + `PreTradeRiskCheck` |
| `options/expiry.py` | `ExpiryManager` — square-off + cash settlement |
| `simulator/fees.py` | Full statutory fee stack (see §6) |
| `web/options_api.py` | `/options` dashboard + JSON API |

---

## 2. Paper trading options

### 2.1 The one-minute version

```python
from datetime import date, timedelta
from decimal import Decimal

from backtest.instruments.option import OptionContract
from backtest.options.paper_trading import OptionPaperBroker, FakeQuoteProvider
from backtest.options.selector import ATMSelector
from backtest.options.structures import LongCall
from backtest.strategy.intent import Direction, MarketView

spot = Decimal("24800")
strikes = [Decimal(s) for s in range(24400, 25201, 100)]
expiry = date.today() + timedelta(days=7)

# 1. A chain: strike -> contract
chain = {
    s: OptionContract(
        instrument_token=f"NIFTY{s}CE", trading_symbol=f"NIFTY{s}CE",
        underlying="NIFTY", expiry=expiry, strike=s,
        option_type="CE", lot_size=75,
    )
    for s in strikes
}

# 2. Select a strike and build the trade intent
strike = ATMSelector().pick_strike(spot, strikes, Direction.BULLISH)
view = MarketView(direction=Direction.BULLISH, underlying="NIFTY", spot_price=spot)
intent = LongCall().build(view, [strike], chain, expiry, strategy_name="my_strategy")

# 3. Execute atomically on the paper book
broker = OptionPaperBroker(capital=1_000_000, commission_per_lot=20.0)
quotes = FakeQuoteProvider(default_price=120.0)
positions = broker.execute_structure(intent, quotes)
```

### 2.2 Multi-leg structures are atomic

`execute_structure()` fills **all legs or none**: if any leg fails margin or
lot-size validation, nothing is created. Each structure gets a
`structure_id`; `close_structure(structure_id, quotes)` closes every leg at
the same moment and books one realized P&L.

Supported structures: `long_call`, `long_put`, `bull_call_spread`,
`bear_put_spread` — build via the `create_structure(name)` factory.

### 2.3 Selectors and expiry policies

```python
from backtest.options.selector import create_selector
from backtest.options.expiry_policy import create_expiry_policy

selector = create_selector("delta", delta_target=0.35)   # ~1.5% OTM
selector = create_selector("fixed_distance", distance_pct=2.0)
selector = create_selector("atm")                         # default

policy = create_expiry_policy("weekly")                   # next Thursday
policy = create_expiry_policy("minimum_days", min_days=7)
```

`ATMSelector` picks the strike closest to spot; `DeltaSelector` approximates
delta from distance (V1 heuristic — real delta needs Black-Scholes, which
§5 covers); `TargetPriceSelector` uses the view's `target_price`.

### 2.4 MTM, closing, and the P&L identity

```python
broker.update_mtm(quotes)               # unrealized P&L from live LTPs
pnl = broker.close_structure(structure_id, quotes)
```

The book keeps one identity you can always audit:

```
total_equity = capital + realized_pnl + unrealized_pnl − commissions_paid
```

`realized_pnl` includes **closed and expired** positions. `available_cash`
moves with actual cash flows (premium debit/credit, commissions), so for an
open long option the two differ by design: the premium is in the cash, not
the P&L.

### 2.5 Querying the book

```python
broker.get_open_positions()               # [OptionPosition]
broker.get_open_structures()              # [StructurePosition]
broker.get_structure(structure_id)        # legs, aggregate P&L, status
broker.total_equity / total_realized_pnl / total_margin_used
```

---

## 3. Going live (mStock)

`LiveOptionTrader` submits the same `TradeIntent` through the real order API.

```python
from backtest.brokers.mstock import MStockBroker
from backtest.options.live_trading import LiveOptionTrader, RetryConfig

broker = MStockBroker(...)          # authenticated session (login + TOTP)
trader = LiveOptionTrader(
    broker,
    dry_run=True,                   # default — log payloads, place nothing
    retry_config=RetryConfig(max_retries=3, base_delay_seconds=1.0),
)

result = trader.execute_structure(intent)   # -> StructureResult
```

Behaviour you can rely on:

- **Legs submit sequentially.** If a leg fails after retries, every
  previously filled leg is **cancelled (rollback)** — a structure never ends
  up half-open by accident.
- **Dry-run is the default — fail-closed.** `dry_run=True` is the *code
  default* (architect review 2026-09-17 §3.2): it logs the exact payloads
  (`POST /openapi/typea/orders/regular`) and places nothing. Arming real
  orders requires **all three gates** at construction, or the constructor
  raises `ValueError` before touching the broker:

  | Gate | Switch | Meaning |
  |---|---|---|
  | 1 | `dry_run=False` | explicit opt-out of the safe default |
  | 2 | `confirm_live=True` | deliberate, per-instance confirmation (never inherited from a copy-pasted config) |
  | 3 | `ALLOW_LIVE_ORDERS=1` (environment) | ops-level kill-switch — a desk can disable all live placement centrally, without a deploy |

  ```python
  trader = LiveOptionTrader(broker, dry_run=False, confirm_live=True)  # + env ALLOW_LIVE_ORDERS=1
  ```

  Arming is logged loudly (`LIVE MODE ARMED`) and every live submission is
  logged. `trader.confirm_live` reads `True` only when actually armed.
- **Status polling.** Fill confirmation polls order status with
  `poll_interval_seconds` / `poll_timeout_seconds`; partial fills and
  rejections surface on `LegResult` and `StructureResult`.
- **Results are queryable.** `trader.get_result(structure_id)` /
  `get_all_results()`.

> **Live trading is gated on your credentials and your risk appetite, not on
> code.** The order path exists and is tested against mocks; the PRD's
> T9.5 live dry-run (run with `dry_run=True` against a real session and read
> the payload logs) is a manual step by design.

---

## 4. Risk controls and margin

### 4.1 Pre-trade checks

`PreTradeRiskCheck` runs four gates before any order (paper broker applies
margin; live trader is your wiring point):

```python
from backtest.options.margin import PreTradeRiskCheck

risk = PreTradeRiskCheck(
    max_margin=2_000_000,          # total margin budget (₹)
    max_positions=20,              # open position count
    max_loss_per_trade_pct=2.0,    # % of capital
    max_single_position_pct=10.0,  # notional cap per trade
    capital=1_000_000,
)
result = risk.check(margin_required=50_000, current_margin_used=0.0,
                    current_positions=3, trade_notional=200_000)
if not result.allowed:
    print(result.reason)
```

### 4.2 Margin model

`MarginCalculator` (V1, conservative):

| Position | Margin |
|---|---|
| Long option | Full premium × units |
| Short option | SPAN + exposure, `SPAN ≈ max(15% × underlying, 10% × strike)` per unit, less premium credit |
| Spread | `max(long margin, short margin) − spread credit` |

Spread margin is the point of spreads: defined risk ⇒ defined margin.
Real SPAN uses delta-weighted risk arrays — treat these numbers as floors,
not broker-matching quotes.

### 4.3 Options config file

`config/options.yaml` (or `options.yaml` in CWD) drives defaults via
`load_options_config()`:

```yaml
enabled: true
underlying: NIFTY
selector: {type: atm}
expiry: {policy: weekly}
structures:
  allowed: [long_call, long_put, bull_call_spread, bear_put_spread]
  default_spread_width: 200
risk:
  max_positions: 10
  max_margin_per_trade: 500000
  max_total_margin: 2000000
  max_loss_per_trade_pct: 2.0
strategy_overrides:
  donchian_breakout:
    selector: {type: delta, params: {delta_target: 0.35}}
```

---

## 5. Greeks

`BlackScholes` prices European contracts and emits delta/gamma/theta/vega/rho;
`PortfolioGreeksCalculator` aggregates across the book (long adds, short
subtracts) with per-underlying breakdown:

```python
from backtest.options.portfolio_greeks import PortfolioGreeksCalculator

greeks = PortfolioGreeksCalculator(default_volatility=0.2).calculate(
    broker.get_open_positions(),
    spot_prices={"NIFTY": 24800.0},   # omit -> strike used as ATM proxy
)
greeks.to_dict()   # total_delta, total_gamma, total_theta, total_vega, ...
greeks.positions   # per-leg detail
```

Two honest notes:

- **Without a spot price the calculator falls back to the strike** (ATM
  approximation). It deliberately does **not** use the option's own premium
  as the underlying price — that produced nonsense deltas once and is
  guarded now.
- **Very long expiries make BSM degenerate** (delta → 1 for everything).
  The integration suite anchors expiries ~3 weeks out; if you compute Greeks
  on synthetic far-future chains, expect deep-ITM behaviour.

An IV solver (`BlackScholes.implied_volatility`) is available for
quote-implied vol.

---

## 6. Fees (the honest part)

Option costs are **not** just ₹20 brokerage. The full NFO stack for one
NIFTY lot (75 units at a ₹120.50 premium), mStock flat plan, FY 2024-25:

| Component | Rate | Amount |
|---|---|---|
| Brokerage | ₹20 **per order** (per leg, not per lot) | 20.00 |
| Exchange txn | 0.03503% of premium, both sides | 3.17 |
| SEBI turnover | ₹10/crore | 0.01 |
| IPFT | ₹10/crore | 0.01 |
| Stamp duty | 0.003%, **buy side only** | 0.27 |
| STT | 0.1% of premium, **sell side only** | — (buy) / 9.04 (sell) |
| GST | 18% on brokerage + exchange + SEBI + IPFT (never on STT/stamp) | 4.17 |
| **Total (buy leg)** | | **27.63** |

```python
from backtest.simulator import CommissionCalculator, TradeSegment

calc = CommissionCalculator.for_broker("mstock")
fees = calc.calculate(quantity=75, fill_price="120.50", side="buy",
                      segment=TradeSegment.OPTIONS)
fees.describe()          # contract-note style itemisation

struct = calc.calculate_structure([          # multi-leg
    {"side": "BUY",  "quantity": 75, "price": "120.50"},
    {"side": "SELL", "quantity": 75, "price": "95.00"},
])
struct.get("brokerage")  # 40.00 — two legs, two orders
```

Per-leg detail lands under `leg_0_*`, `leg_1_*` keys (annotations — excluded
from totals so nothing double-counts).

**Reconciling against a real contract note:**

```python
from backtest.simulator.fees import ContractNote

mismatches = calc.validate_against_contract_note(
    trade_value="9037.50", quantity=75, side="buy",
    segment=TradeSegment.OPTIONS,
    expected={"brokerage": "20.00", "exchange_transaction": "3.17",
              "stamp_duty": "0.27", "gst": "4.17", "total": "27.63"},
    tolerance="0.05",
    document=ContractNote(
        document_id="CN-2026-09-001", broker="mstock",
        note_date=date(2026, 9, 10), trade_value=Decimal("9037.50"),
        file_path="notes/cn.pdf",
    ),
)
assert mismatches == []   # rates reproduce the note
```

Rates are configurable in `config/brokers.yaml` (see the `mstock` entry —
it documents every option rate). **They change; verify against a current
note before trusting cost-sensitive results.**

Current wiring note: the paper broker *records* the statutory stack when a
`fee_calculator` is attached, but only `commission_per_lot` is cash-bearing.
Cost-bearing statutory fees in P&L are the next step.

---

## 7. Expiry handling

`ExpiryManager` runs the end-of-life pipeline; on the dashboard,
`POST /api/options/expiry/process` triggers it:

1. **Auto-square-off** — open positions inside the window (default 30 min
   before 15:30 IST on expiry day) close at LTP, grouped by structure.
2. **Cash settlement** — positions past expiry settle at intrinsic value:
   long ITM receives `|spot − strike| × units`, short ITM pays it, OTM
   expires worthless.
3. **Alerts** — every action emits an `ExpiryAlert`
   (`AUTO_SQUARED_OFF`, `EXPIRED_ITM`, `EXPIRED_OTM`, `SETTLED`, `ERROR`).

```python
from backtest.options.expiry import ExpiryManager, StaticSettlementProvider

manager = ExpiryManager(broker, squareoff_minutes_before=30)
report = manager.process_expiries(
    quote_provider=quotes,
    settlement_provider=StaticSettlementProvider({"NIFTY": 24950.0}),
)
# {"squared_off_count": n, "settled_count": m, "total_pnl": ..., "alerts": [...]}
```

`LtpFallbackSettlementProvider` uses the underlying's LTP when an official
settlement price is not wired.

---

## 8. Dashboard

`/options` in the web app shows the live paper book (5-second polling):

- Summary cards — equity, cash, realized P&L, margin used, open counts
- Portfolio Greeks grid — net delta/gamma/theta/vega with red/green colouring
- Positions and structures tables — Close button per structure
- Expiry alerts panel

JSON API:

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/api/options/summary` | Everything the dashboard polls |
| GET | `/api/options/positions?status=open\|all` | Position list |
| GET | `/api/options/greeks` | Greeks detail |
| POST | `/api/options/structures/<id>/close` | Close a structure at LTP |
| POST | `/api/options/expiry/process` | Run the expiry pipeline |

V1 dashboard binds to a single process-wide `OptionPaperBroker`
(`capital=1,000,000`) — one paper book per process.

---

## 9. What's real vs simulated (read before trusting numbers)

| Real | Simulated / V1-limited |
|---|---|
| Fee stack rates (validated vs contract note) | Paper fills at LTP ± slippage, no order book |
| mStock option chain + quote fetch (`get_option_chain`) | No partial-fill simulation on paper legs |
| Black-Scholes Greeks + IV solver | Margin is a SPAN *approximation* (floors) |
| Atomic multi-leg semantics (both paper and live paths) | Statutory fees recorded but not yet cash-bearing on paper |
| Rollback on failed live legs | One paper book per process, in-memory only |
| DeltaSelector uses a distance heuristic, not true delta | Restart wipes broker/manager state |

## 10. Tests

```bash
PYTHONPATH=src pytest tests/test_instruments.py \
    tests/test_mstock_options.py tests/test_options_expression.py \
    tests/test_options_paper_trading.py tests/test_options_live_trading.py \
    tests/test_options_greeks.py tests/test_options_expiry.py \
    tests/test_options_fees.py tests/test_options_integration.py \
    tests/test_options_web.py -q
```

`tests/test_options_integration.py` is the end-to-end suite: five option
strategies through the full pipeline for a simulated day (orders → fills →
positions → MTM → Greeks → close → settlement → reconciliation), plus the
contract-note fee anchor (₹27.63) asserted on every leg priced through it.
