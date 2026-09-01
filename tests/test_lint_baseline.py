"""Lint baseline guard (ticket #11 / F-10).

Runs the canonical flake8 invocation against src/ inside the normal pytest
gate, so the baseline cannot silently rot (the F-18 lesson: a gate that
quietly stops running is not a gate). Skips honestly if flake8 is absent.
"""

import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("flake8", reason="lint tooling not installed")
REPO_ROOT = Path(__file__).resolve().parents[1]


def test_flake8_baseline_is_clean():
    proc = subprocess.run(
        [sys.executable, "-m", "flake8", "src/"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"flake8 findings regressed:\n{proc.stdout}"
