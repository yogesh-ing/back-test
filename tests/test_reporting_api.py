"""Reporting API + page (PRD-002).

Drives the real Flask app with the labelled demo book so the numbers are
deterministic without a database: a missing database must produce an empty
report with a warning, never a 500, while a malformed *input* must produce a
400 — silently guessing an input to a tax report is how wrong numbers get
filed.
"""

from __future__ import annotations

import email
import io
import json
import textwrap
import zipfile
from email import policy

import pytest

from backtest.web.app import create_app

YAML = textwrap.dedent(
    """
    tax:
      stcg_rate: 0.20
      ltcg_rate: 0.125
      ltcg_exemption: 125000
      business_slab_rate: 0.30
      cess_rate: 0.04
    reconciliation:
      pass_tolerance: 10
      warn_tolerance: 100
      warn_pct: 1.0
    monthly_email:
      enabled: false
      to_email: trader@example.com
    smtp:
      host: ""
      port: 587
      outbox_dir: {outbox}
    exports:
      include_trades: true
      max_trades: 1000
    """
)


@pytest.fixture()
def api(tmp_path, monkeypatch):
    config = tmp_path / "reporting.yaml"
    config.write_text(YAML.format(outbox=tmp_path / "outbox"), encoding="utf-8")
    monkeypatch.setenv("REPORTING_CONFIG_PATH", str(config))
    monkeypatch.delenv("REPORTING_DEMO_TRADES", raising=False)
    monkeypatch.delenv("REPORTING_EMAIL_TO", raising=False)
    app = create_app(source="synthetic")
    app.config.update(TESTING=True)
    return app.test_client()


DEMO = {"demo": True, "from_date": "2026-04-01", "to_date": "2026-09-30"}


def _json(response):
    return response.get_json()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_config_endpoint_exposes_rules_without_secrets(api):
    body = _json(api.get("/api/reporting/config"))
    assert body["success"] is True
    assert body["tax"]["stcg_rate"] == "0.2"
    assert body["tax"]["ltcg_exemption"] == "125000"
    assert body["reconciliation"]["pass_tolerance"] == 10.0
    assert body["email"]["can_send"] is False
    assert "password" not in json.dumps(body)
    assert "chartered accountant" in body["disclaimer"]


# ---------------------------------------------------------------------------
# Consolidated P&L
# ---------------------------------------------------------------------------


def test_consolidated_pnl_for_the_demo_book(api):
    response = api.get("/api/reporting/pnl/consolidated", query_string={**DEMO, "demo": "1"})
    body = _json(response)
    assert response.status_code == 200
    assert body["success"] is True
    assert body["demo"] is True
    assert body["period"]["start"] == "2026-04-01"
    # The demo book is deterministic; these numbers are its contract.
    assert body["summary"]["gross_pnl"] == pytest.approx(7619.50)
    assert body["summary"]["net_pnl"] == pytest.approx(7139.25)
    assert body["summary"]["tax"] == pytest.approx(357.85)
    assert body["summary"]["net_after_tax"] == pytest.approx(6781.40)
    assert body["stats"]["trade_count"] == 9
    assert {row["broker"] for row in body["by_broker"]} >= {"mstock", "dhan"}
    assert any(row["category"] == "PAPER_EXCLUDED" for row in body["by_category"])
    assert body["trades"][0]["tax_category_label"]


def test_report_says_which_sources_spoke(api):
    body = _json(api.get("/api/reporting/pnl/consolidated", query_string={**DEMO, "demo": "1"}))
    names = {source["name"] for source in body["sources"]}
    assert "demo" in names
    # A database that does not exist yet is a note, not an error.
    assert body["warnings"]
    assert any("SIMULATED" in w or "demo" in w.lower() for w in body["warnings"])


def test_include_paper_false_drops_the_simulated_book(api):
    live_only = _json(
        api.get(
            "/api/reporting/pnl/consolidated",
            query_string={**DEMO, "demo": "1", "include_paper": "false"},
        )
    )
    assert live_only["stats"]["paper_net_pnl"] == 0.0
    assert all(not trade["mode"] == "paper" for trade in live_only["trades"])
    assert live_only["stats"]["trade_count"] < 9


