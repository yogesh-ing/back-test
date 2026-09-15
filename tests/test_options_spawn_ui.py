"""Options forward testing, task C1 — spawning an option runner from the UI.

The forward engine accepted ``instrument: {"type": "option", ...}`` since the
Gap remediation, but nothing in the UI ever sent it: ``portfolio.js`` built its
payload from the spawn form and omitted ``instrument`` entirely, so an option
runner could only be created with hand-written JSON. Review finding, verbatim:

    Unreachable from the UI — POST /api/portfolio/runner/create accepts
    instrument, but portfolio.js never sends it.

This module covers the three seams of the fix:

1. the payload translation itself (pure, driven by the Node harness in
   ``tests/js/test_option_config.mjs``);
2. the spawn form carries the controls (rendered HTML assertions);
3. the API accepts a payload shaped exactly like the one the browser sends —
   and refuses the combinations the engine cannot honour.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from backtest.web.app import create_app

REPO_ROOT = Path(__file__).resolve().parents[1]
_JS_HARNESS = REPO_ROOT / "tests" / "js" / "test_option_config.mjs"


@pytest.fixture()
def client():
    app = create_app({"TESTING": True})
    return app.test_client()


# ---------------------------------------------------------------------------
# 1. The payload builder (Node harness, same pattern as test_broker_ui.py)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_option_config_js_behaviour():
    """Drive the real payload builder the browser uses."""
    result = subprocess.run(
        ["node", str(_JS_HARNESS)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"node harness failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "29 tests passed" in result.stdout


@pytest.mark.parametrize(
    "url", ["/", "/portfolio", "/portfolio/paper", "/portfolio/live"]
)
def test_option_components_are_loaded_on_every_page(client, url):
    """The payload builder and the book renderer ride along on every page."""
    html = client.get(url).get_data(as_text=True)
    assert "js/components/option_config.js" in html, f"{url} does not load the builder"
    assert "js/components/option_view.js" in html, f"{url} does not load the renderer"


# ---------------------------------------------------------------------------
# 2. The spawn form
# ---------------------------------------------------------------------------


class TestSpawnForm:
    @pytest.fixture()
    def modal_html(self, client):
        """The spawn modal as the paper portfolio page renders it."""
        html = client.get("/portfolio/paper").get_data(as_text=True)
        assert "spawn-instrument-type" in html, "spawn modal did not render"
        return html

    def test_instrument_selector_offers_equity_and_options(self, modal_html):
        assert 'id="spawn-instrument-type"' in modal_html
        assert '<option value="equity"' in modal_html
        assert '<option value="option"' in modal_html

    def test_option_controls_are_present(self, modal_html):
        for field in (
            "spawn-option-box",
            "spawn-opt-structure",
            "spawn-opt-strike",
            "spawn-opt-delta",
            "spawn-opt-qty",
        ):
            assert f'id="{field}"' in modal_html, f"{field} missing from the spawn form"

    def test_exit_rule_controls_are_present(self, modal_html):
        for field in (
            "spawn-opt-stop",
            "spawn-opt-target",
            "spawn-opt-neutral",
            "spawn-opt-maxbars",
            "spawn-opt-dte",
            "spawn-opt-flip",
            "spawn-opt-settle",
            "spawn-opt-reenter",
        ):
            assert f'id="{field}"' in modal_html, f"{field} missing from the spawn form"

    def test_option_block_starts_hidden(self, modal_html):
        """Equity stays the default; the option panel only appears on demand."""
        assert 'id="spawn-option-box" hidden' in modal_html
        assert 'value="equity" selected' in modal_html

    def test_js_submits_the_instrument_block(self):
        js = (REPO_ROOT / "src/backtest/web/static/js/portfolio.js").read_text()
        assert "OptionConfig.buildInstrument" in js
        assert "body.instrument" in js
        assert "OptionConfig.validate" in js

    def test_js_binds_the_option_controls(self):
        js = (REPO_ROOT / "src/backtest/web/static/js/portfolio.js").read_text()
        assert 'instrumentSel.addEventListener("change", syncOptionForm)' in js
        assert "syncOptionForm()" in js


# ---------------------------------------------------------------------------
# 3. The API accepts what the browser sends
# ---------------------------------------------------------------------------


def _browser_payload(**overrides):
    """The exact shape `portfolio.js` posts for an option runner (C1)."""
    payload = {
        "name": "NIFTY-BCS",
        "strategy": "directional_options",
        "allocated_capital": 1_000_000,
        "timeframe": "1day",
        "mode": "paper",
        "source": "synthetic",
        "target_type": "SINGLE_SYMBOL",
        "symbol": "NIFTY",
        "params": {},
        "instrument": {
            "type": "option",
            "expression": {
                "type": {"BULLISH": "bull_call_spread", "BEARISH": "bear_put_spread"},
                "strike_selection": "atm",
                "quantity": 1,
                "exit": {
                    "min_days_to_expiry": 1,
                    "stop_loss_pct": 0.4,
                    "take_profit_pct": 0.8,
                    "neutral_bars": 2,
                    "reenter": True,
                },
            },
        },
    }
    payload.update(overrides)
    return payload


class TestCreateRunnerApi:
    def test_equity_payload_without_instrument_still_works(self, client):
        response = client.post(
            "/api/portfolio/runner/create",
            json={
                "name": "eq",
                "strategy": "sma_crossover",
                "allocated_capital": 100_000,
                "symbol": "RELIANCE",
                "timeframe": "1day",
            },
        )
        assert response.status_code == 201
        runner = response.get_json()["runner"]
        assert runner["instrument"] == {"type": "equity"}
        assert runner["options"] is None

    def test_option_payload_spawns_an_option_runner(self, client):
        response = client.post("/api/portfolio/runner/create", json=_browser_payload())
        assert response.status_code == 201, response.get_json()

        runner = response.get_json()["runner"]
        assert runner["instrument"]["type"] == "option"
        assert runner["options"] is not None
        assert runner["options"]["open_structures"] == 0
        # The exit policy the form sent is what the runner will apply.
        policy = runner["options"]["exit_policy"]
        assert policy["min_days_to_expiry"] == 1
        assert policy["stop_loss_pct"] == 0.4
        assert policy["take_profit_pct"] == 0.8
        assert policy["neutral_bars"] == 2
        assert policy["reenter"] is True

    def test_optional_exit_keys_default_inside_the_engine(self, client):
        """A minimal option payload is still deployable."""
        payload = _browser_payload()
        payload["instrument"]["expression"].pop("exit")
        response = client.post("/api/portfolio/runner/create", json=payload)
        assert response.status_code == 201
        policy = response.get_json()["runner"]["options"]["exit_policy"]
        assert policy["signal_flip"] is True
        assert policy["min_days_to_expiry"] == 1  # shipped default

    def test_ride_into_settlement_via_explicit_null(self, client):
        payload = _browser_payload()
        payload["instrument"]["expression"]["exit"] = {"min_days_to_expiry": None}
        response = client.post("/api/portfolio/runner/create", json=payload)
        assert response.status_code == 201
        policy = response.get_json()["runner"]["options"]["exit_policy"]
        assert policy["min_days_to_expiry"] is None

    def test_pool_option_runner_is_refused_with_a_reason(self, client):
        payload = _browser_payload(
            target_type="SYMBOL_UNIVERSE",
            universe_id="NIFTY_50",
            symbol=None,
        )
        payload.pop("symbol")
        response = client.post("/api/portfolio/runner/create", json=payload)

        assert response.status_code == 400
        error = response.get_json()["error"]
        assert "single underlying" in error

    def test_unknown_instrument_type_is_still_rejected(self, client):
        response = client.post(
            "/api/portfolio/runner/create",
            json=_browser_payload(instrument={"type": "crypto"}),
        )
        assert response.status_code == 400
        assert "instrument.type" in response.get_json()["error"]

    def test_payload_survives_json_round_trip(self, client):
        """The JS POST is JSON — make sure nothing depends on Python objects."""
        raw = json.dumps(_browser_payload())
        response = client.post(
            "/api/portfolio/runner/create",
            data=raw,
            content_type="application/json",
        )
        assert response.status_code == 201
