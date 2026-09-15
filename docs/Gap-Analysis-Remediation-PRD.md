# Gap Analysis & Remediation PRD

## Executive Summary

The junior engineer identified **4 critical gaps** between "complete PRD" and "working product":

1. ✅ **Expression layer exists but has no driver** - No API endpoint to open trades
2. ✅ **Dashboard shows fictional numbers** - Hardcoded ₹100 quotes, not real market data
3. ✅ **No strategy produces option intents** - All 5 built-in strategies emit equity signals only
4. ✅ **Accounting gaps** - Fees calculated but not deducted, positions not persisted

**Impact**: The options layer is architecturally sound but **unusable** - you can't open a trade via UI, and if you did via Python, it would fill at fake prices.

---

## Gap 1: No Trade Execution Drivertrynow I waasfgsg

### Current State
```python
# Expression layer works:
view = MarketView(underlying="NIFTY", direction="LONG", ...)
intent = BullCallSpread().build_intent(view, chain)
orders = paper_broker.submit_order(intent)

# ✅ This works in tests
# ❌ No UI or API endpoint triggers this flow
```

### Missing Pieces
- **API endpoint**: `POST /api/options/trade` (open structure)
- **UI form**: "Open Structure" modal on `/options` dashboard
- **Validation**: Check margin, Greeks limits before submission

### Solution

#### Task G1.1: Create Trade Execution API (4 hours)
```python
# src/backtest/api/options.py (NEW FILE)

from flask import Blueprint, request, jsonify
from backtest.options.structures import LongCall, BullCallSpread
from backtest.forward.paper_runner import get_paper_broker

options_bp = Blueprint('options', __name__, url_prefix='/api/options')

@options_bp.route('/trade', methods=['POST'])
def open_trade():
    """
    POST /api/options/trade
    Body: {
        "underlying": "NIFTY",
        "structure_type": "bull_call_spread",
        "direction": "LONG",
        "strength": 0.8,
        "config": {
            "long_strike_delta": 0.35,
            "short_strike_delta": 0.15,
            "expiry_days_min": 20,
            "expiry_days_max": 45,
            "quantity": 1  # Number of structures (not lots)
        }
    }
    """
    data = request.json
    
    # 1. Create market view
    view = MarketView(
        underlying=data['underlying'],
        direction=data['direction'],
        strength=Decimal(str(data.get('strength', 1.0))),
        timestamp=datetime.now(),
    )
    
    # 2. Fetch live option chain
    chain = broker.get_option_chain(data['underlying'])
    
    # 3. Build intent
    structure = STRUCTURES[data['structure_type']]  # Registry
    intent = structure.build_intent(view, chain, data['config'])
    
    # 4. Pre-trade risk checks
    risk_check = risk_supervisor.validate_trade(intent)
    if not risk_check.passed:
        return jsonify({"error": risk_check.reason}), 400
    
    # 5. Submit to broker
    orders = paper_broker.submit_order(intent)
    
    # 6. Return order IDs
    return jsonify({
        "structure_id": intent.structure_id,
        "orders": [{"order_id": o.order_id, "status": o.status} for o in orders]
    }), 201
```

**Acceptance Criteria**:
- ✅ POST with valid payload returns 201 + order IDs
- ✅ Invalid margin triggers 400 error
- ✅ Orders appear in `/api/options/positions`

---

#### Task G1.2: UI "Open Trade" Form (6 hours)

**Location**: `src/backtest/web/templates/options.html`

