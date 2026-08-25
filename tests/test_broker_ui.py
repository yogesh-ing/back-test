"""Broker auth epic, Tasks 3.1 + 3.3 — UI wiring tests.

* Template wiring: the broker status pill and ``broker_status.js`` are on
  every page (nav is shared via ``base.html``).
* Browser logic: ``broker_status.js`` polling/toast behaviour is verified by
  the Node harness in ``tests/js/test_broker_status.mjs`` (run when node is
  available, skipped otherwise).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from backtest.web.app import create_app

_REPO_ROOT = Path(__file__).resolve().parents[1]
_JS_HARNESS = _REPO_ROOT / "tests" / "js" / "test_broker_status.mjs"

_PAGES = ["/", "/backtest", "/dashboard", "/compare", "/forward"]


@pytest.fixture()
def client():
    return create_app(source="synthetic").test_client()


@pytest.mark.parametrize("page", _PAGES)
def test_nav_has_broker_status_pill_on_every_page(client, page):
    html = client.get(page).get_data(as_text=True)
    assert 'id="broker-status"' in html
    assert 'id="broker-status-dot"' in html
    assert 'id="broker-status-name"' in html
    assert "broker_status.js" in html  # script loaded on every page


def test_broker_status_starts_grey_before_first_poll(client):
    html = client.get("/").get_data(as_text=True)
    assert "⚪" in html
    assert 'data-state="unknown"' in html


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_broker_status_js_behaviour():
    """Drive the real polling/toast logic under a stub DOM (Tasks 3.1/3.3)."""
    result = subprocess.run(
        ["node", str(_JS_HARNESS)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"node harness failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "12 tests passed" in result.stdout
