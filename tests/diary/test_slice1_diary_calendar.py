"""Slice 1 — PnL Diary Route, Paper/Live Segregation & IST Session Calendar tests.

Covers:
1. Strict ``Paper | Live`` segregation on ``/api/diary/calendar`` and ``/api/diary/summary``
   (plus HTTP 400 on invalid ``mode`` or malformed/excessive date windows).
2. IST midnight boundary attribution (``00:00–05:29 IST`` UTC seam lands on IST day,
   never previous UTC date).
3. 30-Oct-2026 13-trade benchmark parity (``net_pnl == -10520.51``), RAM/DB dedupe,
   exact ``Gross - Commission = Net`` with single ``commission`` sum (zero fee-split keys).
4. ``reconcile_anomalies`` surfacing when a corrupted row violates ``Gross - Commission = Net``.
5. NSE holiday awareness (``NSE_HOLIDAYS_2026`` + ``MarketHoliday`` DB table), weekend
   marking, and Monday-anchored weekly rollups matching monthly totals.
6. Today-only ``running_mtm`` vs historical closed ranges, plus strategy filter scoping.
7. ``/diary`` DOM contracts & ``/portfolio/paper`` + ``/portfolio/live`` deep-links.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from backtest.db.models import MarketHoliday, Portfolio as PortfolioRow, Trade as TradeRow
from backtest.diary.service import DiaryService, parse_ist_date_range, validate_diary_mode
from backtest.forward.portfolio_manager import get_portfolio_manager, reset_portfolio_manager
from backtest.forward.trade_persistence import LiveTradePersister
from backtest.timekeeping import EXCHANGE_TZ, current_session_date
from backtest.web.app import create_app


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


class _StubConfig:
    def __init__(
        self,
        name: str,
        strategy_name: str,
        mode: str = "paper",
        capital: float = 200_000.0,
    ) -> None:
        self.name = name
        self.strategy_name = strategy_name
        self.mode = mode
        self.allocated_capital = capital
        self.source = "mstock"


class _StubRunner:
    def __init__(
        self,
        instance_id: str,
        name: str,
        strategy_name: str,
        mode: str = "paper",
        closed_trades: list[dict] | None = None,
        unrealized_pnl: float = 0.0,
        open_positions: list[dict] | None = None,
        day_start_equity: float = 200_000.0,
    ) -> None:
        self.instance_id = instance_id
        self.config = _StubConfig(name=name, strategy_name=strategy_name, mode=mode)
        self._closed_trades = list(closed_trades or [])
        self._unrealized_pnl = unrealized_pnl
        self._open_positions = list(open_positions or [])
        self._day_start_equity = day_start_equity

    @property
    def closed_trades(self) -> list[dict]:
        return self._closed_trades

    def get_state(self) -> dict:
        return {
            "instance_id": self.instance_id,
            "name": self.config.name,
            "strategy_name": self.config.strategy_name,
            "mode": self.config.mode,
            "allocated_capital": self.config.allocated_capital,
            "day_start_equity": self._day_start_equity,
            "open_pnl": self._unrealized_pnl,
            "open_positions": len(self._open_positions),
        }


class TestDiaryValidationHelpers:
    def test_validate_diary_mode_strict_paper_or_live(self):
        assert validate_diary_mode("paper") == "paper"
        assert validate_diary_mode(" LIVE ") == "live"
        for bad in ("", "both", "all", "combined", "backtest", None):
            with pytest.raises(ValueError, match="mode"):
                validate_diary_mode(bad)

    def test_parse_ist_date_range_guards(self):
        d1, d2 = parse_ist_date_range(
            "2026-10-01", "2026-10-30", today_ist=date(2026, 10, 30)
        )
        assert d1 == date(2026, 10, 1)
        assert d2 == date(2026, 10, 30)

        with pytest.raises(ValueError, match="precedes"):
            parse_ist_date_range("2026-10-30", "2026-10-01")

        with pytest.raises(ValueError, match="400"):
            parse_ist_date_range("2025-01-01", "2026-10-30")

        with pytest.raises(ValueError, match="Invalid"):
            parse_ist_date_range("not-a-date", "2026-10-30")


class TestSlice1DiaryServiceAndBenchmark:
    def test_oct30_13_trade_benchmark_and_ist_midnight_seam(self, sqlite_db):
        """13 closed trades on 30-Oct IST summing to exact net = -10,520.51 with RAM overlap."""
        with sqlite_db.session() as session:
            p_paper = PortfolioRow(
                name="Strangle 30Oct [deadbeef]",
                initial_capital=Decimal("300000"),
                current_cash=Decimal("289479.49"),
                mode="paper",
                source="mstock",
            )
            p_live = PortfolioRow(
                name="Live Hedge [cafe1234]",
                initial_capital=Decimal("150000"),
                current_cash=Decimal("152100.00"),
                mode="live",
                source="mstock",
            )
            session.add_all([p_paper, p_live])
            session.flush()

            # Build 13 paper trades on 2026-10-30 IST whose net_pnl sums to -10,520.51:
            # 12 trades at net = -800.00 (gross -750.00, comm 50.00) => -9,600.00
            # 1 trade  at net = -920.51 (gross -850.00, comm 70.51) =>   -920.51
            # Total: 13 trades, gross = -9,850.00, comm = 670.51, net = -10,520.51
            # Trade #0 intentionally exits at 00:45 IST on 30-Oct (= 2026-10-29 19:15 UTC)
            # to prove UTC-previous-day timestamps stay on 30-Oct IST!
            ram_overlap_trade = None
            for idx in range(13):
                if idx == 0:
                    exit_ist = datetime(2026, 10, 30, 0, 45, 0, tzinfo=EXCHANGE_TZ)
                else:
                    exit_ist = datetime(2026, 10, 30, 9, 30 + idx, 0, tzinfo=EXCHANGE_TZ)
                entry_ist = exit_ist - timedelta(minutes=10)
                gross = Decimal("-750.00") if idx < 12 else Decimal("-850.00")
                comm = Decimal("50.00") if idx < 12 else Decimal("70.51")
                net = gross - comm

                session.add(
                    TradeRow(
                        portfolio_id=p_paper.portfolio_id,
                        symbol=f"NIFTY26OCT_{idx}",
                        strategy_name="immediate_strangle" if idx < 10 else "ema_pullback",
                        direction="short",
                        quantity=Decimal("65"),
                        entry_price=Decimal("200.00"),
                        exit_price=Decimal("211.54"),
                        entry_time=entry_ist.astimezone(timezone.utc),
                        exit_time=exit_ist.astimezone(timezone.utc),
                        gross_pnl=gross,
                        commission_total=comm,
                        net_pnl=net,
                        exit_reason="stop_loss",
                    )
                )
                if idx == 12:
                    ram_overlap_trade = {
                        "symbol": f"NIFTY26OCT_{idx}",
                        "strategy": "ema_pullback",
                        "side": "short",
                        "units": 65,
                        "qty": 1,
                        "entry_price": 200.00,
                        "exit_price": 211.54,
                        "entry_time": entry_ist.isoformat(),
                        "exit_time": exit_ist.isoformat(),
                        "gross_pnl": float(gross),
                        "commission": 40.00,
                        "statutory_fees": 30.51,
                        "pnl": float(net),
                        "reason": "stop_loss",
                    }

            # Add 1 profitable LIVE trade on 30-Oct IST — must NEVER appear in mode=paper
            live_exit = datetime(2026, 10, 30, 11, 0, 0, tzinfo=EXCHANGE_TZ)
            session.add(
                TradeRow(
                    portfolio_id=p_live.portfolio_id,
                    symbol="NIFTY26OCT24500CE",
                    strategy_name="CE_instant_buy",
                    direction="long",
                    quantity=Decimal("65"),
                    entry_price=Decimal("120.00"),
                    exit_price=Decimal("155.00"),
                    entry_time=(live_exit - timedelta(minutes=20)).astimezone(timezone.utc),
                    exit_time=live_exit.astimezone(timezone.utc),
                    gross_pnl=Decimal("2275.00"),
                    commission_total=Decimal("62.40"),
                    net_pnl=Decimal("2212.60"),
                    exit_reason="take_profit",
                )
            )
            # Custom NSE holiday in DB (e.g. Muhurat/special closure test on 2026-10-21)
            session.add(
                MarketHoliday(
                    segment="NSE_FO",
                    holiday_date=date(2026, 10, 21),
                    description="Diwali Laxmi Pujan",
                )
            )

        # Stub PortfolioManager with overlapping RAM runner + live open MTM on paper & live
        class _FakeManager:
            def __init__(self, db_mgr, overlap_trade):
                self._trade_persister = LiveTradePersister(db_mgr)
                self._runners = {
                    "deadbeef-1111": _StubRunner(
                        instance_id="deadbeef-1111",
                        name="Strangle 30Oct",
                        strategy_name="ema_pullback",
                        mode="paper",
                        closed_trades=[overlap_trade],
                        unrealized_pnl=420.25,
                        open_positions=[{"symbol": "NIFTY26OCT24600PE", "qty": 1}],
                        day_start_equity=300_000.0,
                    ),
                    "cafe1234-2222": _StubRunner(
                        instance_id="cafe1234-2222",
                        name="Live Hedge",
                        strategy_name="CE_instant_buy",
                        mode="live",
                        closed_trades=[],
                        unrealized_pnl=815.00,
                        open_positions=[{"symbol": "NIFTY26OCT24700CE", "qty": 1}],
                        day_start_equity=150_000.0,
                    ),
                }

            def list_instances(self, mode=None):
                rows = [r.get_state() for r in self._runners.values()]
                if mode is not None:
                    rows = [s for s in rows if s["mode"] == mode]
                return rows

            def get_runner(self, iid):
                return self._runners.get(iid)

        svc = DiaryService(
            manager=_FakeManager(sqlite_db, ram_overlap_trade),
            db_manager=sqlite_db,
        )

        # 1. Paper calendar for October 2026
        cal_paper = svc.get_calendar(
            mode="paper",
            from_date="2026-10-01",
            to_date="2026-10-31",
            today_ist=date(2026, 10, 30),
        )
        assert cal_paper["mode"] == "paper"
        assert len(cal_paper["days"]) == 31
        by_date = {d["date"]: d for d in cal_paper["days"]}

        # 29-Oct must have 0 trades (even though trade #0 exited at 29-Oct 19:15 UTC!)
        assert by_date["2026-10-29"]["trade_count"] == 0
        # 30-Oct has all 13 deduplicated paper trades summing to -10,520.51
        oct30 = by_date["2026-10-30"]
        assert oct30["trade_count"] == 13
        assert oct30["gross_pnl"] == pytest.approx(-9850.00)
        assert oct30["commission"] == pytest.approx(670.51)
        assert oct30["net_pnl"] == pytest.approx(-10520.51)
        assert round(oct30["gross_pnl"] - oct30["commission"], 2) == pytest.approx(oct30["net_pnl"])
        assert oct30["reconcile_anomalies"] == 0
        assert oct30["is_today"] is True
        assert oct30["running_mtm"] == pytest.approx(420.25)
        assert oct30["open_count"] == 1

        # Static NSE holiday (2-Oct) + DB MarketHoliday (21-Oct) + weekend (4-Oct Sun)
        assert by_date["2026-10-02"]["is_holiday"] is True
        assert "Gandhi" in (by_date["2026-10-02"]["holiday_name"] or "")
        assert by_date["2026-10-21"]["is_holiday"] is True
        assert by_date["2026-10-21"]["holiday_name"] == "Diwali Laxmi Pujan"
        assert by_date["2026-10-04"]["is_weekend"] is True

        # Weekly rollups sum to exact monthly totals
        wk_trades = sum(w["trade_count"] for w in cal_paper["weeks"])
        wk_net = round(sum(w["net_pnl"] for w in cal_paper["weeks"]), 2)
        assert wk_trades == 13
        assert wk_net == pytest.approx(-10520.51)

        # 2. Paper summary on 30-Oct: single commission sum, zero sub-component keys
        sum_paper = svc.get_summary(
            mode="paper",
            from_date="2026-10-30",
            to_date="2026-10-30",
            today_ist=date(2026, 10, 30),
        )
        assert sum_paper["trade_count"] == 13
        assert sum_paper["net_pnl"] == pytest.approx(-10520.51)
        assert sum_paper["running_mtm"] == pytest.approx(420.25)
        assert sum_paper["total_pnl_with_mtm"] == pytest.approx(-10520.51 + 420.25)
        for forbidden_key in ("brokerage", "stt", "gst", "stamp_duty", "sebi_charges"):
            assert forbidden_key not in sum_paper
            assert forbidden_key not in oct30

        # 3. Live summary on 30-Oct: completely isolated from paper losses!
        sum_live = svc.get_summary(
            mode="live",
            from_date="2026-10-30",
            to_date="2026-10-30",
            today_ist=date(2026, 10, 30),
        )
        assert sum_live["mode"] == "live"
        assert sum_live["trade_count"] == 1
        assert sum_live["gross_pnl"] == pytest.approx(2275.00)
        assert sum_live["commission"] == pytest.approx(62.40)
        assert sum_live["net_pnl"] == pytest.approx(2212.60)
        assert sum_live["running_mtm"] == pytest.approx(815.00)
        assert sum_live["win_rate_pct"] == pytest.approx(100.0)

        # 4. Historical window excludes running MTM
        sum_hist = svc.get_summary(
            mode="paper",
            from_date="2026-10-01",
            to_date="2026-10-29",
            today_ist=date(2026, 10, 30),
        )
        assert sum_hist["includes_today"] is False
        assert sum_hist["running_mtm"] is None


class TestSlice1FlaskRoutesAndDom:
    def test_diary_api_routes_and_portfolio_deeplinks(self, sqlite_db):
        reset_portfolio_manager()
        app = create_app(source="synthetic")
        app.config["TESTING"] = True
        mgr = get_portfolio_manager()
        mgr._trade_persister = LiveTradePersister(sqlite_db)

        today_ist = current_session_date()
        exit_utc = datetime.combine(
            today_ist, datetime.min.time(), tzinfo=EXCHANGE_TZ
        ).replace(hour=14, minute=10).astimezone(timezone.utc)

        with sqlite_db.session() as session:
            p_paper = PortfolioRow(
                name="Paper Runner [11223344]",
                initial_capital=Decimal("100000"),
                current_cash=Decimal("100400"),
                mode="paper",
                source="mstock",
            )
            session.add(p_paper)
            session.flush()
            # Valid trade + corrupted B2 anomaly row
            session.add_all(
                [
                    TradeRow(
                        portfolio_id=p_paper.portfolio_id,
                        symbol="INFY",
                        strategy_name="orb_breakout",
                        direction="long",
                        quantity=Decimal("10"),
                        entry_price=Decimal("1500"),
                        exit_price=Decimal("1550"),
                        entry_time=exit_utc - timedelta(minutes=30),
                        exit_time=exit_utc,
                        gross_pnl=Decimal("500.00"),
                        commission_total=Decimal("45.00"),
                        net_pnl=Decimal("455.00"),
                        exit_reason="take_profit",
                    ),
                    TradeRow(
                        portfolio_id=p_paper.portfolio_id,
                        symbol="SBIN",
                        strategy_name="orb_breakout",
                        direction="long",
                        quantity=Decimal("20"),
                        entry_price=Decimal("800"),
                        exit_price=Decimal("805"),
                        entry_time=exit_utc - timedelta(minutes=20),
                        exit_time=exit_utc,
                        gross_pnl=Decimal("100.00"),
                        commission_total=Decimal("15.00"),
                        net_pnl=Decimal("50.00"),  # off by ₹35 -> reconcile_ok=False
                        exit_reason="manual",
                    ),
                ]
            )

        try:
            with app.test_client() as client:
                # 1. Invalid mode rejected with 400
                r_bad = client.get("/api/diary/summary?mode=combined")
                assert r_bad.status_code == 400
                assert r_bad.get_json()["success"] is False

                r_bad_cal = client.get(
                    "/api/diary/calendar?mode=paper&from=2026-10-30&to=2026-10-01"
                )
                assert r_bad_cal.status_code == 400

                # 2. Valid paper summary surfaces anomaly without smoothing net_pnl
                iso_today = today_ist.isoformat()
                r_sum = client.get(
                    f"/api/diary/summary?mode=paper&from={iso_today}&to={iso_today}"
                )
                assert r_sum.status_code == 200
                body = r_sum.get_json()
                assert body["success"] is True
                assert body["trade_count"] == 2
                assert body["net_pnl"] == pytest.approx(505.00)
                assert body["reconcile_anomalies"] == 1

                # 3. Live book summary has 0 trades (strict isolation)
                r_live = client.get(
                    f"/api/diary/summary?mode=live&from={iso_today}&to={iso_today}"
                )
                assert r_live.status_code == 200
                assert r_live.get_json()["trade_count"] == 0

                # 4. /diary?mode=live pre-selects live book & links back to /portfolio/live
                diary_live_html = client.get("/diary?mode=live").get_data(as_text=True)
                assert 'data-initial-mode="live"' in diary_live_html
                assert 'href="/portfolio/live"' in diary_live_html

                # 5. /portfolio/paper & /portfolio/live render mode-matched PnL diary deep-link
                paper_html = client.get("/portfolio/paper").get_data(as_text=True)
                live_html = client.get("/portfolio/live").get_data(as_text=True)
                assert 'href="/diary?mode=paper"' in paper_html
                assert 'href="/diary?mode=live"' in live_html
        finally:
            mgr.shutdown()
            reset_portfolio_manager()
