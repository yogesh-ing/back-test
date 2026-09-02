"""Ticket P4.1 — landing page + Paper/Live subpages.

Acceptance:
* ``/portfolio`` shows a summary of both buckets.
* ``/portfolio/paper`` = mode 'paper' only; ``/portfolio/live`` = 'live' only.
* No live instances leak onto the paper page or vice-versa.
"""

from __future__ import annotations

import re

import pytest

from backtest.forward.paper_runner import RunnerConfig
from backtest.simulator.bucket_risk import BUCKET_RISK_LIMITS


def _config(name: str, **overrides) -> RunnerConfig:
    base = dict(
        name=name,
        strategy_name="rsi_reversion",
        allocated_capital=100_000,
        symbols=["BTC/USD"],
    )
    base.update(overrides)
    return RunnerConfig(**base)


@pytest.fixture
def client():
    from backtest.forward.portfolio_manager import get_portfolio_manager, reset_portfolio_manager
    from backtest.forward.risk_supervisor import GlobalRiskConfig
    from backtest.web.app import create_app

    reset_portfolio_manager(
        risk_config=GlobalRiskConfig(daily_loss_limit=100_000, max_drawdown_pct=0.50),
        tick_seconds=1.0,
        warmup_bars=15,
        auto_start_feed=False,
    )
    app = create_app(source="synthetic")
    with app.test_client() as c:
        yield c
    get_portfolio_manager().shutdown()


def _spawn(client, name: str, **overrides):
    body = {
        "name": name,
        "strategy": "rsi_reversion",
        "target_type": "SINGLE_SYMBOL",
        "symbol": "BTC/USD",
        "allocated_capital": 100_000,
        "auto_start": False,
    }
    body.update(overrides)
    r = client.post("/api/portfolio/runner/create", json=body)
    assert r.status_code == 201, r.get_json()
    return r.get_json()["instance_id"]


# ---------------------------------------------------------------------------
# Runner config / manager level
# ---------------------------------------------------------------------------


def test_runner_config_mode_source_defaults_and_validation():
    cfg = _config("A")
    assert cfg.mode == "paper" and cfg.source == "synthetic"
    assert _config("B", mode="live", source="mstock").mode == "live"
    with pytest.raises(ValueError):
        _config("C", mode="paper_trading")
    with pytest.raises(ValueError):
        _config("D", source="yfinance")


def test_manager_list_instances_filters_by_mode():
    from backtest.forward.portfolio_manager import PortfolioManager

    mgr = PortfolioManager(auto_start_feed=False)
    mgr.add_runner(_config("PAPER-ONE"), start=False)
    mgr.add_runner(_config("PAPER-TWO"), start=False)
    mgr.add_runner(_config("LIVE-ONE", mode="live", source="mstock"), start=False)

    assert [r["name"] for r in mgr.list_instances()] == ["PAPER-ONE", "PAPER-TWO", "LIVE-ONE"]
    assert [r["name"] for r in mgr.list_instances("paper")] == ["PAPER-ONE", "PAPER-TWO"]
    live = mgr.list_instances("live")
    assert [r["name"] for r in live] == ["LIVE-ONE"]
    assert live[0]["mode"] == "live" and live[0]["source"] == "mstock"
    with pytest.raises(ValueError):
        mgr.list_instances("bogus")
    mgr.shutdown()


def test_summary_scoped_to_bucket():
    from backtest.forward.portfolio_manager import PortfolioManager

    mgr = PortfolioManager(auto_start_feed=False)
    mgr.add_runner(_config("PAPER-ONE"), start=False)
    mgr.add_runner(
        _config("LIVE-ONE", mode="live", source="mstock", allocated_capital=250_000), start=False
    )

    combined = mgr.get_portfolio_summary()
    assert combined["runner_count"] == 2
    paper = mgr.get_portfolio_summary("paper")
    assert paper["runner_count"] == 1
    assert paper["runners"][0]["name"] == "PAPER-ONE"
    assert paper["total_capital"] == 100_000  # bucket allocation, not manager total
    live = mgr.get_portfolio_summary("LIVE")  # case-insensitive
    assert live["runners"][0]["name"] == "LIVE-ONE"
    assert live["total_capital"] == 250_000
    with pytest.raises(ValueError):
        mgr.get_portfolio_summary("bogus")
    mgr.shutdown()


# ---------------------------------------------------------------------------
# API / route level
# ---------------------------------------------------------------------------


def test_summary_endpoint_mode_filter(client):
    _spawn(client, "PAPER-ONE")
    _spawn(client, "LIVE-ONE", mode="live", source="mstock")

    paper = client.get("/api/portfolio/summary?mode=paper").get_json()["portfolio"]
    assert [r["name"] for r in paper["runners"]] == ["PAPER-ONE"]
    live = client.get("/api/portfolio/summary?mode=live").get_json()["portfolio"]
    assert [r["name"] for r in live["runners"]] == ["LIVE-ONE"]
    all_ = client.get("/api/portfolio/summary").get_json()["portfolio"]
    assert all_["runner_count"] == 2
    assert client.get("/api/portfolio/summary?mode=bogus").status_code == 400


