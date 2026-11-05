"""Sprint D / Ticket D3 — FROZEN_AT_SPAWN Config-Drift Badge & 1-Click Flat Rollover.

Covers:
1. ``StrategyRunner.config_drift_info()`` / ``get_state()`` detects when a runner's
   frozen spawn expression (e.g. ``carry_policy="intraday"`` on ``straddle_short_atm``
   before ``3ae2888``) drifts from the current strategy template on disk
   (``carry_policy="positional"``), without silently mutating the live runner's
   exit/carry rules mid-trade.
2. ``POST /api/portfolio/runners/<id>/respawn`` strictly refuses (HTTP ``409``)
   while ``open_positions > 0``.
3. Once flat (``open_positions == 0``), ``POST /api/portfolio/runners/<id>/respawn``
   retires the outdated runner and spawns its replacement from the latest
   template rules (``carry_policy == "positional"``, ``config_drift is False``),
   preserving operator lot sizing.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest

from backtest.forward.paper_runner import RunnerConfig
from backtest.forward.portfolio_manager import (
    get_portfolio_manager,
    reset_portfolio_manager,
)
from backtest.options.paper_trading import OptionPosition, StructurePosition
from backtest.strategy.registry import get_strategy
from backtest.timekeeping import EXCHANGE_TZ
from backtest.web.app import create_app


def _legacy_intraday_straddle_config(*, instance_id: str = "legacy-straddle-1") -> RunnerConfig:
    """Simulate a pre-3ae2888 ``straddle_short_atm`` runner frozen on ``intraday``."""
    strat_cls = get_strategy("straddle_short_atm")
    expr = strat_cls.execution_spec().to_expression()
    legacy_expr = {
        **expr,
        "quantity": 2,
        "carry_policy": "intraday",
    }
    return RunnerConfig(
        name="ShortStraddle·NIFTY·5min",
        strategy_name="straddle_short_atm",
        symbols=["NIFTY"],
        timeframe="5min",
        allocated_capital=300_000.0,
        mode="paper",
        source="synthetic",
        instance_id=instance_id,
        instrument={
            "type": "option",
            "expression": legacy_expr,
            "expression_provenance": {"quantity": "user"},
        },
    )


def test_config_drift_detected_and_flat_respawn_rolls_to_latest_template(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALLOW_LIVE_ORDERS", "0")
    monkeypatch.setenv("BACKTEST_DATA_PROFILE", "testing")
    monkeypatch.setenv("FORWARD_TEST_DB_PROFILE", "testing")
    monkeypatch.setenv("FORWARD_TEST_DB_URL", "sqlite:///:memory:")
    reset_portfolio_manager(auto_start_feed=False)
    app = create_app(source="synthetic", log_level="ERROR", TESTING=True)

    mgr = get_portfolio_manager()
    try:
        mgr.add_runner(_legacy_intraday_straddle_config())
        runner = mgr.get_runner("legacy-straddle-1")
        assert runner is not None

        # Frozen spawn contract stays untouched ('intraday'), while get_state() flags drift:
        state = runner.get_state()
        assert state["carry_policy"] == "intraday"
        assert state["config_drift"] is True
        assert any("carry_policy drifted" in r for r in state["config_drift_reasons"])
        assert state["can_respawn"] is True

        # Attach an open structure -> respawn MUST refuse with HTTP 409!
        leg = OptionPosition(
            position_id="leg-strad-1",
            structure_id="struct-strad-1",
            strategy_name="straddle_short_atm",
            underlying="NIFTY",
            strike=Decimal("24000"),
            expiry=date(2026, 11, 5),
            option_type="CE",
            side="SELL",
            quantity=2,
            lot_size=75,
            entry_price=Decimal("110.00"),
            current_price=Decimal("105.00"),
            opened_at=datetime(2026, 11, 3, 9, 20, tzinfo=EXCHANGE_TZ),
            trading_symbol="NIFTY26NOV24000CE",
        )
        struct = StructurePosition(
            structure_id="struct-strad-1",
            structure_type="short_straddle",
            strategy_name="straddle_short_atm",
            underlying="NIFTY",
            expiry=date(2026, 11, 5),
            legs=[leg],
            opened_at=datetime(2026, 11, 3, 9, 20, tzinfo=EXCHANGE_TZ),
        )
        runner.options_bridge.option_broker._structures["struct-strad-1"] = struct
        runner.options_bridge.option_broker._positions["leg-strad-1"] = leg
        runner.options_bridge.open_structure_id = "struct-strad-1"
        assert runner.get_state()["can_respawn"] is False

        with app.test_client() as client:
            blocked = client.post("/api/portfolio/runners/legacy-straddle-1/respawn")
            assert blocked.status_code == 409
            assert "open" in (blocked.get_json() or {}).get("error", "").lower()

            # Close position (book now flat) -> 1-click respawn succeeds (201)!
            runner.options_bridge.option_broker._structures.clear()
            runner.options_bridge.option_broker._positions.clear()
            runner.options_bridge.open_structure_id = None
            assert runner.get_state()["can_respawn"] is True

            ok_resp = client.post("/api/portfolio/runners/legacy-straddle-1/respawn")
            assert ok_resp.status_code == 201
            body = ok_resp.get_json()
            assert body["success"] is True
            assert body["retired_instance_id"] == "legacy-straddle-1"
            new_id = body["instance_id"]
            assert new_id != "legacy-straddle-1"
            assert mgr.get_runner("legacy-straddle-1") is None

            new_runner = mgr.get_runner(new_id)
            assert new_runner is not None
            new_state = new_runner.get_state()
            assert new_state["carry_policy"] == "positional"
            assert new_state["config_drift"] is False
            assert (
                new_runner.config.instrument["expression"]["quantity"] == 2
            )
    finally:
        mgr.shutdown()


def _template_current_config(*, instance_id: str) -> RunnerConfig:
    """An option runner frozen on the template's OWN expression: zero drift by construction."""
    expr = get_strategy("straddle_short_atm").execution_spec().to_expression()
    return RunnerConfig(
        name="ShortStraddle·NIFTY·5min",
        strategy_name="straddle_short_atm",
        symbols=["NIFTY"],
        timeframe="5min",
        allocated_capital=300_000.0,
        mode="paper",
        source="synthetic",
        instance_id=instance_id,
        instrument={"type": "option", "expression": expr},
    )


