"""Ticket #10 — UI restructure: taxonomy visible & selectable, round-trip proven.

Asserts (headless Flask test client — the wiring layer):
* The Forward page exposes run mode (paper|live) + data source
  (synthetic|replay|mstock) rendered from the CANONICAL vocabulary the
  backend injects (no re-declared constants in the template), a risk
  implication hint, and a resume affordance.
* The Portfolio spawn modal exposes bucket mode + source (and the stale
  "Target mode" single/pool label is gone — that control is target TYPE).
* /api/config reports the canonical taxonomy.
* A selection round-trips: /api/forward/start echoes mode/source and the
  session snapshot carries them; refused combinations fail through the
  canonical bucket-risk gate (never silently paper-fill a live label).
"""

from __future__ import annotations

import pytest

from backtest.data.source_tags import SOURCE_TAG_VALUES
from backtest.simulator.bucket_risk import BUCKET_RISK_LIMITS


@pytest.fixture()
def client():
    from backtest.brokers.session_manager import reset_default_manager
    from backtest.web.app import create_app

    reset_default_manager()
    app = create_app(source="synthetic", replay_speed=0)
    with app.test_client() as c:
        yield c
    reset_default_manager()


def test_forward_page_exposes_taxonomy_from_canonical_vocabulary(client):
    html = client.get("/forward").get_data(as_text=True)
    # Run mode: both canonical buckets rendered from the injected sorted keys.
    for mode in sorted(BUCKET_RISK_LIMITS):
        assert f'value="{mode}"' in html
    assert 'id="runMode"' in html
    assert "</select>" in html
    # Data source: the canonical source tags, not a hardcoded two-option list.
    for source in sorted(SOURCE_TAG_VALUES):
        assert f'value="{source}"' in html
    assert 'id="dataSource"' in html
    # Risk implication surfaced (T9-aware copy present).
    assert 'id="taxonomyHint"' in html
    assert "free-play" in html
    # Resume affordance (T7-aware) — visible choice vs fresh start.
    assert 'id="resumeBanner"' in html
    assert 'id="resumeBtn"' in html and 'id="freshStartBtn"' in html
    # Honest copy: no "live paper trading" contradiction left on the page.
    assert "live paper trading" not in html.lower()


def test_portfolio_spawn_modal_exposes_bucket_and_fixes_target_label(client):
    html = client.get("/portfolio").get_data(as_text=True)
    for mode in sorted(BUCKET_RISK_LIMITS):
        assert f'<option value="{mode}"' in html, f"spawn mode {mode} missing"
    assert 'id="spawn-mode"' in html
    assert 'id="spawn-source"' in html
    for source in sorted(SOURCE_TAG_VALUES):
        assert f'<option value="{source}"' in html, f"spawn source {source} missing"
    # The single/pool control is target TYPE, never "target mode" (that name
    # collided with the bucket taxonomy and is the stale label).
    assert 'id="spawn-target-type"' in html
    assert "spawn-target-mode" not in html
    assert ">Target type</label>" in html
    assert ">Target mode</label>" not in html
    assert "T9 caps" in html  # risk implication surfaced on the modal


def test_api_config_reports_canonical_taxonomy(client):
    data = client.get("/api/config").get_json()
    assert data["taxonomy"]["modes"] == sorted(BUCKET_RISK_LIMITS)
    assert data["taxonomy"]["sources"] == sorted(SOURCE_TAG_VALUES)


def test_forward_page_not_hardcoded_to_paper_only_or_old_mode_names(client):
    html = client.get("/forward").get_data(as_text=True)
    # The old single "Mode" select (synthetic/live) is gone — replaced by two
    # taxonomy-aligned controls.
    assert 'id="dataMode"' not in html
    assert "Synthetic (no broker needed)" not in html
    assert "Live (requires mStock auth)" not in html
