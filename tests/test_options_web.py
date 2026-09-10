"""Tests for the options UI & API (Phase 7).

Covers:
- T7.1: JSON API endpoints (summary, positions, greeks, close, expiry)
- T7.2/T7.3: Serializer output shape for tables
- Route wiring: /options page renders, nav data-key present
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from backtest.web.app import create_app
from backtest.web.options_api import (
    get_option_broker,
    reset_option_state,
)
from backtest.strategy.intent import (
    Direction,
    MarketView,
    OptionLeg,
    TradeIntent,
)


@pytest.fixture()
def app():
    reset_option_state()
    app = create_app(source="synthetic")
    app.config["TESTING"] = True
    yield app
    reset_option_state()


@pytest.fixture()
def client(app):
    return app.test_client()


def _seed_two_structures() -> None:
    """Seed the singleton broker with a long call and a bull call spread."""
    broker = get_option_broker()
    from backtest.options.paper_trading import FakeQuoteProvider

    quotes = FakeQuoteProvider(default_price=100.0)
    view = MarketView(
        direction=Direction.BULLISH,
        confidence=0.8,
        underlying="NIFTY",
        spot_price=Decimal("24800"),
    )

    broker.execute_structure(
        TradeIntent(
            view=view,
            structure_type="long_call",
            legs=(OptionLeg("T24800", "NIFTY26SEP24800CE", "BUY", 1, 25),),
            expiry=date(2026, 10, 29),
            strategy_name="strat_a",
            metadata={"option_type": "CE", "strike": "24800"},
        ),
        quotes,
    )
    broker.execute_structure(
        TradeIntent(
            view=view,
            structure_type="bull_call_spread",
            legs=(
                OptionLeg("T24700", "NIFTY26SEP24700CE", "BUY", 1, 25),
                OptionLeg("T25000", "NIFTY26SEP25000CE", "SELL", 1, 25),
            ),
            expiry=date(2026, 10, 29),
            strategy_name="strat_b",
            metadata={
                "option_type": "CE",
                "strikes": {
                    "NIFTY26SEP24700CE": "24700",
                    "NIFTY26SEP25000CE": "25000",
                },
            },
        ),
        quotes,
    )


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

class TestOptionsPage:
    def test_page_renders(self, client):
        resp = client.get("/options")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert "Options Trading" in html

    def test_page_has_greeks_card(self, client):
        html = client.get("/options").get_data(as_text=True)
        assert "Portfolio Greeks" in html
        assert "gk-delta" in html

    def test_nav_link_present(self, client):
        html = client.get("/options").get_data(as_text=True)
        assert 'href="/options"' in html
        assert 'data-key="options"' in html


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

class TestSummaryApi:
    def test_empty_summary(self, client):
        resp = client.get("/api/options/summary")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["open_position_count"] == 0
        assert data["positions"] == []
        assert data["structures"] == []
        assert "greeks" in data
        assert data["greeks"]["total_delta"] == 0.0

    def test_summary_with_positions(self, client):
        _seed_two_structures()
        data = client.get("/api/options/summary").get_json()

        assert data["open_position_count"] == 3
        assert data["open_structure_count"] == 2
        assert len(data["positions"]) == 3
        assert len(data["structures"]) == 2

    def test_summary_greeks_nonzero(self, client):
        _seed_two_structures()
        data = client.get("/api/options/summary").get_json()
        assert data["greeks"]["total_delta"] != 0.0

    def test_summary_capital_fields(self, client):
        _seed_two_structures()
        data = client.get("/api/options/summary").get_json()
        assert data["capital"] == 1_000_000.0
        assert data["available_cash"] < 1_000_000.0
        assert data["total_equity"] > 0


class TestPositionsApi:
    def test_positions_endpoint(self, client):
        _seed_two_structures()
        data = client.get("/api/options/positions").get_json()
        assert len(data["positions"]) == 3

    def test_position_serializer_shape(self, client):
        _seed_two_structures()
        data = client.get("/api/options/positions").get_json()
        p = data["positions"][0]
        for key in (
            "position_id", "structure_id", "trading_symbol", "side",
            "quantity", "lot_size", "strike", "option_type", "expiry",
            "entry_price", "current_price", "unrealized_pnl", "status",
        ):
            assert key in p
        assert p["status"] == "open"


class TestCloseStructureApi:
    def test_close_existing(self, client):
        _seed_two_structures()
        summary = client.get("/api/options/summary").get_json()
        structure_id = summary["structures"][0]["structure_id"]

        resp = client.post(f"/api/options/structures/{structure_id}/close")
        assert resp.status_code == 200
        payload = resp.get_json()
        assert "realized_pnl" in payload

        # Now closed — summary should show one fewer structure
        after = client.get("/api/options/summary").get_json()
        assert after["open_structure_count"] == 1

    def test_close_nonexistent_404(self, client):
        resp = client.post("/api/options/structures/nope/close")
        assert resp.status_code == 404
        assert "error" in resp.get_json()


class TestGreeksApi:
    def test_greeks_endpoint(self, client):
        _seed_two_structures()
        data = client.get("/api/options/greeks").get_json()
        assert "total_delta" in data
        assert "by_underlying" in data
        assert data["total_delta"] != 0.0


class TestExpiryProcessApi:
    def test_expiry_process_no_op(self, client):
        """Pipeline runs cleanly with nothing expired."""
        _seed_two_structures()  # future expiry
        resp = client.post("/api/options/expiry/process")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["squared_off_count"] == 0
        assert data["settled_count"] == 0
