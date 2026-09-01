# Step-by-Step Forward Testing Implementation Plan

## Overview
This plan breaks down the forward testing simulator into 20 actionable steps, each with a detailed prompt for LLM code generation.

---

## **PHASE 1: DATABASE DESIGN & SETUP**

### **Step 1: Database Schema Design**

**Prompt for LLM:**
```
Create a PostgreSQL/SQLite database schema for a forward testing trading simulator with the following tables:

1. PORTFOLIOS table:
   - portfolio_id (PK)
   - name
   - initial_capital
   - current_cash
   - created_at
   - status (active/paused/stopped)

2. POSITIONS table:
   - position_id (PK)
   - portfolio_id (FK)
   - symbol
   - quantity
   - average_entry_price
   - current_price
   - unrealized_pnl
   - realized_pnl
   - opened_at
   - closed_at
   - status (open/closed)

3. ORDERS table:
   - order_id (PK)
   - portfolio_id (FK)
   - symbol
   - order_type (market/limit/stop)
   - side (buy/sell)
   - quantity
   - limit_price
   - stop_price
   - status (pending/filled/partial/cancelled/rejected)
   - submitted_at
   - filled_at
   - time_in_force

4. FILLS table:
   - fill_id (PK)
   - order_id (FK)
   - position_id (FK)
   - quantity
   - fill_price
   - commission
   - slippage
   - filled_at
   - exchange_fees

5. TRADES table:
   - trade_id (PK)
   - portfolio_id (FK)
   - symbol
   - entry_order_id (FK)
   - exit_order_id (FK)
   - quantity
   - entry_price
   - exit_price
   - entry_time
   - exit_time
   - gross_pnl
   - net_pnl
   - commission_total
   - slippage_total
   - holding_period_minutes
   - return_percentage

6. EQUITY_CURVE table:
   - equity_id (PK)
   - portfolio_id (FK)
   - timestamp
   - total_equity
   - cash
   - position_value
   - daily_pnl
   - cumulative_pnl

7. MARKET_DATA_CACHE table:
   - data_id (PK)
   - symbol
   - timestamp
   - open
   - high
   - low
   - close
   - volume
   - bid
   - ask
   - timeframe

8. PERFORMANCE_METRICS table:
   - metric_id (PK)
   - portfolio_id (FK)
   - calculation_date
   - total_trades
   - winning_trades
   - losing_trades
   - win_rate
   - avg_win
   - avg_loss
   - profit_factor
   - sharpe_ratio
   - max_drawdown
   - max_drawdown_percentage
   - total_return

9. STRATEGY_SIGNALS table:
   - signal_id (PK)
   - portfolio_id (FK)
   - symbol
   - signal_type (entry/exit)
   - direction (long/short)
   - strength
   - generated_at
   - indicators_snapshot (JSON)
   - executed (boolean)

10. SYSTEM_LOGS table:
    - log_id (PK)
    - portfolio_id (FK)
    - timestamp
    - log_level (info/warning/error)
    - component
    - message
    - stack_trace

Provide:
1. Complete SQL CREATE statements
2. Appropriate indexes for performance
3. Foreign key constraints
4. SQLAlchemy ORM models (Python)
5. Alembic migration script
```

---

### **Step 2: Database Connection Manager**

**Prompt for LLM:**
```
Create a robust database connection manager for the forward testing system with the following requirements:

1. DatabaseManager class with:
   - Connection pooling (min 5, max 20 connections)
   - Auto-reconnection on failure
   - Context manager support
   - Transaction management
   - Query logging option

2. Features:
   - Support both PostgreSQL and SQLite
   - Environment-based configuration
   - Connection health checks
   - Graceful shutdown
   - Error handling with retries (max 3 attempts)

3. Methods needed:
   - connect()
   - disconnect()
   - execute_query(sql, params)
   - execute_many(sql, params_list)
   - fetch_one(sql, params)
   - fetch_all(sql, params)
   - begin_transaction()
   - commit()
   - rollback()

4. Include:
   - Configuration file (database.yaml)
   - Environment variable support (.env)
   - Unit tests for connection scenarios
   - Documentation with usage examples

Use: SQLAlchemy for ORM, psycopg2 for PostgreSQL, sqlite3 for SQLite
```

---

## **PHASE 2: CORE DATA MODELS**

### **Step 3: Portfolio Model**

