"""Run provenance — engine + data source stamps (PRD backTest-enhance §1.1/§1.2).

The rule these tests protect: a result's numbers are never presented without
saying which engine and which data produced them, and the wording lives in
exactly one module. The UI (JS harness in ``tests/js/test_provenance.mjs``) and
the API (``tests/test_api_backtest_provenance.py``) both read what is asserted
here, so a label change cannot drift between the three.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from backtest.data.provenance import (
    APPROXIMATE_WARNING,
    ENGINE_FILL_EXACT,
    ENGINE_MIXED,
    ENGINE_OPTIONS,
    ENGINE_QUICK_SCREEN,
    MIXED_ENGINE_WARNING,
    REAL_SOURCES,
    build_provenance,
    describe_engine,
    describe_source,
    engine_is_canonical,
    engine_label,
    normalize_engine,
    source_is_real,
    source_label,
)

# ---------------------------------------------------------------------------
# Engine labels
# ---------------------------------------------------------------------------


def test_fill_exact_is_the_canonical_engine():
    assert engine_label(ENGINE_FILL_EXACT) == "Fill-Exact (Canonical)"
    assert engine_is_canonical(ENGINE_FILL_EXACT) is True
    assert describe_engine(ENGINE_FILL_EXACT)["engine_tier"] == "canonical"


def test_quick_screen_is_labelled_approximate_and_never_canonical():
    assert engine_label(ENGINE_QUICK_SCREEN) == "Quick-Screen (Approximate)"
    assert engine_is_canonical(ENGINE_QUICK_SCREEN) is False
    assert describe_engine(ENGINE_QUICK_SCREEN)["engine_tier"] == "approximate"


def test_unknown_engine_is_labelled_not_hidden():
    """A new engine id must be visible rather than silently read as canonical."""
    d = describe_engine("some_future_engine")
    assert d["engine_used"] == "some_future_engine"
    assert d["engine_canonical"] is False
    assert d["engine_tier"] == "unknown"
    assert engine_label("some_future_engine") == "some_future_engine"


def test_empty_engine_defaults_to_the_canonical_path():
    d = describe_engine(None)
    assert d["engine_used"] == ENGINE_FILL_EXACT
    assert d["engine_canonical"] is True


def test_config_level_driver_alias_resolves_to_the_fill_exact_engine():
    """``backtestConfig.engine`` spells it ``driver``; it IS the fill-exact path."""
    assert normalize_engine("driver") == ENGINE_FILL_EXACT
    assert engine_is_canonical("driver") is True
    assert normalize_engine("quick_screen") == ENGINE_QUICK_SCREEN
    assert engine_label("DRIVER") == "Fill-Exact (Canonical)"  # case-insensitive


def test_options_engine_has_its_own_label_and_is_not_claimed_canonical():
    d = describe_engine(ENGINE_OPTIONS)
    assert d["engine_label"] == "Options Engine (Multi-Leg)"
    assert d["engine_tier"] == "options"
    assert d["engine_canonical"] is False


# ---------------------------------------------------------------------------
# Data source labels
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,label,real",
    [
        ("db", "Real (PostgreSQL)", True),
        ("mstock", "Live (mStock)", True),
        ("synthetic", "Synthetic", False),
        ("csv", "CSV", False),
    ],
)
def test_every_app_source_has_a_badge_label(name, label, real):
    assert source_label(name) == label
    assert source_is_real(name) is real
    assert (name in REAL_SOURCES) is real


def test_unknown_source_is_not_treated_as_real():
    """Fail closed: an unrecognised source must never claim certification-grade data."""
    assert source_is_real("some_new_feed") is False
    assert source_label("some_new_feed") == "some_new_feed"


def test_an_unnamed_source_is_not_labelled_synthetic():
    """A provenance record that cannot say its source says Unknown.

    The badge used to default an empty name to synthetic, which quietly
    attached generated-data provenance to a run that never named its source.
    """
    assert source_label("") == "Unknown"
    described = describe_source("")
    assert described["data_source_label"] == "Unknown"
    assert described["data_source_real"] is False


# ---------------------------------------------------------------------------
# Warnings
# ---------------------------------------------------------------------------


def _codes(prov):
    return [w["code"] for w in prov["warnings"]]


def test_synthetic_data_raises_the_red_banner():
    prov = build_provenance(source="synthetic", engine=ENGINE_FILL_EXACT)
    assert "non_real_data" in _codes(prov)
    banner = next(w for w in prov["warnings"] if w["code"] == "non_real_data")
    assert banner["level"] == "error"
    assert "Synthetic" in banner["message"]
    assert "Real-data certification required before paper testing." in banner["message"]


def test_csv_data_names_csv_in_the_banner():
    prov = build_provenance(source="csv", engine=ENGINE_FILL_EXACT)
    assert "CSV" in prov["warnings"][0]["message"]


def test_real_data_raises_no_data_banner():
    for name in ("db", "mstock"):
        prov = build_provenance(source=name, engine=ENGINE_FILL_EXACT)
        assert "non_real_data" not in _codes(prov), name


def test_quick_screen_raises_the_yellow_approximate_warning():
    prov = build_provenance(source="db", engine=ENGINE_QUICK_SCREEN)
    assert "approximate_engine" in _codes(prov)
    assert prov["warnings"][0]["message"] == APPROXIMATE_WARNING
    assert prov["warnings"][0]["level"] == "warning"


def test_canonical_engine_on_real_data_raises_nothing():
    assert build_provenance(source="db", engine=ENGINE_FILL_EXACT)["warnings"] == []


def test_synthetic_plus_quick_screen_raises_both_worst_first():
    codes = _codes(build_provenance(source="synthetic", engine=ENGINE_QUICK_SCREEN))
    assert codes == ["non_real_data", "approximate_engine"]


def test_mixed_engines_warn_that_the_comparison_is_not_like_for_like():
    prov = build_provenance(source="db", engine=ENGINE_MIXED)
    assert prov["warnings"][0]["message"] == MIXED_ENGINE_WARNING
    assert prov["engine_canonical"] is False


def test_options_engine_note_is_informational_not_a_blocker():
    prov = build_provenance(source="db", engine=ENGINE_OPTIONS)
    assert [w["level"] for w in prov["warnings"]] == ["info"]


def test_unknown_engine_warns_about_reproducibility():
    prov = build_provenance(source="db", engine="mystery")
    assert _codes(prov) == ["unknown_engine"]


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------


def test_record_carries_the_audit_fields_the_prd_asks_for():
    prov = build_provenance(
        source="db",
        engine=ENGINE_FILL_EXACT,
        symbol="RELIANCE",
        timeframe="1D",
        start_date="2020-01-01",
        end_date="2024-12-31",
        bars=1247,
    )
    assert prov["data_source"] == "db"
    assert prov["symbol"] == "RELIANCE"
    assert prov["timeframe"] == "1D"
    assert prov["date_range"] == {"from": "2020-01-01", "to": "2024-12-31"}
    assert prov["bars_count"] == 1247
    assert prov["engine_used"] == ENGINE_FILL_EXACT
    assert prov["data_fetch_date"] is not None


def test_actual_candle_coverage_falls_back_to_the_requested_range():
    """No bars reported (compare shared block) — the range still has to read."""
    prov = build_provenance(source="synthetic", start_date="2024-01-01", end_date="2024-06-30")
    assert prov["data_from"] == "2024-01-01"
    assert prov["data_to"] == "2024-06-30"


def test_actual_coverage_wins_over_the_requested_range():
    prov = build_provenance(
        source="db",
        start_date="2020-01-01",
        end_date="2024-12-31",
        data_from=date(2021, 3, 4),
        data_to="2024-11-29T00:00:00",
    )
    assert prov["data_from"] == "2021-03-04"  # symbol was not cached in 2020
    assert prov["data_to"] == "2024-11-29"


def test_real_sources_fall_back_to_the_newest_bar_as_the_fetch_date():
    """A cached feed is exactly as fresh as its newest bar."""
    prov = build_provenance(source="db", data_to="2024-12-30", data_from="2020-01-01")
    assert prov["data_fetch_date"] == "2024-12-30"


def test_synthetic_data_reports_no_fetch_date():
    """Generated data has no fetch date; inventing one would be a false claim."""
    prov = build_provenance(source="synthetic", data_from="2024-01-01", data_to="2024-12-31")
    assert prov["data_fetch_date"] is None


def test_source_object_supplies_the_fetch_date():
    class Src:
        def last_ingested_at(self, symbol, timeframe):
            return "2026-09-27T11:00:00+05:30"

    prov = build_provenance(source="db", symbol="RELIANCE", timeframe="1D", source_obj=Src())
    assert prov["data_fetch_date"] == "2026-09-27"


def test_a_source_that_cannot_answer_never_fails_a_completed_run():
    class Broken:
        def last_ingested_at(self, symbol, timeframe):
            raise RuntimeError("db down")

    prov = build_provenance(source="db", symbol="X", data_to="2024-01-01", source_obj=Broken())
    assert prov["data_fetch_date"] == "2024-01-01"  # fell back, did not raise


def test_dates_are_normalised_to_iso_days():
    prov = build_provenance(
        source="db", start_date=datetime(2020, 1, 1, 9, 15), end_date=date(2024, 12, 31)
    )
    assert prov["date_range"] == {"from": "2020-01-01", "to": "2024-12-31"}


def test_record_is_json_native():
    """Rides along in a jsonify() payload and a stored run row — no numpy, no dates."""
    import json

    prov = build_provenance(
        source="db",
        engine=ENGINE_FILL_EXACT,
        symbol="X",
        timeframe="1D",
        start_date="2024-01-01",
        end_date="2024-02-01",
        bars=10,
    )
    assert json.loads(json.dumps(prov)) == prov
    assert isinstance(prov["bars_count"], int)


def test_bars_is_none_when_the_record_covers_no_bars_of_its_own():
    """A Compare shared block has no bars; ``0`` would claim the run had none."""
    prov = build_provenance(source="db", bars=None)
    assert prov["bars_count"] is None
    assert build_provenance(source="db", bars=0)["bars_count"] == 0


def test_describe_helpers_match_the_full_record():
    prov = build_provenance(source="mstock", engine="quick_screen", bars=5)
    assert {k: prov[k] for k in describe_source("mstock")} == {
        k: prov[k] for k in ("data_source", "data_source_label", "data_source_real")
    }
    assert {k: prov[k] for k in describe_engine("quick_screen")} == {
        k: prov[k] for k in ("engine_used", "engine_label", "engine_canonical", "engine_tier")
    }