```html
<!-- Add button to dashboard -->
<button id="openTradeBtn" class="btn btn-primary">Open Structure</button>

<!-- Modal form -->
<div id="openTradeModal" class="modal">
    <div class="modal-content">
        <h3>Open Option Structure</h3>
        <form id="openTradeForm">
            <label>Underlying</label>
            <select name="underlying">
                <option value="NIFTY">NIFTY</option>
                <option value="BANKNIFTY">BANKNIFTY</option>
            </select>
            
            <label>Structure Type</label>
            <select name="structure_type">
                <option value="long_call">Long Call</option>
                <option value="long_put">Long Put</option>
                <option value="bull_call_spread">Bull Call Spread</option>
                <option value="bear_put_spread">Bear Put Spread</option>
            </select>
            
            <label>Direction</label>
            <select name="direction">
                <option value="LONG">LONG (Bullish)</option>
                <option value="SHORT">SHORT (Bearish)</option>
            </select>
            
            <label>Quantity (structures)</label>
            <input type="number" name="quantity" value="1" min="1" max="10">
            
            <!-- Dynamic fields based on structure_type -->
            <div id="structureConfig"></div>
            
            <button type="submit">Submit Order</button>
        </form>
    </div>
</div>

<script>
document.getElementById('openTradeForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const formData = new FormData(e.target);
    const payload = {
        underlying: formData.get('underlying'),
        structure_type: formData.get('structure_type'),
        direction: formData.get('direction'),
        config: {
            quantity: parseInt(formData.get('quantity')),
            // ... structure-specific params
        }
    };
    
    const response = await fetch('/api/options/trade', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload)
    });
    
    if (response.ok) {
        alert('Trade opened successfully');
        location.reload();  // Refresh positions table
    } else {
        const error = await response.json();
        alert(`Error: ${error.error}`);
    }
});
</script>
```

**Acceptance Criteria**:
- ✅ Form validates inputs client-side (quantity > 0, required fields)
- ✅ Submission triggers API call
- ✅ Success: modal closes, positions table refreshes
- ✅ Error: shows error message in modal

---

## Gap 2: Fictional Quote Provider

### Current State
```python
# src/backtest/web/app.py (line 156-157)
paper_broker = PaperBroker(
    quote_provider=FakeQuoteProvider(default_price=100.0),  # ❌ Hardcoded
    ...
)
```

**Impact**: 
- All options show ₹100 premium regardless of strike/expiry
- MTM P&L doesn't change
- Greeks are meaningless (computed from fake price)

### Solution

#### Task G2.1: Implement `LiveQuoteProvider` (8 hours)

```python
# src/backtest/simulator/quote_providers.py (NEW FILE)

from decimal import Decimal
from typing import Protocol
from backtest.brokers.base import Broker

class QuoteProvider(Protocol):
    def get_quote(self, instrument_token: str) -> Decimal:
        """Return current price for instrument"""
        ...

class FakeQuoteProvider:
    """Hardcoded price (testing only)"""
    def __init__(self, default_price: Decimal = Decimal("100.0")):
        self.default_price = default_price
    
    def get_quote(self, instrument_token: str) -> Decimal:
        return self.default_price

class LiveQuoteProvider:
    """Fetch real-time quotes from broker"""
    def __init__(self, broker: Broker, cache_ttl_seconds: int = 5):
        self.broker = broker
        self.cache = {}
        self.cache_ttl = cache_ttl_seconds
    
    def get_quote(self, instrument_token: str) -> Decimal:
        # Check cache
        if instrument_token in self.cache:
            quote, timestamp = self.cache[instrument_token]
            if (datetime.now() - timestamp).total_seconds() < self.cache_ttl:
                return quote
        
        # Fetch from broker
        quote_data = self.broker.get_option_quote(instrument_token)
        price = Decimal(str(quote_data['ltp']))  # Last traded price
        
        # Cache
        self.cache[instrument_token] = (price, datetime.now())
        
        return price

class BidAskQuoteProvider:
    """Use bid for sells, ask for buys (realistic execution)"""
    def __init__(self, broker: Broker):
        self.broker = broker
    
    def get_quote(self, instrument_token: str, side: str = "BUY") -> Decimal:
        quote_data = self.broker.get_option_quote(instrument_token)
        if side == "BUY":
            return Decimal(str(quote_data['ask']))
        else:
            return Decimal(str(quote_data['bid']))
```

