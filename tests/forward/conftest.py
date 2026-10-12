"""Forward test fixtures.

The legacy forward tests (test_mstock_live_bus, test_feed_quality) exercise
bar dedupe, multi-runner sharing, and queue mechanics under the assumption
of a normal trading day. Without this fixture, running pytest on a weekend
or an official market holiday (like 2026-10-02 Gandhi Jayanti) would cause
the real-time feed holiday gate to rest and fail those non-calendar tests.

Holiday gating itself is thoroughly tested in tests/test_market_status.py.
"""

import pytest
from unittest.mock import patch
from backtest.forward.feed_registry import _BrokerBarFeedBase


@pytest.fixture(autouse=True)
def default_trading_day_for_legacy_forward_tests():
    """Default to TRADING_DAY so legacy feed tests pass regardless of execution day."""
    with patch.object(_BrokerBarFeedBase, "_market_day_state", return_value=("TRADING_DAY", "Trading day")):
        yield
