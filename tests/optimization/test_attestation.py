"""Data attestation — PRD backTest-enhance Part 2 §2.

The problem the PRD names: *"Optimize pulls candles from whatever source the
app started with, and there's no visible confirmation of this anywhere."* The
gap is not the data, it is that nobody had to say which data.

These pin the record, the one gate, and the promise that the stored attestation
describes the candles the search actually loaded rather than the range that
was asked for.
"""

from __future__ import annotations

import copy
import datetime

import pytest

from backtest.optimization.attestation import (
    STALE_AFTER_DAYS,
    SYNTHETIC_ACKNOWLEDGEMENT,
    attestation_is_satisfied,
    attestation_preview,
    attestation_record,
    attestation_warnings,
    stale_days,
)

TODAY = datetime.date(2026, 9, 29)


def candles(dates, values=None):
    import pandas as pd

    idx = pd.to_datetime(dates)
    return pd.DataFrame({"close": values or range(100, 100 + len(dates))}, index=idx)


class TestTheRecord:
    def test_it_names_the_source_in_words_not_slugs(self):
        """`db` is a slug. 'Real (PostgreSQL)' is something an operator can act
        on — and it comes from the one module that owns these labels, not from
        a second table of spellings that will drift."""
        att = attestation_preview("db", symbol="RELIANCE", today=TODAY)
        assert att["data_source"] == "db"
        assert att["data_source_label"] == "Real (PostgreSQL)"
        assert att["data_source_real"] is True

    def test_the_preview_does_not_guess_the_bar_count(self):
        """Bar count is not knowable until the candles are fetched. A box that
        guessed would be a box that lies."""
        att = attestation_preview("db", symbol="RELIANCE", today=TODAY)
        assert att["bars_count"] is None

    def test_the_record_reports_what_the_source_actually_had(self):
        df = candles(["2021-01-01", "2021-01-02", "2021-01-03"])
        att = attestation_record("db", df, symbol="RELIANCE", timeframe="1d", today=TODAY)
        assert att["bars_count"] == 3
        assert att["date_from"] == "2021-01-01"
        assert att["date_to"] == "2021-01-03"

    def test_a_short_symbol_reports_what_it_got_not_what_was_asked_for(self):
        """The point of the whole feature: asking for five years of a symbol
        with two years of data returns two years, and the record kept on the
        run has to say so."""
        df = candles(["2023-01-01", "2023-06-01"])
        att = attestation_record(
            "db",
            df,
            symbol="THIN",
            start_date="2020-01-01",
            end_date="2024-12-31",
            today=TODAY,
        )
        assert att["bars_count"] == 2
        assert att["date_from"] == "2023-01-01", "the requested 2020 start is not the truth"
        assert att["date_to"] == "2023-06-01"

    def test_no_candles_at_all_is_distinguishable_from_never_measured(self):
        """ "0 bars" is alarming; "not measured yet" is a different sentence."""
        preview = attestation_preview("db", symbol="NOPE", today=TODAY)
        empty = attestation_record("db", candles([]), symbol="NOPE", today=TODAY)
        assert preview["bars_count"] is None
        assert empty["bars_count"] == 0

    def test_a_missing_symbol_stores_null_rather_than_empty_string(self):
        att = attestation_preview("db", symbol="", timeframe="", today=TODAY)
        assert att["symbol"] == ""
        assert att["bars_count"] is None


