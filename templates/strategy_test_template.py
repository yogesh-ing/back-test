"""Strategy test template — the executable half of docs/STRATEGY-GUIDELINES.md.

Copy to ``tests/strategies/test_<your_strategy>.py``, set ``STRATEGY_NAME``
(and the three flags below if they apply), run::

    cd src && python -m pytest ../tests/strategies/test_<your_strategy>.py -q

Every test maps to a numbered rule in the guideline (``[R-x]``). They are
deliberately strategy-agnostic: they check the *contract* and the classic
quant failure modes (lookahead, backtest≠forward, fragile edges), not your
alpha. A strategy that fails any of them should not be forward-tested.

What is simulated
-----------------
* Intraday NSE-like sessions (09:15–15:29, IST wall-clock, tz-naive) with
  overnight gaps, in three regimes: trend, chop, and a crash with a vol spike.
* The forward runner's view of the world: a trailing buffer of
  ``FORWARD_WINDOW`` bars (``MAX_BARS_PER_SYMBOL`` in paper_runner.py), and a
  positional index when the feed's timestamps are unusable.
"""

from __future__ import annotations

import time

import numpy as np
import pandas as pd
import pytest

# ----------------------------------------------------------------------------
# Configure me
# ----------------------------------------------------------------------------

STRATEGY_NAME = "sma_crossover"  # ← your strategy's `name`

#: Option strategies only: True if a NEUTRAL view is *meant* to open a
#: structure (e.g. a short strangle). Otherwise NEUTRAL is refused — the
#: bridge OPENS a trade on any non-None view [R-O2].
ALLOWS_NEUTRAL_VIEW = False

#: Set False for strategies that legitimately trade on every bar/never (rare).
EXPECTS_DECISION_CHANGES = True

#: Per-evaluation budget on a full forward buffer. A pool runner evaluates
#: every symbol every bar: 50 symbols × 20 ms = 1 s per tick [R-P1].
PER_BAR_BUDGET_MS = 20.0

FORWARD_WINDOW = 500  # paper_runner.MAX_BARS_PER_SYMBOL
MIN_WARMUP_BARS = 12  # paper_runner.MIN_WARMUP_BARS
SAMPLE_EVERY = 7  # bars between lookahead/window probes


# ----------------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------------


@pytest.fixture(scope="module")
def cls():
    from backtest.plugins import discover_plugins
    from backtest.strategy.registry import get_strategy

    discover_plugins()
    return get_strategy(STRATEGY_NAME)


@pytest.fixture(scope="module")
def kind(cls):
    from backtest.strategy.registry import signal_kind

    return signal_kind(cls)


def session_candles(
    days: int = 14,
    freq: str = "5min",
    regime: str = "trend",
    seed: int = 11,
    spot: float = 25_000.0,
) -> pd.DataFrame:
    """Intraday bars for ``days`` sessions with overnight gaps."""
    rng = np.random.default_rng(seed)
    frames = []
    start = pd.Timestamp("2026-03-02")  # a Monday
    day = start
    level = spot
    while len(frames) < days:
        if day.weekday() < 5:
            idx = pd.date_range(day + pd.Timedelta("9h15min"), day + pd.Timedelta("15h29min"),
                                freq=freq)
            n = len(idx)
            vol = {"trend": 0.0012, "chop": 0.0010, "crash": 0.0035}[regime]
            drift = {"trend": 0.00025, "chop": 0.0, "crash": -0.0009}[regime]
            if regime == "chop":
                rets = rng.normal(0, vol, n) - 0.3 * np.r_[0, np.diff(rng.normal(0, vol, n))]
            else:
                rets = rng.normal(drift, vol, n)
            gap = rng.normal(0, 0.004)  # overnight gap
            close = level * (1 + gap) * np.exp(np.cumsum(rets))
            open_ = np.r_[level * (1 + gap), close[:-1]]
            wick = np.abs(rng.normal(0, vol / 2, n))
            frames.append(pd.DataFrame({
                "open": open_,
                "high": np.maximum(open_, close) * (1 + wick),
                "low": np.minimum(open_, close) * (1 - wick),
                "close": close,
                "volume": rng.integers(50_000, 500_000, n).astype(float),
            }, index=idx))
            level = float(close[-1])
        day += pd.Timedelta(days=1)
    return pd.concat(frames)