**Prompt for LLM:**
```
Create a comprehensive Portfolio model class for forward testing with:

1. Properties:
   - portfolio_id
   - name
   - initial_capital
   - current_cash
   - positions (dictionary)
   - pending_orders (list)
   - filled_orders (list)
   - equity_history (list)
   - created_at
   - status

2. Methods:
   - calculate_total_equity()
   - calculate_position_value()
   - calculate_buying_power()
   - calculate_margin_used()
   - get_position(symbol)
   - add_position(position)
   - update_position(symbol, price)
   - close_position(symbol)
   - can_open_position(symbol, quantity, price)
   - get_current_exposure()
   - to_dict() for serialization
   - from_dict() for deserialization
   - save_to_db()
   - load_from_db(portfolio_id)

3. Validations:
   - Sufficient cash for trades
   - Position size limits
   - Maximum open positions
   - Prevent short selling if not allowed

4. Include:
   - Comprehensive logging
   - Exception handling
   - Unit tests covering all scenarios
   - Type hints
   - Docstrings

Use dataclasses or Pydantic for validation
```

---

### **Step 4: Position Model**

**Prompt for LLM:**
```
Create a Position model class that tracks individual stock positions:

1. Properties:
   - position_id
   - portfolio_id
   - symbol
   - quantity
   - average_entry_price
   - current_price
   - entry_date
   - last_updated
   - position_type (long/short)

2. Calculated Properties:
   - market_value
   - cost_basis
   - unrealized_pnl
   - unrealized_pnl_percentage
   - realized_pnl (from partial closes)
   - total_pnl

3. Methods:
   - update_price(new_price)
   - add_shares(quantity, price, commission)
   - reduce_shares(quantity, price, commission)
   - close_position(price, commission)
   - calculate_average_price()
   - get_pnl_at_price(price)
   - is_profitable()
   - to_dict()
   - save_to_db()

4. Include:
   - Support for partial position closes
   - FIFO/LIFO accounting methods
   - Commission tracking
   - Split/dividend adjustment capability
   - Validation for negative quantities
   - Unit tests
```

---

### **Step 5: Order Model**

**Prompt for LLM:**
```
Create an Order model with full order lifecycle management:

1. Order Types Support:
   - Market orders
   - Limit orders
   - Stop loss orders
   - Stop limit orders
   - Trailing stop orders

2. Properties:
   - order_id (UUID)
   - portfolio_id
   - symbol
   - side (BUY/SELL)
   - order_type
   - quantity
   - filled_quantity
   - remaining_quantity
   - limit_price
   - stop_price
   - trailing_amount
   - time_in_force (DAY/GTC/IOC/FOK)
   - status (PENDING/PARTIAL/FILLED/CANCELLED/REJECTED)
   - submitted_at
   - filled_at
   - reason_for_rejection

3. Methods:
   - submit()
   - cancel()
   - update_status(new_status)
   - add_fill(fill)
   - is_fillable(current_price)
   - calculate_fill_price(market_data)
   - validate()
   - to_dict()
   - save_to_db()

4. State Machine:
   - Valid status transitions
   - Event callbacks (on_submit, on_fill, on_cancel)
   - Timestamp tracking for each state

5. Include:
   - Validation logic for each order type
   - Error handling
   - Type hints with Enums for status/types
   - Comprehensive unit tests
```

---

### **Step 6: Fill Model**

**Prompt for LLM:**
```
Create a Fill (execution) model that records order fills:

1. Properties:
   - fill_id (UUID)
   - order_id
   - position_id
   - symbol
   - side (BUY/SELL)
   - quantity
   - fill_price
   - commission
   - slippage_bps
   - exchange_fee
   - regulatory_fee
   - filled_at
   - liquidity_flag (maker/taker)

2. Methods:
   - calculate_total_cost()
   - calculate_net_price() (including all fees)
   - calculate_slippage_amount()
   - impact_on_position()
   - to_dict()
   - save_to_db()

3. Features:
   - Support partial fills
   - Commission calculation based on configurable model
   - Slippage tracking
   - Link to both order and position
   - Immutable after creation

4. Include:
   - Multiple commission models (per-share, percentage, tiered)
   - Validation that fill doesn't exceed order quantity
   - Unit tests for various scenarios
   - Integration with Position updates
```

---

## **PHASE 3: ORDER EXECUTION SIMULATION**

### **Step 7: Slippage Model**

**Prompt for LLM:**
```
Create a realistic slippage simulation engine with multiple models:

1. Slippage Models:
   a) Fixed slippage (basis points)
   b) Spread-based (bid-ask spread percentage)
   c) Volume-based (market impact formula)
   d) Volatility-based (ATR percentage)
   e) Hybrid model combining factors

2. SlippageCalculator class:
   - calculate_slippage(order, market_data, model_type)
   - estimate_market_impact(order_size, avg_volume)
   - get_effective_spread(bid, ask, time_of_day)
   - calculate_volatility_adjustment(atr, order_size)

3. Factors to consider:
   - Order size relative to average volume
   - Time of day (market open/close higher slippage)
   - Volatility (higher vol = more slippage)
   - Market vs Limit orders
   - Symbol liquidity tier

4. Configuration:
   - YAML/JSON config for each model's parameters
   - Symbol-specific slippage profiles
   - Time-based multipliers (first/last 30 min)

5. Include:
   - Realistic default parameters based on research
   - Backtesting mode (no slippage for comparison)
   - Logging of all slippage calculations
   - Statistical analysis of applied slippage
   - Unit tests with various market conditions
```

