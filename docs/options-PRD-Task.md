# PRD: Options Trading for Paper & Live Trading Only

## Executive Summary

Implement options trading support **exclusively for paper trading and live trading**, bypassing historical backtesting. This approach prioritizes real-world validation (paper) and production deployment (live) over synthetic historical analysis, which is data-intensive and difficult to validate for options.

**Key Decision**: Skip options in historical backtest engine. Focus on:
1. **Paper trading**: Build confidence in strategy before risking capital
2. **Live trading**: Execute real orders with real money

**Scope Exclusions**:
- ❌ Historical options backtesting (requires years of option chain data)
- ❌ Forward testing single-strategy paper trade (existing feature stays equity-only)
- ✅ Portfolio Manager paper trading (multi-strategy) - **options enabled**
- ✅ Portfolio Manager live trading - **options enabled**

---

## 1. Goals & Success Metrics

### Primary Goals
1. **Enable options paper trading** in Portfolio Manager (50+ simultaneous strategies)
2. **Enable options live trading** with broker integration (mStock order routing)
3. **Preserve equity workflow** - existing features continue working unchanged
4. **Production-ready risk controls** - margin, Greeks limits, position limits

### Success Metrics
- ✅ Paper trade 10 option strategies simultaneously for 1 week without errors
- ✅ Live order placement: bull call spread (2-leg) executes atomically
- ✅ Portfolio Greeks dashboard shows real-time net delta, vega
- ✅ Margin checks prevent over-leveraged positions
- ✅ Zero regression: all 1,875 equity tests pass

### Non-Goals (V1)
- ❌ Historical options backtesting
- ❌ Options in forward testing (single-strategy)
- ❌ Stock options (American exercise, physical settlement)
- ❌ Exotic options (binary, barrier, Asian)
- ❌ Futures
- ❌ Volatility surface modeling

---

## 2. Architecture Overview

### Current State (Equity Only)
```
Portfolio Manager
    ↓
Strategy → Signal → Order (equity) → Paper Broker / Live Broker → Fill
    ↓
Position (equity) → Portfolio → Metrics
```

### Target State (Equity + Options)
```
Portfolio Manager
    ↓
Strategy → MarketView (directional intent)
    ↓
Expression Policy (NEW) → selects option structure (long call, bull spread, etc.)
    ↓
Contract Selector (NEW) → picks strikes/expiry from live option chain
    ↓
TradeIntent (multi-leg) → Order(s) → Paper Broker / Live Broker
    ↓
Fill(s) → Position (option-aware) → Portfolio (Greeks, margin) → Metrics
```

**Key Insight**: Equity strategies remain unchanged. Options are an **expression layer** on top of directional signals.

---

## 3. Core Abstractions

### 3.1 MarketView (Strategy Output)
Strategies emit directional intent, not specific orders.

```python
@dataclass
class MarketView:
    underlying: str              # "NIFTY", "BANKNIFTY"
    direction: Literal["LONG", "SHORT", "NEUTRAL"]
    strength: Decimal            # 0.0 to 1.0 (conviction level)
    horizon_days: int | None     # Expected holding period
    timestamp: datetime
    metadata: dict               # Strategy-specific context
```

**Example**: SMA crossover generates `MarketView(underlying="NIFTY", direction="LONG", strength=0.8)`

---

### 3.2 TradeIntent (Expression Output)
Multi-leg order intent (replaces single equity order).

```python
@dataclass
class LegIntent:
    instrument_token: str        # "NSE:NIFTY24DEC24500CE"
    side: Literal["BUY", "SELL"]
    quantity: int                # Number of lots
    leg_id: int                  # 1, 2, 3, ... (for spreads)
    ratio: int = 1               # For ratio spreads

@dataclass
class TradeIntent:
    structure_type: str          # "long_call", "bull_call_spread", "iron_condor"
    underlying: str
    legs: list[LegIntent]
    direction: Literal["OPEN", "CLOSE", "ADJUST"]
    max_loss: Decimal | None     # Theoretical max loss
    max_profit: Decimal | None   # Theoretical max profit
    strategy_id: str             # Which strategy generated this
    timestamp: datetime
```

**Example**: Bull call spread = 2 legs (BUY 24500 CE, SELL 25000 CE)

---

### 3.3 OptionContract (Instrument)
```python
@dataclass(frozen=True)
class OptionContract:
    instrument_token: str        # Unique ID from broker
    trading_symbol: str          # "NIFTY24DEC24500CE"
    underlying: str              # "NIFTY"
    exchange: str                # "NSE"
    segment: str                 # "NFO" (derivatives)
    expiry: date
    strike: Decimal
    option_type: Literal["CE", "PE"]
    lot_size: int
    tick_size: Decimal
    contract_type: Literal["european", "american"]
    settlement_type: Literal["cash", "physical"]
```

