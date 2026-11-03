"""A read that has not arrived must not look like a measurement (finding F8).

/analytics used to print "0 Active Strategies", "0 brokers" and "0 trades"
before the overview fetch resolved, and kept printing them after it failed —
a slow read, a dead read and a genuinely empty book were three shades of the
same lie. The template now ships every placeholder in the fetching state and
``analytics.js`` / ``cross_broker.js`` walk them through
fetching → ready | empty | unavailable; tests/js/test_analytics_states.mjs
drives those three reads end to end. These tests pin the shipped contract:
what the server renders before a single request has answered, and the CSS
vocabulary that makes each state legible.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "backtest" / "web"
ANALYTICS_HTML = WEB / "templates" / "analytics.html"
WORKSPACE_CSS = WEB / "static" / "css" / "workspace.css"
ANALYTICS_JS = WEB / "static" / "js" / "analytics.js"
CROSS_BROKER_JS = WEB / "static" / "js" / "cross_broker.js"

#: Every placeholder the overview pane ships. Each one must arrive in the
#: fetching state, or the first paint states a number nobody has measured.
FETCHING_PLACEHOLDERS = (
    "activeRunnersPill",
    "crossBrokerScopePill",
    "overviewTotalPnl",
    "overviewSharpeRating",
    "overviewTradeCount",
    "overviewMaxDdAmt",
    "crossBrokerTotalPnlSub",
    "crossBrokerSharpeSub",
    "crossBrokerMaxDdSub",
    "crossBrokerTradesSub",
)

ZERO_AS_FACT = (
    "0 Active Strategies",
    "0 brokers",
    "0 trades",
    "Loading strategies…",
    "No active risk or performance degradation alerts detected.",
)


@pytest.fixture
def app(monkeypatch):
    from backtest.forward.portfolio_manager import (
        get_portfolio_manager,
        reset_portfolio_manager,
    )
    from backtest.web.app import create_app

    monkeypatch.setenv("ALLOW_LIVE_ORDERS", "0")
    monkeypatch.setenv("BACKTEST_DATA_PROFILE", "testing")
    monkeypatch.setenv("FORWARD_TEST_DB_PROFILE", "testing")
    monkeypatch.setenv("FORWARD_TEST_DB_URL", "sqlite:///:memory:")
    reset_portfolio_manager(auto_start_feed=False)
    application = create_app(source="synthetic", log_level="ERROR", TESTING=True)
    yield application
    get_portfolio_manager().shutdown()


def _tag(html: str, element_id: str) -> str:
    match = re.search(rf"<[^>]*id=\"{element_id}\"[^>]*>", html)
    assert match, f"#{element_id} is gone from analytics.html"
    return match.group(0)


def test_the_template_ships_no_zero_as_a_fact():
    html = ANALYTICS_HTML.read_text(encoding="utf-8")
    for needle in ZERO_AS_FACT:
        assert needle not in html, f"{needle!r} states a measurement before any read"


def test_every_placeholder_ships_in_the_fetching_state():
    html = ANALYTICS_HTML.read_text(encoding="utf-8")
    for element_id in FETCHING_PLACEHOLDERS:
        tag = _tag(html, element_id)
        assert 'data-state="fetching"' in tag, f"#{element_id} must ship fetching"


def test_the_equity_box_ships_the_empty_chart_treatment():
    html = ANALYTICS_HTML.read_text(encoding="utf-8")
    box = _tag(html, "portfolioEquityEmpty")
    assert "empty-chart" in box, "the equity void must reuse the designed empty-chart state"
    assert 'data-state="fetching"' in box
    canvas = _tag(html, "portfolioEquityChart")
    assert "hidden" in canvas, "an unpainted canvas must not sit there looking like a flat line"


def test_the_state_vocabulary_is_styled():
    css = WORKSPACE_CSS.read_text(encoding="utf-8")
    for selector in (
        '.state-chip[data-state="fetching"]',
        '.state-chip[data-state="unavailable"]',
        '.state-chip[data-state="empty"]',
        '.state-note[data-state="fetching"]',
        '.state-note[data-state="unavailable"]',
        ".chart-section .empty-chart",
    ):
        assert selector in css, f"{selector} has no rule — the state would be invisible"


def test_the_three_states_are_walked_in_code():
    js = ANALYTICS_JS.read_text(encoding="utf-8")
    assert "markOverviewPending();" in js, "loadOverview must announce the read before awaiting it"
    assert js.count("markOverviewUnavailable(") >= 3, "both failure paths must mark the pane"
    assert "showEquityPlaceholder('empty'" in js, "an empty curve must say why, not draw axes"
    xb = CROSS_BROKER_JS.read_text(encoding="utf-8")
    assert "markXb('fetching'" in xb and "markXb('unavailable'" in xb


def test_the_served_page_starts_in_the_fetching_state(app):
    html = app.test_client().get("/analytics").get_data(as_text=True)
    assert 'id="activeRunnersPill"' in html
    assert 'data-state="fetching"' in _tag(html, "activeRunnersPill")
    for needle in ZERO_AS_FACT:
        assert needle not in html, f"the served page states {needle!r} before any read"
