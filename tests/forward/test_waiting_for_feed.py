"""Sprint A1 — a book whose broker is logged out stays visible and refuses to open.

The bug this pins is not "the runner failed to start". It is that the runner
**disappeared**: an option runner restored before the operator authenticated
mStock hit ``option_quote_provider_for`` with no session, that helper raised
``SourceDisabledError`` (a ``ValueError``), the restore loop caught it and filed
the snapshot under ``PortfolioManager._parked_snapshots`` — and the dashboard
showed nine runners instead of nineteen. The open structures, strikes, lot sizes
and the operator's manual SL/TP levels went with them, and so did the risk they
carry. On 2026-11-02 that was ten live paper books invisible from boot until the
next restart *after* a login.

The fix has two halves and both are pinned here:

* **Build it anyway** — with no session and synthetic disabled the helper hands
  back a :class:`DisconnectedQuoteProvider`, so the runner exists, restores, and
  reports ``feed_state=WAITING_FOR_FEED``;
* **but never let it price** — the stub answers every quote with an explicit
  failure and has no ``generator``, so held legs keep their last honest mark and
  every new entry is refused with the ``WAITING_FOR_FEED`` code. Fabricating a
  mark is the one thing worse than showing a stale one.

Exits are deliberately NOT gated: the refusal sits after ``on_intent``'s
open-structure branch, so a held book keeps managing its stops, targets and
expiry off the last honest mark while blind. Going blind on an open position
must not also mean being unable to close it.

Isolation: the stub only exists when the policy has synthetic OFF, so every test
here forces the shipped ``default`` profile (the suite's own conftest turns
synthetic ON). The chain bus is process-wide, hence the autouse reset.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backtest.data import sources_policy
from backtest.forward.feed_registry import get_chain_bus, reset_data_bus
from backtest.forward.options_bridge import WAITING_FOR_FEED, OptionsBridge
from backtest.forward.paper_runner import (
    FEED_STATE_LIVE,
    FEED_STATE_WAITING,
    OrderLedger,
    PaperBroker,
    RunnerConfig,
    StrategyRunner,
)
from backtest.forward.portfolio_manager import PortfolioManager
from backtest.forward.risk_supervisor import GlobalRiskConfig
from backtest.forward.state_store import config_to_dict
from backtest.options.quote_providers import DisconnectedQuoteProvider

EXPRESSION = {
    "expression_version": 2,
    "type": {
        "BULLISH": {"structure": "long_call", "strike_selection": "atm"},
        "BEARISH": {"structure": "long_put", "strike_selection": "atm"},
    },
    "quantity": 1,
    "exit": {"stop_loss_points": 10_000.0, "min_days_to_expiry": None},
}


@pytest.fixture(autouse=True)
def production_and_clean_bus(monkeypatch):
    """Synthetic OFF (the shipped profile) and a fresh process-wide chain bus.

    Without the first half the stub is unreachable — ``option_quote_provider_for``
    hands back the labelled synthetic pair instead, and every assertion here
    would be testing the test profile.
    """
    monkeypatch.delenv(sources_policy.PROFILE_ENV, raising=False)
    reset_data_bus()
    sources_policy.reset_source_policy()
    monkeypatch.setattr(sources_policy, "_POLICY", sources_policy.build_policy(profile="default"))
    assert sources_policy.synthetic_enabled() is False
    yield
    reset_data_bus()
    sources_policy.reset_source_policy()


@pytest.fixture()
def logged_out(monkeypatch):
    """No broker session anywhere — the state the 2026-11-02 boot was in."""
    import backtest.forward.feed_registry as fr

    monkeypatch.setattr(fr, "_default_quote_broker", lambda source=None: None)


def _option_config(name: str = "OPT-BLIND", **overrides) -> RunnerConfig:
    fields = dict(
        name=name,
        strategy_name="directional_options",
        allocated_capital=500_000,
        symbols=["NIFTY"],
        timeframe="1day",
        mode="paper",
        source="mstock",
        instrument={"type": "option", "expression": dict(EXPRESSION)},
    )
    fields.update(overrides)
    return RunnerConfig(**fields)


def _runner(config: RunnerConfig) -> StrategyRunner:
    ledger = OrderLedger()
    return StrategyRunner(config, ledger=ledger, broker=PaperBroker(ledger))


def _bridge(provider=None) -> OptionsBridge:
    return OptionsBridge(
        capital=500_000,
        expression=dict(EXPRESSION),
        quote_provider=(
            provider if provider is not None else DisconnectedQuoteProvider("mstock", "NIFTY")
        ),
    )


# ---------------------------------------------------------------------------
# 1. The bridge: visible, unpriceable, and still able to close
# ---------------------------------------------------------------------------


class TestBridgeRefusesEntries:
    def test_the_stub_book_names_its_own_blindness(self):
        bridge = _bridge()
        assert bridge.feed_disconnected is True
        refusal = bridge.feed_refusal
        assert refusal.startswith(WAITING_FOR_FEED)
        assert "mstock" in refusal and "NIFTY" in refusal, "must say what to log into"
        summary = bridge.summary()
        assert summary["feed_disconnected"] is True
        assert summary["feed_refusal"] == refusal
        assert summary["quote_source"] == "disconnected:mstock"

    def test_a_new_entry_is_refused_and_nothing_is_opened(self):
        bridge = _bridge()
        result = bridge.on_intent({}, SimpleNamespace(bar_timestamp=None), "directional_options")
        assert result == {
            "rejected": True,
            "reason": bridge.feed_refusal,
            "code": WAITING_FOR_FEED,
        }
        assert bridge.open_structure_id is None, "no structure, no legs, no fill"

    def test_the_refusal_repeats_but_journals_once(self):
        """A blind book sees a bar every minute. The journal is edge-triggered
        so it writes one row; the *refusal* must keep coming back, or a standing
        refusal would be indistinguishable from "no signal today"."""
        bridge = _bridge()
        view = SimpleNamespace(bar_timestamp=None)
        first = bridge.on_intent({}, view, "directional_options")
        assert WAITING_FOR_FEED in bridge._halted_codes
        assert bridge._journal_halt_once(WAITING_FOR_FEED, "again") is False
        for _ in range(3):
            assert bridge.on_intent({}, view, "directional_options") == first

    def test_exits_keep_running_while_the_book_is_blind(self, monkeypatch):
        """The refusal sits AFTER the open-structure branch on purpose."""
        bridge = _bridge()
        sentinel = {"managed": True}
        monkeypatch.setattr(bridge, "_has_open_structure", lambda: True)
        monkeypatch.setattr(bridge, "on_market_view", lambda view, name: sentinel)
        assert bridge.on_intent({}, object(), "directional_options") is sentinel

    def test_a_connected_book_is_not_refused(self):
        """The gate keys off the stub, not off "is this an option runner"."""
        from backtest.options.quote_providers import SyntheticQuoteProvider

        bridge = _bridge(SyntheticQuoteProvider(_synthetic_generator()))
        assert bridge.feed_disconnected is False
        assert bridge.feed_refusal is None
        assert bridge._disconnected_entry_refusal() is None


def _synthetic_generator():
    """A generator for the control case only — built through the policy door.

    The suite's conftest enables synthetic; this fixture-forced profile does not,
    so the control bridge is constructed under a temporarily-enabled policy.
    """
    sources_policy.reset_source_policy()
    sources_policy._POLICY = sources_policy.build_policy(profile="testing")
    try:
        from backtest.options.quote_providers import SyntheticChainGenerator

        return SyntheticChainGenerator()
    finally:
        sources_policy.reset_source_policy()
        sources_policy._POLICY = sources_policy.build_policy(profile="default")


# ---------------------------------------------------------------------------
# 2. The runner: built, labelled, holding no refcount
# ---------------------------------------------------------------------------


class TestRunnerBuildsOnTheStub:
    def test_the_book_is_built_instead_of_parked(self, logged_out):
        runner = _runner(_option_config())
        try:
            assert runner.options_bridge is not None
            assert runner.feed_state == FEED_STATE_WAITING
            assert runner._chain_source == "disconnected"
            assert runner.options_bridge.quote_provider.source_name == "disconnected:mstock"
            assert runner.options_bridge.feed_disconnected is True
        finally:
            runner.stop()

    def test_the_stub_takes_no_chain_bus_entry(self, logged_out):
        """So ``stop()`` must not release one — a release against a refcount of
        zero is either a KeyError or, worse, someone else's subscription."""
        runner = _runner(_option_config())
        try:
            assert runner._chain_released is True, "nothing was acquired"
            bus = get_chain_bus()
            assert bus.generator_count() == 0
            assert bus.subscriber_count("NIFTY", source="mstock") == 0
            assert bus.subscriber_count("NIFTY") == 0, "no synthetic entry either"
        finally:
            runner.stop()
        bus = get_chain_bus()
        assert bus.generator_count() == 0
        assert bus.subscriber_count("NIFTY", source="mstock") == 0

    def test_get_state_carries_the_feed_state_for_the_ui_pill(self, logged_out):
        runner = _runner(_option_config())
        try:
            state = runner.get_state()
            assert state["feed_state"] == FEED_STATE_WAITING
            assert state["feed_disconnected"] is True
            assert "mstock" in state["feed_refusal"]
        finally:
            runner.stop()

    def test_a_live_book_still_refuses_to_be_priced_by_nothing(self, logged_out):
        """A1 keeps a *paper* book visible. A live gateway executing against legs
        nobody priced is not the same trade-off, so mode='live' keeps refusing."""
        with pytest.raises(ValueError, match="unpriced WAITING_FOR_FEED chain"):
            _runner(_option_config(name="OPT-LIVE", mode="live"))

    def test_an_equity_runner_is_unaffected(self, logged_out):
        """The bar feed is the manager's poll thread, not a broker chain — an
        equity book has nothing to wait for and must not claim it does."""
        config = RunnerConfig(
            name="EQ-1",
            strategy_name="sma_crossover",
            allocated_capital=100_000,
            symbols=["RELIANCE"],
            timeframe="1day",
            mode="paper",
            source="mstock",
        )
        runner = _runner(config)
        try:
            assert runner.options_bridge is None
            assert runner.feed_state == FEED_STATE_LIVE
            assert runner.get_state()["feed_disconnected"] is False
        finally:
            runner.stop()