**Validation**: V1 only accepts European cash-settled index options (NIFTY, BANKNIFTY).

---

## 4. Implementation Phases

### Phase 1: Foundation (2 weeks)
**Goal**: Instrument model, live option chain fetching, contract registry

#### Tasks
- [ ] **P1.1**: Create `instruments/` package
  - `base.py`, `option.py`, `equity.py`, `registry.py`
  - `OptionContract` dataclass with validation (reject American/physical)
  - Estimate: 6 hours

- [ ] **P1.2**: Instrument registry
  - `register()`, `get_by_token()`, `find_contracts(underlying, expiry, option_type)`
  - In-memory dict (no DB persistence for V1)
  - Estimate: 4 hours

- [ ] **P1.3**: mStock option chain API integration
  - Extend `brokers/mstock.py` with `get_option_chain(underlying: str) -> list[OptionContract]`
  - Parse mStock instrument master (NFO segment)
  - Estimate: 8 hours

- [ ] **P1.4**: Live option quotes
  - `get_option_quote(instrument_token: str) -> OptionQuote`
  - Fields: `bid, ask, ltp, volume, oi, iv` (if available)
  - Estimate: 6 hours

- [ ] **P1.5**: Expiry calendar
  - Generate NIFTY/BANKNIFTY monthly/weekly expiries (last Thursday of month)
  - Estimate: 4 hours

- [ ] **P1.6**: Unit tests
  - Contract parsing, registry lookup, validation
  - Estimate: 4 hours

**Deliverable**: Query live NIFTY option chain from mStock, get 50+ strikes with bid/ask/OI

---

### Phase 2: Expression Layer (2 weeks)
**Goal**: Convert market view → option structure → specific contracts

#### Tasks
- [ ] **P2.1**: Create `strategy/intent.py`
  - `MarketView`, `TradeIntent`, `LegIntent` dataclasses
  - Estimate: 3 hours

- [ ] **P2.2**: Extend `BaseStrategy` (optional interface)
  ```python
  class BaseStrategy:
      def generate_signals(self, df) -> Series:
          # Existing equity interface (unchanged)
      
      def generate_market_view(self, df) -> MarketView:
          # NEW: Optional for options strategies
          # Default: convert signal to view
  ```
  - Estimate: 4 hours

- [ ] **P2.3**: Create `options/selector.py`
  - Base protocol: `ContractSelector`
  - Implement `ATMSelector`, `DeltaSelector` (simple approximation), `FixedDistanceSelector`
  - Estimate: 8 hours

- [ ] **P2.4**: Create `options/structures.py`
  - `LongCall`, `LongPut`, `BullCallSpread`, `BearPutSpread`
  - Each implements `build_intent(view, chain) -> TradeIntent`
  - Estimate: 10 hours

- [ ] **P2.5**: Expiry policy selector
  - Filter: `min_days_to_expiry`, `max_days_to_expiry`, `prefer_monthly`
  - Estimate: 4 hours

- [ ] **P2.6**: Configuration schema
  ```yaml
  # config/options.yaml
  expression:
    type: "bull_call_spread"
    long_strike:
      selection: "delta"
      target: 0.35
    short_strike:
      selection: "delta"
      target: 0.15
    expiry:
      min_days: 20
      max_days: 45
  ```
  - Estimate: 3 hours

- [ ] **P2.7**: Unit tests
  - Mock chain, verify correct strikes selected
  - Bull call spread generates 2-leg intent
  - Estimate: 6 hours

**Deliverable**: Given NIFTY @ 24,000, generate bull call spread intent with strikes 24,500/25,000

---

### Phase 3: Paper Trading Execution (3 weeks)
**Goal**: Multi-leg order execution in paper trading (simulated fills)

#### Tasks
- [ ] **P3.1**: Extend `PaperBroker` for options
  ```python
  class PaperBroker:
      def submit_order(self, intent: TradeIntent) -> list[Order]:
          # Create Order per leg
          # Validate: lot size, bid/ask available, margin
      
      def fill_orders(self, timestamp: datetime):
          # Atomic multi-leg fills: all legs or none
  ```
  - Estimate: 10 hours

- [ ] **P3.2**: Premium calculation
  ```python
  # Long call: pay premium
  cash_impact = -(ask_price * quantity * lot_size)
  
  # Short call: receive premium
  cash_impact = +(bid_price * quantity * lot_size)
  ```
  - Estimate: 4 hours

