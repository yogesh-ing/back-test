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
