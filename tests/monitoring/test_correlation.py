"""PRD 1.2b — Strategy P&L correlation matrix and its alerts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from backtest.monitoring import CorrelationMonitor, MonitorConfig

from .helpers import book

IST = timezone(timedelta(hours=5, minutes=30))


def _curve(pnl, start=None, tz=timezone.utc, jitter_us=0):
    start = start or datetime(2026, 9, 16, 4, 0, tzinfo=timezone.utc)
    eq, out = 100_000.0, []
    for i, d in enumerate(pnl):
        eq += d
        ts = (start + timedelta(minutes=i, microseconds=jitter_us)).astimezone(tz)
        out.append({"ts": ts.isoformat(), "equity": eq})
    return out


@pytest.fixture
def mon():
    return CorrelationMonitor(MonitorConfig())


@pytest.fixture
def rng():
    return np.random.default_rng(7)


def _pair(report, a, b):
    return next(p for p in report["pairs"] if {p["a"], p["b"]} == {a, b})


def test_identical_pnl_is_critical_and_offset_is_info(mon, rng):
    base = rng.normal(0, 100, 60)
    books = [book("A", curve=_curve(base)), book("B", curve=_curve(base * 2)),
             book("C", curve=_curve(-base))]
    r = mon.calculate(books)
    assert r["status"] == "ok"
    assert _pair(r, "A", "B")["correlation"] == pytest.approx(1.0)
    assert _pair(r, "A", "C")["correlation"] == pytest.approx(-1.0)
    alerts = {a["key"]: a for a in r["alerts"]}
    crit = [a for a in alerts.values() if a["severity"] == "critical"]
    assert len(crit) == 1 and "A" in crit[0]["title"] and "B" in crit[0]["title"]
    offsets = [a for a in alerts.values() if a["key"].startswith("correlation:offset")]
    assert offsets and all(a["severity"] == "info" for a in offsets)


def test_matrix_is_symmetric_with_unit_diagonal(mon, rng):
    books = [book(n, curve=_curve(rng.normal(0, 50, 40))) for n in "ABC"]
    r = mon.calculate(books)
    m = np.array(r["matrix"], dtype=float)
    assert np.allclose(m, m.T) and np.allclose(np.diag(m), 1.0)


def test_warning_band(mon, rng):
    x = rng.normal(0, 1, 400)
    y = 0.78 * x + np.sqrt(1 - 0.78**2) * rng.normal(0, 1, 400)
    r = mon.calculate([book("A", curve=_curve(x)), book("B", curve=_curve(y))],
                      lookback=400)
    corr = _pair(r, "A", "B")["correlation"]
    assert 0.70 <= corr < 0.85
    assert [a["severity"] for a in r["alerts"] if "pair" in a["key"]] == ["warning"]


def test_timezones_and_microsecond_jitter_still_align(mon, rng):
    """Runners stamp wall-clock time: same tick, different tz / µs offsets."""
    base = rng.normal(0, 100, 50)
    books = [book("UTC", curve=_curve(base)),
             book("IST", curve=_curve(base, tz=IST, jitter_us=350))]
    r = mon.calculate(books)
    assert r["observations"] >= 45
    assert _pair(r, "UTC", "IST")["correlation"] == pytest.approx(1.0, abs=1e-6)


def test_insufficient_and_single_strategy(mon, rng):
    short = mon.calculate([book("A", curve=_curve(rng.normal(0, 1, 8))),
                           book("B", curve=_curve(rng.normal(0, 1, 8)))])
    assert short["status"] == "insufficient_data" and short["alerts"] == []
    one = mon.calculate([book("A", curve=_curve(rng.normal(0, 1, 50)))])
    assert one["status"] == "need_two_strategies"


def test_flat_strategy_has_no_correlation(mon, rng):
    r = mon.calculate([book("Live", curve=_curve(rng.normal(0, 1, 50))),
                       book("Flat", curve=_curve(np.zeros(50)))])
    assert _pair(r, "Live", "Flat")["correlation"] is None
    assert r["strategies_with_variance"] == 1


def test_duplicate_names_are_kept_apart(mon, rng):
    a, b = book("Same", curve=_curve(rng.normal(0, 1, 30))), book("Same", curve=_curve(
        rng.normal(0, 1, 30)))
    b.strategy_id = "zzzzzz-other"
    assert len(mon.calculate([a, b])["strategies"]) == 2


def test_calculate_from_equity_frame(mon, rng):
    x = np.cumsum(rng.normal(0, 10, 100)) + 1e5
    frame = pd.DataFrame({"A": x, "B": x * 1.5, "C": 2e5 - x})
    r = mon.calculate_from_equity(frame)
    assert r["grid"] == "tick" and r["observations"] == 99
    assert _pair(r, "A", "B")["correlation"] == pytest.approx(1.0)
    assert _pair(r, "A", "C")["correlation"] == pytest.approx(-1.0)


def test_diversification_metrics(mon, rng):
    same = rng.normal(0, 1, 200)
    together = mon.calculate_from_equity(pd.DataFrame({"A": np.cumsum(same), "B": np.cumsum(same)}))
    assert together["effective_strategies"] == pytest.approx(1.0, abs=0.01)
    apart = mon.calculate_from_equity(pd.DataFrame({
        "A": np.cumsum(rng.normal(0, 1, 2000)), "B": np.cumsum(rng.normal(0, 1, 2000))}),
        lookback=2000)
    assert apart["effective_strategies"] == pytest.approx(2.0, abs=0.15)