**Acceptance Criteria**:
- ✅ `LiveQuoteProvider` fetches real LTP from mStock
- ✅ Cache prevents API spam (5-second TTL)
- ✅ Returns `Decimal`, not `float`

---

#### Task G2.2: Wire Live Quotes into Web App (3 hours)

```python
# src/backtest/web/app.py (modify existing)

def create_app(source_mode='synthetic'):
    # ... existing code ...
    
    # Initialize broker (if live mode)
    broker = None
    if source_mode == 'mstock':
        from backtest.brokers.mstock import MStockBroker
        broker = MStockBroker()
        # Login handled separately via /api/broker/login
    
    # Choose quote provider
    if broker and broker.is_authenticated():
        from backtest.simulator.quote_providers import LiveQuoteProvider
        quote_provider = LiveQuoteProvider(broker, cache_ttl_seconds=5)
        logger.info("Using LiveQuoteProvider (real quotes)")
    else:
        from backtest.simulator.quote_providers import FakeQuoteProvider
        quote_provider = FakeQuoteProvider(default_price=Decimal("100.0"))
        logger.warning("Using FakeQuoteProvider (synthetic data)")
    
    # Initialize paper broker
    paper_broker = PaperBroker(
        quote_provider=quote_provider,
        ...
    )
```

**Acceptance Criteria**:
- ✅ `--source mstock` + authenticated → uses `LiveQuoteProvider`
- ✅ `--source synthetic` → uses `FakeQuoteProvider` (backward compatible)
- ✅ Dashboard shows real NIFTY option premiums (verified against NSE site)

---

#### Task G2.3: Add Quote Source Indicator to UI (2 hours)

```html
<!-- src/backtest/web/templates/options.html -->
<div class="alert alert-info">
    <strong>Quote Source:</strong> 
    <span id="quoteSource">{{ quote_source }}</span>
    {% if quote_source == "FakeQuoteProvider" %}
        <span class="badge badge-warning">SIMULATED DATA</span>
    {% else %}
        <span class="badge badge-success">LIVE MARKET DATA</span>
    {% endif %}
</div>
```

**Acceptance Criteria**:
- ✅ Dashboard clearly shows if using fake vs real quotes
- ✅ Warning badge for simulated data

---

## Gap 3: No Options-Aware Strategy

### Current State
All 5 built-in strategies return equity signals (`+1, 0, -1`). None override `generate_market_view()`.

### Solution

#### Task G3.1: Create `DirectionalOptionsStrategy` (6 hours)

```python
# src/backtest/strategies/directional_options.py (NEW FILE)

from backtest.strategy.base import Strategy, register
from backtest.strategy.intent import MarketView
from decimal import Decimal

@register
class DirectionalOptionsStrategy(Strategy):
    """
    Simple directional options strategy.
    Uses price movement threshold to trigger option trades.
    """
    name = "directional_options"
    description = "Directional options based on price movement"
    
    params = {
        "threshold": {
            "type": "number",
            "default": 100,
            "min": 10,
            "max": 500,
            "description": "Price move threshold to trigger trade"
        },
        "lookback": {
            "type": "int",
            "default": 5,
            "min": 1,
            "max": 20,
            "description": "Bars to look back for price change"
        }
    }
    
    def generate_signals(self, data: pd.DataFrame) -> pd.Series:
        """
        Legacy equity interface (backward compatible).
        Returns +1 (bullish), -1 (bearish), 0 (neutral).
        """
        threshold = self.params.get('threshold', 100)
        lookback = self.params.get('lookback', 5)
        
        price_change = data['close'] - data['close'].shift(lookback)
        
        signals = pd.Series(0, index=data.index)
        signals[price_change > threshold] = 1   # Bullish
        signals[price_change < -threshold] = -1  # Bearish
        
        return signals
    
    def generate_market_view(self, data: pd.DataFrame) -> MarketView | None:
        """
        NEW: Options interface.
        Returns MarketView if signal present, else None.
        """
        signals = self.generate_signals(data)
        latest_signal = signals.iloc[-1]
        
        if latest_signal == 0:
            return None  # No view
        
        # Calculate conviction based on magnitude of price change
        threshold = self.params.get('threshold', 100)
        lookback = self.params.get('lookback', 5)
        price_change = abs(data['close'].iloc[-1] - data['close'].iloc[-lookback-1])
        strength = min(price_change / (threshold * 2), 1.0)  # Cap at 1.0
        
        return MarketView(
            underlying=self.symbol,  # Passed from runner
            direction="LONG" if latest_signal == 1 else "SHORT",
            strength=Decimal(str(strength)),
            horizon_days=self.params.get('lookback', 5) * 2,  # 2x lookback
            timestamp=data.index[-1],
            metadata={"price_change": float(price_change)}
        )
```

