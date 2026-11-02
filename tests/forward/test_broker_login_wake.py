"""Sprint A2 — a broker login wakes the books that were waiting on it.

A1 made an unauthenticated option book *visible*: it builds on a frozen-mark
stub, reports ``WAITING_FOR_FEED``, and refuses new entries. That is half the
fix. The other half is what happens when the operator finally logs in — and
before this ticket the answer was "nothing, until the next restart". The book
sat on the stub for the rest of the session, still refusing entries, while the
broker it needed was authenticated and idle. Snapshots that had been *parked*
at restore (a live runner whose gateway refused, a legacy synthetic-named
source) were worse: they were not even on screen.

So every path that makes a session valid now tells the portfolio:

* ``verify_totp`` success — the fast path, with a human watching the screen;
* the monitor loop's unauthenticated→authenticated **transition** — covers a
  session that came back without a TOTP (a renewed token, a remembered session
  restored after the portfolio was already built);
* ``_restore_remembered_sessions`` — a no-op in today's bootstrap order, wired
  so the rule survives a reordering instead of depending on one.

Two constraints shape the code and are pinned below. The wake acquires an
option chain, which is a broker round-trip, so it must not run inline on the
login request; and it must not hold ``PortfolioManager._lock`` across that
round-trip, or every SSE poll and control action stalls behind a TOTP.

Isolation: the stub only exists with synthetic OFF, so these tests force the
shipped ``default`` profile (the suite's conftest turns synthetic ON), and the
process-wide chain bus is reset around each one.
"""

from __future__ import annotations

import threading

import pytest

import backtest.forward.portfolio_manager as pm_module
import backtest.forward.state_store as sm_state_store
from backtest.brokers.base import BrokerAuthBase
from backtest.brokers.session_manager import BrokerSessionManager
from backtest.data import sources_policy
from backtest.forward.feed_registry import get_chain_bus, reset_data_bus
from backtest.forward.paper_runner import (
    FEED_STATE_LIVE,
    FEED_STATE_WAITING,
    STATUS_PAUSED,
    STATUS_RUNNING,
    OrderLedger,
    PaperBroker,
    RunnerConfig,
    StrategyRunner,
)
from backtest.forward.portfolio_manager import PortfolioManager
from backtest.forward.risk_supervisor import GlobalRiskConfig
from backtest.forward.state_store import capture_runner

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
    """Synthetic OFF (the shipped profile) and a fresh process-wide chain bus."""
    monkeypatch.delenv(sources_policy.PROFILE_ENV, raising=False)
    reset_data_bus()
    sources_policy.reset_source_policy()
    monkeypatch.setattr(sources_policy, "_POLICY", sources_policy.build_policy(profile="default"))
    assert sources_policy.synthetic_enabled() is False
    yield
    reset_data_bus()
    sources_policy.reset_source_policy()


@pytest.fixture(autouse=True)
def no_wake_in_flight():
    """The in-flight set is module state; a leaked key would skip a later wake."""
    import backtest.brokers.session_manager as sm

    sm._WAKE_INFLIGHT.clear()
    yield
    sm._WAKE_INFLIGHT.clear()


@pytest.fixture()
def logged_out(monkeypatch):
    """No broker session anywhere — the state the 2026-11-02 boot was in."""
    import backtest.forward.feed_registry as fr

    monkeypatch.setattr(fr, "_default_quote_broker", lambda source=None: None)


