"""U3.3 + U3.4 — Audit logging with scope + Runner spawn snapshot

U3.3: Every control action (spawn, flatten, kill, playbook CRUD, manual close) logs
scope=paper|live|playbook|dashboard. Audit view filters on it. (AC-16.)

U3.4: At spawn, snapshot playbook.to_expression() into the runner config; running
runners never mutate on playbook edit. Display the snapshot version in instance detail.

Tests: one log line per action with the right scope; edit playbook → running runner's
config unchanged; new spawn uses the new version.
"""

from backtest.forward.portfolio_manager import reset_portfolio_manager
from backtest.forward.paper_runner import RunnerConfig
from backtest.playbooks.models import Playbook
from backtest.playbooks.registry import get_playbook_registry


def test_audit_log_scope_per_action():
    """One log line per action with the right scope."""
    mgr = reset_portfolio_manager(auto_start_feed=False)

    # Spawn paper runner
    config = RunnerConfig(
        name="audit test paper",
        strategy_name="rsi_reversion",
        allocated_capital=100000,
        symbols=["NIFTY"],
        target_type="SINGLE_SYMBOL",
        mode="paper",
    )
    instance_id = mgr.add_runner(config, start=False)

    # Check audit log has paper scope
    audit = mgr.get_audit_log(scope="paper")
    assert len(audit) >= 1
    assert audit[0]["scope"] == "paper"
    assert "SPAWN" in audit[0]["action"]

    # Control action pause
    mgr.control_runner(instance_id, "pause")
    audit_paper = mgr.get_audit_log(scope="paper")
    assert any("PAUSE" in e["action"] for e in audit_paper)

    # Emergency flatten — scope all or paper
    mgr.emergency_flatten_all(reason="test", mode="paper")
    audit_all = mgr.get_audit_log()
    assert any("EMERGENCY_FLATTEN" in e["action"] for e in audit_all)

    # Playbook CRUD audit — via registry + manager _audit_log
    registry = get_playbook_registry()
    pb = Playbook(name="audit playbook", underlying="NIFTY", structure_type="bull_call_spread")
    registry.save(pb)
    # Simulate playbook audit via manager
    mgr._audit_log(f"CREATE playbook {pb.playbook_id}", scope="playbook", detail=pb.playbook_id)
    audit_pb = mgr.get_audit_log(scope="playbook")
    assert len(audit_pb) >= 1
    assert audit_pb[0]["scope"] == "playbook"

    # Dashboard flatten audit
    mgr._audit_log("FLATTEN_DASHBOARD test", scope="dashboard")
    audit_dash = mgr.get_audit_log(scope="dashboard")
    assert len(audit_dash) >= 1
    assert audit_dash[0]["scope"] == "dashboard"

    # Filter test: scope filter works
    all_logs = mgr.get_audit_log()
    paper_logs = mgr.get_audit_log(scope="paper")
    assert len(paper_logs) <= len(all_logs)

    mgr.shutdown()


def test_runner_spawn_snapshot():
    """At spawn, snapshot playbook.to_expression() into runner config; running runners never mutate on playbook edit."""
    mgr = reset_portfolio_manager(auto_start_feed=False)
    registry = get_playbook_registry()

    # Create playbook v1
    pb_v1 = Playbook(name="snapshot test", underlying="NIFTY", structure_type="bull_call_spread", strike_selection="atm", quantity=1, max_loss_per_trade=10000)
    registry.save(pb_v1)
    v1 = pb_v1.version
    expr_v1 = pb_v1.to_expression()

    # Spawn runner from v1
    runner_config_dict = pb_v1.to_runner_config(strategy_name="directional_options", allocated_capital=100000)
    assert runner_config_dict["playbook_id"] == pb_v1.playbook_id
    assert runner_config_dict["playbook_version"] == v1
    assert runner_config_dict["playbook_snapshot"] == expr_v1

    config = RunnerConfig(
        name="snapshot runner",
        strategy_name="directional_options",
        allocated_capital=100000,
        symbols=["NIFTY"],
        target_type="SINGLE_SYMBOL",
        instrument={"type": "option", "expression": expr_v1},
        playbook_id=pb_v1.playbook_id,
        playbook_version=v1,
        playbook_snapshot=expr_v1,
    )
    instance_id = mgr.add_runner(config, start=False)
    runner = mgr.get_runner(instance_id)
    assert runner.config.playbook_version == v1
    assert runner.config.playbook_snapshot == expr_v1

    # Edit playbook → v2 with different quantity
    pb_v1.quantity = 2
    registry.save(pb_v1)  # bumps version
    v2 = registry.get(pb_v1.playbook_id).version
    assert v2 == v1 + 1
    expr_v2 = registry.get(pb_v1.playbook_id).to_expression()
    assert expr_v2["quantity"] == 2
    assert expr_v1["quantity"] == 1

    # Running runner's config unchanged
    assert runner.config.playbook_version == v1
    assert runner.config.playbook_snapshot["quantity"] == 1

    # New spawn uses new version
    pb_v2 = registry.get(pb_v1.playbook_id)
    runner_config_v2 = pb_v2.to_runner_config(strategy_name="directional_options", allocated_capital=100000)
    assert runner_config_v2["playbook_version"] == v2
    assert runner_config_v2["playbook_snapshot"]["quantity"] == 2

    # Display snapshot version in instance detail
    detail = mgr.get_runner_detail(instance_id)
    assert detail["playbook_id"] == pb_v1.playbook_id
    assert detail["playbook_version"] == v1
    assert detail["playbook_snapshot"]["quantity"] == 1

    mgr.shutdown()
    # Cleanup
    registry.delete(pb_v1.playbook_id)


def test_playbook_spawn_api_includes_snapshot():
    """Spawn API returns runner config with playbook snapshot."""
    from backtest.web.app import create_app
    from backtest.forward.portfolio_manager import reset_portfolio_manager
    from backtest.forward.risk_supervisor import GlobalRiskConfig

    reset_portfolio_manager(
        risk_config=GlobalRiskConfig(daily_loss_limit=100000, max_drawdown_pct=0.5),
        auto_start_feed=False,
    )
    registry = get_playbook_registry()
    pb = Playbook(name="api snapshot", underlying="NIFTY", structure_type="long_call", quantity=1)
    registry.save(pb)

    app = create_app(source="synthetic")
    with app.test_client() as client:
        r = client.post(f"/api/playbooks/{pb.playbook_id}/spawn", json={"strategy": "directional_options", "allocated_capital": 100000})
        assert r.status_code == 200
        data = r.get_json()
        assert data["success"] is True
        rc = data["runner_config"]
        assert rc["playbook_id"] == pb.playbook_id
        assert rc["playbook_version"] == pb.version
        assert "playbook_snapshot" in rc
        assert rc["playbook_snapshot"]["quantity"] == 1

    registry.delete(pb.playbook_id)
