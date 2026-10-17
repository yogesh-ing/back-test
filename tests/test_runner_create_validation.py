"""Phase 1 — spawn-time strict resolution (Plan v2.1 §6/§7/§8).

create_runner resolves a spec'd strategy's expression once, server-side:
strategy defaults, menu-bounded selections, ceiling refusals (never a
silent clamp). Legacy v1 spawns keep the old behaviour. Rehydration with
a changed strategy bundle comes back PAUSED (STRATEGY_CODE_CHANGED).
"""

from __future__ import annotations

import pytest

from backtest.forward.risk_supervisor import GlobalRiskConfig
from backtest.strategy.templates import DirectionalOptionStrategy


@pytest.fixture(autouse=True)
def _purge_fixture_strategies():
    from backtest.strategy import registry as _registry

    def _purge():
        for name in [n for n in _registry._REGISTRY if n.startswith("phase1")]:
            _registry.unregister(name)

    _purge()
    # The fixture class registered at collection time; the purge removed
    # it, so put exactly this file's class back for the test itself.
    from backtest.strategy.registry import register

    register(Phase1SpecStrategy)
    yield
    _purge()


class Phase1SpecStrategy(DirectionalOptionStrategy):
    name = "phase1_spec_strategy"
    description = "Phase 1 spawn-resolution fixture"
    version = "1.0"
    author = "tests"
    eligible_instruments = ["NIFTY", "BANKNIFTY"]
    execution = {
        "emits": ["bullish", "bearish"],
        "structures": {
            "bullish": {"structure": "long_call", "strike_selection": "atm"},
            "bearish": {"structure": "long_put", "strike_selection": "atm"},
        },
        "exit": {"stop_loss_pct": 0.30, "min_days_to_expiry": None},
        "quantity": 1,
        "min_quantity": 1,
        "max_quantity": 5,
        "allowed_overrides": ["quantity"],
    }

    def entries(self, candles):
        return None

    def generate_market_view(self, candles):
        return None


@pytest.fixture
def client():
    from backtest.forward.portfolio_manager import reset_portfolio_manager
    from backtest.web.app import create_app

    reset_portfolio_manager(
        risk_config=GlobalRiskConfig(daily_loss_limit=100_000, max_drawdown_pct=0.50),
        tick_seconds=1.0,
        warmup_bars=15,
        auto_start_feed=False,
    )
    app = create_app(source="synthetic")
    with app.test_client() as c:
        yield c
    from backtest.forward.portfolio_manager import get_portfolio_manager

    get_portfolio_manager().shutdown()


def _spawn(client, **overrides):
    body = {
        "strategy": "phase1_spec_strategy",
        "target_type": "SINGLE_SYMBOL",
        "symbol": "NIFTY",
        "timeframe": "1day",
        "allocated_capital": 500_000,
        "mode": "paper",
        "instrument": {"type": "option"},
    }
    body.update(overrides)
    return client.post("/api/portfolio/runner/create", json=body)


def test_create_runner_with_strategy_defaults(client):
    r = _spawn(client)
    assert r.status_code == 201, r.get_json()
    runner = r.get_json()["runner"]
    expr = runner["instrument"]["expression"]
    assert expr["expression_version"] == 2
    assert expr["type"]["BULLISH"]["structure"] == "long_call"
    assert expr["quantity"] == 1
    assert runner["instrument"]["expression_provenance"]["type.BULLISH.structure"] == "strategy"
    assert runner["strategy_bundle"]["bundle_sha256"]
    assert runner["strategy_bundle"]["risk_limits_sha256"]
    assert runner["strategy_bundle"]["config_sha256"]


def test_create_runner_with_user_overrides_within_menu(client):
    r = _spawn(client, expression_selections={"quantity": 3})
    assert r.status_code == 201, r.get_json()
    runner = r.get_json()["runner"]
    assert runner["instrument"]["expression"]["quantity"] == 3
    assert runner["instrument"]["expression_provenance"]["quantity"] == "user"


