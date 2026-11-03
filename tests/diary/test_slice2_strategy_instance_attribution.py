"""Slice 2 — 3-Level Strategy → Spawned Instance → Trade/Structure Attribution tests.

Covers:
1. Exact paisa-level hierarchical sums across all 3 levels:
   ``Sum(Trades) == Instance == Strategy == Summary`` on the 30-Oct 13-trade
   ``-10,520.51`` benchmark, including active open-only MTM runners on IST today.
2. Retired DB instance preservation alongside active RAM runners, config badge
   extraction, and ``RUNNING | PAUSED | BLOCKED_BY_RISK | RETIRED`` statuses.
3. E2 single-commission enforcement across all 3 levels (zero fee-split keys)
   plus un-smoothed ``reconcile_ok=False`` surfacing on Level 3.
4. Historical window behavior (``includes_today=False`` -> ``running_mtm=None``,
   ``open_positions=[]``, open-only runners excluded).
5. ``GET /api/diary/attribution`` and ``GET /api/diary/instance/<key>/trades``
   routes, inline truncation drilldown, HTTP 400/404 guards, and Paper/Live isolation.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from backtest.db.models import Portfolio as PortfolioRow, Trade as TradeRow
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


class _StubConfig:
    def __init__(
        self,
        *,
        name: str,
        strategy_name: str,
        mode: str = "paper",
        capital: float = 200_000.0,
        timeframe: str = "5min",
        symbols: list[str] | None = None,
        strategy_params: dict | None = None,
        instrument: dict | None = None,
    ) -> None:
        self.name = name
        self.strategy_name = strategy_name
        self.mode = mode
        self.allocated_capital = capital
        self.timeframe = timeframe
        self.symbols = list(symbols or ["NIFTY"])
        self.strategy_params = dict(strategy_params or {})
        self.instrument = dict(instrument or {"type": "equity"})
        self.source = "mstock"


class _StubRunner:
    def __init__(
        self,
        *,
        instance_id: str,
        config: _StubConfig,
        status: str = "RUNNING",
        closed_trades: list[dict] | None = None,
        open_pnl: float = 0.0,
        open_positions: list[dict] | None = None,
        options_summary: dict | None = None,
        block_reason: str | None = None,
    ) -> None:
        self.instance_id = instance_id
        self.config = config
        self.status = status
        self._closed_trades = list(closed_trades or [])
        self._open_pnl = open_pnl
        self._open_positions = list(open_positions or [])
        self._options_summary = options_summary
        self._block_reason = block_reason

    @property
    def closed_trades(self) -> list[dict]:
        return self._closed_trades

    def get_state(self) -> dict:
        opt_structs = (
            len((self._options_summary or {}).get("structures") or [])
            if self._options_summary
            else 0
        )
        open_cnt = len(self._open_positions) + opt_structs
        return {
            "instance_id": self.instance_id,
            "name": self.config.name,
            "strategy_name": self.config.strategy_name,
            "mode": self.config.mode,
            "status": self.status,
            "symbols": list(self.config.symbols),
            "symbol_count": len(self.config.symbols),
            "timeframe": self.config.timeframe,
            "allocated_capital": self.config.allocated_capital,
            "open_pnl": self._open_pnl,
            "open_positions": open_cnt,
            "instrument": dict(self.config.instrument),
            "options": self._options_summary,
            "trade_health": "BLOCKED" if self._block_reason else "OK",
            "block_reason": self._block_reason,
        }


class _FakeManager:
    def __init__(self, db_mgr, runners: list[_StubRunner]) -> None:
        self._trade_persister = LiveTradePersister(db_mgr)
        self._runners = {r.instance_id: r for r in runners}

    def list_instances(self, mode: str | None = None) -> list[dict]:
        rows = [r.get_state() for r in self._runners.values()]
        if mode is not None:
            rows = [s for s in rows if s["mode"] == mode]
        return rows

    def get_runner(self, iid: str):
        return self._runners.get(iid)


class TestSlice2AttributionHierarchy:
    def test_three_level_sums_retired_instances_and_e3_benchmark(self, sqlite_db):
        """Verify Sum(Trades) == Instance == Strategy == Summary on 30-Oct -10,520.51."""
        with sqlite_db.session() as session:
            # 1) Retired DB-only Paper instance (aaaa1111): 5 trades
            p_retired = PortfolioRow(
                name="Strangle Morning [aaaa1111]",
                initial_capital=Decimal("250000.00"),
                current_cash=Decimal("246000.00"),
                mode="paper",
                source="mstock",
            )
            # 2) Active RUNNING Paper instance (bbbb2222): 5 trades + live open option MTM
            p_active = PortfolioRow(
                name="Strangle Afternoon [bbbb2222]",
                initial_capital=Decimal("300000.00"),
                current_cash=Decimal("296000.00"),
                mode="paper",
                source="mstock",
            )
            # 3) Active PAUSED Paper instance (cccc3333): 3 trades under ema_pullback
            p_ema = PortfolioRow(
                name="EMA Pullback Bank [cccc3333]",
                initial_capital=Decimal("180000.00"),
                current_cash=Decimal("177479.49"),
                mode="paper",
                source="mstock",
            )
            # 4) Separate LIVE book instance — must NEVER appear in mode=paper
            p_live = PortfolioRow(
                name="Live Call [eeee5555]",
                initial_capital=Decimal("120000.00"),
                current_cash=Decimal("121500.00"),
                mode="live",
                source="mstock",
            )
            session.add_all([p_retired, p_active, p_ema, p_live])
            session.flush()

            # 13 paper trades summing to exact net = -10,520.51:
            #   5 on aaaa1111 @ net -800.00 (gross -750, comm 50)  => -4,000.00
            #   5 on bbbb2222 @ net -800.00 (gross -750, comm 50)  => -4,000.00
            #   2 on cccc3333 @ net -800.00 (gross -750, comm 50)  => -1,600.00
            #   1 on cccc3333 @ net -920.51 (gross -850, comm 70.51) => -920.51
            # Grand total = 13 trades, gross = -9,850.00, comm = 670.51, net = -10,520.51
            ram_overlap = None
            for idx in range(13):
                exit_ist = datetime(2026, 10, 30, 9, 45 + idx, 0, tzinfo=EXCHANGE_TZ)
                entry_ist = exit_ist - timedelta(minutes=8)
                if idx < 5:
                    p_row = p_retired
                    strat = "immediate_strangle"
                elif idx < 10:
                    p_row = p_active
                    strat = "immediate_strangle"
                else:
                    p_row = p_ema
                    strat = "ema_pullback"

                gross = Decimal("-750.00") if idx < 12 else Decimal("-850.00")
                comm = Decimal("50.00") if idx < 12 else Decimal("70.51")
                net = gross - comm

                session.add(
                    TradeRow(
                        portfolio_id=p_row.portfolio_id,
                        symbol=f"NIFTY26OCT_{idx}",
                        strategy_name=strat,
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
                if idx == 9:
                    ram_overlap = {
                        "symbol": f"NIFTY26OCT_{idx}",
                        "strategy": "immediate_strangle",
                        "kind": "option",
                        "label": "NIFTY short_strangle 24400/24700",
                        "side": "SHORT",
                        "qty": 1,
                        "units": 65,
                        "entry_price": 200.00,
                        "exit_price": 211.54,
                        "entry_ts": entry_ist.isoformat(),
                        "exit_ts": exit_ist.isoformat(),
                        "gross_pnl": -750.00,
                        "commission": 30.00,
                        "statutory_fees": 20.00,
                        "net_pnl": -800.00,
                        "exit_reason": "stop_loss",
                    }

            # Live trade on 30-Oct
            live_exit = datetime(2026, 10, 30, 12, 0, 0, tzinfo=EXCHANGE_TZ)
            session.add(
                TradeRow(
                    portfolio_id=p_live.portfolio_id,
                    symbol="NIFTY26OCT24500CE",
                    strategy_name="CE_instant_buy",
                    direction="long",
                    quantity=Decimal("65"),
                    entry_price=Decimal("100.00"),
                    exit_price=Decimal("130.00"),
                    entry_time=(live_exit - timedelta(minutes=15)).astimezone(timezone.utc),
                    exit_time=live_exit.astimezone(timezone.utc),
                    gross_pnl=Decimal("1950.00"),
                    commission_total=Decimal("55.00"),
                    net_pnl=Decimal("1895.00"),
                    exit_reason="take_profit",
                )
            )

        runners = [
            _StubRunner(
                instance_id="bbbb2222-1111-2222-3333-444444444444",
                config=_StubConfig(
                    name="Strangle Afternoon",
                    strategy_name="immediate_strangle",
                    mode="paper",
                    capital=300_000.0,
                    timeframe="5min",
                    symbols=["NIFTY"],
                    strategy_params={"lots": 2, "sl_pct": 25, "tp_pct": 40},
                    instrument={
                        "type": "option",
                        "expression": {"structure": "short_strangle", "carry_policy": "intraday"},
                    },
                ),
                status="RUNNING",
                closed_trades=[ram_overlap],
                open_pnl=340.50,
                options_summary={
                    "open_structures": 1,
                    "structures": [
                        {
                            "label": "NIFTY short_strangle 24350/24650",
                            "side": "SHORT",
                            "lots": 2,
                            "entry_premium": 180.0,
                            "current_premium": 177.38,
                            "unrealized_pnl": 340.50,
                        }
                    ],
                },
            ),
            _StubRunner(
                instance_id="cccc3333-1111-2222-3333-444444444444",
                config=_StubConfig(
                    name="EMA Pullback Bank",
                    strategy_name="ema_pullback",
                    mode="paper",
                    capital=180_000.0,
                    timeframe="15min",
                    symbols=["HDFCBANK", "ICICIBANK"],
                ),
                status="PAUSED",
            ),
            # Active runner with 0 closed trades today + open MTM + risk block
            _StubRunner(
                instance_id="dddd4444-1111-2222-3333-444444444444",
                config=_StubConfig(
                    name="ORB Intraday",
                    strategy_name="orb_breakout",
                    mode="paper",
                    capital=150_000.0,
                    timeframe="5min",
                    symbols=["RELIANCE"],
                ),
                status="RUNNING",
                open_pnl=180.00,
                open_positions=[{"symbol": "RELIANCE", "kind": "equity", "qty": 10}],
                block_reason="MAX_DAILY_TRADES",
            ),
            # Live runner — must never leak into mode=paper
            _StubRunner(
                instance_id="eeee5555-1111-2222-3333-444444444444",
                config=_StubConfig(
                    name="Live Call",
                    strategy_name="CE_instant_buy",
                    mode="live",
                    capital=120_000.0,
                ),
                status="RUNNING",
                open_pnl=999.00,
                open_positions=[{"symbol": "NIFTY26OCT24500CE", "qty": 1}],
            ),
        ]

        svc = DiaryService(manager=_FakeManager(sqlite_db, runners), db_manager=sqlite_db)
        summary = svc.get_summary(
            mode="paper",
            from_date="2026-10-30",
            to_date="2026-10-30",
            today_ist=date(2026, 10, 30),
        )
        tree = svc.get_attribution(
            mode="paper",
            from_date="2026-10-30",
            to_date="2026-10-30",
            today_ist=date(2026, 10, 30),
        )

        # 1. Grand totals match Day Summary Tile to the paisa
        totals = tree["totals"]
        assert totals["trade_count"] == 13
        assert totals["gross_pnl"] == pytest.approx(-9850.00)
        assert totals["commission"] == pytest.approx(670.51)
        assert totals["net_pnl"] == pytest.approx(-10520.51)
        assert totals["net_pnl"] == pytest.approx(summary["net_pnl"])
        assert totals["running_mtm"] == pytest.approx(520.50)  # 340.50 + 180.00
        assert totals["running_mtm"] == pytest.approx(summary["running_mtm"])
        assert totals["strategy_count"] == 3
        assert totals["instance_count"] == 4
        assert totals["active_instance_count"] == 3
        assert totals["retired_instance_count"] == 1
        assert tree["duplicates_collapsed"] == 1

        # 2. Hierarchical sum invariants: Sum(Instances) == Strategy, Sum(Strategies) == Totals
        strats = tree["strategies"]
        assert round(sum(s["net_pnl"] for s in strats), 2) == pytest.approx(totals["net_pnl"])
        assert round(sum(s["gross_pnl"] for s in strats), 2) == pytest.approx(totals["gross_pnl"])
        assert round(sum(s["commission"] for s in strats), 2) == pytest.approx(totals["commission"])
        assert round(sum(s["running_mtm"] for s in strats), 2) == pytest.approx(
            totals["running_mtm"]
        )

        by_strat = {s["strategy_name"]: s for s in strats}
        for s in strats:
            for k in FORBIDDEN_FEE_SPLIT_KEYS:
                assert k not in s
            insts = s["instances"]
            assert round(sum(i["net_pnl"] for i in insts), 2) == pytest.approx(s["net_pnl"])
            assert round(sum(i["gross_pnl"] for i in insts), 2) == pytest.approx(s["gross_pnl"])
            assert round(sum(i["commission"] for i in insts), 2) == pytest.approx(s["commission"])
            assert round(sum(i["running_mtm"] for i in insts), 2) == pytest.approx(s["running_mtm"])
            for inst in insts:
                for k in FORBIDDEN_FEE_SPLIT_KEYS:
                    assert k not in inst
                assert round(sum(t["net_pnl"] for t in inst["trades"]), 2) == pytest.approx(
                    inst["net_pnl"]
                )
                for t in inst["trades"]:
                    for k in FORBIDDEN_FEE_SPLIT_KEYS:
                        assert k not in t
                    assert round(t["gross_pnl"] - t["commission"], 2) == pytest.approx(t["net_pnl"])

        # 3. Retired vs Active instance metadata & config badges
        strangle_insts = {
            i["instance_prefix"]: i
            for i in by_strat["immediate_strangle"]["instances"]
        }
        retired = strangle_insts["aaaa1111"]
        assert retired["is_active"] is False
        assert retired["lifecycle_status"] == "RETIRED"
        assert retired["allocated_capital"] == pytest.approx(250_000.0)
        assert retired["trade_count"] == 5
        assert retired["net_pnl"] == pytest.approx(-4000.00)

        active = strangle_insts["bbbb2222"]
        assert active["is_active"] is True
        assert active["lifecycle_status"] == "RUNNING"
        assert active["running_mtm"] == pytest.approx(340.50)
        assert len(active["open_positions"]) == 1
        assert "short_strangle" in active["config_badges"]
        assert "lots=2" in active["config_badges"]
        assert "SL%=25" in active["config_badges"]
        # Merged option label from RAM onto persisted DB trade
        labels = [t["display_symbol"] for t in active["trades"]]
        assert "NIFTY short_strangle 24400/24700" in labels

        orb_inst = by_strat["orb_breakout"]["instances"][0]
        assert orb_inst["lifecycle_status"] == "BLOCKED_BY_RISK"
        assert orb_inst["trade_count"] == 0
        assert orb_inst["running_mtm"] == pytest.approx(180.00)

        # 4. Historical window excludes open-only runners and sets running_mtm=None
        hist_tree = svc.get_attribution(
            mode="paper",
            from_date="2026-10-01",
            to_date="2026-10-29",
            today_ist=date(2026, 10, 30),
        )
        assert hist_tree["includes_today"] is False
        assert hist_tree["totals"]["running_mtm"] is None
        assert hist_tree["strategies"] == []


class TestSlice2FlaskRoutesAndDrilldown:
    def test_attribution_and_instance_trades_endpoints(self, sqlite_db):
        reset_portfolio_manager()
        app = create_app(source="synthetic")
        app.config["TESTING"] = True
        mgr = get_portfolio_manager()
        mgr._trade_persister = LiveTradePersister(sqlite_db)

        today_ist = current_session_date()
        base_exit_utc = datetime.combine(
            today_ist, datetime.min.time(), tzinfo=EXCHANGE_TZ
        ).replace(hour=13, minute=0).astimezone(timezone.utc)

        with sqlite_db.session() as session:
            p_paper = PortfolioRow(
                name="Paper Scalper [facefeed]",
                initial_capital=Decimal("100000"),
                current_cash=Decimal("100500"),
                mode="paper",
                source="mstock",
            )
            session.add(p_paper)
            session.flush()

            for idx in range(5):
                session.add(
                    TradeRow(
                        portfolio_id=p_paper.portfolio_id,
                        symbol=f"TCS_{idx}",
                        strategy_name="vwap_reversion",
                        direction="long",
                        quantity=Decimal("5"),
                        entry_price=Decimal("3500"),
                        exit_price=Decimal("3520"),
                        entry_time=base_exit_utc - timedelta(minutes=10 + idx),
                        exit_time=base_exit_utc + timedelta(minutes=idx),
                        gross_pnl=Decimal("100.00"),
                        commission_total=Decimal("12.00"),
                        net_pnl=Decimal("88.00"),
                        exit_reason="take_profit",
                    )
                )

        try:
            # Verify truncation + full drilldown via DiaryService & HTTP endpoints
            svc = DiaryService(manager=mgr, db_manager=sqlite_db)
            iso_d = today_ist.isoformat()
            trunc_tree = svc.get_attribution(
                mode="paper",
                from_date=iso_d,
                to_date=iso_d,
                inline_trade_limit=2,
            )
            inst_trunc = trunc_tree["strategies"][0]["instances"][0]
            assert inst_trunc["trade_count"] == 5
            assert inst_trunc["trades_truncated"] is True
            assert len(inst_trunc["trades"]) == 2

            with app.test_client() as client:
                r_attr = client.get(
                    f"/api/diary/attribution?mode=paper&from={iso_d}&to={iso_d}"
                )
                assert r_attr.status_code == 200
                body = r_attr.get_json()
                assert body["success"] is True
                assert body["totals"]["trade_count"] == 5
                assert body["totals"]["net_pnl"] == pytest.approx(440.00)
                ikey = body["strategies"][0]["instances"][0]["instance_key"]

                # Drilldown endpoint accepts full instance_key (with ::) and 8-char prefix
                r_drill = client.get(
                    f"/api/diary/instance/{ikey}/trades?mode=paper&from={iso_d}&to={iso_d}"
                )
                assert r_drill.status_code == 200
                drill = r_drill.get_json()
                assert drill["success"] is True
                assert len(drill["instance"]["trades"]) == 5
                assert drill["instance"]["trades_truncated"] is False

                # Live book does not see Paper instance -> 404
                r_live_miss = client.get(
                    f"/api/diary/instance/{ikey}/trades?mode=live&from={iso_d}&to={iso_d}"
                )
                assert r_live_miss.status_code == 404

                # Invalid mode -> 400
                r_bad = client.get("/api/diary/attribution?mode=all")
                assert r_bad.status_code == 400
        finally:
            mgr.shutdown()
            reset_portfolio_manager()
