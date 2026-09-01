"""Ticket #9 — per-bucket risk limits (unit tests).

The bucket risk map is the single source of truth keyed on ``_classify``'s
output: mode (paper|live) keys the exposure/size caps, source gates what can
be traded. Config ``risk.buckets`` only OVERRIDES a bucket's limits.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from backtest.data.source_tags import SOURCE_TAG_VALUES
from backtest.simulator.bucket_risk import (
    BUCKET_RISK_LIMITS,
    BUCKET_RISK_FIELDS,
    BucketRiskLimits,
    LIVE_BUCKET,
    PAPER_BUCKET,
    resolve_bucket_risk,
)
from backtest.simulator.errors import ValidationError


# ---------------------------------------------------------------------------
# Canonical defaults
# ---------------------------------------------------------------------------


def test_bucket_defaults_paper_permissive_and_real_bucket_tight():
    paper_key, paper = resolve_bucket_risk("paper", "synthetic")
    run_key, run = resolve_bucket_risk("live", "mstock")

    assert paper_key == PAPER_BUCKET
    assert run_key == LIVE_BUCKET

    # paper = free play: no exposure caps at all.
    assert paper.max_position_value is None
    assert paper.max_position_pct is None
    assert paper.max_gross_exposure_pct is None
    assert paper.max_open_positions is None
    assert paper.min_trade_value is None
    assert paper.max_leverage == Decimal("1")

    # real fills = real capital: explicit, tight caps.
    assert run.max_position_value == Decimal("10000.0000")
    assert run.max_position_pct == Decimal("0.10")
    assert run.max_gross_exposure_pct == Decimal("0.50")
    assert run.max_open_positions == 5
    assert run.min_trade_value == Decimal("1000.0000")
    assert run.max_leverage == Decimal("1")

    # The boundary: identical inputs would size differently.
    assert (paper.max_position_value, paper.max_position_pct) != (
        run.max_position_value,
        run.max_position_pct,
    )


def test_source_gate_refuses_fake_data_for_real_bucket():
    """live/synthetic and live/replay are the fake-data-trading-real-money
    hazard — refused; paper accepts every source (data trust doesn't change
    paper risk)."""
    for source in ("synthetic", "replay"):
        with pytest.raises(ValidationError, match="refuses source"):
            resolve_bucket_risk("live", source)

    for source in SOURCE_TAG_VALUES:
        _, limits = resolve_bucket_risk("paper", source)
        assert source in limits.allowed_sources

    # Case/whitespace tolerance mirrors _classify normalization.
    _, limits = resolve_bucket_risk("LIVE", "  MSTOCK ")
    assert limits.max_position_pct == Decimal("0.10")


def test_unknown_mode_bucket_or_field_raises():
    with pytest.raises(ValidationError, match="unknown risk bucket"):
        resolve_bucket_risk("scalping", "synthetic")
    with pytest.raises(ValidationError, match="unknown risk bucket override"):
        resolve_bucket_risk("live", "mstock", overrides={"scalping": {}})
    with pytest.raises(ValidationError, match="unknown risk limit override"):
        resolve_bucket_risk("live", "mstock", overrides={"live": {"not_a_limit": 1}})


# ---------------------------------------------------------------------------
# Explicit config overrides
# ---------------------------------------------------------------------------


def test_explicit_bucket_override_wins_and_merges():
    _, limits = resolve_bucket_risk(
        "live", "mstock", overrides={"live": {"max_position_pct": Decimal("0.05")}},
    )
    assert limits.max_position_pct == Decimal("0.05")
    # Non-overridden fields keep the canonical defaults.
    assert limits.max_position_value == Decimal("10000.0000")
    assert limits.max_open_positions == 5

    # Explicit None disables a bucket cap (the sanctioned way to opt out).
    _, limits = resolve_bucket_risk(
        "live", "mstock", overrides={"live": {"max_position_value": None}},
    )
    assert limits.max_position_value is None
    assert limits.max_position_pct == Decimal("0.10")

    # A paper bucket can be tightened explicitly too.
    _, limits = resolve_bucket_risk(
        "paper", "synthetic",
        overrides={"paper": {"max_position_pct": Decimal("0.10")}},
    )
    assert limits.max_position_pct == Decimal("0.10")


def test_bucket_limit_validation():
    with pytest.raises(ValidationError):
        BucketRiskLimits(max_position_pct=Decimal("0"))
    with pytest.raises(ValidationError):
        BucketRiskLimits(max_open_positions=0)
    with pytest.raises(ValidationError):
        BucketRiskLimits(max_leverage=Decimal("0.5"))
    with pytest.raises(ValidationError):
        BucketRiskLimits(allowed_sources={"not-a-source"})
    # to_dict round-trips.
    limits = BUCKET_RISK_LIMITS[LIVE_BUCKET]
    assert set(limits.to_dict()) == set(BUCKET_RISK_FIELDS)


# ---------------------------------------------------------------------------
# Canonical adapters
# ---------------------------------------------------------------------------


def test_adapters_map_bucket_to_sizing_portfolio_and_risk_config():
    _, limits = resolve_bucket_risk("live", "mstock")

    sizing = limits.to_sizing_constraints()
    assert sizing.max_position_value == Decimal("10000.0000")
    assert sizing.max_position_pct == Decimal("0.10")
    assert sizing.max_gross_exposure_pct == Decimal("0.50")
    assert sizing.max_open_positions == 5
    assert sizing.min_trade_value == Decimal("1000.0000")

    portfolio_limits = limits.to_portfolio_limits(allow_short=True)
    assert portfolio_limits.allow_short is True
    assert portfolio_limits.max_position_value == Decimal("10000.0000")
    assert portfolio_limits.max_position_pct == Decimal("0.10")
    assert portfolio_limits.max_open_positions == 5
    assert portfolio_limits.min_trade_value == Decimal("1000.0000")

    risk_cfg = limits.to_risk_config(
        max_drawdown_pct=Decimal("0.05"), daily_loss_limit_pct=Decimal("0.01"),
    )
    assert risk_cfg.max_position_value == Decimal("10000.0000")
    assert risk_cfg.max_position_pct == Decimal("0.10")
    assert risk_cfg.max_open_positions == 5
    assert risk_cfg.max_gross_exposure_pct == Decimal("0.50")
    assert risk_cfg.min_order_value == Decimal("1000.0000")
    assert risk_cfg.max_drawdown_pct == Decimal("0.05")
    assert risk_cfg.daily_loss_limit_pct == Decimal("0.01")


# ---------------------------------------------------------------------------
# Open-book exposure check (risk teeth behind the classification guard)
# ---------------------------------------------------------------------------


class _StubPosition:
    def __init__(self, symbol, quantity, average_entry_price, current_price=None):
        self.symbol = symbol
        self.quantity = Decimal(str(quantity))
        self.average_entry_price = Decimal(str(average_entry_price))
        self.current_price = (
            Decimal(str(current_price)) if current_price is not None else None
        )


class _StubBook:
    def __init__(self, positions, equity=Decimal("100000"), gross=None):
        self.positions = positions
        self._equity = Decimal(str(equity))
        self._gross = Decimal(str(gross)) if gross is not None else None

    def calculate_total_equity(self):
        return self._equity

    def calculate_gross_exposure(self):
        if self._gross is not None:
            return self._gross
        return sum(
            abs(p.quantity) * (p.current_price or p.average_entry_price)
            for p in self.positions.values()
        )


def test_exposure_check_violates_real_bucket_but_not_paper():
    _, real = resolve_bucket_risk("live", "mstock")
    _, paper = resolve_bucket_risk("paper", "synthetic")

    big = _StubBook({"TEST": _StubPosition("TEST", 500, 100)})  # 50,000 notional
    violation = real.check_exposure(big)
    assert violation is not None
    assert "TEST notional 50000" in violation
    assert "bucket max_position_value 10000" in violation
    assert "50.00% of equity > bucket max_position_pct" in violation
    assert paper.check_exposure(big) is None  # free play: no cap violated

    small = _StubBook({"TEST": _StubPosition("TEST", 50, 100)})  # 5,000 notional
    assert real.check_exposure(small) is None

    # Gross-exposure cap: two 7,500 positions = 15% of equity (under 50%),
    # so only when gross crosses does the cap fire.
    crowded = _StubBook(
        {"A": _StubPosition("A", 500, 100), "B": _StubPosition("B", 500, 100)},
        equity=Decimal("100000"),
        gross=Decimal("100000"),
    )
    assert real.check_exposure(crowded) is not None
    assert "gross exposure 100.00% > bucket max_gross_exposure_pct" in (
        real.check_exposure(crowded) or ""
    )

    # max_open_positions check.
    def too_many():
        return _StubBook(
            {f"S{i}": _StubPosition(f"S{i}", 10, 100) for i in range(6)}
        )

    assert "6 open positions > bucket max 5" in (real.check_exposure(too_many()) or "")