---

### **Step 8: Commission Calculator**

**Prompt for LLM:**
```
Create a flexible commission calculation system supporting multiple broker models:

1. Commission Models:
   a) Fixed per trade
   b) Per-share pricing
   c) Percentage of trade value
   d) Tiered based on volume
   e) Zero commission with payment for order flow

2. CommissionCalculator class:
   - calculate_commission(order, fill_price)
   - calculate_regulatory_fees(trade_value, side)
   - calculate_exchange_fees(quantity)
   - get_total_fees()

3. Supported Fee Types:
   - Base commission
   - SEC fees (US stocks, sells only)
   - FINRA TAF fees
   - Exchange fees (ECN remove/add liquidity)
   - Minimum commission per order

4. Broker Presets:
   - Interactive Brokers (IBKR) model
   - TD Ameritrade model
   - Robinhood model (zero commission)
   - Generic discount broker
   - Custom configurable model

5. Include:
   - Configuration file for each broker
   - Method to switch between models
   - Monthly volume tracking for tiered pricing
   - Detailed breakdown in logs
   - Unit tests for each broker model
   - Currency conversion support
```

---

### **Step 9: Order Execution Simulator**

**Prompt for LLM:**
```
Create a realistic order execution simulator that processes orders:

1. OrderExecutor class with:
   - process_market_order(order, market_data)
   - process_limit_order(order, market_data)
   - process_stop_order(order, market_data)
   - simulate_latency(min_ms, max_ms)
   - check_order_fillable(order, market_data)

2. Execution Features:
   - Realistic fill latency (50-500ms configurable)
   - Partial fills for large orders
   - Price improvement possibility
   - Rejection scenarios (insufficient liquidity, outside market hours)
   - Queue position simulation

3. Fill Logic:
   - Market orders: immediate fill at ask (buy) / bid (sell) + slippage
   - Limit orders: fill when price touches limit
   - Stop orders: convert to market when triggered
   - Respect market hours and trading halts

4. Integration:
   - Use SlippageCalculator for price impact
   - Use CommissionCalculator for fees
   - Create Fill objects
   - Update Order status
   - Emit events for monitoring

5. Include:
   - Configurable realism levels (optimistic/realistic/pessimistic)
   - Logging of all execution decisions
   - Rejection reasons with codes
   - Support for multiple simultaneous orders
   - Unit tests covering all order types and edge cases
   - Integration tests with Portfolio
```

---

## **PHASE 4: LIVE DATA INTEGRATION**

### **Step 10: Market Data Handler**

**Prompt for LLM:**
```
Create a market data handler that normalizes live data from broker APIs:

1. MarketDataHandler class:
   - connect_to_feed(broker_api)
   - subscribe_symbols(symbol_list)
   - unsubscribe_symbols(symbol_list)
   - get_current_quote(symbol)
   - get_current_bar(symbol, timeframe)
   - on_tick_received(callback)
   - on_bar_closed(callback)

2. Data Normalization:
   - Convert broker-specific format to standard format:
     {
       'symbol': str,
       'timestamp': datetime,
       'bid': float,
       'ask': float,
       'last': float,
       'volume': int,
       'open': float,
       'high': float,
       'low': float,
       'close': float
     }

3. Bar Aggregation:
   - Build bars from ticks (1min, 5min, 15min, 1hr, 1day)
   - Align bars to standard boundaries
   - Handle gaps and missing data
   - Late data handling

4. Features:
   - Multi-symbol support
   - Reconnection on disconnect
   - Data validation and sanity checks
   - Cache recent data in memory
   - Store to MARKET_DATA_CACHE table
   - Publish to subscribers (observer pattern)

5. Include:
   - Abstract base class for different broker APIs
   - Concrete implementation for at least one broker (e.g., Alpaca)
   - Mock data feed for testing
   - Buffer management (prevent memory leaks)
   - Comprehensive error handling
   - Unit tests with mock data
```

---

### **Step 11: Data Quality Validator**

