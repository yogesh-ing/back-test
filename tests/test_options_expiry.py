"""Tests for expiry handling (Phase 6).

Covers:
- T6.1: Expiry detection (within window, past expiry, not yet expiring)
- T6.2: Auto-square off before expiry
- T6.3: Cash settlement (ITM/OTM, long/short)
- T6.4: Settlement price providers (static, LTP fallback)
- T6.5: Expiry notifications (alerts)
- T6.6: Integration test: hold through expiry
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from backtest.options.paper_trading import (
    FakeQuoteProvider,
    OptionPaperBroker,
    OptionPosition,
    PositionStatus,
)
from backtest.options.expiry import (
    ExpiryAlert,
    ExpiryAlertType,
    ExpiryManager,
    LtpFallbackSettlementProvider,
    StaticSettlementProvider,
)
from backtest.strategy.intent import (
    Direction,
    MarketView,
    OptionLeg,
    TradeIntent,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_view() -> MarketView:
    return MarketView(
        direction=Direction.BULLISH,
        confidence=0.8,
        underlying="NIFTY",
        spot_price=Decimal("24800"),
    )


def _expiry_on(d: date) -> date:
    return d


def _make_intent(
    strike: int = 24800,
    option_type: str = "CE",
    side: str = "BUY",
    expiry: date | None = None,
) -> TradeIntent:
    leg = OptionLeg(
        instrument_token=f"T{strike}",
        trading_symbol=f"NIFTY26SEP{strike}{option_type}",
        side=side,
        quantity=1,
        lot_size=25,
    )
    return TradeIntent(
        view=_make_view(),
        structure_type="long_call",
        legs=(leg,),
        expiry=expiry or date(2026, 9, 24),
        strategy_name="test_strategy",
        metadata={"option_type": option_type, "strike": str(strike)},
    )


def _broker_with_position(
    expiry: date,
    side: str = "BUY",
    option_type: str = "CE",
    strike: int = 24800,
    price: float = 100.0,
) -> tuple[OptionPaperBroker, str]:
    """Create a broker with one open position; return (broker, structure_id)."""
    broker = OptionPaperBroker(capital=1_000_000.0)
    quotes = FakeQuoteProvider(default_price=price)
    intent = _make_intent(strike=strike, option_type=option_type, side=side, expiry=expiry)
    positions = broker.execute_structure(intent, quotes)
    return broker, positions[0].structure_id


# Expiry date used across tests: a Thursday in the past relative to "now"
_PAST_EXPIRY = date(2026, 9, 24)
_NOW = datetime(2026, 9, 25, 10, 0)  # day after expiry


# ---------------------------------------------------------------------------
# T6.1 — Detection
# ---------------------------------------------------------------------------

class TestDetection:
    def test_detects_position_expiring_today_within_window(self):
        expiry = date(2026, 9, 24)
        # 15:10 on expiry day → within 30 min of 15:30 close
        now = datetime(2026, 9, 24, 15, 10)
        broker, _ = _broker_with_position(expiry)
        manager = ExpiryManager(broker, squareoff_minutes_before=30)

        expiring = manager.detect_expiring(as_of=now)
        assert len(expiring) == 1
        assert expiring[0].expiry == expiry

    def test_does_not_detect_outside_window(self):
        expiry = date(2026, 9, 24)
        # 14:00 on expiry day → more than 30 min before close
        now = datetime(2026, 9, 24, 14, 0)
        broker, _ = _broker_with_position(expiry)
        manager = ExpiryManager(broker, squareoff_minutes_before=30)

        expiring = manager.detect_expiring(as_of=now)
        assert len(expiring) == 0

    def test_detects_past_expiry_as_stale(self):
        broker, _ = _broker_with_position(_PAST_EXPIRY)
        manager = ExpiryManager(broker)

        # Stale positions only appear when explicitly requested
        expiring = manager.detect_expiring(as_of=_NOW, include_stale=True)
        assert len(expiring) == 1
        assert manager.detect_expiring(as_of=_NOW) == []

    def test_ignores_future_expiry(self):
        future = date(2026, 10, 29)
        broker, _ = _broker_with_position(future)
        manager = ExpiryManager(broker)

        expiring = manager.detect_expiring(as_of=_NOW)
        assert len(expiring) == 0

    def test_custom_threshold(self):
        expiry = date(2026, 9, 24)
        now = datetime(2026, 9, 24, 14, 45)  # 45 min before close
        broker, _ = _broker_with_position(expiry)
        manager = ExpiryManager(broker)

        # 30 min window: 14:45 is outside
        assert len(manager.detect_expiring(as_of=now)) == 0
        # 60 min window: 14:45 is inside
        assert len(manager.detect_expiring(as_of=now, minutes_threshold=60)) == 1


# ---------------------------------------------------------------------------
# T6.2 — Auto square off
# ---------------------------------------------------------------------------

class TestAutoSquareOff:
    def test_squares_off_expiring_position(self):
        expiry = date(2026, 9, 24)
        now = datetime(2026, 9, 24, 15, 10)
        broker, structure_id = _broker_with_position(expiry, price=100.0)
        quotes = FakeQuoteProvider(default_price=120.0)  # price moved up

        manager = ExpiryManager(broker, squareoff_minutes_before=30)
        results = manager.auto_square_off(quotes, as_of=now)

        assert len(results) == 1
        assert results[0].squared_off is True
        assert results[0].realized_pnl > 0  # bought at 100, closed at 120
        assert broker.get_open_positions() == []

    def test_no_square_off_when_nothing_expiring(self):
        future = date(2026, 10, 29)
        broker, _ = _broker_with_position(future)
        quotes = FakeQuoteProvider()

        manager = ExpiryManager(broker)
        results = manager.auto_square_off(quotes, as_of=_NOW)
        assert results == []
        assert len(broker.get_open_positions()) == 1

    def test_emits_alert(self):
        expiry = date(2026, 9, 24)
        now = datetime(2026, 9, 24, 15, 10)
        broker, _ = _broker_with_position(expiry)
        quotes = FakeQuoteProvider(default_price=110.0)

        manager = ExpiryManager(broker)
        manager.auto_square_off(quotes, as_of=now)

        alerts = manager.get_alerts(ExpiryAlertType.AUTO_SQUARED_OFF)
        assert len(alerts) == 1
        assert "Auto-squared off" in alerts[0].message

    def test_square_off_zero_disabled(self):
        expiry = date(2026, 9, 24)
        now = datetime(2026, 9, 24, 15, 10)
        broker, _ = _broker_with_position(expiry)
        quotes = FakeQuoteProvider(default_price=110.0)

        # squareoff_minutes_before=0 → 15:30 cutoff → 15:10 is before it
        manager = ExpiryManager(broker, squareoff_minutes_before=0)
        results = manager.auto_square_off(quotes, as_of=now)
        assert results == []
        assert len(broker.get_open_positions()) == 1


# ---------------------------------------------------------------------------
# T6.3 — Cash settlement
# ---------------------------------------------------------------------------

class TestCashSettlement:
    def test_long_call_itm_receives_intrinsic(self):
        broker, _ = _broker_with_position(
            _PAST_EXPIRY, side="BUY", option_type="CE", strike=24800, price=100.0
        )
        provider = StaticSettlementProvider({"NIFTY": 25000.0})  # 200 pts ITM

        manager = ExpiryManager(broker)
        results = manager.settle_expired(provider, as_of=_NOW)

        assert len(results) == 1
        r = results[0]
        assert r.was_itm is True
        assert r.intrinsic_value == 200.0
        # Long ITM: receives 200 × 25 = 5000 (premium already debited at entry)
        assert r.realized_pnl == Decimal("5000")
        assert broker.get_open_positions() == []

    def test_long_call_otm_expires_worthless(self):
        broker, _ = _broker_with_position(
            _PAST_EXPIRY, side="BUY", option_type="CE", strike=24800, price=100.0
        )
        provider = StaticSettlementProvider({"NIFTY": 24500.0})  # OTM

        manager = ExpiryManager(broker)
        results = manager.settle_expired(provider, as_of=_NOW)

        assert len(results) == 1
        r = results[0]
        assert r.was_itm is False
        assert r.intrinsic_value == 0.0
        assert r.realized_pnl == Decimal("0")

    def test_short_call_itm_pays_intrinsic(self):
        broker, _ = _broker_with_position(
            _PAST_EXPIRY, side="SELL", option_type="CE", strike=24800, price=100.0
        )
        provider = StaticSettlementProvider({"NIFTY": 25000.0})

        manager = ExpiryManager(broker)
        results = manager.settle_expired(provider, as_of=_NOW)

        assert len(results) == 1
        r = results[0]
        assert r.was_itm is True
        # Short ITM: pays 200 × 25 = 5000
        assert r.realized_pnl == Decimal("-5000")

    def test_long_put_itm(self):
        broker, _ = _broker_with_position(
            _PAST_EXPIRY, side="BUY", option_type="PE", strike=25000, price=100.0
        )
        provider = StaticSettlementProvider({"NIFTY": 24500.0})  # 500 pts ITM

        manager = ExpiryManager(broker)
        results = manager.settle_expired(provider, as_of=_NOW)

        assert len(results) == 1
        assert results[0].intrinsic_value == 500.0
        assert results[0].realized_pnl == Decimal("12500")  # 500 × 25

    def test_position_marked_expired(self):
        broker, _ = _broker_with_position(_PAST_EXPIRY)
        provider = StaticSettlementProvider({"NIFTY": 25000.0})

        manager = ExpiryManager(broker)
        manager.settle_expired(provider, as_of=_NOW)

        positions = broker.get_open_positions()
        assert positions == []
        # Position still exists internally with EXPIRED status
        all_pos = list(broker._positions.values())
        assert len(all_pos) == 1
        assert all_pos[0].status == PositionStatus.EXPIRED

    def test_cash_updated_after_settlement(self):
        broker, _ = _broker_with_position(
            _PAST_EXPIRY, side="BUY", option_type="CE", strike=24800, price=100.0
        )
        cash_before = broker.available_cash
        provider = StaticSettlementProvider({"NIFTY": 25000.0})

        manager = ExpiryManager(broker)
        manager.settle_expired(provider, as_of=_NOW)

        assert broker.available_cash > cash_before

    def test_does_not_settle_unexpired(self):
        future = date(2026, 10, 29)
        broker, _ = _broker_with_position(future)
        provider = StaticSettlementProvider({"NIFTY": 25000.0})

        manager = ExpiryManager(broker)
        results = manager.settle_expired(provider, as_of=_NOW)
        assert results == []


# ---------------------------------------------------------------------------
# T6.4 — Settlement price providers
# ---------------------------------------------------------------------------

class TestSettlementProviders:
    def test_static_provider(self):
        provider = StaticSettlementProvider({"NIFTY": 25000.0, "BANKNIFTY": 52000.0})
        assert provider.get_settlement_price("NIFTY") == 25000.0
        assert provider.get_settlement_price("BANKNIFTY") == 52000.0

    def test_static_provider_default(self):
        provider = StaticSettlementProvider(default=100.0)
        assert provider.get_settlement_price("UNKNOWN") == 100.0

    def test_ltp_fallback_provider(self):
        qp = FakeQuoteProvider(default_price=24750.0)
        provider = LtpFallbackSettlementProvider(qp)
        assert provider.get_settlement_price("NIFTY") == 24750.0


# ---------------------------------------------------------------------------
# T6.5 — Alerts
# ---------------------------------------------------------------------------

class TestAlerts:
    def test_expired_itm_alert(self):
        broker, _ = _broker_with_position(
            _PAST_EXPIRY, side="BUY", option_type="CE", strike=24800
        )
        provider = StaticSettlementProvider({"NIFTY": 25000.0})

        manager = ExpiryManager(broker)
        manager.settle_expired(provider, as_of=_NOW)

        alerts = manager.get_alerts(ExpiryAlertType.EXPIRED_ITM)
        assert len(alerts) == 1
        assert "ITM" in alerts[0].message

    def test_expired_otm_alert(self):
        broker, _ = _broker_with_position(
            _PAST_EXPIRY, side="BUY", option_type="CE", strike=24800
        )
        provider = StaticSettlementProvider({"NIFTY": 24500.0})

        manager = ExpiryManager(broker)
        manager.settle_expired(provider, as_of=_NOW)

        alerts = manager.get_alerts(ExpiryAlertType.EXPIRED_OTM)
        assert len(alerts) == 1
        assert "OTM" in alerts[0].message

    def test_settled_summary_alert(self):
        broker, _ = _broker_with_position(_PAST_EXPIRY)
        provider = StaticSettlementProvider({"NIFTY": 25000.0})

        manager = ExpiryManager(broker)
        manager.settle_expired(provider, as_of=_NOW)

        alerts = manager.get_alerts(ExpiryAlertType.SETTLED)
        assert len(alerts) == 1
        assert "Settled 1" in alerts[0].message

    def test_alert_to_dict(self):
        alert = ExpiryAlert(
            alert_type=ExpiryAlertType.EXPIRED_ITM,
            message="test message",
            position_ids=["p1"],
        )
        d = alert.to_dict()
        assert d["type"] == "expired_itm"
        assert d["message"] == "test message"
        assert d["position_ids"] == ["p1"]

    def test_clear_alerts(self):
        broker, _ = _broker_with_position(_PAST_EXPIRY)
        provider = StaticSettlementProvider({"NIFTY": 25000.0})

        manager = ExpiryManager(broker)
        manager.settle_expired(provider, as_of=_NOW)
        assert len(manager.get_alerts()) > 0

        manager.clear_alerts()
        assert manager.get_alerts() == []


# ---------------------------------------------------------------------------
# T6.6 — Integration: hold through expiry
# ---------------------------------------------------------------------------

class TestIntegrationThroughExpiry:
    def test_full_expiry_pipeline(self):
        """T6.6: Open → hold past expiry → square-off window → settlement."""
        expiry = date(2026, 9, 24)
        broker = OptionPaperBroker(capital=1_000_000.0)
        quotes = FakeQuoteProvider(default_price=100.0)

        # Open a long call at strike 24800
        intent = _make_intent(strike=24800, side="BUY", expiry=expiry)
        positions = broker.execute_structure(intent, quotes)
        structure_id = positions[0].structure_id
        assert len(broker.get_open_positions()) == 1

        # Day before expiry: nothing happens
        manager = ExpiryManager(broker, squareoff_minutes_before=30)
        day_before = datetime(2026, 9, 23, 15, 0)
        summary = manager.process_expiries(
            quotes, StaticSettlementProvider({"NIFTY": 25000.0}), as_of=day_before
        )
        assert summary["squared_off_count"] == 0
        assert summary["settled_count"] == 0
        assert len(broker.get_open_positions()) == 1

        # Expiry day, inside square-off window: auto-squared off at LTP
        expiry_day = datetime(2026, 9, 24, 15, 10)
        quotes.set_price("T24800", 150.0)
        summary = manager.process_expiries(
            quotes, StaticSettlementProvider({"NIFTY": 25000.0}), as_of=expiry_day
        )
        assert summary["squared_off_count"] == 1
        assert summary["settled_count"] == 0
        assert broker.get_open_positions() == []
        assert positions[0].status == PositionStatus.CLOSED

    def test_settlement_when_not_squared_off(self):
        """If square-off is disabled, position rides into cash settlement."""
        expiry = date(2026, 9, 24)
        broker = OptionPaperBroker(capital=1_000_000.0)
        quotes = FakeQuoteProvider(default_price=100.0)

        intent = _make_intent(strike=24800, side="BUY", expiry=expiry)
        broker.execute_structure(intent, quotes)

        # Square-off disabled (0 minutes) → 15:10 is before 15:30 cutoff
        manager = ExpiryManager(broker, squareoff_minutes_before=0)
        expiry_day = datetime(2026, 9, 24, 15, 10)
        summary = manager.process_expiries(
            quotes, StaticSettlementProvider({"NIFTY": 25000.0}), as_of=expiry_day
        )
        # Not squared off (before cutoff), not yet settled (expiry == today, not past)
        assert summary["squared_off_count"] == 0
        assert summary["settled_count"] == 0

        # Day after expiry: stale position gets cash-settled
        day_after = datetime(2026, 9, 25, 10, 0)
        summary = manager.process_expiries(
            quotes, StaticSettlementProvider({"NIFTY": 25000.0}), as_of=day_after
        )
        assert summary["settled_count"] == 1
        assert summary["total_pnl"] == Decimal("5000")  # 200 pts ITM × 25

    def test_spread_squared_off_together(self):
        """Both legs of a spread square off in the same pass."""
        expiry = date(2026, 9, 24)
        broker = OptionPaperBroker(capital=1_000_000.0)
        quotes = FakeQuoteProvider(default_price=100.0)

        long_leg = OptionLeg(
            instrument_token="T24700", trading_symbol="NIFTY26SEP24700CE",
            side="BUY", quantity=1, lot_size=25,
        )
        short_leg = OptionLeg(
            instrument_token="T25000", trading_symbol="NIFTY26SEP25000CE",
            side="SELL", quantity=1, lot_size=25,
        )
        intent = TradeIntent(
            view=_make_view(),
            structure_type="bull_call_spread",
            legs=(long_leg, short_leg),
            expiry=expiry,
            strategy_name="test",
            metadata={"option_type": "CE"},
        )
        positions = broker.execute_structure(intent, quotes)

        manager = ExpiryManager(broker, squareoff_minutes_before=30)
        expiry_day = datetime(2026, 9, 24, 15, 10)
        results = manager.auto_square_off(quotes, as_of=expiry_day)

        assert len(results) == 2
        assert all(r.squared_off for r in results)
        assert broker.get_open_positions() == []