**Acceptance Criteria**:
- ✅ Strategy appears in `/api/strategies` list
- ✅ `generate_signals()` works (backward compatible with equity)
- ✅ `generate_market_view()` returns `MarketView` when signal present
- ✅ Strength scales with price change magnitude

---

#### Task G3.2: Wire Strategy into Portfolio Manager (4 hours)

```python
# src/backtest/forward/portfolio_manager.py (modify existing)

def _process_strategy_tick(self, runner: StrategyRunner, bar: pd.Series):
    """Process one bar for one strategy"""
    
    # Append bar to strategy's data buffer
    runner.append_bar(bar)
    
    # Generate view (NEW)
    view = runner.strategy.generate_market_view(runner.data)
    
    if view is None:
        return  # No trade signal
    
    # Get instrument config
    instrument_config = runner.config.get('instrument', {})
    
    if instrument_config.get('type') == 'option':
        # Option trade flow
        expression = instrument_config['expression']
        chain = self.broker.get_option_chain(view.underlying)
        
        structure_class = STRUCTURES[expression['type']]
        intent = structure_class.build_intent(view, chain, expression)
        
        # Submit via order ledger
        self.order_ledger.submit(intent, runner.strategy_id)
    else:
        # Equity trade flow (existing)
        signal = runner.strategy.generate_signals(runner.data).iloc[-1]
        # ... existing equity logic ...
```

**Acceptance Criteria**:
- ✅ Strategy with `instrument.type = "option"` triggers expression layer
- ✅ Strategy with `instrument.type = "equity"` uses existing flow
- ✅ Both flows coexist without breaking each other

---

## Gap 4: Accounting & Persistence Gaps

### Gap 4.1: Fees Calculated But Not Deducted

**Current State**: `calculate_fees()` returns ₹27.63, but only commission (₹20) is subtracted from cash.

#### Task G4.1: Apply Full Fee Stack (3 hours)

```python
# src/backtest/simulator/execution.py (modify _execute_fill)

def _execute_fill(self, order: Order, fill_price: Decimal) -> Fill:
    # ... existing fill creation ...
    
    # Calculate fees
    fees = calculate_fees(
        segment=order.segment,
        side=order.side,
        price=fill_price,
        quantity=order.quantity,
        lot_size=order.lot_size,
    )
    
    # Apply to cash (NEW: use full fee, not just commission)
    if order.side == "BUY":
        cash_impact = -(fill_price * order.quantity * order.lot_size + fees.total)
    else:
        cash_impact = +(fill_price * order.quantity * order.lot_size - fees.total)
    
    self.portfolio.cash += cash_impact
    
    # Record fee breakdown in fill
    fill.fees = fees
    fill.cash_impact = cash_impact
    
    return fill
```