**Prompt for LLM:**
```
Create a data quality validation system to ensure clean market data:

1. DataValidator class:
   - validate_tick(tick_data)
   - validate_bar(bar_data)
   - check_for_spikes(price, symbol, threshold=3_std_dev)
   - check_for_gaps(timestamp, last_timestamp, max_gap_seconds)
   - validate_ohlc_relationship(open, high, low, close)
   - check_volume_anomaly(volume, avg_volume, threshold=5x)

2. Validation Rules:
   - Price within reasonable range (not zero, not negative)
   - OHLC consistency (high >= open/close/low, low <= all)
   - Volume non-negative
   - Timestamp chronological
   - Bid <= Ask
   - Last price between bid and ask (usually)

3. Handling Bad Data:
   - Log validation failures
   - Option to reject or interpolate
   - Alert on repeated failures
   - Configurable strictness levels

4. Statistical Checks:
   - Z-score for price movements
   - Rolling standard deviation
   - Comparison with other data sources (if available)

5. Include:
   - Configurable validation rules (YAML)
   - Detailed error messages
   - Statistics on data quality
   - Unit tests with corrupted data
   - Integration with MarketDataHandler
```

---

### **Step 12: Time Synchronization Manager**

**Prompt for LLM:**
```
Create a time management system for accurate timestamp handling:

1. TimeManager class:
   - get_current_time() (market time)
   - is_market_open(symbol_exchange)
   - get_next_market_open()
   - get_next_market_close()
   - align_to_timeframe(timestamp, timeframe)
   - get_trading_days_between(start, end)

2. Market Hours Support:
   - US market hours (9:30-16:00 ET)
   - Pre-market (4:00-9:30 ET)
   - After-hours (16:00-20:00 ET)
   - Handle holidays (NYSE calendar)
   - Multiple exchange support

3. Features:
   - Timezone handling (UTC, ET, local)
   - DST (Daylight Saving Time) awareness
   - Sync with NTP server for accuracy
   - Latency measurement and tracking

4. Bar Alignment:
   - Align timestamps to bar boundaries
   - Handle late-arriving data
   - Determine bar close events

5. Include:
   - Holiday calendar (pandas_market_calendars)
   - Configuration for different exchanges
   - Mock time for testing (controllable clock)
   - Unit tests across DST transitions
   - Logging of time-related events
```

---

## **PHASE 5: STRATEGY INTEGRATION**

### **Step 13: Strategy Adapter**

**Prompt for LLM:**
```
Create an adapter to run backtested strategies in forward testing environment:

1. StrategyAdapter class:
   - __init__(backtest_strategy, portfolio, order_executor)
   - on_market_data(market_data)
   - generate_signals()
   - execute_signals(signals)
   - on_order_filled(fill)
   - on_bar_close(bar)
   (Superseded by F-01, 2026-08-31: __init__ no longer takes order_executor;
    execute_signals → create_orders (no fill); on_order_filled removed.)

2. Signal Generation:
   - Call strategy's analysis methods
   - Ensure no lookahead bias (only use complete bars)
   - Convert strategy output to Signal objects:
     {
       'symbol': str,
       'action': 'BUY'/'SELL'/'HOLD',
       'quantity': int,
       'order_type': 'MARKET'/'LIMIT',
       'limit_price': float (optional),
       'reason': str,
       'indicators': dict
     }

3. Signal to Order Conversion:
   - Create Order objects from signals
   - Validate against portfolio (sufficient cash/shares)
   - Apply position sizing rules
   - Check risk limits before submission

4. State Management:
   - Track strategy state across bars
   - Persist indicators and intermediate calculations
   - Handle strategy initialization

5. Features:
   - Support for multi-symbol strategies
   - Signal strength/confidence levels
   - Dry-run mode (generate signals but don't trade)
   - Signal history logging to STRATEGY_SIGNALS table

6. Include:
   - Abstract base Strategy class
   - Example strategy implementation (SMA crossover)
   - Comprehensive logging
   - Unit tests with mock market data
   - Integration tests with Portfolio
```

---

### **Step 14: Position Sizing Engine**

**Prompt for LLM:**
```
Create a position sizing system for risk-based order sizing:

1. PositionSizer class:
   - calculate_position_size(signal, portfolio, risk_params)
   - apply_fixed_quantity(quantity)
   - apply_fixed_dollar_amount(dollar_amount)
   - apply_percentage_of_portfolio(percentage)
   - apply_risk_percentage(risk_per_trade, stop_loss_pct)
   - apply_kelly_criterion(win_rate, avg_win, avg_loss, fraction)
   - apply_volatility_based(atr, risk_amount)

2. Position Sizing Methods:
   a) Fixed quantity (e.g., 100 shares)
   b) Fixed dollar amount (e.g., $10,000 per trade)
   c) Percentage of portfolio (e.g., 5% of equity)
   d) Risk-based (risk X% of portfolio on each trade)
   e) ATR-based (volatility-adjusted sizing)
   f) Kelly Criterion (optimal growth)

3. Constraints:
   - Maximum position size per symbol
   - Maximum total exposure
   - Minimum trade size (avoid tiny positions)
   - Round lots (if required)

4. Risk Parameters:
   - Max risk per trade (e.g., 1% of portfolio)
   - Max total risk (sum of all open positions)
   - Max concentration per symbol
   - Sector exposure limits

5. Include:
   - Configuration file for sizing rules
   - Validation of calculated sizes
   - Logging of sizing decisions
   - Unit tests for each method
   - Integration with StrategyAdapter
```

