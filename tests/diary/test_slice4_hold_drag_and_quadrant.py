"""Slice 4 — Hold-Duration / Commission-Drag & Payoff Breakeven Quadrant tests.

Covers:
1. Hold-duration bucket boundaries (``<1m``, ``1–5m``, ``5–15m``, ``15–60m``,
   ``60m+``, ``unknown_hold``) and drag bands (``healthy | warning | critical``).
2. Hyperbolic breakeven curve coordinates ($R = (1 - W)/W$) at exact reference points.
3. All 5 deterministic Edge Prescription tags (``CHURN_FRICTION_DRAG``,
   ``POSITIVE_EDGE``, ``WIDEN_TP_OR_CUT_SL``, ``TIGHTEN_ENTRY_FILTER``,
   ``LOW_SAMPLE``), exact paisa reconciliation of ``hold_buckets`` and ``entities``
   against ``DiaryService.get_summary``, and zero fee-split keys (E2).
4. ``GET /api/diary/edge/payoff`` HTTP validation guards and Paper/Live isolation.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from backtest.db.models import Portfolio as PortfolioRow, Trade as TradeRow
from backtest.diary.payoff import (
    build_breakeven_curve_points,
    classify_drag_band,
    classify_hold_bucket,
)
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


class TestHoldDragAndBreakevenHelpers:
    def test_hold_bucket_boundaries(self):
        assert classify_hold_bucket(0) == "lt_1m"
        assert classify_hold_bucket(59.9) == "lt_1m"
        assert classify_hold_bucket(60.0) == "1m_5m"
        assert classify_hold_bucket(299.9) == "1m_5m"
        assert classify_hold_bucket(300.0) == "5m_15m"
        assert classify_hold_bucket(899.9) == "5m_15m"
        assert classify_hold_bucket(900.0) == "15m_60m"
        assert classify_hold_bucket(3599.9) == "15m_60m"
        assert classify_hold_bucket(3600.0) == "ge_60m"
        assert classify_hold_bucket(14_400.0) == "ge_60m"
        assert classify_hold_bucket(None) == "unknown_hold"

    def test_drag_bands_and_hyperbolic_breakeven_curve(self):
        assert classify_drag_band(1000.0, 200.0, 5) == "healthy"   # 20%
        assert classify_drag_band(1000.0, 400.0, 5) == "warning"   # 40%
        assert classify_drag_band(1000.0, 650.0, 5) == "critical"  # 65%
        assert classify_drag_band(-500.0, 120.0, 5) == "critical"  # Gross <= 0 + cost
        assert classify_drag_band(0.0, 0.0, 0) == "none"

        pts = {p["win_rate_pct"]: p["payoff_ratio"] for p in build_breakeven_curve_points(4.0)}
        assert pts[20.0] == pytest.approx(4.0)
        assert pts[50.0] == pytest.approx(1.0)
        assert pts[80.0] == pytest.approx(0.25)
        assert pts[100.0] == pytest.approx(0.0)


class TestSlice4PayoffQuadrantAndPrescriptions:
    def test_all_five_prescriptions_and_paisa_reconciliation(self, sqlite_db):
        class _EmptyMgr:
            def __init__(self, db_mgr):
                self._trade_persister = LiveTradePersister(db_mgr)

            def list_instances(self, mode=None):
                return []

            def get_runner(self, iid):
                return None

        with sqlite_db.session() as session:
            p_churn = PortfolioRow(
                name="Scalp Churner [11111111]",
                initial_capital=Decimal("200000"),
                current_cash=Decimal("199850"),
                mode="paper",
                source="mstock",
            )
            p_win = PortfolioRow(
                name="Trend Winner [22222222]",
                initial_capital=Decimal("200000"),
                current_cash=Decimal("203200"),
                mode="paper",
                source="mstock",
            )
            p_tp = PortfolioRow(
                name="Tight Target [33333333]",
                initial_capital=Decimal("200000"),
                current_cash=Decimal("199600"),
                mode="paper",
                source="mstock",
            )
            p_chop = PortfolioRow(
                name="Choppy Entry [44444444]",
                initial_capital=Decimal("200000"),
                current_cash=Decimal("199200"),
                mode="paper",
                source="mstock",
            )
            p_low = PortfolioRow(
                name="Fresh Probe [55555555]",
                initial_capital=Decimal("200000"),
                current_cash=Decimal("199640"),
                mode="paper",
                source="mstock",
            )
            session.add_all([p_churn, p_win, p_tp, p_chop, p_low])
            session.flush()

            base_t = datetime(2026, 10, 30, 9, 30, 0, tzinfo=EXCHANGE_TZ)

            def _add_trade(p_row, strat, sym, hold_sec, gross, comm):
                ent = base_t
                ext = base_t + timedelta(seconds=hold_sec)
                net = Decimal(str(gross)) - Decimal(str(comm))
                session.add(
                    TradeRow(
                        portfolio_id=p_row.portfolio_id,
                        symbol=sym,
                        strategy_name=strat,
                        direction="long",
                        quantity=Decimal("10"),
                        entry_price=Decimal("100"),
                        exit_price=Decimal("105"),
                        entry_time=ent.astimezone(timezone.utc),
                        exit_time=ext.astimezone(timezone.utc),
                        gross_pnl=Decimal(str(gross)),
                        commission_total=Decimal(str(comm)),
                        net_pnl=net,
                        exit_reason="take_profit" if net > 0 else "stop_loss",
                    )
                )

            # 1) CHURN_FRICTION_DRAG (<1m holds): 5 trades, Gross +40, Comm 70 => Net -30 each
            for i in range(5):
                _add_trade(p_churn, "scalp_v1", f"CHURN_{i}", 35, "40.00", "70.00")

            # 2) POSITIVE_EDGE (60m+ holds): 12 trades (8W @ +500 net, 4L @ -200 net)
            for i in range(8):
                _add_trade(p_win, "orb_v2", f"WIN_{i}", 4200, "540.00", "40.00")
            for i in range(4):
                _add_trade(p_win, "orb_v2", f"WINL_{i}", 3900, "-160.00", "40.00")

            # 3) WIDEN_TP_OR_CUT_SL (5-15m holds): 10 trades (6W @ +100 net, 4L @ -250 net)
            #    W = 60% >= 50%, R = 0.40 < BE 0.667, E = -40 <= 0
            for i in range(6):
                _add_trade(p_tp, "mean_rev", f"TPW_{i}", 480, "130.00", "30.00")
            for i in range(4):
                _add_trade(p_tp, "mean_rev", f"TPL_{i}", 540, "-220.00", "30.00")

            # 4) TIGHTEN_ENTRY_FILTER (1-5m holds): 10 trades (3W @ +200 net, 7L @ -200 net)
            #    W = 30% < 40%, R = 1.00 < BE 2.333, E = -80 <= 0
            for i in range(3):
                _add_trade(p_chop, "breakout_chop", f"CHW_{i}", 180, "230.00", "30.00")
            for i in range(7):
                _add_trade(p_chop, "breakout_chop", f"CHL_{i}", 150, "-170.00", "30.00")

            # 5) LOW_SAMPLE (15-60m holds): 3 trades @ -120 net (Gross -100, Comm 20)
            for i in range(3):
                _add_trade(p_low, "probe_strat", f"PRB_{i}", 1800, "-100.00", "20.00")

        svc = DiaryService(manager=_EmptyMgr(sqlite_db), db_manager=sqlite_db)
        summary = svc.get_summary(
            mode="paper",
            from_date="2026-10-30",
            to_date="2026-10-30",
            today_ist=date(2026, 10, 30),
        )
        payoff = svc.get_edge_payoff(
            mode="paper",
            from_date="2026-10-30",
            to_date="2026-10-30",
            group_by="instance",
            today_ist=date(2026, 10, 30),
        )

        # 1. Hold-bucket sums match Summary Tile to the paisa
        hb = payoff["hold_buckets"]
        ht = payoff["hold_totals"]
        assert ht["trade_count"] == summary["trade_count"] == 40
        assert sum(b["trade_count"] for b in hb) == 40
        assert round(sum(b["gross_pnl"] for b in hb), 2) == pytest.approx(summary["gross_pnl"])
        assert round(sum(b["commission"] for b in hb), 2) == pytest.approx(summary["commission"])
        assert round(sum(b["net_pnl"] for b in hb), 2) == pytest.approx(summary["net_pnl"])

        by_hkey = {b["key"]: b for b in hb}
        assert by_hkey["lt_1m"]["trade_count"] == 5
        assert by_hkey["lt_1m"]["drag_band"] == "critical"
        assert by_hkey["ge_60m"]["trade_count"] == 12
        assert by_hkey["ge_60m"]["drag_band"] == "healthy"

        # 2. Entity quadrant coordinates & all 5 prescriptions verified
        ents = payoff["entities"]
        assert len(ents) == 5
        assert round(sum(e["net_pnl"] for e in ents), 2) == pytest.approx(summary["net_pnl"])

        by_prefix = {e["instance_prefix"]: e for e in ents}
        assert by_prefix["11111111"]["prescription_code"] == "CHURN_FRICTION_DRAG"
        assert by_prefix["22222222"]["prescription_code"] == "POSITIVE_EDGE"
        assert by_prefix["22222222"]["payoff_ratio"] == pytest.approx(2.5)
        assert by_prefix["22222222"]["breakeven_payoff_ratio"] == pytest.approx(0.5)
        assert by_prefix["33333333"]["prescription_code"] == "WIDEN_TP_OR_CUT_SL"
        assert by_prefix["44444444"]["prescription_code"] == "TIGHTEN_ENTRY_FILTER"
        assert by_prefix["55555555"]["prescription_code"] == "LOW_SAMPLE"

        for container in [*hb, ht, *ents]:
            for fk in FORBIDDEN_FEE_SPLIT_KEYS:
                assert fk not in container


class TestSlice4FlaskEndpoint:
    def test_edge_payoff_route_guards_and_isolation(self, sqlite_db):
        reset_portfolio_manager()
        app = create_app(source="synthetic")
        app.config["TESTING"] = True
        mgr = get_portfolio_manager()
        mgr._trade_persister = LiveTradePersister(sqlite_db)

        today_ist = current_session_date()
        iso_d = today_ist.isoformat()
        ent_utc = datetime.combine(
            today_ist, datetime.min.time(), tzinfo=EXCHANGE_TZ
        ).replace(hour=10, minute=0).astimezone(timezone.utc)

        with sqlite_db.session() as session:
            p_paper = PortfolioRow(
                name="Paper Swing [99887766]",
                initial_capital=Decimal("100000"),
                current_cash=Decimal("100400"),
                mode="paper",
                source="mstock",
            )
            session.add(p_paper)
            session.flush()
            session.add(
                TradeRow(
                    portfolio_id=p_paper.portfolio_id,
                    symbol="TCS",
                    strategy_name="swing_ema",
                    direction="long",
                    quantity=Decimal("5"),
                    entry_price=Decimal("3500"),
                    exit_price=Decimal("3600"),
                    entry_time=ent_utc,
                    exit_time=ent_utc + timedelta(minutes=25),
                    gross_pnl=Decimal("500.00"),
                    commission_total=Decimal("40.00"),
                    net_pnl=Decimal("460.00"),
                    exit_reason="take_profit",
                )
            )

        try:
            with app.test_client() as client:
                assert client.get("/api/diary/edge/payoff?mode=combined").status_code == 400
                assert (
                    client.get("/api/diary/edge/payoff?mode=paper&group_by=broker").status_code
                    == 400
                )

                r_ok = client.get(
                    f"/api/diary/edge/payoff?mode=paper&from={iso_d}&to={iso_d}&group_by=instance"
                )
                assert r_ok.status_code == 200
                body = r_ok.get_json()
                assert body["success"] is True
                assert body["hold_totals"]["net_pnl"] == pytest.approx(460.00)
                assert body["entities"][0]["unbounded_payoff"] is True

                r_live = client.get(
                    f"/api/diary/edge/payoff?mode=live&from={iso_d}&to={iso_d}"
                )
                assert r_live.status_code == 200
                assert r_live.get_json()["hold_totals"]["trade_count"] == 0
        finally:
            mgr.shutdown()
            reset_portfolio_manager()
