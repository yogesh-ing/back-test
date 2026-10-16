"""Phase 2 cutover tests (Plan v2.1 §9.3) for the rewritten families.

Per rewritten strategy: registers + validates, declared spec is coherent,
spawn-from-defaults works, and (for the incident family) the documented
intentional divergence from the snapshot original is pinned — signal
*intent* preserved, contract violations (NEUTRAL-as-entry) abolished.
Originals remain byte-identical in docs/archive/strategy-snapshot-2026-10-15.
"""

from __future__ import annotations

from decimal import Decimal

import pandas as pd
import pytest

from backtest.forward.risk_supervisor import GlobalRiskConfig
from backtest.options.quote_providers import (
    SyntheticChainGenerator,
    SyntheticQuoteProvider,
)
from backtest.strategy.intent import Direction, MarketView


def _candles(closes=(24800.0, 24810.0, 24820.0)):
    idx = pd.date_range("2026-09-01", periods=len(closes), freq="D")
    return pd.DataFrame({"close": list(closes)}, index=idx)


@pytest.fixture
def client():
    from backtest.forward.portfolio_manager import reset_portfolio_manager
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


def _get(name):
    from backtest.plugins import discover_plugins
    from backtest.strategy.registry import get_strategy

    discover_plugins()
    return get_strategy(name)


# --------------------------------------------------------------------------- #
# MVP family: CE/PE instant buy (the incident, as tests)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "name,side,direction,structure",
    [
        ("CE_instant_buy", "CE", Direction.BULLISH, "long_call"),
        ("PE_instant_buy", "PE", Direction.BEARISH, "long_put"),
    ],
)
def test_instant_buy_strategies_state_the_trade(name, side, direction, structure):
    cls = _get(name)
    cls.validate()
    strategy = cls()
    assert strategy.side == side
    view = strategy.generate_market_view(_candles())
    assert view.direction is direction  # explicit — never NEUTRAL-as-entry
    intent = strategy.build_opening_intent(view)
    assert intent["structure"] == structure
    assert intent["exit"]["structure_capped"] is True
    # NEUTRAL from anything remains silence under this spec:
    assert cls.execution_spec().direction_rule_for(Direction.NEUTRAL) is None


def test_the_incident_both_underlyings_identical(client):
    # NIFTY + BANKNIFTY, same strategy, zero overrides -> identical v2
    # expression and both spawn. This exact asymmetry failed in production.
    created = {}
    for symbol in ("NIFTY", "BANKNIFTY"):
        r = client.post(
            "/api/portfolio/runner/create",
            json={
                "strategy": "CE_instant_buy",
                "target_type": "SINGLE_SYMBOL",
                "symbol": symbol,
                "timeframe": "1day",
                "allocated_capital": 500_000,
                "mode": "paper",
                "instrument": {"type": "option"},
            },
        )
        assert r.status_code == 201, (symbol, r.get_json())
        created[symbol] = r.get_json()["runner"]["instrument"]["expression"]
    assert created["NIFTY"] == created["BANKNIFTY"]
    assert created["NIFTY"]["type"]["BULLISH"]["structure"] == "long_call"


def test_abolished_strategies_are_gone_from_the_registry(client):
    from backtest.strategy.registry import list_strategies

    names = list_strategies()
    for gone in ("atm_instant_buy", "immediate_entry", "nifty_instant_buy",
                 "banknifty_straddle"):
        assert gone not in names
    assert "banknifty_ema_momentum" in names  # renamed, behaviour-named


# --------------------------------------------------------------------------- #
# Directional family: specs declared, signal logic untouched
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "name",
    [
        "ema_reversion_pob",
        "momentum_burst",
        "time_alternator",
        "delta_momentum",
        "directional_options",
        "banknifty_ema_momentum",
    ],
)
def test_directional_family_declares_coherent_spec(name):
    cls = _get(name)
    cls.validate()
    spec = cls.execution_spec()
    assert spec is not None
    assert spec.structures[Direction.BULLISH].structure == "long_call"
    assert spec.structures[Direction.BEARISH].structure == "long_put"
    assert spec.exit_plan.stop_loss_pct is not None  # mandatory hard stop
    from backtest.strategy.templates import DirectionalOptionStrategy

    assert issubclass(cls, DirectionalOptionStrategy)


# --------------------------------------------------------------------------- #
# Structure family: immediate_strangle states its trade explicitly
# --------------------------------------------------------------------------- #


