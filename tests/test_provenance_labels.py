"""Phase 2 — B4 provenance: the label comes from the broker that produced the row.

Pinned here (impl plan Phase 2):

* ``LiveQuoteProvider`` / ``LiveChainProvider`` ``source_name`` derives from
  ``broker.broker_name`` — a live label names the broker behind it, and a
  duck-typed client without a name is honestly ``live:unnamed``;
* the ChainBus keys live entries by ``(underlying, source)`` — the stored
  ``_FeedEntry.key`` tuple IS the lookup key: the same underlying from two
  sources is TWO providers with TWO derived ``source_name``\\s, both
  ``+bidask``-suffixed (the label path itself unchanged), with per-pair
  identity and refcounts;
* ``execution_engine._resolve_quote_source`` labels the live path
  ``live:<broker>`` after the session broker that produced the quotes;
* ``ChainSnapshotRecorder`` writes the producing broker's name into
  ``option_chain_snapshots.source``;
* ``_persist_bars`` stamps the fetcher's broker name into
  ``market_data_cache.source`` — never the column's ``server_default``;
* the dashboard chain probe (``web/options_api``) tries the configured
  source first, not a fixed mstock-first priority.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event, text

import backtest.brokers.session_manager as session_manager_mod
import backtest.data.sources_policy as sources_policy
import backtest.forward.feed_registry as feed_registry
import backtest.web.options_api as options_api
from backtest.api.data_manager import _persist_bars
from backtest.db.manager import DatabaseManager
from backtest.db.models import MarketDataCache
from backtest.engine.execution_engine import ExecutionEngine
from backtest.forward.feed_registry import get_chain_bus, reset_data_bus
from backtest.instruments.base import OptionType
from backtest.instruments.option import OptionContract
from backtest.options.chain_snapshots import ChainSnapshotRecorder, load_snapshots
from backtest.options.quote_providers import (
    BidAskQuoteProvider,
    LiveChainProvider,
    LiveQuoteProvider,
    broker_label,
)

EXP1 = date.today() + timedelta(days=30)


class _DuckBroker:
    """Minimal quote broker — just enough for ``LiveChainProvider`` + the bus."""

    def __init__(self, name: str) -> None:
        self.broker_name = name

    def get_option_chain(self, underlying):
        return []

    def get_option_quote(self, token):
        return {}


# ---------------------------------------------------------------------------
# source_name derives from the producing broker
# ---------------------------------------------------------------------------


class TestDerivedSourceName:
    def test_quote_provider_label_follows_broker_name(self):
        provider = LiveQuoteProvider(broker=_DuckBroker("dhan"))
        assert provider.source_name == "live:dhan"

    def test_chain_provider_label_follows_broker_name(self):
        provider = LiveChainProvider(_DuckBroker("upstox"))
        assert provider.source_name == "live:upstox"

    def test_mstock_broker_still_reads_live_mstock(self):
        """The familiar label is preserved — it is now DERIVED, not literal."""
        provider = LiveQuoteProvider(broker=_DuckBroker("mstock"))
        assert provider.source_name == "live:mstock"

    def test_duck_broker_without_a_name_is_honestly_unnamed(self):
        assert LiveQuoteProvider(broker=object()).source_name == "live:unnamed"
        assert broker_label(None) == "unnamed"
        assert broker_label(_DuckBroker("dhan")) == "dhan"


# ---------------------------------------------------------------------------
# ChainBus: one live entry per (underlying, source)
# ---------------------------------------------------------------------------


@pytest.fixture()
def bus():
    reset_data_bus()
    yield
    reset_data_bus()


class TestChainBusPerSourceIdentity:
    def test_same_underlying_two_sources_two_providers(self, bus):
        """THE Phase-2 acceptance: two sources ⇒ two providers, two derived
        ``source_name``\\s, both ``+bidask``-suffixed — the label path
        (``feed_registry`` wrap + suffix) itself unchanged."""
        cb = get_chain_bus()
        p_mstock = cb.acquire("NIFTY", source="mstock", broker=_DuckBroker("mstock"))
        p_upstox = cb.acquire("NIFTY", source="upstox", broker=_DuckBroker("upstox"))

        assert p_mstock is not p_upstox, "two sources are two feeds"
        assert isinstance(p_mstock, BidAskQuoteProvider)
        assert isinstance(p_upstox, BidAskQuoteProvider)
        assert p_mstock.source_name == "live:mstock+bidask"
        assert p_upstox.source_name == "live:upstox+bidask"
        assert p_mstock.source_name.endswith("+bidask")
        assert p_upstox.source_name.endswith("+bidask")

    def test_identity_and_refcount_hold_per_pair(self, bus):
        cb = get_chain_bus()
        a1 = cb.acquire("NIFTY", source="mstock", broker=_DuckBroker("mstock"))
        a2 = cb.acquire("NIFTY", source="mstock", broker=_DuckBroker("mstock"))
        cb.acquire("NIFTY", source="upstox", broker=_DuckBroker("upstox"))

        assert a1 is a2, "same (underlying, source) shares one feed"
        assert cb.subscriber_count("NIFTY", source="mstock") == 2
        assert cb.subscriber_count("NIFTY", source="upstox") == 1

        assert cb.release("NIFTY", source="mstock") == 1
        assert cb.subscriber_count("NIFTY", source="mstock") == 1
        assert cb.subscriber_count("NIFTY", source="upstox") == 1, "sibling untouched"

        assert cb.release("NIFTY", source="mstock") == 0
        assert cb.subscriber_count("NIFTY", source="mstock") == 0
        assert cb.subscriber_count("NIFTY", source="upstox") == 1, (
            "pair eviction never crosses sources"
        )

        assert cb.release("NIFTY", source="upstox") == 0
        assert cb.generator_count() == 0

    def test_release_of_unknown_pair_is_idempotent_zero(self, bus):
        cb = get_chain_bus()
        cb.acquire("NIFTY", source="mstock", broker=_DuckBroker("mstock"))
        assert cb.release("NIFTY", source="upstox") == 0, "no such pair"
        assert cb.release("NIFTY", source="dhan") == 0
        assert cb.subscriber_count("NIFTY", source="mstock") == 1, "own pair intact"


# ---------------------------------------------------------------------------
# execution_engine labels the live path after the session broker
# ---------------------------------------------------------------------------


class _SessionStub:
    def __init__(self, broker):
        self._broker = broker

    def is_authenticated(self):
        return True

    def get_active_broker(self):
        return self._broker


class TestExecutionEngineLabel:
    def test_live_label_is_the_session_brokers_name(self, monkeypatch):
        monkeypatch.setattr(
            session_manager_mod,
            "get_session_manager",
            lambda: _SessionStub(_DuckBroker("dhan")),
        )
        provider, label = ExecutionEngine()._resolve_quote_source("mstock", "live")
        assert label == "live:dhan"
        assert provider.source_name == "live:dhan"

    def test_live_label_reads_mstock_for_an_mstock_session(self, monkeypatch):
        monkeypatch.setattr(
            session_manager_mod,
            "get_session_manager",
            lambda: _SessionStub(_DuckBroker("mstock")),
        )
        _, label = ExecutionEngine()._resolve_quote_source("mstock", "live")
        assert label == "live:mstock"


# ---------------------------------------------------------------------------
# ChainSnapshotRecorder writes the producing broker's name
# ---------------------------------------------------------------------------


def _contract(strike: int, expiry: date, ctype: OptionType = OptionType.CE) -> OptionContract:
    token = f"TOK-{expiry.strftime('%m%d')}-{strike}-{ctype.value}"
    return OptionContract(
        instrument_token=token,
        trading_symbol=f"NIFTY{expiry.strftime('%y%m')}{strike}{ctype.value}",
        underlying="NIFTY",
        expiry=expiry,
        strike=Decimal(strike),
        option_type=ctype,
        lot_size=75,
        metadata={},
    )


class _SnapshotClient:
    """Canned client that plays a Dhan broker (B4: rows say so)."""

    broker_name = "dhan"

    def __init__(self, contracts):
        self.contracts = list(contracts)

    def get_option_chain(self, underlying):
        return list(self.contracts)

    def get_option_chain_data(self, underlying, expiry_code, token):
        return {"data": []}


class TestChainSnapshotSource:
    def test_rows_carry_the_producing_brokers_name(self, tmp_path):
        manager = DatabaseManager.from_env(url=f"sqlite:///{tmp_path / 'snap.db'}")
        manager.connect()
        client = _SnapshotClient([_contract(24_800, EXP1)])
        recorder = ChainSnapshotRecorder(client=client, manager=manager)
        recorder.ensure_schema()

        assert recorder.record("NIFTY", quote_expiries=0) == 1
        rows = load_snapshots(manager, underlying="NIFTY")
        assert rows[0]["source"] == "dhan", "the label is the broker that produced the row"


# ---------------------------------------------------------------------------
# _persist_bars stamps the fetcher's broker — never the server_default
# ---------------------------------------------------------------------------


def _mdc_engine(path):
    """Real ``MarketDataCache`` schema on SQLite, plus the Postgres ``now()``
    the upsert SQL calls, registered as a scalar function (before the pool
    hands out its first connection)."""
    engine = create_engine(f"sqlite:///{path}")

    @event.listens_for(engine, "connect")
    def _add_now(dbapi_conn, _record):
        dbapi_conn.create_function(
            "now", 0, lambda: datetime.now(timezone.utc).isoformat(sep=" ")
        )

    MarketDataCache.__table__.create(engine)
    return engine


class TestPersistBarsSource:
    def test_rows_carry_the_fetching_brokers_name(self, tmp_path):
        engine = _mdc_engine(tmp_path / "mdc.db")
        bars = [{"t": "2026-11-10 09:15:00", "o": 100, "h": 105, "l": 95, "c": 101, "v": 1000}]

        inserted = _persist_bars(engine, bars, "NIFTY", "NSE", "1min", source="upstox")
        assert inserted == 1
        with engine.connect() as conn:
            sources = [
                row[0]
                for row in conn.execute(text("SELECT source FROM market_data_cache")).fetchall()
            ]
        assert sources == ["upstox"], "server_default 'mstock' never fires on a live row"

    def test_each_broker_stamps_its_own_rows(self, tmp_path):
        engine = _mdc_engine(tmp_path / "mdc2.db")
        for i, broker in enumerate(("mstock", "upstox", "dhan")):
            bars = [
                {
                    "t": f"2026-11-10 09:{15 + i}:00",
                    "o": 100,
                    "h": 105,
                    "l": 95,
                    "c": 101,
                    "v": 1000,
                }
            ]
            assert _persist_bars(engine, bars, "NIFTY", "NSE", "1min", source=broker) == 1
        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT source FROM market_data_cache ORDER BY ts")
            ).fetchall()
        assert [row[0] for row in rows] == ["mstock", "upstox", "dhan"]


# ---------------------------------------------------------------------------
# dashboard chain probe: the configured source first, not a fixed priority
# ---------------------------------------------------------------------------


class TestDashboardProbeOrder:
    def test_configured_source_is_probed_first(self, monkeypatch):
        calls: list[str] = []

        def fake_default_qb(source=None):
            calls.append(str(source))
            return None

        monkeypatch.setattr(sources_policy, "default_broker_source", lambda: "upstox")
        monkeypatch.setattr(feed_registry, "_default_quote_broker", fake_default_qb)

        assert options_api._live_chain_generator("NIFTY") is None, "no broker anywhere"
        assert calls == ["upstox", "mstock", "dhan"], (
            "configured source first, remaining brokers as fallbacks, no double probe"
        )

    def test_non_broker_default_still_falls_through_to_every_broker(self, monkeypatch):
        calls: list[str] = []

        def fake_default_qb(source=None):
            calls.append(str(source))
            return None

        monkeypatch.setattr(sources_policy, "default_broker_source", lambda: "synthetic")
        monkeypatch.setattr(feed_registry, "_default_quote_broker", fake_default_qb)

        assert options_api._live_chain_generator("NIFTY") is None
        assert calls == ["synthetic", "mstock", "dhan", "upstox"]

    def test_configured_source_wins_when_several_are_authenticated(self, bus, monkeypatch):
        brokers = {"mstock": _DuckBroker("mstock"), "upstox": _DuckBroker("upstox")}
        monkeypatch.setattr(sources_policy, "default_broker_source", lambda: "upstox")
        monkeypatch.setattr(
            feed_registry,
            "_default_quote_broker",
            lambda source=None: brokers.get(str(source)),
        )

        gen = options_api._live_chain_generator("NIFTY")
        assert isinstance(gen, LiveChainProvider)
        assert gen.source_name == "live:upstox", "configured beats fixed priority"
        assert get_chain_bus().subscriber_count("NIFTY", source="upstox") == 1
        assert get_chain_bus().subscriber_count("NIFTY", source="mstock") == 0
