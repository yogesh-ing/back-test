"""GAP-1 (2026-09-21): the Master Audit Log tab always showed empty.

Two stacked causes, both covered here:

1. **Backend:** ``get_audit_log(scope="all")`` treated ``"all"`` as a literal
   scope value — no entry ever has scope ``"all"``, so the Audit tab's
   ``?scope=all`` fetch returned ``[]`` forever. Contract says ``all`` is a
   selector meaning "no filter".
2. **Frontend:** the tab rendered only browser-session ``addAudit`` events
   and never called the API (fixed in portfolio.js; ordering guard below).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backtest.forward.risk_supervisor import GlobalRiskConfig
from backtest.forward.paper_runner import RunnerConfig, TARGET_SINGLE


@pytest.fixture
def manager():
    from backtest.forward.portfolio_manager import PortfolioManager

    mgr = PortfolioManager(
        risk_config=GlobalRiskConfig(daily_loss_limit=100_000, max_drawdown_pct=0.50),
        warmup_bars=5,
        auto_start_feed=False,
    )
    yield mgr
    mgr.shutdown()


def _spawn(mgr, name="audit-gap1"):
    rid = mgr.add_runner(
        RunnerConfig(
            name=name,
            strategy_name="rsi_reversion",
            allocated_capital=50_000,
            target_type=TARGET_SINGLE,
            symbols=["RELIANCE"],
            timeframe="1hour",
            mode="paper",
        ),
        start=False,
    )
    return rid


class TestAuditScopeAll:
    def test_scope_all_returns_every_scope(self, manager):
        _spawn(manager, "paper-runner")
        manager._audit_log("FLATTEN_DASHBOARD test", scope="dashboard", detail="d=1")
        entries = manager.get_audit_log(scope="all")
        scopes = {e["scope"] for e in entries}
        assert "paper" in scopes and "dashboard" in scopes

    def test_scope_all_equals_no_filter(self, manager):
        _spawn(manager)
        assert len(manager.get_audit_log(scope="all")) == len(
            manager.get_audit_log()
        )

    def test_scope_filter_still_filters(self, manager):
        _spawn(manager, "paper-runner")
        manager._audit_log("FLATTEN_DASHBOARD test", scope="dashboard", detail="d=1")
        paper = manager.get_audit_log(scope="paper")
        assert paper and all(e["scope"] == "paper" for e in paper)
        dash = manager.get_audit_log(scope="dashboard")
        assert dash and all(e["scope"] == "dashboard" for e in dash)

    def test_control_actions_are_recorded(self, manager):
        rid = _spawn(manager)
        manager.control_runner(rid, "pause")
        manager.control_runner(rid, "resume")
        actions = [e["action"] for e in manager.get_audit_log(scope="all")]
        assert any(a.startswith("PAUSE") for a in actions)
        assert any(a.startswith("RESUME") for a in actions)


class TestFrontendFetchesBackendAudit:
    def test_tab_open_triggers_backend_fetch(self):
        """Source-level guard: opening the log tab must hit the audit API."""
        src = Path("src/backtest/web/static/js/portfolio.js").read_text(encoding="utf-8")
        assert 'fetchBackendAudit()' in src
        assert '/api/portfolio/audit?scope=all&limit=200' in src
        # The merged render must include backend entries, not just live ones.
        assert 'state.backendAudit' in src
        assert 'live.concat(backend)' in src