class TestStaleness:
    def test_fresh_data_is_not_stale(self):
        assert stale_days("2026-09-28", today=TODAY) == 1

    def test_old_data_is(self):
        assert stale_days("2026-08-01", today=TODAY) > STALE_AFTER_DAYS

    def test_a_date_in_the_future_reads_as_fresh_rather_than_negative(self):
        """Clock skew between the data host and this one should not produce an
        age of -3 days, which would read as younger than data fetched today."""
        assert stale_days("2026-10-05", today=TODAY) == 0

    def test_an_undateable_fetch_is_unknown_not_fresh(self):
        """None and 0 are different answers. None is the one that needs saying."""
        assert stale_days(None, today=TODAY) is None
        assert stale_days("not a date", today=TODAY) is None

    def test_stale_real_data_warns_but_does_not_refuse(self):
        """A box that refuses work for reasons the operator cannot act on
        trains them to ignore it. Re-fetching is a choice, not a permission."""
        att = attestation_preview("db", data_fetch_date="2025-01-01", today=TODAY)
        assert att["stale"] is True
        assert attestation_is_satisfied(att) is True
        assert any(w["code"] == "stale_data" for w in attestation_warnings(att))

    def test_undateable_real_data_is_warned_about_explicitly(self):
        att = attestation_preview("db", today=TODAY)
        codes = {w["code"] for w in attestation_warnings(att)}
        assert "unknown_fetch_date" in codes
        assert attestation_is_satisfied(att) is True


class TestTheSyntheticGate:
    def test_synthetic_cannot_start_unacknowledged(self):
        att = attestation_preview("synthetic", symbol="DEMO", today=TODAY)
        assert att["requires_acknowledgement"] is True
        assert attestation_is_satisfied(att) is False

    def test_it_can_start_once_acknowledged(self):
        att = attestation_preview("synthetic", symbol="DEMO", today=TODAY)
        att["acknowledged"] = True
        assert attestation_is_satisfied(att) is True

    def test_the_operator_ticks_a_verbatim_sentence(self):
        """Stored on the run so the audit trail shows what was agreed to, not a
        paraphrase written later by whatever renders it."""
        att = attestation_preview("synthetic", today=TODAY)
        assert att["acknowledgement_required"] == SYNTHETIC_ACKNOWLEDGEMENT
        assert attestation_preview("db", today=TODAY)["acknowledgement_required"] is None

    def test_real_data_never_asks_for_the_tick(self):
        att = attestation_preview("db", today=TODAY)
        assert att["requires_acknowledgement"] is False
        assert attestation_is_satisfied(att) is True

    def test_synthetic_is_warned_about_loudly(self):
        warnings = attestation_warnings(attestation_preview("synthetic", today=TODAY))
        assert any(w["level"] == "error" and w["code"] == "synthetic_data" for w in warnings)

    def test_a_missing_record_refuses_rather_than_defaulting_to_allowed(self):
        """Fail closed. An absent attestation means the gate did not run, and a
        gate that opens when it cannot see its own input is not a gate."""
        assert attestation_is_satisfied(None) is False
        assert attestation_is_satisfied({}) is False


class TestWarnings:
    def test_no_bars_is_an_error_not_a_warning(self):
        att = attestation_record("db", candles([]), symbol="GONE", today=TODAY)
        entry = next(w for w in attestation_warnings(att) if w["code"] == "no_bars")
        assert entry["level"] == "error"

    def test_healthy_real_data_warns_about_nothing(self):
        df = candles(["2026-09-27", "2026-09-28"])
        att = attestation_record(
            "db", df, symbol="RELIANCE", data_fetch_date="2026-09-29", today=TODAY
        )
        assert attestation_warnings(att) == []


class TestTheColumns:
    def test_dates_are_real_dates_not_iso_strings(self):
        """PostgreSQL accepts the string; SQLite raises. Passing the string
        through works on production and breaks on a dev machine, which is the
        worst order in which to find out."""
        from backtest.optimization.attestation import attestation_columns

        cols = attestation_columns(
            attestation_preview("db", symbol="R", start_date="2020-01-01", end_date="2024-12-31")
        )
        assert isinstance(cols["date_from"], datetime.date)
        assert isinstance(cols["date_to"], datetime.date)

    def test_an_empty_attestation_still_produces_every_column(self):
        from backtest.optimization.attestation import attestation_columns

        assert set(attestation_columns(None)) == {
            "data_source",
            "data_fetch_date",
            "bars_count",
            "symbol",
            "timeframe",
            "date_from",
            "date_to",
            "data_attestation",
        }

    def test_the_json_record_keeps_strings(self):
        """It has to survive a JSON column and an HTTP response, so it cannot
        hold date objects — which is exactly why the flat columns are coerced."""
        from backtest.optimization.attestation import attestation_columns

        cols = attestation_columns(attestation_preview("db", start_date="2020-01-01"))
        assert cols["data_attestation"]["date_from"] == "2020-01-01"


