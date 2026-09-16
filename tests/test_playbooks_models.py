"""U1.1 — Playbook dataclass + registry

Tests per UNIFIED-TRADING-TASKS.md U1.1:
- schema round-trip
- to_runner_config produces payload POST /runner/create accepts
- risk_envelope cap math (spot 25,000 lot 75 qty1 → ≈ ₹37,500 × qty, capped)
- C1 shared-default regression
- version bump on update
"""

from backtest.playbooks.models import Playbook
from backtest.playbooks.registry import PlaybookRegistry, reset_playbook_registry


def test_schema_round_trip():
    pb = Playbook(
        name="NIFTY Test Spread",
        underlying="NIFTY",
        structure_type={"BULLISH": "bull_call_spread", "BEARISH": "bear_put_spread"},
        strike_selection="atm",
        quantity=2,
        exit_config={"signal_flip": True, "stop_loss_pct": 0.5, "take_profit_pct": 1.0, "min_days_to_expiry": 1, "reenter": False},
        max_loss_per_trade=5000,
        description="Test",
        tags=["test", "nifty"],
    )
    d = pb.to_dict()
    pb2 = Playbook.from_dict(d)
    assert pb2.name == pb.name
    assert pb2.underlying == pb.underlying
    assert pb2.quantity == pb.quantity
    assert pb2.tags == pb.tags
    assert pb2.version == pb.version
    assert pb2.exit_config == pb.exit_config


def test_to_runner_config_payload():
    pb = Playbook(name="Test", underlying="NIFTY", quantity=1)
    cfg = pb.to_runner_config(strategy_name="directional_options", allocated_capital=100000, mode="paper", source="synthetic")
    # Payload that POST /runner/create accepts
    assert cfg["strategy"] == "directional_options"
    assert cfg["allocated_capital"] == 100000
    assert cfg["symbol"] == "NIFTY"
    assert cfg["instrument"]["type"] == "option"
    assert "expression" in cfg["instrument"]
    expr = cfg["instrument"]["expression"]
    assert "type" in expr
    assert "strike_selection" in expr
    assert "quantity" in expr
    assert "exit" in expr


def test_risk_envelope_cap_math():
    pb = Playbook(name="Test", underlying="NIFTY", strike_selection="atm", quantity=1)
    env = pb.risk_envelope(spot_price=25000, lot_size=75)
    # 25,000 * 0.02 = 500 per unit, *75 = 37,500
    assert env["max_loss_per_signal"] == 37500
    assert env["estimated"] is True
    assert env["lot_size"] == 75
    assert env["underlying"] == "NIFTY"
    # Capped version
    pb_capped = Playbook(name="Test", underlying="NIFTY", strike_selection="atm", quantity=1, max_loss_per_trade=5000)
    env_capped = pb_capped.risk_envelope(spot_price=25000, lot_size=75)
    assert env_capped["max_loss_per_signal"] == 5000
    assert env_capped["estimated"] is True


def test_c1_shared_default_regression():
    pb1 = Playbook(name="Test 1")
    pb2 = Playbook(name="Test 2")
    pb1.tags.append("shared")
    assert "shared" not in pb2.tags
    assert pb1.tags is not pb2.tags
    pb1.exit_config["new_key"] = "value"
    assert "new_key" not in pb2.exit_config
    assert pb1.exit_config is not pb2.exit_config


def test_version_bump_on_update():
    reg = reset_playbook_registry()
    pb = Playbook(name="Version Test", underlying="NIFTY")
    saved = reg.save(pb)
    assert saved.version == 1
    # Update — should bump
    saved.name = "Version Test Updated"
    saved2 = reg.save(saved)
    assert saved2.version == 2
    assert saved2.created_at == saved.created_at  # created_at preserved
    assert saved2.updated_at != saved.created_at or saved2.updated_at is not None


def test_registry_seeded_defaults():
    reg = reset_playbook_registry()
    all_pbs = reg.list()
    assert len(all_pbs) >= 3
    ids = {pb.playbook_id for pb in all_pbs}
    assert "pb_default_bull_spread" in ids
    assert "pb_default_long" in ids
    assert "pb_default_banknifty_delta" in ids


def test_registry_delete_blocks_default():
    reg = reset_playbook_registry()
    try:
        reg.delete("pb_default_bull_spread")
        assert False, "Should have raised ValueError for built-in"
    except ValueError as e:
        assert "built-in" in str(e).lower() or "Cannot delete" in str(e)


def test_lot_size_not_stored():
    """Architecture: lot_size is resolved from instrument master at call time — never stored in playbook."""
    pb = Playbook(name="Test", underlying="NIFTY")
    d = pb.to_dict()
    assert "lot_size" not in d, "lot_size must never be stored in playbook"
    expr = pb.to_expression()
    assert "lot_size" not in expr, "lot_size must never be stored in expression"