def _fake_broker(name: str = "mstock"):
    """A broker that can actually answer an option chain, so the wake is real."""
    from datetime import date, timedelta
    from decimal import Decimal

    from backtest.instruments.base import OptionType
    from backtest.instruments.option import OptionContract

    expiry = date.today() + timedelta(days=30)
    contracts = [
        OptionContract(
            instrument_token=f"TOK-{name.upper()}-{strike}-{kind.value}",
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
        broker_name = name

        def get_option_chain(self, underlying):
            return list(contracts)

        def get_option_quote(self, token):
            return quotes.get(token, {"ltp": 0.0})

    return _FakeBroker()


@pytest.fixture()
def authenticate(monkeypatch):
    """Install a working broker session *now* — after the books were built blind.

    A fixture that was already logged in at setup would prove nothing here:
    ``add_runner`` would resolve the live chain directly and there would be no
    stub to wake. The wake is a *transition*, so the test has to make one.
    """
    import backtest.forward.feed_registry as fr

    def _login(name: str = "mstock"):
        broker = _fake_broker(name)
        monkeypatch.setattr(
            fr,
            "_default_quote_broker",
            lambda source=None: broker if source in (None, name) else None,
        )
        return broker

    return _login


def _option_config(name: str = "OPT-WAKE", **overrides) -> RunnerConfig:
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


def _equity_config(name: str = "EQ-WAKE") -> RunnerConfig:
    return RunnerConfig(
        name=name,
        strategy_name="sma_crossover",
        allocated_capital=100_000,
        symbols=["RELIANCE"],
        timeframe="1day",
        mode="paper",
        source="mstock",
    )


def _runner(config: RunnerConfig) -> StrategyRunner:
    ledger = OrderLedger()
    return StrategyRunner(config, ledger=ledger, broker=PaperBroker(ledger))


def _manager(state_path) -> PortfolioManager:
    return PortfolioManager(
        risk_config=GlobalRiskConfig(daily_loss_limit=1_000_000, max_drawdown_pct=0.9),
        tick_seconds=1.0,
        warmup_bars=5,
        auto_start_feed=False,
        state_path=str(state_path),
        persist_trades=False,
    )


def _plant_parked(manager: PortfolioManager, config: RunnerConfig, status: str = STATUS_RUNNING):
    """Put a snapshot in ``_parked_snapshots`` the way a refused restore would.

    The *reason* a boot refused is not this hook's business — a live gateway
    with no session, a retired strategy name, a legacy synthetic source all
    land in the same dict. What matters is that a parked entry naming this
    broker gets re-created when the broker arrives.
    """
    rid = manager.add_runner(config, start=False)
    runner = manager._runners[rid]
    snapshot = capture_runner(runner)
    snapshot["status"] = status
    manager.remove_runner(rid)
    manager._parked_snapshots[snapshot["config"]["instance_id"]] = snapshot
    return snapshot


# ---------------------------------------------------------------------------
# 1. The hook: what a login does to the books already on screen
# ---------------------------------------------------------------------------


class TestWakesConstructedBooks:
    def test_a_login_wakes_every_book_waiting_on_that_broker(
        self, tmp_path, logged_out, authenticate, monkeypatch
    ):
        marks: list[tuple[str, bool]] = []
        manager = _manager(tmp_path / "state.json")
        try:
            manager.add_runner(_option_config("WAKE-1"), start=False)
            manager.add_runner(_option_config("WAKE-2"), start=False)
            for runner in manager._runners.values():
                assert runner.feed_state == FEED_STATE_WAITING, "precondition: both blind"
            monkeypatch.setattr(
                manager,
                "_repair_runner_marks_internal",
                lambda runner, *, force, **kw: marks.append((runner.config.name, force)) or {},
            )
            authenticate()

            result = manager.on_broker_authenticated("mstock")

            assert sorted(result["woken"]) == sorted(manager._runners.keys())
            assert result["still_waiting"] == []
            assert result["unparked"] == []
            for runner in manager._runners.values():
                assert runner.feed_state == FEED_STATE_LIVE
                assert runner._chain_source == "mstock"
                assert runner.options_bridge.quote_provider.source_name.startswith("live:")
            assert sorted(marks) == [("WAKE-1", True), ("WAKE-2", True)], (
                "the marks were frozen — a woken book must be re-priced at once, "
                "not at the next timer tick"
            )
            assert result["risk_evaluated"] is True
        finally:
            manager.shutdown()

    def test_the_broker_key_is_matched_not_just_any_login(
        self, tmp_path, logged_out, authenticate
    ):
        """A Dhan login must not claim to have woken an mStock book. Matching on
        the wrong broker would report ``LIVE`` for a book still on the stub."""
        manager = _manager(tmp_path / "state.json")
        try:
            manager.add_runner(_option_config("MSTOCK-1"), start=False)
            manager.add_runner(_option_config("DHAN-1", source="dhan"), start=False)
            authenticate()  # mStock only — Dhan still has no session

            result = manager.on_broker_authenticated("dhan")

            by_name = {r.config.name: r for r in manager._runners.values()}
            assert result["woken"] == []
            assert len(result["still_waiting"]) == 1, "the Dhan book was asked and stayed blind"
            assert by_name["MSTOCK-1"].feed_state == FEED_STATE_WAITING, (
                "an mStock book is not a Dhan login's business"
            )
            assert by_name["DHAN-1"].feed_state == FEED_STATE_WAITING
            assert result["risk_evaluated"] is False, "nothing woke, nothing to re-run"
        finally:
            manager.shutdown()

    def test_the_execution_venue_counts_as_waiting_on_the_broker(self, tmp_path, logged_out):
        """A book priced off one broker but routed through another is waiting on
        both — its orders cannot go out, which is the more urgent half."""
        manager = _manager(tmp_path / "state.json")
        try:
            config = _option_config("VENUE-1", source="upstox", execution_broker="mstock")
            manager.add_runner(config, start=False)
            runner = next(iter(manager._runners.values()))
            assert runner.feed_state == FEED_STATE_WAITING
            calls: list[str] = []
            runner._maybe_upgrade_to_live_chain = lambda: calls.append("swap")
            manager.on_broker_authenticated("mstock")
            assert calls == ["swap"], "the execution venue is a reason to wake the book"
        finally:
            manager.shutdown()

    def test_an_equity_book_is_never_a_candidate(self, tmp_path, logged_out):
        manager = _manager(tmp_path / "state.json")
        try:
            manager.add_runner(_equity_config(), start=False)
            runner = next(iter(manager._runners.values()))
            assert runner.feed_state == FEED_STATE_LIVE
            result = manager.on_broker_authenticated("mstock")
            assert result["woken"] == [] and result["still_waiting"] == []
            assert result["marked"] == 0 and result["risk_evaluated"] is False
        finally:
            manager.shutdown()

    def test_a_login_that_wakes_nothing_runs_no_risk_pass(self, tmp_path, monkeypatch):
        """The risk pass moves halt latches. Firing it on a login that changed
        nothing would be a spurious re-evaluation of every fuse in the book."""
        manager = _manager(tmp_path / "state.json")
        try:
            def _boom():
                raise AssertionError("risk pass ran with nothing to re-price")

            monkeypatch.setattr(manager, "_evaluate_risk", _boom)
            result = manager.on_broker_authenticated("mstock")
            assert result == {
                "broker": "mstock",
                "woken": [],
                "unparked": [],
                "still_waiting": [],
                "marked": 0,
                "risk_evaluated": False,
            }
        finally:
            manager.shutdown()

    def test_an_empty_broker_name_is_ignored(self, tmp_path):
        manager = _manager(tmp_path / "state.json")
        try:
            assert manager.on_broker_authenticated("")["broker"] == ""
            assert manager.on_broker_authenticated(None)["broker"] == ""
        finally:
            manager.shutdown()


# ---------------------------------------------------------------------------
# 2. The hook: parked snapshots come back on screen
# ---------------------------------------------------------------------------


class TestUnparksRefusedSnapshots:
    def test_a_parked_snapshot_is_re_created_when_its_broker_arrives(
        self, tmp_path, logged_out, authenticate
    ):
        manager = _manager(tmp_path / "state.json")
        try:
            snapshot = _plant_parked(manager, _option_config("PARKED-1"))
            park_key = snapshot["config"]["instance_id"]
            assert list(manager._parked_snapshots) == [park_key]
            assert manager._runners == {}, "precondition: not on screen"
            authenticate()

            result = manager.on_broker_authenticated("mstock")

            assert result["unparked"] == [park_key]
            assert manager._parked_snapshots == {}, "the park entry must not be re-saved"
            runner = manager._runners[park_key]
            assert runner.config.name == "PARKED-1"
            assert runner.feed_state == FEED_STATE_LIVE, "built on the live chain, not the stub"
            assert result["risk_evaluated"] is True
        finally:
            manager.shutdown()

    def test_an_unparked_book_comes_back_paused_not_running(
        self, tmp_path, logged_out, authenticate
    ):
        """Waking the feed is not the operator's intent to trade. ``restore_runner``
        already fails RUNNING → PAUSED closed; a login must not undo that."""
        manager = _manager(tmp_path / "state.json")
        try:
            snapshot = _plant_parked(
                manager, _option_config("PARKED-RUN"), status=STATUS_RUNNING
            )
            authenticate()
            manager.on_broker_authenticated("mstock")
            runner = manager._runners[snapshot["config"]["instance_id"]]
            assert runner.status == STATUS_PAUSED
        finally:
            manager.shutdown()

    def test_a_parked_snapshot_for_another_broker_stays_parked(
        self, tmp_path, logged_out, authenticate
    ):
        manager = _manager(tmp_path / "state.json")
        try:
            mine = _plant_parked(manager, _option_config("MINE"))
            theirs = _plant_parked(manager, _option_config("THEIRS", source="dhan"))
            authenticate()
            result = manager.on_broker_authenticated("mstock")
            assert result["unparked"] == [mine["config"]["instance_id"]]
            assert list(manager._parked_snapshots) == [theirs["config"]["instance_id"]], (
                "a Dhan book must stay parked until Dhan logs in — unparking it "
                "would re-create it on a stub and silently change its feed_state"
            )
        finally:
            manager.shutdown()

    def test_a_snapshot_that_still_refuses_stays_parked_and_does_not_raise(
        self, tmp_path, logged_out, authenticate, monkeypatch
    ):
        """The reason for the refusal may have nothing to do with the session (a
        retired strategy name). Such a snapshot must survive the login intact."""
        manager = _manager(tmp_path / "state.json")
        try:
            snapshot = _plant_parked(manager, _option_config("STUCK-1"))
            park_key = snapshot["config"]["instance_id"]
            authenticate()

            def _refuse(config, start=True):
                raise KeyError("directional_options")

            monkeypatch.setattr(manager, "add_runner", _refuse)
            result = manager.on_broker_authenticated("mstock")
            assert result["unparked"] == []
            assert list(manager._parked_snapshots) == [park_key], "kept for the next boot"
        finally:
            manager.shutdown()

    def test_a_snapshot_that_cannot_be_rehydrated_does_not_break_the_login(
        self, tmp_path, logged_out, authenticate, monkeypatch
    ):
        """``restore_runner`` reads a lot of optional shape out of the snapshot.
        A corrupt one must cost that book, not the login that woke the others."""
        manager = _manager(tmp_path / "state.json")
        try:
            good = _plant_parked(manager, _option_config("GOOD"))
            bad = _plant_parked(manager, _option_config("BAD"))
            authenticate()

            real_restore = sm_state_store.restore_runner

            def _restore(runner, snapshot):
                if snapshot["config"]["name"] == "BAD":
                    raise ValueError("corrupt snapshot")
                return real_restore(runner, snapshot)

            monkeypatch.setattr(pm_module, "restore_runner", _restore)
            result = manager.on_broker_authenticated("mstock")
            assert result["unparked"] == [good["config"]["instance_id"]]
            assert list(manager._parked_snapshots) == [bad["config"]["instance_id"]], (
                "the un-rehydratable book stays parked rather than half-built"
            )
            names = [r.config.name for r in manager._runners.values()]
            assert names == ["GOOD"], "and is not left on screen in a broken state"
        finally:
            manager.shutdown()


# ---------------------------------------------------------------------------
# 3. The hook must be safe to call from a login path
# ---------------------------------------------------------------------------


class TestTheHookIsSafe:
    def test_a_book_that_fails_to_swap_does_not_stop_the_others(
        self, tmp_path, logged_out, authenticate, monkeypatch
    ):
        manager = _manager(tmp_path / "state.json")
        try:
            manager.add_runner(_option_config("BAD"), start=False)
            manager.add_runner(_option_config("GOOD"), start=False)
            by_name = {r.config.name: r for r in manager._runners.values()}

            def _explode():
                raise RuntimeError("chain acquire blew up")

            monkeypatch.setattr(by_name["BAD"], "_maybe_upgrade_to_live_chain", _explode)
            authenticate()
            result = manager.on_broker_authenticated("mstock")
            assert len(result["still_waiting"]) == 1, "reported, not swallowed"
            assert len(result["woken"]) == 1
            assert by_name["GOOD"].feed_state == FEED_STATE_LIVE
            assert by_name["BAD"].feed_state == FEED_STATE_WAITING, "still honest"
        finally:
            manager.shutdown()

    def test_a_failing_risk_pass_does_not_fail_the_login(
        self, tmp_path, logged_out, authenticate, monkeypatch
    ):
        manager = _manager(tmp_path / "state.json")
        try:
            manager.add_runner(_option_config("WAKE-1"), start=False)

            def _boom():
                raise RuntimeError("risk supervisor down")

            monkeypatch.setattr(manager, "_evaluate_risk", _boom)
            authenticate()
            result = manager.on_broker_authenticated("mstock")
            assert len(result["woken"]) == 1, "the book still woke"
            assert result["risk_evaluated"] is False
        finally:
            manager.shutdown()

    def test_the_manager_lock_is_not_held_across_the_chain_acquire(
        self, tmp_path, logged_out, monkeypatch
    ):
        """The wake is a broker round-trip. Holding ``_lock`` across it would
        stall every SSE poll, position row and control action behind a TOTP."""
        manager = _manager(tmp_path / "state.json")
        try:
            manager.add_runner(_option_config("LOCK-1"), start=False)
            runner = next(iter(manager._runners.values()))
            reached = threading.Event()
            release = threading.Event()

            def _slow_swap():
                reached.set()
                assert release.wait(5.0), "test harness stalled"

            monkeypatch.setattr(runner, "_maybe_upgrade_to_live_chain", _slow_swap)
            worker = threading.Thread(
                target=manager.on_broker_authenticated, args=("mstock",), daemon=True
            )
            worker.start()
            assert reached.wait(5.0), "the wake never reached the chain acquire"

            got_lock = threading.Event()

            def _take_lock():
                with manager._lock:
                    got_lock.set()

            probe = threading.Thread(target=_take_lock, daemon=True)
            probe.start()
            probe.join(2.0)
            try:
                assert got_lock.is_set(), (
                    "the manager lock was held across a broker round-trip"
                )
            finally:
                release.set()
                worker.join(5.0)
        finally:
            manager.shutdown()

    def test_a_wake_leaves_the_chain_bus_with_only_the_live_entries(
        self, tmp_path, logged_out, authenticate
    ):
        """The stub takes no bus entry, so a wake must end with exactly one
        generator per woken book — and zero after shutdown (no refcount leak)."""
        manager = _manager(tmp_path / "state.json")
        try:
            manager.add_runner(_option_config("BUS-1"), start=False)
            assert get_chain_bus().generator_count() == 0, "the stub is not an entry"
            authenticate()
            manager.on_broker_authenticated("mstock")
            assert get_chain_bus().generator_count() == 1
        finally:
            manager.shutdown()
        assert get_chain_bus().generator_count() == 0


# ---------------------------------------------------------------------------
# 4. The wiring: which session-manager events fire the wake
# ---------------------------------------------------------------------------


class _AuthBroker(BrokerAuthBase):
    """Scriptable broker: enough surface for login / TOTP / the monitor poll."""

    broker_name = "mstock"
    broker_display_name = "mStock"

    def __init__(self, totp_ok: bool = True):
        self.totp_ok = totp_ok
        self.authenticated = False
        self.status_override: str | None = None
        self._expires_at = "2099-01-01T00:00:00"

    def login(self, username, password):
        return {"success": True, "requires_totp": True}

    def verify_totp(self, totp_code):
        if not self.totp_ok:
            return {"success": False, "message": "bad code"}
        self.authenticated = True
        return {"success": True, "expires_at": self._expires_at}

    def get_session_status(self):
        return {
            "status": self.status_override
            or ("authenticated" if self.authenticated else "unauthenticated"),
            "expires_at": self._expires_at,
            "broker": self.broker_name,
        }

    def get_session_token(self):
        return "tok" if self.authenticated else None

    def restore_session(self, token, expires_at):
        self.authenticated = True

    def logout(self):
        self.authenticated = False


@pytest.fixture()
def wakes(monkeypatch):
    """Record the wake calls instead of spawning threads."""
    import backtest.brokers.session_manager as sm

    seen: list[str] = []
    monkeypatch.setattr(sm, "_notify_portfolio_authenticated", seen.append)
    return seen


def _session_manager(broker) -> BrokerSessionManager:
    # ``set_broker``, not just the factory: ``verify_totp(broker_name=...)`` goes
    # through ``_broker_for``, which builds from the REGISTRY and would hand the
    # test a real MStockBroker instead of the scriptable one.
    manager = BrokerSessionManager(broker_factory=lambda: broker)
    manager.set_broker(broker)
    return manager


class TestSessionManagerWiring:
    def test_a_successful_totp_fires_the_wake(self, wakes):
        manager = _session_manager(_AuthBroker())
        result = manager.verify_totp("123456", broker_name="mstock")
        assert result["success"] is True
        assert wakes == ["mstock"]

    def test_a_failed_totp_does_not(self, wakes):
        manager = _session_manager(_AuthBroker(totp_ok=False))
        assert manager.verify_totp("000000", broker_name="mstock")["success"] is False
        assert wakes == [], "a rejected code established no session"

    def test_the_monitor_wakes_on_the_transition_only(self, wakes):
        """Every tick is authenticated once a session exists. Waking each tick
        would re-acquire the option chain every 30 seconds forever."""
        manager = _session_manager(_AuthBroker())
        manager._poll_once()
        assert wakes == [], "still unauthenticated"
        manager._sessions["mstock"].authenticated = True
        manager._poll_once()
        assert wakes == ["mstock"], "the unauthenticated→authenticated edge"
        manager._poll_once()
        manager._poll_once()
        assert wakes == ["mstock"], "not re-fired on the steady state"

    def test_a_recovery_after_expiry_wakes_again(self, wakes):
        """Expiry is not an authentication, but recovering from one is."""
        from backtest.brokers.base import STATUS_EXPIRED

        manager = _session_manager(_AuthBroker())
        broker = manager._sessions["mstock"]
        broker.authenticated = True
        manager._poll_once()
        assert wakes == ["mstock"], "the first authenticated edge"
        manager._poll_once()
        assert wakes == ["mstock"], "steady state fires nothing"

        broker.authenticated = False
        broker.status_override = STATUS_EXPIRED
        manager._poll_once()
        assert wakes == ["mstock"], "expiry clears the token; it authenticates nothing"

        broker.status_override = None
        manager._poll_once()
        assert wakes == ["mstock"], "an unauthenticated broker fires nothing either"

        broker.authenticated = True
        manager._poll_once()
        assert wakes == ["mstock", "mstock"], "re-authenticating is a new transition"


# ---------------------------------------------------------------------------
# 5. The dispatch itself: off the caller's thread, and never raising
# ---------------------------------------------------------------------------


class TestWakeDispatch:
    def test_the_wake_runs_off_the_calling_thread(self, monkeypatch):
        import backtest.brokers.session_manager as sm

        started = threading.Event()
        release = threading.Event()
        seen: list[str] = []

        class _BlockingManager:
            def on_broker_authenticated(self, key):
                seen.append(threading.current_thread().name)
                started.set()
                assert release.wait(5.0)
                return {"woken": [], "unparked": []}

        monkeypatch.setattr(
            "backtest.forward.portfolio_manager.peek_portfolio_manager",
            lambda: _BlockingManager(),
        )
        caller = threading.current_thread().name
        sm._notify_portfolio_authenticated("mstock")
        try:
            assert started.wait(5.0)
            assert "mstock" in sm._WAKE_INFLIGHT, "marked in flight while it runs"
            assert seen == ["login-wake-mstock"]
            assert seen[0] != caller, "a login response must not wait on a chain acquire"
        finally:
            release.set()
        for _ in range(100):
            if not sm._WAKE_INFLIGHT:
                break
            threading.Event().wait(0.02)
        assert sm._WAKE_INFLIGHT == set(), "the in-flight mark must always clear"

    def test_a_second_wake_for_the_same_broker_is_skipped_not_queued(self, monkeypatch):
        import backtest.brokers.session_manager as sm

        release = threading.Event()
        calls: list[str] = []

        class _SlowManager:
            def on_broker_authenticated(self, key):
                calls.append(key)
                assert release.wait(5.0)
                return {"woken": [], "unparked": []}

        monkeypatch.setattr(
            "backtest.forward.portfolio_manager.peek_portfolio_manager", lambda: _SlowManager()
        )
        sm._notify_portfolio_authenticated("mstock")
        while "mstock" not in sm._WAKE_INFLIGHT:
            threading.Event().wait(0.01)
        sm._notify_portfolio_authenticated("mstock")
        sm._notify_portfolio_authenticated("mstock")
        release.set()
        for _ in range(200):
            if not sm._WAKE_INFLIGHT:
                break
            threading.Event().wait(0.02)
        assert calls == ["mstock"], "the in-flight wake sees the same session"

    def test_no_portfolio_built_means_no_wake_and_no_error(self, monkeypatch):
        import backtest.brokers.session_manager as sm

        monkeypatch.setattr(
            "backtest.forward.portfolio_manager.peek_portfolio_manager", lambda: None
        )
        sm._notify_portfolio_authenticated("mstock")
        for _ in range(200):
            if not sm._WAKE_INFLIGHT:
                break
            threading.Event().wait(0.02)
        assert sm._WAKE_INFLIGHT == set()

    def test_a_wake_that_raises_never_surfaces_on_the_login(self, monkeypatch):
        import backtest.brokers.session_manager as sm

        def _boom():
            raise RuntimeError("portfolio import failed")

        monkeypatch.setattr("backtest.forward.portfolio_manager.peek_portfolio_manager", _boom)
        sm._notify_portfolio_authenticated("mstock")
        for _ in range(200):
            if not sm._WAKE_INFLIGHT:
                break
            threading.Event().wait(0.02)
        assert sm._WAKE_INFLIGHT == set(), "a failed wake must not wedge later ones"

    def test_an_empty_broker_name_spawns_nothing(self):
        import backtest.brokers.session_manager as sm

        sm._notify_portfolio_authenticated("")
        sm._notify_portfolio_authenticated(None)
        assert sm._WAKE_INFLIGHT == set()


def test_peek_does_not_construct_a_manager(monkeypatch):
    """A login must not build the portfolio as a side effect. Constructing one
    restores state, subscribes feeds and books capital — none of which a TOTP
    check has any business triggering on a process that never opened the page."""
    import backtest.forward.portfolio_manager as pm

    monkeypatch.setattr(pm, "_MANAGER", None)
    assert pm.peek_portfolio_manager() is None
    assert pm._MANAGER is None, "peeking built nothing"


# ---------------------------------------------------------------------------
# 6. Sprint A4: the count the login banner is gated on
# ---------------------------------------------------------------------------


def _boot_session_manager(monkeypatch) -> BrokerSessionManager:
    """A session manager in the shape a cold boot has: nothing restored.

    ``__init__`` replays the remembered-session store, which would construct a
    real broker (and its WebSocket feed) from whatever this checkout happens to
    have saved. These tests are about the poll's payload, not about restoring.
    """
    monkeypatch.setattr(
        BrokerSessionManager, "_restore_remembered_sessions", lambda self: 0
    )
    return BrokerSessionManager(broker_factory=lambda: _AuthBroker())


class TestWaitingRunnerCount:
    """/api/broker/status carries "how many books are stuck on this broker".

    The topbar CTA and the auto-opened login popup only appear when a runner
    genuinely needs that broker, so the poll has to say how many do — the client
    has no other way to know, and a second heavier endpoint polled from every
    page would be worse than one integer in the payload it already fetches.
    """

    def test_blind_books_count_against_the_broker_they_need(self, tmp_path, logged_out):
        manager = _manager(tmp_path / "state.json")
        try:
            manager.add_runner(_option_config("COUNT-1"), start=False)
            manager.add_runner(_option_config("COUNT-2"), start=False)

            assert manager.count_runners_waiting_on("mstock") == 2
            assert manager.count_runners_waiting_on("MSTOCK") == 2, "the key is normalized"
            assert manager.count_runners_waiting_on("dhan") == 0
            assert manager.count_runners_waiting_on("") == 0
            assert manager.count_runners_waiting_on(None) == 0
        finally:
            manager.shutdown()

    def test_the_execution_venue_counts_as_waiting_too(self, tmp_path, logged_out):
        """Both sides of the routing count: quotes and orders.

        A book priced off Upstox but routed through mStock is stuck until either
        one arrives — the frozen chain is the quote source, and the entry would
        still be refused on the execution venue. So it is reported against both,
        and only against brokers it actually names.
        """
        manager = _manager(tmp_path / "state.json")
        try:
            manager.add_runner(
                _option_config("ROUTE-1", source="upstox", execution_broker="mstock"),
                start=False,
            )
            assert manager.count_runners_waiting_on("mstock") == 1
            assert manager.count_runners_waiting_on("upstox") == 1
            assert manager.count_runners_waiting_on("dhan") == 0
        finally:
            manager.shutdown()

    def test_a_woken_book_stops_counting(self, tmp_path, logged_out, authenticate):
        manager = _manager(tmp_path / "state.json")
        try:
            manager.add_runner(_option_config("COUNT-3"), start=False)
            assert manager.count_runners_waiting_on("mstock") == 1

            authenticate()
            manager.on_broker_authenticated("mstock")

            assert manager.count_runners_waiting_on("mstock") == 0, (
                "a live book is not waiting — leaving it counted would keep the "
                "red banner up after the login that cleared it"
            )
        finally:
            manager.shutdown()

    def test_a_parked_snapshot_is_not_claimed_as_waiting_on_a_login(
        self, tmp_path, logged_out
    ):
        """Parked means "a restore refused this", for any of a dozen reasons.

        Claiming all of them as broker-shaped would put a red banner up for a
        book that a re-login cannot fix (a retired strategy name, a code change
        at spawn) — and the operator would log in, see the banner still there,
        and stop trusting it.
        """
        manager = _manager(tmp_path / "state.json")
        try:
            _plant_parked(manager, _option_config("PARK-1"))
            assert len(manager._parked_snapshots) == 1
            assert manager.count_runners_waiting_on("mstock") == 0
        finally:
            manager.shutdown()

    def test_the_status_poll_carries_the_count(self, tmp_path, logged_out, monkeypatch):
        manager = _manager(tmp_path / "state.json")
        try:
            manager.add_runner(_option_config("POLL-1"), start=False)
            manager.add_runner(_option_config("POLL-2"), start=False)
            monkeypatch.setattr(pm_module, "_MANAGER", manager)

            sessions = _boot_session_manager(monkeypatch).get_all_status()

            assert sessions["mstock"]["waiting_runners"] == 2
            assert sessions["mstock"]["status"] == "unauthenticated"
            # Every row carries the key, so the client never has to special-case
            # a broker the portfolio has never heard of.
            assert all("waiting_runners" in row for row in sessions.values())
            assert sessions["dhan"]["waiting_runners"] == 0
        finally:
            manager.shutdown()

    def test_the_count_never_builds_a_portfolio(self, monkeypatch):
        """A status poll runs on every open tab — it must not construct the
        singleton (feeds, restore, capital) to answer it."""
        monkeypatch.setattr(pm_module, "_MANAGER", None)

        sessions = _boot_session_manager(monkeypatch).get_all_status()

        assert pm_module._MANAGER is None, "counting built a portfolio"
        assert all(row["waiting_runners"] == 0 for row in sessions.values())

    def test_a_portfolio_that_raises_still_answers_zero(self, monkeypatch):
        class _Broken:
            def count_runners_waiting_on(self, key):
                raise RuntimeError("portfolio on fire")

        monkeypatch.setattr(pm_module, "_MANAGER", _Broken())
        sessions = _boot_session_manager(monkeypatch).get_all_status()
        assert all(row["waiting_runners"] == 0 for row in sessions.values())
