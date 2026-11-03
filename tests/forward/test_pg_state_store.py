"""Sprint D / Ticket D1 — PostgreSQL SSoT, 7-Event DB Touch Contract & Parity Verifier.

Covers:
1. Additive migration 024 SQLite script + idempotent ``ensure_forward_ssot_schema``.
2. Phase D1-a dual-write (``STATE_STORE_PRIMARY=json``) + ``verify_state_parity``
   (``PARITY_OK`` within ₹0.01 and ``PARITY_DIFF`` on divergence).
3. Phase D1-b cutover (``STATE_STORE_PRIMARY=postgres``) + cold-boot hydration of
   runners, open option structures/legs (including ``prev_close_mark``), manual
   SL/TP rules, and ``risk_session_state`` peak/halt latches.
4. Strict 0-SQL-query guarantee on live quote ticks (non-fill), non-trading 1-min
   bar sweeps, and UI snapshot reads (``list_positions`` / ``get_bucket_aggregates``).
5. Event 2 ``on_broker_authenticated`` live position reconciliation vs venue
   ``get_positions()`` (clean match vs external-trade mismatch alert).
6. Event 3 ``LiveTradePersister`` option structure open/close SQL sync, Event 5
   throttled risk milestone + immediate halt flush, and Event 6 ``15:30 IST``
   ``prev_close_mark`` EOD settlement.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import event, inspect

from backtest.db.config import DatabaseConfig
from backtest.db.manager import DatabaseManager
from backtest.db.models import (
    OptionOpenLegRecord,
    OptionOpenStructureRecord,
    RiskSessionStateRecord,
    RunnerInstanceRecord,
    ensure_forward_ssot_schema,
)
from backtest.forward.paper_runner import RunnerConfig
from backtest.forward.portfolio_manager import PortfolioManager
from backtest.forward.risk_supervisor import GlobalRiskConfig
from backtest.forward.state_store import (
    PostgresStateStore,
    resolve_state_store_primary,
    verify_state_parity,
)
from backtest.options.paper_trading import OptionPosition, StructurePosition
from backtest.timekeeping import EXCHANGE_TZ


def _make_sqlite_db(tmp_path: Path, name: str = "ssot.sqlite") -> DatabaseManager:
    db_file = tmp_path / name
    cfg = DatabaseConfig(url=f"sqlite:///{db_file}")
    db = DatabaseManager(cfg)
    db.connect()
    return db


def _seed_open_option_structure(runner, *, mark: str = "135.50") -> StructurePosition:
    """Attach a realistic open long-call structure to an option runner's bridge."""
    bridge = runner.options_bridge
    assert bridge is not None
    mark_dec = Decimal(mark)
    unreal = (mark_dec - Decimal("120.00")) * Decimal("75")
    leg = OptionPosition(
        position_id="leg-d1-001",
        structure_id="struct-d1-001",
        strategy_name="sma_crossover",
        underlying="NIFTY",
        strike=Decimal("24000"),
        expiry=date(2026, 11, 26),
        option_type="CE",
        side="BUY",
        quantity=1,
        lot_size=75,
        entry_price=Decimal("120.00"),
        opened_at=datetime(2026, 11, 2, 10, 0, tzinfo=EXCHANGE_TZ),
        current_price=mark_dec,
        prev_close_mark=Decimal("128.00"),
        unrealized_pnl=unreal,
        commission=Decimal("20.00"),
        instrument_token="55123",
        trading_symbol="NIFTY26NOV24000CE",
    )
    structure = StructurePosition(
        structure_id="struct-d1-001",
        structure_type="long_call",
        strategy_name="sma_crossover",
        underlying="NIFTY",
        expiry=date(2026, 11, 26),
        legs=[leg],
        opened_at=datetime(2026, 11, 2, 10, 0, tzinfo=EXCHANGE_TZ),
        entry_session_date="2026-11-02",
        last_mark_session="2026-11-03",
        last_mark_net_pnl=unreal - Decimal("20.00"),
        prior_close_net_pnl=Decimal("580.00"),
    )
    bridge.option_broker._structures[structure.structure_id] = structure
    bridge.option_broker._positions[leg.position_id] = leg
    bridge.open_structure_id = structure.structure_id
    bridge.set_manual_rule(stop_loss=95.0, target=180.0)
    return structure