**Acceptance Criteria**:
- ✅ Full fee stack (STT, exchange, SEBI, stamp, GST) deducted from cash
- ✅ P&L matches: `entry_premium - exit_premium - total_fees`
- ✅ Reconciliation test: hand-calculate vs system P&L within ₹1

---

### Gap 4.2: Positions Not Persisted

**Current State**: Refresh server → lose all positions.

#### Task G4.2: Database Persistence (6 hours)

**Already designed** in original PRD (Phase 3, T3.9-T3.10):
- Tables: `trade_structures`, extend `orders`/`positions`
- Schema ready (see PRD Section 5)

**Implementation**:
```python
# src/backtest/db/models.py (add models)

class TradeStructure(Base):
    __tablename__ = 'trade_structures'
    structure_id = Column(String, primary_key=True)
    strategy_id = Column(String, nullable=False)
    structure_type = Column(String, nullable=False)
    underlying = Column(String, nullable=False)
    opened_at = Column(DateTime, nullable=False)
    closed_at = Column(DateTime)
    status = Column(String, nullable=False)  # 'open', 'closed'
    # ... (full schema from PRD)

# src/backtest/simulator/execution.py

def _execute_fill(self, order: Order, fill_price: Decimal) -> Fill:
    # ... existing logic ...
    
    # Persist to database
    if order.structure_id:
        db_structure = TradeStructure.query.get(order.structure_id)
        if not db_structure:
            db_structure = TradeStructure(
                structure_id=order.structure_id,
                strategy_id=order.strategy_id,
                structure_type=order.structure_type,
                # ... populate fields ...
            )
            db.session.add(db_structure)
    
    db.session.commit()
```

**Acceptance Criteria**:
- ✅ Open position → row in `trade_structures` (status='open')
- ✅ Close position → update `closed_at`, `status='closed'`
- ✅ Server restart → positions reload from DB

---

### Gap 4.3: Live Dry-Run Needs Real Credentials

**Current State**: T9.5 requires mStock login, but many developers won't have it.

#### Task G4.3: Mock Broker for Dry-Run (4 hours)

```python
# src/backtest/brokers/mock.py (NEW FILE)

class MockBroker(Broker):
    """
    Mock broker for testing.
    Returns synthetic option chains, accepts orders but doesn't submit.
    """
    
    def __init__(self):
        self._authenticated = True
    
    def is_authenticated(self) -> bool:
        return True
    
    def get_option_chain(self, underlying: str) -> list[OptionContract]:
        # Generate synthetic chain
        spot = Decimal("24000") if underlying == "NIFTY" else Decimal("50000")
        expiry = date.today() + timedelta(days=30)
        
        contracts = []
        for strike in range(int(spot) - 500, int(spot) + 500, 100):
            for option_type in ["CE", "PE"]:
                contracts.append(OptionContract(
                    instrument_token=f"{underlying}{strike}{option_type}",
                    trading_symbol=f"{underlying}24DEC{strike}{option_type}",
                    underlying=underlying,
                    exchange="NSE",
                    segment="NFO",
                    expiry=expiry,
                    strike=Decimal(str(strike)),
                    option_type=option_type,
                    lot_size=50,
                    tick_size=Decimal("0.05"),
                    contract_type="european",
                    settlement_type="cash",
                ))
        
        return contracts
    
    def get_option_quote(self, instrument_token: str) -> dict:
        # Return synthetic quote
        return {
            "ltp": 150.0,
            "bid": 149.5,
            "ask": 150.5,
            "volume": 1000,
            "oi": 5000,
        }
    
    def place_order(self, payload: dict) -> dict:
        # Log payload, return fake order ID
        logger.info(f"[DRY-RUN] Order: {payload}")
        return {"order_id": f"MOCK{random.randint(10000, 99999)}"}
```

**Usage**:
```bash
PYTHONPATH=src python -m backtest.web.app --source mock_broker
```

**Acceptance Criteria**:
- ✅ `--source mock_broker` → no authentication required
- ✅ Option chain returns synthetic NIFTY/BANKNIFTY contracts
- ✅ Orders logged but not submitted to real broker

