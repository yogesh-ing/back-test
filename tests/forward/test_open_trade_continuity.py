"""Sprint A-2 / Ticket A3 — Open-Trade Continuity & ``armed_at_utc`` Stale-Signal Guard.

Verifies the five institutional execution invariants of Ticket A3:
1. Open-exposure containers (equity positions, open option structures, or
   working orders) restore directly into ``STATUS_RUNNING`` while flat books
   restore into ``STATUS_PAUSED`` (``RESTART_FLAT_SAFETY``).
2. Unauthenticated boot + late TOTP login (``09:45 IST``): an option book
   holding an open structure boots ``STATUS_RUNNING`` + ``WAITING_FOR_FEED``
   on frozen marks, wakes to ``LIVE`` on ``on_broker_authenticated("mstock")``,
   and immediately exits a morning gap stop-loss breach in ``< 1s`` without
   waiting for a bar close.
3. ``CODE_UPDATED_WHILE_IN_TRADE``: when bundle code changes on disk while a
   runner holds an open trade, restore keeps ``STATUS_RUNNING`` to protect the
   open trade and automatically transitions to ``STATUS_PAUSED`` the instant
   the open trade exits, blocking un-migrated new entries.
4. Risk circuit-breaker precedence: same-day halted buckets/managers force
   ``STATUS_PAUSED`` (``BLOCKED_BY_RISK``) even when holding exposure.
5. ``armed_at_utc`` stale-bar / stale-tick guard + paused option bar-entry
   block: catch-up bars closing before ``armed_at_utc`` warm indicator buffers
   without firing stale entries, while fresh bars (``bar_close_utc >= armed_at_utc``)
   execute normally.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import backtest.forward.feed_registry as fr
from backtest.data import sources_policy
from backtest.forward.feed_registry import reset_data_bus
from backtest.forward.paper_runner import (
    FEED_STATE_LIVE,
    FEED_STATE_WAITING,
    OrderLedger,
    PaperBroker,
    RunnerConfig,
    STATUS_PAUSED,
    STATUS_RUNNING,
    StrategyRunner,
)
from backtest.forward.portfolio_manager import PortfolioManager
from backtest.forward.risk_supervisor import GlobalRiskConfig
from backtest.forward.state_store import capture_runner, restore_runner
from backtest.instruments.base import OptionType
from backtest.instruments.option import OptionContract
from backtest.options.paper_trading import OptionPosition, StructurePosition
from backtest.strategy.base import Strategy
from backtest.strategy.registry import _REGISTRY, get_strategy, register
from backtest.timekeeping import EXCHANGE_TZ

LONG_CALL_EXPRESSION: dict[str, Any] = {
    "expression_version": 2,
    "type": {
        "BULLISH": {"structure": "long_call", "strike_selection": "atm"},
        "BEARISH": {"structure": "long_put", "strike_selection": "atm"},
    },
    "quantity": 1,
    "exit": {"stop_loss_points": 25.0, "min_days_to_expiry": None},
}


class _AlwaysBullishEquityStrategy(Strategy):
    name = "_a3_bullish_eq"
    description = "Emits +1 on every bar for deterministic entry gating tests."

    def generate_signals(self, data):
        import pandas as pd

        return pd.Series([1] * len(data), index=data.index)


def _ensure_test_strategies() -> None:
    if "_a3_bullish_eq" not in _REGISTRY:
        register(_AlwaysBullishEquityStrategy)


def _make_runner(cfg: RunnerConfig) -> StrategyRunner:
    ledger = OrderLedger()
    return StrategyRunner(cfg, ledger=ledger, broker=PaperBroker(ledger))


def _make_manager(state_path: Path) -> PortfolioManager:
    return PortfolioManager(
        risk_config=GlobalRiskConfig(daily_loss_limit=1_000_000, max_drawdown_pct=0.9),
        tick_seconds=1.0,
        warmup_bars=5,
        auto_start_feed=False,
        enable_broker_warmup=False,
        state_path=str(state_path),
        persist_trades=False,
    )


def _fake_mstock_broker(*, ltp: float = 80.0, bid: float = 79.5, ask: float = 80.5):
    expiry = date(2026, 11, 5)
    contract = OptionContract(
        instrument_token="TOK-MSTOCK-24000-CE",
        trading_symbol="NIFTY261124000CE",
        underlying="NIFTY",
        expiry=expiry,
        strike=Decimal("24000"),
        option_type=OptionType.CE,
        lot_size=75,
        metadata={},
    )
    quote = {"ltp": ltp, "bid": bid, "ask": ask, "volume": 100, "oi": 1000}

    class _FakeBroker:
        broker_name = "mstock"

        def get_option_chain(self, underlying: str):
            return [contract]

        def get_option_quote(self, token: str):
            if token in (contract.instrument_token, contract.trading_symbol):
                return dict(quote)
            return {"ltp": 0.0}

    return _FakeBroker()


def _open_long_call_on_runner(
    runner: StrategyRunner,
    *,
    entry_price: str = "120.00",
    current_price: str = "120.00",
) -> StructurePosition:
    leg = OptionPosition(
        position_id="leg-a3-1",
        structure_id="struct-a3-1",
        strategy_name=runner.config.strategy_name,
        underlying="NIFTY",
        strike=Decimal("24000"),
        expiry=date(2026, 11, 5),
        option_type="CE",
        side="BUY",
        quantity=1,
        lot_size=75,
        entry_price=Decimal(entry_price),
        current_price=Decimal(current_price),
        opened_at=datetime(2026, 11, 2, 14, 30, tzinfo=EXCHANGE_TZ),
        trading_symbol="NIFTY261124000CE",
        instrument_token="TOK-MSTOCK-24000-CE",
    )
    struct = StructurePosition(
        structure_id="struct-a3-1",
        structure_type="long_call",
        strategy_name=runner.config.strategy_name,
        underlying="NIFTY",
        expiry=date(2026, 11, 5),
        legs=[leg],
        opened_at=datetime(2026, 11, 2, 14, 30, tzinfo=EXCHANGE_TZ),
    )
    assert runner.options_bridge is not None
    broker = runner.options_bridge.option_broker
    broker._structures[struct.structure_id] = struct
    broker._positions[leg.position_id] = leg
    runner.options_bridge.open_structure_id = struct.structure_id
    runner.options_bridge._entry_premium = Decimal(entry_price)
    return struct


def test_open_equity_restores_running_while_flat_equity_restores_paused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BACKTEST_DATA_PROFILE", "testing")
    sources_policy.reset_source_policy()
    _ensure_test_strategies()
    state_file = tmp_path / "state.json"

    mgr1 = _make_manager(state_file)
    try:
        in_trade_id = mgr1.add_runner(
            RunnerConfig(
                name="EQ-IN-TRADE",
                strategy_name="_a3_bullish_eq",
                symbols=["RELIANCE"],
                timeframe="5min",
                allocated_capital=200_000.0,
                source="synthetic",
                instance_id="eq-open-1",
            ),
            start=True,
        )
        flat_id = mgr1.add_runner(
            RunnerConfig(
                name="EQ-FLAT",
                strategy_name="sma_crossover",
                symbols=["TCS"],
                timeframe="5min",
                allocated_capital=200_000.0,
                source="synthetic",
                instance_id="eq-flat-1",
            ),
            start=True,
        )
        r_open = mgr1.get_runner(in_trade_id)
        assert r_open is not None
        for i in range(15):
            r_open.process_candle_event(
                "RELIANCE",
                {
                    "ts": f"2026-11-02T10:{i:02d}:00+05:30",
                    "open": 1000.0,
                    "high": 1005.0,
                    "low": 995.0,
                    "close": 1000.0,
                    "volume": 1000,
                },
            )
        assert r_open.has_open_exposure() is True
        r_open.set_position_rule("RELIANCE", "stop_loss", 970.0)
        mgr1.save_state()
    finally:
        mgr1.shutdown()

    mgr2 = _make_manager(state_file)
    try:
        revived_open = mgr2.get_runner(in_trade_id)
        revived_flat = mgr2.get_runner(flat_id)
        assert revived_open is not None and revived_flat is not None

        # Open exposure -> RUNNING; flat -> PAUSED (RESTART_FLAT_SAFETY)
        assert revived_open.status == STATUS_RUNNING
        assert revived_open.pause_reason is None
        assert revived_flat.status == STATUS_PAUSED
        assert revived_flat.pause_reason == "RESTART_FLAT_SAFETY"

        # Sub-second WS tick stop-loss protection works immediately without manual Resume!
        revived_open.on_tick("RELIANCE", 965.0, "2026-11-03T03:46:00+00:00")
        assert revived_open.has_open_exposure() is False
    finally:
        mgr2.shutdown()


def test_late_totp_wake_evaluates_gap_stop_loss_immediately_on_open_option_book(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(sources_policy.PROFILE_ENV, raising=False)
    reset_data_bus()
    sources_policy.reset_source_policy()
    monkeypatch.setattr(
        sources_policy, "_POLICY", sources_policy.build_policy(profile="default")
    )
    monkeypatch.setattr(fr, "_default_quote_broker", lambda source=None: None)
    state_file = tmp_path / "opt_wake.json"

    # Build snapshot representing an open mStock option structure at yesterday's close:
    mgr1 = _make_manager(state_file)
    try:
        rid = mgr1.add_runner(
            RunnerConfig(
                name="OPT-CARRY",
                strategy_name="directional_options",
                symbols=["NIFTY"],
                timeframe="5min",
                allocated_capital=300_000.0,
                mode="paper",
                source="mstock",
                instance_id="opt-carry-1",
                instrument={"type": "option", "expression": dict(LONG_CALL_EXPRESSION)},
            ),
            start=True,
        )
        r1 = mgr1.get_runner(rid)
        assert r1 is not None
        _open_long_call_on_runner(r1, entry_price="120.00", current_price="120.00")
        r1.last_price["NIFTY"] = 24000.0
        mgr1.save_state()
    finally:
        mgr1.shutdown()

    # Boot at 08:30 IST while mStock is logged out -> RUNNING + WAITING_FOR_FEED:
    mgr2 = _make_manager(state_file)
    try:
        r2 = mgr2.get_runner("opt-carry-1")
        assert r2 is not None
        assert r2.status == STATUS_RUNNING
        assert r2.feed_state == FEED_STATE_WAITING
        assert r2.has_open_exposure() is True

        # Operator logs in at 09:45 IST; option gapped from 120 -> 80 (-40 pts > 25 pt SL!)
        fake_broker = _fake_mstock_broker(ltp=80.0, bid=79.5, ask=80.5)
        monkeypatch.setattr(
            fr,
            "_default_quote_broker",
            lambda source=None: fake_broker if source in (None, "mstock") else None,
        )

        wake_res = mgr2.on_broker_authenticated("mstock")
        assert wake_res["woken"] == ["opt-carry-1"]
        assert r2.feed_state == FEED_STATE_LIVE
        # Exited immediately on wake without waiting for a 1-min/5-min bar close!
        assert wake_res.get("protective_exits") == 1
        assert r2.has_open_exposure() is False
        assert r2.options_bridge.closed_count == 1
    finally:
        mgr2.shutdown()
        reset_data_bus()
        sources_policy.reset_source_policy()


def test_code_updated_while_in_trade_protects_open_position_then_pauses_when_flat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BACKTEST_DATA_PROFILE", "testing")
    sources_policy.reset_source_policy()
    _ensure_test_strategies()

    cfg = RunnerConfig(
        name="DRIFT-IN-TRADE",
        strategy_name="_a3_bullish_eq",
        symbols=["INFY"],
        timeframe="5min",
        allocated_capital=200_000.0,
        source="synthetic",
        instance_id="drift-eq-1",
        strategy_bundle={
            "bundle_files": {"nonexistent_file.py": "0" * 64},
        },
    )
    runner = _make_runner(cfg)
    runner.start()
    for i in range(15):
        runner.process_candle_event(
            "INFY",
            {
                "ts": f"2026-11-02T11:{i:02d}:00+05:30",
                "open": 1500.0,
                "high": 1505.0,
                "low": 1495.0,
                "close": 1500.0,
                "volume": 500,
            },
        )
    assert runner.has_open_exposure() is True
    runner.set_position_rule("INFY", "stop_loss", 1480.0)
    snap = capture_runner(runner)

    # Restore against drifted bundle_files -> stays RUNNING while position is open!
    revived = _make_runner(cfg)
    monkeypatch.setattr(
        "backtest.strategy.bundle.bundle_matches", lambda cls, files: False
    )
    restore_runner(revived, snap)

    assert revived.status == STATUS_RUNNING
    assert revived.strategy_code_changed is True
    assert revived.code_updated_while_in_trade is True

    # Stop-loss triggers -> position closes cleanly AND runner auto-pauses before re-entry!
    revived.on_tick("INFY", 1475.0, "2026-11-03T04:00:00+00:00")
    assert revived.has_open_exposure() is False
    assert revived.status == STATUS_PAUSED
    assert revived.pause_reason == "CODE_UPDATED_WHILE_IN_TRADE"

    # Subsequent bullish bar cannot open a new position until explicit resume():
    revived.process_candle_event(
        "INFY",
        {
            "ts": "2026-11-03T09:35:00+05:30",
            "open": 1480.0,
            "high": 1490.0,
            "low": 1478.0,
            "close": 1488.0,
            "volume": 600,
        },
    )
    assert revived.has_open_exposure() is False


def test_same_day_risk_halt_overrides_open_exposure_to_paused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BACKTEST_DATA_PROFILE", "testing")
    sources_policy.reset_source_policy()
    _ensure_test_strategies()
    state_file = tmp_path / "halted_state.json"

    mgr1 = _make_manager(state_file)
    try:
        rid = mgr1.add_runner(
            RunnerConfig(
                name="HALTED-OPEN",
                strategy_name="_a3_bullish_eq",
                symbols=["SBIN"],
                timeframe="5min",
                allocated_capital=200_000.0,
                source="synthetic",
                instance_id="halt-open-1",
            ),
            start=True,
        )
        r1 = mgr1.get_runner(rid)
        assert r1 is not None
        for i in range(15):
            r1.process_candle_event(
                "SBIN",
                {
                    "ts": f"2026-11-03T09:{15 + i:02d}:00+05:30",
                    "open": 800.0,
                    "high": 802.0,
                    "low": 798.0,
                    "close": 800.0,
                    "volume": 1000,
                },
            )
        assert r1.has_open_exposure() is True
        # Simulate same-day bucket circuit breaker tripped while runner had status=RUNNING in snap:
        mgr1._bucket_halted["paper"] = True
        mgr1._bucket_halt_reason["paper"] = "paper daily loss fuse"
        r1.status = STATUS_RUNNING
        mgr1.save_state()
    finally:
        mgr1.shutdown()

    mgr2 = _make_manager(state_file)
    try:
        r2 = mgr2.get_runner("halt-open-1")
        assert r2 is not None
        assert r2.has_open_exposure() is True
        assert r2.status == STATUS_PAUSED
        assert r2.pause_reason == "BLOCKED_BY_RISK"
    finally:
        mgr2.shutdown()


def test_armed_at_utc_skips_stale_bars_and_paused_option_runner_blocks_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BACKTEST_DATA_PROFILE", "testing")
    sources_policy.reset_source_policy()
    reset_data_bus()

    from backtest.plugins import discover_plugins

    discover_plugins()
    straddle_cls = get_strategy("straddle_short_atm")
    expr = straddle_cls.execution_spec().to_expression()
    opt_runner = _make_runner(
        RunnerConfig(
            name="PAUSED-OPT-GUARD",
            strategy_name="straddle_short_atm",
            symbols=["NIFTY"],
            timeframe="5min",
            allocated_capital=300_000.0,
            mode="paper",
            source="synthetic",
            instrument={"type": "option", "expression": expr},
        )
    )
    opt_runner.start()
    opt_runner.pause()
    assert opt_runner.status == STATUS_PAUSED

    # Finding 2 regression guard: a PAUSED option runner MUST NOT enter on a closed bar!
    opt_runner.process_candle_event(
        "NIFTY",
        {
            "ts": "2026-11-03T09:30:00+05:30",
            "open": 24000.0,
            "high": 24020.0,
            "low": 23990.0,
            "close": 24010.0,
            "volume": 1000,
        },
    )
    assert opt_runner.has_open_exposure() is False

    # Resume and arm at 09:45:10 IST (04:15:10 UTC):
    opt_runner.resume()
    armed_dt = datetime(2026, 11, 3, 4, 15, 10, tzinfo=timezone.utc)
    opt_runner.arm_entries(at_utc=armed_dt)

    # 1) 5-min bar stamped 09:40:00+05:30 closes at 09:45:00 IST (< 09:45:10 IST) -> SKIPPED!
    opt_runner.process_candle_event(
        "NIFTY",
        {
            "ts": "2026-11-03T09:40:00+05:30",
            "open": 24010.0,
            "high": 24030.0,
            "low": 24000.0,
            "close": 24020.0,
            "volume": 1000,
        },
    )
    assert opt_runner.has_open_exposure() is False
    assert any(
        "ENTRY_STALE_BAR_SKIPPED" in str(sig.get("reason", ""))
        for sig in opt_runner.signal_log
    )

    # 2) 5-min bar stamped 09:45:00+05:30 closes at 09:50:00 IST (>= 09:45:10 IST) -> ENTERS!
    opt_runner.process_candle_event(
        "NIFTY",
        {
            "ts": "2026-11-03T09:45:00+05:30",
            "open": 24020.0,
            "high": 24050.0,
            "low": 24015.0,
            "close": 24040.0,
            "volume": 1200,
        },
    )
    assert opt_runner.has_open_exposure() is True
    opt_runner.stop()
    reset_data_bus()
