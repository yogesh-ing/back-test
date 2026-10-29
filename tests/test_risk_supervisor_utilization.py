"""RISK_UTILIZATION telemetry must be edge-triggered (2026-10-23).

``RiskSupervisor.evaluate`` runs on every feed tick, so the 50%/80% daily-loss
utilization warnings must fire once per bucket crossing — not once per tick —
while still re-arming after recovery, so "why did the breaker trip" stays
answerable from the log without flooding it.
"""

from __future__ import annotations

import logging

from backtest.forward.risk_supervisor import GlobalRiskConfig, RiskSupervisor


def _utilization_messages(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "RISK_UTILIZATION" in r.getMessage()]


def test_utilization_logs_once_per_threshold_crossing(caplog):
    with caplog.at_level(logging.WARNING, logger="backtest.forward.risk"):
        sup = RiskSupervisor(GlobalRiskConfig(daily_loss_limit=100_000))
        sup.evaluate([], 1_000_000, 1_000_000, -10_000)  # 10%: silent
        sup.evaluate([], 1_000_000, 1_000_000, -55_000)  # 55%: logs 50%
        sup.evaluate([], 1_000_000, 1_000_000, -60_000)  # same bucket: silent
        sup.evaluate([], 1_000_000, 1_000_000, -85_000)  # 85%: logs 80%

    messages = _utilization_messages(caplog)
    assert len(messages) == 2
    assert "50%" in messages[0]
    assert "80%" in messages[1]


def test_utilization_recovery_rearms_the_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="backtest.forward.risk"):
        sup = RiskSupervisor(GlobalRiskConfig(daily_loss_limit=100_000))
        sup.evaluate([], 1_000_000, 1_000_000, -60_000)  # logs 50%
        sup.evaluate([], 1_000_000, 1_000_000, -20_000)  # recovers: bucket 0
        sup.evaluate([], 1_000_000, 1_000_000, -55_000)  # logs 50% again

    assert len(_utilization_messages(caplog)) == 2


def test_disabled_limit_never_logs_utilization(caplog):
    with caplog.at_level(logging.WARNING, logger="backtest.forward.risk"):
        sup = RiskSupervisor(GlobalRiskConfig(daily_loss_limit=0))
        sup.evaluate([], 1_000_000, 1_000_000, -500_000)

    assert _utilization_messages(caplog) == []
