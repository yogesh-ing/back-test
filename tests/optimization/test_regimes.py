"""Regime breakdown — PRD backTest-enhance Part 2 §6.1.

"A strategy that only works in bull markets should not be certified for
all-weather paper trading." The table exists to make that visible before
paper trading rather than after.

The engine maths is pinned here; the wiring through a real run is pinned in
``test_regime_wiring.py``.
"""

from __future__ import annotations

import datetime

import pytest

from backtest.optimization.regimes import (
    MIN_BARS,
    REGIME_BANDS,
    UNKNOWN_BAND_LABEL,
    regime_breakdown,
)


def _in(day: datetime.date, start: str, end: str) -> bool:
    return datetime.date.fromisoformat(start) <= day <= datetime.date.fromisoformat(end)


def series(start: str, end: str, growth: float, shock: tuple[str, str, float] | None = None):
    """Daily equity from ``start`` to ``end`` at a constant growth rate."""
    dates, equity = [], []
    day = datetime.date.fromisoformat(start)
    last = datetime.date.fromisoformat(end)
    value = 100_000.0
    while day <= last:
        step = growth
        if shock and _in(day, shock[0], shock[1]):
            step = shock[2]
        value *= 1 + step
        dates.append(day.isoformat())
        equity.append(round(value, 2))
        day += datetime.timedelta(days=1)
    return dates, equity


class TestTheBands:
    def test_they_are_the_prds_five_named_periods(self):
        assert [b[2] for b in REGIME_BANDS] == [
            "COVID crash",
            "Recovery bull",
            "Rate-hike correction",
            "Volatile recovery",
            "Low-volatility grind",
        ]

    def test_they_are_contiguous_and_ordered(self):
        """A gap would silently drop bars from every table."""
        for (_, end, _), (start, _, _) in zip(REGIME_BANDS, REGIME_BANDS[1:]):
            assert end < start, f"{end} then {start} leaves a hole"
        assert REGIME_BANDS[0][0] == "2020-01-01"

    def test_dates_outside_every_band_are_kept_not_dropped(self):
        """A 2015-2019 run is mostly not in any band. Silently omitting those
        bars would make the table look complete when it describes nothing."""
        out = regime_breakdown(
            ["2015-01-0%d" % i for i in range(1, 9)], [100.0, 101, 99, 102, 103, 104, 105, 106]
        )
        assert out["named_coverage_pct"] == 0.0
        assert len(out["periods"]) == 1
        assert out["periods"][0]["label"] == UNKNOWN_BAND_LABEL
        assert out["periods"][0]["named"] is False
        assert out["total_bars"] == 8

    def test_a_run_inside_one_band_reports_only_that_band(self):
        dates, equity = series("2022-01-01", "2022-06-30", 0.001)
        out = regime_breakdown(dates, equity)
        assert [p["label"] for p in out["periods"]] == ["Rate-hike correction"]
        assert out["named_coverage_pct"] == 100.0

    def test_coverage_is_a_percentage_of_bars_actually_present(self):
        dates, equity = series("2021-01-01", "2021-12-31", 0.0005)
        out = regime_breakdown(dates, equity)
        assert out["named_coverage_pct"] == 100.0
        assert out["total_bars"] == len(dates)