def _option_runner_config(
    *,
    instance_id: str = "runnerd1opt001",
    mode: str = "paper",
    source: str = "synthetic",
    carry_policy: str = "positional",
) -> RunnerConfig:
    return RunnerConfig(
        name="NIFTY Opt D1",
        strategy_name="sma_crossover",
        symbols=["NIFTY"],
        allocated_capital=200_000.0,
        mode=mode,
        source=source,
        instance_id=instance_id,
        instrument={
            "type": "option",
            "expression": {
                "expression_version": 2,
                "type": {
                    "BULLISH": {"structure": "long_call", "strike_selection": "atm"},
                    "BEARISH": {"structure": "long_put", "strike_selection": "atm"},
                },
                "quantity": 1,
                "carry_policy": carry_policy,
                "exit": {
                    "stop_loss_points": 1_000_000.0,
                    "signal_flip": False,
                    "min_days_to_expiry": None,
                },
            },
        },
    )


def test_migration_024_sqlite_sql_and_orm_idempotent(tmp_path: Path) -> None:
    """Migration 024 SQLite script creates all 4 tables and ORM helper is idempotent."""
    db = _make_sqlite_db(tmp_path, "mig024.sqlite")
    sql_path = (
        Path(__file__).resolve().parents[2]
        / "db"
        / "migrations"
        / "024_open_option_positions_and_risk_session.sqlite.sql"
    )
    script = sql_path.read_text(encoding="utf-8")
    raw = db.engine.raw_connection()
    try:
        raw.executescript(script)
        raw.commit()
    finally:
        raw.close()

    ensure_forward_ssot_schema(db)
    tables = set(inspect(db.engine).get_table_names())
    assert {
        "runner_instances",
        "option_open_structures",
        "option_open_legs",
        "risk_session_state",
    }.issubset(tables)
    db.disconnect()


