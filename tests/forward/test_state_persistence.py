"""P2.4 — portfolio/runner state persistence (Gap #3, "V2").

The complaint on record: *a restart loses the book and resurrects halted
breakers*. These tests pin the fix end-to-end through the real manager:

* persistence is opt-in (no path → old in-memory behaviour, no file);
* every control-plane mutation snapshots (atomic, tmp+rename);
* a fresh manager on the same path rehydrates: runner configs with identity,
  the FULL equity book (``Portfolio.to_dict``/``from_dict`` — cash, open
  positions, orders, equity history), runtime scalars, bucket breaker
  latches and day anchors;
* RUNNING comes back PAUSED (fail-closed — nothing trades after a restart
  until a human resumes); a tripped breaker STAYS tripped;
* option runners bring their whole bridge book back (open structures, legs,
  broker cash, bar-clock scalars);
* corrupt / wrong-schema files boot clean, never crash;
* the write is atomic — a failure mid-dump leaves the last good file intact.

SQLite-free, thread-free, all fakes/synthetic.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest


from backtest.forward.feed_registry import reset_data_bus
from backtest.forward.portfolio_manager import PortfolioManager
from backtest.forward.paper_runner import SIDE_BUY, SIDE_SELL, RunnerConfig
from backtest.forward.risk_supervisor import GlobalRiskConfig
from backtest.forward.state_store import PortfolioStateStore, STATE_SCHEMA_VERSION
from options_support import research_profile


def _manager(state_path=None) -> PortfolioManager:
    return PortfolioManager(
        risk_config=GlobalRiskConfig(daily_loss_limit=1_000_000, max_drawdown_pct=0.9),
        tick_seconds=1.0,
        warmup_bars=5,
        auto_start_feed=False,
        state_path=state_path,
        # This file's own promise is "SQLite-free". Leaving the seam unset meant
        # keeping it only because the pytest firewall rewrites a live DB URL to
        # ``sqlite:///:memory:`` — the persister still attached the ledger and the
        # boot reap still queried real rows. Ticket 2.4: ask for none of it.
        persist_trades=False,
    )


def _runner_config(name="R1", symbol="RELIANCE", **overrides) -> RunnerConfig:
    fields = dict(
        name=name,
        strategy_name="sma_crossover",
        allocated_capital=100_000,
        symbols=[symbol],
        timeframe="1day",
        mode="paper",
        source="synthetic",
    )
    fields.update(overrides)
    return RunnerConfig(**fields)


def _run_bars(manager, count, start_day=1):
    base = datetime.now(timezone.utc) + timedelta(days=start_day)
    for i in range(count):
        manager.tick(ts=base + timedelta(days=i))


@pytest.fixture(autouse=True)
def _clean_bus():
    reset_data_bus()
    yield
    reset_data_bus()


# ---------------------------------------------------------------------------
# Opt-in semantics
# ---------------------------------------------------------------------------


class TestOptIn:
    def test_no_path_means_no_file_and_old_behaviour(self, tmp_path):
        mgr = _manager()
        mgr.add_runner(_runner_config())
        mgr.shutdown()
        assert list(tmp_path.iterdir()) == [], "nothing written without a path"

    def test_env_var_wires_the_singleton(self, tmp_path, monkeypatch):
        from backtest.forward.portfolio_manager import reset_portfolio_manager

        state_file = tmp_path / "state.json"
        monkeypatch.setenv("PORTFOLIO_STATE_PATH", str(state_file))
        # Opt-in flag: the env default is skipped under pytest (a production
        # snapshot would leak into clean-manager tests); this test opts in.
        monkeypatch.setenv("PORTFOLIO_STATE_PATH_TEST_OPTIN", "1")
        try:
            mgr = reset_portfolio_manager(auto_start_feed=False)
            assert mgr._state_store is not None
            assert mgr._state_store.path == state_file
            mgr.add_runner(_runner_config("ENV-RUNNER"))
            assert state_file.exists(), "singleton persists through the env path"
            assert "ENV-RUNNER" in state_file.read_text()
        finally:
            reset_portfolio_manager(auto_start_feed=False)


# ---------------------------------------------------------------------------
# Save-on-mutation + file shape
# ---------------------------------------------------------------------------


class TestSaveOnMutation:
    def test_add_and_remove_are_snapshotted(self, tmp_path):
        state = tmp_path / "state.json"
        mgr = _manager(state)
        instance_id = mgr.add_runner(_runner_config("KEEP-ME"))
        payload = json.loads(state.read_text())
        assert payload["schema"] == STATE_SCHEMA_VERSION
        assert [r["config"]["name"] for r in payload["runners"]] == ["KEEP-ME"]

        mgr.remove_runner(instance_id)
        payload = json.loads(state.read_text())
        assert payload["runners"] == []

    def test_shutdown_writes_final_state(self, tmp_path):
        state = tmp_path / "state.json"
        mgr = _manager(state)
        mgr.shutdown()
        payload = json.loads(state.read_text())
        assert payload["manager"]["total_capital"] == 0.0

    def test_tick_cadence_persists_scalars(self, tmp_path):
        state = tmp_path / "state.json"
        mgr = _manager(state)
        mgr.add_runner(_runner_config())
        _run_bars(mgr, 61)  # crosses the every-60-ticks save
        payload = json.loads(state.read_text())
        assert payload["manager"]["tick_index"] >= 60
        assert payload["runners"][0]["bars_processed"] >= 60
        mgr.shutdown()


# ---------------------------------------------------------------------------
# The restart cycle — books, breakers, identity
# ---------------------------------------------------------------------------


class TestRestartCycle:
    def test_full_round_trip_configs_books_and_anchors(self, tmp_path):
        state = tmp_path / "state.json"

        # --- session one: run, trade, anchor -----------------------------
        mgr1 = _manager(state)
        mgr1.add_runner(_runner_config("CYCLE"))
        mgr1.feed.warmup()
        _run_bars(mgr1, 10)
        runner1 = mgr1.get_runner(list(mgr1._runners)[0])
        # Far-future date: can never collide with the real wall clock (the
        # bar timestamps are now+1d), so this test is date-independent.
        mgr1._current_day = "2030-01-01"
        runner1._current_day = "2030-01-01"
        mgr1._day_start_equity = float(runner1.portfolio.current_cash)
        mgr1.shutdown()

        # --- session two: restore ----------------------------------------
        mgr2 = _manager(state)
        try:
            assert len(mgr2._runners) == 1
            restored = mgr2.get_runner(list(mgr2._runners)[0])

            # identity + config survive
            assert restored.instance_id == runner1.instance_id
            assert restored.config.name == "CYCLE"
            assert restored.config.allocated_capital == 100_000

            # fail-closed: RUNNING comes back PAUSED
            assert restored.status == "PAUSED"

            # the book survives (cash/equity from the traded session)
            assert float(restored.portfolio.current_cash) == float(
                runner1.portfolio.current_cash
            )
            assert float(restored.portfolio.realized_pnl) == float(
                runner1.portfolio.realized_pnl
            )
            assert restored.portfolio.portfolio_id == runner1.portfolio.portfolio_id
            assert restored.bars_processed == runner1.bars_processed
            # Runner-level day anchor survives; MANAGER-level anchors are
            # session-scoped (re-baselined on the first bar) — the ghost-
            # anchor breaker-trip fix of 2026-09-23.
            assert restored._current_day == "2030-01-01"
            assert mgr2._current_day is None

            # and it can trade again after an explicit resume
            restored.resume()
            _run_bars(mgr2, 3, start_day=50)
            assert restored.bars_processed > runner1.bars_processed
        finally:
            mgr2.shutdown()

    def test_open_position_survives_the_restart(self, tmp_path):
        state = tmp_path / "state.json"
        mgr1 = _manager(state)
        mgr1.add_runner(_runner_config("LONG"))
        mgr1.feed.warmup()
        _run_bars(mgr1, 8)
        runner1 = mgr1.get_runner(list(mgr1._runners)[0])
        # force an open position through the runner's REAL order path
        # (ledger → broker fill → runner.on_fill → portfolio accounting)
        price = runner1.last_price.get("RELIANCE", 100.0)
        runner1.broker.submit_market(
            runner1.instance_id, "RELIANCE", SIDE_BUY, 10, price
        )
        assert runner1.portfolio.positions, "setup: position must be open"
        before = {
            sym: (str(pos.quantity), str(pos.average_entry_price))
            for sym, pos in runner1.portfolio.positions.items()
        }
        mgr1.shutdown()

        mgr2 = _manager(state)
        try:
            restored = mgr2.get_runner(list(mgr2._runners)[0])
            after = {
                sym: (str(pos.quantity), str(pos.average_entry_price))
                for sym, pos in restored.portfolio.positions.items()
            }
            assert after == before, "open positions round-trip exactly"
        finally:
            mgr2.shutdown()

    def test_tripped_breaker_stays_tripped(self, tmp_path, caplog):
        """Ticket B1: on a SAME-DAY IST restart, a tripped breaker stays latched
        until an operator explicitly calls ``reset_circuit_breaker``.
        """
        state = tmp_path / "state.json"
        mgr1 = _manager(state)
        mgr1.add_runner(_runner_config("HALTED"))
        mgr1._bucket_halted["paper"] = True
        mgr1._bucket_halt_reason["paper"] = "daily loss limit breached"
        mgr1._bucket_halt_mode["paper"] = "pause"
        mgr1._bucket_halted_ts["paper"] = "2026-11-03T05:00:00+00:00"
        mgr1.shutdown()

        with caplog.at_level("WARNING"):
            mgr2 = _manager(state)
        try:
            assert mgr2._bucket_halted["paper"] is True
            assert mgr2._bucket_halt_reason["paper"] == "daily loss limit breached"
            assert "same-day session risk peak & breaker latches restored" in caplog.text
            # Scoped resume is refused while the same-day halt latch is active:
            with pytest.raises(RuntimeError, match="halted"):
                mgr2.resume_all("paper")
            # Explicit operator reset clears the latch and allows resume:
            mgr2.reset_circuit_breaker("paper")
            assert mgr2._bucket_halted["paper"] is False
            mgr2.resume_all("paper")
        finally:
            mgr2.shutdown()

    def test_manager_level_halt_survives_same_day_and_clears_next_day(self, tmp_path):
        """Ticket B1: manager-level master kill persists within the same IST
        session, and rolls clean on a new IST session.
        """
        state = tmp_path / "state.json"
        mgr1 = _manager(state)
        mgr1.add_runner(_runner_config("MASTER-HALT"))
        mgr1.halted = True
        mgr1.halt_reason = "master kill"
        mgr1.halt_mode = "flatten"
        mgr1.halted_ts = "2026-11-03T06:00:00+00:00"
        mgr1.shutdown()

        # Same IST session -> stays latched
        mgr_same = _manager(state)
        try:
            assert mgr_same.halted is True
            assert mgr_same.halt_reason == "master kill"
        finally:
            mgr_same.shutdown()

        # Stamp prior IST session date -> rolls clean on morning boot
        raw = json.loads(state.read_text(encoding="utf-8"))
        raw["manager"]["session_date_ist"] = "2026-09-21"
        for b in raw.get("buckets", {}).values():
            b["session_date_ist"] = "2026-09-21"
        state.write_text(json.dumps(raw), encoding="utf-8")

        mgr_next = _manager(state)
        try:
            assert mgr_next.halted is False
            assert mgr_next.halt_reason is None
        finally:
            mgr_next.shutdown()

    def test_prior_day_anchors_and_halts_reset_on_new_ist_session(self, tmp_path, caplog):
        """Ticket B1 (replaces contradictory Sept-23/Oct-15 test):
        Prior-session peaks (`₹51.7L`) and halts are wiped when `session_date_ist`
        differs from today's IST session date, while `_day_start_equity` seeds
        from the restored book's session baseline (`₹1,00,000`) so pre-bar risk
        checks never divide by zero or resurrect ghost losses.
        """
        state = tmp_path / "state.json"
        mgr1 = _manager(state)
        mgr1.add_runner(_runner_config())
        mgr1._risk_session_date_ist = "2026-09-21"
        mgr1._session_peak_daily_pnl = 450_000.0
        mgr1._bucket_session_peak_pnl["paper"] = 23_456.0
        mgr1._bucket_peak["paper"] = 123_456.0
        mgr1._bucket_halted["paper"] = True
        mgr1._bucket_halt_reason["paper"] = "Emergency flatten (yesterday)"
        mgr1._bucket_day_start["paper"] = 99_000.0
        mgr1._bucket_day["paper"] = "2026-09-17"
        mgr1._day_start_equity = 17_300_000.0
        mgr1.peak_equity = 5_174_942.0
        mgr1.halted = True
        mgr1.halt_reason = "Emergency flatten (yesterday)"
        mgr1._current_day = "2026-09-21"
        mgr1.shutdown()

        with caplog.at_level("WARNING"):
            mgr2 = _manager(state)
        try:
            assert mgr2._session_peak_daily_pnl == 0.0
            assert mgr2._bucket_session_peak_pnl["paper"] == 0.0
            assert mgr2._bucket_peak["paper"] == 0.0
            assert mgr2.peak_equity == 0.0
            assert mgr2.halted is False
            assert mgr2._bucket_halted["paper"] is False
            assert mgr2._day_start_equity == pytest.approx(100_000.0)
            assert mgr2._bucket_day_start["paper"] == pytest.approx(100_000.0)
            assert mgr2._bucket_day["paper"] is None
            assert mgr2._current_day is None
            assert (
                "prior-session (2026-09-21) risk peak & breaker latches rolled clean"
                in caplog.text
            )
            mgr2.resume_all("paper")  # must not raise
        finally:
            mgr2.shutdown()

    def test_same_day_restart_preserves_peak_drawdown_and_halt_latch(self, tmp_path):
        """Ticket B1: intraday high-water mark (`session_peak_daily_pnl`) survives
        a mid-day restart and immediately trips `max_drawdown_pct` when the book
        gives back more than the allowed drawdown from morning peak — AND the
        resulting halt flushes to disk immediately without waiting 5 ticks.
        """
        state = tmp_path / "state.json"

        def _tight_mgr() -> PortfolioManager:
            return PortfolioManager(
                risk_config=GlobalRiskConfig(
                    daily_loss_limit=500_000,
                    max_drawdown_pct=0.10,  # 10% trailing intraday peak drawdown
                ),
                tick_seconds=1.0,
                warmup_bars=5,
                auto_start_feed=False,
                state_path=state,
                persist_trades=False,
            )

        mgr1 = _tight_mgr()
        rid = mgr1.add_runner(_runner_config("PEAK-TEST"))
        # Morning rally: buy 800 @ 100, sell 800 @ 125 (~+₹20,000 net peak):
        mgr1.broker.submit_market(rid, "RELIANCE", SIDE_BUY, 800, 100.0)
        mgr1.broker.submit_market(rid, "RELIANCE", SIDE_SELL, 800, 125.0)
        mgr1._evaluate_risk()
        peak_pnl_1 = mgr1._session_peak_daily_pnl
        peak_eq_1 = mgr1.peak_equity
        assert peak_pnl_1 > 19_500.0
        assert peak_eq_1 == pytest.approx(100_000.0 + peak_pnl_1)
        assert mgr1.halted is False

        # Slight pullback before restart (~-₹4,000 -> ~3.4% off peak, still +₹16k on day):
        mgr1.broker.submit_market(rid, "RELIANCE", SIDE_BUY, 800, 100.0)
        mgr1.broker.submit_market(rid, "RELIANCE", SIDE_SELL, 800, 95.0)
        mgr1._evaluate_risk()
        assert mgr1.halted is False
        mgr1.shutdown()

        # Mid-day restart on the same IST session:
        mgr2 = _tight_mgr()
        try:
            assert mgr2._session_peak_daily_pnl == pytest.approx(peak_pnl_1)
            assert mgr2.peak_equity == pytest.approx(peak_eq_1)
            assert mgr2._bucket_peak["paper"] == pytest.approx(peak_eq_1)
            assert mgr2.halted is False

            # Post-restart reversal (~-₹10,400 -> equity ~₹105.5k: STILL +₹5.5k UP on
            # the day, yet ~12% below the ~₹120k morning peak, exceeding 10% limit!):
            mgr2.broker.submit_market(rid, "RELIANCE", SIDE_BUY, 800, 100.0)
            mgr2.broker.submit_market(rid, "RELIANCE", SIDE_SELL, 800, 87.0)
            assert mgr2.get_runner(rid).daily_pnl() > 0.0, "still net green on the day"
            mgr2._evaluate_risk()
            assert mgr2._bucket_halted["paper"] is True
            assert mgr2.halted is True
            assert "drawdown" in (mgr2.halt_reason or "").lower()

            # Immediate durability: even WITHOUT calling shutdown() or running 5 ticks,
            # a crash-recovery boot (`mgr3`) sees the halt latch on disk right away!
            mgr3 = _tight_mgr()
            try:
                assert mgr3.halted is True
                assert mgr3._bucket_halted["paper"] is True
            finally:
                mgr3.shutdown()
        finally:
            mgr2.shutdown()

    def test_offline_runner_removal_does_not_trigger_false_drawdown_on_restart(
        self, tmp_path
    ):
        """Ticket B1: cash-flow-neutral peak reconstruction (`open_baseline +
        session_peak_daily_pnl`) prevents a phantom 50% drawdown crash when one
        of two ₹5L runners is removed offline from `portfolio_state.json`.
        """
        state = tmp_path / "state.json"

        def _tight_mgr() -> PortfolioManager:
            return PortfolioManager(
                risk_config=GlobalRiskConfig(
                    daily_loss_limit=500_000,
                    max_drawdown_pct=0.10,  # 10% max drawdown
                ),
                tick_seconds=1.0,
                warmup_bars=5,
                auto_start_feed=False,
                state_path=state,
                persist_trades=False,
            )

        mgr1 = _tight_mgr()
        id1 = mgr1.add_runner(
            _runner_config("SURVIVOR", symbol="RELIANCE", allocated_capital=500_000)
        )
        mgr1.add_runner(
            _runner_config("REMOVED-OFFLINE", symbol="TCS", allocated_capital=500_000)
        )
        # SURVIVOR makes ~+₹10k intraday gain -> ~₹10.1L raw combined peak:
        mgr1.broker.submit_market(id1, "RELIANCE", SIDE_BUY, 1_000, 100.0)
        mgr1.broker.submit_market(id1, "RELIANCE", SIDE_SELL, 1_000, 110.0)
        mgr1._evaluate_risk()
        peak_pnl_1 = mgr1._session_peak_daily_pnl
        assert peak_pnl_1 > 9_500.0
        assert mgr1.peak_equity == pytest.approx(1_000_000.0 + peak_pnl_1)
        mgr1.shutdown()

        # Operator removes `REMOVED-OFFLINE` from the state file while offline:
        raw = json.loads(state.read_text(encoding="utf-8"))
        raw["runners"] = [
            r for r in raw["runners"] if r["config"]["name"] == "SURVIVOR"
        ]
        state.write_text(json.dumps(raw), encoding="utf-8")

        # Same-day boot: raw saved peak was ~₹10.1L vs ~₹5.1L survivor equity,
        # which would have been a fake 49.5% drawdown under raw scalar restore.
        # Cash-flow-neutral reconstruction yields ₹5.0L + peak_pnl_1 (0% DD)!
        mgr2 = _tight_mgr()
        try:
            expected_survivor_peak = 500_000.0 + peak_pnl_1
            assert mgr2.peak_equity == pytest.approx(expected_survivor_peak)
            assert mgr2._bucket_peak["paper"] == pytest.approx(expected_survivor_peak)
            mgr2._evaluate_risk()
            assert mgr2.halted is False
            assert mgr2._bucket_halted["paper"] is False
        finally:
            mgr2.shutdown()

    def test_in_process_profitable_runner_removal_does_not_trip_survivor_drawdown(
        self, tmp_path
    ):
        """Ticket B1: removing a profitable runner in-process adjusts
        `session_peak_daily_pnl` alongside `_record_capital_flow` so a smaller
        surviving runner does not inherit the retired runner's ₹80k gain as a
        phantom drawdown ceiling.
        """
        state = tmp_path / "state.json"
        mgr = PortfolioManager(
            risk_config=GlobalRiskConfig(
                daily_loss_limit=500_000,
                max_drawdown_pct=0.10,
            ),
            tick_seconds=1.0,
            warmup_bars=5,
            auto_start_feed=False,
            state_path=state,
            persist_trades=False,
        )
        try:
            id_big = mgr.add_runner(
                _runner_config("BIG-WINNER", symbol="RELIANCE", allocated_capital=500_000)
            )
            mgr.add_runner(
                _runner_config("SMALL-FLAT", symbol="TCS", allocated_capital=100_000)
            )
            mgr.broker.submit_market(id_big, "RELIANCE", SIDE_BUY, 2_000, 100.0)
            mgr.broker.submit_market(id_big, "RELIANCE", SIDE_SELL, 2_000, 140.0)

            mgr._evaluate_risk()
            assert mgr._session_peak_daily_pnl > 79_000.0

            assert mgr.remove_runner(id_big) is True
            assert mgr._session_peak_daily_pnl == pytest.approx(0.0)
            assert mgr.peak_equity == pytest.approx(100_000.0)

            mgr._evaluate_risk()
            assert mgr.halted is False
            assert mgr._bucket_halted["paper"] is False
        finally:
            mgr.shutdown()

    def test_segment_slice_persists_same_day_and_live_midnight_rolls_clean(
        self, tmp_path, monkeypatch
    ):
        """Ticket B1: `segment:*` breaker latches round-trip across same-day
        restarts in `buckets`, and `_maybe_roll_risk_session_ist` clears active
        halts + peaks automatically when `current_session_date()` advances across
        00:00 IST in a long-running process.
        """
        from datetime import date
        import backtest.forward.portfolio_manager as pm_mod

        state = tmp_path / "state.json"
        monkeypatch.setattr(pm_mod, "current_session_date", lambda: date(2026, 11, 3))

        mgr1 = _manager(state)
        mgr1.add_runner(_runner_config("SEG-PAPER"))
        seg_key = mgr1._segment_key("nse_fno")
        mgr1._ensure_bucket_state(seg_key)
        mgr1._bucket_halted[seg_key] = True
        mgr1._bucket_halt_reason[seg_key] = "segment daily loss breached"
        mgr1._bucket_halt_mode[seg_key] = "pause"
        mgr1._bucket_halted_ts[seg_key] = "2026-11-03T07:00:00+00:00"
        mgr1.shutdown()

        # Same-day boot (2026-11-03) -> segment latch preserved!
        mgr2 = _manager(state)
        try:
            assert mgr2._bucket_halted[seg_key] is True
            assert mgr2._bucket_halt_reason[seg_key] == "segment daily loss breached"

            # Advance clock across 00:00 IST to 2026-11-04 in-process:
            monkeypatch.setattr(pm_mod, "current_session_date", lambda: date(2026, 11, 4))
            mgr2._evaluate_risk()
            assert mgr2._risk_session_date_ist == "2026-11-04"
            assert mgr2._bucket_halted[seg_key] is False
            assert mgr2._bucket_halt_reason[seg_key] is None
            assert mgr2._session_peak_daily_pnl == 0.0
        finally:
            mgr2.shutdown()


# ---------------------------------------------------------------------------
# Option runners: the bridge book comes back
# ---------------------------------------------------------------------------


class TestOptionBookRestore:
    def _option_manager(self, state_path):
        return PortfolioManager(
            risk_config=GlobalRiskConfig(daily_loss_limit=1_000_000, max_drawdown_pct=0.9),
            tick_seconds=1.0,
            warmup_bars=30,
            auto_start_feed=False,
            state_path=state_path,
            persist_trades=False,  # same promise as ``_manager`` above
        )

    def test_open_structure_and_bridge_scalars_round_trip(self, tmp_path):
        state = tmp_path / "state.json"
        mgr1 = self._option_manager(state)
        opt_id = mgr1.add_runner(
            RunnerConfig(
                name="OPT",
                strategy_name="directional_options",
                allocated_capital=500_000,
                symbols=["NIFTY"],
                timeframe="1day",
                mode="paper",
                source="synthetic",
                instrument={
                    "type": "option",
                    "expression": {
                        "expression_version": 2,
                        "type": "bull_call_spread",
                        "quantity": 3,
                        "exit": {"stop_loss_points": 10_000.0, "min_days_to_expiry": 2},
                    },
                },
            )
        )
        # bull call spread book: the V1 gate refuses it, so widen the runner
        # before any bar reaches it.
        research_profile(mgr1.get_runner(opt_id))
        mgr1.feed.warmup()
        base = datetime.now(timezone.utc) + timedelta(days=1)
        for i in range(80):
            mgr1.tick(ts=base + timedelta(days=i))
        runner1 = mgr1.get_runner(list(mgr1._runners)[0])
        bridge1 = runner1.options_bridge
        assert bridge1.executed_count >= 1, "setup: option runner must have traded"
        broker1 = bridge1.option_broker
        cash_before = float(broker1.available_cash)
        n_structures = len(broker1._structures)
        n_positions = len(broker1._positions)
        assert n_structures >= 1
        mgr1.shutdown()

        mgr2 = self._option_manager(state)
        try:
            restored = mgr2.get_runner(list(mgr2._runners)[0])
            assert restored.status == "PAUSED"
            bridge2 = restored.options_bridge
            broker2 = bridge2.option_broker
            assert len(broker2._structures) == n_structures
            assert len(broker2._positions) == n_positions
            assert float(broker2.available_cash) == cash_before
            assert bridge2.open_structure_id == bridge1.open_structure_id
            assert bridge2._bar_index == bridge1._bar_index
            assert str(bridge2._entry_premium) == str(bridge1._entry_premium)

            # legs survived with exact terms
            def _legs(broker):
                return sorted(
                    (leg.trading_symbol, leg.side, leg.quantity)
                    for leg in broker._positions.values()
                )

            assert _legs(broker2) == _legs(bridge1.option_broker)
        finally:
            mgr2.shutdown()


# ---------------------------------------------------------------------------
# Robustness: corrupt state never blocks a boot; writes are atomic
# ---------------------------------------------------------------------------


class TestRobustness:
    def test_corrupt_file_boots_clean_and_is_renamed_aside(self, tmp_path):
        state = tmp_path / "state.json"
        state.write_text("{ this is not json")
        mgr = _manager(state)
        try:
            assert mgr._runners == {}
            assert (
                not state.exists() or list(tmp_path.glob("*.corrupt"))
            ), "corrupt file renamed aside, boot clean"
        finally:
            mgr.shutdown()

    def test_wrong_schema_is_ignored(self, tmp_path):
        state = tmp_path / "state.json"
        state.write_text(json.dumps({"schema": 999, "runners": []}))
        mgr = _manager(state)
        try:
            assert mgr._runners == {}
        finally:
            mgr.shutdown()

    def test_failed_write_leaves_last_good_file(self, tmp_path):
        state = tmp_path / "state.json"
        store = PortfolioStateStore(state)
        store.save({"manager": {}, "buckets": {}, "runners": []})
        good = state.read_text()

        real_dump = json.dump

        def boom(*args, **kwargs):
            raise OSError("disk full")

        with pytest.raises(OSError):
            import json as _json

            _json.dump = boom
            try:
                store.save({"manager": {}, "buckets": {}, "runners": [{"x": 1}]})
            finally:
                _json.dump = real_dump

        assert state.read_text() == good, "last good state intact"
        assert not list(tmp_path.glob("*.tmp")), "no temp litter"

    def test_manual_clear_forgets_state(self, tmp_path):
        state = tmp_path / "state.json"
        store = PortfolioStateStore(state)
        store.save({"runners": [1]})
        store.clear()
        assert not state.exists()
        assert store.load() is None

    def test_store_isolated_from_config_round_trip(self, tmp_path):
        """Unknown future config keys are dropped with a warning, not a crash."""
        from backtest.forward.state_store import _config_from_dict, config_to_dict

        payload = config_to_dict(_runner_config("FUT"))
        payload["some_future_key"] = 123
        config = _config_from_dict(payload)
        assert config.name == "FUT"


# ---------------------------------------------------------------------------
# Restore isolation: one un-restorable runner must not abort the restore
# ---------------------------------------------------------------------------


class TestRestoreIsolation:
    def test_retired_strategy_runner_is_skipped_not_fatal(self, tmp_path):
        """Deploy hazard (Phase 2 flag): state written before a strategy
        was retired (atm_instant_buy etc.) must not abort the restore
        loop — the retired runner is skipped, later runners restore, and
        manager scalars still apply."""
        import copy

        from backtest.forward.state_store import capture_runner

        src = _manager(tmp_path / "src.json")
        instance_id = src.add_runner(_runner_config("KEEP-ME"))
        valid_snap = capture_runner(src._runners[instance_id])

        retired_snap = copy.deepcopy(valid_snap)
        retired_snap["config"] = dict(
            valid_snap["config"],
            name="GONE",
            strategy_name="atm_instant_buy",  # retired in Phase 2
            instance_id="retired-instance",
        )

        dst = _manager(tmp_path / "dst.json")
        restored = dst._apply_state_payload(
            {
                # Retired FIRST: proves the loop continues past it.
                "runners": [retired_snap, valid_snap],
                "manager": {"total_capital": 123.0, "tick_count": 7},
                "buckets": {},
            }
        )
        assert restored == 1
        assert [r.config.name for r in dst._runners.values()] == ["KEEP-ME"]
        assert dst.tick_count == 7  # manager scalars after the loop applied
        src.shutdown()
        dst.shutdown()


# ---------------------------------------------------------------------------
# A restore refusal must not erase the snapshot (2026-10-27)
# ---------------------------------------------------------------------------


class TestRefusedRestoreKeepsSnapshot:
    """Five option runners were lost on 2026-10-27 this way: an overnight
    session expiry made restore refuse them (fail-closed, correct), only the
    equity runner came back, and the operator's 10:52 resume — a control-plane
    mutation — rewrote the state file from ``self._runners``, which no longer
    held the refused five. Configs, bridge books and day anchors lived nowhere
    else, so a temporary refusal became permanent deletion.
    """

    @staticmethod
    def _refuse_named(monkeypatch, name: str):
        original = PortfolioManager.add_runner

        def guarded(self, config, *args, **kwargs):
            if config.name == name:
                raise ValueError(
                    f"option runner on {config.symbols[0]} asked for "
                    "source=mstock but there is no authenticated mstock session"
                )
            return original(self, config, *args, **kwargs)

        monkeypatch.setattr(PortfolioManager, "add_runner", guarded)
        return original

    def test_refused_runner_survives_saves_and_the_next_boot(
        self, tmp_path, monkeypatch
    ):
        state = tmp_path / "state.json"

        # session one: two runners on the record
        mgr1 = _manager(state)
        mgr1.add_runner(_runner_config("GOOD"))
        mgr1.add_runner(_runner_config("REFUSED-ME", symbol="NIFTY"))
        before = json.loads(state.read_text())
        assert [r["config"]["name"] for r in before["runners"]] == [
            "GOOD",
            "REFUSED-ME",
        ]
        refused_before = [
            r for r in before["runners"] if r["config"]["name"] == "REFUSED-ME"
        ][0]
        mgr1.shutdown()

        # session two: the second one is refused at restore …
        self._refuse_named(monkeypatch, "REFUSED-ME")
        mgr2 = _manager(state)
        assert [r.config.name for r in mgr2._runners.values()] == ["GOOD"]

        # … and every save afterwards still carries it
        assert mgr2.save_state() is True
        after = json.loads(state.read_text())
        assert sorted(r["config"]["name"] for r in after["runners"]) == [
            "GOOD",
            "REFUSED-ME",
        ]
        parked = [r for r in after["runners"] if r["config"]["name"] == "REFUSED-ME"][0]
        assert parked == refused_before, "the parked snapshot is byte-for-byte intact"
        mgr2.shutdown()

        # session three: the blocker is gone → it comes back, exactly once
        monkeypatch.undo()
        mgr3 = _manager(state)
        assert sorted(r.config.name for r in mgr3._runners.values()) == [
            "GOOD",
            "REFUSED-ME",
        ]
        assert mgr3._parked_snapshots == {}, "a restored runner leaves the parked set"
        mgr3.save_state()
        names = [r["config"]["name"] for r in json.loads(state.read_text())["runners"]]
        assert sorted(names) == ["GOOD", "REFUSED-ME"] and len(names) == 2
        mgr3.shutdown()

    def test_parked_runner_is_really_recoverable_by_identity(
        self, tmp_path, monkeypatch
    ):
        """The park key is the stored instance_id, so a later boot must reuse
        the same identity — not mint a new one and orphan the parked twin."""
        state = tmp_path / "state.json"
        mgr1 = _manager(state)
        instance_id = mgr1.add_runner(_runner_config("REFUSED-ME", symbol="NIFTY"))
        mgr1.shutdown()

        self._refuse_named(monkeypatch, "REFUSED-ME")
        mgr2 = _manager(state)
        assert instance_id in mgr2._parked_snapshots
        mgr2.save_state()
        mgr2.shutdown()

        monkeypatch.undo()
        mgr3 = _manager(state)
        assert list(mgr3._runners) == [instance_id]
        mgr3.shutdown()

    def test_operator_can_delete_a_parked_runner(self, tmp_path, monkeypatch):
        state = tmp_path / "state.json"
        mgr1 = _manager(state)
        instance_id = mgr1.add_runner(_runner_config("REFUSED-ME", symbol="NIFTY"))
        mgr1.shutdown()

        self._refuse_named(monkeypatch, "REFUSED-ME")
        mgr2 = _manager(state)
        assert mgr2.remove_runner(instance_id) is True
        names = [r["config"]["name"] for r in json.loads(state.read_text())["runners"]]
        assert names == []
        mgr2.shutdown()

    def test_unknown_instance_still_returns_false(self, tmp_path):
        mgr = _manager(tmp_path / "state.json")
        assert mgr.remove_runner("no-such-instance") is False
        mgr.shutdown()

    def test_boot_freezes_the_file_before_any_save(self, tmp_path, monkeypatch):
        """Second net: if the restore itself blows up, the boot logs 'starting
        clean' — and the first save would then overwrite the file from an
        emptier memory. The pre-restore copy keeps that recoverable."""
        state = tmp_path / "state.json"
        mgr1 = _manager(state)
        mgr1.add_runner(_runner_config("BOOT-BACKUP"))
        mgr1.shutdown()

        def explode(self, payload):
            raise RuntimeError("simulated restore failure")

        monkeypatch.setattr(PortfolioManager, "_apply_state_payload", explode)
        mgr2 = _manager(state)  # boots clean after the caught failure
        backups = list(tmp_path.glob("state.json.boot_*"))
        assert len(backups) == 1, "one copy per boot"
        assert "BOOT-BACKUP" in backups[0].read_text()
        assert mgr2.save_state() is True
        saved = json.loads(backups[0].read_text())
        assert [r["config"]["name"] for r in saved["runners"]] == ["BOOT-BACKUP"]
        mgr2.shutdown()

    def test_boot_backups_are_bounded(self, tmp_path, monkeypatch):
        store = PortfolioStateStore(tmp_path / "state.json")
        store.save({"runners": [], "manager": {}, "buckets": {}})
        keep = PortfolioStateStore.KEEP_BOOT_BACKUPS
        for i in range(keep + 5):
            (tmp_path / f"state.json.boot_2026010{i:02d}000000").write_text("{}")
        assert store.boot_backup() is not None
        remaining = list(tmp_path.glob("state.json.boot_*"))
        assert len(remaining) == keep, "oldest pruned, newest kept"


# ---------------------------------------------------------------------------
# Ticket 2.4 — the hermetic seam itself
# ---------------------------------------------------------------------------


class TestNoPersistenceSeam:
    """``persist_trades=False`` must mean *never resolved*, not *sandboxed*.

    Under pytest the DB firewall rewrites a live URL to ``sqlite:///:memory:``,
    so a manager that resolves one still passes every test — while the ledger
    seam is attached, real rows are queried by the boot reap, and the isolation
    depends on a guard nobody reads. These two pin the difference.
    """

    def test_a_hermetic_manager_never_resolves_a_database_url(self, tmp_path, monkeypatch):
        from backtest.db import manager as db_manager

        resolved = []

        def _boom(*args, **kwargs):
            # Counted, not only raised: GAP-4 is fail-soft, so an exception out
            # of ``from_env`` would be swallowed and still leave a nil persister.
            resolved.append(True)
            raise AssertionError("a hermetic manager must not resolve a DB URL")

        monkeypatch.setattr(db_manager.DatabaseManager, "from_env", staticmethod(_boom))

        mgr = _manager(tmp_path / "state.json")
        try:
            assert resolved == [], "persist_trades=False must not reach for a DB URL"
            assert mgr._trade_persister is None
            # BUG-37's boot reap is a DB write; with no seam it is a no-op.
            mgr._reap_orphan_portfolio_rows()
        finally:
            mgr.shutdown()

    def test_the_default_still_opens_the_gap_4_seam_and_attaches_the_ledger(
        self, tmp_path, monkeypatch
    ):
        """The flag is what turns persistence off — not a missing database.

        Same spy, opposite answer: the manager hands the persister this
        process's ``OrderLedger`` (B3), which is exactly what the hermetic
        manager must not do.
        """
        from backtest.db import manager as db_manager

        built = []

        class _FakeDB:
            def session(self):  # pragma: no cover — never used by these asserts
                raise AssertionError("no DB work in this test")

        monkeypatch.setattr(
            db_manager.DatabaseManager, "from_env", staticmethod(lambda *a, **k: _FakeDB())
        )
        import backtest.forward.trade_persistence as tp_mod

        real_persister = tp_mod.LiveTradePersister

        def _spy(db_manager):
            persister = real_persister(db_manager)
            built.append(persister)
            return persister

        monkeypatch.setattr(tp_mod, "LiveTradePersister", _spy)

        mgr = PortfolioManager(
            risk_config=GlobalRiskConfig(daily_loss_limit=1_000_000, max_drawdown_pct=0.9),
            auto_start_feed=False,
            state_path=tmp_path / "state.json",
        )
        try:
            assert built, "the default must keep GAP-4 trade history"
            assert mgr._trade_persister is built[0]
            assert built[0]._ledger is mgr.ledger, "B3: the ledger rides the seam"
        finally:
            mgr.shutdown()
