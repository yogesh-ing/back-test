"""Golden-number tests for the analytics metric math (gap fix #4).

docs/archive/ANALYTICS-TAB-GAPS.md §6: the endpoints were tested, the MATH was not — so the
2026-09-28 methodology fixes (#2 timezone, #5 monthly Sharpe/Sortino/Calmar,
#6 health gate, #7 carry-forward, #8 sentinels/streaks, #10 class split)
each pin their convention here with hand-derivable numbers.
"""

from __future__ import annotations

import math
from datetime import timedelta

import pytest

from backtest.analytics.portfolio import (
    IST,
    AnalyticsService,
    _calculate_streaks,
    _instrument_class,
    _natural_key,
    _parse_ts,
    compute_metrics_from_trades,
    get_health_rating,
)


def _trades(day_pnls, start="2026-09-01"):
    """One trade per (day-offset, pnl) pair, exits stamped IST-naive."""
    import datetime as _dt

    base = _dt.date.fromisoformat(start)
    return [
        {"pnl": pnl, "exit_ts": f"{(base + _dt.timedelta(days=off)).isoformat()}T14:30:00"}
        for off, pnl in day_pnls
    ]


# ---------------------------------------------------------------------------
# Fix #2 — timezone: naive stamps are IST market time
# ---------------------------------------------------------------------------


class TestTimezone:
    def test_naive_timestamp_is_ist(self):
        dt = _parse_ts("2026-09-10T10:00:00")
        assert dt is not None and dt.utcoffset() == timedelta(hours=5, minutes=30)

    def test_aware_timestamp_is_trusted(self):
        dt = _parse_ts("2026-09-10T10:00:00+00:00")
        assert dt is not None and dt.utcoffset() == timedelta(0)

    def test_ist_constant(self):
        assert IST.utcoffset(None) == timedelta(hours=5, minutes=30)


# ---------------------------------------------------------------------------
# Fix #8 — streak convention: breakeven (0) is neutral EVERYWHERE
# ---------------------------------------------------------------------------


class TestStreaks:
    def test_golden_sequence(self):
        s = _calculate_streaks([100, 200, -50, 100, 100, 100])
        assert s["max_win_streak"] == 3
        assert s["max_loss_streak"] == 1
        assert s["current_streak"] == 3 and s["current_streak_is_win"] is True

    def test_breakeven_last_trade_means_no_current_streak(self):
        s = _calculate_streaks([100, 100, 0])
        assert s["current_streak"] == 0
        assert s["max_win_streak"] == 2  # max counters agree: 0 is neutral

    def test_breakeven_breaks_the_current_run(self):
        s = _calculate_streaks([100, 0, 100])
        assert s["current_streak"] == 1  # the run ends at the neutral trade
        assert s["max_win_streak"] == 1


# ---------------------------------------------------------------------------
# Fix #5/#8 — Sharpe (golden), Sortino n/a sentinel, PF null sentinel
# ---------------------------------------------------------------------------


class TestRatios:
    def test_daily_sharpe_golden_number(self):
        # 5 distinct days, capital 100k, rf=0:
        # returns r = [.01, -.005, .015, -.01, .02]; mean = .03/5 = .006
        # mean(r²) = (.0001+.000025+.000225+.0001+.0004)/5 = .00017
        # var(ddof=0) = .00017 − .006² = .000134 → std = .0115758
        # sharpe = √252 · .006/.0115758 = 8.2283 → 8.23
        trades = _trades([(0, 1000), (1, -500), (2, 1500), (3, -1000), (4, 2000)])
        m = compute_metrics_from_trades(trades, allocated_capital=100_000, risk_free_rate=0.0)
        expected = math.sqrt(252.0) * (0.006 / math.sqrt(0.000134))
        assert m["sharpe_ratio"] == pytest.approx(round(expected, 2))
        assert m["sharpe_ratio"] == 8.23

    def test_sortino_is_null_not_sharpe_when_no_losses(self):
        trades = _trades([(0, 1000), (1, 500), (2, 1500)])
        m = compute_metrics_from_trades(trades, allocated_capital=100_000, risk_free_rate=0.0)
        assert m["sortino_ratio"] is None
        assert m["sharpe_ratio"] > 0  # Sharpe itself is real

    def test_sortino_is_a_number_with_downside(self):
        trades = _trades([(0, 1000), (1, -500), (2, 1500), (3, -1000), (4, 2000)])
        m = compute_metrics_from_trades(trades, allocated_capital=100_000, risk_free_rate=0.0)
        assert isinstance(m["sortino_ratio"], float)

    def test_profit_factor_null_when_no_losing_trades(self):
        m = compute_metrics_from_trades(_trades([(0, 1000), (1, 500)]), 100_000)
        assert m["profit_factor"] is None

    def test_profit_factor_finite_with_losses(self):
        m = compute_metrics_from_trades(_trades([(0, 1000), (1, -500)]), 100_000)
        assert m["profit_factor"] == 2.0