class TestTheMetrics:
    def test_a_crash_shows_up_as_a_crash(self):
        dates, equity = series(
            "2020-01-01", "2020-03-31", 0.0, shock=("2020-02-01", "2020-04-01", -0.05)
        )
        row = regime_breakdown(dates, equity)["periods"][0]
        assert row["label"] == "COVID crash"
        assert row["return_pct"] < -30
        assert row["max_drawdown_pct"] > 25
        assert row["sharpe"] < 0

    def test_return_is_measured_from_the_first_bar_of_the_period(self):
        """Not from the run's opening balance — a period that starts mid-run has
        no relationship to where the account was in January."""
        dates, equity = series("2022-01-01", "2022-03-01", 0.001)
        row = regime_breakdown(dates, equity)["periods"][0]
        expected = ((equity[-1] / equity[0]) - 1.0) * 100.0
        assert row["return_pct"] == pytest.approx(expected, abs=0.01)
        assert 5.0 < row["return_pct"] < 8.0, "0.1%/day over ~60 days"

    def test_drawdown_is_measured_from_the_periods_own_peak(self):
        """The first bar is a peak candidate: a period that never exceeds its
        opening value has a drawdown from that opening, not from a peak that
        never existed."""
        dates = ["2022-01-01", "2022-01-02", "2022-01-03"]
        row = regime_breakdown(dates, [100.0, 80.0, 90.0])["periods"][0]
        assert row["max_drawdown_pct"] == pytest.approx(20.0, abs=0.01)

    def test_a_short_period_is_marked_insufficient(self):
        """Twenty daily bars is three months; below that the Sharpe's standard
        error exceeds the number."""
        dates, equity = series("2022-01-01", "2022-01-10", 0.001)
        row = regime_breakdown(dates, equity)["periods"][0]
        assert row["bars"] < MIN_BARS
        assert row["sufficient"] is False

    def test_a_flat_period_has_no_sharpe_rather_than_infinity(self):
        """A perfectly straight line has zero variance. Dividing by it is a
        crash or an infinity, neither of which is a finding."""
        dates = ["2022-01-01", "2022-01-02", "2022-01-03", "2022-01-04"]
        row = regime_breakdown(dates, [100.0, 100.0, 100.0, 100.0])["periods"][0]
        assert row["sharpe"] is None

    def test_a_missing_trade_map_leaves_trades_unknown(self):
        """None means "we did not count", which is different from zero."""
        dates, equity = series("2022-01-01", "2022-06-30", 0.001)
        assert regime_breakdown(dates, equity)["periods"][0]["trades"] is None

    def test_trades_are_counted_by_the_day_they_closed(self):
        dates, equity = series("2022-01-01", "2022-06-30", 0.001)
        counts = {"2022-02-01": 2, "2022-03-01": 1, "2020-01-02": 4}
        row = regime_breakdown(dates, equity, counts)["periods"][0]
        assert row["trades"] == 3, "the 2020 trade belongs to another period"

    def test_the_first_bar_of_a_period_sets_its_return_base(self):
        """The bar before the period belongs to the PREVIOUS band. Using it as
        the base would report the fall from 500 to 100 as the period's result.
        """
        dates = ["2021-12-31", "2022-01-01", "2022-01-02"]
        out = regime_breakdown(dates, [500.0, 100.0, 110.0])
        by_label = {p["label"]: p for p in out["periods"]}
        assert set(by_label) == {"Recovery bull", "Rate-hike correction"}
        assert by_label["Recovery bull"]["return_pct"] == pytest.approx(0.0, abs=0.01)
        assert by_label["Rate-hike correction"]["return_pct"] == pytest.approx(
            10.0, abs=0.01
        ), "the pre-period bar must not set the base"


class TestDegenerateInput:
    def test_no_bars(self):
        out = regime_breakdown([], [])
        assert out["available"] is False
        assert out["total_bars"] == 0
        assert out["named_coverage_pct"] == 0.0

    def test_a_single_bar(self):
        out = regime_breakdown(["2022-01-04"], [100.0])
        assert out["available"] is True
        assert out["periods"][0]["bars"] == 1
        assert out["periods"][0]["sufficient"] is False
        assert out["periods"][0]["sharpe"] is None

    def test_more_dates_than_values_is_not_an_index_error(self):
        """Zip stops at the shorter; a mismatch should degrade, not explode."""
        out = regime_breakdown(["2022-01-01", "2022-01-02", "2022-01-03"], [100.0, 101.0])
        assert out["total_bars"] == 2

    def test_a_zero_opening_balance_does_not_divide_by_zero(self):
        out = regime_breakdown(["2022-01-01", "2022-01-02"], [0.0, 100.0])
        assert out["periods"][0]["return_pct"] == 0.0