---

## **PHASE 6: RISK MANAGEMENT**

### **Step 15: Risk Manager**

**Prompt for LLM:**
```
Create a comprehensive risk management system with pre-trade and post-trade checks:

1. RiskManager class:
   - validate_order(order, portfolio)
   - check_buying_power(required_cash)
   - check_position_limits(symbol, new_quantity)
   - check_drawdown_limits(portfolio)
   - check_daily_loss_limit(portfolio)
   - check_leverage(portfolio)
   - emergency_stop_all(reason)

2. Pre-Trade Checks:
   - Sufficient cash for the trade
   - Position size within limits
   - Not exceeding max positions
   - Symbol not on restricted list
   - Order size not too large (% of daily volume)

3. Position-Level Limits:
   - Max position size per symbol (shares or $)
   - Max percentage of portfolio per symbol
   - Max number of open positions
   - Sector concentration limits

4. Portfolio-Level Limits:
   - Max drawdown from peak (e.g., 10%)
   - Daily loss limit (e.g., 2% of equity)
   - Weekly/monthly loss limits
   - Max leverage ratio
   - Maximum total exposure

5. Circuit Breakers:
   - Halt trading if drawdown exceeded
   - Stop trading on consecutive losses
   - Pause on technical errors
   - Resume conditions

6. Include:
   - Configurable risk parameters (YAML/JSON)
   - Risk limit hierarchy (order -> position -> portfolio)
   - Detailed rejection reasons
   - Alert system for limit breaches
   - Override mechanism (manual intervention)
   - Logging all risk decisions
   - Unit tests for each check
```

---

### **Step 16: Stop Loss & Take Profit Manager**

**Prompt for LLM:**
```
Create an automated stop loss and take profit management system:

1. StopLossManager class:
   - add_stop_loss(position, stop_type, params)
   - add_take_profit(position, target_type, params)
   - update_trailing_stops(current_prices)
   - check_stops(market_data)
   - remove_stops(position_id)

2. Stop Loss Types:
   a) Fixed price stop
   b) Percentage stop (e.g., 2% below entry)
   c) ATR-based stop (e.g., 2x ATR below entry)
   d) Trailing stop (fixed amount)
   e) Trailing stop (percentage)
   f) Time-based stop (exit after X bars)

3. Take Profit Types:
   a) Fixed price target
   b) Percentage target (e.g., 5% above entry)
   c) Risk/reward ratio (e.g., 2:1)
   d) Resistance level
   e) Trailing take profit

4. Management Features:
   - Automatic order creation for stops
   - Move stop to breakeven after target hit
   - Scale out (partial profit taking)
   - OCO (One-Cancels-Other) orders

5. Update Logic:
   - Check stops on every new price update
   - Update trailing stops when price moves favorably
   - Log all stop modifications
   - Generate exit signals when stops hit

6. Include:
   - Configuration for each stop type
   - Support for multiple stops per position
   - Backtesting mode (log what would have happened)
   - Unit tests for each stop type
   - Integration with OrderExecutor
```

---

## **PHASE 7: PERFORMANCE TRACKING**

### **Step 17: Performance Calculator**

**Prompt for LLM:**
```
Create a comprehensive performance metrics calculation engine:

1. PerformanceCalculator class:
   - calculate_all_metrics(portfolio)
   - calculate_returns_metrics()
   - calculate_risk_metrics()
   - calculate_trade_statistics()
   - calculate_ratios()
   - update_equity_curve()

2. Return Metrics:
   - Total return (%)
   - Daily returns
   - Cumulative returns
   - Annualized return
   - CAGR (Compound Annual Growth Rate)
   - Month-over-month returns
   - Best/worst day, week, month

3. Risk Metrics:
   - Volatility (std dev of returns)
   - Annualized volatility
   - Maximum drawdown ($)
   - Maximum drawdown (%)
   - Drawdown duration
   - Current drawdown
   - Value at Risk (VaR 95%, 99%)

4. Risk-Adjusted Metrics:
   - Sharpe Ratio (assuming risk-free rate)
   - Sortino Ratio (downside deviation)
   - Calmar Ratio (return/max drawdown)
   - Information Ratio
   - Treynor Ratio

5. Trade Statistics:
   - Total trades
   - Winning trades / Losing trades
   - Win rate (%)
   - Average win / Average loss
   - Largest win / Largest loss
   - Profit factor (gross profit / gross loss)
   - Average holding period
   - Expectancy per trade

6. Additional Metrics:
   - Average trade size
   - Average bars in trade
   - Commission total
   - Slippage total
   - Net profit
   - Number of consecutive wins/losses

7. Include:
   - Real-time calculation on each trade close
   - Historical metrics storage in PERFORMANCE_METRICS table
   - Benchmark comparison (e.g., vs S&P 500)
   - Configurable risk-free rate
   - Unit tests for each calculation
   - Pandas/NumPy for efficient computation
```

