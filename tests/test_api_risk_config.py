"""Tests for the /api/portfolio/risk/config endpoints (validation fix).

The old handler wrote config attributes directly, so a ``max_positions``
typo was silently accepted (and did nothing) and ``allowed_sources`` could
be weakened with no validation. These tests pin the validated-update
behavior: valid payloads apply, invalid payloads get 400 and change nothing.
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal

import pytest


@pytest.fixture
def client():
    from backtest.forward.portfolio_manager import reset_portfolio_manager
    from backtest.web.app import create_app

    reset_portfolio_manager(
        tick_seconds=1.0,
        warmup_bars=15,
        auto_start_feed=False,
    )
    app = create_app(source="synthetic")
    app.config["PORTFOLIO_SSE_INTERVAL"] = 0.05
    with app.test_client() as c:
        yield c
    from backtest.forward.portfolio_manager import get_portfolio_manager

    get_portfolio_manager().shutdown()


def _bucket_snapshot():
    from backtest.simulator.bucket_risk import BUCKET_RISK_LIMITS, BucketRiskLimits

    return {
        mode: {f.name: getattr(lim, f.name) for f in dataclasses.fields(BucketRiskLimits)}
        for mode, lim in BUCKET_RISK_LIMITS.items()
    }


def _restore_buckets(snapshot):
    from backtest.simulator.bucket_risk import BUCKET_RISK_LIMITS

    for mode, fields in snapshot.items():
        for name, value in fields.items():
            setattr(BUCKET_RISK_LIMITS[mode], name, value)


def test_risk_config_view_reports_canonical_limits(client):
    r = client.get("/api/portfolio/risk/config")
    assert r.status_code == 200
    body = r.get_json()
    assert body["success"] is True
    assert set(body["buckets"]) == {"paper", "live"}
    # The canonical field name is max_open_positions — no stray max_positions
    assert "max_positions" not in body["buckets"]["live"]
    assert "max_open_positions" in body["buckets"]["live"]


def test_valid_global_update_applies(client):
    r = client.post(
        "/api/portfolio/risk/config",
        json={"global": {"daily_loss_limit": 25000, "max_drawdown_pct": 0.3}},
    )
    assert r.status_code == 200
    assert r.get_json()["updated"]["global"]

    from backtest.forward.portfolio_manager import get_portfolio_manager

    cfg = get_portfolio_manager().supervisor.config
    assert cfg.daily_loss_limit == 25000
    assert cfg.max_drawdown_pct == 0.3


def test_invalid_global_value_rejected_and_state_untouched(client):
    from backtest.forward.portfolio_manager import get_portfolio_manager

    before = get_portfolio_manager().supervisor.config.daily_loss_limit

    r = client.post(
        "/api/portfolio/risk/config",
        json={"global": {"daily_loss_limit": -5}},
    )
    assert r.status_code == 400
    # Live config untouched
    assert get_portfolio_manager().supervisor.config.daily_loss_limit == before


def test_unknown_global_field_rejected(client):
    r = client.post(
        "/api/portfolio/risk/config",
        json={"global": {"daily_loss_limit_typo": 1000}},
    )
    assert r.status_code == 400


def test_invalid_breach_mode_rejected(client):
    r = client.post(
        "/api/portfolio/risk/config",
        json={"global": {"breach_mode": "NONSENSE"}},
    )
    assert r.status_code == 400


def test_bucket_update_applies_and_typo_is_rejected(client):
    snapshot = _bucket_snapshot()
    try:
        r = client.post(
            "/api/portfolio/risk/config",
            json={"buckets": {"live": {"max_position_pct": 0.05}}},
        )
        assert r.status_code == 200
        from backtest.simulator.bucket_risk import BUCKET_RISK_LIMITS

        assert BUCKET_RISK_LIMITS["live"].max_position_pct == Decimal("0.05")

        # The exact typo the old handler silently accepted (and ignored):
        # an unknown field must now 400 instead of pretending to save.
        r2 = client.post(
            "/api/portfolio/risk/config",
            json={"buckets": {"live": {"max_positions": 3}}},
        )
        assert r2.status_code == 400

        # Unknown bucket 400s
        r3 = client.post(
            "/api/portfolio/risk/config",
            json={"buckets": {"demo": {"max_position_pct": 0.1}}},
        )
        assert r3.status_code == 400
    finally:
        _restore_buckets(snapshot)


def test_bucket_invalid_value_rejected_and_state_untouched(client):
    snapshot = _bucket_snapshot()
    try:
        before = _bucket_snapshot()["live"]["max_gross_exposure_pct"]
        r = client.post(
            "/api/portfolio/risk/config",
            json={"buckets": {"live": {"max_gross_exposure_pct": -1}}},
        )
        assert r.status_code == 400
        assert _bucket_snapshot()["live"]["max_gross_exposure_pct"] == before
    finally:
        _restore_buckets(snapshot)


def test_bucket_unknown_source_value_rejected(client):
    """allowed_sources edits must go through BucketRiskLimits validation —
    a bogus tag can no longer weaken the live bucket's fail-closed gate."""
    snapshot = _bucket_snapshot()
    try:
        r = client.post(
            "/api/portfolio/risk/config",
            json={"buckets": {"live": {"allowed_sources": ["not-a-source"]}}},
        )
        assert r.status_code == 400
        from backtest.simulator.bucket_risk import BUCKET_RISK_LIMITS

        assert BUCKET_RISK_LIMITS["live"].allowed_sources == frozenset({"mstock"})
    finally:
        _restore_buckets(snapshot)
