"""Integration tests for forward testing simulator (Step 24)."""

# Integration tests cover flows between components:
# - order execution flow: order -> executor -> fill -> portfolio
# - strategy to trade: strategy -> adapter -> sizer -> risk -> order -> executor
# - full trading day: engine replay

# Example integration tests are in tests/test_strategy_adapter.py, test_forward_engine.py, etc.
# This package is a placeholder for future reorganization as per Step 24 spec:
#   tests/integration/
#   ├── test_order_execution_flow.py
#   ├── test_strategy_to_trade.py
#   └── test_full_trading_day.py
