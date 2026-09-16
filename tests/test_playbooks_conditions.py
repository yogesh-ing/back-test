"""U0.1 — Clear consultant conditions C1–C5

C1 mutable defaults → field(default_factory=...) on Playbook.tags/exit_config.
C4 risk_envelope() returns estimated: true.

These are the two conditions that have automated regression tests per UNIFIED-TRADING-TASKS.md.
C2, C3, C5 are doc/assert verified — see execution_engine.py and web/app.py.
"""

from backtest.options.playbook import Playbook


def test_c1_tags_not_shared():
    """C1 regression: two playbooks don't share a list."""
    pb1 = Playbook(name="Test 1")
    pb2 = Playbook(name="Test 2")
    pb1.tags.append("conservative")
    assert "conservative" not in pb2.tags, "C1: tags list is shared between instances — mutable default bug"
    assert pb1.tags is not pb2.tags


def test_c1_exit_config_not_shared():
    """C1 regression: two playbooks don't share exit_config dict."""
    pb1 = Playbook(name="Test 1")
    pb2 = Playbook(name="Test 2")
    pb1.exit_config["stop_loss_pct"] = 0.99
    # pb2 should still have default, not 0.99
    assert pb2.exit_config.get("stop_loss_pct") != 0.99 or "stop_loss_pct" not in pb2.exit_config or pb2.exit_config["stop_loss_pct"] == 0.5, (
        "C1: exit_config dict is shared between instances — mutable default bug"
    )
    assert pb1.exit_config is not pb2.exit_config


def test_c4_risk_envelope_estimated_flag():
    """C4: risk_envelope() output carries an estimated: true flag."""
    pb = Playbook(name="Test", underlying="NIFTY", strike_selection="atm", quantity=1)
    env = pb.risk_envelope(spot_price=25000, lot_size=75)
    assert "estimated" in env, "C4: risk_envelope() must return estimated flag"
    assert env["estimated"] is True, "C4: estimated flag must be True in V1"
    # Also check math: spot 25,000 lot 75 qty 1 → ≈ ₹37,500 (2% ATM)
    # 25,000 * 0.02 = 500 per unit, *75 = 37,500
    assert env["max_loss_per_signal"] == 37500.0 or env["max_loss_per_signal"] == 37500, f"Unexpected max_loss {env['max_loss_per_signal']}"


def test_c4_risk_envelope_capped():
    """C4 + risk cap: max_loss_per_trade caps the envelope."""
    pb = Playbook(name="Test", underlying="NIFTY", strike_selection="atm", quantity=1, max_loss_per_trade=5000)
    env = pb.risk_envelope(spot_price=25000, lot_size=75)
    assert env["max_loss_per_signal"] == 5000, "Risk cap should cap max_loss_per_signal"
    assert env["estimated"] is True


def test_c1_version_is_int():
    """Final spec: version is int, auto-bump on PUT."""
    pb = Playbook(name="Test")
    assert isinstance(pb.version, int), f"C1/C2: version must be int, got {type(pb.version)}"
    assert pb.version == 1


def test_c1_timestamps_exist():
    """Final spec: created_at and updated_at exist."""
    pb = Playbook(name="Test")
    assert pb.created_at is not None
    assert pb.updated_at is not None
    assert pb.created_at == pb.updated_at  # on creation they match