---

### **Step 18: Trade Analyzer**

**Prompt for LLM:**
```
Create a trade analysis system for detailed trade insights:

1. TradeAnalyzer class:
   - analyze_trade(trade)
   - categorize_trades(trades_list)
   - find_patterns(trades_list)
   - generate_trade_report(date_range)
   - export_trades(format='csv'/'json'/'excel')

2. Trade Categorization:
   - By symbol
   - By strategy signal type
   - By time of day entered
   - By day of week
   - By holding period (scalp/day/swing)
   - By profit/loss buckets
   - By exit reason (stop loss/take profit/signal/timeout)

3. Pattern Analysis:
   - Winning/losing streaks
   - Performance by hour of day
   - Performance by day of week
   - Best/worst performing symbols
   - Optimal holding periods
   - Entry price vs average price analysis

4. Trade Quality Metrics:
   - Execution quality (fill price vs mid price)
   - Slippage analysis
   - Commission as % of PnL
   - MAE (Maximum Adverse Excursion)
   - MFE (Maximum Favorable Excursion)

5. Reporting:
   - Daily trade summary
   - Weekly performance report
   - Monthly analysis with charts
   - Trade-by-trade breakdown
   - Export to Excel with formatting

6. Include:
   - Matplotlib/Plotly for visualizations
   - PDF report generation
   - Email capability for reports
   - Configurable report templates
   - Unit tests for analysis functions
```

---

### **Step 19: Real-Time Dashboard**

**Prompt for LLM:**
```
Create a real-time monitoring dashboard for forward testing:

1. Dashboard Framework:
   - Use Streamlit, Dash, or Flask + HTML/JS
   - Auto-refresh every 5-30 seconds
   - Responsive design

2. Dashboard Sections:

   a) Portfolio Overview:
      - Current equity (large display)
      - Cash available
      - Positions value
      - Today's P&L ($ and %)
      - Total P&L ($ and %)

   b) Open Positions Table:
      - Symbol
      - Quantity
      - Entry price
      - Current price
      - Unrealized P&L
      - Unrealized P&L %
      - Position age

   c) Recent Trades Table:
      - Last 10-20 trades
      - Symbol, entry/exit, P&L, % return
      - Color coded (green/red)

   d) Performance Charts:
      - Equity curve (line chart)
      - Daily P&L (bar chart)
      - Drawdown chart
      - Win/loss ratio (pie chart)

   e) Active Orders:
      - Pending orders
      - Order status
      - Cancel button

   f) Key Metrics Panel:
      - Total trades today
      - Win rate
      - Sharpe ratio
      - Max drawdown
      - Current risk exposure

   g) System Status:
      - Market data connection status
      - Strategy status (running/paused)
      - Last data update timestamp
      - System health indicators

3. Features:
   - Start/Stop/Pause buttons
   - Manual order entry form
   - Position close buttons
   - Alert notifications
   - Dark/light mode

4. Include:
   - WebSocket for real-time updates (optional)
   - Authentication for multi-user
   - Logging page showing recent logs
   - Configuration page for parameters
   - Mobile-responsive design
   - Unit tests for backend logic
```

---

## **PHASE 8: SYSTEM ORCHESTRATION**

### **Step 20: Main Forward Testing Engine**