# ---------------------------------------------------------------------------
# 3. The hot swap: logging in must wake the book without a restart
# ---------------------------------------------------------------------------


class TestLoginHotSwap:
    def _fake_broker(self):
        from datetime import date, timedelta

        from backtest.instruments.base import OptionType
        from backtest.instruments.option import OptionContract
        from decimal import Decimal

        expiry = date.today() + timedelta(days=30)
        contracts = [
            OptionContract(
                instrument_token=f"TOK-NIFTY-{strike}-{kind.value}",
                trading_symbol=f"NIFTY{expiry.strftime('%y%m')}{strike}{kind.value}",
                underlying="NIFTY",
                expiry=expiry,
                strike=Decimal(strike),
                option_type=kind,
                lot_size=75,
                metadata={},
            )
            for strike in (24_800, 24_850)
            for kind in (OptionType.CE, OptionType.PE)
        ]
        quotes = {
            c.instrument_token: {"ltp": 112.5, "bid": 111.0, "ask": 114.0, "volume": 1, "oi": 1}
            for c in contracts
        }

        class _FakeBroker:
            broker_name = "mstock"

            def get_option_chain(self, underlying):
                return list(contracts)

            def get_option_quote(self, token):
                return quotes.get(token, {"ltp": 0.0})

        return _FakeBroker()

    def test_the_upgrade_is_a_noop_while_still_logged_out(self, logged_out):
        runner = _runner(_option_config())
        try:
            runner._maybe_upgrade_to_live_chain()
            assert runner.feed_state == FEED_STATE_WAITING
            assert runner._chain_source == "disconnected"
            assert runner.options_bridge.quote_provider.source_name == "disconnected:mstock"
        finally:
            runner.stop()

    def test_a_login_swaps_the_stub_for_the_live_chain(self, logged_out, monkeypatch):
        import backtest.forward.feed_registry as fr

        broker = self._fake_broker()
        runner = _runner(_option_config())
        try:
            monkeypatch.setattr(fr, "_default_quote_broker", lambda source=None: broker)
            runner._maybe_upgrade_to_live_chain()
            assert runner.feed_state == FEED_STATE_LIVE
            assert runner._chain_source == "mstock"
            assert runner._chain_released is False, "stop() now owns a real entry"
            provider = runner.options_bridge.quote_provider
            assert provider.source_name == "live:mstock+bidask"
            assert runner.options_bridge.feed_disconnected is False
            assert get_chain_bus().subscriber_count("NIFTY", source="mstock") == 1
        finally:
            runner.stop()
        assert get_chain_bus().subscriber_count("NIFTY", source="mstock") == 0

    def test_a_blind_book_with_an_open_structure_still_upgrades(
        self, logged_out, monkeypatch
    ):
        """The opposite of the synthetic→live rule, and the reason the stub
        exists: those legs were opened on the REAL chain before the session
        dropped, so they carry the venue's own tokens. Refusing the swap here
        would mean never re-pricing an open position — going blind permanently."""
        import backtest.forward.feed_registry as fr

        broker = self._fake_broker()
        runner = _runner(_option_config())
        try:
            monkeypatch.setattr(runner.options_bridge, "_has_open_structure", lambda: True)
            monkeypatch.setattr(fr, "_default_quote_broker", lambda source=None: broker)
            runner._maybe_upgrade_to_live_chain()
            assert runner.feed_state == FEED_STATE_LIVE
            assert runner.options_bridge.quote_provider.source_name == "live:mstock+bidask"
        finally:
            runner.stop()


