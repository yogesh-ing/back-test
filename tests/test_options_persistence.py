"""Gap G4.2 — option structure persistence.

The paper book must survive a server restart:

* open a structure  -> ``trade_structures`` row ``status='open'``
* close a structure -> row stamped ``closed`` with realized P&L
* settle at expiry  -> row stamped ``expired``
* fresh process     -> open rows rehydrate into a new broker, debiting
  cash exactly as the original execution did

Runs on a file-backed SQLite database (tmp_path), no external services.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal as D

import pytest

from backtest.db.config import DEFAULT_CONFIG_PATH
from backtest.db.manager import DatabaseManager
from backtest.db.models import TradeStructure, TradeStructureStatus
from backtest.options.paper_trading import (
    FakeQuoteProvider,
    OptionPaperBroker,
    PositionStatus,
)
from backtest.options.persistence import StructurePersistence
from backtest.strategy.intent import Direction, MarketView, OptionLeg, TradeIntent


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def persistence(tmp_path):
    manager = DatabaseManager.from_env(
        path=str(DEFAULT_CONFIG_PATH),
        profile="testing",
        url=f"sqlite:///{tmp_path / 'options.db'}",
    )
    manager.connect()
    p = StructurePersistence(manager)
    p.ensure_schema()
    return p


def _intent(structure_id_seed: str = "L1") -> TradeIntent:
    legs = (OptionLeg(structure_id_seed, structure_id_seed, "BUY", 1, 75),)
    return TradeIntent(
        view=MarketView(
            direction=Direction.BULLISH, underlying="NIFTY", spot_price=D("24800")
        ),
        structure_type="long_call",
        legs=legs,
        expiry=date.today() + timedelta(days=20),
        metadata={"option_type": "CE", "strikes": {structure_id_seed: "24800"}},
    )


def _wired_broker(persistence, capital=100_000.0, **kwargs):
    """An OptionPaperBroker whose open/close events mirror into the DB."""

    def on_opened(structure, entry_fees):
        persistence.save_open(structure, entry_fees=entry_fees)

    def on_closed(structure, realized_pnl):
        all_expired = all(leg.status == PositionStatus.EXPIRED for leg in structure.legs)
        persistence.mark_closed(
            structure.structure_id,
            realized_pnl,
            closed_at=structure.closed_at,
            status="expired" if all_expired else "closed",
        )

    return OptionPaperBroker(
        capital=capital,
        slippage_pct=0.0,
        commission_per_lot=0.0,
        on_structure_opened=on_opened,
        on_structure_closed=on_closed,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Write path
# ---------------------------------------------------------------------------


class TestWritePath:
    def test_open_structure_persists_open_row(self, persistence):
        broker = _wired_broker(persistence)
        quotes = FakeQuoteProvider(default_price=120.50)
        positions = broker.execute_structure(_intent(), quotes)
        structure_id = positions[0].structure_id

        row = persistence.get_row(structure_id)
        assert row is not None
        assert row.status == TradeStructureStatus.OPEN.value
        assert row.structure_type == "long_call"
        assert row.underlying == "NIFTY"
        assert row.closed_at is None
        assert D(str(row.entry_cost)) == D("9037.50")  # 75 x 120.50
        assert len(row.legs["items"]) == 1
        assert row.legs["items"][0]["trading_symbol"] == "L1"

    def test_close_structure_stamps_row(self, persistence):
        broker = _wired_broker(persistence)
        quotes = FakeQuoteProvider(default_price=120.50)
        positions = broker.execute_structure(_intent(), quotes)
        structure_id = positions[0].structure_id

        quotes.set_price("L1", 130.00)
        broker.close_structure(structure_id, quotes)

        row = persistence.get_row(structure_id)
        assert row.status == TradeStructureStatus.CLOSED.value
        assert row.closed_at is not None
        # Gross realized P&L: (130 - 120.50) x 75
        assert D(str(row.realized_pnl)) == D("712.50")

    def test_expiry_settlement_stamps_expired(self, persistence):
        from backtest.options.expiry import ExpiryManager, StaticSettlementProvider

        broker = _wired_broker(persistence)
        quotes = FakeQuoteProvider(default_price=120.50)

        # A structure that expired yesterday (long 24800 CE, ITM at 24900).
        intent = TradeIntent(
            view=MarketView(
                direction=Direction.BULLISH, underlying="NIFTY", spot_price=D("24800")
            ),
            structure_type="long_call",
            legs=(OptionLeg("E1", "E1", "BUY", 1, 75),),
            expiry=date.today() - timedelta(days=1),
            metadata={"option_type": "CE", "strikes": {"E1": "24800"}},
        )
        positions = broker.execute_structure(intent, quotes)
        structure_id = positions[0].structure_id

        manager = ExpiryManager(broker)
        results = manager.settle_expired(StaticSettlementProvider({"NIFTY": 24900.0}))
        assert len(results) == 1

        row = persistence.get_row(structure_id)
        assert row.status == TradeStructureStatus.EXPIRED.value
        assert row.closed_at is not None
        # Realized P&L is net of the entry premium (120.50), like
        # `OptionPosition.close`: (100 intrinsic − 120.50) x 75. The gross
        # 100 x 75 = 7,500 double-counted the premium the buyer had paid.
        entry_price = broker._positions[positions[0].position_id].entry_price
        assert D(str(row.realized_pnl)) == (D("100") - entry_price) * 75


# ---------------------------------------------------------------------------
# Read path — restart rehydration
# ---------------------------------------------------------------------------


class TestReload:
    def test_load_open_round_trip(self, persistence):
        broker = _wired_broker(persistence)
        quotes = FakeQuoteProvider(default_price=120.50)
        positions = broker.execute_structure(_intent(), quotes)
        structure_id = positions[0].structure_id

        loaded = persistence.load_open()
        assert len(loaded) == 1
        structure, entry_fees = loaded[0]
        assert structure.structure_id == structure_id
        assert structure.structure_type == "long_call"
        assert structure.expiry is not None
        assert len(structure.legs) == 1
        leg = structure.legs[0]
        assert leg.entry_price == D("120.50")
        assert leg.quantity == 1 and leg.lot_size == 75
        assert leg.status == PositionStatus.OPEN
        assert entry_fees == D("0")  # no fee calculator attached here

    def test_restore_rebuilds_cash_exactly(self, persistence):
        from backtest.simulator.fees import CommissionCalculator

        # Phase 1: open with the full statutory stack, note the cash.
        broker = _wired_broker(
            persistence, fee_calculator=CommissionCalculator.for_broker("mstock")
        )
        quotes = FakeQuoteProvider(default_price=120.50)
        broker.execute_structure(_intent(), quotes)
        cash_before_restart = broker.available_cash

        # Phase 2: "restart" — a brand-new broker restores from the DB.
        fresh = OptionPaperBroker(capital=100_000.0, slippage_pct=0.0, commission_per_lot=0.0)
        for structure, entry_fees in persistence.load_open():
            fresh.restore_structure(structure, fees_paid=entry_fees)

        assert fresh.available_cash == cash_before_restart
        assert len(fresh.get_open_positions()) == 1
        assert len(fresh.get_open_structures()) == 1

    def test_closed_rows_are_not_reloaded(self, persistence):
        broker = _wired_broker(persistence)
        quotes = FakeQuoteProvider(default_price=120.50)
        positions = broker.execute_structure(_intent(), quotes)
        broker.close_structure(positions[0].structure_id, quotes)

        assert persistence.load_open() == []


# ---------------------------------------------------------------------------
# Web-level restart round trip (POST trade -> restart -> positions back)
# ---------------------------------------------------------------------------


class TestWebRestartRoundTrip:
    def test_positions_survive_restart(self, tmp_path, monkeypatch):
        from backtest.web import options_api
        from backtest.web.app import create_app

        manager = DatabaseManager.from_env(
            path=str(DEFAULT_CONFIG_PATH),
            profile="testing",
            url=f"sqlite:///{tmp_path / 'web_options.db'}",
        )
        manager.connect()
        web_persistence = StructurePersistence(manager)
        web_persistence.ensure_schema()

        monkeypatch.setattr(options_api, "_build_persistence", lambda: web_persistence)
        monkeypatch.delenv("OPTIONS_PERSISTENCE", raising=False)
        options_api.reset_option_state()
        app = create_app(source="synthetic")
        app.config["TESTING"] = True
        client = app.test_client()

        # Phase 1: open a structure via the dashboard driver.
        resp = client.post(
            "/api/options/trade",
            json={"underlying": "NIFTY", "structure_type": "long_call", "quantity": 1},
        )
        assert resp.status_code == 201
        structure_id = resp.get_json()["structure_id"]
        cash_phase1 = client.get("/api/options/summary").get_json()["available_cash"]

        row = web_persistence.get_row(structure_id)
        assert row is not None and row.status == "open"

        # Phase 2: simulate a server restart — singletons are gone, only
        # the database remains.
        options_api.reset_option_state()
        data = client.get("/api/options/summary").get_json()
        assert data["open_position_count"] == 1
        assert data["open_structure_count"] == 1
        assert data["positions"][0]["structure_id"] == structure_id
        # Cash rehydrated exactly (premium + commissions + entry fees).
        assert data["available_cash"] == pytest.approx(float(cash_phase1), abs=0.01)

        # Phase 3: close after the restart — the row flips to 'closed'.
        resp = client.post(f"/api/options/structures/{structure_id}/close")
        assert resp.status_code == 200
        row = web_persistence.get_row(structure_id)
        assert row.status == "closed"
        assert row.closed_at is not None

        options_api.reset_option_state()

    def test_persistence_disabled_by_env(self, monkeypatch):
        from backtest.web import options_api
        from backtest.web.app import create_app

        monkeypatch.setenv("OPTIONS_PERSISTENCE", "off")
        options_api.reset_option_state()
        app = create_app(source="synthetic")
        app.config["TESTING"] = True
        client = app.test_client()

        resp = client.post(
            "/api/options/trade",
            json={"underlying": "NIFTY", "structure_type": "long_call"},
        )
        assert resp.status_code == 201
        assert options_api._persistence is None
        options_api.reset_option_state()