- [ ] **P3.3**: Lot size validation
  - Reject orders where `quantity % lot_size != 0`
  - Estimate: 2 hours

- [ ] **P3.4**: Multi-leg atomicity
  - If leg 1 fills but leg 2 fails (margin/liquidity), rollback leg 1
  - Use database transaction or in-memory rollback
  - Estimate: 8 hours

- [ ] **P3.5**: Position tracking (option-aware)
  ```python
  @dataclass
  class OptionPosition:
      structure_id: str            # Links legs together
      instrument_token: str
      option_type: Literal["CE", "PE"]
      strike: Decimal
      expiry: date
      quantity: int                # Positive (long) or negative (short)
      entry_premium: Decimal
      current_premium: Decimal
      unrealized_pnl: Decimal
  ```
  - Estimate: 8 hours

- [ ] **P3.6**: MTM (mark-to-market)
  - Update `current_premium` from live quotes
  - `unrealized_pnl = (current_premium - entry_premium) * quantity * lot_size`
  - Estimate: 4 hours

- [ ] **P3.7**: Position closing
  - Match all legs by `structure_id`, close atomically
  - Calculate realized P&L
  - Estimate: 6 hours

- [ ] **P3.8**: Integration test
  - Open bull call spread, hold 1 day, close
  - Verify P&L = (exit_premium - entry_premium) - fees
  - Estimate: 6 hours

**Deliverable**: Paper trade bull call spread, verify P&L matches hand calculation

---

### Phase 4: Live Trading Execution (3 weeks)
**Goal**: Real order placement via mStock API

#### Tasks
- [ ] **P4.1**: mStock order API integration
  - `POST /openapi/typea/orders/regular` (place order)
  - `PUT /openapi/typea/orders/{order_id}` (modify)
  - `DELETE /openapi/typea/orders/{order_id}` (cancel)
  - Reference: `docs/archive/mstock-typea-api-reference.md`
  - Estimate: 12 hours

- [ ] **P4.2**: Order payload construction
  ```python
  {
      "exchange": "NFO",
      "trading_symbol": "NIFTY24DEC24500CE",
      "transaction_type": "BUY",
      "order_type": "LIMIT",
      "quantity": 50,
      "price": 150.50,
      "product": "NRML",  # Normal (carry forward) for options
      "validity": "DAY"
  }
  ```
  - Estimate: 6 hours