class TestCalmar:
    def test_calmar_is_annualized(self):
        """Same trades over 365 days vs ~30 days → the shorter window's
        return is scaled up ~12×, making the two windows comparable."""
        year = _trades([(0, 5000), (364, 5000)])
        month = _trades([(0, 5000), (29, 5000)])
        m_year = compute_metrics_from_trades(year, 100_000, risk_free_rate=0.0)
        m_month = compute_metrics_from_trades(month, 100_000, risk_free_rate=0.0)
        assert m_year["annualized_return_pct"] == pytest.approx(10.0 * 365 / 364, abs=0.02)
        assert m_month["annualized_return_pct"] == pytest.approx(10.0 * 365 / 29, abs=0.2)

    def test_max_dd_from_equity_history_golden(self):
        eq = [{"equity": e} for e in (100_000, 110_000, 99_000, 105_000)]
        m = compute_metrics_from_trades(_trades([(0, 100), (1, -100)]), 100_000, equity_history=eq)
        assert m["max_drawdown_pct"] == 10.0  # (99k−110k)/110k
        assert m["max_drawdown_amount"] == 11_000.0


# ---------------------------------------------------------------------------
# Fix #6 — health rating: n≥30 gate, PF criterion instead of win rate
# ---------------------------------------------------------------------------


class TestHealthRating:
    def test_small_sample_is_gray_not_green(self):
        h = get_health_rating(2.5, 3.0, profit_factor=3.0, total_trades=3)
        assert h["status"] == "gray" and "Insufficient" in h["badge"]

    def test_low_winrate_high_rr_book_can_be_green(self):
        # 40% win rate used to hard-block green; PF is the criterion now.
        h = get_health_rating(1.8, 5.0, win_rate=40.0, profit_factor=2.0, total_trades=50)
        assert h["status"] == "green"

    def test_weak_profit_factor_blocks_green(self):
        h = get_health_rating(1.8, 5.0, profit_factor=1.1, total_trades=50)
        assert h["status"] == "yellow"

    def test_null_profit_factor_means_infinity_and_passes(self):
        h = get_health_rating(1.8, 5.0, profit_factor=None, total_trades=50)
        assert h["status"] == "green"

    def test_legacy_call_without_new_kwargs_still_works(self):
        assert get_health_rating(1.8, 5.0, 55.0)["status"] == "green"
        assert get_health_rating(0.6, 22.0, 40.0)["status"] == "red"


# ---------------------------------------------------------------------------
# Fix #5 — monthly breakdown: a REAL Sharpe or an honest None
# ---------------------------------------------------------------------------


class TestMonthlyBreakdown:
    def _svc(self):
        svc = AnalyticsService.__new__(AnalyticsService)  # no manager needed
        return svc

    def test_single_day_month_has_no_sharpe(self):
        rows = self._svc()._build_monthly_breakdown(_trades([(0, 500), (0, 700)]), capital=100_000)
        assert rows[0]["sharpe_ratio"] is None  # 1 trading day ≠ a Sharpe

    def test_multi_day_month_sharpe_is_annualized_daily(self):
        trades = _trades([(0, 1000), (1, -500), (2, 1500)])
        rows = self._svc()._build_monthly_breakdown(trades, capital=100_000)
        # daily returns [.01, -.005, .015]: mean=.006667, std(ddof=0)=.008498
        expected = round((0.0066667 / 0.0084984) * math.sqrt(252.0), 2)
        assert rows[0]["sharpe_ratio"] == pytest.approx(expected, abs=0.02)


# ---------------------------------------------------------------------------
# Fix #10 — instrument-class tagging + split distributions
# ---------------------------------------------------------------------------