**Prompt for LLM:**
```
Create the main orchestration engine that ties all components together:

1. ForwardTestingEngine class:
   - __init__(config_file)
   - initialize_system()
   - start()
   - stop()
   - pause()
   - resume()
   - run_loop()

2. Initialization:
   - Load configuration from YAML/JSON
   - Initialize database connection
   - Create/load portfolio
   - Initialize all managers (risk, order, data, etc.)
   - Load strategy
   - Connect to market data feed
   - Restore state if recovering from crash

3. Main Event Loop:
   ```python
   while running:
       # Get market data
       market_data = data_handler.get_latest_data()
       
       # Validate data
       if not data_validator.validate(market_data):
           continue
       
       # Update portfolio positions with current prices
       portfolio.update_positions(market_data)
       
       # Check and update stops
       stop_manager.check_stops(market_data)
       
       # Generate strategy signals
       signals = strategy_adapter.generate_signals(market_data)
       
       # Apply position sizing
       sized_signals = position_sizer.size_positions(signals)
       
       # Risk check
       approved_orders = risk_manager.validate_orders(sized_signals)
       
       # Execute orders
       for order in approved_orders:
           order_executor.submit_order(order)
       
       # Process pending order fills
       filled_orders = order_executor.check_fills(market_data)
       for fill in filled_orders:
           portfolio.process_fill(fill)
       
       # Update performance metrics
       performance_calculator.update_metrics(portfolio)
       
       # Save state periodically
       if should_save_state():
           state_manager.save_state()
       
       # Sleep until next iteration
       time.sleep(sleep_interval)
   ```

4. State Management:
   - Save full system state every N minutes
   - Checkpoint after each trade
   - Graceful shutdown (save state before exit)
   - Recovery on restart

5. Error Handling:
   - Try-catch around main loop
   - Reconnection logic for data feed
   - Alert on critical errors
   - Automatic pause on repeated errors
   - Detailed error logging

6. Configuration:
   ```yaml
   portfolio:
     initial_capital: 100000
     name: "Forward Test 1"
   
   strategy:
     name: "SMA_Crossover"
     parameters:
       fast_period: 10
       slow_period: 30
   
   risk:
     max_position_size: 10000
     max_positions: 5
     max_drawdown_pct: 10
     daily_loss_limit_pct: 2
   
   execution:
     slippage_model: "volume_based"
     commission_model: "ibkr"
   
   data:
     provider: "alpaca"
     symbols: ["AAPL", "MSFT", "GOOGL"]
     timeframe: "1min"
   
   system:
     loop_interval_seconds: 1
     save_state_interval_minutes: 5
     market: "US"
   ```

7. Lifecycle Hooks:
   - on_start()
   - on_market_open()
   - on_market_close()
   - on_stop()
   - on_error()

8. Monitoring:
   - Log heartbeat every minute
   - Track loop execution time
   - Memory usage monitoring
   - Alert on slow loops (>1 second)

9. Include:
   - Comprehensive logging setup
   - Signal handlers (SIGINT, SIGTERM)
   - Configuration validation on startup
   - Dry-run mode
   - Backtesting mode (replay historical data)
   - Integration tests simulating full day
   - Docker container setup
   - systemd service file for Linux
```

---

## **BONUS STEPS**

### **Step 21: Alert & Notification System**

**Prompt for LLM:**
```
Create a multi-channel alerting system:

1. AlertManager class:
   - send_alert(level, message, channels)
   - configure_alerts(config)
   - alert_on_trade(trade)
   - alert_on_error(error)
   - alert_on_limit_breach(limit_type)

2. Alert Channels:
   - Email (SMTP)
   - SMS (Twilio)
   - Slack webhook
   - Discord webhook
   - Telegram bot
   - Desktop notification
   - Log file

3. Alert Types:
   - Trade executed
   - Stop loss hit
   - Take profit hit
   - Risk limit breached
   - System error
   - Daily/weekly summary
   - Position opened/closed

4. Configuration:
   - Alert level thresholds (INFO/WARNING/ERROR/CRITICAL)
   - Channel routing (errors to SMS, trades to email)
   - Quiet hours
   - Rate limiting (max N alerts per hour)

5. Include:
   - Template system for messages
   - Alert history in database
   - Unit tests for each channel
```

---

### **Step 22: Backtesting Comparison Tool**

**Prompt for LLM:**
```
Create a tool to compare forward test results with backtest results:

1. ComparisonAnalyzer class:
   - load_backtest_results(file_path)
   - load_forward_test_results(portfolio_id)
   - compare_metrics()
   - compare_trades()
   - generate_comparison_report()

2. Comparison Metrics:
   - Return difference
   - Sharpe ratio difference
   - Win rate difference
   - Trade count difference
   - Average slippage/commission impact
   - Drawdown comparison

3. Analysis:
   - Identify where forward test underperformed
   - Calculate total slippage/commission cost
   - Detect execution quality issues
   - Look-ahead bias detection

4. Visualization:
   - Side-by-side equity curves
   - Metric comparison table
   - Difference attribution (what caused variance)

5. Include:
   - Export comparison report to PDF
   - Recommendations for improvement
   - Statistical significance tests
```

---

### **Step 23: Configuration Manager**

