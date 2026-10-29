"""The synthetic kill-switch is config-driven and has no fallback (2026-09-30).

The bug these tests pin was not a missing flag — ``config/data_sources.yaml``
already disabled synthetic — it was that the flag was consulted in exactly one
place (the backtest/optimize request guard) while every other component that
picks a data source still had its own hard-coded default or its own labelled
fallback:

* ``api/portfolio.py`` created a runner with ``source or "synthetic"``;
* ``PortfolioManager`` built the 1s random-walk feed unconditionally and routed
  anything not exactly ``"mstock"`` onto it;
* ``option_quote_provider_for`` answered a broker request with Black-Scholes
  chains when no session was authenticated;
* ``runner.build_source`` returned a ``SyntheticSource`` for anyone who asked.

So a runner displayed as ``paper/MSTOCK`` while pricing and filling on
generated candles. The rule now: **when the policy says synthetic is off, no
path reaches synthetic — not by choice, not by default, not as a fallback.**
The code is not deleted; the deployment that wants it sets
``BACKTEST_DATA_PROFILE=testing``, which is exactly what ``tests/conftest.py``
does for the suite.
"""

from __future__ import annotations

import pytest

from backtest.data import sources_policy
from backtest.data.sources_policy import (
    SourceDisabledError,
    build_policy,
    policy_key,
    require_synthetic,
    reset_source_policy,
    source_policy,
    synthetic_enabled,
)


@pytest.fixture()
def production_policy(monkeypatch):
    """Force the shipped default profile (synthetic OFF) for one test."""
    monkeypatch.delenv(sources_policy.PROFILE_ENV, raising=False)
    reset_source_policy()
    policy = build_policy(profile="default")
    monkeypatch.setattr(sources_policy, "_POLICY", policy)
    assert policy.is_enabled("synthetic") is False
    yield policy
    reset_source_policy()


# -- the vocabulary ---------------------------------------------------------


def test_aliases_resolve_feed_names_to_policy_keys():
    """``replay`` is DB bars and ``dhan`` is a broker — not generated data."""
    assert policy_key("replay") == "db"
    assert policy_key("dhan") == "dhan"
    assert policy_key("mstock") == "mstock"
    assert policy_key("synthetic") == "synthetic"
    assert policy_key("banana") is None


def test_unknown_source_is_refused_not_assumed_real(production_policy):
    with pytest.raises(SourceDisabledError, match="Unknown data source"):
        sources_policy.require_enabled("banana", where="test")


def test_dhan_is_a_named_source_of_the_policy(production_policy):
    """A broker a runner can name must be describable, or the control cannot
    allow/refuse it on purpose."""
    assert production_policy.get("dhan") is not None
    assert production_policy.is_enabled("dhan") is True


# -- the guards themselves --------------------------------------------------


def test_require_synthetic_refuses_when_disabled(production_policy):
    with pytest.raises(SourceDisabledError, match="data_sources.yaml"):
        require_synthetic("unit test")


def test_option_chain_never_falls_back_to_synthetic(production_policy):
    """The exact path that produced the wrong-expiry paper book."""
    from backtest.forward.feed_registry import option_quote_provider_for

    with pytest.raises(SourceDisabledError, match="no authenticated mstock session"):
        option_quote_provider_for("mstock", "NIFTY")


def test_no_authenticated_session_does_not_change_the_refusal(production_policy, monkeypatch):
    monkeypatch.setattr(
        "backtest.forward.feed_registry._default_quote_broker", lambda source=None: None
    )
    from backtest.forward.feed_registry import option_quote_provider_for

    with pytest.raises(SourceDisabledError):
        option_quote_provider_for("dhan", "BANKNIFTY")


def test_build_source_refuses_synthetic(production_policy):
    from backtest.runner import build_source

    with pytest.raises(SourceDisabledError, match="no fallback"):
        build_source("synthetic")


def test_portfolio_manager_has_no_synthetic_feed_object(production_policy, tmp_path):
    from backtest.forward.portfolio_manager import PortfolioManager

    mgr = PortfolioManager(state_path=str(tmp_path / "state.json"), auto_start_feed=False)
    assert mgr._synthetic_allowed is False
    assert mgr.feed is None, "the random-walk generator must not even be constructed"
    with pytest.raises(ValueError, match="synthetic feed"):
        mgr._feed_for("synthetic")


def test_feed_routing_is_explicit_not_a_catch_all(production_policy, tmp_path):
    """``else self.feed`` used to swallow typos and unset sources."""
    from backtest.forward.portfolio_manager import PortfolioManager

    mgr = PortfolioManager(state_path=str(tmp_path / "state.json"), auto_start_feed=False)
    assert mgr._feed_for("mstock") is mgr.mstock_feed
    assert mgr._feed_for("dhan") is mgr.dhan_feed
    with pytest.raises(ValueError, match="no bar feed"):
        mgr._feed_for("banana")


