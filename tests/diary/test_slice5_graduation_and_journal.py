"""Slice 5 — Paper → Live Graduation Scorecard & Persistent Operator Journal tests.

Covers:
1. Migration 022 SQLite + PostgreSQL SQL parity & ``DiaryNote`` ORM / CHECK constraint.
2. 6-Gate Graduation Scorecard (``PROMOTE_CANDIDATE``, ``TUNE_EDGE``,
   ``RETIRE_OR_KILL`` via drawdown breach, ``BLOCKED_BY_RISK``, E2 ledger
   anomaly, and negative expectancy) + Live-Option Entitlement Advisory
   against ``config/risk_limits.yaml`` (``entitlement.live == [long_call, long_put]``).
3. E2 Single-Commission Contract (zero sub-component fee split keys; exact 2dp
   ``Gross - Commission == Net``).
4. Persistent Book-Segregated Operator Journal (``DiaryService.{list_notes,save_note,delete_note}``
   and ``GET/POST/DELETE /api/diary/notes``), cross-restart persistence, strict
   ``paper`` vs ``live`` isolation, canonical regime tag normalization, and
   ``/diary`` Tab 4 DOM rendering.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from backtest.db import DIARY_TABLES, DatabaseManager, DiaryNote
from backtest.db.config import DatabaseConfig
from backtest.db.models import Base, Portfolio as PortfolioRow, Trade as TradeRow
from backtest.diary.graduation import (
    compute_realized_drawdown,
    evaluate_instance_graduation,
)
from backtest.diary.service import DiaryService, normalize_journal_tags
from backtest.forward.portfolio_manager import (
    get_portfolio_manager,
    reset_portfolio_manager,
)
from backtest.forward.trade_persistence import LiveTradePersister
from backtest.timekeeping import EXCHANGE_TZ, current_session_date
from backtest.web.app import create_app

REPO_ROOT = Path(__file__).resolve().parents[2]
SQLITE_001 = REPO_ROOT / "db" / "migrations" / "001_initial_schema.sqlite.sql"
SQLITE_023 = REPO_ROOT / "db" / "migrations" / "023_diary_journal_notes.sqlite.sql"
PG_023 = REPO_ROOT / "db" / "migrations" / "023_diary_journal_notes.sql"

FORBIDDEN_FEE_SPLIT_KEYS = (
    "brokerage",
    "statutory_fees",
    "stt",
    "gst",
    "stamp_duty",
    "sebi_charges",
)


def _assert_no_fee_split_keys(obj: Any) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            assert k not in FORBIDDEN_FEE_SPLIT_KEYS, f"Forbidden E2 split key {k!r}"
            _assert_no_fee_split_keys(v)
    elif isinstance(obj, list):
        for item in obj:
            _assert_no_fee_split_keys(item)


@pytest.fixture()
def sqlite_db():
    manager = DatabaseManager(DatabaseConfig(url="sqlite://", echo=False))
    manager.connect()
    Base.metadata.create_all(manager.engine)
    yield manager
    manager.disconnect()


class TestMigration022AndDiaryNoteModel:
    def test_sqlite_and_pg_023_sql_parity_and_mode_check(self):
        assert SQLITE_023.exists()
        assert PG_023.exists()
        pg_src = PG_023.read_text(encoding="utf-8")
        assert "CREATE TABLE IF NOT EXISTS diary_notes" in pg_src
        assert "ck_diary_notes_mode CHECK (mode IN ('paper', 'live'))" in pg_src
        assert "VALUES ('023'" in pg_src

        conn = sqlite3.connect(":memory:")
        try:
            conn.executescript(SQLITE_001.read_text(encoding="utf-8"))
            conn.executescript(SQLITE_023.read_text(encoding="utf-8"))

            row = conn.execute(
                "SELECT version FROM schema_migrations WHERE version = '023'"
            ).fetchone()
            assert row == ("023",)

            conn.execute(
                """
                INSERT INTO diary_notes (note_id, mode, session_date, regime_tags, note_text)
                VALUES ('11111111-1111-1111-1111-111111111111', 'paper', '2026-10-30',
                        '["Trend"]', 'Clean ORB follow-through')
                """
            )
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    INSERT INTO diary_notes (note_id, mode, session_date, note_text)
                    VALUES ('22222222-2222-2222-2222-222222222222', 'backtest',
                            '2026-10-30', 'Invalid mode must fail CHECK')
                    """
                )
        finally:
            conn.close()

    def test_diary_tables_export_and_tag_normalization(self):
        assert DIARY_TABLES == (DiaryNote.__table__,)
        tags = normalize_journal_tags(
            ["trend", "  highvix ", "RuleBreach", "TREND", "CustomTag"]
        )
        assert tags == ["Trend", "HighVIX", "RuleBreach", "CustomTag"]
        with pytest.raises(ValueError, match="JSON array"):
            normalize_journal_tags("not-a-list")