**Prompt for LLM:**
```
Create a centralized configuration management system:

1. ConfigManager class:
   - load_config(file_path)
   - validate_config()
   - get(key_path)
   - set(key_path, value)
   - save_config()
   - reload_config()

2. Features:
   - Support YAML and JSON formats
   - Environment variable overrides
   - Schema validation (using JSON Schema)
   - Default values
   - Hot-reload capability (no restart needed)

3. Configuration Categories:
   - Database settings
   - Broker API credentials
   - Strategy parameters
   - Risk limits
   - Execution parameters
   - Alert settings

4. Security:
   - Encrypt sensitive values (API keys)
   - .env file support for secrets
   - Never log sensitive values

5. Include:
   - Example configuration files
   - Configuration documentation
   - Validation error messages
   - Unit tests
```

---

### **Step 24: Testing & CI/CD Setup**

**Prompt for LLM:**
```
Create a comprehensive testing framework and CI/CD pipeline:

1. Test Suite Structure:
   ```
   tests/
   ├── unit/
   │   ├── test_portfolio.py
   │   ├── test_position.py
   │   ├── test_order.py
   │   ├── test_fills.py
   │   ├── test_risk_manager.py
   │   └── ...
   ├── integration/
   │   ├── test_order_execution_flow.py
   │   ├── test_strategy_to_trade.py
   │   └── ...
   ├── e2e/
   │   └── test_full_trading_day.py
   └── fixtures/
       └── market_data_samples.py
   ```

2. Testing Tools:
   - pytest for test framework
   - pytest-cov for coverage
   - pytest-mock for mocking
   - hypothesis for property-based testing
   - tox for multiple Python versions

3. Mock Components:
   - MockBrokerAPI (simulates broker responses)
   - MockMarketDataFeed (provides test data)
   - MockTimeManager (controllable time)
   - MockDatabase (in-memory test DB)

4. CI/CD Pipeline (GitHub Actions / GitLab CI):
   ```yaml
   stages:
     - lint
     - test
     - build
     - deploy
   
   lint:
     - pylint
     - black (formatting)
     - mypy (type checking)
   
   test:
     - unit tests (must pass)
     - integration tests
     - coverage report (min 80%)
   
   build:
     - Docker image
     - Documentation
   
   deploy:
     - Deploy to staging
     - Run smoke tests
     - Deploy to production (manual approval)
   ```

5. Include:
   - Pre-commit hooks
   - Code coverage badge
   - Test data generators
   - Performance benchmarks
   - Load testing scripts
```

---

## **Implementation Timeline**

| Phase | Steps | Estimated Time | Dependencies |
|-------|-------|----------------|--------------|
| **Phase 1** | 1-2 | 3-5 days | None |
| **Phase 2** | 3-6 | 5-7 days | Phase 1 |
| **Phase 3** | 7-9 | 4-6 days | Phase 2 |
| **Phase 4** | 10-12 | 5-7 days | Phase 1 |
| **Phase 5** | 13-14 | 4-5 days | Phase 2, 4 |
| **Phase 6** | 15-16 | 4-5 days | Phase 2, 3 |
| **Phase 7** | 17-19 | 5-7 days | Phase 2, 3 |
| **Phase 8** | 20 | 3-5 days | All previous |
| **Bonus** | 21-24 | 5-7 days | Phase 8 |

**Total Estimated Time: 6-8 weeks**

---

## **Usage Instructions**

For each step, copy the corresponding prompt and provide it to an LLM with this context:

```
I am building a forward testing trading simulator system. This component is part of a larger system with the following technology stack:
- Language: Python 3.9+
- Database: PostgreSQL (or SQLite for dev)
- ORM: SQLAlchemy
- Data: pandas, numpy
- Testing: pytest
- Broker API: [YOUR_BROKER - e.g., Alpaca, Interactive Brokers]

Please provide:
1. Complete, production-ready code with type hints
2. Error handling and logging
3. Docstrings for all classes and methods
4. Unit tests covering main scenarios
5. A requirements.txt section for dependencies
6. Brief usage example

[PASTE SPECIFIC STEP PROMPT HERE]
```

---

## **Additional Recommendations**

1. **Start with Steps 1-6** (Database + Core Models) - This is your foundation
2. **Then Steps 10-12** (Data Integration) - Get live data flowing
3. **Then Steps 7-9** (Execution) - Make orders work
4. **Then Step 20** (Main Engine) - Tie it together
5. **Then remaining steps** - Add sophistication

6. **Version Control**: Commit after each step completion
7. **Documentation**: Keep a running documentation of each component
8. **Testing**: Test each component before moving to next
9. **Incremental**: Get basic version working, then enhance

Would you like me to elaborate on any specific step or provide the code for any particular component to get you started?
