"""Presentation contracts for the shared trading workspace.

These checks require no browser or external assets. The optional browser gate
in scripts/check_workspace_ui.py exercises layout, focus and theme changes.
"""
from __future__ import annotations

import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "src" / "backtest" / "web"
PAGES = [
    ("/", "backtest"),
    ("/backtest", "backtest"),
    ("/compare", "compare"),
    ("/optimize", "optimize"),
    ("/optimize/runs/design-check", "optimize"),
    ("/strategy-builder", "strategy_builder"),
    ("/portfolio", "portfolio"),
    ("/portfolio/paper", "portfolio"),
    ("/portfolio/live", "portfolio"),
    ("/analytics", "analytics"),
    ("/risk", "risk"),
    ("/reporting", "reporting"),
    ("/forward", "forward"),
    ("/settings", "settings"),
    ("/data", "data"),
]


class Elements(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.elements = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))

    def by_id(self, ident):
        return next(attrs for _, attrs in self.elements if attrs.get("id") == ident)


@pytest.fixture
def client(monkeypatch):
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
    app = create_app(source="synthetic", log_level="ERROR", TESTING=True)
    with app.test_client() as test_client:
        yield test_client
    get_portfolio_manager().shutdown()


@pytest.mark.parametrize("path,active", PAGES)
def test_every_view_has_consistent_accessible_navigation(client, path, active):
    response = client.get(path)
    assert response.status_code == 200
    parsed = Elements(response.get_data(as_text=True))
    selected = [
        attrs for tag, attrs in parsed.elements
        if tag == "a" and attrs.get("aria-current") == "page"
    ]
    assert len(selected) == 1
    assert selected[0]["data-key"] == active
    assert len([tag for tag, _ in parsed.elements if tag == "h1"]) == 1
    assert parsed.by_id("main-content")["tabindex"] == "-1"
    assert parsed.by_id("sidebar-toggle")["aria-controls"] == "app-sidebar"
    assert parsed.by_id("workspace-command")["aria-label"]
    assert parsed.by_id("theme-toggle")["aria-label"] == "Switch to light theme"
    ids = [attrs["id"] for _, attrs in parsed.elements if attrs.get("id")]
    assert len(ids) == len(set(ids)), "IDs are controller contracts; duplicates are unsafe"


def test_shell_keeps_source_currency_and_safety_mounts(client):
    parsed = Elements(client.get("/").get_data(as_text=True))
    body = next(attrs for tag, attrs in parsed.elements if tag == "body")
    assert body["data-source"] == "synthetic"
    assert body["data-currency-code"] == "INR"
    assert body["data-currency-symbol"] == "₹"
    for ident in [
        "halt-pill", "broker-status", "broker-strip", "risk-strip", "toast-stack",
        "risk-flatten-modal", "risk-flatten-confirm", "broker-auth-overlay",
        "broker-board-overlay", "data-fetch-indicator", "alert-widget",
    ]:
        assert parsed.by_id(ident) is not None
    assert "hidden" in parsed.by_id("data-fetch-indicator")
    assert "hidden" in parsed.by_id("risk-flatten-modal")
    assert parsed.by_id("risk-flatten-modal")["aria-modal"] == "true"


def test_workspace_assets_are_local_and_licensed(client):
    parsed = Elements(client.get("/").get_data(as_text=True))
    paths = [
        attrs["src"] for tag, attrs in parsed.elements
        if tag == "script" and "src" in attrs
    ]
    paths += [
        attrs["href"] for tag, attrs in parsed.elements
        if tag == "link" and attrs.get("rel") in {"stylesheet", "preload", "icon"}
    ]
    assert paths and all(path.startswith("/static/") for path in paths)
    assert any("chart.umd-4.4.1.js" in path for path in paths)
    assert not any("theme-gradientable" in path for path in paths)
    for path in paths:
        assert client.get(path).status_code == 200, path
    for license_path in [
        "fonts/Inter-LICENSE.txt", "fonts/JetBrainsMono-LICENSE.txt",
        "vendor/Chartjs-LICENSE.md",
    ]:
        assert (WEB / "static" / license_path).is_file()


def test_backtest_redesign_preserves_controller_ids_and_empty_values(client):
    html = client.get("/backtest").get_data(as_text=True)
    parsed = Elements(html)
    for ident in [
        "strategy", "symbol", "timeframe", "fromDate", "toDate", "capital",
        "fastPreview", "params-container", "dataSourceGate", "runBtn", "results",
        "emptyState", "metricsCards", "resultProvenance", "metricSections",
        "runChecks", "certification", "tuneThis", "equityChart", "drawdownChart",
        "signalsChart", "tradeTable-wrap", "pagination", "exportCsvBtn",
        "saveCompareBtn", "promoteBtn",
    ]:
        assert parsed.by_id(ident) is not None
    assert 'class="preview-metric-value">—</div>' in html
    assert "Simulation only. No orders are placed." in html
    assert "hidden" in parsed.by_id("results")
    assert "checked" not in parsed.by_id("fastPreview")


@pytest.mark.parametrize("source,visible", [("synthetic", True), ("db", False), ("csv", False)])
def test_generated_instrument_is_explicit_and_never_a_real_market_symbol(client, source, visible):
    from flask import render_template

    with client.application.test_request_context("/backtest"):
        html = render_template("backtest.html", source=source, data_sources={})
    parsed = Elements(html)
    buttons = [attrs for tag, attrs in parsed.elements if attrs.get("id") == "useSyntheticDemo"]
    assert bool(buttons) is visible
    if visible:
        assert buttons[0]["type"] == "button"
        assert "onclick" not in buttons[0]
        assert "Use generated demo instrument" in html
    js = (WEB / "static" / "js" / "backtest.js").read_text()
    demo_handler = js.split('const demoButton = $("useSyntheticDemo");', 1)[1]
    demo_handler = demo_handler.split("\n    try", 1)[0]
    assert 'symbolPicker.setValue("DEMO")' in demo_handler
    assert "not real market prices" in demo_handler
    assert "runBacktest" not in demo_handler
    assert "fetch(" not in demo_handler


def test_chrome_cannot_submit_a_trading_request():
    js = (WEB / "static" / "js" / "workspace.js").read_text()
    assert "fetch(" not in js
    assert "XMLHttpRequest" not in js
    assert "/api/" not in js
    assert '"trading-workspace.theme"' in js
    css = (WEB / "static" / "css" / "app.css").read_text()
    assert "[hidden] { display: none !important; }" in css


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_shared_chart_theme_preserves_financial_data():
    result = subprocess.run(
        ["node", str(ROOT / "tests" / "js" / "test_chart_theme.mjs")],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "10 tests passed" in result.stdout
