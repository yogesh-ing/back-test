"""Phase 3 — UI contract, server side (Plan v2.1 Phase 3, §7, §11).

The spawn form now sends field selections only; playbooks travel as
layer-4 selection bundles; ceilings refuse, never clamp; the Layer-1
rulebook is readable but not writable; runner detail carries the §8
spawn fingerprints and the code-drift flag.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def client():
    from backtest.forward.portfolio_manager import reset_portfolio_manager
    from backtest.forward.risk_supervisor import GlobalRiskConfig
    from backtest.web.app import create_app

    reset_portfolio_manager(
        risk_config=GlobalRiskConfig(daily_loss_limit=100_000, max_drawdown_pct=0.50),
        tick_seconds=1.0, warmup_bars=15, auto_start_feed=False,
    )
    app = create_app(source="synthetic")
    with app.test_client() as c:
        yield c
    from backtest.forward.portfolio_manager import get_portfolio_manager

    get_portfolio_manager().shutdown()


def _spawn(client, **overrides):
    body = {
        "strategy": "directional_options",
        "target_type": "SINGLE_SYMBOL",
        "symbol": "NIFTY",
        "timeframe": "1day",
        "allocated_capital": 500_000,
        "mode": "paper",
        "instrument": {"type": "option"},
    }
    body.update(overrides)
    return client.post("/api/portfolio/runner/create", json=body)


def _playbook(playbook_id):
    from backtest.playbooks.registry import get_playbook_registry

    return get_playbook_registry().get(playbook_id)


# --------------------------------------------------------------------------- #
# Selections-only spawn (the form's new shape)
# --------------------------------------------------------------------------- #


def test_spawn_with_no_selections_uses_strategy_defaults(client):
    r = _spawn(client)
    assert r.status_code == 201, r.get_json()
    expr = r.get_json()["runner"]["instrument"]["expression"]
    assert expr["type"]["BULLISH"]["structure"] == "long_call"
    assert expr["exit"]["stop_loss_pct"] == 0.5  # the strategy's, no one else's


def test_menu_bounded_exit_override_applies_with_provenance(client):
    r = _spawn(client, expression_selections={"exit": {"stop_loss_pct": 0.25}})
    assert r.status_code == 201, r.get_json()
    runner = r.get_json()["runner"]
    assert runner["instrument"]["expression"]["exit"]["stop_loss_pct"] == 0.25
    prov = runner["instrument"]["expression_provenance"]
    assert prov["exit.stop_loss_pct"] == "user"
    assert prov["type.BULLISH.structure"] == "strategy"


def test_quantity_above_menu_refuses_never_clamps(client):
    r = _spawn(client, expression_selections={"quantity": 99})
    assert r.status_code == 400
    assert "menu" in r.get_json()["error"]


def test_structure_override_outside_menu_refuses(client):
    r = _spawn(
        client,
        expression_selections={"type": {"BULLISH": {"structure": "bull_call_spread"}}},
    )
    assert r.status_code == 400
    assert "type" in r.get_json()["error"]


def test_restating_strategy_structures_is_a_noop_not_a_refusal(client):
    # A selection naming the strategy's own values for a non-menu field
    # changes nothing — it must not refuse against itself.
    r = _spawn(
        client,
        expression_selections={
            "type": {
                "BULLISH": {"structure": "long_call", "strike_selection": "atm"},
                "BEARISH": {"structure": "long_put", "strike_selection": "atm"},
            }
        },
    )
    assert r.status_code == 201, r.get_json()


# --------------------------------------------------------------------------- #
# Playbooks as layer-4 bundles
# --------------------------------------------------------------------------- #


def test_directional_playbook_bundle_resolves(client):
    pb = _playbook("pb_default_long")
    r = _spawn(
        client,
        playbook_id=pb.playbook_id,
        playbook_selections=pb.to_selections(),
        expression_selections={"quantity": 2},
    )
    assert r.status_code == 201, r.get_json()
    runner = r.get_json()["runner"]
    expr = runner["instrument"]["expression"]
    assert expr["exit"]["stop_loss_pct"] == 0.3  # playbook layer
    assert expr["exit"]["take_profit_pct"] == 1.5
    assert expr["exit"]["max_loss_inr"] == 3000.0  # playbook cap, translated
    assert expr["quantity"] == 2  # user layer wins over the bundle
    prov = runner["instrument"]["expression_provenance"]
    assert prov["exit.stop_loss_pct"] == "playbook"
    assert prov["quantity"] == "user"
    assert prov["type.BULLISH.structure"] == "strategy"


def test_spread_playbook_against_directional_strategy_refuses(client):
    # The bundle wants bull/bear spreads; directional_options' menu does
    # not offer structure choice. Loud refusal naming the field (§11).
    pb = _playbook("pb_default_bull_spread")
    r = _spawn(
        client,
        playbook_id=pb.playbook_id,
        playbook_selections=pb.to_selections(),
    )
    assert r.status_code == 400, r.get_json()
    assert "type" in r.get_json()["error"]


# --------------------------------------------------------------------------- #
# Read-only rulebook + runner fingerprints
# --------------------------------------------------------------------------- #


def test_risk_limits_endpoint_is_read_only_and_current(client):
    r = client.get("/api/portfolio/risk/limits")
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["read_only"] is True
    limits = body["limits"]
    assert limits["per_trade_max_loss_inr"] == 25000
    assert limits["entitlement"]["live"] == ["long_call", "long_put"]
    assert len(limits["sha256"]) == 64
    # No mutation verb exists on this surface.
    assert client.post("/api/portfolio/risk/limits", json={}).status_code in (404, 405)


def test_runner_detail_carries_spawn_fingerprints(client):
    # Builtin strategy: plugin files can be rewritten by other suites'
    # converter tests mid-process, which makes plugin bundle hashing
    # fail (loudly logged, stamp omitted) — the builtin's files are stable.
    r = _spawn(client)
    assert r.status_code == 201, r.get_json()
    instance_id = r.get_json()["runner"]["instance_id"]
    detail = client.get(f"/api/portfolio/runner/{instance_id}")
    assert detail.status_code == 200, detail.get_json()
    runner = detail.get_json()["runner"]
    bundle = runner["strategy_bundle"]
    assert bundle["bundle_sha256"]
    assert bundle["risk_limits_sha256"]
    assert runner["strategy_code_changed"] is False


def test_take_profit_above_100pct_is_expressible(client):
    # A premium can more than double: targets above +100% are legal v2
    # (the strict parser's old shared (0, 1] bound conflated stop and
    # target; stops remain bounded, targets do not).
    r = _spawn(client, expression_selections={"exit": {"take_profit_pct": 1.5}})
    assert r.status_code == 201, r.get_json()
    assert r.get_json()["runner"]["instrument"]["expression"]["exit"]["take_profit_pct"] == 1.5
    # …while a stop above 100% of premium remains inexpressible.
    r = _spawn(client, expression_selections={"exit": {"stop_loss_pct": 1.5}})
    assert r.status_code == 400


def test_playbook_page_flow_spawn_config_creates_runner(client):
    # playbooks.js posts the spawn endpoint's runner_config verbatim to
    # runner/create — that round trip must work for spec'd strategies.
    spawn = client.post(
        "/api/playbooks/pb_default_long/spawn",
        json={
            "strategy": "directional_options",
            "allocated_capital": 500_000,
            "mode": "paper",
            "source": "synthetic",
        },
    )
    assert spawn.status_code == 200, spawn.get_json()
    rc = spawn.get_json()["runner_config"]
    rc["timeframe"] = "1day"
    r = client.post("/api/portfolio/runner/create", json=rc)
    assert r.status_code == 201, r.get_json()
    expr = r.get_json()["runner"]["instrument"]["expression"]
    assert expr["exit"]["stop_loss_pct"] == 0.3