- [ ] **P4.3**: Multi-leg order orchestration
  - Submit legs sequentially (mStock doesn't support basket orders)
  - If leg 2 fails, auto-cancel leg 1 (risk mitigation)
  - Estimate: 10 hours

- [ ] **P4.4**: Order status polling
  - Poll `/openapi/typea/orders/{order_id}` until `status = "complete"`
  - Handle partial fills, rejections
  - Estimate: 8 hours

- [ ] **P4.5**: Fill reconciliation
  - Match broker fills to internal orders
  - Update positions, cash
  - Estimate: 8 hours

- [ ] **P4.6**: Error handling
  - Insufficient margin → reject order before submission
  - RMS rejection → log, alert, mark order as failed
  - Network timeout → retry with exponential backoff
  - Estimate: 6 hours

- [ ] **P4.7**: Dry-run mode
  - `LIVE_ORDER_ENABLED=false` → log order payloads, don't submit
  - For pre-production validation
  - Estimate: 3 hours

- [ ] **P4.8**: Live test (manual)
  - Place 1-lot NIFTY call, verify order appears in broker app
  - Square off, verify P&L
  - Estimate: 4 hours

**Deliverable**: Place real bull call spread order via mStock, verify execution in broker app

---

### Phase 5: Greeks, Margin & Risk (2 weeks)
**Goal**: Portfolio-level Greeks, margin enforcement, risk limits

#### Tasks
- [ ] **P5.1**: Greeks calculation
  - Implement Black-Scholes: `delta, gamma, theta, vega`
  - OR fetch from mStock (if API provides Greeks)
  - Estimate: 10 hours

- [ ] **P5.2**: Portfolio Greeks aggregation
  ```python
  def aggregate_greeks(positions: list[OptionPosition]) -> dict:
      return {
          "net_delta": sum(p.delta * p.quantity for p in positions),
          "gross_delta": sum(abs(p.delta * p.quantity) for p in positions),
          "vega": sum(p.vega * p.quantity for p in positions),
          "theta": sum(p.theta * p.quantity for p in positions),
      }
  ```
  - Estimate: 4 hours

- [ ] **P5.3**: Margin calculation
  - **Conservative model**: `2x max_loss` for short options
  - Long options: no margin (premium already paid)
  - Spreads: `max_loss` of structure
  - Estimate: 8 hours

- [ ] **P5.4**: Margin reservation
  - Before order: check `portfolio.available_cash() >= required_margin`
  - After fill: `portfolio.used_margin += required_margin`
  - After close: `portfolio.used_margin -= required_margin`
  - Estimate: 4 hours

- [ ] **P5.5**: Extend `RiskSupervisor`
  ```python
  @dataclass
  class OptionRiskLimits:
      max_option_margin_pct: Decimal = Decimal("0.25")  # 25% of portfolio
      max_net_delta: Decimal = Decimal("5000")
      max_vega: Decimal = Decimal("100000")
      max_short_option_exposure: Decimal = Decimal("50000")
      min_days_to_expiry: int = 7  # Close positions before last week
  ```
  - Estimate: 6 hours

- [ ] **P5.6**: Pre-trade risk checks
  - Delta limit, margin limit, vega limit, expiry proximity
  - Reject order if any check fails
  - Estimate: 4 hours

- [ ] **P5.7**: Greeks dashboard (UI)
  - Add card: "Portfolio Greeks" → net delta, vega, theta, gamma
  - Real-time updates via SSE stream
  - Estimate: 6 hours

**Deliverable**: Portfolio with 10 bull call spreads shows correct net delta, margin enforced

---

### Phase 6: Expiry & Settlement (1 week)
**Goal**: Auto-close positions on expiry, cash settlement

#### Tasks
- [ ] **P6.1**: Expiry detection
  - Background job: check for positions expiring today
  - Trigger at 3:00 PM IST (before 3:30 PM settlement)
  - Estimate: 4 hours

- [ ] **P6.2**: Auto-square off
  - Submit market orders to close all legs 30 min before expiry
  - If paper trading: synthetic close at settlement price
  - Estimate: 6 hours

- [ ] **P6.3**: Cash settlement (paper trading)
  - Intrinsic value: `max(0, spot - strike)` for CE, `max(0, strike - spot)` for PE
  - Credit/debit: `(intrinsic - premium_paid) * qty * lot_size`
  - Estimate: 4 hours

- [ ] **P6.4**: Settlement price source
  - Fetch NSE settlement price (if API available)
  - Fallback: use 3:30 PM LTP
  - Estimate: 4 hours

- [ ] **P6.5**: Expiry notification
  - Alert: "3 positions expiring in 1 day"
  - Email/SMS integration (optional)
  - Estimate: 3 hours

- [ ] **P6.6**: Integration test
  - Hold position through expiry, verify auto-close
  - Estimate: 4 hours

**Deliverable**: NIFTY call expires worthless, auto-squared off, loss = premium paid

---

### Phase 7: UI & API (2 weeks)
**Goal**: Frontend support for options display, trade tables, portfolio Greeks

#### Tasks
- [ ] **P7.1**: Extend API responses
  ```json
  {
      "position_id": "uuid",
      "instrument_type": "option",
      "structure_type": "bull_call_spread",
      "underlying": "NIFTY",
      "legs": [
          {"side": "BUY", "strike": 24500, "option_type": "CE", "quantity": 50},
          {"side": "SELL", "strike": 25000, "option_type": "CE", "quantity": 50}
      ],
      "entry_premium": 3500,
      "current_premium": 4200,
      "unrealized_pnl": 700,
      "greeks": {"delta": 0.18, "gamma": 0.003, "theta": -25, "vega": 120}
  }
  ```
  - Estimate: 4 hours

- [ ] **P7.2**: Trade table (multi-leg display)
  - Group legs by `structure_id`
  - Show: "Bull Call Spread: BUY 24500 CE / SELL 25000 CE"
  - Expandable details (strike, expiry, premium per leg)
  - Estimate: 8 hours

- [ ] **P7.3**: Position table (option-aware)
  - Columns: Underlying, Structure, Strikes, Expiry, P&L, Greeks
  - Color-code: green (long), red (short)
  - Estimate: 6 hours

- [ ] **P7.4**: Portfolio summary (Greeks card)
  - Real-time: net delta, vega, theta, gamma
  - Visual: delta exposure bar chart
  - Estimate: 6 hours

- [ ] **P7.5**: Option selection UI (strategy config)
  - Dropdown: instrument type (equity/option)
  - If option: show expression policy form (structure, strikes, expiry)
  - Estimate: 8 hours

- [ ] **P7.6**: Live order confirmation modal
  - Show: "Confirm Live Order: BUY 50 NIFTY 24500 CE @ ₹150"
  - Require explicit confirmation (prevent accidental orders)
  - Estimate: 4 hours

- [ ] **P7.7**: Order status widget
  - Show: pending, filled, rejected orders
  - Color-code: pending (yellow), filled (green), rejected (red)
  - Estimate: 4 hours

**Deliverable**: UI shows option positions, Greeks, multi-leg trades, live order confirmation

---

### Phase 8: Cost Modeling (1 week)
**Goal**: Accurate fee calculation for Indian options

#### Tasks
- [ ] **P8.1**: Extend `simulator/fees.py`
  ```python
  class TradeSegment(Enum):
      INDEX_OPTIONS = "index_options"
      STOCK_OPTIONS = "stock_options"
  
  @dataclass
  class OptionFees:
      brokerage_flat: Decimal = Decimal("20")  # ₹20 per order (typical)
      stt_sell_pct: Decimal = Decimal("0.0625")  # 0.0625% on sell premium
      exchange_fee_pct: Decimal = Decimal("0.05")  # 0.05% of premium
      sebi_turnover_pct: Decimal = Decimal("0.0001")
      stamp_duty_buy_pct: Decimal = Decimal("0.003")  # Buy side only
      gst_pct: Decimal = Decimal("0.18")  # 18% on brokerage + charges
  ```
  - Estimate: 6 hours

- [ ] **P8.2**: Multi-leg fee calculation
  - Calculate per leg, sum for structure
  - Estimate: 3 hours

- [ ] **P8.3**: Fee validation
  - Compare against broker contract note (manual test)
  - Document assumptions in `config/brokers.yaml`
  - Estimate: 4 hours

- [ ] **P8.4**: Unit tests
  - Bull call spread: verify total fees = sum(leg fees)
  - Estimate: 3 hours

**Deliverable**: Option fees match broker charges ±5%

---

### Phase 9: Documentation & Testing (1 week)
**Goal**: User docs, integration tests, production readiness

#### Tasks
- [ ] **P9.1**: Write `docs/OPTIONS-PAPER-LIVE.md`
  - How to paper trade options
  - How to go live
  - Risk controls, margin, Greeks
  - Estimate: 8 hours

- [ ] **P9.2**: Update `docs/PORTFOLIO-CENTER.md`
  - Add options section
  - Example configs
  - Estimate: 4 hours

- [ ] **P9.3**: Update `README.md`
  - Add "Options Trading (Paper & Live)" section
  - Estimate: 2 hours

- [ ] **P9.4**: Integration test suite
  - Paper trade 5 option strategies for 1 day
  - Verify: orders, fills, positions, Greeks, P&L
  - Estimate: 12 hours

- [ ] **P9.5**: Live dry-run (manual)
  - `LIVE_ORDER_ENABLED=false` → verify payloads logged correctly
  - Estimate: 4 hours

- [ ] **P9.6**: Regression tests
  - Run full test suite (equity tests must pass)
  - Estimate: 2 hours

**Deliverable**: Complete documentation, passing integration tests

---

## 5. Database Schema

### New Tables

**`instruments`** (option contracts)
```sql
CREATE TABLE instruments (
    instrument_token VARCHAR(50) PRIMARY KEY,
    trading_symbol VARCHAR(50) NOT NULL,
    underlying VARCHAR(20),
    exchange VARCHAR(10) NOT NULL,
    segment VARCHAR(10) NOT NULL,
    instrument_type VARCHAR(20) NOT NULL,  -- 'equity', 'option', 'future'
    expiry DATE,
    strike NUMERIC(20, 2),
    option_type VARCHAR(2),  -- 'CE', 'PE'
    lot_size INT NOT NULL DEFAULT 1,
    tick_size NUMERIC(20, 8) NOT NULL DEFAULT 0.01,
    contract_type VARCHAR(20),  -- 'european', 'american'
    settlement_type VARCHAR(20),  -- 'cash', 'physical'
    active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_underlying_expiry (underlying, expiry)
);
```

**`trade_structures`** (links multi-leg trades)
```sql
CREATE TABLE trade_structures (
    structure_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    strategy_id VARCHAR(100) NOT NULL,
    structure_type VARCHAR(50) NOT NULL,  -- 'bull_call_spread', 'iron_condor'
    underlying VARCHAR(20) NOT NULL,
    opened_at TIMESTAMP NOT NULL,
    closed_at TIMESTAMP,
    status VARCHAR(20) NOT NULL,  -- 'open', 'closed', 'expired'
    entry_underlying_price NUMERIC(20, 4),
    exit_underlying_price NUMERIC(20, 4),
    premium_paid NUMERIC(20, 4),
    premium_received NUMERIC(20, 4),
    max_profit NUMERIC(20, 4),
    max_loss NUMERIC(20, 4),
    margin_required NUMERIC(20, 4),
    realized_pnl NUMERIC(20, 4),
    fees_total NUMERIC(20, 4),
    INDEX idx_strategy_status (strategy_id, status)
);
```

### Extended Tables

**`orders`** (add option fields)
```sql
ALTER TABLE orders 
ADD COLUMN structure_id UUID REFERENCES trade_structures(structure_id),
ADD COLUMN leg_id INT,
ADD COLUMN instrument_token VARCHAR(50),
ADD COLUMN option_type VARCHAR(2),
ADD COLUMN strike NUMERIC(20, 2),
ADD COLUMN expiry DATE;
```

**`positions`** (add Greeks)
```sql
ALTER TABLE positions
ADD COLUMN structure_id UUID REFERENCES trade_structures(structure_id),
ADD COLUMN instrument_token VARCHAR(50),
ADD COLUMN option_type VARCHAR(2),
ADD COLUMN strike NUMERIC(20, 2),
ADD COLUMN expiry DATE,
ADD COLUMN delta NUMERIC(10, 6),
ADD COLUMN gamma NUMERIC(10, 6),
ADD COLUMN theta NUMERIC(10, 6),
ADD COLUMN vega NUMERIC(10, 6);
```

---

## 6. Configuration

**`config/options.yaml`** (new file)
```yaml
options:
  # Contract selection
  selection:
    min_days_to_expiry: 20
    max_days_to_expiry: 45
    prefer_monthly_expiry: true
    min_open_interest: 100
    min_volume: 10
    max_bid_ask_spread_pct: 0.02
    
  # Execution
  execution:
    fill_model: "realistic"  # optimistic, realistic, conservative
    multi_leg_mode: "atomic"  # all legs or none
    
  # Greeks
  greeks:
    model: "black_scholes"
    risk_free_rate: 0.065  # 6.5%
    
  # Margin
  margin:
    model: "conservative"  # 2x max loss
    multiplier: 2.0
    
  # Settlement
  settlement:
    auto_close_hours_before_expiry: 0.5  # 30 min
    price_source: "ltp"  # ltp, settlement_price
    
  # Risk limits
  risk:
    max_option_margin_pct: 0.25
    max_portfolio_delta: 5000
    max_portfolio_vega: 100000
    min_days_to_expiry_warning: 7
```

**Extend `config/forward_testing.yaml`**
```yaml
strategy:
  name: "sma_crossover"
  
instrument:
  type: "option"  # NEW: equity | option
  underlying: "NIFTY"
  
expression:  # NEW: only for options
  type: "bull_call_spread"
  long_strike:
    selection: "delta"
    target: 0.35
  short_strike:
    selection: "delta"
    target: 0.15
```

---

## 7. Risk Management

### Portfolio-Level Limits
```python
@dataclass
class OptionRiskLimits:
    # Exposure
    max_option_margin_pct: Decimal = Decimal("0.25")
    max_net_delta: Decimal = Decimal("5000")
    max_gross_delta: Decimal = Decimal("10000")
    max_vega: Decimal = Decimal("100000")
    
    # Concentration
    max_single_underlying_pct: Decimal = Decimal("0.30")
    max_short_option_exposure: Decimal = Decimal("50000")
    
    # Time
    min_days_to_expiry: int = 7
    warn_days_to_expiry: int = 14
    
    # Structural
    reject_naked_shorts: bool = True
```

### Pre-Trade Checks
1. ✅ Margin available
2. ✅ Delta limit not breached
3. ✅ Expiry >7 days
4. ✅ No naked shorts
5. ✅ Bid-ask spread <2%

---

## 8. Testing Strategy

### Unit Tests (Per Phase)
- **Phase 1**: Contract parsing, registry lookup
- **Phase 2**: Strike selection, structure generation
- **Phase 3**: Multi-leg atomicity, premium calculation
- **Phase 4**: Order payload construction, status polling
- **Phase 5**: Greeks calculation, margin calculation

### Integration Tests
- **Paper trade workflow**: Open → hold → close bull call spread
- **Live dry-run**: Verify order payloads (no submission)
- **Greeks aggregation**: 10 positions, verify portfolio Greeks

### Regression Tests
- All 1,875 equity tests must pass

---

## 9. Deployment Checklist

### Pre-Production
- [ ] All unit tests pass
- [ ] Integration tests pass (paper trading)
- [ ] Live dry-run verified (payloads correct)
- [ ] Documentation complete
- [ ] Database migrations tested on staging
- [ ] Risk limits configured

### Production
- [ ] Deploy to production server
- [ ] Monitor logs for 48 hours
- [ ] Test with small position (1 lot)
- [ ] Gradually increase to production size

---

## 10. Timeline & Effort

| Phase | Duration | Effort (hours) |
|-------|----------|----------------|
| P1: Foundation | 2 weeks | 32 |
| P2: Expression | 2 weeks | 38 |
| P3: Paper Execution | 3 weeks | 48 |
| P4: Live Execution | 3 weeks | 57 |
| P5: Greeks & Risk | 2 weeks | 36 |
| P6: Expiry | 1 week | 25 |
| P7: UI & API | 2 weeks | 40 |
| P8: Costs | 1 week | 16 |
| P9: Docs & Testing | 1 week | 32 |
| **Total** | **17 weeks** | **324 hours** |

**At 40 hours/week**: 8 weeks full-time  
**At 20 hours/week**: 16 weeks part-time

---

## 11. Success Criteria

### Phase Completion Gates
- ✅ **P1**: Query live NIFTY option chain, 50+ strikes returned
- ✅ **P2**: Generate bull call spread intent for NIFTY
- ✅ **P3**: Paper trade spread, P&L reconciles ±₹10
- ✅ **P4**: Place real order via mStock, verify in broker app
- ✅ **P5**: Portfolio Greeks dashboard shows correct values
- ✅ **P6**: Position auto-closes on expiry
- ✅ **P7**: UI displays multi-leg trades correctly
- ✅ **P8**: Fees match broker charges ±5%
- ✅ **P9**: Docs complete, all tests pass

### Launch Criteria
- [ ] Paper trade 10 option strategies for 1 week (no errors)
- [ ] Live order placement successful (1 lot test)
- [ ] Risk limits enforced (manual breach test)
- [ ] Zero equity regression (all tests pass)

---

# Detailed Task List

## Phase 1: Foundation (Weeks 1-2)

### Week 1
- [ ] **T1.1**: Create `instruments/` package structure (2h)
- [ ] **T1.2**: Implement `OptionContract` dataclass with validation (4h)
- [ ] **T1.3**: Implement `InstrumentRegistry` (4h)
- [ ] **T1.4**: Extend `brokers/mstock.py` with `get_option_chain()` (8h)
- [ ] **T1.5**: Parse mStock instrument master (NFO segment) (4h)
- [ ] **T1.6**: Unit tests: contract parsing, validation (4h)

### Week 2
- [ ] **T1.7**: Implement `get_option_quote()` API (6h)
- [ ] **T1.8**: Expiry calendar generator (4h)
- [ ] **T1.9**: Integration test: query live chain, verify strikes (4h)
- [ ] **T1.10**: Error handling: API failures, missing data (4h)

---

## Phase 2: Expression Layer (Weeks 3-4)

### Week 3
- [ ] **T2.1**: Create `strategy/intent.py` (3h)
- [ ] **T2.2**: Extend `BaseStrategy` with `generate_market_view()` (4h)
- [ ] **T2.3**: Create `options/selector.py` base protocol (2h)
- [ ] **T2.4**: Implement `ATMSelector` (3h)
- [ ] **T2.5**: Implement `DeltaSelector` (simple approximation) (5h)
- [ ] **T2.6**: Implement `FixedDistanceSelector` (3h)

### Week 4
- [ ] **T2.7**: Create `options/structures.py` base class (3h)
- [ ] **T2.8**: Implement `LongCall` (3h)
- [ ] **T2.9**: Implement `LongPut` (3h)
- [ ] **T2.10**: Implement `BullCallSpread` (4h)
- [ ] **T2.11**: Implement `BearPutSpread` (4h)
- [ ] **T2.12**: Expiry policy selector (4h)
- [ ] **T2.13**: Configuration schema (`options.yaml`) (3h)
- [ ] **T2.14**: Unit tests: selection, structure generation (6h)

---

## Phase 3: Paper Trading (Weeks 5-7)

### Week 5
- [ ] **T3.1**: Extend `PaperBroker.submit_order()` for multi-leg (6h)
- [ ] **T3.2**: Premium calculation logic (4h)
- [ ] **T3.3**: Lot size validation (2h)
- [ ] **T3.4**: Multi-leg atomicity (rollback on partial fill) (8h)

### Week 6
- [ ] **T3.5**: Create `OptionPosition` dataclass (4h)
- [ ] **T3.6**: Extend position tracking for options (4h)
- [ ] **T3.7**: MTM calculation (update from live quotes) (4h)
- [ ] **T3.8**: Position closing (match by structure_id) (6h)
- [ ] **T3.9**: Database schema: `trade_structures` table (3h)
- [ ] **T3.10**: Database schema: extend `orders`, `positions` (3h)

### Week 7
- [ ] **T3.11**: Integration test: open/hold/close spread (6h)
- [ ] **T3.12**: P&L reconciliation test (4h)
- [ ] **T3.13**: Error handling: insufficient margin, rejected orders (4h)
- [ ] **T3.14**: Logging: multi-leg fill events (2h)

---

## Phase 4: Live Trading (Weeks 8-10)

### Week 8
- [ ] **T4.1**: mStock order API: `POST /orders/regular` (6h)
- [ ] **T4.2**: Order payload construction (6h)
- [ ] **T4.3**: Multi-leg orchestration (submit legs sequentially) (6h)
- [ ] **T4.4**: Auto-cancel leg 1 if leg 2 fails (4h)

### Week 9
- [ ] **T4.5**: Order status polling (`GET /orders/{id}`) (4h)
- [ ] **T4.6**: Fill reconciliation (match broker fills to orders) (8h)
- [ ] **T4.7**: Handle partial fills, rejections (6h)
- [ ] **T4.8**: Retry logic with exponential backoff (4h)

### Week 10
- [ ] **T4.9**: Dry-run mode (`LIVE_ORDER_ENABLED=false`) (3h)
- [ ] **T4.10**: Live test: place 1-lot call (manual) (4h)
- [ ] **T4.11**: Error logging: RMS rejection, timeout (3h)
- [ ] **T4.12**: Integration test: live order workflow (dry-run) (6h)

---

## Phase 5: Greeks & Risk (Weeks 11-12)

### Week 11
- [ ] **T5.1**: Implement Black-Scholes pricing (6h)
- [ ] **T5.2**: Calculate delta, gamma, theta, vega (4h)
- [ ] **T5.3**: Portfolio Greeks aggregation (4h)
- [ ] **T5.4**: Unit tests: Greeks vs reference (py_vollib) (4h)
- [ ] **T5.5**: Margin calculation (conservative model) (4h)

### Week 12
- [ ] **T5.6**: Margin reservation logic (4h)
- [ ] **T5.7**: Extend `RiskSupervisor` with option limits (6h)
- [ ] **T5.8**: Pre-trade risk checks (4h)
- [ ] **T5.9**: Greeks dashboard UI (6h)
- [ ] **T5.10**: Integration test: portfolio with 10 spreads (4h)

---

## Phase 6: Expiry (Week 13)

- [ ] **T6.1**: Expiry detection (background job) (4h)
- [ ] **T6.2**: Auto-square off (market orders 30 min before expiry) (6h)
- [ ] **T6.3**: Cash settlement (intrinsic value calculation) (4h)
- [ ] **T6.4**: Settlement price source (NSE API or LTP fallback) (4h)
- [ ] **T6.5**: Expiry notification (alert) (3h)
- [ ] **T6.6**: Integration test: hold through expiry (4h)

---

## Phase 7: UI & API (Weeks 14-15)

### Week 14
- [ ] **T7.1**: Extend API responses (option fields) (4h)
- [ ] **T7.2**: Trade table: multi-leg display (8h)
- [ ] **T7.3**: Position table: option-aware columns (6h)
- [ ] **T7.4**: Portfolio Greeks card (6h)

### Week 15
- [ ] **T7.5**: Option selection UI (strategy config form) (8h)
- [ ] **T7.6**: Live order confirmation modal (4h)
- [ ] **T7.7**: Order status widget (4h)
- [ ] **T7.8**: End-to-end UI test (manual) (4h)

---

## Phase 8: Costs (Week 16)

- [ ] **T8.1**: Extend `simulator/fees.py` for options (6h)
- [ ] **T8.2**: Multi-leg fee calculation (3h)
- [ ] **T8.3**: Fee validation vs broker contract note (4h)
- [ ] **T8.4**: Unit tests: fee calculation (3h)

---

## Phase 9: Docs & Testing (Week 17)

- [ ] **T9.1**: Write `docs/OPTIONS-PAPER-LIVE.md` (8h)
- [ ] **T9.2**: Update `docs/PORTFOLIO-CENTER.md` (4h)
- [ ] **T9.3**: Update `README.md` (2h)
- [ ] **T9.4**: Integration test suite (12h)
- [ ] **T9.5**: Live dry-run (manual) (4h)
- [ ] **T9.6**: Regression tests (2h)

---

**Total: 324 hours across 17 weeks**

This PRD and task list focuses exclusively on paper and live trading, bypassing the data-intensive historical backtesting challenge while delivering immediate practical value.