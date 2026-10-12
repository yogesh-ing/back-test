"""Node harnesses for the shared UI components (metrics cards, trade table).

Same pattern as ``tests/test_broker_ui.py``: drive the real files under
``web/static/js/components/`` in a stub DOM, skip when node is unavailable.
These components are where the G1/G2 metric semantics become visible to a user,
so they are pinned in JS rather than only in Python.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_HARNESS = _REPO_ROOT / "tests" / "js" / "test_metrics_cards.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_metrics_cards_and_trade_table_behaviour():
    result = subprocess.run(
        ["node", str(_HARNESS)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert (
        result.returncode == 0
    ), f"node harness failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "10 tests passed" in result.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_forward_live_widgets_render():
    """G3: the forward page's equity chart, metric cards, progress and positions."""
    harness = _REPO_ROOT / "tests" / "js" / "test_forward_widgets.mjs"
    result = subprocess.run(
        ["node", str(harness)], cwd=_REPO_ROOT, capture_output=True, text=True, timeout=60
    )
    assert (
        result.returncode == 0
    ), f"node harness failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "7 tests passed" in result.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
@pytest.mark.parametrize(
    "harness,expected",
    [
        ("test_position_actions.mjs", 12),
        # Phase 3 (amend / aging / retry lineage) added nine more.
        ("test_orders_tab.mjs", 21),
        # Portfolio Intelligence: alert widget + Risk Board intelligence sections.
        ("test_alert_widget.mjs", 11),
    ],
)
def test_live_order_management_components(harness, expected):
    """The positions-table actions and the Orders tab (Live Order Management).

    These are the app's money-moving controls: the harness pins that an action
    carries the row's own identity, that a server refusal is shown verbatim
    instead of closing the modal, that a live order only *placed* at the venue
    never reads as filled, and that slippage stays adverse-positive.
    """
    result = subprocess.run(
        ["node", str(_REPO_ROOT / "tests" / "js" / harness)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert (
        result.returncode == 0
    ), f"node harness failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert f"{expected} tests passed" in result.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_provenance_badges_behaviour():
    """PRD backTest-enhance §1.1/§1.2: the engine + data badges a result page
    shows above its numbers, and the banners it may never hide."""
    result = subprocess.run(
        ["node", str(_REPO_ROOT / "tests" / "js" / "test_provenance.mjs")],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert (
        result.returncode == 0
    ), f"node harness failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "13 tests passed" in result.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_symbol_picker_and_timeframes_behaviour():
    """PRD backTest-enhance §1.3/§1.4: the shared instrument list and the
    timeframe vocabulary behind it.

    Pins the two defects the PRD names: a symbol with no cached bars silently
    disappearing from the picker, and a timeframe dropdown offering
    granularities that produce no bars (and annualising them with a daily
    factor when they do). Also pins issues.txt B1 (2026-10-01): a data-only
    dropdown must count and explain what it hid, and a page capped by
    PAGE_SIZE must say so instead of looking complete.

    And the names-only list (2026-10-06): the picker asks for symbols and
    names without asking the server to measure the catalogue, so its rows
    carry ``data_available: null`` — which is falsy, so a consumer that reads
    the null as "no data" disables the entire dropdown. That trap is what the
    last seven cases exist for.
    """
    result = subprocess.run(
        ["node", str(_REPO_ROOT / "tests" / "js" / "test_symbol_picker.mjs")],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert (
        result.returncode == 0
    ), f"node harness failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "31 tests passed" in result.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_data_manager_row_labels_show_stored_not_derived_timeframes():
    """The Data tab's row label reads "1min · 4,817 bars" — what was downloaded.

    ``timeframes_available`` and ``timeframes_stored`` sit on the same coverage
    row and mean different things: available is the derived set the Backtest
    picker offers (nine entries for any symbol holding 1min data, identical for
    every one of them), stored is what the fetch actually wrote. Printing the
    former put the same long list under every instrument, which is noise rather
    than information. The picker's copy of the same rule is pinned in
    ``test_symbol_picker_and_timeframes_behaviour`` above.
    """
    result = subprocess.run(
        ["node", str(_REPO_ROOT / "tests" / "js" / "test_data_manager_labels.mjs")],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert (
        result.returncode == 0
    ), f"node harness failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "16 tests passed" in result.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_backtest_chart_panes_behaviour():
    """The three result panes must plot the shape the server actually sends.

    ``BacktestAdapter`` returns equity/drawdown as ``{dates, values}`` and signals
    as ``{candles, buys, sells}``. A caller that tests those wrappers for being
    Arrays rejects every payload, and all three panes go blank for every run —
    metrics and trade ledger intact, so nothing else looks wrong. Pins that each
    pane reaches its renderer, and that a series with no points leaves a blank
    canvas rather than the previous run's curve.
    """
    result = subprocess.run(
        ["node", str(_REPO_ROOT / "tests" / "js" / "test_backtest_chart_panes.mjs")],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert (
        result.returncode == 0
    ), f"node harness failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "7 tests passed" in result.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_richer_metric_sections_behaviour():
    """PRD backTest-enhance §2.2: the four expandable metric sections and the
    insufficient-trade-count banner.

    Pins the two things a screenshot cannot: that a metric the server did not
    send renders as NOTHING rather than as a confident 0.00, and that the
    "fewer than 20 closed trades" banner actually appears — and stays away when
    the sample is fine.
    """
    result = subprocess.run(
        ["node", str(_REPO_ROOT / "tests" / "js" / "test_metric_sections.mjs")],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert (
        result.returncode == 0
    ), f"node harness failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "19 tests passed" in result.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_single_run_checks_behaviour():
    """PRD backTest-enhance §3: the benchmark / cost-shock / Monte Carlo panels.

    Pins the three ways a diagnostics panel can lie while looking healthy: an
    absent check rendering as an absent panel, a "Base (0.05%)" column implying
    a frictionless run was costed, and the Monte Carlo's reorder block — whose
    final equity is invariant by construction — presented as a distribution.
    """
    result = subprocess.run(
        ["node", str(_REPO_ROOT / "tests" / "js" / "test_run_checks.mjs")],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert (
        result.returncode == 0
    ), f"node harness failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "28 tests passed" in result.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_cross_broker_analytics_ui_behaviour():
    """PRD-003: the By Broker / Execution Quality sections and their modals.

    The harness pins what a screenshot cannot: that a missing metric renders
    as "—" and never as a fabricated 0.00, that an un-significant comparison
    says so, that a declined fill rate is announced, and that a broker name
    containing markup is escaped before it reaches innerHTML.
    """
    result = subprocess.run(
        ["node", str(_REPO_ROOT / "tests" / "js" / "test_cross_broker.mjs")],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert (
        result.returncode == 0
    ), f"node harness failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "18 tests passed" in result.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_trade_table_is_container_scoped():
    """Two tables on one page must keep their own sort/page state (G13 remnant)."""
    script = """
const { readFileSync } = require("node:fs");
const vm = require("node:vm");
const code = readFileSync("src/backtest/web/static/js/components/trade_table.js", "utf8");
function makeEl(id) {
    const el = { id, innerHTML: "", children: {}, querySelector(sel) {
        this.children[sel] = this.children[sel] || makeEl(id + sel); return this.children[sel]; },
        querySelectorAll() { return []; }, addEventListener() {} };
    return el;
}
const els = {};
const sandbox = { console, document: {
    getElementById: (id) => (els[id] = els[id] || makeEl(id)) } };
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(code + "\\n;globalThis.T = TradeTable;", sandbox);
const rows = (n) => Array.from({ length: n }, (_, i) => ({ id: i + 1, date: "2024-01-0" + (i + 1),
    side: "LONG", entry: 1, exit: 2, pnl: 1, result: "Win", is_open: false }));
sandbox.T.render("a", rows(3));
sandbox.T.render("b", rows(7));
const count = (html) => (html.match(/<tr>/g) || []).length;
console.log(JSON.stringify({ a: count(els["a"].querySelector("tbody").innerHTML),
                            b: count(els["b"].querySelector("tbody").innerHTML) }));
"""
    result = subprocess.run(
        ["node", "-e", script], cwd=_REPO_ROOT, capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    # 3 rows in container "a", 7 in "b" — both under the 20-row page size, so a
    # shared/hardcoded container would have left "a" empty or overwritten.
    assert json.loads(result.stdout.strip()) == {
        "a": 3,
        "b": 7,
    }, f"containers must render independently: {result.stdout}{result.stderr}"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_compare_analytics_panels_behaviour():
    """PRD backTest-enhance §4.1/§4.3/§4.4/§4.5: the Compare tab's analytics.

    Pins the four claims this tab now makes that a screenshot cannot check: a
    correlated pair is explained in words, an "n/a" cell is explained rather
    than left blank, a server verdict of "no significant difference" is not
    upgraded by the renderer, and every equity curve starts at exactly 100.
    """
    result = subprocess.run(
        ["node", str(_REPO_ROOT / "tests" / "js" / "test_compare_panels.mjs")],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert (
        result.returncode == 0
    ), f"node harness failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "24 tests passed" in result.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_compare_controller_sends_the_right_request():
    """PRD backTest-enhance §4.1/§4.2: what the Compare page actually SENDS.

    The API tests prove the server honours a request; these prove the page
    makes one worth honouring. A controller that quietly re-introduced a
    per-slot timeframe, or let generalization send four different parameter
    sets, would leave every server-side test still green.
    """
    result = subprocess.run(
        ["node", str(_REPO_ROOT / "tests" / "js" / "test_compare_controller.mjs")],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert (
        result.returncode == 0
    ), f"node harness failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "14 tests passed" in result.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_certification_readiness_panel_behaviour():
    """PRD backTest-enhance §5: the advisory traffic light.

    Pins the one behaviour that would be dangerous to get wrong: a check that
    could not be evaluated must render neutrally and be named, never folded
    into the pass count. Everything else in the panel is cosmetic by design —
    it makes no decision and blocks nothing.
    """
    result = subprocess.run(
        ["node", str(_REPO_ROOT / "tests" / "js" / "test_certification.mjs")],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert (
        result.returncode == 0
    ), f"node harness failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "14 tests passed" in result.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_regime_and_warning_panel_behaviour():
    """PRD Part 2 §6.1/§6.2. That a short period prints no Sharpe, and that a
    warning cannot be clicked away."""
    result = subprocess.run(
        ["node", str(_REPO_ROOT / "tests" / "js" / "test_regime_warnings.mjs")],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert (
        result.returncode == 0
    ), f"node harness failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "13 tests passed" in result.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_deflated_sharpe_panel_behaviour():
    """PRD Part 2 §3. The maths is pinned in tests/optimization/test_deflation.py;
    this pins that the two statistics stay distinguishable in the UI."""
    result = subprocess.run(
        ["node", str(_REPO_ROOT / "tests" / "js" / "test_deflated_sharpe.mjs")],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert (
        result.returncode == 0
    ), f"node harness failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "11 tests passed" in result.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_data_source_gate_behaviour():
    """Runs 16 checks on the gate; the refusal itself is pinned in
    tests/test_api_data_source_policy.py."""
    result = subprocess.run(
        ["node", str(_REPO_ROOT / "tests" / "js" / "test_data_source_gate.mjs")],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert (
        result.returncode == 0
    ), f"node harness failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "16 tests passed" in result.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_tune_this_prefill_behaviour():
    """PRD backTest-enhance §6: what the hand-off actually carries.

    The one that matters most is the engine. Dropping it means a result screened
    on Quick-Screen gets tuned on the canonical driver — §1.1's bug, one hop
    downstream, with an audit trail showing one continuous lineage.
    """
    result = subprocess.run(
        ["node", str(_REPO_ROOT / "tests" / "js" / "test_tune_this.mjs")],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert (
        result.returncode == 0
    ), f"node harness failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "28 tests passed" in result.stdout