# ---------------------------------------------------------------------------
# 4. The manager: restore end-to-end, and one bad runner cannot eat the rest
# ---------------------------------------------------------------------------


def _manager(state_path) -> PortfolioManager:
    return PortfolioManager(
        risk_config=GlobalRiskConfig(daily_loss_limit=1_000_000, max_drawdown_pct=0.9),
        tick_seconds=1.0,
        warmup_bars=5,
        auto_start_feed=False,
        state_path=str(state_path),
        persist_trades=False,
    )


class TestManagerRestore:
    def test_a_book_saved_by_an_earlier_process_restores_blind_not_parked(
        self, tmp_path, logged_out
    ):
        state_file = tmp_path / "state.json"
        first = _manager(state_file)
        try:
            first.add_runner(_option_config("SAVED-1"), start=False)
            first.add_runner(_option_config("SAVED-2"), start=False)
        finally:
            first.shutdown()

        second = _manager(state_file)
        try:
            runners = list(second._runners.values())
            assert sorted(r.config.name for r in runners) == ["SAVED-1", "SAVED-2"], (
                "both books are back on screen"
            )
            assert second._parked_snapshots == {}, "nothing was filed out of sight"
            for runner in runners:
                assert runner.feed_state == FEED_STATE_WAITING
                assert runner.options_bridge.feed_disconnected is True
        finally:
            second.shutdown()

    def test_a_position_row_says_frozen_rather_than_just_stale(
        self, tmp_path, logged_out, monkeypatch
    ):
        """``stale`` already meant "runner is not RUNNING". A row on a blind book
        is a different situation with a different fix — log the broker in — so
        the row carries ``feed_state`` and the UI can say which."""
        manager = _manager(tmp_path / "state.json")
        try:
            manager.add_runner(_option_config("ROW-1"), start=False)
            runner = next(iter(manager._runners.values()))
            monkeypatch.setattr(
                runner,
                "positions_detail",
                lambda: [{"position_key": "LEG-1", "symbol": "NIFTY25000CE", "qty": 75}],
            )
            rows = manager.list_positions()
            assert len(rows) == 1
            assert rows[0]["feed_state"] == FEED_STATE_WAITING
            assert rows[0]["stale"] is True, "both flags, not one instead of the other"
        finally:
            manager.shutdown()

    def test_the_sse_payload_the_browser_renders_carries_the_feed_state(
        self, tmp_path, logged_out
    ):
        """End of the wire: ``/api/portfolio/stream`` sends
        ``get_portfolio_summary()`` verbatim, and ``portfolio.js`` reads
        ``r.feed_state`` / ``r.feed_refusal`` off it to draw the pill. If the
        summary projected fields instead of passing ``get_state()`` through, the
        pill would silently never appear."""
        manager = _manager(tmp_path / "state.json")
        try:
            manager.add_runner(_option_config("SSE-1"), start=False)
            runners = manager.get_portfolio_summary()["runners"]
            row = next(r for r in runners if r["name"] == "SSE-1")
            assert row["feed_state"] == FEED_STATE_WAITING
            assert row["feed_disconnected"] is True
            assert "mstock" in row["feed_refusal"]
        finally:
            manager.shutdown()

    def test_a_refused_live_runner_does_not_abort_the_restore(self, tmp_path, monkeypatch):
        """``BrokerSessionExpired`` is a ``RuntimeError``. The restore loop used
        to catch only ``(ValueError, KeyError)``, so one live runner whose broker
        had logged out escaped the handler, blew through the loop, and took every
        *paper* runner after it plus all the manager scalars — all reported as
        "starting clean"."""
        from backtest.brokers.execution_router import BrokerSessionExpired
        from backtest.forward.state_store import STATE_SCHEMA_VERSION

        state_file = tmp_path / "state.json"
        writer = _manager(state_file)
        try:
            writer.add_runner(_option_config("PAPER-AFTER"), start=False)
        finally:
            writer.shutdown()

        import json

        payload = json.loads(state_file.read_text())
        live_snap = {
            "config": dict(
                config_to_dict(_option_config("LIVE-FIRST", mode="live")),
                instance_id="live-first",
            )
        }
        payload["runners"] = [live_snap] + payload["runners"]
        payload["schema"] = STATE_SCHEMA_VERSION
        state_file.write_text(json.dumps(payload))

        def _refuse(self, execution_broker=None):
            raise BrokerSessionExpired("mstock")

        monkeypatch.setattr(PortfolioManager, "_live_gateway_for", _refuse)

        restored = _manager(state_file)
        try:
            names = [runner.config.name for runner in restored._runners.values()]
            assert names == ["PAPER-AFTER"], "the paper runner after it survived"
            assert list(restored._parked_snapshots) == ["live-first"]
        finally:
            restored.shutdown()

    def test_a_synthetic_named_book_in_an_old_state_file_still_refuses(self, tmp_path):
        """A1 widens the door for a *broker* runner with no session. It must not
        widen it for a snapshot that names synthetic outright — that is the
        kill-switch, and it still has no fallback."""
        import json

        from backtest.forward.state_store import STATE_SCHEMA_VERSION

        state_file = tmp_path / "state.json"
        payload = {
            "schema": STATE_SCHEMA_VERSION,
            "runners": [
                {
                    "config": dict(
                        config_to_dict(_option_config("LEGACY", source="synthetic")),
                        instance_id="legacy-1",
                    )
                }
            ],
            "manager": {},
        }
        state_file.write_text(json.dumps(payload))

        manager = _manager(state_file)
        try:
            assert list(manager._runners.values()) == []
            assert list(manager._parked_snapshots) == ["legacy-1"]
        finally:
            manager.shutdown()
