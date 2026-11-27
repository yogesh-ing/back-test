"""BUG-27 regression: one source of truth for the global risk breakers.

The /risk page, the API and the RiskSupervisor must all quote the SAME
drawdown / daily-loss numbers, and those numbers must come from
``config/forward_testing.yaml`` — not from divergent hardcoded defaults.
These tests pin that chain: YAML -> loader -> PortfolioManager -> API.
"""

from __future__ import annotations

import pytest


def _expected_from_yaml():
    """Independently recompute the expected config straight from the YAML."""
    from backtest.forward.risk_config_loader import (
        DEFAULT_FORWARD_CONFIG_PATH,
        load_risk_config_from_yaml,
    )

    loaded = load_risk_config_from_yaml()
    assert loaded.limit_source == "forward_testing.yaml"
    return loaded


def test_loader_reads_forward_testing_yaml():
    loaded = _expected_from_yaml()
    # 0.10 drawdown, 0.02 * 100000 = 2000 daily loss (per the shipped YAML).
    assert loaded.paper.max_drawdown_pct == pytest.approx(0.10)
    assert loaded.paper.daily_loss_limit == pytest.approx(2000.0)
    # paper and live are independent instances (no aliasing).
    assert loaded.paper is not loaded.live


def test_manager_defaults_match_yaml():
    from backtest.forward.portfolio_manager import (
        get_portfolio_manager,
        reset_portfolio_manager,
    )

    loaded = _expected_from_yaml()
    reset_portfolio_manager(tick_seconds=1.0, warmup_bars=5, auto_start_feed=False)
    try:
        mgr = get_portfolio_manager()
        assert mgr.limit_source == "forward_testing.yaml"
        for mode in ("paper", "live"):
            cfg = mgr.get_risk_config(mode)
            assert cfg.max_drawdown_pct == pytest.approx(
                loaded.paper.max_drawdown_pct
            ), mode
            assert cfg.daily_loss_limit == pytest.approx(
                loaded.paper.daily_loss_limit
            ), mode
        # The supervisor (what actually enforces) quotes the same number.
        assert mgr.supervisor.config.max_drawdown_pct == pytest.approx(
            loaded.paper.max_drawdown_pct
        )
    finally:
        get_portfolio_manager().shutdown()


def test_explicit_config_is_marked_explicit():
    from backtest.forward.portfolio_manager import (
        get_portfolio_manager,
        reset_portfolio_manager,
    )
    from backtest.forward.risk_supervisor import GlobalRiskConfig

    reset_portfolio_manager(
        risk_config=GlobalRiskConfig(max_drawdown_pct=0.5),
        tick_seconds=1.0,
        warmup_bars=5,
        auto_start_feed=False,
    )
    try:
        mgr = get_portfolio_manager()
        assert mgr.limit_source == "explicit"
        assert mgr.get_risk_config("paper").max_drawdown_pct == pytest.approx(0.5)
    finally:
        get_portfolio_manager().shutdown()


def test_api_reports_same_limit_source():
    from backtest.forward.portfolio_manager import reset_portfolio_manager
    from backtest.web.app import create_app

    loaded = _expected_from_yaml()
    reset_portfolio_manager(tick_seconds=1.0, warmup_bars=5, auto_start_feed=False)
    app = create_app(source="synthetic")
    with app.test_client() as c:
        cfg = c.get("/api/portfolio/risk/config").get_json()
        assert cfg["limit_source"] == "forward_testing.yaml"
        assert cfg["global"]["max_drawdown_pct"] == pytest.approx(
            loaded.paper.max_drawdown_pct
        )
        assert cfg["global"]["daily_loss_limit"] == pytest.approx(
            loaded.paper.daily_loss_limit
        )

        board = c.get("/api/portfolio/risk").get_json()
        assert board["risk"]["limit_source"] == "forward_testing.yaml"
        assert board["risk"]["global_config"]["max_drawdown_pct"] == pytest.approx(
            loaded.paper.max_drawdown_pct
        )

    from backtest.forward.portfolio_manager import get_portfolio_manager

    get_portfolio_manager().shutdown()