---

## Revised Timeline

| Gap | Tasks | Effort | Priority |
|-----|-------|--------|----------|
| **G1: Trade Driver** | API endpoint + UI form | 10h | 🔴 Critical |
| **G2: Real Quotes** | LiveQuoteProvider + wiring | 13h | 🔴 Critical |
| **G3: Strategy** | DirectionalOptions + wiring | 10h | 🟡 High |
| **G4.1: Fees** | Apply full fee stack | 3h | 🟡 High |
| **G4.2: Persistence** | DB models + save/load | 6h | 🟢 Medium |
| **G4.3: Mock Broker** | No-credential testing | 4h | 🟢 Medium |
| **Total** | | **46 hours** | |

**At 20 hours/week**: 2.5 weeks  
**At 40 hours/week**: 1 week

---

## Recommended Implementation Order

### Week 1 (Critical Path)
1. **G2: Real Quotes** (13h) - Foundation for realistic P&L
2. **G1: Trade Driver** (10h) - Makes UI functional

**Deliverable**: Dashboard with real quotes, ability to open trades via UI

### Week 2 (Complete Product)
3. **G3: Strategy** (10h) - Automated option trading
4. **G4.1: Fees** (3h) - Accurate P&L
5. **G4.2: Persistence** (6h) - Production-ready
6. **G4.3: Mock Broker** (4h) - Developer experience

**Deliverable**: Fully functional options trading platform (paper + live)

---

## Updated Success Criteria

### Immediate (Post-Week 1)
- [ ] Dashboard shows real NIFTY option premiums (verified against NSE)
- [ ] "Open Structure" button → submits trade → appears in positions table
- [ ] MTM P&L updates as market moves
- [ ] Close position → realized P&L matches hand calculation ±₹10

### Complete (Post-Week 2)
- [ ] `DirectionalOptionsStrategy` generates bull call spread when NIFTY rises ₹100
- [ ] Full fee stack deducted (P&L includes STT, stamp duty, etc.)
- [ ] Server restart → positions reload from DB
- [ ] Mock broker mode works without mStock credentials

---

## Risk Mitigation

### If mStock API Rate Limits
**Mitigation**: Implement `CachedQuoteProvider` wrapper:
```python
class CachedQuoteProvider:
    def __init__(self, inner: QuoteProvider, ttl: int = 60):
        self.inner = inner
        self.cache = {}
        self.ttl = ttl
```

### If Database Schema Migration Breaks Prod
**Mitigation**: 
1. Test on staging first
2. Add rollback script:
   ```sql
   DROP TABLE IF EXISTS trade_structures;
   ALTER TABLE orders DROP COLUMN IF EXISTS structure_id;
   ```
3. Backup before migration

### If UI Form Validation Misses Edge Case
**Mitigation**: Server-side validation is authoritative:
```python
@options_bp.route('/trade', methods=['POST'])
def open_trade():
    # Re-validate all inputs server-side
    if not (1 <= data['config']['quantity'] <= 10):
        return jsonify({"error": "Quantity must be 1-10"}), 400
```

---

## Final Checklist (Before Calling It "Done")

- [ ] ✅ Open trade via UI → appears in positions table
- [ ] ✅ Positions show real premiums (not ₹100)
- [ ] ✅ Close trade → P&L matches hand calculation
- [ ] ✅ `DirectionalOptionsStrategy` trades automatically
- [ ] ✅ Full fees deducted from cash
- [ ] ✅ Server restart → positions persist
- [ ] ✅ Mock broker works without credentials
- [ ] ✅ All existing 1,875 equity tests pass (no regression)

---

This remediation plan closes the gap between "architecturally complete" and "actually usable." The junior engineer's feedback was spot-on — these 4 gaps would make the feature feel broken despite all the underlying infrastructure being solid.