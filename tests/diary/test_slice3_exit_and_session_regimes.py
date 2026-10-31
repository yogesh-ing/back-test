"""Slice 3 — Exit-Reason Attribution & NSE Intraday Session Regime Matrix tests.

Covers:
1. Canonical exit-reason precedence (``trailing_stop`` & ``time_stop`` never
   shadowed by ``stop_loss``; ``intraday_squareoff`` / ``expiry_settlement`` ->
   ``eod_flat``; ``manual_target`` -> ``take_profit``; ``risk_cap`` -> ``risk_limit``).
2. Exact IST session regime boundaries on aware UTC timestamps across all 5 NSE
   intraday buckets + ``off_hours`` overflow.
3. 2D row/column/grand-total paisa reconciliation against ``DiaryService.get_summary``,
   ``clock='entry'`` vs ``clock='exit'`` bucket shift, ``group_by='strategy'|'instance'``,
   and zero fee-split keys (E2).
4. Deterministic diagnostic callout triggers (``TP_NET_NEGATIVE``,
   ``STOP_LOSS_DOMINANCE``, ``MIDDAY_CHOP_BLEED``, ``REGIME_SWEET_SPOT``).
5. ``GET /api/diary/edge/regimes`` validation guards & Paper/Live book isolation.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from backtest.db.models import Portfolio as PortfolioRow, Trade as TradeRow
from backtest.diary.regimes import canonical_exit_reason, classify_ist_session_regime
from backtest.diary.service import DiaryService
from backtest.forward.portfolio_manager import get_portfolio_manager, reset_portfolio_manager
from backtest.forward.trade_persistence import LiveTradePersister
from backtest.timekeeping import EXCHANGE_TZ, current_session_date
from backtest.web.app import create_app

FORBIDDEN_FEE_SPLIT_KEYS = (
    "brokerage",
    "statutory_fees",
    "stt",
    "gst",
    "stamp_duty",
    "sebi_charges",
)


@pytest.fixture()
def sqlite_db():
    from backtest.db import DatabaseManager
    from backtest.db.config import DatabaseConfig
    from backtest.db.models import Base

    manager = DatabaseManager(DatabaseConfig(url="sqlite://", echo=False))
    manager.connect()
    Base.metadata.create_all(manager.engine)
    yield manager
    manager.disconnect()


class TestExitReasonAndSessionClassifiers:
    def test_canonical_exit_reason_precedence(self):
        assert canonical_exit_reason("trailing_stop") == "trailing_stop"
        assert canonical_exit_reason("trail_hit") == "trailing_stop"
        assert canonical_exit_reason("time_stop") == "time_stop"
        assert canonical_exit_reason("max_bars_reached") == "time_stop"
        assert canonical_exit_reason("intraday_squareoff") == "eod_flat"
        assert canonical_exit_reason("auto_square_off") == "eod_flat"
        assert canonical_exit_reason("expiry_settlement") == "eod_flat"
        assert canonical_exit_reason("eod_flat") == "eod_flat"
        assert canonical_exit_reason("take_profit") == "take_profit"
        assert canonical_exit_reason("manual_target") == "take_profit"
        assert canonical_exit_reason("risk_cap") == "risk_limit"
        assert canonical_exit_reason("emergency_flatten") == "risk_limit"
        assert canonical_exit_reason("stop_loss") == "stop_loss"
        assert canonical_exit_reason("manual_stop_loss") == "stop_loss"
        assert canonical_exit_reason("manual") == "manual"
        assert canonical_exit_reason("signal_flip") == "signal"
        assert canonical_exit_reason(None) == "unspecified"
        assert canonical_exit_reason("") == "unspecified"

    def test_ist_session_boundaries_from_utc_timestamps(self):
        # 09:15:00 IST == 03:45:00 UTC -> opening_drive
        assert classify_ist_session_regime("2026-10-30T03:45:00+00:00") == "opening_drive"
        # 09:44:59 IST == 04:14:59 UTC -> opening_drive
        assert classify_ist_session_regime("2026-10-30T04:14:59+00:00") == "opening_drive"
        # 09:45:00 IST == 04:15:00 UTC -> morning_trend
        assert classify_ist_session_regime("2026-10-30T04:15:00+00:00") == "morning_trend"
        # 11:30:00 IST == 06:00:00 UTC -> midday_chop
        assert classify_ist_session_regime("2026-10-30T06:00:00+00:00") == "midday_chop"
        # 13:30:00 IST == 08:00:00 UTC -> afternoon_gamma
        assert classify_ist_session_regime("2026-10-30T08:00:00+00:00") == "afternoon_gamma"
        # 15:10:00 IST == 09:40:00 UTC -> closing_squareoff
        assert classify_ist_session_regime("2026-10-30T09:40:00+00:00") == "closing_squareoff"
        # 15:30:30 IST == 10:00:30 UTC -> closing_squareoff
        assert classify_ist_session_regime("2026-10-30T10:00:30+00:00") == "closing_squareoff"
        # 18:15:00 IST == 12:45:00 UTC -> off_hours
        assert classify_ist_session_regime("2026-10-30T12:45:00+00:00") == "off_hours"


class TestSlice3MatricesAndCallouts:
    def test_matrices_reconcile_clock_shift_and_callouts(self, sqlite_db):
        class _EmptyMgr:
            def __init__(self, db_mgr):
                self._trade_persister = LiveTradePersister(db_mgr)

            def list_instances(self, mode=None):
                return []

            def get_runner(self, iid):
                return None

        with sqlite_db.session() as session:
            p_a = PortfolioRow(
                name="Scalper Alpha [1111aaaa]",
                initial_capital=Decimal("200000"),
                current_cash=Decimal("198000"),
                mode="paper",
                source="mstock",
            )
            p_b = PortfolioRow(
                name="Trend Beta [2222bbbb]",
                initial_capital=Decimal("200000"),
                current_cash=Decimal("203000"),
                mode="paper",
                source="mstock",
            )
            session.add_all([p_a, p_b])
            session.flush()

            # Strategy 1 (scalp_strangle on p_a):
            # - 2 take_profit trades where Gross > 0 (+30 each = +60) but Commission = 50 each
            #   -> Net = -20 each (-40 total) => triggers TP_NET_NEGATIVE!
            # - 4 stop_loss trades entered in Midday Chop (12:00 IST) and exited in Square-Off
            #   (15:15 IST), each Gross -400, Comm 50, Net -450 (-1,800 total)
            #   => 4/6 = 67% stop_loss -> triggers STOP_LOSS_DOMINANCE &
            #      MIDDAY_CHOP_BLEED (on entry clock)!
            for i in range(2):
                ent = datetime(2026, 10, 30, 10, 10 + i, 0, tzinfo=EXCHANGE_TZ)
                ext = datetime(2026, 10, 30, 10, 20 + i, 0, tzinfo=EXCHANGE_TZ)
                session.add(
                    TradeRow(
                        portfolio_id=p_a.portfolio_id,
                        symbol=f"NIFTY_TP_{i}",
                        strategy_name="scalp_strangle",
                        direction="short",
                        quantity=Decimal("65"),
                        entry_price=Decimal("100"),
                        exit_price=Decimal("99.54"),
                        entry_time=ent.astimezone(timezone.utc),
                        exit_time=ext.astimezone(timezone.utc),
                        gross_pnl=Decimal("30.00"),
                        commission_total=Decimal("50.00"),
                        net_pnl=Decimal("-20.00"),
                        exit_reason="take_profit",
                    )
                )

            for i in range(4):
                ent = datetime(2026, 10, 30, 12, 5 + i, 0, tzinfo=EXCHANGE_TZ)
                ext = datetime(2026, 10, 30, 15, 15 + i, 0, tzinfo=EXCHANGE_TZ)
                session.add(
                    TradeRow(
                        portfolio_id=p_a.portfolio_id,
                        symbol=f"NIFTY_SL_{i}",
                        strategy_name="scalp_strangle",
                        direction="short",
                        quantity=Decimal("65"),
                        entry_price=Decimal("100"),
                        exit_price=Decimal("106.15"),
                        entry_time=ent.astimezone(timezone.utc),
                        exit_time=ext.astimezone(timezone.utc),
                        gross_pnl=Decimal("-400.00"),
                        commission_total=Decimal("50.00"),
                        net_pnl=Decimal("-450.00"),
                        exit_reason="stop_loss",
                    )
                )

            # Strategy 2 (orb_trend on p_b):
            # - 3 winning morning_trend trades (10:00 -> 11:00 IST),
            #   each Gross +900, Comm 40, Net +860 => triggers REGIME_SWEET_SPOT!
            for i in range(3):
                ent = datetime(2026, 10, 30, 10, 0 + i, 0, tzinfo=EXCHANGE_TZ)
                ext = datetime(2026, 10, 30, 11, 0 + i, 0, tzinfo=EXCHANGE_TZ)
                session.add(
                    TradeRow(
                        portfolio_id=p_b.portfolio_id,
                        symbol=f"RELIANCE_{i}",
                        strategy_name="orb_trend",
                        direction="long",
                        quantity=Decimal("20"),
                        entry_price=Decimal("1300"),
                        exit_price=Decimal("1345"),
                        entry_time=ent.astimezone(timezone.utc),
                        exit_time=ext.astimezone(timezone.utc),
                        gross_pnl=Decimal("900.00"),
                        commission_total=Decimal("40.00"),
                        net_pnl=Decimal("860.00"),
                        exit_reason="trailing_stop",
                    )
                )

        svc = DiaryService(manager=_EmptyMgr(sqlite_db), db_manager=sqlite_db)
        summary = svc.get_summary(
            mode="paper",
            from_date="2026-10-30",
            to_date="2026-10-30",
            today_ist=date(2026, 10, 30),
        )
        reg_entry = svc.get_edge_regimes(
            mode="paper",
            from_date="2026-10-30",
            to_date="2026-10-30",
            clock="entry",
            group_by="strategy",
            today_ist=date(2026, 10, 30),
        )

        # 1. Both Matrix A & Matrix B grand totals reconcile to Summary Tile to the paisa
        for mat_name in ("exit_matrix", "session_matrix"):
            mat = reg_entry[mat_name]
            gt = mat["grand_totals"]
            assert gt["trade_count"] == summary["trade_count"] == 9
            assert gt["gross_pnl"] == pytest.approx(summary["gross_pnl"])
            assert gt["commission"] == pytest.approx(summary["commission"])
            assert gt["net_pnl"] == pytest.approx(summary["net_pnl"])

            # Row sums == Row totals & Column sums == Grand total
            for r in mat["rows"]:
                assert round(
                    sum(c["net_pnl"] for c in r["cells"].values()), 2
                ) == pytest.approx(r["totals"]["net_pnl"])
                for c in r["cells"].values():
                    for fk in FORBIDDEN_FEE_SPLIT_KEYS:
                        assert fk not in c
            assert round(
                sum(c["net_pnl"] for c in mat["column_totals"].values()), 2
            ) == pytest.approx(gt["net_pnl"])

        # 2. Entry vs Exit clock shifts the 4 midday->squareoff trades cleanly
        scalp_entry = {
            r["row_key"]: r for r in reg_entry["session_matrix"]["rows"]
        }["scalp_strangle"]
        assert scalp_entry["cells"]["midday_chop"]["trade_count"] == 4
        assert scalp_entry["cells"]["closing_squareoff"]["trade_count"] == 0

        reg_exit = svc.get_edge_regimes(
            mode="paper",
            from_date="2026-10-30",
            to_date="2026-10-30",
            clock="exit",
            group_by="instance",
            today_ist=date(2026, 10, 30),
        )
        scalp_inst_exit = {
            r["strategy_name"]: r for r in reg_exit["session_matrix"]["rows"]
        }["scalp_strangle"]
        assert "1111aaaa" in scalp_inst_exit["row_label"]
        assert scalp_inst_exit["cells"]["midday_chop"]["trade_count"] == 0
        assert scalp_inst_exit["cells"]["closing_squareoff"]["trade_count"] == 4

        # 3. Deterministic callouts fired on reg_entry
        codes = {c["code"] for c in reg_entry["callouts"]}
        assert "TP_NET_NEGATIVE" in codes
        assert "STOP_LOSS_DOMINANCE" in codes
        assert "MIDDAY_CHOP_BLEED" in codes
        assert "REGIME_SWEET_SPOT" in codes


class TestSlice3FlaskEndpoint:
    def test_edge_regimes_route_validation_and_book_isolation(self, sqlite_db):
        reset_portfolio_manager()
        app = create_app(source="synthetic")
        app.config["TESTING"] = True
        mgr = get_portfolio_manager()
        mgr._trade_persister = LiveTradePersister(sqlite_db)

        today_ist = current_session_date()
        iso_d = today_ist.isoformat()
        exit_utc = datetime.combine(
            today_ist, datetime.min.time(), tzinfo=EXCHANGE_TZ
        ).replace(hour=10, minute=30).astimezone(timezone.utc)

        with sqlite_db.session() as session:
            p_paper = PortfolioRow(
                name="Paper One [abcd1234]",
                initial_capital=Decimal("100000"),
                current_cash=Decimal("100200"),
                mode="paper",
                source="mstock",
            )
            session.add(p_paper)
            session.flush()
            session.add(
                TradeRow(
                    portfolio_id=p_paper.portfolio_id,
                    symbol="INFY",
                    strategy_name="orb_breakout",
                    direction="long",
                    quantity=Decimal("10"),
                    entry_price=Decimal("1500"),
                    exit_price=Decimal("1525"),
                    entry_time=exit_utc,
                    exit_time=exit_utc,
                    gross_pnl=Decimal("250.00"),
                    commission_total=Decimal("30.00"),
                    net_pnl=Decimal("220.00"),
                    exit_reason="take_profit",
                )
            )

        try:
            with app.test_client() as client:
                assert client.get("/api/diary/edge/regimes?mode=both").status_code == 400
                assert client.get("/api/diary/edge/regimes?mode=paper&clock=utc").status_code == 400
                assert (
                    client.get("/api/diary/edge/regimes?mode=paper&group_by=symbol").status_code
                    == 400
                )

                r_ok = client.get(
                    f"/api/diary/edge/regimes?mode=paper&from={iso_d}&to={iso_d}"
                    "&clock=entry&group_by=instance"
                )
                assert r_ok.status_code == 200
                body = r_ok.get_json()
                assert body["success"] is True
                assert body["exit_matrix"]["grand_totals"]["net_pnl"] == pytest.approx(220.00)
                assert body["session_matrix"]["grand_totals"]["net_pnl"] == pytest.approx(220.00)

                r_live = client.get(
                    f"/api/diary/edge/regimes?mode=live&from={iso_d}&to={iso_d}"
                )
                assert r_live.status_code == 200
                assert r_live.get_json()["exit_matrix"]["grand_totals"]["trade_count"] == 0
        finally:
            mgr.shutdown()
            reset_portfolio_manager()