SCENARIOS = {
    "trend": session_candles(regime="trend", seed=1),
    "chop": session_candles(regime="chop", seed=2),
    "crash": session_candles(regime="crash", seed=3),
}


def test_scenarios_are_longer_than_the_forward_buffer():
    """Guard for this file itself: every probe below needs > FORWARD_WINDOW bars."""
    for name, frame in SCENARIOS.items():
        assert len(frame) >= 2 * FORWARD_WINDOW, f"{name}: only {len(frame)} bars"


def decision(cls, kind, frame, **params):
    """The one value the runner acts on for the LAST bar of ``frame``."""
    strat = cls(**params)
    if kind == "option":
        view = strat.generate_market_view(frame)
        if view is None:
            return None
        return (view.direction.value, round(float(view.confidence), 6))
    return int(strat.generate_signals(frame).iloc[-1])


# ----------------------------------------------------------------------------
# [R-C] Contract
# ----------------------------------------------------------------------------


def test_conformance_battery(cls):
    """[R-C1] The loader's own battery: identity, metadata, labels, shape, determinism."""
    from backtest.plugins import conformance_errors

    assert conformance_errors(cls).errors == []


def test_params_are_bounded_and_documented(cls):
    """[R-M2] Every numeric param has min/max and a tooltip a trader can act on."""
    for key, spec in cls.param_schema().items():
        if spec["type"] in ("int", "float"):
            assert spec["min"] is not None and spec["max"] is not None, f"{key}: unbounded"
        assert spec["tooltip"], f"{key}: no tooltip"


@pytest.mark.parametrize("edge", ["min", "max"])
def test_runs_at_param_extremes(cls, kind, edge):
    """[R-M2] The spawn form allows min..max — every value must run."""
    params = {}
    for key, spec in cls.param_schema().items():
        if spec["type"] in ("int", "float") and spec[edge] is not None:
            params[key] = spec[edge]
    frame = SCENARIOS["trend"]
    try:
        cls(**params)
    except ValueError:
        pytest.skip("param combination rejected by the strategy's own validation")
    a = decision(cls, kind, frame.iloc[-FORWARD_WINDOW:], **params)
    b = decision(cls, kind, frame.iloc[-FORWARD_WINDOW:], **params)
    assert a == b


# ----------------------------------------------------------------------------
# [R-D] Data, lookahead, backtest ≡ forward
# ----------------------------------------------------------------------------


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_no_lookahead_by_truncation(cls, kind, scenario):
    """[R-D1] The decision at bar t must not change when bars after t are removed."""
    frame = SCENARIOS[scenario]
    if kind == "option":
        pytest.skip("option views are last-bar only; covered by the window test")
    full = cls().generate_signals(frame)
    bad = [
        t for t in range(MIN_WARMUP_BARS, len(frame), SAMPLE_EVERY)
        if decision(cls, kind, frame.iloc[: t + 1]) != int(full.iloc[t])
    ]
    assert not bad, f"lookahead: {len(bad)} bars change when the future is removed, e.g. {bad[:5]}"


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_backtest_equals_forward_window(cls, kind, scenario):
    """[R-D2] Full history (backtest) and the 500-bar buffer (forward) agree.

    Fails for positional logic (``len(candles)``, ``iloc[0]``), frame-anchored
    cumulative measures (VWAP/cumsum from the first row) and indicators whose
    lookback is too long to converge inside the buffer.
    """
    frame = SCENARIOS[scenario]
    bad = []
    for t in range(FORWARD_WINDOW, len(frame), SAMPLE_EVERY):
        full = decision(cls, kind, frame.iloc[: t + 1])
        window = decision(cls, kind, frame.iloc[t + 1 - FORWARD_WINDOW: t + 1])
        if full != window:
            bad.append(t)
    probes = len(range(FORWARD_WINDOW, len(frame), SAMPLE_EVERY))
    assert not bad, f"backtest≠forward on {len(bad)}/{probes} probes (e.g. bar {bad[:3]})"


def test_instance_is_not_mutated_by_evaluation(cls, kind):
    """[R-D4] One instance lives for the runner's lifetime: it must stay a pure function."""
    a, b = SCENARIOS["trend"].iloc[-300:], SCENARIOS["crash"].iloc[-300:]
    strat = cls()

    def run(frame):
        if kind == "option":
            view = strat.generate_market_view(frame)
            return None if view is None else (view.direction, round(view.confidence, 6))
        return strat.generate_signals(frame).tolist()

    first = run(a)
    run(b)
    assert run(a) == first


