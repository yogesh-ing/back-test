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
from backtest.forward.paper_runner import SIDE_BUY, RunnerConfig
from backtest.forward.risk_supervisor import GlobalRiskConfig
from backtest.forward.state_store import PortfolioStateStore, STATE_SCHEMA_VERSION


def _manager(state_path=None) -> PortfolioManager:
    return PortfolioManager(
        risk_config=GlobalRiskConfig(daily_loss_limit=1_000_000, max_drawdown_pct=0.9),
        tick_seconds=1.0,
        warmup_bars=5,
        auto_start_feed=False,
        state_path=state_path,
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
            # day anchors survive (runner + manager level)
            assert restored._current_day == "2030-01-01"
            assert mgr2._current_day == "2030-01-01"

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

    def test_tripped_breaker_stays_tripped(self, tmp_path):
        """THE original complaint: a restart must not resurrect a halted breaker."""
        state = tmp_path / "state.json"
        mgr1 = _manager(state)
        mgr1.add_runner(_runner_config("HALTED"))
        mgr1._bucket_halted["paper"] = True
        mgr1._bucket_halt_reason["paper"] = "daily loss limit breached"
        mgr1._bucket_halt_mode["paper"] = "daily_loss_limit"
        mgr1._bucket_halted_ts["paper"] = "2026-09-17T10:00:00+00:00"
        mgr1.shutdown()

        mgr2 = _manager(state)
        try:
            assert mgr2._bucket_halted["paper"] is True
            assert mgr2._bucket_halt_reason["paper"] == "daily loss limit breached"
            assert mgr2._bucket_halt_mode["paper"] == "daily_loss_limit"
            # and the guard still refuses a scoped resume
            with pytest.raises(RuntimeError, match="halted by circuit breaker"):
                mgr2.resume_all("paper")
        finally:
            mgr2.shutdown()

    def test_manager_level_halt_survives(self, tmp_path):
        state = tmp_path / "state.json"
        mgr1 = _manager(state)
        mgr1.halted = True
        mgr1.halt_reason = "master kill"
        mgr1.shutdown()
        mgr2 = _manager(state)
        try:
            assert mgr2.halted is True and mgr2.halt_reason == "master kill"
        finally:
            mgr2.shutdown()

    def test_bucket_anchors_survive(self, tmp_path):
        state = tmp_path / "state.json"
        mgr1 = _manager(state)
        mgr1.add_runner(_runner_config())
        mgr1._bucket_peak["paper"] = 123_456.0
        mgr1._bucket_day_start["paper"] = 99_000.0
        mgr1._bucket_day["paper"] = "2026-09-17"
        mgr1.shutdown()
        mgr2 = _manager(state)
        try:
            assert mgr2._bucket_peak["paper"] == 123_456.0
            assert mgr2._bucket_day_start["paper"] == 99_000.0
            assert mgr2._bucket_day["paper"] == "2026-09-17"
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
        )

    def test_open_structure_and_bridge_scalars_round_trip(self, tmp_path):
        state = tmp_path / "state.json"
        mgr1 = self._option_manager(state)
        mgr1.add_runner(
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
                        "type": "bull_call_spread",
                        "strike_selection": "atm",
                        "quantity": 3,
                        "exit": {"min_days_to_expiry": 2},
                    },
                },
            )
        )
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