def test_strangle_intent_is_explicit_and_opens_via_bridge():
    from backtest.forward.options_bridge import OptionsBridge

    cls = _get("immediate_strangle")
    cls.validate()
    strategy = cls()
    view = MarketView(
        direction=Direction.NEUTRAL, confidence=1.0, underlying="NIFTY",
        spot_price=Decimal("24800"),
    )
    intent = strategy.build_opening_intent(view)
    assert intent["structure"] == "strangle"
    assert intent["distance_pct"] == 2.0
    assert intent["exit"]["stop_loss_points"] == 100

    # Silence alone still opens nothing — the intent, by name, does.
    bridge = OptionsBridge(
        capital=1_000_000.0,
        expression=cls.default_expression(),
        quote_provider=SyntheticQuoteProvider(SyntheticChainGenerator()),
        mode="paper",
    )
    assert bridge.strict is True
    assert bridge.on_market_view(view, "immediate_strangle") is None
    result = bridge.on_intent(intent, view, "immediate_strangle")
    assert result and result.get("structure_type") == "strangle", result
    assert bridge.executed_count == 1


# --------------------------------------------------------------------------- #
# Equity family: template port, logic unchanged
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "name",
    [
        "bollinger_reversion", "buy_and_hold", "donchian_breakout", "ema_pullback",
        "macd_trend", "momentum_roc", "nifty_scalper", "price_move",
        "rsi_reversion", "sma_crossover",
    ],
)
def test_equity_builtins_ported_to_equity_template(name):
    from backtest.strategy.templates import EquityStrategy

    cls = _get(name)
    assert issubclass(cls, EquityStrategy)
    cls.validate()
    assert cls.execution_spec() is None  # equity path imposes no option contract


# --------------------------------------------------------------------------- #
# Directional tail + equity ports + imported triage (plan §9.4 steps 3/5/6)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", ["volume_oi_surge", "vwap_ema_rsi"])
def test_directional_tail_declares_spec(name):
    from backtest.strategy.templates import DirectionalOptionStrategy

    cls = _get(name)
    assert issubclass(cls, DirectionalOptionStrategy)
    cls.validate()
    spec = cls.execution_spec()
    assert spec.structures[Direction.BULLISH].structure == "long_call"
    assert spec.structures[Direction.BEARISH].structure == "long_put"
    # "volume" strike selection never existed in the bridge (it silently
    # degraded to ATM); ATM is now the declared behaviour, not a fallback.
    assert spec.structures[Direction.BULLISH].strike_selection == "atm"


@pytest.mark.parametrize(
    "name",
    [
        "hf_equity_scalp", "one_min_scalp", "swing_donchian_breakout",
        "swing_ema_pullback", "swing_hourly_supertrend", "swing_rsi2_meanrev",
        "supertrend_equity",
    ],
)
def test_equity_plugins_ported(name):
    from backtest.strategy.templates import EquityStrategy

    cls = _get(name)
    assert issubclass(cls, EquityStrategy)
    cls.validate()


def test_imported_triage_one_survivor_five_retired():
    from backtest.strategy.registry import list_strategies

    _get("supertrend_equity")  # forces discovery
    names = list_strategies()
    assert "supertrend_equity" in names
    for retired in ("v1", "v2", "v3", "mysupertrendv1",
                    "outsidebarstrategy", "supertrendstrategy"):
        assert retired not in names  # snapshot preserves every original


def test_survivor_signals_match_snapshot_original():
    # Fixture equivalence (§9.3 #2): the survivor's signals on deterministic
    # candles must equal the snapshot original's, bar for bar.
    import importlib.util
    from pathlib import Path

    from backtest.strategy import registry

    snap = (
        Path(__file__).resolve().parents[1]
        / "docs/archive/strategy-snapshot-2026-10-15/plugins/supertrend_strategy_imported.py"
    )
    module_spec = importlib.util.spec_from_file_location("_snapshot_supertrend", snap)
    module = importlib.util.module_from_spec(module_spec)
    try:
        module_spec.loader.exec_module(module)
        idx = pd.date_range("2026-01-01", periods=120, freq="D")
        closes = [100 + i * 0.8 for i in range(60)] + [148 - i * 0.9 for i in range(60)]
        candles = pd.DataFrame(
            {
                "open": closes, "high": [c * 1.01 for c in closes],
                "low": [c * 0.99 for c in closes], "close": closes,
                "volume": [1000] * 120,
            },
            index=idx,
        )
        original = module.SupertrendStrategy()
        survivor = _get("supertrend_equity")()
        assert list(survivor.generate_signals(candles)) == list(
            original.generate_signals(candles)
        )
    finally:
        registry.unregister("supertrendstrategy")