class TestInstrumentClass:
    def test_kind_tag_wins(self):
        assert _instrument_class({"kind": "option", "symbol": "RELIANCE"}) == "option"
        assert _instrument_class({"kind": "equity", "symbol": "NIFTY26OCT24800CE"}) == "equity"

    def test_symbol_heuristic_for_untagged_rows(self):
        assert _instrument_class({"symbol": "NIFTY26OCT24800CE"}) == "option"
        assert _instrument_class({"symbol": "NIFTY26OCT24800PE"}) == "option"
        assert _instrument_class({"symbol": "RELIANCE"}) == "equity"

    def test_distribution_splits_a_mixed_book(self):
        svc = AnalyticsService.__new__(AnalyticsService)
        trades = [
            {
                "pnl": 500.0,
                "kind": "equity",
                "symbol": "RELIANCE",
                "exit_ts": "2026-09-01T11:00:00",
            },
            {
                "pnl": -200.0,
                "kind": "option",
                "symbol": "NIFTY26OCT24800CE",
                "exit_ts": "2026-09-02T11:00:00",
            },
        ]
        dist = svc._build_trade_distribution(trades)
        assert set(dist["by_class"]) == {"equity", "option"}
        assert dist["by_class"]["equity"]["trades"] == 1
        assert any("Mixed book" in i for i in dist["insights"])


# ---------------------------------------------------------------------------
# Fix #7 — portfolio curve carry-forward
# ---------------------------------------------------------------------------


class _FakeMgr:
    def __init__(self, runners):
        self._runners = runners

    def get_runner(self, iid):
        return self._runners.get(iid)


class TestPortfolioCurveCarryForward:
    def test_late_starter_does_not_jump_the_curve(self):
        """Runner B starts on day 2 with 50k allocated. The old points-map
        summed only reported dates → day-1 was 100k, day-2 leapt to 151k.
        Carry-forward seeds B's allocation on day 1: baseline 150k, and the
        day-2 move is only B's actual +1k."""
        svc = AnalyticsService.__new__(AnalyticsService)
        svc.mgr = _FakeMgr({})
        # PRD-003: the curve is built from collect_books() records, so the
        # per-runner equity curve rides along on the book rather than being
        # looked up again per runner here.
        books = [
            {
                "instance_id": "A",
                "allocated_capital": 100_000,
                "equity_curve": [
                    {"ts": "2026-09-01T10:00:00", "equity": 100_000},
                    {"ts": "2026-09-02T10:00:00", "equity": 100_000},
                ],
            },
            {
                "instance_id": "B",
                "allocated_capital": 50_000,
                "equity_curve": [{"ts": "2026-09-02T10:00:00", "equity": 51_000}],
            },
        ]
        curve = svc._build_portfolio_equity_curve(books, "30d")
        assert [c["equity"] for c in curve] == [150_000.0, 151_000.0]
        assert curve[0]["pnl"] == 0.0  # baseline = ACTUAL start equity
        assert curve[1]["pnl"] == 1_000.0
        assert all(c["drawdown_pct"] == 0.0 for c in curve)


# ---------------------------------------------------------------------------
# Fix #3 — persisted-history merge plumbing
# ---------------------------------------------------------------------------


class TestHistoryMerge:
    def test_natural_key_matches_persister_convention(self):
        t = {"exit_ts": "2026-09-01T14:30:00.123456", "symbol": "RELIANCE", "pnl": 100.456}
        assert _natural_key(t) == ("2026-09-01T14:30:00", "RELIANCE", "100.46")

    def test_memory_wins_on_collision_and_db_extends(self):
        svc = AnalyticsService.__new__(AnalyticsService)
        svc.mgr = type("M", (), {"_trade_persister": None})()

        class R:
            instance_id = "abcd1234efgh"
            closed_trades = [
                {"symbol": "X", "pnl": 100.0, "exit_ts": "2026-09-02T10:00:00", "kind": "equity"},
            ]

        merged, history = svc._runner_trades_with_history(R())
        assert history == {
            "memory_trades": 1,
            "persisted_trades": 0,
            "merged_trades": 1,
            "source": "memory",
        }
        assert merged[0]["instrument_class"] == "equity"


# ---------------------------------------------------------------------------
# §2.3 — degradation detector absolute floor
# ---------------------------------------------------------------------------


class TestDegradationFloor:
    def test_always_bad_strategy_now_alerts(self):
        svc = AnalyticsService.__new__(AnalyticsService)
        rolling = [{"rolling_sharpe": s} for s in (-0.5, -0.8, -0.6, -0.9, -0.7)]
        alert = svc._detect_edge_degradation(rolling, {})
        assert alert is not None and alert["alert_type"] == "sharpe_floor"
        assert alert["severity"] == "critical"

    def test_healthy_book_stays_quiet(self):
        svc = AnalyticsService.__new__(AnalyticsService)
        rolling = [{"rolling_sharpe": s} for s in (2.0, 2.1, 2.2, 2.1, 2.3)]
        assert svc._detect_edge_degradation(rolling, {}) is None
