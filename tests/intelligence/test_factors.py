"""Multi-factor regime engine (``backtest.intelligence.factors``).

Each detector is a pure function; these tests pin the status/tone contract and
the composite roll-up. The service integration test proves ``regime_snapshot``
carries the block without touching the database.
"""

from __future__ import annotations

from datetime import datetime, timezone

from backtest.intelligence import factors as F
from backtest.intelligence.service import PortfolioIntelligence


def _closes(n, start=100.0, step=1.0):
    return [start + i * step for i in range(n)]


# --------------------------------------------------------------------------- #
# Trend
# --------------------------------------------------------------------------- #


def test_trend_up():
    f = F.trend_factor(_closes(200, step=1.0), 200)
    assert f.status == "up" and f.tone == "positive"
    assert f.value is not None and f.value > 0


def test_trend_down():
    f = F.trend_factor(_closes(200, start=300.0, step=-1.0), 200)
    assert f.status == "down" and f.tone == "negative"


def test_trend_unknown_without_history():
    f = F.trend_factor(_closes(10), 200)
    assert f.status == "unknown" and f.tone == "unknown"


# --------------------------------------------------------------------------- #
# Volatility
# --------------------------------------------------------------------------- #


def test_volatility_elevated_calm_inline():
    assert F.volatility_factor(30.0, 20.0).status == "elevated"
    assert F.volatility_factor(10.0, 20.0).status == "calm"
    assert F.volatility_factor(20.0, 20.0).status == "in_line"


def test_volatility_unknown_without_inputs():
    assert F.volatility_factor(None, 20.0).status == "unknown"
    assert F.volatility_factor(20.0, None).status == "unknown"


# --------------------------------------------------------------------------- #
# Breadth
# --------------------------------------------------------------------------- #


def test_breadth_broad_narrow_mixed():
    up = {f"S{i}": _closes(200, start=100.0 + i, step=1.0) for i in range(10)}
    down = {f"S{i}": _closes(200, start=300.0 - i, step=-1.0) for i in range(10)}
    assert F.breadth_factor(up, 200).status == "broad"
    assert F.breadth_factor(down, 200).status == "narrow"
    half = {**up, **{f"D{i}": v for i, v in enumerate(down.values())}}
    assert F.breadth_factor(half, 200).status == "mixed"


def test_breadth_unknown_without_history():
    assert F.breadth_factor({}, 200).status == "unknown"
    assert F.breadth_factor({"X": [1.0, 2.0]}, 200).status == "unknown"


# --------------------------------------------------------------------------- #
# IV / RV
# --------------------------------------------------------------------------- #


def test_iv_rv_states():
    assert F.iv_rv_factor(30.0, 20.0, 5.0).status == "sell_vol"
    assert F.iv_rv_factor(10.0, 20.0, 5.0).status == "buy_vol"
    assert F.iv_rv_factor(22.0, 20.0, 5.0).status == "fair"


def test_iv_rv_unknown_without_inputs():
    f = F.iv_rv_factor(None, 20.0)
    assert f.status == "unknown"
    assert "implied vol" in f.detail
    f = F.iv_rv_factor(20.0, None)
    assert f.status == "unknown"
    assert "realised vol" in f.detail


# --------------------------------------------------------------------------- #
# Session
# --------------------------------------------------------------------------- #


def test_session_maps_ist_window():
    # 04:00 UTC == 09:30 IST → opening drive.
    f = F.session_factor(datetime(2026, 11, 24, 4, 0, tzinfo=timezone.utc))
    assert f.status == "opening_drive"
    assert "IST" in f.detail


def test_session_off_hours():
    f = F.session_factor(datetime(2026, 11, 24, 0, 0, tzinfo=timezone.utc))
    assert f.status == "off_hours"


# --------------------------------------------------------------------------- #
# Gamma
# --------------------------------------------------------------------------- #


def test_gamma_thresholds():
    assert F.gamma_factor(-200.0, -150.0).status == "critical"
    assert F.gamma_factor(-100.0, -150.0).status == "watch"
    assert F.gamma_factor(0.0, -150.0).status == "ok"
    assert F.gamma_factor(None, -150.0).status == "unknown"


# --------------------------------------------------------------------------- #
# VIX
# --------------------------------------------------------------------------- #


def test_vix_maps_regime_to_tone():
    assert F.vix_factor({"regime": "low_vol", "vix": 12.0}).tone == "positive"
    assert F.vix_factor({"regime": "high_vol", "vix": 30.0}).tone == "negative"
    assert F.vix_factor({"regime": "unknown", "vix": None}).tone == "unknown"


# --------------------------------------------------------------------------- #
# Composite + assembly
# --------------------------------------------------------------------------- #


def test_build_factors_order_and_shape():
    block = F.build_factors(
        regime_snap={"regime": "moderate_vol", "vix": 18.0, "realized_vol": 18.0},
        benchmark_closes=_closes(200),
        breadth_closes={f"S{i}": _closes(200, start=100.0 + i) for i in range(10)},
        net_gamma=-100.0,
        atm_iv=18.0,
    )
    keys = [f["key"] for f in block["factors"]]
    assert keys == list(F.FACTOR_ORDER)
    assert set(block["composite"]) >= {"label", "tone", "risk_off", "risk_on"}
    assert block["sma_window"] == 200


def test_composite_risk_off_on_critical_gamma():
    block = F.build_factors(
        regime_snap={"regime": "high_vol", "vix": 30.0, "realized_vol": 30.0},
        benchmark_closes=_closes(200, start=300.0, step=-1.0),
        breadth_closes={f"S{i}": _closes(200, start=300.0 - i, step=-1.0) for i in range(10)},
        net_gamma=-500.0,
    )
    assert block["composite"]["label"] == "risk_off"
    assert block["composite"]["tone"] == "negative"


def test_composite_risk_on_when_all_positive():
    block = F.build_factors(
        regime_snap={"regime": "low_vol", "vix": 12.0, "realized_vol": 10.0},
        benchmark_closes=_closes(200),
        breadth_closes={f"S{i}": _closes(200, start=100.0 + i) for i in range(10)},
        net_gamma=100.0,
    )
    assert block["composite"]["label"] == "risk_on"


# --------------------------------------------------------------------------- #
# Service integration
# --------------------------------------------------------------------------- #


def test_regime_snapshot_includes_factors(monkeypatch):
    intel = PortfolioIntelligence(manager=object())
    monkeypatch.setattr(intel, "snapshot", lambda mode=None: {"roster": {}})
    monkeypatch.setattr(
        intel,
        "_factor_inputs",
        lambda: {
            "benchmark_closes": _closes(200),
            "breadth_closes": {f"S{i}": _closes(200, start=100.0 + i) for i in range(5)},
            "net_gamma": -100.0,
        },
    )
    snap = intel.regime_snapshot()
    assert "factors" in snap
    assert [f["key"] for f in snap["factors"]["factors"]] == list(F.FACTOR_ORDER)
    assert snap["factors"]["composite"]["label"] in {"risk_off", "risk_on", "neutral"}