def test_a_forward_run_may_not_label_itself_synthetic(production_policy):
    """The (mode, source) a forward run claims has to be true of the config."""
    from flask import Flask

    from backtest.api.forward import _resolve_classification
    from backtest.simulator.errors import ValidationError

    app = Flask(__name__)
    app.config["BACKTEST_SOURCE"] = "db"
    with app.test_request_context(json={}):
        with pytest.raises(ValidationError, match="data_sources.yaml"):
            _resolve_classification({"mode": "paper", "source": "synthetic"})
        # A real source classifies as before, and an omitted one inherits the
        # app's resolved source rather than a literal.
        assert _resolve_classification({"mode": "paper", "source": "mstock"}) == (
            "paper",
            "mstock",
        )
        mode, source = _resolve_classification({"mode": "paper"})
        assert mode == "paper" and source != "synthetic"


def test_the_default_runner_source_is_the_broker(production_policy):
    """``source`` omitted used to mean synthetic; now it means the broker.

    Both the Portfolio spawn endpoint and the playbook spawn read this helper,
    so there is one rule for an unnamed runner.
    """
    from backtest.api.portfolio import _default_runner_source

    assert _default_runner_source() == "mstock"


def test_runner_config_default_is_not_a_backdoor(production_policy, tmp_path):
    """With synthetic off, an unnamed ``RunnerConfig`` is a broker runner.

    The default itself used to be the literal generated feed, so omitting a
    source was the backdoor and only the construction-time gate stood in the
    way. The dataclass now asks the policy, so the default is honest before any
    gate runs — while a state file that still names synthetic keeps refusing.
    """
    from backtest.forward.paper_runner import OrderLedger, RunnerConfig, StrategyRunner

    cfg = RunnerConfig(
        name="ghost", strategy_name="sma_crossover", allocated_capital=100_000,
        symbols=["RELIANCE"],
    )
    assert cfg.source == "mstock", "the default is the policy's broker feed, not a literal"
    assert cfg.source != "synthetic"

    legacy = RunnerConfig(
        name="resurrected", strategy_name="sma_crossover", allocated_capital=100_000,
        symbols=["RELIANCE"], source="synthetic",
    )
    with pytest.raises(SourceDisabledError):
        StrategyRunner(legacy, OrderLedger())


def test_the_options_dashboard_has_no_black_scholes_feed(production_policy, monkeypatch):
    """The dashboard's own quote singleton, separate from the runner's.

    ``get_quote_provider()`` used to answer "no session" with generated
    Black-Scholes chains, and ``_execute_trade`` then read the EXPIRY off a
    generator of its own — the 29-Oct-vs-27-Oct class of bug. With the
    kill-switch off there is no provider at all until a session exists.
    """
    from backtest.brokers import session_manager
    from backtest.web import options_api

    class _NoSession:
        def is_authenticated(self, broker_name=None):
            return False

        def get_active_broker(self):
            return None

        def get_authenticated_broker(self, broker_name=None):
            return None

    # Both lookups go through the manager at call time, so this is the whole
    # of "no broker is authenticated".
    monkeypatch.setattr(session_manager, "get_session_manager", lambda: _NoSession())
    options_api.reset_option_state()
    try:
        with pytest.raises(SourceDisabledError, match="data_sources.yaml"):
            options_api.get_quote_provider()
        assert options_api._quote_provider is None, "refused, not cached as synthetic"
        assert options_api._live_chain_generator("NIFTY") is None
    finally:
        options_api.reset_option_state()


def test_the_pages_pre_select_the_broker_not_the_disabled_feed(production_policy):
    """A form that opens on a source its policy refuses is a 400 waiting to
    happen — the default selection comes from the config too."""
    import re

    from backtest.web.app import create_app

    app = create_app()
    for path in ("/portfolio", "/forward"):
        html = app.test_client().get(path).get_data(as_text=True)
        # The vocabulary stays complete (the taxonomy test depends on it)…
        for tag in ("synthetic", "replay", "mstock"):
            assert f'value="{tag}"' in html, f"{path} lost source {tag}"
        # …but only the broker feed is pre-selected.
        selected = re.findall(r'<option value="([a-z_]+)"\s+selected>', html)
        assert "mstock" in selected, (path, selected)
        assert "synthetic" not in selected, (path, selected)


# -- and the opt-in still works (that is what the profile is FOR) -----------


def test_testing_profile_restores_every_path(monkeypatch, tmp_path):
    monkeypatch.setenv(sources_policy.PROFILE_ENV, "testing")
    reset_source_policy()
    try:
        assert synthetic_enabled() is True
        import re

        from backtest.forward.feed_registry import option_quote_provider_for
        from backtest.forward.portfolio_manager import PortfolioManager
        from backtest.runner import build_source
        from backtest.web.app import create_app

        provider, label = option_quote_provider_for("mstock", "NIFTY")
        assert label == "synthetic:bs", "labelled fallback is the test profile's contract"
        assert build_source("synthetic") is not None
        mgr = PortfolioManager(state_path=str(tmp_path / "s.json"), auto_start_feed=False)
        assert mgr.feed is not None
        assert mgr._feed_for("synthetic") is mgr.feed
        from backtest.api.portfolio import _default_runner_source

        assert _default_runner_source() == "synthetic", "the suite omits a source on purpose"
        from flask import Flask

        from backtest.api.forward import _resolve_classification

        fapp = Flask(__name__)
        fapp.config["BACKTEST_SOURCE"] = "synthetic"
        with fapp.test_request_context(json={}):
            assert _resolve_classification({"mode": "paper", "source": "synthetic"}) == (
                "paper",
                "synthetic",
            )
        # The forms follow the same switch: under the test profile the page
        # does pre-select synthetic, so the check above is the config talking.
        html = create_app().test_client().get("/portfolio").get_data(as_text=True)
        assert "synthetic" in re.findall(r'<option value="([a-z_]+)"\s+selected>', html)
    finally:
        reset_source_policy()


