"""UNITS in replay: a ``*_points`` bracket is points of premium, never rupees.

The replay harness itself is currently broken on an unrelated seam (it passes
``chain_generator=``, which :class:`OptionsBridge` no longer accepts), so these
state the resolver's own rule directly instead of waiting on that repair. See
``docs/OPTION-JUNK-ASK-INCIDENT-2026-11-08.md`` and the UNITS section of
``backtest.options.exit_policy``.
"""

from __future__ import annotations

from decimal import Decimal

from backtest.options.exit_policy import ExitConfig
from backtest.replay.exits import OHLCConservativeExitResolver


def test_points_thresholds_are_priced_by_the_structures_units():
    cfg = ExitConfig(stop_loss_points=2000, take_profit_points=1000, min_days_to_expiry=None)
    units = Decimal("150")  # one 75-lot of each of two legs
    assert OHLCConservativeExitResolver._stop_threshold(cfg, Decimal("0"), units) == Decimal(
        "-300000"
    )
    assert OHLCConservativeExitResolver._target_threshold(cfg, Decimal("0"), units) == Decimal(
        "150000"
    )


def test_an_unpriced_points_rule_is_skipped_not_read_as_rupees():
    cfg = ExitConfig(stop_loss_points=2000, take_profit_points=1000, min_days_to_expiry=None)
    assert OHLCConservativeExitResolver._stop_threshold(cfg, Decimal("0")) is None
    assert OHLCConservativeExitResolver._target_threshold(cfg, Decimal("0")) is None


def test_percent_and_points_still_take_the_closer_level():
    cfg = ExitConfig(
        stop_loss_points=13, stop_loss_pct=0.9, take_profit_points=7, min_days_to_expiry=None
    )
    units = Decimal("150")
    basis = Decimal("100000")
    # 13 pts × 150 = ₹1,950 binds long before 90% of a ₹100,000 premium base.
    assert OHLCConservativeExitResolver._stop_threshold(cfg, basis, units) == Decimal("-1950")
    assert OHLCConservativeExitResolver._target_threshold(cfg, basis, units) == Decimal("1050")