def test_broker_filter_and_trade_omission(api):
    body = _json(
        api.get(
            "/api/reporting/pnl/consolidated",
            query_string={**DEMO, "demo": "1", "brokers": "dhan", "include_trades": "false"},
        )
    )
    assert {row["broker"] for row in body["by_broker"]} == {"dhan"}
    assert "trades" not in body


def test_bad_dates_are_rejected_not_guessed(api):
    for query in (
        {"from_date": "not-a-date", "to_date": "2026-09-30"},
        {"from_date": "2026-09-30", "to_date": "2026-04-01"},
    ):
        response = api.get("/api/reporting/pnl/consolidated", query_string=query)
        assert response.status_code == 400
        assert response.get_json()["success"] is False
        assert "period" in response.get_json()["error"]


def test_an_empty_book_returns_a_report_not_an_error(api):
    body = _json(
        api.get(
            "/api/reporting/pnl/consolidated",
            query_string={"from_date": "2020-01-01", "to_date": "2020-01-31"},
        )
    )
    assert body["success"] is True
    assert body["summary"]["net_pnl"] == 0.0
    assert body["stats"]["trade_count"] == 0


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------


def test_pdf_export_downloads_a_pdf(api):
    response = api.post("/api/reporting/pnl/export/pdf", json=DEMO)
    assert response.status_code == 200
    assert response.mimetype == "application/pdf"
    assert response.data.startswith(b"%PDF")
    disposition = response.headers["Content-Disposition"]
    assert "consolidated_pnl_2026-04-01_2026-09-30.pdf" in disposition


def test_itr_export_downloads_a_valid_workbook(api):
    response = api.post("/api/reporting/pnl/export/itr", json={**DEMO, "format": "xlsx"})
    assert response.status_code == 200
    assert "spreadsheetml" in response.mimetype
    archive = zipfile.ZipFile(io.BytesIO(response.data))
    assert "xl/workbook.xml" in archive.namelist()
    workbook_xml = archive.read("xl/workbook.xml").decode("utf-8")
    assert "Capital gains" in workbook_xml and "Filing guidance" in workbook_xml


def test_itr_export_can_be_json_for_the_ui(api):
    response = api.post(
        "/api/reporting/pnl/export/itr",
        json={**DEMO, "format": "json", "annexures": ["stt", "guidance"]},
    )
    body = _json(response)
    assert response.status_code == 200
    assert [sheet["title"] for sheet in body["sheets"]] == [
        "STT summary",
        "Filing guidance",
    ]
    assert "chartered accountant" in body["disclaimer"]


def test_itr_export_csv_packs_one_file_per_annexure(api):
    response = api.post("/api/reporting/pnl/export/itr", json={**DEMO, "format": "csv"})
    assert response.status_code == 200
    assert response.mimetype == "application/zip"
    archive = zipfile.ZipFile(io.BytesIO(response.data))
    names = archive.namelist()
    assert len(names) == 5
    first = archive.read(names[0]).decode("utf-8")
    assert "Schedule CG" in first


def test_unsupported_export_format_is_a_400(api):
    response = api.post("/api/reporting/pnl/export/itr", json={**DEMO, "format": "docx"})
    assert response.status_code == 400
    assert "unsupported format" in response.get_json()["error"]


def test_trade_ledger_csv_is_complete(api):
    response = api.post("/api/reporting/pnl/export/trades", json=DEMO)
    assert response.status_code == 200
    assert response.mimetype == "text/csv"
    lines = response.data.decode("utf-8").strip().splitlines()
    assert lines[0].startswith("trade_id,broker,mode,segment,symbol")
    rows = [line for line in lines if not line.startswith("#")]
    assert len(rows) == 10  # header + the demo book's nine trades
    assert "PAPER_EXCLUDED" in response.data.decode("utf-8")
    # Caveats travel with the file, as comments a spreadsheet is fine with.
    assert any(line.startswith("#") for line in lines)


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