def test_create_runner_out_of_menu_is_refused_not_clamped(client):
    r = _spawn(client, expression_selections={"quantity": 99})
    assert r.status_code == 400
    assert "lot menu" in r.get_json()["error"]


def test_create_runner_rejects_undeclared_override_field(client):
    r = _spawn(client, expression_selections={"exit": {"stop_loss_pct": 0.1}})
    assert r.status_code == 400  # 'exit' is not in this strategy's allowed_overrides


class _LegacyProbeContext:
    """Registers an unmigrated (spec-less) option strategy for the duration
    of a test, then removes it. directional_options itself was rewritten in
    Phase 2, so the transitional spawn paths need a genuinely unmigrated
    subject; defining the class inside the test keeps it out of collection-
    time registration for other suites."""

    def __enter__(self):
        from backtest.strategy import registry
        from backtest.strategy.base import Strategy
        from backtest.strategy.intent import Direction, MarketView

        class LegacyOptionProbe(Strategy):
            name = "legacy_option_probe"

            def generate_market_view(self, df):  # noqa: ANN001, ANN201
                return MarketView(direction=Direction.BULLISH, confidence=1.0)

        self._registry = registry
        return "legacy_option_probe"

    def __exit__(self, *exc):
        self._registry._REGISTRY.pop("legacy_option_probe", None)
        return False


def test_v2_expression_without_spec_is_gate_validated(client):
  with _LegacyProbeContext() as probe:
    r = client.post(
        "/api/portfolio/runner/create",
        json={
            "strategy": probe,
            "target_type": "SINGLE_SYMBOL",
            "symbol": "NIFTY",
            "timeframe": "1day",
            "allocated_capital": 500_000,
            "mode": "paper",
            "instrument": {
                "type": "option",
                "expression": {
                    "expression_version": 2,
                    "type": {"BULLISH": "bull_call_spread"},
                    "quantity": 1,
                    "exit": {"take_profit_pct": 1.0},  # no hard stop -> refuse
                },
            },
        },
    )
    assert r.status_code == 400
    assert "hard stop" in r.get_json()["error"] or "exactly one" in r.get_json()["error"]


def test_legacy_v1_spawn_still_works_and_keeps_clamp(client):
  with _LegacyProbeContext() as probe:
    r = client.post(
        "/api/portfolio/runner/create",
        json={
            "strategy": probe,
            "target_type": "SINGLE_SYMBOL",
            "symbol": "NIFTY",
            "timeframe": "1day",
            "allocated_capital": 500_000,
            "mode": "paper",
            "instrument": {
                "type": "option",
                "expression": {"type": "long_call", "quantity": 500},
            },
        },
    )
    assert r.status_code == 201, r.get_json()
    # Legacy silent clamp still in place until Phase 4 (documented, not liked).
    assert r.get_json()["runner"]["instrument"]["expression"]["quantity"] == 100


# --------------------------------------------------------------------------- #
# Rehydration: bundle mismatch -> PAUSED + STRATEGY_CODE_CHANGED (plan §8)
# --------------------------------------------------------------------------- #


def _runner_with_bundle(tmp_bundle):
    from backtest.forward.paper_runner import OrderLedger, RunnerConfig, StrategyRunner

    return StrategyRunner(
        RunnerConfig(
            name="BUNDLE",
            strategy_name="buy_and_hold",
            allocated_capital=1_000_000,
            symbols=["RELIANCE"],
            timeframe="1day",
            mode="paper",
            source="synthetic",
            strategy_bundle=tmp_bundle,
        ),
        ledger=OrderLedger(),
    )