def test_engine_code_drift_is_told_but_does_not_flag_the_pill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bundle hashes four strategy-side files, so the engine under a book was invisible.

    2026-11-05: a whitespace-only description edit flagged all seven live books
    ``strategy code changed`` while six engine fixes — including the bar-clock
    seam that had blocked every entry on 04-Nov — landed under them unflagged.
    The move is now a reason, but it deliberately does NOT set ``config_drift``:
    the only cure is respawn, and ``respawn_runner`` calls ``remove_runner``,
    which deletes an option book's history (state file only — open legs have no
    DB row). Raising that pill on every row after every deploy would put a
    history-destroying button in front of the operator as a matter of routine.
    """
    from backtest.strategy.bundle import engine_git_sha

    monkeypatch.setenv("ALLOW_LIVE_ORDERS", "0")
    monkeypatch.setenv("BACKTEST_DATA_PROFILE", "testing")
    monkeypatch.setenv("FORWARD_TEST_DB_PROFILE", "testing")
    monkeypatch.setenv("FORWARD_TEST_DB_URL", "sqlite:///:memory:")
    reset_portfolio_manager(auto_start_feed=False)
    create_app(source="synthetic", log_level="ERROR", TESTING=True)

    mgr = get_portfolio_manager()
    try:
        moved = _template_current_config(instance_id="engine-moved-1")
        moved.strategy_bundle = {"engine_git_sha": "0" * 40}
        mgr.add_runner(moved)
        state = mgr.get_runner("engine-moved-1").get_state()
        assert state["engine_code_changed_since_spawn"] is True
        notes = [r for r in state["config_drift_reasons"] if r.startswith("engine code")]
        assert len(notes) == 1, state["config_drift_reasons"]
        assert "0000000 ->" in notes[0], notes[0]
        # Told, not flagged: nothing here asks for a respawn.
        assert state["config_drift"] is False

        current = _template_current_config(instance_id="engine-current-1")
        current.strategy_bundle = {"engine_git_sha": engine_git_sha()}
        mgr.add_runner(current)
        clean = mgr.get_runner("engine-current-1").get_state()
        assert clean["engine_code_changed_since_spawn"] is False
        assert clean["config_drift_reasons"] == []

        # An unstamped snapshot has nothing to compare — report no drift rather
        # than inventing either a match or a move.
        untracked = _template_current_config(instance_id="engine-unknown-1")
        untracked.strategy_bundle = None
        mgr.add_runner(untracked)
        assert (
            mgr.get_runner("engine-unknown-1").get_state()["engine_code_changed_since_spawn"]
            is False
        )
    finally:
        mgr.shutdown()
