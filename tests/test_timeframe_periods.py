"""Timeframe annualisation (PRD backTest-enhance §1.4, engine half).

``periods_per_year`` converts a per-bar mean and a per-bar standard deviation
into an annual Sharpe and an annual CAGR. Scoring 1-minute bars with the daily
factor of 252 reports a Sharpe that is wrong by roughly sqrt(375) — silently,
because nothing about the number looks broken. The PRD requires this to ship
with timeframe support, so it is pinned here against the table it specifies.
"""

from __future__ import annotations

import pytest

from backtest.data.base import (
    BARS_PER_TRADING_DAY,
    CANONICAL_TIMEFRAMES,
    TRADING_DAYS_PER_YEAR,
    TRADING_MINUTES_PER_DAY,
    normalize_timeframe,
    periods_per_year,
)

# ---------------------------------------------------------------------------
# The PRD's table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "timeframe,expected",
    [
        ("1min", 252 * 375),
        ("5min", 252 * 75),
        ("15min", 252 * 25),
        ("1hour", 252 * 6),
        ("1day", 252),
    ],
)
def test_prd_annualisation_table(timeframe, expected):
    assert periods_per_year(timeframe) == expected


def test_intraday_broker_spellings_the_prd_lists():
    """10-minute and 30-minute are in the PRD's table; brokers ask for them."""
    assert periods_per_year("10min") == 252 * 37
    assert periods_per_year("30min") == 252 * 12


def test_weekly_annualises_by_52_weeks_not_by_a_fifth_of_252():
    """A week is not 252/5 days; scoring it as 50.4 invents 4 extra periods."""
    assert periods_per_year("1week") == 52
    assert periods_per_year("1week") != round(TRADING_DAYS_PER_YEAR / 5)


def test_session_constants():
    assert TRADING_MINUTES_PER_DAY == 375
    assert TRADING_DAYS_PER_YEAR == 252
    assert BARS_PER_TRADING_DAY["1min"] == 375
    assert BARS_PER_TRADING_DAY["5min"] == 75
    assert BARS_PER_TRADING_DAY["15min"] == 25
    assert BARS_PER_TRADING_DAY["1hour"] == 6


# ---------------------------------------------------------------------------
# Spelling tolerance
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "spelling,canonical",
    [
        ("1day", "1day"),
        ("1D", "1day"),
        ("1d", "1day"),
        ("day", "1day"),
        ("DAILY", "1day"),
        ("1week", "1week"),
        ("1W", "1week"),
        ("week", "1week"),
        ("1hour", "1hour"),
        ("1H", "1hour"),
        ("60min", "1hour"),
        ("hour", "1hour"),
        ("1min", "1min"),
        ("1m", "1min"),
        ("minute", "1min"),
        ("4hour", "4hour"),
        ("4H", "4hour"),
        ("240min", "4hour"),
    ],
)
def test_every_accepted_spelling_normalises(spelling, canonical):
    assert normalize_timeframe(spelling) == canonical
    assert periods_per_year(spelling) == periods_per_year(canonical)


def test_unknown_timeframe_falls_back_to_daily_instead_of_raising():
    """A caller that never said must not crash a run; daily is the safe default."""
    assert normalize_timeframe("banana") is None
    assert normalize_timeframe("") is None
    assert normalize_timeframe(None) is None
    assert periods_per_year("banana") == TRADING_DAYS_PER_YEAR
    assert periods_per_year(None) == TRADING_DAYS_PER_YEAR


def test_every_canonical_timeframe_has_a_period_count():
    for tf in CANONICAL_TIMEFRAMES:
        assert periods_per_year(tf) >= 1, tf


def test_coarser_bars_annualise_strictly_less_often():
    ordered = ["1min", "5min", "15min", "1hour", "4hour", "1day"]
    values = [periods_per_year(tf) for tf in ordered]
    assert values == sorted(values, reverse=True)
    assert all(a > b for a, b in zip(values, values[1:]))


# ---------------------------------------------------------------------------
# It reaches the engine
# ---------------------------------------------------------------------------


def test_backtest_config_annualises_from_the_timeframe():
    """`run_backtest(timeframe=...)` must reach BacktestConfig.periods_per_year."""
    from backtest.data.synthetic import SyntheticSource
    from backtest.engine.backtest_runner import run_backtest

    candles = SyntheticSource().get_candles("DEMO", "2022-01-01", "2023-01-01", "day")
    daily = run_backtest(candles, "sma_crossover", {"fast": 5, "slow": 20}, "DEMO", 100_000)
    intraday = run_backtest(
        candles, "sma_crossover", {"fast": 5, "slow": 20}, "DEMO", 100_000, timeframe="1min"
    )

    assert daily.config.periods_per_year == 252
    assert intraday.config.periods_per_year == 252 * 375
    assert intraday.metrics["timeframe"] == "1min"
    # Undeclared stays undeclared — the stamp reports what the caller said, and
    # the API layer is what knows the symbol's real granularity.
    assert daily.metrics["timeframe"] is None


def test_the_same_bars_score_differently_per_timeframe():
    """The point of §1.4: one equity curve, two honest annualisations."""
    from backtest.data.synthetic import SyntheticSource
    from backtest.engine.backtest_runner import run_backtest

    candles = SyntheticSource().get_candles("DEMO", "2022-01-01", "2023-01-01", "day")
    daily = run_backtest(candles, "sma_crossover", {"fast": 5, "slow": 20}, "DEMO", 100_000)
    hourly = run_backtest(
        candles, "sma_crossover", {"fast": 5, "slow": 20}, "DEMO", 100_000, timeframe="1hour"
    )
    # Sharpe is per-bar mean / per-bar sd x sqrt(periods) — same bars, different
    # annualisation, so the numbers must differ.
    assert daily.metrics["sharpe"] != hourly.metrics["sharpe"]
    assert daily.metrics["total_return"] == pytest.approx(hourly.metrics["total_return"])