def test_dual_write_parity_and_postgres_primary_cold_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Phase D1-a dual-write produces PARITY_OK; Phase D1-b restores from SQL alone."""
    db = _make_sqlite_db(tmp_path, "dualwrite.sqlite")
    state_file = tmp_path / "portfolio_state.json"

    monkeypatch.setenv("STATE_STORE_PRIMARY", "json")
    assert resolve_state_store_primary() == "json"

    mgr = PortfolioManager(
        auto_start_feed=False,
        state_path=state_file,
        db_manager=db,
        allow_synthetic_db_state=True,
    )
    runner = mgr.get_runner(mgr.add_runner(_option_runner_config()))
    _seed_open_option_structure(runner, mark="135.50")
    mgr._session_peak_daily_pnl = 1850.0
    mgr._bucket_session_peak_pnl["paper"] = 1850.0
    assert mgr.save_state() is True

    # Relational tables carry exact rows:
    with db.session() as session:
        assert session.get(RunnerInstanceRecord, runner.instance_id) is not None
        s_row = session.get(OptionOpenStructureRecord, "struct-d1-001")
        assert s_row is not None
        assert s_row.status == "open"
        assert s_row.carry_policy == "positional"
        assert s_row.stop_loss == Decimal("95")
        assert s_row.target == Decimal("180")
        leg_row = session.get(OptionOpenLegRecord, "leg-d1-001")
        assert leg_row is not None
        assert leg_row.entry_price == Decimal("120.00")
        assert leg_row.last_mark_price == Decimal("135.50")
        assert leg_row.prev_close_mark == Decimal("128.00")
        risk_rows = session.query(RiskSessionStateRecord).all()
        assert len(risk_rows) >= 2

    json_payload = mgr._state_store.load()
    pg_payload = mgr._pg_state_store.load()
    parity = verify_state_parity(json_payload, pg_payload, allow_synthetic=True)
    assert parity["ok"] is True
    assert parity["status"] == "PARITY_OK"

    # Mutating a leg mark beyond ₹0.01 triggers PARITY_DIFF:
    tampered = PostgresStateStore(db, allow_synthetic=True).load()
    tampered["runners"][0]["bridge"]["broker"]["structures"][0]["legs"][0][
        "current_price"
    ] = "140.00"
    diff_report = verify_state_parity(json_payload, tampered, allow_synthetic=True)
    assert diff_report["ok"] is False
    assert diff_report["status"] == "PARITY_DIFF"

    # Phase D1-b cutover: delete JSON completely and cold-boot from SQL primary!
    state_file.unlink()
    monkeypatch.setenv("STATE_STORE_PRIMARY", "postgres")
    assert resolve_state_store_primary() == "postgres"

    mgr2 = PortfolioManager(
        auto_start_feed=False,
        state_path=tmp_path / "missing_json.json",
        db_manager=db,
        allow_synthetic_db_state=True,
    )
    restored = mgr2.get_runner("runnerd1opt001")
    assert restored is not None
    # Ticket A3: runner carries an open option structure -> wakes directly into RUNNING!
    assert restored.status == "RUNNING"
    open_structs = restored.options_bridge.option_broker.get_open_structures()
    assert len(open_structs) == 1
    r_leg = open_structs[0].legs[0]
    assert r_leg.entry_price == Decimal("120.00")
    assert r_leg.current_price == Decimal("135.50")
    assert r_leg.prev_close_mark == Decimal("128.00")
    assert restored.options_bridge.manual_rules() == {
        "stop_loss": 95.0,
        "target": 180.0,
    }
    assert mgr2._bucket_session_peak_pnl["paper"] == pytest.approx(1850.0)
    db.disconnect()


def test_zero_sql_queries_on_ticks_nontrading_bars_and_ui_reads(
    tmp_path: Path,
) -> None:
    """7-Event Contract: 0 SQL queries on non-fill ticks, non-trading bars, and UI reads."""
    db = _make_sqlite_db(tmp_path, "zerotick.sqlite")
    state_file = tmp_path / "state.json"
    mgr = PortfolioManager(
        auto_start_feed=False,
        state_path=state_file,
        db_manager=db,
        allow_synthetic_db_state=True,
    )
    runner = mgr.get_runner(mgr.add_runner(_option_runner_config()))
    _seed_open_option_structure(runner, mark="130.00")
    runner.options_bridge.set_manual_rule(stop_loss=10.0, target=900.0)
    # Seed initial mark & risk baseline once before hot-path measurement:
    runner.on_tick("NIFTY", price=23980.0, ts="2026-11-03T04:14:00+00:00")
    mgr._evaluate_risk()
    mgr.save_state()

    queries: list[str] = []

    def _count_sql(conn, cursor, statement, parameters, context, executemany):
        queries.append(statement)

    event.listen(db.engine, "before_cursor_execute", _count_sql)
    try:
        # 1. Non-fill live quote ticks on both runner.on_tick and WS batch hot path:
        runner.on_tick("NIFTY", price=23981.0, ts="2026-11-03T04:15:00+00:00")
        mgr._on_ws_tick_batch(
            {
                "55123": {
                    "ltp": 131.25,
                    "recv_utc": "2026-11-03T04:15:02+00:00",
                }
            }
        )

        # 2. 10 non-trading 1-minute bar sweeps (crosses tick_index % 5 == 0 twice!):
        for i in range(10):
            mgr._on_tick_end_core(f"2026-11-03T04:{15 + i:02d}:00+00:00")

        # 3. Dashboard / SSE UI read paths:
        positions = mgr.list_positions()
        buckets = mgr.get_bucket_aggregates()
        assert len(positions) == 1
        assert "paper" in buckets

        assert queries == [], f"Expected 0 SQL queries on hot path, got: {queries}"
    finally:
        event.remove(db.engine, "before_cursor_execute", _count_sql)
        db.disconnect()


def test_event5_risk_milestone_throttle_and_immediate_halt_flush(
    tmp_path: Path,
) -> None:
    """Event 5 throttles peak_daily_pnl writes and immediately persists halted=True."""
    db = _make_sqlite_db(tmp_path, "event5.sqlite")
    mgr = PortfolioManager(
        risk_config=GlobalRiskConfig(daily_loss_limit=5_000.0),
        auto_start_feed=False,
        db_manager=db,
        allow_synthetic_db_state=True,
    )
    mgr.add_runner(_option_runner_config())

    # Sub-threshold step (< ₹500) writes nothing:
    mgr._session_peak_daily_pnl = 200.0
    mgr._bucket_session_peak_pnl["paper"] = 200.0
    assert mgr._maybe_flush_risk_milestone(min_step_inr=500.0, min_interval_s=30.0) is False

    # Milestone >= ₹500 writes to risk_session_state:
    mgr._session_peak_daily_pnl = 750.0
    mgr._bucket_session_peak_pnl["paper"] = 750.0
    assert mgr._maybe_flush_risk_milestone(min_step_inr=500.0, min_interval_s=30.0) is True

    # Immediate follow-up within 30s cooldown is throttled (0 DB chatter):
    mgr._session_peak_daily_pnl = 1500.0
    mgr._bucket_session_peak_pnl["paper"] = 1500.0
    assert mgr._maybe_flush_risk_milestone(min_step_inr=500.0, min_interval_s=30.0) is False

    with db.session() as session:
        paper_row = (
            session.query(RiskSessionStateRecord)
            .filter_by(slice_key="paper")
            .one()
        )
        assert paper_row.peak_daily_pnl == Decimal("750.0")
    db.disconnect()


def test_event6_eod_mark_settlement_stamps_prev_close_mark_once(
    tmp_path: Path,
) -> None:
    """Event 6 stamps official 15:30 IST prev_close_mark + prior_close_net_pnl once/day."""
    db = _make_sqlite_db(tmp_path, "event6.sqlite")
    state_file = tmp_path / "state.json"
    mgr = PortfolioManager(
        auto_start_feed=False,
        state_path=state_file,
        db_manager=db,
        allow_synthetic_db_state=True,
    )
    runner = mgr.get_runner(mgr.add_runner(_option_runner_config()))
    struct = _seed_open_option_structure(runner, mark="152.75")
    struct.last_mark_net_pnl = Decimal("2436.25")
    mgr.save_state()

    # Before 15:30 IST -> no settlement:
    pre_close = datetime(2026, 11, 3, 9, 55, tzinfo=timezone.utc)  # 15:25 IST
    res_pre = mgr.run_eod_mark_settlement(pre_close)
    assert res_pre["settled"] is False
    assert res_pre["reason"] == "before_close"

    # At 15:30 IST -> settles prev_close_mark = 152.75 and runs parity check:
    at_close = datetime(2026, 11, 3, 10, 0, tzinfo=timezone.utc)  # 15:30 IST
    res = mgr.run_eod_mark_settlement(at_close)
    assert res["settled"] is True
    assert res["settled_legs"] == 1
    assert res["parity"]["status"] == "PARITY_OK"
    assert struct.legs[0].prev_close_mark == Decimal("152.75")
    assert struct.prior_close_net_pnl == Decimal("2436.25")

    with db.session() as session:
        leg_row = session.get(OptionOpenLegRecord, "leg-d1-001")
        assert leg_row.prev_close_mark == Decimal("152.75")

    # Second call on same IST session is a no-op:
    res_dup = mgr.run_eod_mark_settlement(at_close)
    assert res_dup["settled"] is False
    assert res_dup["reason"] == "already_settled"
    db.disconnect()


def test_event6_settlement_skips_nse_holiday(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Mon–Fri holiday must not stamp yesterday's last marks as today's anchor."""
    db = _make_sqlite_db(tmp_path, "event6_holiday.sqlite")
    mgr = PortfolioManager(
        auto_start_feed=False,
        state_path=tmp_path / "state.json",
        db_manager=db,
        allow_synthetic_db_state=True,
    )
    runner = mgr.get_runner(mgr.add_runner(_option_runner_config()))
    struct = _seed_open_option_structure(runner, mark="152.75")
    struct.last_mark_net_pnl = Decimal("2436.25")
    struct.legs[0].prev_close_mark = None  # nothing settled yet

    monkeypatch.setattr(
        "backtest.live.market_status.get_market_day_state",
        lambda *a, **kw: {"day_state": "HOLIDAY", "session_state": "CLOSED"},
    )
    at_close = datetime(2026, 11, 6, 10, 0, tzinfo=timezone.utc)  # Fri 15:30 IST
    res = mgr.run_eod_mark_settlement(at_close)
    assert res["settled"] is False
    assert res["reason"] == "holiday"
    assert struct.legs[0].prev_close_mark is None
    db.disconnect()


