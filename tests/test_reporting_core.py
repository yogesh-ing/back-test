"""PRD-002 core — tax, records, consolidation and reconciliation.

These tests pin the decisions that would be dangerous to regress, not the
arithmetic of every helper:

* the *category* a trade lands in (F&O/intraday = business income, delivery
  splits on the 12-month rule, paper never enters a return, unknown is
  fail-closed);
* the *base* the rate is applied to (STT deductible for business income,
  excluded for capital gains) and the LTCG exemption;
* losses produce a carry-forward, never a negative tax;
* one trade found twice is counted once, and a source that dies takes only
  its own rows with it;
* reconciliation tolerance bands and the "no trades at all" verdict.
"""

from __future__ import annotations

import textwrap
from datetime import date, datetime
from decimal import Decimal

import pytest

from backtest.reporting.consolidator import ConsolidatedPnL
from backtest.reporting.records import (
    Period,
    TradeRecord,
    estimate_round_trip_fees,
    instrument_from_symbol,
)
from backtest.reporting.reconciliation import (
    BrokerReconciliation,
    load_reconciliation_thresholds,
)
from backtest.reporting.tax import (
    BusinessBase,
    TaxCategory,
    audit_position,
    categorize_trade,
    estimate_tax,
    fno_turnover,
    is_long_term,
    load_tax_rules,
    turnover_by_category,
)
from backtest.simulator.fees import FeeBreakdown, TradeSegment

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def dec(value) -> Decimal:
    return Decimal(str(value))


#: Exit stamps reused across fixtures — all inside June 2026.
JUN_2 = datetime(2026, 6, 2, 15, 0)
JUN_3 = datetime(2026, 6, 3, 15, 0)
JUN_4 = datetime(2026, 6, 4, 15, 0)


def trade(**kwargs) -> TradeRecord:
    """A closed round trip with sane defaults for the field under test."""
    kwargs.setdefault("broker", "mstock")
    kwargs.setdefault("mode", "live")
    kwargs.setdefault("segment", TradeSegment.EQUITY_DELIVERY)
    kwargs.setdefault("symbol", "INFY")
    kwargs.setdefault("quantity", dec(10))
    kwargs.setdefault("entry_price", dec(100))
    kwargs.setdefault("exit_price", dec(110))
    kwargs.setdefault("entry_time", datetime(2026, 5, 4, 10, 0))
    kwargs.setdefault("exit_time", datetime(2026, 5, 4, 15, 0))
    kwargs.setdefault("gross_pnl", dec(100))
    return TradeRecord(**kwargs)


class StaticSource:
    """A source that hands back exactly the records it was given."""

    name = "static"

    def __init__(self, records, *, fail: bool = False):
        self.records = list(records)
        self.fail = fail
        self.seen_periods = []

    def fetch(self, period):
        self.seen_periods.append(period)
        if self.fail:
            raise RuntimeError("source exploded")
        return [r for r in self.records if period.contains(r.exit_time)]

    def describe(self):
        return {"kind": "static", "name": self.name, "note": f"{len(self.records)} record(s)"}


