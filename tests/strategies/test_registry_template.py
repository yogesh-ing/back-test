"""Run the strategy test template against EVERY registered strategy (CI sweep).

The template (``templates/strategy_test_template.py``) was designed to be
copied per strategy into ``tests/strategies/``. Nobody did, so most
strategies were only ever checked by the loader's conformance battery —
the deeper R-D (lookahead, backtest≡forward), R-T (RangeIndex) and R-P
(per-bar budget) rules were unenforced. This module closes that gap: it
imports the template ONCE and parametrizes its checks over the whole
strategy registry (built-ins + plugins), so:

* every strategy — existing or added tomorrow — gets the full battery
  automatically; no manual copy, no forgotten copy;
* a strategy that regresses on a rule fails CI with the rule name.

Known violations are recorded in ``KNOWN_FAILS`` as *xfails with the rule
they break* (the list mirrors docs/STRATEGY-GUIDELINES.md §8, "platform
facts that bite"). They show as ``xfailed`` — visibly tracked, not
silently green. When one is fixed, it flips to ``XPASS`` (a pass, not a
failure) and the entry should be deleted. A NEW strategy failing a rule
shows as a hard failure: fix the strategy or, if it is a genuine known
issue, add it to ``KNOWN_FAILS`` with the rule and reason.

Note on scope: the lookahead / backtest≡forward probes sample every
``SWEEP_SAMPLE_EVERY`` bars (looser than the template's 7) and run one
scenario for the two quadratic-cost checks, to keep the whole sweep
CI-affordable while still catching frame-anchored logic. A per-strategy
copy of the template (with ``SAMPLE_EVERY = 7`` and all three scenarios)
remains the gold standard for a strategy about to go live.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Registry snapshot (collection time — same pattern as tests/test_strategy_conformance.py)
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: Strategies documented as failing specific template rules (guidelines §8).
#: strategy name → {template test id → "rule — reason"}
KNOWN_FAILS: dict[str, dict[str, str]] = {
    "time_alternator": {
        "test_backtest_equals_forward_window": (
            "R-D2 — direction freezes in the forward window (bar-parity "
            "anchored to frame start)"
        ),
    },
    "vwap_ema_rsi": {
        "test_backtest_equals_forward_window": (
            "R-D2 — frame-anchored VWAP; rare disagreements on 1-min"
        ),
    },
    "ema_reversion_pob": {
        "test_positional_index_fallback": (
            "R-T2 — raises on a RangeIndex feed (no parseable timestamps)"
        ),
    },
    "bollinger_reversion": {
        "test_per_bar_budget": "R-P1 — 25–31 ms per evaluation on a full buffer",
    },
    "donchian_breakout": {
        "test_per_bar_budget": "R-P1 — 25–31 ms per evaluation on a full buffer",
    },
    "nifty_scalper": {
        "test_per_bar_budget": "R-P1 — 25–31 ms per evaluation on a full buffer",
    },
}

#: Strategies whose docstrings document a DELIBERATE unconditional NEUTRAL
#: view (fixed single-structure expression: every direction maps to one
#: structure, so NEUTRAL is the "enter now" signal — template flag
#: ``ALLOWS_NEUTRAL_VIEW``). Anything else returning NEUTRAL is a bug.
DELIBERATE_NEUTRAL = {"atm_instant_buy", "immediate_entry", "immediate_strangle"}

#: Strategies whose docstrings declare a constant/holding contract — the
#: template's ``EXPECTS_DECISION_CHANGES`` is set False for them via a
#: module-level flag before the test body runs. (``buy_and_hold`` holds
#: forever; ``time_alternator`` alternates on every bar in the same window;
#: ``vwap_ema_rsi`` only emits a view on crossover bars.)
CONSTANT_DECISION = {
    "buy_and_hold",
    "time_alternator",
    "vwap_ema_rsi",
    # Instant-entry instruments: unconditional view on EVERY bar by design
    # ("book the structure at once, hold per the runner's exit config").
    "atm_instant_buy",
    "immediate_entry",
    "immediate_strangle",
}

_TEMPLATE_PATH = PROJECT_ROOT / "templates" / "strategy_test_template.py"


def _load_template():
    """Import the template module once (its test bodies are plain functions)."""
    spec = importlib.util.spec_from_file_location("_strategy_test_template", _TEMPLATE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TPL = _load_template()

# Looser sampling than the template, so the sweep over ~17 strategies stays
# inside a CI-friendly wall clock (see module docstring).
TPL.SAMPLE_EVERY = 17

# Quadratic-cost checks (re-decide per probe) run the trend scenario only.
QUADRATIC_SCENARIOS = ("trend",)
# Cheap checks run all three regimes.
ALL_SCENARIOS = tuple(sorted(TPL.SCENARIOS))


def _real_strategies() -> list[str]:
    """Registered strategies that are real (built-ins + plugins), not
    test-module pollution. The registry is global and any test module that
    defines a Strategy subclass lands in it, so filter by defining module."""
    from backtest.plugins import discover_plugins
    from backtest.strategy.registry import get_strategy, list_strategies

    discover_plugins()
    names = []
    for name in sorted(list_strategies()):
        module = getattr(get_strategy(name), "__module__", "")
        if module.startswith(("backtest.strategies", "backtest.plugins")) or module.startswith(
            "_plugin_"
        ):
            names.append(name)
    return names


NAMES = _real_strategies()


def _params(test_id: str):
    """Per-strategy params with xfail marks from the allowlist."""
    return [
        pytest.param(
            name,
            id=name,
            marks=tuple(pytest.mark.xfail(reason=reason) for reason in [KNOWN_FAILS[name][test_id]])
            if test_id in KNOWN_FAILS.get(name, {})
            else (),
        )
        for name in NAMES
    ]


def _pair(name: str):
    from backtest.strategy.registry import get_strategy, signal_kind

    cls = get_strategy(name)
    # Strategies with a documented deliberate-NEUTRAL contract.
    TPL.ALLOWS_NEUTRAL_VIEW = name in DELIBERATE_NEUTRAL
    # Strategies with a documented constant/hold decision contract.
    TPL.EXPECTS_DECISION_CHANGES = name not in CONSTANT_DECISION
    return cls, signal_kind(cls)


# ---------------------------------------------------------------------------
# The sweep — one test per template rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", _params("test_conformance_battery"))
def test_conformance_battery(name):
    """[R-C1] Loader's battery: identity, metadata, shape, determinism."""
    cls, _ = _pair(name)
    from backtest.plugins import conformance_errors

    assert conformance_errors(cls).errors == []