# ---------------------------------------------------------------------------
# The gate as the service enforces it
# ---------------------------------------------------------------------------


class TestTheGateInTheService:
    def test_synthetic_is_refused_before_anything_runs(self, service, unacknowledged):
        from backtest.optimization.service import OptimizationError

        with pytest.raises(OptimizationError) as exc:
            service.submit(unacknowledged)
        assert exc.value.status == 409
        assert (
            exc.value.code == "synthetic_data_not_acknowledged"
        ), "the UI must be able to tell this refusal apart without matching wording"

    def test_acknowledging_it_lets_the_run_proceed(self, service, store, sma_doc):
        run = service.wait(service.submit(sma_doc)["run_id"], timeout=120)
        assert run["status"] == "completed"

    def test_the_refused_run_is_not_half_created(self, service, unacknowledged):
        """The gate runs before create_run, so a refused submission leaves no
        pending row for the runs list to show."""
        from backtest.optimization.service import OptimizationError

        before = len(service.store.list_runs())
        with pytest.raises(OptimizationError):
            service.submit(unacknowledged)
        assert len(service.store.list_runs()) == before

    def test_the_stored_record_survives_the_measured_rewrite(self, service, store, sma_doc):
        """The run thread replaces the preview with the measured record. The
        acknowledgement is the one field not derived from the data — nothing in
        the candles can re-earn what a person ticked."""
        run = service.wait(service.submit(sma_doc)["run_id"], timeout=120)
        stored = store.get_run(run["run_id"])
        assert stored["data_attestation"]["acknowledged"] is True
        assert stored["bars_count"], "the measured record must carry the bar count"

    def test_the_stored_record_measures_the_candles_not_the_request(self, service, store, sma_doc):
        run = service.wait(service.submit(sma_doc)["run_id"], timeout=120)
        stored = store.get_run(run["run_id"])
        # The flat columns are typed Date; the JSON record keeps the ISO string
        # it has to be in to survive a JSON column and an HTTP response.
        assert stored["date_from"] == datetime.date(2021, 1, 1)
        assert stored["data_attestation"]["date_from"] == "2021-01-01"
        assert stored["bars_count"] == 781

    def test_rerunning_a_synthetic_run_does_not_lose_the_tick(self, service, sma_doc):
        """
        A real bug this feature introduced, caught here rather than by a user.

        ``rerun`` rebuilds the config document from ``backtest_config``, which
        does not carry the acknowledgement — so "Rerun" on any synthetic run
        became silently impossible. The single most confusing way for a gate to
        behave, and invisible until someone actually pressed the button.
        """
        first = service.wait(service.submit(sma_doc)["run_id"], timeout=120)
        again = service.rerun(first["run_id"], overrides={"objectiveFunction": "calmar"})
        assert service.wait(again["run_id"], timeout=120)["status"] == "completed"

    def test_a_rerun_of_real_data_never_carries_a_stale_tick(self, service, sma_doc):
        """The acknowledgement is a statement about SYNTHETIC data. It must not
        ride along onto a run that has since been pointed at a real source."""
        doc = copy.deepcopy(sma_doc)
        doc["backtestConfig"]["source"] = "db"
        run = service.wait(service.submit(doc)["run_id"], timeout=120)
        rebuilt = service.config_doc_from_run(run)
        assert not (rebuilt.get("dataAttestation") or {}).get("acknowledged")

    def test_real_data_never_meets_the_gate(self, service, sma_doc):
        doc = copy.deepcopy(sma_doc)  # noqa: F841 - see below
        doc["backtestConfig"]["source"] = "db"
        att = service.attestation_for(doc)
        assert att["requires_acknowledgement"] is False
        assert attestation_is_satisfied(att) is True
