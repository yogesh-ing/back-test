"""Slice 6 — Backtest-vs-Paper Reality Matrix & Constrained Live Roster Selector tests.

Covers:
1. Canonical root underlying extraction across spot, index, and NSE option symbols
   (e.g. ``NIFTY26NOV24500CE`` & ``NSE_INDEX|Nifty 50`` -> ``NIFTY``).
2. Backtest-vs-Paper Sim-to-Real matching from persisted ``BacktestRun`` SQL rows:
   - Exact ``(strategy_id, underlying, timeframe)`` priority with ``backtest_driver``
     preferred over ``quick_screen``.
   - Decimal-fraction ``BacktestRun.max_drawdown`` / ``total_return`` normalized to ``%``
     while ``win_rate`` stays on ``0..100%``.
   - ``monte_carlo_p_profit`` extracted from ``readiness["checks"]``.
   - ``CURVE_FIT_HAIRCUT`` vs ``RETENTION_STRONG`` classification.
3. Full multi-runner 30-session Tournament under real-world Live constraints:
   - Composite capital-efficiency ranking (Return/DD + PF + Drag + Sim-to-Real).
   - Multi-timeframe deduplication on same ``(strategy, underlying)``.
   - Daily IST Net P&L Pearson correlation pruning (``PRUNED_CORRELATED_CLONE``).
   - Single-underlying concentration cap across spot + options (``DEFERRED_UNDERLYING_CAP``).
   - Live Option Entitlement hold (``HOLD_ENTITLEMENT_REVIEW`` for ``short_strangle``).
   - Cash reserve buffer & slot cap (``Wave 1`` / ``Wave 2`` vs ``Warm Bench``).
4. ``GET /api/diary/roster-plan`` contract, parameter validation, strict ``paper`` vs
   ``live`` segregation, and E2 single-sum ``Commission`` invariant.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest

from backtest.db import DatabaseManager
from backtest.db.config import DatabaseConfig
from backtest.db.models import BacktestRun, Base
from backtest.diary import (
    canonical_underlying,
    compute_daily_pnl_correlation,
    compute_tail_crash_overlap,
    count_active_ist_sessions,
    evaluate_edge_trajectory,
    evaluate_sim_to_real,
    extract_worst_red_days,
    match_backtest_benchmark,
)
from backtest.diary.service import DiaryService
from backtest.forward.portfolio_manager import reset_portfolio_manager
from backtest.timekeeping import EXCHANGE_TZ
from backtest.web.app import create_app

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


def _make_weekdays(start: date, count: int) -> list[date]:
    days: list[date] = []
    cur = start
    while len(days) < count:
        if cur.weekday() < 5:
            days.append(cur)
        cur += timedelta(days=1)
    return days


class _StubConfig:
    def __init__(
        self,
        *,
        strategy_name: str,
        symbols: list[str],
        timeframe: str,
        initial_capital: float = 60_000.0,
        instrument: dict[str, Any] | None = None,
    ):
        self.strategy_name = strategy_name
        self.symbols = symbols
        self.timeframe = timeframe
        self.initial_capital = initial_capital
        self.instrument = instrument or {"type": "equity"}
        self.strategy_params = {}


class _StubRunner:
    def __init__(
        self,
        runner_id: str,
        name: str,
        cfg: _StubConfig,
        closed_trades: list[dict[str, Any]],
    ):
        self.runner_id = runner_id
        self.name = name
        self.config = cfg
        self.closed_trades = closed_trades
        self.positions = {}
        self.last_price = {}


def _build_series_trades(
    *,
    symbol: str,
    session_days: list[date],
    daily_gross_pattern: list[float],
    commission_per_trade: float = 20.0,
) -> list[dict[str, Any]]:
    trades: list[dict[str, Any]] = []
    for idx, (day, gross) in enumerate(zip(session_days, daily_gross_pattern)):
        entry_ist = datetime(day.year, day.month, day.day, 9, 35, tzinfo=EXCHANGE_TZ)
        exit_ist = datetime(day.year, day.month, day.day, 11, 10, tzinfo=EXCHANGE_TZ)
        net = round(gross - commission_per_trade, 2)
        trades.append(
            {
                "trade_id": f"{symbol}-{idx:03d}",
                "symbol": symbol,
                "side": "LONG",
                "qty": 1,
                "entry_price": 100.0,
                "exit_price": 102.0 if gross >= 0 else 99.0,
                "entry_ts": entry_ist.astimezone(timezone.utc).isoformat(),
                "exit_ts": exit_ist.astimezone(timezone.utc).isoformat(),
                "gross_pnl": round(gross, 2),
                "commission": round(commission_per_trade, 2),
                "pnl": net,
                "net_pnl": net,
                "exit_reason": "target" if gross >= 0 else "stop_loss",
            }
        )
    return trades


class TestUnderlyingAndBacktestMatching:
    def test_canonical_underlying_groups_spot_and_option_contracts(self):
        assert canonical_underlying("NSE_INDEX|Nifty 50") == "NIFTY"
        assert canonical_underlying("NIFTY26NOV24500CE") == "NIFTY"
        assert canonical_underlying("BANKNIFTY26OCT51500PE") == "BANKNIFTY"
        assert canonical_underlying("NIFTY BANK") == "BANKNIFTY"
        assert canonical_underlying("NSE:RELIANCE-EQ") == "RELIANCE"

    def test_backtest_matcher_prefers_exact_backtest_driver_and_flags_haircut(self):
        runs = [
            {
                "run_id": "qs-1",
                "engine": "quick_screen",
                "strategy_id": "orb_breakout",
                "symbol": "NIFTY 50",
                "timeframe": "5m",
                "profit_factor": 2.80,
                "win_rate_pct": 62.0,
                "max_drawdown_pct": 4.0,
                "total_trades": 55,
                "created_at": "2026-10-20T10:00:00Z",
            },
            {
                "run_id": "bt-1",
                "engine": "backtest_driver",
                "strategy_id": "orb_breakout",
                "symbol": "NIFTY26OCT24500CE",
                "timeframe": "5min",
                "profit_factor": 2.20,
                "win_rate_pct": 58.0,
                "max_drawdown_pct": 6.5,
                "total_trades": 80,
                "monte_carlo_profit_prob_pct": 84.0,
                "created_at": "2026-10-18T10:00:00Z",
            },
        ]
        matched = match_backtest_benchmark(
            strategy_name="orb_breakout",
            primary_underlying="NIFTY",
            timeframe="5m",
            backtest_runs=runs,
        )
        assert matched["matched"] is True
        assert matched["match_tier"] == "exact"
        assert matched["run_id"] == "bt-1"

        strong = evaluate_sim_to_real(
            paper_pf=1.75,
            paper_unbounded_pf=False,
            paper_win_rate_pct=55.0,
            paper_max_dd_pct=5.2,
            benchmark=matched,
        )
        assert strong["status"] == "RETENTION_STRONG"
        assert strong["pf_retention_pct"] == pytest.approx(79.5, abs=0.2)

        haircut = evaluate_sim_to_real(
            paper_pf=0.92,
            paper_unbounded_pf=False,
            paper_win_rate_pct=41.0,
            paper_max_dd_pct=9.8,
            benchmark=matched,
        )
        assert haircut["status"] == "CURVE_FIT_HAIRCUT"
        assert haircut["pf_retention_pct"] < 45.0


class TestMultiRunnerRosterTournament:
    def test_5_stage_tournament_prunes_tf_dupes_clones_and_enforces_caps(
        self, sqlite_db
    ):
        today_ist = date(2026, 10, 30)
        days = _make_weekdays(date(2026, 9, 21), 22)

        # Persist a real BacktestRun row to verify decimal-fraction max_drawdown scaling
        with sqlite_db.session() as session:
            session.add(
                BacktestRun(
                    run_id="bt-orb-nifty-5m",
                    config_hash="a" * 64,
                    code_fingerprint="b" * 40,
                    engine="backtest_driver",
                    strategy_id="orb_breakout",
                    symbol="NIFTY 50",
                    timeframe="5min",
                    date_from=date(2026, 1, 1),
                    date_to=date(2026, 9, 15),
                    capital=Decimal("100000"),
                    params={},
                    config={"strategy": "orb_breakout", "symbol": "NIFTY 50"},
                    total_return=Decimal("0.185000"),
                    max_drawdown=Decimal("0.062000"),  # 6.2% decimal fraction
                    win_rate=Decimal("58.50"),         # already 0-100 scale
                    profit_factor=Decimal("2.0500"),
                    sharpe=Decimal("1.9200"),
                    total_trades=64,
                    metrics={},
                    readiness={
                        "checks": [
                            {
                                "id": "monte_carlo_p_profit",
                                "value": "86%",
                                "status": "green",
                            }
                        ]
                    },
                )
            )
            session.commit()

        pat_a = [650.0 if i % 3 != 0 else -220.0 for i in range(22)]
        pat_a_15m = [480.0 if i % 3 != 0 else -210.0 for i in range(22)]
        pat_clone = [510.0 if i % 3 != 0 else -225.0 for i in range(22)]
        pat_b = [-190.0 if i % 3 != 0 else 820.0 for i in range(22)]
        pat_nifty_opt = [540.0 if i % 2 == 0 else -180.0 for i in range(22)]
        pat_c = [520.0 if (i + 1) % 2 == 0 else -170.0 for i in range(22)]
        pat_d = [430.0 if (i + 1) % 3 != 0 else -175.0 for i in range(22)]
        pat_strangle = [580.0 if i % 4 != 0 else -200.0 for i in range(22)]
        pat_low_sample = [400.0, 350.0, -120.0, 410.0, 380.0, -100.0]

        runners_list = [
            _StubRunner(
                "aaaa1111-0000",
                "ORB NIFTY 5m",
                _StubConfig(
                    strategy_name="orb_breakout",
                    symbols=["NIFTY"],
                    timeframe="5min",
                    initial_capital=90_000.0,
                ),
                _build_series_trades(
                    symbol="NIFTY", session_days=days, daily_gross_pattern=pat_a
                ),
            ),
            _StubRunner(
                "aaaa2222-0000",
                "ORB NIFTY 15m",
                _StubConfig(
                    strategy_name="orb_breakout",
                    symbols=["NIFTY"],
                    timeframe="15min",
                    initial_capital=60_000.0,
                ),
                _build_series_trades(
                    symbol="NIFTY", session_days=days, daily_gross_pattern=pat_a_15m
                ),
            ),
            _StubRunner(
                "bbbb1111-0000",
                "Momentum Clone FINNIFTY",
                _StubConfig(
                    strategy_name="ema_momentum",
                    symbols=["FINNIFTY"],
                    timeframe="5min",
                    initial_capital=60_000.0,
                ),
                _build_series_trades(
                    symbol="FINNIFTY",
                    session_days=days,
                    daily_gross_pattern=pat_clone,
                ),
            ),
            _StubRunner(
                "cccc1111-0000",
                "VWAP Revert BANKNIFTY",
                _StubConfig(
                    strategy_name="vwap_revert",
                    symbols=["BANKNIFTY"],
                    timeframe="5min",
                    initial_capital=80_000.0,
                ),
                _build_series_trades(
                    symbol="BANKNIFTY", session_days=days, daily_gross_pattern=pat_b
                ),
            ),
            _StubRunner(
                "dddd1111-0000",
                "CE Instant NIFTY Opt",
                _StubConfig(
                    strategy_name="ce_instant_buy",
                    symbols=["NIFTY 50"],
                    timeframe="5min",
                    initial_capital=60_000.0,
                    instrument={
                        "type": "option",
                        "expression": {"structure": "long_call"},
                    },
                ),
                _build_series_trades(
                    symbol="NIFTY26OCT24500CE",
                    session_days=days,
                    daily_gross_pattern=pat_nifty_opt,
                ),
            ),
            _StubRunner(
                "eeee1111-0000",
                "Swing Pullback RELIANCE",
                _StubConfig(
                    strategy_name="ema_pullback",
                    symbols=["RELIANCE"],
                    timeframe="15min",
                    initial_capital=60_000.0,
                ),
                _build_series_trades(
                    symbol="RELIANCE", session_days=days, daily_gross_pattern=pat_c
                ),
            ),
            _StubRunner(
                "ffff1111-0000",
                "Breakout TCS",
                _StubConfig(
                    strategy_name="donchian_trend",
                    symbols=["TCS"],
                    timeframe="15min",
                    initial_capital=60_000.0,
                ),
                _build_series_trades(
                    symbol="TCS", session_days=days, daily_gross_pattern=pat_d
                ),
            ),
            _StubRunner(
                "99991111-0000",
                "Short Strangle ICICIBANK",
                _StubConfig(
                    strategy_name="theta_harvest",
                    symbols=["ICICIBANK"],
                    timeframe="15min",
                    initial_capital=60_000.0,
                    instrument={
                        "type": "option",
                        "expression": {"structure": "short_strangle"},
                    },
                ),
                _build_series_trades(
                    symbol="ICICIBANK26OCT1300CE",
                    session_days=days,
                    daily_gross_pattern=pat_strangle,
                ),
            ),
            _StubRunner(
                "88881111-0000",
                "Early Trial INFY",
                _StubConfig(
                    strategy_name="rsi_bounce",
                    symbols=["INFY"],
                    timeframe="5min",
                    initial_capital=50_000.0,
                ),
                _build_series_trades(
                    symbol="INFY",
                    session_days=days[:6],
                    daily_gross_pattern=pat_low_sample,
                ),
            ),
        ]

        class _FakeMgr:
            def __init__(self):
                self._by_id = {r.runner_id: r for r in runners_list}

            def get_runner(self, rid: str):
                return self._by_id.get(rid)

            def list_instances(self, mode: str | None = None):
                if mode == "live":
                    return []
                return [
                    {
                        "instance_id": r.runner_id,
                        "name": r.name,
                        "strategy_name": r.config.strategy_name,
                        "symbols": r.config.symbols,
                        "timeframe": r.config.timeframe,
                        "allocated_capital": r.config.initial_capital,
                        "instrument": r.config.instrument,
                        "mode": "paper",
                        "status": "RUNNING",
                        "trade_health": "OK",
                        "open_pnl": 0.0,
                        "open_positions": 0,
                    }
                    for r in runners_list
                ]

        svc = DiaryService(manager=_FakeMgr(), db_manager=sqlite_db)
        plan = svc.get_live_roster_plan(
            mode="paper",
            from_date="2026-09-20",
            to_date="2026-10-30",
            today_ist=today_ist,
            live_capital=300_000.0,
            reserve_pct=20.0,
            max_underlying_pct=40.0,
            max_correlation=0.65,
            max_live_runners=5,
        )

        _assert_no_fee_split_keys(plan)
        by_label = {c["instance_label"]: c for c in plan["candidates"]}

        # 1. ORB NIFTY 5m selected & matched to SQL BacktestRun (6.2% DD, 86% MC)
        orb_5m = by_label["ORB NIFTY 5m [aaaa1111]"]
        assert orb_5m["decision"] == "SELECTED_LIVE_ROSTER"
        assert orb_5m["sim_to_real"]["has_backtest"] is True
        assert orb_5m["sim_to_real"]["benchmark"]["max_drawdown_pct"] == pytest.approx(
            6.2, abs=0.05
        )
        assert orb_5m["sim_to_real"]["benchmark"][
            "monte_carlo_profit_prob_pct"
        ] == pytest.approx(86.0)

        # 2. ORB NIFTY 15m pruned as same-(strategy, NIFTY) multi-timeframe duplicate
        assert (
            by_label["ORB NIFTY 15m [aaaa2222]"]["decision"]
            == "PRUNED_TIMEFRAME_DUPLICATE"
        )

        # 3. Momentum Clone FINNIFTY pruned by daily IST Net P&L correlation > 0.65
        clone_row = by_label["Momentum Clone FINNIFTY [bbbb1111]"]
        assert clone_row["decision"] == "PRUNED_CORRELATED_CLONE"
        assert clone_row["max_corr_with_selected"] > 0.90

        # 4. CE Instant NIFTY Opt deferred because 90k + 60k = 150k > 120k (40% NIFTY cap)
        assert (
            by_label["CE Instant NIFTY Opt [dddd1111]"]["decision"]
            == "DEFERRED_UNDERLYING_CAP"
        )

        # 5. Short Strangle held for Live Option Entitlement review
        assert (
            by_label["Short Strangle ICICIBANK [99991111]"]["decision"]
            == "HOLD_ENTITLEMENT_REVIEW"
        )

        # 6. Early Trial INFY disqualified on 6-gate check
        assert (
            by_label["Early Trial INFY [88881111]"]["decision"]
            == "DISQUALIFIED_GATES"
        )

        # 7. Selected roster respects 240k deployable cap (90k + 60k + 60k = 210k; 80k -> Bench)
        selected_labels = [r["instance_label"] for r in plan["selected_roster"]]
        assert "ORB NIFTY 5m [aaaa1111]" in selected_labels
        assert "Breakout TCS [ffff1111]" in selected_labels
        assert "Swing Pullback RELIANCE [eeee1111]" in selected_labels
        assert (
            by_label["VWAP Revert BANKNIFTY [cccc1111]"]["decision"]
            == "DEFERRED_CAPITAL_CAP"
        )
        assert plan["summary"]["allocated_capital_inr"] == pytest.approx(210_000.0)
        assert plan["summary"]["reserve_inr"] == pytest.approx(60_000.0)

        # 8. Live book remains strictly empty and segregated from Paper tournament
        live_plan = svc.get_live_roster_plan(
            mode="live",
            from_date="2026-09-20",
            to_date="2026-10-30",
            today_ist=today_ist,
        )
        assert live_plan["summary"]["total_evaluated"] == 0


class TestRosterPlanApiEndpoint:
    def test_roster_plan_route_and_diary_page_controls(self):
        reset_portfolio_manager()
        app = create_app()
        client = app.test_client()

        qs = (
            "mode=paper&live_capital=400000&reserve_pct=25"
            "&max_underlying_pct=35&max_correlation=0.60&max_live_runners=4"
        )
        resp = client.get(f"/api/diary/roster-plan?{qs}")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["success"] is True
        assert body["mode"] == "paper"
        assert body["constraints"]["live_capital"] == pytest.approx(400_000.0)
        assert body["constraints"]["deployable_budget"] == pytest.approx(300_000.0)
        assert body["constraints"]["max_correlation"] == pytest.approx(0.60)
        _assert_no_fee_split_keys(body)

        bad_corr = client.get("/api/diary/roster-plan?mode=paper&max_correlation=1.8")
        assert bad_corr.status_code == 400

        bad_mode = client.get("/api/diary/roster-plan?mode=blended")
        assert bad_mode.status_code == 400

        html_resp = client.get("/diary")
        assert html_resp.status_code == 200
        html = html_resp.get_data(as_text=True)
        assert 'id="diary-roster-block"' in html
        assert 'id="diary-roster-form"' in html
        assert 'id="diary-roster-min-sessions"' in html
        assert 'id="diary-roster-block-decay"' in html
        assert 'id="diary-roster-log-journal"' in html
        assert 'id="diary-roster-table-wrap"' in html
        assert 'data-open-edge-playbook="roster"' in html
        assert "Tail Co-Crash" in html
        assert "Recent Half Decay" in html

        # New safeguard parameter bounds
        ok_guards = client.get(
            "/api/diary/roster-plan?mode=paper&min_session_days=7"
            "&block_decaying_edge=false&max_tail_overlap=2"
        )
        assert ok_guards.status_code == 200
        gbody = ok_guards.get_json()
        assert gbody["constraints"]["min_session_days"] == 7
        assert gbody["constraints"]["block_decaying_edge"] is False
        assert gbody["constraints"]["max_tail_overlap"] == 2

        bad_sess = client.get("/api/diary/roster-plan?mode=paper&min_session_days=0")
        assert bad_sess.status_code == 400
        bad_tail = client.get("/api/diary/roster-plan?mode=paper&max_tail_overlap=9")
        assert bad_tail.status_code == 400


class TestInstitutionalRosterSafeguards:
    """Tests for Session Breadth, H1->H2 Edge Decay, and Tail Co-Crash Overlap."""

    def test_count_active_sessions_and_half_split_trajectory(self):
        # 16 trades on 1 day: H1 strongly positive, H2 net negative -> DECAYING_RECENT_EDGE
        trades_decay: list[dict[str, Any]] = []
        base_dt = datetime(2026, 10, 29, 9, 20, tzinfo=EXCHANGE_TZ)
        for idx in range(16):
            net = 600.0 if idx < 8 else -120.0
            exit_dt = base_dt + timedelta(minutes=12 * (idx + 1))
            trades_decay.append(
                {
                    "entry_date_ist": "2026-10-29",
                    "exit_date_ist": "2026-10-29",
                    "exit_ts_utc": exit_dt.astimezone(timezone.utc).isoformat(),
                    "net_pnl": net,
                }
            )
        assert count_active_ist_sessions(trades_decay) == 1
        traj = evaluate_edge_trajectory(trades_decay)
        assert traj["status"] == "DECAYING_RECENT_EDGE"
        assert traj["h1_expectancy_net"] > 0
        assert traj["h2_expectancy_net"] < 0

        # Steady positive trajectory across both halves
        trades_steady = [
            {
                "exit_date_ist": f"2026-10-{10 + (idx % 10):02d}",
                "exit_ts_utc": (
                    base_dt + timedelta(days=idx)
                ).astimezone(timezone.utc).isoformat(),
                "net_pnl": 350.0 if idx % 3 != 0 else -150.0,
            }
            for idx in range(18)
        ]
        assert count_active_ist_sessions(trades_steady) == 10
        assert evaluate_edge_trajectory(trades_steady)["status"] == "STEADY_OR_EXPANDING"

    def test_tail_crash_overlap_detects_shared_worst_red_days_despite_low_linear_corr(self):
        days = _make_weekdays(date(2026, 9, 28), 20)
        crash_idx = {3, 10, 16}
        map_a: dict[str, Decimal] = {}
        map_b: dict[str, Decimal] = {}
        for idx, d in enumerate(days):
            iso = d.isoformat()
            if idx in crash_idx:
                map_a[iso] = Decimal("-900.00")
                map_b[iso] = Decimal("-880.00")
            else:
                map_a[iso] = Decimal("1400.00") if idx % 2 == 0 else Decimal("100.00")
                map_b[iso] = Decimal("100.00") if idx % 2 == 0 else Decimal("1400.00")

        day_strs = [d.isoformat() for d in days]
        rho = compute_daily_pnl_correlation(map_a, map_b, all_session_days=day_strs)
        assert rho is not None and rho < 0.45
        assert len(extract_worst_red_days(map_a, top_k=3)) == 3
        tail = compute_tail_crash_overlap(map_a, map_b, top_k=3)
        assert tail["shared_count"] == 3
        assert len(tail["shared_dates"]) == 3

    def test_tournament_enforces_session_breadth_h2_decay_and_tail_cocrash(self, sqlite_db):
        days = _make_weekdays(date(2026, 9, 25), 22)
        today_ist = date(2026, 10, 30)

        # Anchor winner: higher R/DD & PF on NIFTY, crashes on idx {4, 11, 18}
        pat_anchor = [
            -550.0 if i in (4, 11, 18) else (1750.0 if i % 2 == 0 else 350.0)
            for i in range(22)
        ]
        # Tail co-crash peer on HDFCBANK: opposite sign on normal days (low rho < 0.45),
        # but shares the EXACT 3 worst crash days {4, 11, 18}!
        pat_tail_clone = [
            -700.0 if i in (4, 11, 18) else (200.0 if i % 2 == 0 else 1200.0)
            for i in range(22)
        ]
        # Front-loaded luck runner on SBIN: +1800 on first 11 sessions, -150 on last 11 sessions
        # Total Net > 0, PF > 2.0, passes 6/6 gates, but E[H2] < 0!
        pat_h2_decay = [1800.0 if i < 11 else -150.0 for i in range(22)]

        # 1-day scalper on AXISBANK: 22 trades all on 2026-10-28 (18 wins, 4 small losses)
        one_day_trades: list[dict[str, Any]] = []
        one_day_base = datetime(2026, 10, 28, 9, 20, tzinfo=EXCHANGE_TZ)
        for t_idx in range(22):
            gross = 450.0 if t_idx % 5 != 4 else -220.0
            comm = 20.0
            ent = one_day_base + timedelta(minutes=10 * t_idx)
            ext = ent + timedelta(minutes=6)
            one_day_trades.append(
                {
                    "symbol": "AXISBANK",
                    "side": "BUY",
                    "quantity": 25,
                    "entry_price": 1150.0,
                    "exit_price": 1158.0 if gross > 0 else 1141.0,
                    "entry_time": ent.astimezone(timezone.utc).isoformat(),
                    "exit_time": ext.astimezone(timezone.utc).isoformat(),
                    "gross_pnl": gross,
                    "commission": comm,
                    "pnl": round(gross - comm, 2),
                    "exit_reason": "take_profit" if gross > 0 else "stop_loss",
                }
            )

        runners_list = [
            _StubRunner(
                "1010aaaa-0000",
                "Anchor Trend NIFTY",
                _StubConfig(
                    strategy_name="orb_breakout",
                    symbols=["NIFTY"],
                    timeframe="5min",
                    initial_capital=75_000.0,
                ),
                _build_series_trades(
                    symbol="NIFTY",
                    session_days=days,
                    daily_gross_pattern=pat_anchor,
                ),
            ),
            _StubRunner(
                "2020bbbb-0000",
                "Tail CoCrash HDFCBANK",
                _StubConfig(
                    strategy_name="vwap_revert",
                    symbols=["HDFCBANK"],
                    timeframe="15min",
                    initial_capital=65_000.0,
                ),
                _build_series_trades(
                    symbol="HDFCBANK",
                    session_days=days,
                    daily_gross_pattern=pat_tail_clone,
                ),
            ),
            _StubRunner(
                "3030cccc-0000",
                "FrontLoaded SBIN",
                _StubConfig(
                    strategy_name="ema_cross",
                    symbols=["SBIN"],
                    timeframe="5min",
                    initial_capital=60_000.0,
                ),
                _build_series_trades(
                    symbol="SBIN",
                    session_days=days,
                    daily_gross_pattern=pat_h2_decay,
                ),
            ),
            _StubRunner(
                "4040dddd-0000",
                "OneDay Scalper AXISBANK",
                _StubConfig(
                    strategy_name="scalp_burst",
                    symbols=["AXISBANK"],
                    timeframe="1min",
                    initial_capital=50_000.0,
                ),
                one_day_trades,
            ),
        ]

        class _GuardMgr:
            def __init__(self):
                self._by_id = {r.runner_id: r for r in runners_list}

            def get_runner(self, rid: str):
                return self._by_id.get(rid)

            def list_instances(self, mode: str | None = None):
                if mode == "live":
                    return []
                return [
                    {
                        "instance_id": r.runner_id,
                        "name": r.name,
                        "strategy_name": r.config.strategy_name,
                        "symbols": r.config.symbols,
                        "timeframe": r.config.timeframe,
                        "allocated_capital": r.config.initial_capital,
                        "instrument": r.config.instrument,
                        "mode": "paper",
                        "status": "RUNNING",
                        "trade_health": "OK",
                        "open_pnl": 0.0,
                        "open_positions": 0,
                    }
                    for r in runners_list
                ]

        svc = DiaryService(manager=_GuardMgr(), db_manager=sqlite_db)
        plan = svc.get_live_roster_plan(
            mode="paper",
            from_date="2026-09-20",
            to_date="2026-10-30",
            today_ist=today_ist,
            live_capital=300_000.0,
        )
        by_label = {c["instance_label"]: c for c in plan["candidates"]}

        # All 4 pass individual 6/6 gates (backwards-compatible gates_total == 6)
        for row in plan["candidates"]:
            assert row["gates_total"] == 6
            assert row["verdict"] == "PROMOTE_CANDIDATE"

        assert by_label["Anchor Trend NIFTY [1010aaaa]"]["decision"] == "SELECTED_LIVE_ROSTER"

        tail_row = by_label["Tail CoCrash HDFCBANK [2020bbbb]"]
        assert tail_row["max_corr_with_selected"] < 0.65
        assert tail_row["max_tail_overlap_days"] == 3
        assert tail_row["decision"] == "PRUNED_TAIL_CRASH_OVERLAP"

        decay_row = by_label["FrontLoaded SBIN [3030cccc]"]
        assert decay_row["trajectory"]["status"] == "DECAYING_RECENT_EDGE"
        assert decay_row["decision"] == "HOLD_RECENT_DECAY"

        sess_row = by_label["OneDay Scalper AXISBANK [4040dddd]"]
        assert sess_row["active_session_days"] == 1
        assert sess_row["decision"] == "HOLD_SESSION_BREADTH"

        assert plan["summary"]["held_session_breadth_count"] == 1
        assert plan["summary"]["held_recent_decay_count"] == 1
        assert plan["summary"]["pruned_tail_crash_count"] == 1

        # Relaxing min_session_days=1 and block_decaying_edge=False unblocks SBIN & AXISBANK
        relaxed = svc.get_live_roster_plan(
            mode="paper",
            from_date="2026-09-20",
            to_date="2026-10-30",
            today_ist=today_ist,
            live_capital=300_000.0,
            min_session_days=1,
            block_decaying_edge=False,
        )
        relaxed_by_label = {c["instance_label"]: c for c in relaxed["candidates"]}
        assert (
            relaxed_by_label["FrontLoaded SBIN [3030cccc]"]["decision"]
            == "SELECTED_LIVE_ROSTER"
        )
        assert (
            relaxed_by_label["OneDay Scalper AXISBANK [4040dddd]"]["decision"]
            == "SELECTED_LIVE_ROSTER"
        )
