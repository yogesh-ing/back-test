"""Broker auth epic, Tasks 3.1 + 3.2 + 3.3 — UI wiring tests.

* Template wiring: the broker status pill and ``broker_status.js`` are on
  every page (nav is shared via ``base.html``).
* Template wiring: the auth modal overlay is on every page via the
  ``components/broker_auth_modal.html`` include and its JS is loaded too.
* Browser logic: ``broker_status.js`` polling/toast behaviour is verified by
  the Node harness in ``tests/js/test_broker_status.mjs`` (run when node is
  available, skipped otherwise).
* Browser logic: ``broker_auth_modal.js`` view transitions, error paths,
  password clearing, logout wiring — verified by
  ``tests/js/test_broker_auth_modal.mjs``.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from backtest.web.app import create_app

_REPO_ROOT = Path(__file__).resolve().parents[1]
_JS_HARNESS = _REPO_ROOT / "tests" / "js" / "test_broker_status.mjs"
_MODAL_HARNESS = _REPO_ROOT / "tests" / "js" / "test_broker_auth_modal.mjs"

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


@pytest.mark.parametrize("page", _PAGES)
def test_auth_modal_overlay_on_every_page(client, page):
    """Task 3.2 — the auth popup is included on every page via base.html."""
    html = client.get(page).get_data(as_text=True)
    assert 'id="broker-auth-overlay"' in html
    assert "broker_auth_modal.js" in html
    assert "broker_auth_modal.html" not in html  # template is rendered, not visible as text
    # three views present in markup
    assert 'id="broker-auth-step-credentials"' in html
    assert 'id="broker-auth-step-totp"' in html
    assert 'id="broker-auth-step-authenticated"' in html
    # key interactive elements
    assert 'id="broker-auth-login-btn"' in html
    assert 'id="broker-auth-totp-btn"' in html
    assert 'id="broker-auth-logout-btn"' in html
    assert 'id="broker-auth-close"' in html


def test_forward_page_loads_forward_js_with_auth_gate(client):
    """Task 4.1 — forward.html loads forward.js which contains the auth gate."""
    html = client.get("/forward").get_data(as_text=True)
    assert "forward.js" in html
    assert 'id="startBtn"' in html


def test_auth_modal_views_hidden_by_default(client):
    """Step 1 (credentials) is shown; step 2 and 3 are hidden."""
    html = client.get("/").get_data(as_text=True)
    # step-totp and step-authenticated are hidden
    assert 'id="broker-auth-step-totp" class="broker-auth-view" hidden' in html
    assert 'id="broker-auth-step-authenticated" class="broker-auth-view" hidden' in html


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


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_broker_auth_modal_js_behaviour():
    """Drive the real modal view transitions under a stub DOM (Task 3.2)."""
    result = subprocess.run(
        ["node", str(_MODAL_HARNESS)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"node harness failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "14 tests passed" in result.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_forward_auth_gate_js_behaviour():
    """Drive the forward.js Start-button auth gate under a stub DOM (Task 4.1)."""
    gate_harness = _REPO_ROOT / "tests" / "js" / "test_forward_auth_gate.mjs"
    result = subprocess.run(
        ["node", str(gate_harness)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"node harness failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "12 tests passed" in result.stdout