def test_policy_is_resolved_once_and_cached_per_process():
    assert source_policy() is source_policy()


def test_the_chain_bus_has_no_ungated_synth_entry(production_policy):
    """``ChainBus`` was the last ungated way to get a generated chain.

    Its ``acquire``/``release``/``subscriber_count`` defaulted to the synthetic
    feed by literal, and ``_acquire_synthetic`` built a ``SyntheticChainGenerator``
    without asking the config — so any caller that omitted ``source=`` (or asked
    for a broker it had no session for) got generated candles anyway. Now the
    default is the policy's own answer, and the single synthetic constructor is
    the gate.
    """
    from backtest.forward.feed_registry import ChainBus

    bus = ChainBus()
    # Nothing named → the broker feed → a client is required, never a generator.
    with pytest.raises(ValueError, match="no silent synthetic substitution"):
        bus.acquire("NIFTY")
    # Named synthetic → refused by the policy, at the one place it could be built.
    with pytest.raises(SourceDisabledError, match="data_sources.yaml"):
        bus.acquire("NIFTY", source="synthetic")
    assert bus.subscriber_count("NIFTY", source="synthetic") == 0
    # A broker without a client is a refusal too (dhan used to fall through to
    # the synthetic generator — the substitution this closes).
    with pytest.raises(ValueError, match="requires a quote broker client"):
        bus.acquire("NIFTY", source="dhan")


def test_the_chain_bus_still_serves_the_testing_profile():
    """Same bus, policy switched on: the suite keeps its source-less chains."""
    from backtest.data import sources_policy as sp
    from backtest.forward.feed_registry import ChainBus

    sp.reset_source_policy()
    policy = sp.build_policy(profile="testing")
    sp._POLICY = policy
    try:
        bus = ChainBus()
        assert bus.acquire("NIFTY") is not None
        assert bus.subscriber_count("NIFTY") == 1
        assert bus.release("NIFTY") == 0
        assert bus.generator_count() == 0
        broker = type("_Client", (), {})()
        live = bus.acquire("BANKNIFTY", source="dhan", broker=broker)
        assert live.inner.__class__.__name__ == "LiveChainProvider", "dhan is a broker feed"
        assert bus.release("BANKNIFTY", source="dhan") == 0
    finally:
        sp.reset_source_policy()


def test_the_options_bridge_cannot_invent_a_chain(production_policy):
    """``OptionsBridge`` was the last synthetic constructor that asked nothing.

    Built with no provider it made a Black-Scholes book, and handed a provider
    with no chain surface it fitted a generated chain onto it — including onto
    a broker provider. That is the class of bug that priced a live-labelled
    structure on an expiry the market never lists (29 Oct 2026 for a monthly
    that is Tuesday 27 Oct). Both doors now consult config/data_sources.yaml.
    """
    from backtest.forward.options_bridge import OptionsBridge

    class _QuotesButNoChain:
        """Duck-typed provider: a quote feed with no ``.generator`` surface."""

        def get_quote(self, instrument_token):
            return {}

    _V2 = {
        "expression_version": 2,
        "type": "long_call",
        "quantity": 1,
        "exit": {"stop_loss_points": 10_000.0, "min_days_to_expiry": None},
    }
    with pytest.raises(SourceDisabledError, match="data_sources.yaml"):
        OptionsBridge(capital=100_000, expression=_V2)

    bridge = OptionsBridge(capital=100_000, expression=_V2, quote_provider=_QuotesButNoChain())
    assert getattr(bridge.quote_provider, "generator", None) is None, "no silent fitting-out"
    with pytest.raises(SourceDisabledError, match="no option chain feed"):
        bridge._generator()


def test_the_options_bridge_still_builds_for_the_testing_profile():
    """Same bridge, policy on: the suite's provider-less constructions work."""
    from backtest.data import sources_policy as sp
    from backtest.forward.options_bridge import OptionsBridge

    sp.reset_source_policy()
    sp._POLICY = sp.build_policy(profile="testing")
    try:
        bridge = OptionsBridge(
            capital=100_000,
            expression={
                "expression_version": 2,
                "type": "long_call",
                "quantity": 1,
                "exit": {"stop_loss_points": 10_000.0, "min_days_to_expiry": None},
            },
        )
        assert bridge.quote_provider.__class__.__name__ == "SyntheticQuoteProvider"
        assert bridge._generator() is bridge.quote_provider.generator
    finally:
        sp.reset_source_policy()