def test_reconcile_an_exact_match(api):
    body = _json(
        api.post(
            "/api/reporting/pnl/reconcile",
            json={**DEMO, "broker": "broker_c", "contract_note_pnl": 3962.00},
        )
    )
    assert body["success"] is True
    assert body["status"] == "PASS"
    assert body["difference"] == 0.0
    assert body["platform_summary"]["net_pnl"] == pytest.approx(7139.25)


def test_reconcile_reports_a_material_difference_with_causes(api):
    body = _json(
        api.post(
            "/api/reporting/pnl/reconcile",
            json={**DEMO, "broker": "broker_c", "contract_note_pnl": 2500.00},
        )
    )
    assert body["status"] == "FAIL"
    assert body["difference"] == pytest.approx(1462.00)
    assert body["likely_causes"]


def test_reconcile_unknown_broker_fails_with_a_reason(api):
    body = _json(
        api.post(
            "/api/reporting/pnl/reconcile",
            json={**DEMO, "broker": "no-such-broker", "contract_note_pnl": 100},
        )
    )
    assert body["status"] == "FAIL"
    assert "no platform trades" in body["likely_causes"][0]


def test_reconcile_requires_the_note_figure(api):
    no_note = api.post("/api/reporting/pnl/reconcile", json={**DEMO, "broker": "dhan"})
    assert no_note.status_code == 400
    assert api.post("/api/reporting/pnl/reconcile", json=DEMO).status_code == 400


def test_reconcile_several_brokers_will_not_guess_a_missing_note(api):
    response = api.post(
        "/api/reporting/pnl/reconcile",
        json={**DEMO, "notes": {"dhan": 1581.22, "mstock": {"note_ref": "CN-1"}}},
    )
    assert response.status_code == 400
    assert "contract_note_pnl" in response.get_json()["error"]


def test_reconcile_several_brokers_at_once(api):
    body = _json(
        api.post(
            "/api/reporting/pnl/reconcile",
            json={**DEMO, "notes": {"dhan": 1581.22, "broker_c": {"pnl": 3962.00}}},
        )
    )
    assert [row["status"] for row in body["results"]] == ["PASS", "PASS"]


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------


def test_email_endpoint_dry_runs_by_default(api, tmp_path):
    body = _json(api.post("/api/reporting/email", json=DEMO))
    assert body["success"] is True
    assert body["sent"] is False
    assert body["dry_run"] is True
    path = tmp_path / "outbox"
    assert path.exists() and list(path.glob("*.eml"))
    message = email.message_from_bytes(
        next(path.glob("*.eml")).read_bytes(), policy=policy.default
    )
    assert list(message.iter_attachments())[0].get_filename().endswith(".pdf")


def test_email_endpoint_falls_back_to_the_configured_recipient(api):
    body = _json(api.post("/api/reporting/email", json={**DEMO, "to_email": ""}))
    assert body["success"] is True
    assert body["sent"] is False
    assert body["to"] == "trader@example.com"


def test_email_endpoint_reports_a_missing_recipient(tmp_path, monkeypatch):
    config = tmp_path / "reporting.yaml"
    config.write_text(
        "monthly_email:\n  enabled: false\n  to_email: \"\"\n", encoding="utf-8"
    )
    monkeypatch.setenv("REPORTING_CONFIG_PATH", str(config))
    monkeypatch.delenv("REPORTING_EMAIL_TO", raising=False)
    client = create_app(source="synthetic").test_client()
    body = _json(client.post("/api/reporting/email", json=DEMO))
    assert body["success"] is True
    assert body["sent"] is False
    assert body["error"] and "recipient" in body["error"]


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------


def test_reporting_page_renders_with_the_expected_hooks(api):
    html = api.get("/reporting").get_data(as_text=True)
    assert "Consolidated P&amp;L" in html
    assert "js/reporting.js" in html
    assert 'id="rp-from"' in html and 'id="rp-tax-block"' in html
    assert 'href="/reporting"' in html  # the nav entry itself


def test_page_links_are_present_on_every_page(api):
    for route in ("/", "/portfolio"):
        assert 'href="/reporting"' in api.get(route).get_data(as_text=True)
