"""/api/monitor/* endpoints and the /monitor page."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from backtest.forward.paper_runner import RunnerConfig
from backtest.forward.risk_supervisor import GlobalRiskConfig

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def client():
    from backtest.forward.portfolio_manager import get_portfolio_manager, reset_portfolio_manager
    from backtest.web.app import create_app

    mgr = reset_portfolio_manager(
        risk_config=GlobalRiskConfig(daily_loss_limit=100_000, max_drawdown_pct=0.50),
        auto_start_feed=False,
    )
    for name in ("A", "B", "C"):
        mgr.add_runner(RunnerConfig(name=name, strategy_name="buy_and_hold",
                                    allocated_capital=100_000, symbols=["NIFTY"],
                                    target_type="SINGLE_SYMBOL", mode="paper"), start=True)
    base = datetime(2026, 9, 16, 9, 15)
    for i in range(1, 30):
        mgr.tick(ts=base + timedelta(minutes=i))
    app = create_app(source="synthetic")
    with app.test_client() as c:
        yield c
    get_portfolio_manager().shutdown()


def test_snapshot_has_every_section(client):
    body = client.get("/api/monitor/snapshot").get_json()
    assert body["success"]
    snap = body["snapshot"]
    for key in ("summary", "greeks", "concentration", "correlation", "regime", "alerts",
                "alert_counts"):
        assert key in snap
    assert snap["summary"]["strategies"] == 3


@pytest.mark.parametrize("section", ["greeks", "concentration", "correlation", "regime"])
def test_section_endpoints(client, section):
    body = client.get(f"/api/monitor/{section}").get_json()
    assert body["success"] and section in body


def test_invalid_mode_is_400(client):
    res = client.get("/api/monitor/snapshot?mode=bogus")
    assert res.status_code == 400 and res.get_json()["success"] is False


def test_paper_mode_accepted(client):
    assert client.get("/api/monitor/snapshot?mode=paper").get_json()["snapshot"]["mode"] == \
        "paper"


def test_alerts_and_acknowledge(client):
    client.get("/api/monitor/snapshot")  # evaluate once
    body = client.get("/api/monitor/alerts").get_json()
    ackable = [a for a in body["alerts"] if a["severity"] != "info"]
    assert ackable, "3 runners long NIFTY must raise a concentration alert"
    target = ackable[0]["id"]
    res = client.post(f"/api/monitor/alerts/{target}/ack")
    assert res.status_code == 200 and res.get_json()["alert"]["acknowledged"] is True
    assert client.post("/api/monitor/alerts/does-not-exist/ack").status_code == 404


def test_config_endpoint(client):
    cfg = client.get("/api/monitor/config").get_json()
    assert cfg["success"]
    assert cfg["config"]["concentration"]["underlying_pct"] == {"warn": 0.5, "critical": 0.7}


@pytest.mark.parametrize("path", ["/monitor", "/portfolio/greeks"])
def test_page_renders(client, path):
    res = client.get(path)
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert 'id="mon-page"' in html and "js/monitor.js" in html
    assert 'data-active="monitor"' in html


def test_every_element_the_script_touches_exists_in_the_template():
    js = (ROOT / "src/backtest/web/static/js/monitor.js").read_text()
    html = (ROOT / "src/backtest/web/templates/monitor.html").read_text()
    ids = set(re.findall(r'(?:\$|setHTML|chart)\("(mon-[a-z-]+)"', js))
    assert len(ids) > 20
    missing = sorted(i for i in ids if f'id="{i}"' not in html)
    assert not missing, f"monitor.js references ids missing from monitor.html: {missing}"
