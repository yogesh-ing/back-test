"""Phase 0 — Layer-1 risk limits rulebook (Plan v2.1 §7.1, Gap #4).

The file is the Risk Gate's rulebook: loaded once, validated, fail-closed.
Enforcement itself is Phase 1; here we pin loading semantics.
"""

from __future__ import annotations

import textwrap

import pytest

from backtest.risk_limits import RiskLimitsError, load_risk_limits

VALID = textwrap.dedent(
    """
    schema_version: 1
    per_trade_max_loss_inr: 5000
    per_runner:
      max_lots_per_trade: 10
      max_order_value_inr: 200000
    entitlement:
      paper: [long_call, long_put, bull_call_spread]
      live: [long_call]
    kill_switch:
      engaged_on_boot: false
    """
)


def _write(tmp_path, body: str):
    path = tmp_path / "risk_limits.yaml"
    path.write_text(body)
    return path


def test_loads_shipped_config():
    limits = load_risk_limits()  # config/risk_limits.yaml in the repo
    assert limits.per_trade_max_loss_inr > 0
    assert "long_call" in limits.permitted_structures("paper")
    assert limits.permitted_structures("no-such-mode") == frozenset()  # permits nothing
    assert len(limits.sha256) == 64


def test_valid_file_round_trip(tmp_path):
    limits = load_risk_limits(_write(tmp_path, VALID))
    assert limits.per_runner_max_lots == 10
    assert limits.permitted_structures("LIVE") == frozenset({"long_call"})
    assert limits.kill_switch_engaged_on_boot is False


def test_missing_file_fails_closed(tmp_path):
    with pytest.raises(RiskLimitsError):
        load_risk_limits(tmp_path / "absent.yaml")


def test_unknown_and_overlapping_keys_refused(tmp_path):
    with pytest.raises(RiskLimitsError, match="unknown"):
        load_risk_limits(_write(tmp_path, VALID + "\nsurprise: 1\n"))
    overlapping = VALID.replace(
        "per_trade_max_loss_inr: 5000",
        "per_trade_max_loss_inr: 5000\ndaily_loss_limit_pct: 0.15",
    )
    with pytest.raises(RiskLimitsError, match="may not set"):
        load_risk_limits(_write(tmp_path, overlapping))


def test_unknown_structure_and_bad_values_refused(tmp_path):
    bad = VALID.replace("[long_call, long_put, bull_call_spread]", "[long_call, moon_landing]")
    with pytest.raises(RiskLimitsError, match="unknown structures"):
        load_risk_limits(_write(tmp_path, bad))
    bad2 = VALID.replace("per_trade_max_loss_inr: 5000", "per_trade_max_loss_inr: -5")
    with pytest.raises(RiskLimitsError):
        load_risk_limits(_write(tmp_path, bad2))
