"""PRD 1.3 — Market regime detector and strategy regime fit."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backtest.monitoring import MarketRegimeDetector, MonitorConfig
from backtest.monitoring.config import StrategyProfile
from backtest.monitoring.regime import legs_side, periods_per_year

from .helpers import book


def _frame(daily_sd, n=60, freq="B", seed=3, vix=None, last_range_mult=1.0):
    rng = np.random.default_rng(seed)
    close = 25000 * np.exp(np.cumsum(rng.normal(0, daily_sd, n)))
    idx = pd.date_range("2026-06-01", periods=n, freq=freq)
    rng_pct = np.full(n, 0.01)
    rng_pct[-1] *= last_range_mult
    df = pd.DataFrame({"close": close, "high": close * (1 + rng_pct / 2),
                       "low": close * (1 - rng_pct / 2)}, index=idx)
    if vix is not None:
        df["vix"] = vix
    return df


@pytest.fixture
def det():
    return MarketRegimeDetector(MonitorConfig())


class TestAnnualisation:
    def test_daily_bars(self):
        assert periods_per_year(pd.date_range("2026-01-01", periods=30, freq="B")) == \
            pytest.approx(252, rel=0.01)

    def test_minute_bars(self):
        idx = pd.date_range("2026-01-01 09:15", periods=100, freq="min")
        assert periods_per_year(idx) == pytest.approx(252 * 375, rel=0.01)


class TestClassification:
    def test_low_vol(self, det):
        r = det.detect(_frame(0.004))  # ≈ 6% annualised
        assert r["regime"] == "low_vol" and r["vol_index_source"] == "realized"

    def test_high_vol(self, det):
        r = det.detect(_frame(0.025))  # ≈ 40% annualised
        assert r["regime"] == "high_vol"

    def test_vix_column_has_priority(self, det):
        r = det.detect(_frame(0.004, vix=30.0))
        assert r["vol_index_source"] == "vix" and r["vol_index"] == 30.0
        assert r["regime"] == "high_vol"

    def test_too_few_bars_is_unknown(self, det):
        assert det.detect(_frame(0.01, n=2))["regime"] == "unknown"


class TestTransitions:
    def test_vix_spike_flags_transition(self, det):
        vix = np.full(60, 14.0)
        vix[-1] = 19.0  # +36% vs 5 bars ago
        r = det.detect(_frame(0.004, vix=vix))
        assert r["transitioning"] and r["change_basis"] == "vix"
        alert = next(a for a in det.check(r, []) if a.key.startswith("regime:transition"))
        assert alert.severity == "warning" and "spiking" in alert.title

    def test_option_iv_never_compared_against_realized(self, det):
        """Regression: IV 12 vs realized 40 must not read as a −70% 'collapse'."""
        r = det.detect(_frame(0.025), implied_vol=0.12)
        assert r["vol_index_source"] == "option_iv" and r["vol_index"] == 12.0
        assert r["change_basis"] in (None, "realized")
        if r["vol_index_change_pct"] is not None:
            assert abs(r["vol_index_change_pct"]) < 60

    def test_range_expansion(self, det):
        r = det.detect(_frame(0.004, last_range_mult=2.0))
        assert r["range_expanding"]
        assert any(a.key.startswith("regime:range") for a in det.check(r, []))


class TestStrategyFit:
    def _regime(self, regime, vol):
        return {"regime": regime, "vol_index": vol, "label": regime, "symbol": "NIFTY",
                "vol_index_source": "vix"}

    def test_configured_profile_wins(self):
        cfg = MonitorConfig()
        cfg.strategy_profiles["banknifty_straddle"] = StrategyProfile(
            "low_vol", [10, 16], ["short_premium"])
        det = MarketRegimeDetector(cfg)
        fit = det.strategy_fit(book("BankNifty_Straddle"), self._regime("high_vol", 28))
        assert fit["fit"] == "mismatch"
        assert "premium seller" in fit["recommendation"]
        # A premium SELLER outside its range in a HIGH-vol regime is critical…
        alert = det.check(self._regime("high_vol", 28), [fit])[0]
        assert alert.severity == "critical" and "outside its regime" in alert.title
        # …while other mismatches are warnings.
        low = det.strategy_fit(book("BankNifty_Straddle"), self._regime("low_vol", 8))
        assert low["fit"] == "mismatch"
        assert det.check(self._regime("low_vol", 8), [low])[0].severity == "warning"

    def test_optimal_and_acceptable(self, det):
        trend = book("x", kind="donchian_breakout")
        assert det.strategy_fit(trend, self._regime("high_vol", 30))["fit"] == "optimal"
        # In range but a different regime label → acceptable.
        assert det.strategy_fit(trend, self._regime("moderate_vol", 20))["fit"] == "acceptable"

    def test_repo_strangle_is_short_premium(self, det):
        """options_bridge maps 'strangle' to ShortStrangle (SELL + SELL)."""
        p = det.profile_for(book("s", structure_type="strangle"))
        assert "short_premium" in p.tags

    def test_open_legs_outrank_the_name(self, det):
        p = det.profile_for(book("s", structure_type="strangle"), side="long")
        assert "long_premium" in p.tags and p.source == "inferred:legs"
        assert legs_side(["SHORT", "SHORT"]) == "short"
        assert legs_side(["LONG", "SHORT"]) is None  # spreads keep the name profile
        assert legs_side([]) is None

    def test_unprofiled_asks_for_config(self, det):
        fit = det.strategy_fit(book("mystery", kind="buy_and_hold"), self._regime("low_vol", 12))
        assert fit["fit"] == "unprofiled" and "strategy_profiles" in fit["recommendation"]
