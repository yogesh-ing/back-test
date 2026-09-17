"""P1.1 — live option chain/quotes wiring (the last synthetic gap).

Four seams, one story:

1. ``LiveChainProvider`` — real mStock chain + LTP behind the generator duck
   type the ``OptionsBridge`` already speaks (``generate_chain`` /
   ``available_expiries`` / ``get_spot`` / ``get_quote`` / ``register_chain``);
   ONE instrument-master fetch per TTL no matter the caller count, error-soft
   quotes, and a fail-loud ``get_spot`` until the first real bar (never a
   silent synthetic-scale substitution).
2. ``ChainBus`` live path — ``acquire(u, source="mstock", broker=...)`` hands
   the SAME provider to every runner on the underlying (refcounted, evicted
   at zero); a live acquire without a broker raises.
3. Runner wiring — an option runner with ``source="mstock"`` + authenticated
   session gets a bridge whose ``quote_source`` reads ``live:mstock``; stop()
   releases the LIVE refcount (symmetric with acquire); no session → the
   labelled synthetic fallback.
4. Engine quote seam (U2 fix) — ``_resolve_quote_source`` in live mode now
   returns a ``LiveQuoteProvider`` (the QUOTE contract) gated on a session
   check that actually exists; previously it returned a BAR feed behind a
   gate that could never pass.

Everything runs against fakes — no network, no credentials.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from backtest.forward.feed_registry import (
    get_chain_bus,
    option_quote_provider_for,
    reset_data_bus,
)
from backtest.forward.paper_runner import RunnerConfig
from backtest.options.quote_providers import (
    LiveChainProvider,
    LiveQuoteProvider,
    SyntheticChainGenerator,
)
from backtest.instruments.base import OptionType
from backtest.instruments.option import OptionContract


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _contract(
    strike: int,
    expiry: date,
    option_type: OptionType = OptionType.CE,
    underlying: str = "NIFTY",
    token: str | None = None,
) -> OptionContract:
    token = token or f"TOK-{underlying}-{strike}-{option_type.value}"
    return OptionContract(
        instrument_token=token,
        trading_symbol=f"{underlying}{expiry.strftime('%y%m')}{strike}{option_type.value}",
        underlying=underlying,
        expiry=expiry,
        strike=Decimal(strike),
        option_type=option_type,
        lot_size=75,
        metadata={},
    )


class FakeBroker:
    """Duck-typed MStockBroker: canned chain + L1 quotes, counts calls."""

    def __init__(self, contracts=None, quotes=None):
        self.contracts = list(contracts or [])
        self.quotes = dict(quotes or {})
        self.chain_calls = 0
        self.quote_calls = 0

    def get_option_chain(self, underlying):
        self.chain_calls += 1
        return list(self.contracts)

    def get_option_quote(self, token):
        self.quote_calls += 1
        return self.quotes.get(token, {"ltp": 0.0})


EXP1 = date.today() + timedelta(days=30)
EXP2 = date.today() + timedelta(days=60)
CHAIN = [
    _contract(24_700, EXP1), _contract(24_750, EXP1), _contract(24_800, EXP1),
    _contract(24_850, EXP1), _contract(24_900, EXP1),
    _contract(24_800, EXP1, OptionType.PE), _contract(24_850, EXP1, OptionType.PE),
    _contract(24_800, EXP2),
]
QUOTES = {
    "TOK-NIFTY-24800-CE": {"ltp": 112.5, "bid": 111.0, "ask": 114.0, "volume": 1000, "oi": 5000},
    "TOK-NIFTY-24850-CE": {"ltp": 84.25, "bid": 83.0, "ask": 85.5},
}


@pytest.fixture()
def bus():
    reset_data_bus()
    yield
    reset_data_bus()


# ---------------------------------------------------------------------------
# 1. LiveChainProvider
# ---------------------------------------------------------------------------


class TestLiveChainProvider:
    def test_identity_and_generator_self_reference(self):
        provider = LiveChainProvider(broker=FakeBroker(CHAIN))
        assert provider.source_name == "live:mstock"
        assert provider.generator is provider, "bridge._generator() contract"

    def test_generate_chain_filters_expiry_and_type(self):
        provider = LiveChainProvider(broker=FakeBroker(CHAIN))
        chain = provider.generate_chain("NIFTY", expiry=EXP1, option_type="CE")
        assert set(chain) == {
            Decimal(24_700), Decimal(24_750), Decimal(24_800), Decimal(24_850), Decimal(24_900),
        }
        assert all(c.option_type is OptionType.CE for c in chain.values())

        puts = provider.generate_chain("NIFTY", expiry=EXP1, option_type="PE")
        assert set(puts) == {Decimal(24_800), Decimal(24_850)}

    def test_generate_chain_default_expiry_is_nearest(self):
        provider = LiveChainProvider(broker=FakeBroker(CHAIN))
        chain = provider.generate_chain("NIFTY", option_type="CE")
        assert {c.expiry for c in chain.values()} == {EXP1}

    def test_generate_chain_no_matching_contracts_raises(self):
        provider = LiveChainProvider(broker=FakeBroker([_contract(24_800, EXP2)]))
        with pytest.raises(ValueError, match="no CE contracts"):
            provider.generate_chain("NIFTY", expiry=EXP1)

    def test_generate_chain_bad_option_type_raises(self):
        with pytest.raises(ValueError, match="option_type"):
            LiveChainProvider(broker=FakeBroker(CHAIN)).generate_chain(
                "NIFTY", option_type="XX"
            )

    def test_one_chain_fetch_shared_across_calls(self):
        """The rate-limit rule: N calls → ONE instrument-master fetch."""
        broker = FakeBroker(CHAIN)
        provider = LiveChainProvider(broker=broker, chain_ttl_seconds=900)
        provider.generate_chain("NIFTY", option_type="CE")
        provider.available_expiries("NIFTY")
        provider.generate_chain("NIFTY", option_type="PE")
        assert broker.chain_calls == 1

    def test_available_expiries_sorted_and_filtered_by_reference(self):
        provider = LiveChainProvider(broker=FakeBroker(CHAIN))
        assert provider.available_expiries("NIFTY") == [EXP1, EXP2]
        assert provider.available_expiries("NIFTY", reference=EXP1 + timedelta(days=1)) == [EXP2]

    def test_quotes_are_live_ltp(self):
        broker = FakeBroker(CHAIN, QUOTES)
        provider = LiveChainProvider(broker=broker)
        quote = provider.get_quote("TOK-NIFTY-24800-CE")
        assert quote["ltp"] == 112.5 and quote["bid"] == 111.0
        assert broker.quote_calls == 1

    def test_quote_ttl_caches(self):
        broker = FakeBroker(CHAIN, QUOTES)
        provider = LiveChainProvider(broker=broker, quote_ttl_seconds=60)
        provider.get_quote("TOK-NIFTY-24800-CE")
        provider.get_quote("TOK-NIFTY-24800-CE")
        assert broker.quote_calls == 1

    def test_quote_failure_is_error_soft(self):
        broker = FakeBroker(CHAIN)
        broker.get_option_quote = lambda token: (_ for _ in ()).throw(RuntimeError("api down"))
        provider = LiveChainProvider(broker=broker)
        quote = provider.get_quote("TOK-NIFTY-24800-CE")
        assert quote["ltp"] == 0.0 and "error" in quote

    def test_chain_fetch_failure_is_error_soft_with_last_known(self):
        """Warm cache survives a broker outage; a cold cache serves nothing."""
        broker = FakeBroker(CHAIN)
        provider = LiveChainProvider(broker=broker)
        provider.generate_chain("NIFTY", option_type="CE")  # warm the cache

        def boom(underlying):
            raise RuntimeError("master down")

        broker.get_option_chain = boom
        assert provider.generate_chain("NIFTY", option_type="CE"), "last-known terms"

        cold = LiveChainProvider(broker=FakeBroker([]))
        with pytest.raises(ValueError, match="no option contracts"):
            cold.generate_chain("NIFTY")  # nothing known, nothing served

    def test_get_spot_fail_loud_until_first_bar(self):
        provider = LiveChainProvider(broker=FakeBroker(CHAIN))
        with pytest.raises(ValueError, match="no live spot yet"):
            provider.get_spot("NIFTY")
        provider.set_spot("NIFTY", 24_812.5)
        assert provider.get_spot("NIFTY") == 24_812.5

    def test_price_contract_uses_live_ltp(self):
        provider = LiveChainProvider(broker=FakeBroker(CHAIN, QUOTES))
        contract = _contract(24_800, EXP1)
        assert provider.price_contract(contract) == 112.5

    def test_set_reference_is_accepted_noop(self):
        LiveChainProvider(broker=FakeBroker(CHAIN)).set_reference(datetime.now())


# ---------------------------------------------------------------------------
# 2. ChainBus live path
# ---------------------------------------------------------------------------


class TestChainBusLive:
    def test_live_acquire_requires_broker(self, bus):
        with pytest.raises(ValueError, match="requires a quote broker"):
            get_chain_bus().acquire("NIFTY", source="mstock")

    def test_two_runners_share_one_live_provider(self, bus):
        broker = FakeBroker(CHAIN)
        cb = get_chain_bus()
        p1 = cb.acquire("NIFTY", source="mstock", broker=broker)
        p2 = cb.acquire("NIFTY", source="mstock", broker=FakeBroker())
        assert p1 is p2 and isinstance(p1, LiveChainProvider)
        assert cb.subscriber_count("NIFTY", source="mstock") == 2

    def test_live_release_evicts_at_zero(self, bus):
        cb = get_chain_bus()
        cb.acquire("BANKNIFTY", source="mstock", broker=FakeBroker(CHAIN))
        assert cb.release("BANKNIFTY", source="mstock") == 0
        assert cb.generator_count() == 0

    def test_live_and_synthetic_entries_are_independent(self, bus):
        cb = get_chain_bus()
        live = cb.acquire("NIFTY", source="mstock", broker=FakeBroker(CHAIN))
        synthetic = cb.acquire("NIFTY")
        assert isinstance(live, LiveChainProvider)
        assert isinstance(synthetic, SyntheticChainGenerator)
        assert cb.generator_count() == 2

    def test_synthetic_path_unchanged(self, bus):
        cb = get_chain_bus()
        gen = cb.acquire("NIFTY")
        assert isinstance(gen, SyntheticChainGenerator)
        assert cb.release("NIFTY") == 0  # default source still synthetic


# ---------------------------------------------------------------------------
# 3. Runner wiring
# ---------------------------------------------------------------------------


def _option_runner(source: str) -> RunnerConfig:
    return RunnerConfig(
        name=f"OPT-{source.upper()}",
        strategy_name="directional_options",
        allocated_capital=500_000,
        symbols=["NIFTY"],
        timeframe="1day",
        mode="paper",
        source=source,
        instrument={
            "type": "option",
            "expression": {"type": "bull_call_spread", "strike_selection": "atm", "quantity": 1},
        },
    )


@pytest.fixture()
def authenticated(monkeypatch):
    """The session manager reports an authenticated FakeBroker."""
    broker = FakeBroker(CHAIN, QUOTES)
    import backtest.forward.feed_registry as fr

    monkeypatch.setattr(fr, "_default_quote_broker", lambda: broker)
    return broker


def _runner(config):
    """A runner with its own ledger/broker (as the manager would build it)."""
    from backtest.forward.paper_runner import OrderLedger, PaperBroker, StrategyRunner

    ledger = OrderLedger()
    return StrategyRunner(config, ledger=ledger, broker=PaperBroker(ledger))


class TestRunnerWiring:
    def test_mstock_runner_gets_live_provider(self, bus, authenticated):
        runner = _runner(_option_runner("mstock"))
        try:
            assert runner.options_bridge is not None
            assert runner.options_bridge.quote_provider.source_name == "live:mstock"
            assert runner._chain_source == "mstock"
            assert get_chain_bus().subscriber_count("NIFTY", source="mstock") == 1
        finally:
            runner.stop()

    def test_mstock_runner_stop_releases_the_live_refcount(self, bus, authenticated):
        runner = _runner(_option_runner("mstock"))
        runner.stop()
        assert get_chain_bus().subscriber_count("NIFTY", source="mstock") == 0
        assert get_chain_bus().generator_count() == 0

    def test_two_mstock_runners_share_one_provider(self, bus, authenticated):
        r1 = _runner(_option_runner("mstock"))
        r2 = _runner(_option_runner("mstock"))
        try:
            assert r1.options_bridge.quote_provider is r2.options_bridge.quote_provider
            assert get_chain_bus().subscriber_count("NIFTY", source="mstock") == 2
        finally:
            r1.stop()
            r2.stop()

    def test_no_session_falls_back_to_labelled_synthetic(self, bus, monkeypatch):
        import backtest.forward.feed_registry as fr

        monkeypatch.setattr(fr, "_default_quote_broker", lambda: None)
        runner = _runner(_option_runner("mstock"))
        try:
            assert runner.options_bridge.quote_provider.source_name == "synthetic:bs"
            assert runner._chain_source == "synthetic", "release must match the store"
            assert get_chain_bus().subscriber_count("NIFTY") == 1  # synthetic store
        finally:
            runner.stop()

    def test_synthetic_runner_path_untouched(self, bus):
        runner = _runner(_option_runner("synthetic"))
        try:
            assert runner.options_bridge.quote_provider.source_name == "synthetic:bs"
            assert runner._chain_source == "synthetic"
        finally:
            runner.stop()


# ---------------------------------------------------------------------------
# 4. option_quote_provider_for + engine quote seam
# ---------------------------------------------------------------------------


class TestProviderFor:
    def test_synthetic_tuple(self, bus):
        provider, label = option_quote_provider_for("synthetic", "NIFTY")
        assert label == "synthetic:bs"
        assert hasattr(provider, "generator")

    def test_live_tuple_with_broker(self, bus):
        provider, label = option_quote_provider_for(
            "mstock", "NIFTY", quote_broker=FakeBroker(CHAIN)
        )
        assert label == "live:mstock"
        assert isinstance(provider, LiveChainProvider)

    def test_live_without_session_falls_back_labelled(self, bus, monkeypatch):
        import backtest.forward.feed_registry as fr

        monkeypatch.setattr(fr, "_default_quote_broker", lambda: None)
        provider, label = option_quote_provider_for("mstock", "NIFTY")
        assert label == "synthetic:bs", "fallback must be labelled, never silent"


class TestEngineQuoteSeam:
    """The U2 fix: live mode resolves to the QUOTE contract, gated on a real check."""

    @pytest.fixture()
    def engine(self):
        from backtest.engine.execution_engine import ExecutionEngine

        return ExecutionEngine()

    def _patch_session(self, monkeypatch, authenticated: bool, broker=None):
        import backtest.brokers.session_manager as sm

        mgr = MagicMock()
        mgr.is_authenticated.return_value = authenticated
        mgr.get_active_broker.return_value = broker or MagicMock()
        monkeypatch.setattr(sm, "get_session_manager", lambda: mgr)
        return mgr

    def test_live_authenticated_returns_live_quote_provider(self, engine, monkeypatch):
        self._patch_session(monkeypatch, True)
        provider, label = engine._resolve_quote_source("mstock", "live")
        assert label == "live:mstock"
        assert isinstance(provider, LiveQuoteProvider)
        assert hasattr(provider, "get_quote"), "the QUOTE contract"

    def test_live_unauthenticated_returns_labelled_fallback(self, engine, monkeypatch):
        self._patch_session(monkeypatch, False)
        provider, label = engine._resolve_quote_source("mstock", "live")
        assert label == "synthetic-fallback"

    def test_paper_mode_never_touches_the_session(self, engine, monkeypatch):
        mgr = self._patch_session(monkeypatch, True)
        provider, label = engine._resolve_quote_source("mstock", "paper")
        assert label == "synthetic"
        mgr.is_authenticated.assert_not_called(), "paper mode must not consult the broker"

    def test_provider_is_not_the_bar_feed(self, engine, monkeypatch):
        """Regression pin: the seam must never again return MStockLiveFeed."""
        from backtest.data.mstock_live_feed import MStockLiveFeed

        self._patch_session(monkeypatch, True)
        provider, _ = engine._resolve_quote_source("mstock", "live")
        assert not isinstance(provider, MStockLiveFeed)