@pytest.fixture()
def reporting_config(tmp_path, monkeypatch):
    """Point the reporting config at a throwaway yaml and reset the caches."""
    path = tmp_path / "reporting.yaml"
    path.write_text(
        textwrap.dedent(
            """
            tax:
              stcg_rate: 0.20
              ltcg_rate: 0.125
              ltcg_exemption: 125000
              ltcg_holding_months: 12
              business_slab_rate: 0.30
              cess_rate: 0.04
              surcharge_rate: 0.0
            reconciliation:
              pass_tolerance: 10
              warn_tolerance: 100
              warn_pct: 1.0
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("REPORTING_CONFIG_PATH", str(path))
    yield path
    monkeypatch.delenv("REPORTING_CONFIG_PATH", raising=False)


# ---------------------------------------------------------------------------
# Periods
# ---------------------------------------------------------------------------


def test_period_parse_bounds_and_financial_year():
    period = Period.parse("2026-04-01", "2026-09-30")
    assert (period.start, period.end) == (date(2026, 4, 1), date(2026, 9, 30))
    assert period.contains(datetime(2026, 9, 30, 23, 59))
    assert not period.contains(datetime(2026, 10, 1))
    assert not period.contains(None)
    assert period.days() == 183

    fy = Period.financial_year(date(2026, 2, 14))
    assert (fy.start, fy.end) == (date(2025, 4, 1), date(2026, 3, 31))
    ytd = Period.financial_year_to_date(date(2026, 9, 30))
    assert (ytd.start, ytd.end) == (date(2026, 4, 1), date(2026, 9, 30))


def test_period_parse_rejects_inverted_and_bad_dates():
    with pytest.raises(ValueError):
        Period.parse("2026-09-30", "2026-04-01")
    # A typo must not silently become "financial year to date".
    with pytest.raises(ValueError):
        Period.parse("not-a-date", "2026-04-01")
    with pytest.raises(ValueError):
        Period.parse("2026-04-01", "31/03/2027")


def test_period_previous_month_crosses_the_year_boundary():
    period = Period.previous_month(date(2026, 1, 14))
    assert (period.start, period.end) == (date(2025, 12, 1), date(2025, 12, 31))


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "segment,mode,expected",
    [
        (TradeSegment.FUTURES, "live", TaxCategory.FNO_NON_SPECULATIVE),
        (TradeSegment.OPTIONS, "live", TaxCategory.FNO_NON_SPECULATIVE),
        (TradeSegment.EQUITY_INTRADAY, "live", TaxCategory.EQUITY_SPECULATIVE),
        (TradeSegment.EQUITY_DELIVERY, "live", TaxCategory.EQUITY_STCG),
        (TradeSegment.EQUITY_DELIVERY, "paper", TaxCategory.PAPER_EXCLUDED),
        (TradeSegment.FUTURES, "paper", TaxCategory.PAPER_EXCLUDED),
        ("nonsense", "live", TaxCategory.UNCLASSIFIED),
    ],
)
def test_categorize_trade(segment, mode, expected):
    assert (
        categorize_trade(
            segment=segment,
            mode=mode,
            entry_date=date(2026, 5, 1),
            exit_date=date(2026, 6, 1),
        )
        is expected
    )


def test_delivery_holding_period_boundary_is_strictly_more_than_12_months():
    assert not is_long_term(date(2026, 1, 15), date(2027, 1, 15))
    assert is_long_term(date(2026, 1, 15), date(2027, 1, 16))
    assert (
        categorize_trade(
            segment=TradeSegment.EQUITY_DELIVERY,
            mode="live",
            entry_date=date(2025, 6, 1),
            exit_date=date(2026, 8, 1),
        )
        is TaxCategory.EQUITY_LTCG
    )


def test_instrument_inference_from_symbol():
    assert instrument_from_symbol("NIFTY24NOV22000CE") == "options"
    assert instrument_from_symbol("RELIANCE26FEB2700PE") == "options"
    assert instrument_from_symbol("NIFTY26FEBFUT") == "futures"
    assert instrument_from_symbol("INFY") == "equity"
    assert instrument_from_symbol(None) == "equity"


# ---------------------------------------------------------------------------
# Tax rules
# ---------------------------------------------------------------------------


def test_tax_rules_come_from_the_config_file(reporting_config):
    rules = load_tax_rules()
    assert rules.stcg_rate == dec("0.20")
    assert rules.ltcg_rate == dec("0.125")
    assert rules.ltcg_exemption == dec("125000")
    assert rules.business_slab_rate == dec("0.30")
    assert "Estimate only" in rules.disclaimer


def test_tax_rules_fall_back_to_law_defaults_when_unconfigured(monkeypatch, tmp_path):
    monkeypatch.setenv("REPORTING_CONFIG_PATH", str(tmp_path / "missing.yaml"))
    rules = load_tax_rules()
    assert rules.ltcg_rate == dec("0.125")
    assert rules.ltcg_exemption == dec("125000")


# ---------------------------------------------------------------------------
# The estimate itself
# ---------------------------------------------------------------------------


def test_capital_gains_are_taxed_without_the_stt_deduction():
    """STT is not a transfer expense for Schedule CG (proviso to s.48)."""
    base = BusinessBase(
        gross=dec(100000), costs=dec(1000), deductible_costs=dec(400),
        taxable_base=dec(99600), net=dec(99000), trade_ids=["t1"],
    )
    estimate = estimate_tax({TaxCategory.EQUITY_STCG: base})
    line = next(line for line in estimate.lines if line.category is TaxCategory.EQUITY_STCG)
    assert line.taxable_base == dec("99600")
    assert line.tax == dec("19920.00")
    assert "not deductible" in line.note
    assert estimate.total == dec("20716.80")  # 19920 * 1.04 cess


def test_business_income_is_taxed_on_the_net_after_every_cost():
    base = BusinessBase(
        gross=dec(100000), costs=dec(5000), deductible_costs=dec(5000),
        taxable_base=dec(95000), net=dec(95000), trade_ids=["t1"],
    )
    estimate = estimate_tax({TaxCategory.FNO_NON_SPECULATIVE: base})
    line = next(line for line in estimate.lines if line.category.is_business_income)
    assert line.taxable_base == dec("95000")
    assert line.tax == dec("28500.00")
    assert line.rate == dec("0.30")


def test_ltcg_gets_the_annual_exemption_and_reports_how_much_it_used():
    base = BusinessBase(
        gross=dec(200000), costs=dec(0), deductible_costs=dec(0),
        taxable_base=dec(200000), net=dec(200000), trade_ids=["t1"],
    )
    estimate = estimate_tax({TaxCategory.EQUITY_LTCG: base})
    line = estimate.lines[0]
    assert line.taxable_base == dec("75000")  # 200000 - 125000
    assert line.tax == dec("9375.00")
    assert estimate.ltcg_exemption_used == dec("125000")
    assert "exemption" in line.note


def test_ltcg_below_the_exemption_is_tax_free():
    base = BusinessBase(
        gross=dec(90000), costs=dec(0), deductible_costs=dec(0),
        taxable_base=dec(90000), net=dec(90000), trade_ids=["t1"],
    )
    estimate = estimate_tax({TaxCategory.EQUITY_LTCG: base})
    assert estimate.total == dec("0.00")
    assert estimate.ltcg_exemption_used == dec("90000")


def test_losses_become_a_carry_forward_not_a_refund():
    base = BusinessBase(
        gross=dec(-40000), costs=dec(500), deductible_costs=dec(500),
        taxable_base=dec(-40500), net=dec(-40500), trade_ids=["t1"],
    )
    estimate = estimate_tax({TaxCategory.EQUITY_SPECULATIVE: base})
    assert estimate.total == dec("0.00")
    carry = estimate.carry_forward[0]
    assert carry["category"] == TaxCategory.EQUITY_SPECULATIVE.value
    assert carry["amount"] == 40500.0
    assert "4" in carry["rule"]


def test_paper_is_excluded_from_the_estimate_but_still_visible():
    base = BusinessBase(
        gross=dec(500000), costs=dec(0), deductible_costs=dec(0),
        taxable_base=dec(500000), net=dec(500000), trade_ids=["t1"],
    )
    estimate = estimate_tax({TaxCategory.PAPER_EXCLUDED: base})
    assert estimate.total == dec("0.00")
    line = estimate.lines[0]
    assert line.taxable_base == dec("0") and line.rate == dec("0")
    assert "excluded" in line.note.lower()


def test_unclassified_trades_are_flagged_not_guessed():
    base = BusinessBase(gross=dec(1000), net=dec(1000), taxable_base=dec(1000), trade_ids=["t1"])
    estimate = estimate_tax({TaxCategory.UNCLASSIFIED: base})
    assert estimate.total == dec("0.00")
    assert not estimate.lines
    assert any("UNCLASSIFIED" in note for note in estimate.notes)


# ---------------------------------------------------------------------------
# Turnover & audit
# ---------------------------------------------------------------------------


def test_fno_turnover_uses_the_icai_absolute_pnl_method():
    trades = [
        trade(segment=TradeSegment.FUTURES, gross_pnl=dec(6000)),
        trade(segment=TradeSegment.OPTIONS, gross_pnl=dec(-2500)),
        trade(segment=TradeSegment.EQUITY_DELIVERY, gross_pnl=dec(9000)),  # not F&O
    ]
    assert fno_turnover(trades) == dec("8500")
    # Unclassified records are still classified from their segment — dropping
    # them would understate the number the audit threshold is measured on.
    by_category = turnover_by_category(trades)
    assert by_category[TaxCategory.FNO_NON_SPECULATIVE] == dec("8500")
    assert by_category[TaxCategory.EQUITY_STCG] == dec("9000")


def test_audit_position_flags_the_threshold_and_the_digital_assumption():
    small = audit_position(dec(100000))
    assert small.audit_required is False
    assert small.turnover_all_digital is True
    assert small.presumptive_eligible is True

    large = audit_position(dec(12000000))
    assert large.audit_required is True
    assert "REVIEW" in large.to_dict()["note"] or "threshold" in large.to_dict()["note"]


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


def test_net_pnl_prefers_the_source_when_it_is_authoritative():
    derived = trade(gross_pnl=dec(100), net_pnl_recorded=dec(50), net_authoritative=False)
    assert derived.net_pnl == dec("100")  # 100 gross - 0 fees - 0 slippage
    authoritative = trade(
        gross_pnl=dec(100), net_pnl_recorded=dec(95), net_authoritative=True,
    )
    assert authoritative.net_pnl == dec("95")
    assert authoritative.ladder_residual == dec("5")


def test_ladder_residual_is_zero_when_the_numbers_agree():
    record = trade(
        gross_pnl=dec(100),
        fees=FeeBreakdown(components={"stt": dec(3), "brokerage": dec(2)}),
        slippage=dec(0),
        net_pnl_recorded=dec(95),
        net_authoritative=True,
    )
    assert record.fees_total == dec("5")
    assert record.net_pnl == dec("95")
    assert record.ladder_residual == dec("0")


def test_fee_estimation_is_flagged_as_estimated_and_never_silent():
    fees = estimate_round_trip_fees(
        segment=TradeSegment.EQUITY_DELIVERY,
        broker="some-unknown-broker",
        quantity=dec(100),
        entry_price=dec(1500),
        exit_price=dec(1510),
    )
    assert fees.total > 0
    # A delivery round trip pays stamp duty on the buy and STT on both sides.
    assert fees.get("stt") > 0
    assert fees.get("stamp_duty") > 0

    record = trade(gross_pnl=dec(1000), fees=fees, fees_basis="estimated")
    assert record.net_pnl == record.gross_pnl - record.fees_total
    assert record.net_pnl < record.gross_pnl


def test_a_known_broker_gets_its_own_schedule_not_the_zero_fallback():
    mstock = estimate_round_trip_fees(
        segment=TradeSegment.EQUITY_INTRADAY,
        broker="mstock",
        quantity=dec(100),
        entry_price=dec(1500),
        exit_price=dec(1510),
    )
    unknown = estimate_round_trip_fees(
        segment=TradeSegment.EQUITY_INTRADAY,
        broker="entirely-unknown-broker",
        quantity=dec(100),
        entry_price=dec(1500),
        exit_price=dec(1510),
    )
    assert mstock.total > 0 and unknown.total > 0
    # The fallback profile charges no brokerage, so its stack is never larger.
    assert unknown.total <= mstock.total * dec("2")


def test_unknown_segment_is_kept_and_marked():
    record = trade(segment="exotic_swaps")
    assert record.segment == "exotic_swaps"
    assert any("UNCLASSIFIED" in note for note in record.notes)


# ---------------------------------------------------------------------------
# Consolidation
# ---------------------------------------------------------------------------


def test_report_sums_a_mixed_book_and_classifies_every_trade():
    records = [
        trade(
            trade_id="fno", symbol="NIFTY26JUNFUT", segment=TradeSegment.FUTURES,
            gross_pnl=dec(4000), exit_time=datetime(2026, 6, 25, 15, 0),
        ),
        trade(
            trade_id="intraday", symbol="SBIN", segment=TradeSegment.EQUITY_INTRADAY,
            gross_pnl=dec(1500), exit_time=datetime(2026, 6, 20, 15, 0),
        ),
        trade(
            trade_id="delivery", symbol="TCS", segment=TradeSegment.EQUITY_DELIVERY,
            gross_pnl=dec(2500), exit_time=datetime(2026, 6, 15, 15, 0),
        ),
        trade(
            trade_id="paper", symbol="ITC", mode="paper", segment=TradeSegment.EQUITY_DELIVERY,
            gross_pnl=dec(9000), exit_time=datetime(2026, 6, 10, 15, 0),
        ),
    ]
    report = ConsolidatedPnL(sources=[StaticSource(records)]).generate_report(
        "2026-06-01", "2026-06-30"
    )
    assert report.trade_count == 4
    assert report.gross_pnl == dec("17000")
    assert report.net_pnl == dec("17000")
    by_category = {row.category: row for row in report.by_category}
    assert by_category[TaxCategory.FNO_NON_SPECULATIVE].net_pnl == dec("4000")
    assert by_category[TaxCategory.PAPER_EXCLUDED].net_pnl == dec("9000")
    # business 4000 + 1500 at 30%, capital gain 2500 at 20%, then 4% cess
    assert report.tax.total == dec("2236.00")
    assert report.net_after_tax == dec("17000") - report.tax.total


def test_paper_trades_can_be_left_out_entirely_and_never_enter_tax():
    records = [
        trade(trade_id="live", gross_pnl=dec(1000), exit_time=datetime(2026, 6, 20, 15, 0)),
        trade(
            trade_id="paper", mode="paper", gross_pnl=dec(5000),
            exit_time=datetime(2026, 6, 10, 15, 0),
        ),
    ]
    consolidator = ConsolidatedPnL(sources=[StaticSource(records)])
    excluded = consolidator.generate_report("2026-06-01", "2026-06-30", include_paper=False)
    assert excluded.trade_count == 1
    assert excluded.paper_net_pnl == dec("0")
    assert excluded.tax.total == dec("208.00")  # delivery gain 1000 * 20% * 1.04

    included = consolidator.generate_report("2026-06-01", "2026-06-30", include_paper=True)
    assert included.trade_count == 2
    assert included.tax.total == dec("208.00")  # paper still not taxed
    paper_rows = [row for row in included.by_mode if row.broker == "paper"]
    assert paper_rows and paper_rows[0].net_pnl == dec("5000")
    # Grouped rows serialise under their own dimension, not as a fake broker.
    modes = [row["mode"] for row in included.to_dict()["by_mode"]]
    assert set(modes) == {"live", "paper"}
    assert "broker" in included.to_dict()["by_broker"][0]


def test_broker_filter_and_grouping():
    records = [
        trade(trade_id="a", broker="mstock", gross_pnl=dec(100), exit_time=JUN_2),
        trade(trade_id="b", broker="dhan", gross_pnl=dec(300), exit_time=JUN_3),
        trade(trade_id="c", broker="mstock", gross_pnl=dec(-50), exit_time=JUN_4),
    ]
    consolidator = ConsolidatedPnL(sources=[StaticSource(records)])
    report = consolidator.generate_report("2026-06-01", "2026-06-30")
    assert {row.broker: row.net_pnl for row in report.by_broker} == {
        "mstock": dec("50"),
        "dhan": dec("300"),
    }
    assert sum(row.share_pct for row in report.by_broker) == pytest.approx(100.0)

    filtered = consolidator.generate_report("2026-06-01", "2026-06-30", brokers=["dhan"])
    assert filtered.trade_count == 1
    assert filtered.gross_pnl == dec("300")


def test_the_same_trade_from_two_sources_is_counted_once():
    record = trade(trade_id="dup", gross_pnl=dec(1000), exit_time=JUN_2)
    twin = trade(trade_id="dup2", gross_pnl=dec(1000), exit_time=JUN_2)
    report = ConsolidatedPnL(
        sources=[StaticSource([record]), StaticSource([twin])]
    ).generate_report("2026-06-01", "2026-06-30")
    assert report.trade_count == 1
    assert report.gross_pnl == dec("1000")
    merged = " ".join(report.data_notes).lower()
    assert "duplicate" in merged or "merged" in merged


def test_a_dead_source_is_reported_but_does_not_sink_the_report():
    good = trade(gross_pnl=dec(1000), exit_time=datetime(2026, 6, 2, 15, 0))
    report = ConsolidatedPnL(
        sources=[StaticSource([good]), StaticSource([], fail=True)]
    ).generate_report("2026-06-01", "2026-06-30")
    assert report.trade_count == 1
    assert any("failed" in warning for warning in report.warnings)


def test_empty_book_still_returns_a_well_formed_report():
    report = ConsolidatedPnL(sources=[StaticSource([])]).generate_report("2026-06-01", "2026-06-30")
    assert report.trade_count == 0
    assert report.tax.total == dec("0.00")
    summary = report.summary()
    assert summary["net_pnl"] == 0.0
    assert report.win_rate == 0.0


def test_report_dict_is_json_ready_and_carries_its_caveats():
    record = trade(gross_pnl=dec(1000), exit_time=JUN_2)
    report = ConsolidatedPnL(sources=[StaticSource([record])]).generate_report(
        "2026-06-01", "2026-06-30"
    )
    payload = report.to_dict()
    assert payload["summary"]["gross_pnl"] == 1000.0
    assert payload["trades"][0]["tax_category"] == TaxCategory.EQUITY_STCG.value
    assert payload["tax"]["rules"]["stcg_rate"]
    assert payload["period"]["start"] == "2026-06-01"
    assert payload["sources"][0]["name"] == "static"
    without = report.to_dict(include_trades=False)
    assert "trades" not in without  # omitted, not an empty list that reads as "no trades"


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


def _report_with_broker():
    records = [
        trade(trade_id="a", broker="mstock", gross_pnl=dec(1000), exit_time=JUN_2),
    ]
    return ConsolidatedPnL(sources=[StaticSource(records)]).generate_report(
        "2026-06-01", "2026-06-30"
    )


def test_thresholds_load_from_the_config_file(reporting_config):
    thresholds = load_reconciliation_thresholds()
    assert thresholds["pass_tolerance"] == dec("10")
    assert thresholds["warn_tolerance"] == dec("100")
    assert thresholds["warn_pct"] == dec("1.0")


def test_reconciliation_pass_warning_fail_bands(reporting_config):
    reconciler = BrokerReconciliation()
    report = _report_with_broker()

    exact = reconciler.reconcile(report, "mstock", contract_note_pnl=dec(1000))
    assert exact.status == "PASS"
    assert exact.difference == dec("0")

    near = reconciler.reconcile(report, "mstock", contract_note_pnl=dec(1050))
    assert near.status == "WARNING"

    far = reconciler.reconcile(report, "mstock", contract_note_pnl=dec(1300))
    assert far.status == "FAIL"
    assert far.likely_causes


def test_reconciliation_compares_fee_components_when_the_note_has_them(reporting_config):
    record = trade(
        broker="mstock",
        gross_pnl=dec(1000),
        fees=FeeBreakdown(
            components={"stt": dec(120), "brokerage": dec(40), "gst": dec(7.2)}
        ),
        exit_time=datetime(2026, 6, 2, 15, 0),
    )
    report = ConsolidatedPnL(sources=[StaticSource([record])]).generate_report(
        "2026-06-01", "2026-06-30"
    )
    reconciler = BrokerReconciliation()
    result = reconciler.reconcile(
        report,
        "mstock",
        contract_note_pnl=dec(1000),
        contract_note_fees={"stt": dec(150), "brokerage": dec(40)},
    )
    breakdown = result.to_dict()["component_breakdown"]
    assert breakdown["stt"]["difference"] == pytest.approx(-30.0)
    assert any("stt" in cause.lower() for cause in result.likely_causes)


def test_reconciliation_with_no_platform_trades_is_a_fail_with_a_reason(reporting_config):
    report = ConsolidatedPnL(sources=[StaticSource([])]).generate_report("2026-06-01", "2026-06-30")
    result = BrokerReconciliation().reconcile(report, "mstock", contract_note_pnl=dec(5000))
    assert result.status == "FAIL"
    assert result.likely_causes
    assert "no" in result.to_dict()["likely_causes"][0].lower()


def test_reconcile_report_handles_several_brokers_at_once(reporting_config):
    records = [
        trade(trade_id="a", broker="mstock", gross_pnl=dec(1000), exit_time=JUN_2),
        trade(trade_id="b", broker="dhan", gross_pnl=dec(500), exit_time=JUN_3),
    ]
    report = ConsolidatedPnL(sources=[StaticSource(records)]).generate_report(
        "2026-06-01", "2026-06-30"
    )
    results = BrokerReconciliation().reconcile_report(
        report, {"mstock": {"contract_note_pnl": 995}, "dhan": 500}
    )
    assert [r.status for r in results] == ["PASS", "PASS"]
    assert results[0].difference == dec("5")


def test_a_missing_note_pnl_is_an_input_error_not_a_zero_note(reporting_config):
    report = _report_with_broker()
    with pytest.raises(ValueError):
        BrokerReconciliation().reconcile(report, "mstock", contract_note_pnl=None)
    with pytest.raises(ValueError):
        BrokerReconciliation().reconcile_report(report, {"mstock": {"note_ref": "CN-1"}})


def test_consolidator_mixed_tz_awareness_paper_and_live():
    """Ensure mixed offset-naive and offset-aware exit_times sort without TypeError."""
    from datetime import timezone
    records = [
        TradeRecord(
            trade_id="aware_live",
            symbol="RELIANCE",
            mode="live",
            gross_pnl=Decimal("1500"),
            exit_time=datetime(2026, 6, 20, 15, 0, tzinfo=timezone.utc),
        ),
        TradeRecord(
            trade_id="naive_paper",
            symbol="INFY",
            mode="paper",
            gross_pnl=Decimal("800"),
            exit_time=datetime(2026, 6, 18, 14, 30),
        ),
        TradeRecord(
            trade_id="iso_str_exit",
            symbol="TCS",
            mode="paper",
            gross_pnl=Decimal("500"),
            exit_time=datetime(2026, 6, 10, 10, 0),
        ),
    ]
    report = ConsolidatedPnL(sources=[StaticSource(records)]).generate_report(
        "2026-06-01", "2026-06-30", include_paper=True
    )
    assert report.trade_count == 3
    assert [t.symbol for t in report.trades] == ["TCS", "INFY", "RELIANCE"]
