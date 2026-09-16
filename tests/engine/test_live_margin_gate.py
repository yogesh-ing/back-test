"""U2.3 — Live-mode margin/risk gate

Per-bar: mode=live path broker margin query before order;
margin failure → OrderRejected("margin").
No live path on synthetic fallback — live orders require authenticated broker session,
else OrderRejected("no_session"). Paper mode never queries margin.

Tests per UNIFIED-TRADING-TASKS.md U2.3.
"""

from decimal import Decimal

from backtest.engine.execution_engine import ExecutionEngine, Fill, OrderRejected, RiskHalted
from backtest.playbooks.models import Playbook
from backtest.strategy.intent import Direction
from backtest.strategy.signal import RiskEnvelope, UnifiedSignal


def _signal(underlying="NIFTY", spot=25000, direction=Direction.BULLISH):
    return UnifiedSignal.option_view(
        direction=direction,
        confidence=0.8,
        underlying=underlying,
        spot_price=Decimal(str(spot)),
        strategy_name="test_strategy",
    )


def _playbook(max_loss=100000):
    return Playbook(
        name="test",
        underlying="NIFTY",
        structure_type="bull_call_spread",
        strike_selection="atm",
        quantity=1,
        max_loss_per_trade=max_loss,
    )


class StubBrokerSufficient:
    """Broker with sufficient margin."""

    def __init__(self, available=1000000):
        self.available = available
        self.queried = False

    def get_available_margin(self):
        self.queried = True
        return self.available


class StubBrokerInsufficient:
    """Broker with insufficient margin."""

    def __init__(self, available=100):
        self.available = available
        self.queried = False

    def get_available_margin(self):
        self.queried = True
        return self.available

    def check_margin(self, required):
        self.queried = True
        return False


class StubBrokerCheckMarginBool:
    def __init__(self, ok=True):
        self.ok = ok
        self.queried = False

    def check_margin(self, required):
        self.queried = True
        return self.ok


def test_margin_reject():
    """Live mode with insufficient margin → OrderRejected(reason=margin)."""
    broker = StubBrokerInsufficient(available=100)
    engine = ExecutionEngine(live_broker=broker)
    signal = _signal()
    playbook = _playbook(max_loss=200000)

    # Force live path without going through session manager fallback
    # We inject a quote provider that returns live:mstock label
    # Instead, we directly test _check_live_margin via execute with mode=live, source=synthetic
    # But synthetic source + live mode would not trigger fallback if we mock _resolve_quote_source
    # So we mock _resolve_quote_source to return live:mstock

    original_resolve = engine._resolve_quote_source

    def mock_resolve(source, mode):
        return (None, "live:mstock")

    engine._resolve_quote_source = mock_resolve

    result = engine.execute(signal, playbook, mode="live", source="mstock")
    assert isinstance(result, OrderRejected)
    assert result.reason == "margin"
    assert broker.queried is True


def test_no_session_reject():
    """No live path on synthetic fallback — OrderRejected(no_session)."""
    engine = ExecutionEngine()
    signal = _signal()
    playbook = _playbook()

    # Default _resolve_quote_source will return synthetic-fallback when source=mstock and no session
    result = engine.execute(signal, playbook, mode="live", source="mstock")
    assert isinstance(result, OrderRejected)
    assert result.reason == "no_session"
    assert result.data_source == "synthetic-fallback"


def test_paper_mode_never_queries_margin():
    """Paper mode never queries margin — even if broker configured with 0 margin."""
    broker = StubBrokerInsufficient(available=0)
    engine = ExecutionEngine(live_broker=broker)
    signal = _signal()
    playbook = _playbook()

    result = engine.execute(signal, playbook, mode="paper", source="synthetic")
    # Should be Fill, not margin reject, and broker not queried
    assert isinstance(result, Fill)
    assert broker.queried is False


def test_live_mode_sufficient_margin_allows_fill():
    """Live mode with sufficient margin → Fill (not margin reject)."""
    broker = StubBrokerSufficient(available=1000000)
    engine = ExecutionEngine(live_broker=broker)

    def mock_resolve(source, mode):
        return (None, "live:mstock")

    engine._resolve_quote_source = mock_resolve

    signal = _signal()
    playbook = _playbook()

    result = engine.execute(signal, playbook, mode="live", source="mstock")
    assert isinstance(result, Fill)
    assert broker.queried is True
    assert result.data_source == "live:mstock"


def test_live_mode_no_broker_configured_allows_fill():
    """Live mode with no broker configured → skip margin check, allow Fill (if session mocked as live)."""
    engine = ExecutionEngine(live_broker=None)

    def mock_resolve(source, mode):
        return (None, "live:mstock")

    engine._resolve_quote_source = mock_resolve

    signal = _signal()
    playbook = _playbook()

    result = engine.execute(signal, playbook, mode="live", source="mstock")
    # Should be Fill, not margin reject, because no broker configured = skip check
    assert isinstance(result, Fill)


def test_margin_reject_via_check_margin_interface():
    """Broker with check_margin returning False → OrderRejected margin."""
    broker = StubBrokerCheckMarginBool(ok=False)
    engine = ExecutionEngine(live_broker=broker)

    def mock_resolve(source, mode):
        return (None, "live:mstock")

    engine._resolve_quote_source = mock_resolve

    signal = _signal()
    playbook = _playbook()

    result = engine.execute(signal, playbook, mode="live", source="mstock")
    assert isinstance(result, OrderRejected)
    assert result.reason == "margin"
