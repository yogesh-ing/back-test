"""Cost & Risk Settings panel — Phases 1+2 (certified design, 2026-09-28).

Phase 1 coverage:

* broker-profile CRUD with a per-field audit trail (who/when/old→new);
* negative-rate refusal (fail closed);
* preset seeding from BROKER_PRESETS (yaml bootstrap semantics);
* the contract-note validator endpoint — PASS stamps, FAIL returns
  per-component mismatches and stamps nothing;
* the active-broker selector and DB-first ``load_broker_profile`` resolution;
* the profile rebuilt from a DB row prices identically to the preset it
  overrides (the string-rate coercion regression).

Phase 2 coverage (certified v2 §0 #2/#4/#6):

* segment CRUD with risk-limit validation + per-field audit;
* the global live kill-switch — default OFF, audited when toggled;
* the two-tier live-arming gate: kill-switch ON + segment mode=live +
  positive daily_loss_limit + contract-note-validated broker, fail closed;
* graceful degradation: with no settings DB configured the gate defers to
  the legacy ALLOW_LIVE_ORDERS env behavior (panel-not-configured ≠ blocked).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from backtest.api.broker_profiles_store import BrokerProfileStore
from backtest.db.models import BrokerProfileRow
from backtest.simulator.fees import BrokerProfile, CommissionCalculator, load_broker_profile

_REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """A store on a throwaway SQLite DB; resets the process-wide singleton."""
    from backtest.db import DatabaseManager

    monkeypatch.setenv("FORWARD_TEST_DB_URL", f"sqlite+pysqlite:///{tmp_path}/settings.db")
    monkeypatch.setattr("backtest.api.broker_profiles_store._STORE", None)
    monkeypatch.setattr("backtest.api.segments_store._STORE", None)
    manager = DatabaseManager.from_env(url=f"sqlite+pysqlite:///{tmp_path}/settings.db")
    manager.connect()
    yield BrokerProfileStore(manager)
    manager.disconnect()


@pytest.fixture()
def client(store, monkeypatch):
    from backtest.web.app import create_app

    app = create_app()
    with app.test_client() as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# Store: CRUD + audit
# ---------------------------------------------------------------------------


class TestStoreCrud:
    def test_seed_creates_all_presets_exactly_once(self, store):
        first = store.seed_from_yaml()
        second = store.seed_from_yaml()
        assert first > 0
        assert second == 0, "reseeding must be idempotent"
        names = {p["profile_id"] for p in store.list_profiles()}
        assert {"zerodha", "mstock", "ibkr"} <= names

    def test_upsert_create_then_update_audits_each_change(self, store):
        store.upsert_profile(
            {
                "profile_id": "test_broker",
                "profile_name": "Test Broker",
                "statutory_rates": {"stt_delivery": "0.001", "gst_rate": "0.18"},
            }
        )
        store.upsert_profile(
            {"profile_id": "test_broker", "statutory_rates": {"stt_delivery": "0.00125"}}
        )
        audit = store.get_audit("test_broker")
        fields = [a["field_changed"] for a in audit]
        assert fields[0] == "statutory_rates", "most recent change first"
        assert "__created__" in fields
        rate_change = next(a for a in audit if a["field_changed"] == "statutory_rates")
        assert "0.00125" in rate_change["new_value"]
        assert "0.001" in rate_change["old_value"]

    def test_negative_rate_refused_fail_closed(self, store):
        with pytest.raises(ValueError, match="must not be negative"):
            store.upsert_profile(
                {"profile_id": "bad", "statutory_rates": {"stt_delivery": "-0.5"}}
            )

    def test_unknown_statutory_rate_refused(self, store):
        with pytest.raises(ValueError, match="unknown statutory rate"):
            store.upsert_profile({"profile_id": "bad", "statutory_rates": {"stt_bogus": "1"}})

    def test_active_broker_roundtrip_and_unknown_refused(self, store):
        assert store.get_active_broker() is None
        with pytest.raises(ValueError, match="unknown profile"):
            store.set_active_broker("nope")
        store.upsert_profile({"profile_id": "mine", "profile_name": "Mine"})
        store.set_active_broker("mine")
        assert store.get_active_broker() == "mine"
        # The choice itself is audited (certified: audit everything).
        assert any(a["field_changed"] == "active_broker" for a in store.get_audit("__active__"))


# ---------------------------------------------------------------------------
# Catalogue: yaml brokers are visible, and grouped by whether you trade them
# ---------------------------------------------------------------------------


class TestBrokerCatalogue:
    """``dhan`` is defined in ``config/brokers.yaml`` but is not a preset.

    It used to be invisible in the panel — so it could not be edited or
    contract-note validated there even though the fee engine priced it.
    """

    def test_internal_marker_rows_are_not_brokers(self, store):
        """The kill-switch / active-broker markers live in the same table."""
        from backtest.api.segments_store import KILL_SWITCH_ID

        store.seed_from_yaml()
        store.set_active_broker("mstock")
        with store._manager.session() as session:
            session.add(
                BrokerProfileRow(
                    profile_id=KILL_SWITCH_ID,
                    profile_name="Global live kill-switch",
                    is_preset=False,
                    commission_model={"enabled": False},
                    statutory_rates={},
                )
            )
        ids = {row["profile_id"] for row in store.list_catalogue()}
        assert "__active__" not in ids
        assert KILL_SWITCH_ID not in ids
        assert "mstock" in ids
        # the API must not turn them into cards either
        assert store.get_catalogue_profile(KILL_SWITCH_ID) is None

    def test_yaml_only_broker_appears_with_its_rates(self, store):
        catalogue = {row["profile_id"]: row for row in store.list_catalogue()}
        assert "dhan" in catalogue
        dhan = catalogue["dhan"]
        assert dhan["currency"] == "INR"
        # The rates come from the file, not from a made-up default.
        assert dhan["statutory_rates"]["stt_delivery"] == "0.001"
        assert dhan["statutory_rates"]["gst_rate"] == "0.18"

    def test_yaml_only_broker_is_editable_and_resolvable(self, store):
        before = store.get_catalogue_profile("dhan")
        assert before is not None
        store.upsert_profile(
            {
                "profile_id": "dhan",
                "profile_name": "Dhan (validated)",
                "statutory_rates": dict(before["statutory_rates"]),
                "commission_model": {"default": {"model": "flat", "per_trade": "25"}},
            }
        )
        after = store.get_profile("dhan")
        assert after["commission_model"]["default"]["per_trade"] == "25"
        # Editing is an audited override, not a silent rewrite.
        assert any(a["field_changed"] == "__created__" for a in store.get_audit("dhan"))
        per_trade = store.resolve("dhan").commission_model.to_dict()["per_trade"]
        assert float(per_trade) == 25.0

    def test_validation_stamp_on_a_yaml_only_broker_is_recorded(self, store):
        store.mark_validated("dhan", "CN-2026-10-01")
        stored = store.get_profile("dhan")
        assert stored is not None, "the stamp needs a row to live in"
        assert stored["validated_on"] is not None
        assert stored["contract_note_ref"] == "CN-2026-10-01"

    def test_yaml_only_broker_can_be_made_active(self, store):
        store.set_active_broker("dhan")
        assert store.get_active_broker() == "dhan"
        assert load_broker_profile().name == "dhan"  # DB-first resolution

    def test_api_groups_the_catalogue_and_marks_usage(self, client, monkeypatch, tmp_path):
        import textwrap

        segments = tmp_path / "segments.yaml"
        segments.write_text(
            textwrap.dedent(
                """
                segments:
                  options_index:
                    broker: mstock
                    mode: paper
                  equity_intraday:
                    broker: dhan
                    mode: paper
                data:
                  primary: mstock
                """
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("SEGMENTS_CONFIG_PATH", str(segments))
        from backtest.brokers.segments import reset_segments_config

        reset_segments_config()
        body = client.get("/api/settings/brokers").get_json()
        rows = {row["profile_id"]: row for row in body["profiles"]}
        assert rows["mstock"]["group"] == "in_use"
        assert rows["dhan"]["group"] == "in_use"
        assert "segment: equity_intraday" in rows["dhan"]["usage"]
        assert "data: primary" in rows["mstock"]["usage"]
        # A broker nobody references is not "in use" — it stays available
        # (upstox is a preset that config/brokers.yaml does not define at all).
        # The file's active_broker prices every run that names no broker.
        assert "active broker" in rows["zerodha"]["usage"]
        assert rows["zerodha"]["group"] == "in_use"
        assert rows["upstox"]["group"] == "catalogue"
        assert rows["upstox"]["usage"] == []
        # ibkr is written in config/brokers.yaml, so it is "configured", but it
        # is still not in use — the panel does not expand it.
        assert rows["ibkr"]["group"] == "configured"
        # A stored row that still says exactly what the file says is labelled
        # with the file — no drift, nothing to explain.
        assert rows["ibkr"]["usage"] == ["config/brokers.yaml"]
        assert body["counts"]["in_use"] >= 2
        assert body["counts"]["configured"] >= 1
        reset_segments_config()

    def test_a_seeded_row_tracks_the_file_until_the_panel_edits_it(self, store):
        """A seeded row is a cache of the file, not a decision.

        The live DB can carry rows seeded before the yaml was read, so
        DB-first resolution would keep pricing runs with numbers the file no
        longer says. Re-seeding re-applies the file — once, and only while
        nobody has edited the row.
        """
        from backtest.api.broker_profiles_store import YAML_SYNC_ACTOR, _preset_rows, _yaml_rows

        store.seed_from_yaml()
        preset = next(r for r in _preset_rows() if r["profile_id"] == "zerodha")
        file_row = next(r for r in _yaml_rows() if r["profile_id"] == "zerodha")
        assert preset["commission_model"] != file_row["commission_model"], "fixture drifted"

        # Simulate the stale state: the preset's numbers, seeded, untouched.
        with store._manager.session() as session:
            row = session.get(BrokerProfileRow, "zerodha")
            row.commission_model = dict(preset["commission_model"])
        assert store.get_profile("zerodha")["commission_model"] != file_row["commission_model"]

        store.seed_from_yaml()
        synced = store.get_profile("zerodha")
        assert synced["commission_model"] == file_row["commission_model"]
        assert synced["is_preset"] is True, "lineage is not a rate — the sync keeps it"
        sync_audit = [a for a in store.get_audit("zerodha") if a["changed_by"] == YAML_SYNC_ACTOR]
        assert sync_audit, "a re-sync is audited"
        assert all(a["field_changed"] != "is_preset" for a in sync_audit)

        # A panel edit makes the row a decision: the file no longer overwrites it.
        store.upsert_profile(
            {"profile_id": "zerodha", "statutory_rates": {"stt_delivery": "0.0099"}}
        )
        store.seed_from_yaml()
        assert store.get_profile("zerodha")["statutory_rates"]["stt_delivery"] == "0.0099"
        assert any(
            a["field_changed"] == "statutory_rates" and a["changed_by"] == "admin"
            for a in store.get_audit("zerodha")
        ), "the panel edit is the row's own record"

    def test_a_stored_row_says_whether_it_still_matches_the_file(self, store):
        """Seeded rows copy the file; a panel edit is what makes them an override."""
        store.seed_from_yaml()
        catalogue = {row["profile_id"]: row for row in store.list_catalogue()}
        assert catalogue["dhan"]["matches_file"] is True
        assert catalogue["ibkr"]["matches_file"] is True
        # upstox is a preset the file does not define: nothing to match.
        assert catalogue["upstox"]["matches_file"] is False

        store.upsert_profile(
            {"profile_id": "dhan", "statutory_rates": {"stt_delivery": "0.002"}}
        )
        after = {row["profile_id"]: row for row in store.list_catalogue()}
        assert after["dhan"]["matches_file"] is False

    def test_a_validated_row_is_never_re_synced(self, store):
        """A validation stamp is a decision: the file must not undo it."""
        store.mark_validated("mstock", "CN-1")
        with store._manager.session() as session:
            session.get(BrokerProfileRow, "mstock").commission_model = {"stale": True}
        store.seed_from_yaml()
        assert store.get_profile("mstock")["commission_model"] == {"stale": True}

    def test_api_serves_a_yaml_only_profile_for_the_editor(self, client):
        response = client.get("/api/settings/brokers/dhan")
        assert response.status_code == 200
        profile = response.get_json()["profile"]
        assert profile["profile_id"] == "dhan"
        assert profile["statutory_rates"]["gst_rate"] == "0.18"

    def test_panel_override_of_the_file_is_flagged(self, client, store):
        store.upsert_profile(
            {
                "profile_id": "dhan",
                "profile_name": "Dhan",
                "statutory_rates": {"stt_delivery": "0.002", "gst_rate": "0.18"},
            }
        )
        payload = client.get("/api/settings/brokers").get_json()
        rows = {r["profile_id"]: r for r in payload["profiles"]}
        assert any("this row wins" in reason for reason in rows["dhan"]["usage"])
        assert rows["dhan"]["group"] == "in_use"  # a segment uses it


# ---------------------------------------------------------------------------
# DB row → BrokerProfile: pricing parity with the preset
# ---------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_panel_grouping_harness():
    """The broker cards in a stub DOM — the page's JS is inline, so drive it.

    This harness is what caught the summary line's nested double quotes, which
    killed the whole inline script (the panel rendered as static markup).
    """
    harness = _REPO_ROOT / "tests" / "js" / "test_settings_profiles.mjs"
    result = subprocess.run(
        ["node", str(harness)], cwd=_REPO_ROOT, capture_output=True, text=True, timeout=60
    )
    assert (
        result.returncode == 0
    ), f"node harness failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "9 tests passed" in result.stdout


class TestProfileRebuild:
    def test_mstock_row_prices_identically_to_preset(self, store):
        """The string-rate coercion regression: DB/JSON round-trips rates as
        strings; an uncoerced str field exploded with a TypeError at multiply
        time. The rebuilt profile must price the same ₹1L intraday sell as the
        preset it came from."""
        store.seed_from_yaml()
        stored = store.get_profile("mstock")
        rebuilt = BrokerProfile(
            **BrokerProfileRow(**store._db_shape(stored)).to_profile_kwargs()
        )
        preset = CommissionCalculator.for_broker("mstock")
        rebuilt_calc = CommissionCalculator(broker=rebuilt)
        a = preset.calculate(quantity=100, fill_price=1250, side="SELL", segment="equity_intraday")
        b = rebuilt_calc.calculate(
            quantity=100, fill_price=1250, side="SELL", segment="equity_intraday"
        )
        assert b.total == a.total > 0

    def test_edited_rate_changes_the_price(self, store):
        store.upsert_profile(
            {
                "profile_id": "expensive",
                "profile_name": "Expensive",
                "statutory_rates": {"stt_delivery": "0.002", "gst_rate": "0.18"},
                "commission_model": {"default": {"model": "flat", "per_trade": "40"}},
            }
        )
        profile = load_broker_profile(broker="expensive")
        fees = CommissionCalculator(broker=profile).calculate(
            quantity=100, fill_price=1250, side="SELL", segment="equity_delivery"
        )
        assert fees.get("brokerage") == 40  # the edited flat rate, not a preset
        assert fees.get("stt") == 250.0  # 0.2% of 125k — the edited STT


# ---------------------------------------------------------------------------
# DB-first resolution
# ---------------------------------------------------------------------------


class TestDbFirstResolution:
    def test_load_broker_profile_prefers_db_row(self, store, monkeypatch):
        monkeypatch.setattr("backtest.api.broker_profiles_store._STORE", store)
        store.upsert_profile(
            {
                "profile_id": "panel_broker",
                "profile_name": "Panel Broker",
                "commission_model": {"default": {"model": "flat", "per_trade": "33"}},
            }
        )
        profile = load_broker_profile(broker="panel_broker")
        assert profile.name == "panel_broker"
        assert profile.commission_model.to_dict()["per_trade"] == "33.0000"

    def test_unknown_profile_falls_through_to_preset(self, store, monkeypatch):
        monkeypatch.setattr("backtest.api.broker_profiles_store._STORE", store)
        profile = load_broker_profile(broker="mstock")
        assert profile.name == "mstock"  # preset fallback, not a DB miss crash

    def test_active_broker_used_when_no_explicit_name(self, store, monkeypatch):
        monkeypatch.setattr("backtest.api.broker_profiles_store._STORE", store)
        store.seed_from_yaml()  # 'mstock' must exist before it can be active
        store.set_active_broker("mstock")
        assert load_broker_profile().name == "mstock"


# ---------------------------------------------------------------------------
# HTTP API + page
# ---------------------------------------------------------------------------


class TestSettingsApi:
    def test_list_and_get(self, client):
        data = client.get("/api/settings/brokers").get_json()
        assert "mstock" in {p["profile_id"] for p in data["profiles"]}
        body = client.get("/api/settings/brokers/mstock")
        assert body.status_code == 200
        assert body.get_json()["profile"]["profile_id"] == "mstock"
        assert client.get("/api/settings/brokers/nope").status_code == 404

    def test_upsert_rejects_bad_rates_with_400(self, client):
        r = client.put(
            "/api/settings/brokers/x", json={"statutory_rates": {"stt_delivery": "-1"}}
        )
        assert r.status_code == 400

    def test_contract_note_pass_stamps_profile(self, client):
        expected = {
            "brokerage": 20,
            "stt": 31.25,
            "exchange_transaction": 3.71,
            "sebi_turnover": 0.13,
            "ipft": 0.13,
            "gst": 4.31,
        }
        r = client.post(
            "/api/settings/brokers/mstock/validate",
            json={
                "trade_value": 125000,
                "quantity": 100,
                "side": "SELL",
                "segment": "equity_intraday",
                "expected": expected,
                "document_id": "note-77",
            },
        )
        assert r.status_code == 200
        assert r.get_json()["status"] == "PASS"
        profile = client.get("/api/settings/brokers/mstock").get_json()["profile"]
        assert profile["contract_note_ref"] == "note-77"
        assert profile["validated_on"] is not None

    def test_contract_note_fail_lists_mismatches_and_stamps_nothing(self, client):
        r = client.post(
            "/api/settings/brokers/mstock/validate",
            json={
                "trade_value": 125000,
                "quantity": 100,
                "side": "SELL",
                "segment": "equity_intraday",
                "expected": {"stt": 99.0},
                "document_id": "note-bad",
            },
        )
        assert r.status_code == 422
        assert any("stt" in m for m in r.get_json()["mismatches"])
        profile = client.get("/api/settings/brokers/mstock").get_json()["profile"]
        assert profile["validated_on"] is None

    def test_active_broker_endpoints(self, client):
        assert client.get("/api/settings/active-broker").get_json()["active_broker"] is None
        ok = client.put("/api/settings/active-broker", json={"profile_id": "mstock"})
        assert ok.status_code == 200
        bad = client.put("/api/settings/active-broker", json={"profile_id": "nope"})
        assert bad.status_code == 400

    def test_settings_page_renders(self, client):
        page = client.get("/settings")
        assert page.status_code == 200
        assert b"Cost &amp; Risk Settings" in page.data


# ---------------------------------------------------------------------------
# Phase 2 — segments, kill-switch, live-arming gate
# ---------------------------------------------------------------------------


@pytest.fixture()
def segments(store):
    """SegmentsStore on the same throwaway DB as the profiles store."""
    from backtest.api.segments_store import SegmentsStore

    return SegmentsStore(store._manager)


class TestKillSwitch:
    def test_default_off(self, segments):
        assert segments.is_live_kill_switch_on() is False

    def test_toggle_roundtrip_and_audit(self, segments):
        segments.set_live_kill_switch(True)
        assert segments.is_live_kill_switch_on() is True
        segments.set_live_kill_switch(True)  # idempotent — no second audit row
        rows = [
            a
            for a in segments.get_audit("__live_kill_switch__")
            if a["field_changed"] == "live_kill_switch"
        ]
        assert len(rows) == 1
        assert rows[0]["new_value"] == "True"
        segments.set_live_kill_switch(False)
        assert segments.is_live_kill_switch_on() is False


class TestSegmentCrud:
    def test_upsert_and_list(self, segments):
        seg = segments.upsert_segment(
            {
                "segment_id": "equity_intraday",
                "segment_name": "Equity Intraday",
                "mode": "paper",
                "allocated_capital": "250000",
                "risk_limits": {"daily_loss_limit": "5000", "max_positions": 4},
            }
        )
        assert seg["mode"] == "paper"
        assert seg["allocated_capital"] == 250000.0
        assert seg["risk_limits"]["daily_loss_limit"] == 5000.0
        assert seg["risk_limits"]["max_positions"] == 4
        assert {s["segment_id"] for s in segments.list_segments()} >= {"equity_intraday"}

    def test_update_audits_each_field(self, segments):
        segments.upsert_segment({"segment_id": "s1", "mode": "paper"})
        segments.upsert_segment(
            {"segment_id": "s1", "mode": "live", "risk_limits": {"daily_loss_limit": 1000}}
        )
        fields = [a["field_changed"] for a in segments.get_audit("s1")]
        assert fields[0] in ("mode", "risk_limits")
        assert "mode" in fields and "risk_limits" in fields

    def test_validation_fail_closed(self, segments):
        with pytest.raises(ValueError, match="mode must be"):
            segments.upsert_segment({"segment_id": "x", "mode": "yolo"})
        with pytest.raises(ValueError, match="unknown risk limit"):
            segments.upsert_segment({"segment_id": "x", "risk_limits": {"bogus": 1}})
        with pytest.raises(ValueError, match="must not be negative"):
            segments.upsert_segment({"segment_id": "x", "risk_limits": {"daily_loss_limit": -5}})
        with pytest.raises(ValueError, match="not be negative"):
            segments.upsert_segment({"segment_id": "x", "allocated_capital": -1})

    def test_delete(self, segments):
        segments.upsert_segment({"segment_id": "gone", "mode": "paper"})
        assert segments.delete_segment("gone") is True
        assert segments.delete_segment("gone") is False
        assert segments.get_segment("gone") is None


class TestLiveArmingGate:
    """The certified two-tier gate: fail closed, every check must PASS."""

    def _validated_broker(self, store, profile_id="mstock"):
        store.seed_from_yaml()
        store.mark_validated(profile_id, "note-1")

    def test_kill_switch_off_blocks_everything(self, segments):
        blockers = segments.live_arming_blockers()
        assert any("kill-switch" in b for b in blockers)

    def test_kill_switch_on_no_segment_checks_global_only(self, segments):
        segments.set_live_kill_switch(True)
        assert segments.live_arming_blockers() == []

    def test_segment_needs_mode_limit_and_validated_broker(self, segments, store):
        segments.set_live_kill_switch(True)
        self._validated_broker(store)
        segments.upsert_segment(
            {"segment_id": "eq", "mode": "paper", "broker_profile_id": "mstock"}
        )
        blockers = segments.live_arming_blockers("eq")
        assert any("mode is 'paper'" in b for b in blockers)
        assert any("daily_loss_limit" in b for b in blockers)

        segments.upsert_segment(
            {
                "segment_id": "eq",
                "mode": "live",
                "broker_profile_id": "mstock",
                "risk_limits": {"daily_loss_limit": 4000},
            }
        )
        assert segments.live_arming_blockers("eq") == []

    def test_unvalidated_broker_blocks_live(self, segments, store):
        segments.set_live_kill_switch(True)
        store.seed_from_yaml()  # mstock exists but is NOT validated
        segments.upsert_segment(
            {
                "segment_id": "eq",
                "mode": "live",
                "broker_profile_id": "mstock",
                "risk_limits": {"daily_loss_limit": 4000},
            }
        )
        blockers = segments.live_arming_blockers("eq")
        assert any("contract-note validated" in b for b in blockers)

    def test_zero_daily_loss_limit_blocks(self, segments, store):
        segments.set_live_kill_switch(True)
        self._validated_broker(store)
        segments.upsert_segment(
            {
                "segment_id": "eq",
                "mode": "live",
                "broker_profile_id": "mstock",
                "risk_limits": {"daily_loss_limit": 0},
            }
        )
        assert any("daily_loss_limit" in b for b in segments.live_arming_blockers("eq"))

    def test_unknown_segment_and_broker_block(self, segments, store):
        segments.set_live_kill_switch(True)
        blockers = segments.live_arming_blockers("nope")
        assert any("unknown segment" in b for b in blockers)


class TestArmingWiring:
    """assert_live_arming_allowed: the panel gates only deployments that
    opted in (kill-switch sentinel row present); everyone else keeps the
    legacy ALLOW_LIVE_ORDERS env behavior."""

    def test_unconfigured_panel_defers_to_env_gate(self, monkeypatch):
        from backtest.api.segments_store import assert_live_arming_allowed

        monkeypatch.setattr("backtest.api.segments_store._panel_configured", lambda: False)
        # No raise even though nothing is armed — the env gate stays in charge.
        assert_live_arming_allowed()

    def test_panel_in_control_blocks_when_switch_off(self, segments, monkeypatch):
        from backtest.api import segments_store as mod

        monkeypatch.setattr(mod, "_panel_configured", lambda: True)
        monkeypatch.setattr(mod, "_STORE", segments)
        with pytest.raises(ValueError, match="kill-switch is OFF"):
            mod.assert_live_arming_allowed()

    def test_panel_in_control_allows_when_fully_armed(self, segments, store, monkeypatch):
        from backtest.api import segments_store as mod

        store.seed_from_yaml()
        store.mark_validated("mstock", "note-1")
        segments.set_live_kill_switch(True)
        segments.upsert_segment(
            {
                "segment_id": "eq",
                "mode": "live",
                "broker_profile_id": "mstock",
                "risk_limits": {"daily_loss_limit": 4000},
            }
        )
        monkeypatch.setattr(mod, "_panel_configured", lambda: True)
        monkeypatch.setattr(mod, "_STORE", segments)
        mod.assert_live_arming_allowed("eq")  # no raise

    def test_gateway_refuses_when_panel_blocks(self, segments, monkeypatch):
        """LiveEquityGateway arming now passes through the panel gate."""
        from backtest.api import segments_store as mod
        from backtest.forward.live_gateway import LiveEquityGateway

        monkeypatch.setattr(mod, "_panel_configured", lambda: True)
        monkeypatch.setattr(mod, "_STORE", segments)
        monkeypatch.setenv("ALLOW_LIVE_ORDERS", "1")

        class FakeBroker:
            def is_authenticated(self):
                return True

        with pytest.raises(ValueError, match="kill-switch is OFF"):
            LiveEquityGateway(ledger=None, broker=FakeBroker(), confirm_live=True)

    def test_panel_probe_fail_soft_without_sentinel(self, tmp_path, monkeypatch):
        """A reachable DB WITHOUT the sentinel row → not panel-controlled
        (legacy env-gate behavior); the probe never raises."""
        from backtest.api import segments_store as mod

        monkeypatch.setenv("FORWARD_TEST_DB_URL", f"sqlite+pysqlite:///{tmp_path}/plain.db")
        assert mod._panel_configured() is False
        monkeypatch.setattr(mod, "_panel_configured", lambda: False)
        mod.assert_live_arming_allowed()  # legacy path, no raise


class TestPhase2Api:
    def test_segment_endpoints_roundtrip(self, client):
        r = client.put(
            "/api/settings/segments/eq",
            json={"segment_name": "EQ", "mode": "paper", "risk_limits": {"daily_loss_limit": 1000}},
        )
        assert r.status_code == 200
        assert r.get_json()["segment"]["segment_id"] == "eq"
        assert client.get("/api/settings/segments").get_json()["segments"]
        assert client.get("/api/settings/segments/eq").status_code == 200
        assert client.get("/api/settings/segments/eq/audit").get_json()["audit"]
        assert client.delete("/api/settings/segments/eq").status_code == 200
        assert client.get("/api/settings/segments/eq").status_code == 404

    def test_segment_bad_mode_400(self, client):
        assert client.put("/api/settings/segments/x", json={"mode": "wild"}).status_code == 400

    def test_arming_endpoint_lists_blockers(self, client):
        body = client.get("/api/settings/segments/eq/arming").get_json()
        assert body["armed"] is False
        assert any("kill-switch" in b for b in body["blockers"])

    def test_kill_switch_endpoints(self, client):
        assert client.get("/api/settings/live-kill-switch").get_json()["enabled"] is False
        ok = client.put("/api/settings/live-kill-switch", json={"enabled": True})
        assert ok.status_code == 200
        assert client.get("/api/settings/live-kill-switch").get_json()["enabled"] is True

    def test_settings_page_has_phase2_sections(self, client):
        page = client.get("/settings")
        assert b"Global live kill-switch" in page.data
        assert b"Segments" in page.data