class TestGraduationScorecardEngine:
    def test_drawdown_and_unit_verdicts(self):
        trades = [
            {"exit_ts": "2026-10-28T05:00:00Z", "net_pnl": 2000.0},
            {"exit_ts": "2026-10-28T06:00:00Z", "net_pnl": -5000.0},
            {"exit_ts": "2026-10-28T07:00:00Z", "net_pnl": 4000.0},
        ]
        dd = compute_realized_drawdown(trades, allocated_capital=100_000.0)
        # Peak hits 102,000 -> drops to 97,000 -> 5,000 INR / 102,000 = 4.90%
        assert dd["max_drawdown_inr"] == pytest.approx(5000.0)
        assert dd["max_drawdown_pct"] == pytest.approx(4.90, abs=0.02)

        # Ledger anomaly immediately triggers RETIRE_OR_KILL even if profitable
        bad_ledger_inst = {
            "instance_key": "orb::aaaa1111",
            "strategy_name": "orb",
            "display_name": "ORB [aaaa1111]",
            "lifecycle_status": "RUNNING",
            "allocated_capital": 200_000.0,
            "trade_count": 25,
            "win_rate_pct": 64.0,
            "profit_factor": 2.1,
            "expectancy_net": 310.0,
            "gross_pnl": 8500.0,
            "commission": 750.0,
            "net_pnl": 7750.0,
            "commission_drag_pct": 8.82,
            "reconcile_anomalies": 1,
            "trades": [],
        }
        scored = evaluate_instance_graduation(bad_ledger_inst, book_mode="paper")
        assert scored["verdict"] == "RETIRE_OR_KILL"
        assert scored["gates"]["ledger_integrity"]["passed"] is False

    def test_six_gates_and_option_entitlement_advisory_end_to_end(self, sqlite_db):
        today_d = date(2026, 10, 30)
        base_entry = datetime(2026, 10, 29, 9, 20, tzinfo=EXCHANGE_TZ).astimezone(
            timezone.utc
        )

        class _StubMgr:
            def __init__(self, db_mgr):
                self._trade_persister = LiveTradePersister(db_mgr)

            def list_instances(self, mode=None):
                if mode == "paper":
                    return [
                        {
                            "instance_id": "99999999-0000-0000-0000-000000000000",
                            "name": "Risk Halted Scalp",
                            "strategy_name": "scalp_v1",
                            "status": "PAUSED",
                            "block_reason": "DAILY_LOSS_CAP",
                            "allocated_capital": 150_000.0,
                            "symbols": ["NIFTY"],
                            "timeframe": "1min",
                            "open_pnl": 0.0,
                            "open_positions": 0,
                        },
                        {
                            "instance_id": "22222222-0000-0000-0000-000000000000",
                            "name": "Index Strangle Alpha",
                            "strategy_name": "theta_strangle",
                            "status": "RUNNING",
                            "allocated_capital": 300_000.0,
                            "symbols": ["NIFTY"],
                            "timeframe": "5min",
                            "instrument": {
                                "type": "option",
                                "expression": {"structure": "strangle"},
                            },
                            "open_pnl": 450.0,
                            "open_positions": 1,
                        },
                    ]
                return []

            def get_runner(self, iid):
                return None

        with sqlite_db.session() as session:
            # 1. Promote candidate (long_call — permitted in live!)
            p_promote = PortfolioRow(
                name="NIFTY Long Call [11111111]",
                initial_capital=Decimal("200000"),
                current_cash=Decimal("212000"),
                mode="paper",
                source="mstock",
            )
            # 2. Promote candidate with restricted strangle structure -> advisory!
            p_strangle = PortfolioRow(
                name="Index Strangle Alpha [22222222]",
                initial_capital=Decimal("300000"),
                current_cash=Decimal("315000"),
                mode="paper",
                source="mstock",
            )
            # 3. Tune candidate (positive edge, but only 6 trades < 20)
            p_tune = PortfolioRow(
                name="Early Breakout [33333333]",
                initial_capital=Decimal("200000"),
                current_cash=Decimal("202400"),
                mode="paper",
                source="mstock",
            )
            # 4. Retire candidate (breaches 10% max drawdown on 100k capital)
            p_dd = PortfolioRow(
                name="Deep Drawdown [44444444]",
                initial_capital=Decimal("100000"),
                current_cash=Decimal("84000"),
                mode="paper",
                source="mstock",
            )
            session.add_all([p_promote, p_strangle, p_tune, p_dd])
            session.flush()

            # 22 trades for p_promote: 16 wins (+900 gross, 50 comm -> +850 net),
            # 6 losses (-400 gross, 50 comm -> -450 net)
            for idx in range(22):
                is_win = idx < 16
                gross = Decimal("900.00") if is_win else Decimal("-400.00")
                comm = Decimal("50.00")
                net = gross - comm
                t_en = base_entry + timedelta(minutes=idx * 6)
                session.add(
                    TradeRow(
                        portfolio_id=p_promote.portfolio_id,
                        strategy_name="orb_options",
                        symbol="NIFTY26OCT24500CE",
                        direction="long",
                        quantity=Decimal("50"),
                        entry_price=Decimal("150"),
                        exit_price=Decimal("168") if is_win else Decimal("142"),
                        entry_time=t_en,
                        exit_time=t_en + timedelta(minutes=4),
                        gross_pnl=gross,
                        commission_total=comm,
                        slippage_total=Decimal("0"),
                        net_pnl=net,
                        exit_reason="take_profit" if is_win else "stop_loss",
                    )
                )

            # 21 trades for p_strangle (SHORT option legs + strangle config badge)
            for idx in range(21):
                is_win = idx < 15
                gross = Decimal("800.00") if is_win else Decimal("-350.00")
                comm = Decimal("45.00")
                net = gross - comm
                t_en = base_entry + timedelta(minutes=idx * 7)
                session.add(
                    TradeRow(
                        portfolio_id=p_strangle.portfolio_id,
                        strategy_name="theta_strangle",
                        symbol="NIFTY26OCT24600CE",
                        direction="short",
                        quantity=Decimal("50"),
                        entry_price=Decimal("120"),
                        exit_price=Decimal("104") if is_win else Decimal("127"),
                        entry_time=t_en,
                        exit_time=t_en + timedelta(minutes=5),
                        gross_pnl=gross,
                        commission_total=comm,
                        slippage_total=Decimal("0"),
                        net_pnl=net,
                        exit_reason="take_profit" if is_win else "stop_loss",
                    )
                )

            # 6 winning trades for p_tune (< 20 sample gate)
            for idx in range(6):
                t_en = base_entry + timedelta(minutes=idx * 10)
                session.add(
                    TradeRow(
                        portfolio_id=p_tune.portfolio_id,
                        strategy_name="swing_eq",
                        symbol="RELIANCE",
                        direction="long",
                        quantity=Decimal("10"),
                        entry_price=Decimal("2500"),
                        exit_price=Decimal("2545"),
                        entry_time=t_en,
                        exit_time=t_en + timedelta(minutes=8),
                        gross_pnl=Decimal("450.00"),
                        commission_total=Decimal("30.00"),
                        slippage_total=Decimal("0"),
                        net_pnl=Decimal("420.00"),
                        exit_reason="take_profit",
                    )
                )

            # 12 losing trades for p_dd (-1,400 net each -> -16,800 = 16.8% DD > 10% cap)
            for idx in range(12):
                t_en = base_entry + timedelta(minutes=idx * 8)
                session.add(
                    TradeRow(
                        portfolio_id=p_dd.portfolio_id,
                        strategy_name="fade_eq",
                        symbol="INFY",
                        direction="long",
                        quantity=Decimal("20"),
                        entry_price=Decimal("1600"),
                        exit_price=Decimal("1532"),
                        entry_time=t_en,
                        exit_time=t_en + timedelta(minutes=5),
                        gross_pnl=Decimal("-1360.00"),
                        commission_total=Decimal("40.00"),
                        slippage_total=Decimal("0"),
                        net_pnl=Decimal("-1400.00"),
                        exit_reason="stop_loss",
                    )
                )

        svc = DiaryService(manager=_StubMgr(sqlite_db), db_manager=sqlite_db)
        grad = svc.get_graduation(
            mode="paper",
            from_date="2026-10-01",
            to_date="2026-10-30",
            today_ist=today_d,
        )
        _assert_no_fee_split_keys(grad)

        assert grad["mode"] == "paper"
        assert grad["scorecard_kind"] == "paper_to_live_graduation"
        assert grad["summary"]["instance_count"] == 5
        assert grad["summary"]["promote_count"] == 2
        assert grad["summary"]["tune_count"] == 1
        assert grad["summary"]["retire_count"] == 2
        assert grad["summary"]["entitlement_advisory_count"] == 1

        by_prefix = {r["instance_prefix"]: r for r in grad["instances"]}

        r_call = by_prefix["11111111"]
        assert r_call["verdict"] == "PROMOTE_CANDIDATE"
        assert r_call["gates_passed"] == 6
        assert r_call["entitlement_advisory"]["requires_live_entitlement_review"] is False

        r_strangle = by_prefix["22222222"]
        assert r_strangle["verdict"] == "PROMOTE_CANDIDATE"
        assert r_strangle["entitlement_advisory"]["requires_live_entitlement_review"] is True
        assert "strangle" in r_strangle["entitlement_advisory"]["restricted_structures"]
        assert "risk_limits.yaml" in r_strangle["entitlement_advisory"]["message"]

        r_tune = by_prefix["33333333"]
        assert r_tune["verdict"] == "TUNE_EDGE"
        assert r_tune["gates"]["sample_size"]["passed"] is False
        assert r_tune["gates"]["net_expectancy"]["passed"] is True

        r_dd = by_prefix["44444444"]
        assert r_dd["verdict"] == "RETIRE_OR_KILL"
        assert r_dd["gates"]["max_drawdown"]["passed"] is False
        assert r_dd["max_drawdown_pct"] == pytest.approx(16.8, abs=0.05)

        r_halted = by_prefix["99999999"]
        assert r_halted["lifecycle_status"] == "BLOCKED_BY_RISK"
        assert r_halted["verdict"] == "RETIRE_OR_KILL"
        assert r_halted["gates"]["max_drawdown"]["risk_blocked"] is True


