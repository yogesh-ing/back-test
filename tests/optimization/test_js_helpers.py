"""Run the optimize_common.js harness, feeding it backend-generated grid cases.

The setup page's live "N combinations" counter (JS) must agree with
``ParameterSpec.size()`` (Python) — including float steps, where naive
floating-point maths is off by one. Cases are generated here from the Python
implementation and checked by ``tests/js/test_optimize_common.mjs``.
"""

from __future__ import annotations

import json
import random
import shutil
import subprocess
from pathlib import Path

import pytest

from backtest.optimization.config import ParameterSpec, default_space

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HARNESS = _REPO_ROOT / "tests" / "js" / "test_optimize_common.mjs"


def _cases() -> list[list[float]]:
    rng = random.Random(7)
    out: list[list[float]] = []
    for _ in range(150):  # floats with 1–3 decimals
        dp = rng.choice([1, 2, 3])
        step = round(rng.choice([1, 2, 5, 25]) / 10 ** dp, dp)
        lo = round(rng.randint(0, 50) / 10 ** dp, dp)
        hi = round(lo + step * rng.randint(0, 40) + rng.choice([0, step / 2]), dp)
        out.append([lo, hi, step, ParameterSpec("x", "float", lo, hi, step, lo).size()])
    for _ in range(50):  # ints
        lo, step = rng.randint(1, 50), rng.randint(1, 10)
        hi = lo + rng.randint(0, 200)
        out.append([lo, hi, step, ParameterSpec("n", "int", lo, hi, step, lo).size()])
    for strategy in ("sma_crossover", "rsi_reversion", "bollinger_reversion",
                     "directional_options"):
        for row in default_space(strategy):  # what the setup page pre-fills
            spec = ParameterSpec(row["name"], row["type"], row["min"], row["max"], row["step"],
                                 row["current"])
            out.append([row["min"], row["max"], row["step"], spec.size()])
    return out


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_optimize_common_js(tmp_path):
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps(_cases()))
    result = subprocess.run(["node", str(_HARNESS), str(cases)], capture_output=True,
                            text=True, timeout=60, cwd=_REPO_ROOT)
    assert result.returncode == 0, (
        f"node harness failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}")
    assert "optimize_common tests passed" in result.stdout