def test_landing_page_shows_both_buckets(client):
    _spawn(client, "PAPER-ONE")
    _spawn(client, "LIVE-ONE", mode="live")
    html = client.get("/portfolio").get_data(as_text=True)
    assert "📄 PAPER" in html and "🔴 LIVE" in html
    # Overview now shows Live Command Center (scoped to live) + Paper Sandbox summary.
    assert 'data-mode="live"' in html
    assert "Paper Sandbox" in html


# NOTE: names/ids avoid the substring "live" — CI runs `-k "not live"` to
# skip real-API integration tests, and a substring hit would silently
# deselect this ticket's core no-leak acceptance test.
@pytest.mark.parametrize(
    ("path", "page_mode", "included", "excluded"),
    [
        ("/portfolio/paper", "paper", "PAPER-ONE", "LIVE-ONE"),
        ("/portfolio/live", "live", "LIVE-ONE", "PAPER-ONE"),
    ],
    ids=["paper-bucket-page", "broker-bucket-page"],
)
def test_bucket_pages_do_not_leak(client, path, page_mode, included, excluded):
    _spawn(client, "PAPER-ONE")
    _spawn(client, "LIVE-ONE", mode="live", source="mstock")
    html = client.get(path).get_data(as_text=True)
    assert included in html
    assert excluded not in html  # no cross-bucket leak
    assert f'data-mode="{page_mode}"' in html
    if page_mode == "live":
        assert "LIVE/MSTOCK" in html  # source tag visible on the server table


def test_spawn_accepts_mode_and_source(client):
    _spawn(client, "TAGGED", mode="live", source="mstock")
    row = client.get("/api/portfolio/summary?mode=live").get_json()["portfolio"]["runners"][0]
    assert row["mode"] == "live" and row["source"] == "mstock"
    # invalid mode → 400 (RunnerConfig validation)
    r = client.post(
        "/api/portfolio/runner/create",
        json={
            "strategy": "rsi_reversion",
            "symbol": "BTC/USD",
            "allocated_capital": 100_000,
            "mode": "bogus",
        },
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# P4.2 — mode/source badges
# ---------------------------------------------------------------------------


def test_mode_source_badges_on_bucket_pages(client):
    _spawn(client, "PAPER-SYN")  # defaults: paper / synthetic
    _spawn(client, "PAPER-MST", source="mstock")
    _spawn(client, "LIVE-MST", mode="live", source="mstock")

    paper_html = client.get("/portfolio/paper").get_data(as_text=True)
    assert '<span class="badge badge-paper">PAPER/SYNTH</span>' in paper_html
    assert '<span class="badge badge-paper">PAPER/MSTOCK</span>' in paper_html
    assert "badge-live" not in paper_html  # badge classes match the bucket

    live_html = client.get("/portfolio/live").get_data(as_text=True)
    assert '<span class="badge badge-live">LIVE/MSTOCK</span>' in live_html
    assert "badge-paper" not in live_html

    # the combined command-center matrix carries the Mode/Source column too
    landing = client.get("/portfolio").get_data(as_text=True)
    assert "<th>Mode/Source</th>" in landing


# ---------------------------------------------------------------------------
# Ticket #10 — landing page follows the CANONICAL taxonomy; money labels come
# from the app's currency config (never a hard-coded symbol)
# ---------------------------------------------------------------------------


def test_landing_page_bucket_cards_driven_by_canonical_map(client):
    """The /portfolio bucket cards iterate the backend-owned bucket vocabulary.

    The template must not re-declare the mode list (T3 pattern): it renders
    one card per key of ``BUCKET_RISK_LIMITS`` (injected as ``bucket_modes``),
    so a future bucket appears/disappears in exactly one place — the map.
    """
    html = client.get("/portfolio").get_data(as_text=True)
    rendered = set(re.findall(r'href="/portfolio/([a-z]+)"', html))
    assert rendered == set(BUCKET_RISK_LIMITS)
    for mode in BUCKET_RISK_LIMITS:
        assert f"/portfolio/{mode}" in html


def _usd_client():
    """A second app configured with a NON-default currency (USD)."""
    from backtest.forward.portfolio_manager import reset_portfolio_manager
    from backtest.forward.risk_supervisor import GlobalRiskConfig
    from backtest.web.app import create_app

    reset_portfolio_manager(
        risk_config=GlobalRiskConfig(daily_loss_limit=100_000, max_drawdown_pct=0.50),
        tick_seconds=1.0,
        warmup_bars=15,
        auto_start_feed=False,
    )
    app = create_app(source="synthetic", currency="USD")
    return app


def test_portfolio_and_forward_pages_render_configured_currency_symbol():
    """The server-rendered money labels follow the app's currency config.

    Runs the app with ``currency="USD"`` and asserts the portfolio/forward
    pages print ``$`` (the configured symbol) and contain no hard-coded ``₹``
    anywhere in the HTML — the mirror of the audit finding that Backtest/
    Compare once printed a hard-coded ``$`` on an INR deployment.
    """
    app = _usd_client()
    try:
        with app.test_client() as c:
            for path in ("/portfolio", "/portfolio/paper", "/portfolio/live", "/forward"):
                html = c.get(path).get_data(as_text=True)
                assert "$" in html, f"{path}: configured currency symbol missing"
                assert "₹" not in html, f"{path}: hard-coded INR symbol leaked"
    finally:
        from backtest.forward.portfolio_manager import get_portfolio_manager

        get_portfolio_manager().shutdown()