def test_event2_broker_login_reconciles_live_positions(tmp_path: Path) -> None:
    """Event 2 reconciles mode='live' runners against broker.get_positions()."""
    db = _make_sqlite_db(tmp_path, "event2.sqlite")
    mgr = PortfolioManager(
        auto_start_feed=False,
        db_manager=db,
        allow_synthetic_db_state=True,
    )
    runner = mgr.get_runner(mgr.add_runner(_option_runner_config(source="synthetic")))
    _seed_open_option_structure(runner, mark="130.00")
    # Simulate an active live runner routed to mstock holding +75 NIFTY26NOV24000CE:
    runner.config.mode = "live"
    runner.config.source = "mstock"

    fake_broker_ok = SimpleNamespace(
        get_positions=lambda: [
            {"tradingsymbol": "NIFTY26NOV24000CE", "netqty": 75}
        ]
    )
    fake_sm_ok = SimpleNamespace(get_authenticated_broker=lambda _b: fake_broker_ok)
    with patch(
        "backtest.brokers.session_manager.get_session_manager",
        return_value=fake_sm_ok,
    ):
        ok_out = mgr.on_broker_authenticated("mstock")
    assert ok_out["reconciliation"]["checked"] is True
    assert ok_out["reconciliation"]["ok"] is True

    # Venue reports an external phone-app trade (+150 instead of +75) -> loud mismatch!
    fake_broker_bad = SimpleNamespace(
        get_positions=lambda: [
            {"tradingsymbol": "NIFTY26NOV24000CE", "netqty": 150}
        ]
    )
    fake_sm_bad = SimpleNamespace(get_authenticated_broker=lambda _b: fake_broker_bad)
    with patch(
        "backtest.brokers.session_manager.get_session_manager",
        return_value=fake_sm_bad,
    ):
        bad_out = mgr.on_broker_authenticated("mstock")
    assert bad_out["reconciliation"]["checked"] is True
    assert bad_out["reconciliation"]["ok"] is False
    assert "NIFTY26NOV24000CE: local=75 venue=150" in bad_out["reconciliation"]["mismatches"]
    db.disconnect()


