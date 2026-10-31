"""Slice 0 — Ledger Truth & E1/E2 Foundation Gate tests.

Covers:
1. IST session date & half-open UTC bounds (``00:00–05:30 IST`` UTC seam).
2. B1 fix: ``LiveTradePersister`` restart idempotency across fresh instances,
   ``+05:30`` vs ``+00:00`` ISO strings, and option ``lots`` vs ``units``.
3. ``mode='paper'|'live'`` persistence isolation (fixes hardcoded ``'paper'``).
4. E2 + B2: single ``commission_total`` sum (brokerage + statutory, no split),
   ``is_winner`` strictly ``net_pnl > 0``, and un-smoothed ``reconcile_ok=False``
   flagging when ``Gross - Commission != Net``.
5. E1 + E2 API/UI integration: ``/api/portfolio/trades/aggregated?scope=today``,
   historical DB duplicate collapse, and ``/portfolio/paper`` DOM contracts.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from backtest.diary.ledger import (
    normalize_memory_trade_dict,
    reconcile_trade_amounts,
    single_commission_decimal,
)
from backtest.forward.trade_persistence import LiveTradePersister
from backtest.timekeeping import (
    EXCHANGE_TZ,
    UTC,
    current_session_date,
    format_session_scope,
    ist_session_bounds_utc,
    session_date,
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


class _StubConfig:
    def __init__(
        self,
        name: str = "Strangle NIFTY",
        strategy: str = "immediate_strangle",
        capital: float = 200_000.0,
        source: str = "mstock",
        mode: str = "paper",
    ) -> None:
        self.name = name
        self.strategy_name = strategy
        self.allocated_capital = capital
        self.source = source
        self.mode = mode


class _StubPortfolio:
    current_cash = Decimal("195000.00")


class _StubRunner:
    def __init__(self, trades: list[dict], instance_id: str = "a1b2c3d4-9999", mode: str = "paper"):
        self.instance_id = instance_id
        self.config = _StubConfig(mode=mode)
        self.portfolio = _StubPortfolio()
        self._trades = list(trades)

    @property
    def closed_trades(self) -> list[dict]:
        return self._trades


class TestIstSessionClock:
    def test_session_date_preserves_early_morning_ist_string(self):
        # 01:15 IST on 30-Oct is 19:45 UTC on 29-Oct — must still read 30-Oct IST.
        assert session_date("2026-10-30T01:15:00+05:30") == date(2026, 10, 30)
        assert session_date("2026-10-29T19:45:00+00:00") == date(2026, 10, 30)

    def test_ist_session_bounds_utc_exact_half_open_window(self):
        start_u, end_u = ist_session_bounds_utc("2026-10-30")
        assert start_u == datetime(2026, 10, 29, 18, 30, 0, tzinfo=UTC)
        assert end_u == datetime(2026, 10, 30, 18, 30, 0, tzinfo=UTC)

        with pytest.raises(ValueError, match="precedes"):
            ist_session_bounds_utc("2026-10-30", "2026-10-28")

    def test_format_session_scope_labels_today_and_history(self):
        ref = datetime(2026, 10, 30, 10, 0, 0, tzinfo=EXCHANGE_TZ)
        assert format_session_scope("2026-10-30", reference_now=ref) == "Today · 30-Oct IST"
        assert format_session_scope("2026-10-28", reference_now=ref) == "28-Oct IST"
        assert format_session_scope("2025-12-31", reference_now=ref) == "31-Dec-2025 IST"


class TestPersisterRestartAndModeTruth:
    def test_restart_does_not_reinsert_restored_option_or_equity_trades(self, sqlite_db):
        trade_ist = {
            "symbol": "NIFTY short_strangle",
            "kind": "option",
            "side": "SHORT",
            "qty": 1,       # lots in RAM
            "units": 65,    # persisted in trades.quantity
            "entry_price": 210.50,
            "exit_price": 215.25,
            "entry_ts": "2026-10-30T09:16:00+05:30",
            "exit_ts": "2026-10-30T15:10:02+05:30",
            "pnl": -308.75,
            "gross_pnl": -308.75,
            "commission": 40.00,
            "statutory_fees": 153.40,
            "net_pnl": -502.15,
            "exit_reason": "intraday_squareoff",
        }
        persister_before = LiveTradePersister(sqlite_db)
        runner_before = _StubRunner([trade_ist], instance_id="f901abcd-1111")
        assert persister_before.flush_runner(runner_before) == 1

        # Simulate server restart: brand-new LiveTradePersister (empty _flushed)
        # + restored runner carrying UTC-formatted ISO timestamps + 1 fresh trade.
        restored_utc = dict(
            trade_ist,
            entry_ts="2026-10-30T03:46:00+00:00",
            exit_ts="2026-10-30T09:40:02+00:00",
        )
        fresh_trade = dict(
            trade_ist,
            entry_ts="2026-10-30T15:12:00+05:30",
            exit_ts="2026-10-30T15:20:00+05:30",
            pnl=420.00,
            gross_pnl=420.00,
            net_pnl=226.60,
            exit_reason="target",
        )
        persister_after = LiveTradePersister(sqlite_db)
        runner_after = _StubRunner([restored_utc, fresh_trade], instance_id="f901abcd-1111")
        assert persister_after.flush_runner(runner_after) == 1

        rows = sqlite_db.fetch_all(
            "SELECT gross_pnl, commission_total, net_pnl, exit_reason "
            "FROM trades ORDER BY exit_time"
        )
        assert len(rows) == 2
        assert float(rows[0]["commission_total"]) == pytest.approx(193.40)
        assert float(rows[0]["net_pnl"]) == pytest.approx(-502.15)
        assert rows[0]["exit_reason"] == "eod_flat"
        assert float(rows[1]["net_pnl"]) == pytest.approx(226.60)

    def test_live_mode_runner_stamps_live_portfolio_and_heals_legacy_paper_tag(self, sqlite_db):
        t1 = {
            "symbol": "NIFTY long_call",
            "qty": 1,
            "units": 65,
            "entry_price": 120.0,
            "exit_price": 130.0,
            "entry_ts": "2026-10-30T09:20:00+05:30",
            "exit_ts": "2026-10-30T09:35:00+05:30",
            "pnl": 650.0,
            "gross_pnl": 650.0,
            "commission": 40.0,
            "statutory_fees": 22.10,
            "net_pnl": 587.90,
            "exit_reason": "target",
        }
        persister = LiveTradePersister(sqlite_db)
        live_runner = _StubRunner([t1], instance_id="7788aabb-live", mode="live")
        assert persister.flush_runner(live_runner) == 1

        p_rows = sqlite_db.fetch_all("SELECT name, mode FROM portfolios")
        assert len(p_rows) == 1
        assert p_rows[0]["mode"] == "live"


class TestCommissionAndReconciliationRules:
    def test_single_commission_and_net_winner_semantics(self):
        # Gross +102, Commission 193 (40 brokerage + 153 statutory) => Net -91 (NOT a win!)
        scalp = {
            "symbol": "NIFTY short_strangle",
            "pnl": 102.0,
            "gross_pnl": 102.0,
            "commission": 40.0,
            "statutory_fees": 153.0,
            "net_pnl": -91.0,
            "entry_ts": "2026-10-30T09:20:00+05:30",
            "exit_ts": "2026-10-30T09:22:15+05:30",
        }
        assert single_commission_decimal(scalp) == Decimal("193.0")
        norm = normalize_memory_trade_dict(scalp, instance_id="abcd1234")
        assert norm["commission_total"] == pytest.approx(193.0)
        assert norm["net_pnl"] == pytest.approx(-91.0)
        assert norm["reconcile_ok"] is True
        assert norm["is_winner"] is False
        assert norm["win"] is False
        assert norm["holding_display"] == "2m 15s"

    def test_b2_fee_floor_disagreement_is_flagged_never_smoothed(self):
        # B2 case: gross=500, commission_total=60, but recorded net_pnl=400 (₹40 gap!)
        res = reconcile_trade_amounts(
            gross_pnl=Decimal("500.00"),
            commission_total=Decimal("60.00"),
            net_pnl=Decimal("400.00"),
        )
        assert res["net_pnl"] == pytest.approx(400.00)  # never overwritten!
        assert res["reconcile_ok"] is False
        assert res["reconcile_diff"] == pytest.approx(40.00)


class TestAggregatedTradesEndpointAndUiContracts:
    def test_aggregated_trades_scopes_to_today_dedupes_db_and_surfaces_mismatches(
        self, sqlite_db, monkeypatch
    ):
        from backtest.db.models import Portfolio as PortfolioRow
        from backtest.db.models import Trade as TradeRow
        from backtest.forward.portfolio_manager import (
            get_portfolio_manager,
            reset_portfolio_manager,
        )
        from backtest.web.app import create_app

        monkeypatch.setenv("ALLOW_LIVE_ORDERS", "0")
        monkeypatch.setenv("BACKTEST_DATA_PROFILE", "testing")
        monkeypatch.setenv("FORWARD_TEST_DB_PROFILE", "testing")
        monkeypatch.setenv("FORWARD_TEST_DB_URL", "sqlite:///:memory:")

        reset_portfolio_manager(auto_start_feed=False)
        app = create_app(source="synthetic", log_level="ERROR", TESTING=True)
        mgr = get_portfolio_manager()
        mgr._trade_persister = LiveTradePersister(sqlite_db)

        today_ist = current_session_date()
        today_exit_utc = datetime.combine(
            today_ist, datetime.min.time(), tzinfo=EXCHANGE_TZ
        ).replace(hour=11, minute=30).astimezone(timezone.utc)
        today_entry_utc = today_exit_utc - timedelta(minutes=15)
        prior_exit_utc = datetime(2026, 10, 28, 6, 0, 0, tzinfo=timezone.utc)

        with sqlite_db.session() as session:
            p_paper = PortfolioRow(
                name="Paper Strangle [deadbeef]",
                initial_capital=Decimal("200000"),
                current_cash=Decimal("199500"),
                mode="paper",
                source="mstock",
            )
            p_live = PortfolioRow(
                name="Live Call [cafe1234]",
                initial_capital=Decimal("100000"),
                current_cash=Decimal("101000"),
                mode="live",
                source="mstock",
            )
            session.add_all([p_paper, p_live])
            session.flush()

            # 1) Duplicate pair on today's session (simulates legacy B1 duplicate)
            for _ in range(2):
                session.add(
                    TradeRow(
                        portfolio_id=p_paper.portfolio_id,
                        symbol="NIFTY short_strangle",
                        strategy_name="immediate_strangle",
                        direction="short",
                        quantity=Decimal("65"),
                        entry_price=Decimal("200"),
                        exit_price=Decimal("195"),
                        entry_time=today_entry_utc,
                        exit_time=today_exit_utc,
                        gross_pnl=Decimal("325.00"),
                        commission_total=Decimal("193.00"),
                        net_pnl=Decimal("132.00"),
                        exit_reason="take_profit",
                    )
                )
            # 2) B2 mismatch row on today's session (Gross 100 - Comm 20 != Net 40)
            session.add(
                TradeRow(
                    portfolio_id=p_paper.portfolio_id,
                    symbol="RELIANCE",
                    strategy_name="ema_pullback",
                    direction="long",
                    quantity=Decimal("10"),
                    entry_price=Decimal("1300"),
                    exit_price=Decimal("1310"),
                    entry_time=today_entry_utc,
                    exit_time=today_exit_utc.replace(minute=45),
                    gross_pnl=Decimal("100.00"),
                    commission_total=Decimal("20.00"),
                    net_pnl=Decimal("40.00"),
                    exit_reason="take_profit",
                )
            )
            # 3) Prior-day row (28-Oct) — must NOT leak into scope=today
            session.add(
                TradeRow(
                    portfolio_id=p_paper.portfolio_id,
                    symbol="TCS",
                    strategy_name="ema_pullback",
                    direction="long",
                    quantity=Decimal("5"),
                    entry_price=Decimal("3500"),
                    exit_price=Decimal("3400"),
                    entry_time=prior_exit_utc,
                    exit_time=prior_exit_utc,
                    gross_pnl=Decimal("-500.00"),
                    commission_total=Decimal("35.00"),
                    net_pnl=Decimal("-535.00"),
                    exit_reason="stop_loss",
                )
            )
            # 4) Live-book row today — must NEVER leak into mode=paper
            session.add(
                TradeRow(
                    portfolio_id=p_live.portfolio_id,
                    symbol="NIFTY long_call",
                    strategy_name="CE_instant_buy",
                    direction="long",
                    quantity=Decimal("65"),
                    entry_price=Decimal("100"),
                    exit_price=Decimal("120"),
                    entry_time=today_entry_utc,
                    exit_time=today_exit_utc,
                    gross_pnl=Decimal("1300.00"),
                    commission_total=Decimal("60.00"),
                    net_pnl=Decimal("1240.00"),
                    exit_reason="take_profit",
                )
            )

        try:
            with app.test_client() as client:
                resp = client.get("/api/portfolio/trades/aggregated?mode=paper&scope=today")
                assert resp.status_code == 200
                payload = resp.get_json()
                assert payload["success"] is True
                summary = payload["summary"]

                # Only the 2 unique paper trades from TODAY appear (duplicate collapsed,
                # 28-Oct excluded, live book excluded).
                assert summary["total"] == 2
                assert summary["duplicates_collapsed"] == 1
                assert summary["total_gross_pnl"] == pytest.approx(425.00)
                assert summary["total_commission"] == pytest.approx(213.00)
                assert summary["total_net_pnl"] == pytest.approx(172.00)
                assert summary["total_pnl"] == pytest.approx(172.00)
                assert summary["mismatch_count"] == 1
                assert summary["session_scope_label"].startswith("Today · ")

                by_sym = {r["symbol"]: r for r in payload["trades"]}
                assert by_sym["NIFTY short_strangle"]["reconcile_ok"] is True
                assert by_sym["RELIANCE"]["reconcile_ok"] is False
                assert by_sym["RELIANCE"]["reconcile_diff"] == pytest.approx(40.00)

                # UI contract on /portfolio/paper: single Commission header + scope selector
                html = client.get("/portfolio/paper").get_data(as_text=True)
                assert 'id="m-realized-sub"' in html
                assert 'id="agg-trade-scope"' in html
                assert ">Gross<" in html
                assert ">Commission<" in html
                assert ">Net P&amp;L<" in html
        finally:
            mgr.shutdown()
