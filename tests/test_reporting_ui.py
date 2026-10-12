"""Node harness for the Consolidated P&L page (reporting.js).

Same pattern as ``tests/test_web_components.py``: drive the real file under
``web/static/js/`` in a stub DOM and skip when node is unavailable. The view
models are pinned in JS because they are where "estimated", "excluded" and
"carried forward" become visible to the person reading the number.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_HARNESS = _REPO_ROOT / "tests" / "js" / "test_reporting.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_reporting_page_view_models():
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
    assert "13 tests passed" in result.stdout