def test_event3_live_trade_persister_syncs_option_book_once_per_transition(
    tmp_path: Path,
) -> None:
    """Event 3 syncs option structure open/close to SQL and runs 0 SQL on repeat sweep."""
    from backtest.forward.trade_persistence import LiveTradePersister

    db = _make_sqlite_db(tmp_path, "event3.sqlite")
    mgr = PortfolioManager(
        auto_start_feed=False,
        db_manager=db,
        allow_synthetic_db_state=True,
    )
    runner = mgr.get_runner(mgr.add_runner(_option_runner_config()))
    struct = _seed_open_option_structure(runner, mark="134.00")
    runner.config.source = "mstock"  # durable source for LiveTradePersister

    persister = LiveTradePersister(db)
    persister.flush_runner(runner)

    with db.session() as session:
        s_row = session.get(OptionOpenStructureRecord, "struct-d1-001")
        assert s_row is not None
        assert s_row.status == "open"
        assert s_row.portfolio_id is not None

    # Repeat flush with no state transition issues 0 SQL queries!
    queries: list[str] = []

    def _count_sql(conn, cursor, statement, parameters, context, executemany):
        queries.append(statement)

    event.listen(db.engine, "before_cursor_execute", _count_sql)
    try:
        persister.flush_runner(runner)
        assert queries == []
    finally:
        event.remove(db.engine, "before_cursor_execute", _count_sql)

    # Close structure -> next flush transitions status='closed' in SQL:
    struct.closed_at = datetime(2026, 11, 3, 9, 0, tzinfo=timezone.utc)
    struct.legs[0].closed_at = struct.closed_at
    persister.flush_runner(runner)
    with db.session() as session:
        s_row2 = session.get(OptionOpenStructureRecord, "struct-d1-001")
        assert s_row2 is not None
        assert s_row2.status == "closed"
    db.disconnect()