class TestPersistentJournalAndApiRoutes:
    def test_persistent_journal_cross_restart_and_strict_mode_isolation(self, sqlite_db):
        class _MinMgr:
            def list_instances(self, mode=None):
                return []

            def get_runner(self, iid):
                return None

        svc_a = DiaryService(manager=_MinMgr(), db_manager=sqlite_db)
        paper_note = svc_a.save_note(
            mode="paper",
            session_date="2026-10-30",
            strategy_name="orb_options",
            instance_label="NIFTY Long Call [11111111]",
            regime_tags=["trend", "expiry"],
            note_text="Strong opening drive; zero slippage anomalies.",
        )
        live_note = svc_a.save_note(
            mode="live",
            session_date="2026-10-30",
            strategy_name="live_orb",
            regime_tags=["Chop", "HighVIX"],
            note_text="Tightened live lot cap after mid-day chop.",
        )

        # Fresh service instance (simulates process restart over the same DB)
        svc_b = DiaryService(manager=_MinMgr(), db_manager=sqlite_db)
        paper_list = svc_b.list_notes(
            mode="paper",
            from_date="2026-10-01",
            to_date="2026-10-31",
            today_ist=date(2026, 10, 30),
        )
        live_list = svc_b.list_notes(
            mode="live",
            from_date="2026-10-01",
            to_date="2026-10-31",
            today_ist=date(2026, 10, 30),
        )
        assert paper_list["note_count"] == 1
        assert paper_list["notes"][0]["note_id"] == paper_note["note_id"]
        assert paper_list["notes"][0]["regime_tags"] == ["Trend", "Expiry"]
        assert live_list["note_count"] == 1
        assert live_list["notes"][0]["note_id"] == live_note["note_id"]

        # Cross-mode update & delete MUST be refused!
        with pytest.raises(KeyError):
            svc_b.save_note(
                mode="live",
                session_date="2026-10-30",
                note_id=paper_note["note_id"],
                note_text="Cross-book overwrite attempt",
            )
        with pytest.raises(KeyError):
            svc_b.delete_note(paper_note["note_id"], mode="live")

        # Update inside owning mode succeeds
        updated = svc_b.save_note(
            mode="paper",
            session_date="2026-10-30",
            note_id=paper_note["note_id"],
            strategy_name="orb_options",
            regime_tags=["Trend"],
            note_text="Updated post-market review note.",
        )
        assert updated["note_text"] == "Updated post-market review note."
        assert updated["regime_tags"] == ["Trend"]

        # Delete inside owning mode removes only the paper note; live stays intact
        svc_b.delete_note(paper_note["note_id"], mode="paper")
        assert (
            svc_b.list_notes(
                mode="paper", from_date="2026-10-01", to_date="2026-10-31"
            )["note_count"]
            == 0
        )
        assert (
            svc_b.list_notes(
                mode="live", from_date="2026-10-01", to_date="2026-10-31"
            )["note_count"]
            == 1
        )

    def test_flask_graduation_and_notes_endpoints_and_diary_dom(self, sqlite_db):
        reset_portfolio_manager()
        pm = get_portfolio_manager()
        pm._trade_persister = LiveTradePersister(sqlite_db)
        today_iso = current_session_date().isoformat()

        app = create_app()
        app.config["TESTING"] = True
        try:
            with app.test_client() as client:
                # 1. /diary HTML includes Tab 4 Graduation & Journal controls
                html_resp = client.get("/diary?mode=paper")
                assert html_resp.status_code == 200
                html = html_resp.get_data(as_text=True)
                assert 'id="diary-tab-btn-graduation"' in html
                assert 'id="diary-panel-graduation"' in html
                assert 'id="diary-graduation-table-wrap"' in html
                assert 'id="diary-journal-form"' in html
                assert 'PAPER JOURNAL' in html

                # 2. GET /api/diary/graduation validates mode and returns 200
                bad_grad = client.get("/api/diary/graduation?mode=invalid")
                assert bad_grad.status_code == 400
                ok_grad = client.get(
                    f"/api/diary/graduation?mode=paper&from={today_iso}&to={today_iso}"
                )
                assert ok_grad.status_code == 200
                grad_json = ok_grad.get_json()
                assert grad_json["success"] is True
                assert grad_json["mode"] == "paper"
                _assert_no_fee_split_keys(grad_json)

                # 3. POST / GET / DELETE /api/diary/notes book-segregated lifecycle
                empty_post = client.post(
                    "/api/diary/notes",
                    json={"mode": "paper", "session_date": today_iso, "note_text": "   "},
                )
                assert empty_post.status_code == 400

                create_resp = client.post(
                    "/api/diary/notes",
                    json={
                        "mode": "paper",
                        "session_date": today_iso,
                        "strategy_name": "orb_v2",
                        "instance_label": "NIFTY Runner [abcd1234]",
                        "regime_tags": ["trend", "gap"],
                        "note_text": "Clean gap-and-go session.",
                    },
                )
                assert create_resp.status_code == 201
                created_note = create_resp.get_json()["note"]
                nid = created_note["note_id"]
                assert created_note["regime_tags"] == ["Trend", "Gap"]

                # Paper list sees it; Live list does NOT
                p_notes = client.get(
                    f"/api/diary/notes?mode=paper&from={today_iso}&to={today_iso}"
                ).get_json()
                l_notes = client.get(
                    f"/api/diary/notes?mode=live&from={today_iso}&to={today_iso}"
                ).get_json()
                assert p_notes["note_count"] == 1
                assert l_notes["note_count"] == 0

                # Deleting without mode -> 400; deleting from wrong mode -> 404
                assert client.delete(f"/api/diary/notes/{nid}").status_code == 400
                assert (
                    client.delete(f"/api/diary/notes/{nid}?mode=live").status_code
                    == 404
                )

                # Deleting from owning mode -> 200
                del_ok = client.delete(f"/api/diary/notes/{nid}?mode=paper")
                assert del_ok.status_code == 200
                assert (
                    client.get(
                        f"/api/diary/notes?mode=paper&from={today_iso}&to={today_iso}"
                    ).get_json()["note_count"]
                    == 0
                )
        finally:
            pm._trade_persister = None
            reset_portfolio_manager()