def test_short_history_is_safe(cls, kind):
    """[R-D3] 1..warmup bars (runner start, option runners skip warmup): flat, no crash."""
    frame = SCENARIOS["trend"]
    for n in range(1, MIN_WARMUP_BARS + 2):
        out = decision(cls, kind, frame.iloc[:n])
        assert out is None or out in (-1, 0, 1) or isinstance(out, tuple)


def test_positional_index_fallback(cls, kind):
    """[R-T2] Feeds without parseable timestamps give a RangeIndex frame."""
    frame = SCENARIOS["trend"].iloc[-FORWARD_WINDOW:].reset_index(drop=True)
    decision(cls, kind, frame)  # must not raise


def test_degenerate_bars(cls, kind):
    """[R-D5] Flat prices (σ = 0), zero volume, one NaN volume: no crash, no NaN out."""
    frame = SCENARIOS["chop"].iloc[-FORWARD_WINDOW:].copy()
    frame.loc[frame.index[-60:], ["open", "high", "low", "close"]] = float(frame["close"].iloc[-61])
    frame.loc[frame.index[-30:], "volume"] = 0.0
    frame.loc[frame.index[-5], "volume"] = np.nan
    if kind == "option":
        decision(cls, kind, frame)
    else:
        out = cls().generate_signals(frame)
        assert not out.isna().any(), "NaN in signals on degenerate bars"


# ----------------------------------------------------------------------------
# [R-S] / [R-O] Output semantics
# ----------------------------------------------------------------------------


def test_output_semantics(cls, kind):
    """[R-S1] equity: {-1,0,1}, aligned, int. [R-O1..O4] option: valid, honest view."""
    frame = SCENARIOS["trend"].iloc[-FORWARD_WINDOW:]
    if kind != "option":
        out = cls().generate_signals(frame)
        assert out.index.equals(frame.index)
        assert set(out.unique()) <= {-1, 0, 1}
        return
    eligible = [s.upper() for s in (getattr(cls, "eligible_instruments", None) or [])]
    for scenario in SCENARIOS.values():
        for t in range(FORWARD_WINDOW, len(scenario), SAMPLE_EVERY * 3):
            window = scenario.iloc[t + 1 - FORWARD_WINDOW: t + 1]
            view = cls().generate_market_view(window)
            if view is None:
                continue
            if not ALLOWS_NEUTRAL_VIEW:
                assert view.direction.value != "neutral", (
                    "NEUTRAL view returned — the bridge OPENS a trade on it; return None"
                )
            assert 0.0 <= float(view.confidence) <= 1.0
            assert float(view.spot_price) == pytest.approx(float(window["close"].iloc[-1]))
            assert view.bar_timestamp == window.index[-1]
            if eligible:
                assert str(view.underlying).upper() in eligible


def test_decisions_change_somewhere(cls, kind):
    """[R-S3] A strategy that never changes its mind across trend/chop/crash is broken
    (warmup longer than the buffer, thresholds that never trigger)."""
    if not EXPECTS_DECISION_CHANGES:
        pytest.skip("strategy declared as constant")
    seen = set()
    for frame in SCENARIOS.values():
        for t in range(FORWARD_WINDOW, len(frame), SAMPLE_EVERY):
            seen.add(decision(cls, kind, frame.iloc[t + 1 - FORWARD_WINDOW: t + 1]))
    assert len(seen) > 1, f"always {seen}"


# ----------------------------------------------------------------------------
# [R-P] Performance
# ----------------------------------------------------------------------------


def test_per_bar_budget(cls, kind):
    """[R-P1] One evaluation on a full forward buffer stays inside the budget."""
    frame = SCENARIOS["trend"].iloc[-FORWARD_WINDOW:]
    decision(cls, kind, frame)  # warm caches/imports
    runs = []
    for _ in range(7):
        t0 = time.perf_counter()
        decision(cls, kind, frame)
        runs.append((time.perf_counter() - t0) * 1000)
    median = sorted(runs)[len(runs) // 2]
    assert median < PER_BAR_BUDGET_MS, f"{median:.1f} ms per evaluation"
