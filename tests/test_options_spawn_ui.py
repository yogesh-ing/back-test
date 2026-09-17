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
import sys
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
        encoding="utf-8",  # ₹/→ in JS output; Windows cp1252 cannot decode them
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
    """U6.1 — the spawn form is a 7-field routing contract.

    Trading logic (structure, strikes, exit rules) lives in Playbooks, not
    here. The instrument is derived from the strategy's signal_kind and is
    never asked; option strategies lock the target type to Single Symbol and
    restrict the symbol to index underlyings.
    """

    @pytest.fixture()
    def modal_html(self, client):
        """The spawn modal as the paper portfolio page renders it."""
        html = client.get("/portfolio/paper").get_data(as_text=True)
        assert "spawn-strategy" in html, "spawn modal did not render"
        return html

    def test_form_has_exactly_the_routing_fields(self, modal_html):
        for field in (
            "spawn-name",
            "spawn-strategy",
            "spawn-playbook",
            "spawn-target-type",
            "spawn-timeframe",
            "spawn-mode",
            "spawn-source",
            "spawn-symbol",
            "spawn-capital",
        ):
            assert f'id="{field}"' in modal_html, f"{field} missing from the spawn form"

    def test_form_has_no_trading_logic_controls(self, modal_html):
        """Instrument/structure/strike/exit controls are Playbook territory."""
        for gone in (
            "spawn-instrument-type",
            "spawn-option-box",
            "spawn-opt-structure",
            "spawn-opt-strike",
            "spawn-opt-delta",
            "spawn-opt-qty",
            "spawn-opt-stop",
            "spawn-opt-target",
            "spawn-opt-neutral",
            "spawn-opt-maxbars",
            "spawn-opt-dte",
            "spawn-opt-flip",
            "spawn-opt-settle",
            "spawn-opt-reenter",
        ):
            assert f'id="{gone}"' not in modal_html, (
                f"{gone} is Playbook territory — must not be on the spawn form"
            )

    def test_index_datalist_is_bound_by_js(self):
        """Option symbols come from the index datalist the JS injects."""
        js = (REPO_ROOT / "src/backtest/web/static/js/portfolio.js").read_text(
            encoding="utf-8"
        )
        assert 'spawn-index-list' in js
        assert "OPTION_INDEXES" in js
        assert "NIFTY" in js and "BANKNIFTY" in js

    def test_js_locks_target_type_and_symbol_by_signal_kind(self):
        js = (REPO_ROOT / "src/backtest/web/static/js/portfolio.js").read_text(
            encoding="utf-8"
        )
        assert "signal_kind" in js
        assert "poolOpt.disabled = isOption" in js
        assert "spawn-playbook" in js

    def test_api_exposes_signal_kind(self, client):
        """GET /api/strategies carries signal_kind (option vs equity)."""
        catalogue = client.get("/api/strategies").get_json()
        kinds = {s["name"]: s["signal_kind"] for s in catalogue}
        assert kinds["directional_options"] == "option"
        assert kinds["sma_crossover"] == "equity"

    def test_js_submits_the_instrument_block(self):
        js = (REPO_ROOT / "src/backtest/web/static/js/portfolio.js").read_text(
            encoding="utf-8"
        )
        # Option routing: playbook snapshot or engine defaults — never form fields.
        assert "body.instrument" in js
        assert "/api/playbooks/" in js
        assert "bull_call_spread" in js  # engine-default expression


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