def test_rehydration_with_changed_bundle_pauses_runner():
    from backtest.forward.state_store import capture_runner, restore_runner
    from backtest.strategy.bundle import strategy_bundle
    from backtest.strategy.registry import get_strategy

    good = strategy_bundle(get_strategy("buy_and_hold")).to_dict()
    original = _runner_with_bundle(good)
    captured = capture_runner(original)

    tampered = {
        **good,
        "bundle_files": [
            {**f, "sha256": "0" * 64} if i == 0 else f
            for i, f in enumerate(good["bundle_files"])
        ],
    }
    revived = _runner_with_bundle(tampered)
    restore_runner(revived, captured)
    assert revived.strategy_code_changed is True
    assert revived.status == "PAUSED"
    assert "STRATEGY_CODE_CHANGED" in (revived.error or "")

    # Resuming is the conscious migration: bundle restamped, flag cleared.
    revived.resume()
    assert revived.strategy_code_changed is False
    assert revived.config.strategy_bundle["bundle_sha256"] == good["bundle_sha256"]


def test_rehydration_with_matching_bundle_is_clean():
    from backtest.forward.state_store import capture_runner, restore_runner
    from backtest.strategy.bundle import strategy_bundle
    from backtest.strategy.registry import get_strategy

    good = strategy_bundle(get_strategy("buy_and_hold")).to_dict()
    original = _runner_with_bundle(good)
    revived = _runner_with_bundle(good)
    restore_runner(revived, capture_runner(original))
    assert revived.strategy_code_changed is False


def test_full_default_expression_from_client_is_no_selections(client):
    # Flag 3: the Phase 3 UI shape — the strategy's own full v2 expression
    # sent unchanged — must spawn as strategy defaults, not 400 on the
    # expression_version envelope key.
    from backtest.strategy.registry import get_strategy

    default_expr = get_strategy("phase1_spec_strategy").default_expression()
    r = _spawn(client, instrument={"type": "option", "expression": default_expr})
    assert r.status_code == 201, r.get_json()
    runner = r.get_json()["runner"]
    assert runner["instrument"]["expression"]["quantity"] == 1
    assert (
        runner["instrument"]["expression_provenance"]["type.BULLISH.structure"]
        == "strategy"
    )


def test_restart_mid_position_preserves_structure_and_exits(client):
    # Flag 4b / §9.3 #5: a v2 runner restarted mid-position keeps its open
    # structure from the snapshot, and the frozen exit rules still fire.
    from backtest.forward.paper_runner import OrderLedger, RunnerConfig, StrategyRunner
    from backtest.forward.state_store import capture_runner, restore_runner
    from backtest.strategy.intent import Direction, MarketView
    from decimal import Decimal

    expr = {
        "expression_version": 2,
        "type": {"BULLISH": {"structure": "long_call", "strike_selection": "atm"}},
        "quantity": 1,
        "exit": {
            "stop_loss_pct": 0.30,
            "signal_flip": True,
            "min_days_to_expiry": None,
        },
    }

    def _make():
        return StrategyRunner(
            RunnerConfig(
                name="V2-RESTART",
                strategy_name="directional_options",
                allocated_capital=500_000,
                symbols=["NIFTY"],
                timeframe="1day",
                mode="paper",
                source="synthetic",
                instrument={"type": "option", "expression": dict(expr)},
            ),
            ledger=OrderLedger(),
        )

    runner = _make()
    view = MarketView(
        direction=Direction.BULLISH, confidence=0.9, underlying="NIFTY",
        spot_price=Decimal("24800"),
    )
    opened = runner.options_bridge.on_market_view(view, "directional_options")
    assert opened and runner.options_bridge.open_structure_id

    revived = _make()
    restore_runner(revived, capture_runner(runner))
    assert revived.options_bridge.open_structure_id == runner.options_bridge.open_structure_id
    assert revived.strategy_code_changed is False  # no bundle was stamped

    flip = MarketView(
        direction=Direction.BEARISH, confidence=0.9, underlying="NIFTY",
        spot_price=Decimal("24800"),
    )
    closed = revived.options_bridge.on_market_view(flip, "directional_options")
    assert closed and closed.get("exited") is True
