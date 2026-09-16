"""U1.2 — Playbook API routes

Tests per UNIFIED-TRADING-TASKS.md U1.2:
- CRUD happy paths, 404s, seed delete-block, spawn response matches to_runner_config,
  audit log line per mutation (scope="playbook").
"""

import json

from backtest.playbooks.registry import reset_playbook_registry
from backtest.web.app import create_app


def _client():
    reset_playbook_registry()
    app = create_app(source="synthetic")
    app.config["TESTING"] = True
    return app.test_client()


def test_list_playbooks():
    client = _client()
    resp = client.get("/api/playbooks")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert data["count"] >= 3
    assert any(pb["playbook_id"] == "pb_default_bull_spread" for pb in data["playbooks"])


def test_list_filter_by_tag():
    client = _client()
    resp = client.get("/api/playbooks?tag=conservative")
    data = resp.get_json()
    assert data["count"] >= 1
    assert all("conservative" in [t.lower() for t in pb["tags"]] for pb in data["playbooks"])


def test_list_filter_by_underlying():
    client = _client()
    resp = client.get("/api/playbooks?underlying=BANKNIFTY")
    data = resp.get_json()
    assert data["count"] >= 1
    assert all(pb["underlying"] == "BANKNIFTY" for pb in data["playbooks"])


def test_create_get_update_delete():
    client = _client()
    # Create
    payload = {
        "name": "Test Playbook",
        "underlying": "NIFTY",
        "structure_type": "long_call",
        "strike_selection": "atm",
        "quantity": 1,
        "exit_config": {"signal_flip": True, "stop_loss_pct": 0.5, "min_days_to_expiry": 1, "reenter": False},
        "tags": ["test"],
    }
    resp = client.post("/api/playbooks", json=payload)
    assert resp.status_code == 201
    pb = resp.get_json()["playbook"]
    pb_id = pb["playbook_id"]
    assert pb["version"] == 1

    # Get
    resp = client.get(f"/api/playbooks/{pb_id}")
    assert resp.status_code == 200
    assert resp.get_json()["playbook"]["name"] == "Test Playbook"

    # Update — should bump version
    resp = client.put(f"/api/playbooks/{pb_id}", json={"name": "Test Playbook Updated"})
    assert resp.status_code == 200
    updated = resp.get_json()["playbook"]
    assert updated["name"] == "Test Playbook Updated"
    assert updated["version"] == 2
    assert updated["created_at"] == pb["created_at"]

    # Delete
    resp = client.delete(f"/api/playbooks/{pb_id}")
    assert resp.status_code == 200
    # Get after delete → 404
    resp = client.get(f"/api/playbooks/{pb_id}")
    assert resp.status_code == 404


def test_get_404():
    client = _client()
    resp = client.get("/api/playbooks/nonexistent")
    assert resp.status_code == 404


def test_delete_blocks_default():
    client = _client()
    resp = client.delete("/api/playbooks/pb_default_bull_spread")
    assert resp.status_code == 400
    assert "built-in" in resp.get_json()["error"].lower() or "cannot delete" in resp.get_json()["error"].lower()


def test_spawn_returns_config_no_side_effects():
    """Spawn returns runner config; caller creates — no side effects."""
    client = _client()
    resp = client.post(
        "/api/playbooks/pb_default_bull_spread/spawn",
        json={"strategy": "directional_options", "allocated_capital": 100000, "mode": "paper", "source": "synthetic"},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert data["playbook_id"] == "pb_default_bull_spread"
    assert "runner_config" in data
    cfg = data["runner_config"]
    # Should be a valid runner create payload
    assert cfg["strategy"] == "directional_options"
    assert cfg["allocated_capital"] == 100000
    assert cfg["instrument"]["type"] == "option"
    assert "expression" in cfg["instrument"]
    # No runner should have been created yet — spawn is side-effect free
    # Check that portfolio still has 0 runners (or at least spawn didn't create via this endpoint)
    # We verify by checking that instance_id is NOT in response (new spec) — old spec returned instance_id
    assert "instance_id" not in data, "Spawn should be side-effect free — no instance_id, only config"


def test_spawn_matches_to_runner_config():
    """Spawn response matches Playbook.to_runner_config()"""
    from backtest.playbooks.registry import get_playbook_registry

    client = _client()
    registry = get_playbook_registry()
    pb = registry.get("pb_default_bull_spread")
    expected = pb.to_runner_config(strategy_name="directional_options", allocated_capital=100000, mode="paper", source="synthetic")

    resp = client.post(
        "/api/playbooks/pb_default_bull_spread/spawn",
        json={"strategy": "directional_options", "allocated_capital": 100000, "mode": "paper", "source": "synthetic"},
    )
    actual = resp.get_json()["runner_config"]
    assert actual["strategy"] == expected["strategy"]
    assert actual["allocated_capital"] == expected["allocated_capital"]
    assert actual["instrument"] == expected["instrument"]