@pytest.mark.parametrize("name", _params("test_params_are_bounded_and_documented"))
def test_params_are_bounded_and_documented(name):
    """[R-M2] Numeric params bounded, every param has a trader-actionable tooltip."""
    cls, _ = _pair(name)
    for key, spec in cls.param_schema().items():
        if spec["type"] in ("int", "float"):
            assert spec["min"] is not None and spec["max"] is not None, f"{key}: unbounded"
        assert spec["tooltip"], f"{key}: no tooltip"


@pytest.mark.parametrize("edge", ["min", "max"])
@pytest.mark.parametrize("name", _params("test_runs_at_param_extremes"))
def test_runs_at_param_extremes(name, edge):
    """[R-M2] Every value the spawn form allows must run — deterministically."""
    cls, kind = _pair(name)
    frame = TPL.SCENARIOS["trend"]
    params = {}
    for key, spec in cls.param_schema().items():
        if spec["type"] in ("int", "float") and spec[edge] is not None:
            params[key] = spec[edge]
    try:
        cls(**params)
    except ValueError:
        pytest.skip("param combination rejected by the strategy's own validation")
    a = TPL.decision(cls, kind, frame.iloc[-TPL.FORWARD_WINDOW:], **params)
    b = TPL.decision(cls, kind, frame.iloc[-TPL.FORWARD_WINDOW:], **params)
    assert a == b


@pytest.mark.parametrize("scenario", QUADRATIC_SCENARIOS)
@pytest.mark.parametrize("name", _params("test_no_lookahead_by_truncation"))
def test_no_lookahead_by_truncation(name, scenario):
    """[R-D1] Decision at bar t must not change when bars after t are removed."""
    cls, kind = _pair(name)
    TPL.test_no_lookahead_by_truncation(cls, kind, scenario)


@pytest.mark.parametrize("scenario", QUADRATIC_SCENARIOS)
@pytest.mark.parametrize("name", _params("test_backtest_equals_forward_window"))
def test_backtest_equals_forward_window(name, scenario):
    """[R-D2] Full history and the 500-bar forward buffer agree."""
    cls, kind = _pair(name)
    TPL.test_backtest_equals_forward_window(cls, kind, scenario)


@pytest.mark.parametrize("name", _params("test_instance_is_not_mutated_by_evaluation"))
def test_instance_is_not_mutated_by_evaluation(name):
    """[R-D4] One instance lives for the runner's lifetime — stay a pure function."""
    cls, kind = _pair(name)
    TPL.test_instance_is_not_mutated_by_evaluation(cls, kind)


@pytest.mark.parametrize("name", _params("test_short_history_is_safe"))
def test_short_history_is_safe(name):
    """[R-D3] 1..warmup bars: flat or valid, never a crash."""
    cls, kind = _pair(name)
    TPL.test_short_history_is_safe(cls, kind)


@pytest.mark.parametrize("name", _params("test_positional_index_fallback"))
def test_positional_index_fallback(name):
    """[R-T2] Feeds without parseable timestamps give a RangeIndex frame."""
    cls, kind = _pair(name)
    TPL.test_positional_index_fallback(cls, kind)


@pytest.mark.parametrize("name", _params("test_degenerate_bars"))
def test_degenerate_bars(name):
    """[R-D5] Flat prices, zero volume, NaN volume: no crash, no NaN out."""
    cls, kind = _pair(name)
    TPL.test_degenerate_bars(cls, kind)


@pytest.mark.parametrize("name", _params("test_output_semantics"))
def test_output_semantics(name):
    """[R-S1]/[R-O1..O4] Equity: {-1,0,1} aligned int. Option: valid, honest view."""
    cls, kind = _pair(name)
    TPL.test_output_semantics(cls, kind)


@pytest.mark.parametrize("name", _params("test_decisions_change_somewhere"))
def test_decisions_change_somewhere(name):
    """[R-S3] A strategy that never changes its mind across regimes is broken."""
    cls, kind = _pair(name)
    TPL.test_decisions_change_somewhere(cls, kind)


@pytest.mark.parametrize("name", _params("test_per_bar_budget"))
def test_per_bar_budget(name):
    """[R-P1] One evaluation on a full forward buffer inside the budget."""
    cls, kind = _pair(name)
    TPL.test_per_bar_budget(cls, kind)


# ---------------------------------------------------------------------------
# R-E1 — the cost haircut
# ---------------------------------------------------------------------------


#: Equity strategies only (the canonical engine trades equity signals).
#: Known-fail entries for the cost test, same discipline as the allowlist
#: above: an entry is a DOCUMENTED reason to expect the strategy to be
#: marginal at real costs, not a free pass — fix the strategy or retire it.
COST_KNOWN_FAILS: dict[str, str] = {
    "price_move": (
        "R-E1 — measured 2026-09-28: gross +6.3% but net −10.5% at mstock "
        "costs (79 all-in round trips × ~0.1%/side delivery STT = ₹160k "
        "fees on ₹1M). The threshold flip-flops too often for delivery-cost "
        "equity; needs a turnover dampener or intraday segment before it is "
        "deployment-worthy"
    ),
}


@pytest.mark.parametrize("name", _params("test_survives_real_costs"))
def test_survives_real_costs(name):
    """[R-E1] Net-of-costs equity does not collapse vs the zero-cost run.

    Runs the CANONICAL engine twice on the same trend scenario: zero-cost
    (the default everywhere) and with the ``mstock`` statutory stack
    (₹20/order + STT/exchange/SEBI/stamp/GST via ``costed_executor``).
    Passes when the costed final equity stays within ``COLLAPSE_RATIO`` of
    the zero-cost final equity — i.e. costs did not eat the strategy.
    Deterministic on both paths (seeded executor), so the comparison is
    exact, not statistical.
    """
    import pandas as pd

    from backtest.engine.backtest_runner import run_backtest

    if name in KNOWN_FAILS and "test_survives_real_costs" in KNOWN_FAILS[name]:
        pytest.xfail(KNOWN_FAILS[name]["test_survives_real_costs"])
    if name in COST_KNOWN_FAILS:
        pytest.xfail(COST_KNOWN_FAILS[name])

    cls, kind = _pair(name)
    if kind != "equity":
        pytest.skip("cost haircut runs the equity engine; option views skip")

    frame = TPL.SCENARIOS["trend"]
    symbol = "TEST"
    capital = 1_000_000.0

    free = run_backtest(frame, name, None, symbol, capital)
    costed = run_backtest(frame, name, None, symbol, capital, broker="mstock")

    # Sanity: the costed run must actually have charged something when it
    # traded — otherwise this test silently compares two zero-cost runs.
    if float(free.equity.iloc[-1]) != float(costed.equity.iloc[-1]):
        assert float(costed.metrics.get("fees_paid", 0)) > 0, (
            "costed run differs but reports zero fees — fee plumbing broken"
        )

    free_final = float(free.equity.iloc[-1])
    costed_final = float(costed.equity.iloc[-1])
    # "Collapse" = costs turn a profitable/flat strategy deeply negative.
    # Threshold: net result worse than -10% of capital while gross was
    # non-negative, or net < gross - 50% of capital (fee bleed).
    collapsed = costed_final < capital * 0.90 and free_final >= capital
    bled = (free_final - costed_final) > capital * 0.50
    assert not (collapsed or bled), (
        f"R-E1 cost collapse: gross final {free_final:,.0f} vs mstock-cost "
        f"final {costed_final:,.0f} on {capital:,.0f} capital — the edge does "
        "not survive realistic costs; rework turnover or size"
    )
