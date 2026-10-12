"""Data coverage — the symbol picker's answer (PRD backTest-enhance §1.3).

The complaint being fixed: symbols silently disappear when they have no
cached bars, indices are missing entirely, and nothing explains the gap. These
tests pin the three parts of that answer: the union (a symbol is listed if ANY
source knows it), the honesty (``data_available`` / ``hint`` say what choosing
it will do), and the degradation (no database still yields a usable picker).
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from backtest.data.base import CANONICAL_TIMEFRAMES
from backtest.data.coverage import (
    INDEX_UNIVERSE,
    INSTRUMENT_TYPES,
    NO_DATA_HINT,
    BarCoverage,
    build_coverage,
    classify_instrument,
    filter_coverage,
    index_universe,
    load_bar_coverage,
    load_catalogue,
    load_equity_universe,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_MDC_DDL = """
CREATE TABLE market_data_cache (
    data_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol    TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    ts        TEXT NOT NULL
)
"""

_INSTRUMENTS_DDL = """
CREATE TABLE instruments (
    instrument_token INTEGER PRIMARY KEY,
    tradingsymbol    TEXT NOT NULL,
    name             TEXT,
    instrument_type  TEXT,
    segment          TEXT,
    exchange         TEXT
)
"""


@pytest.fixture()
def engine(tmp_path):
    """A DB with a cache table but NO instruments catalogue (common offline)."""
    eng = create_engine(f"sqlite:///{tmp_path / 'cov.db'}", echo=False)
    with eng.connect() as conn:
        conn.execute(text(_MDC_DDL))
        conn.commit()
    yield eng
    eng.dispose()


@pytest.fixture()
def bar_engine(tmp_path):
    """A DB with BOTH tables: a cache with bars and a broker catalogue."""
    eng = create_engine(f"sqlite:///{tmp_path / 'bars.db'}", echo=False)
    with eng.connect() as conn:
        conn.execute(text(_MDC_DDL))
        conn.execute(text(_INSTRUMENTS_DDL))
        conn.commit()
    yield eng
    eng.dispose()


def _seed_bars(eng, rows):
    with eng.connect() as conn:
        for symbol, timeframe, ts in rows:
            conn.execute(
                text("INSERT INTO market_data_cache (symbol, timeframe, ts) VALUES (:s,:t,:ts)"),
                {"s": symbol, "t": timeframe, "ts": ts},
            )
        conn.commit()


def _seed_instruments(eng, rows):
    with eng.connect() as conn:
        for symbol, name, itype, segment, exchange in rows:
            conn.execute(
                text(
                    "INSERT INTO instruments (tradingsymbol, name, instrument_type, segment, "
                    "exchange) VALUES (:s,:n,:i,:g,:e)"
                ),
                {"s": symbol, "n": name, "i": itype, "g": segment, "e": exchange},
            )
        conn.commit()


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("symbol,name", INDEX_UNIVERSE)
def test_every_prd_index_is_recognised_as_an_index(symbol, name):
    assert classify_instrument(symbol) == "index"
    assert name  # the display name is what the picker shows


def test_prd_index_list_is_covered():
    """NIFTY 50, BANKNIFTY, SENSEX, NIFTY BANK, NIFTY MIDCAP 150, INDIA VIX."""
    known = {sym for sym, _ in INDEX_UNIVERSE}
    assert {"NIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY", "INDIAVIX"} <= known


def test_plain_nse_symbols_are_equities():
    for sym in ("RELIANCE", "TCS", "HDFCBANK", "360ONE", "INFY"):
        assert classify_instrument(sym) == "equity", sym


@pytest.mark.parametrize(
    "symbol,kind",
    [
        ("NIFTY25SEP24500CE", "options"),
        ("NIFTY25SEP24500PE", "options"),
        ("RELIANCE24500CE", "options"),  # no expiry token
        ("RELIANCE25SEP24500CE", "options"),
        ("BANKNIFTY25SEP2026FUT", "futures"),
        ("BANKNIFTY25SEP2026FUTNR", "futures"),
        ("NIFTYFUT", "futures"),
        ("NIFTY25SEPFUT", "futures"),
    ],
)
def test_contracts_are_classified_by_shape(symbol, kind):
    assert classify_instrument(symbol) == kind


def test_catalogue_type_wins_over_shape():
    """A catalogue that says EQ means equity even for an odd symbol."""
    assert classify_instrument("SOMETHING", "EQ", "NSE", "Something Ltd") == "equity"
    assert classify_instrument("NIFTY", "EQ", "NSE", "Nifty") == "index"  # universe wins


def test_unknown_symbol_falls_back_to_equity():
    """Never classify to nothing — a bare NSE symbol is a share."""
    assert classify_instrument("ZZZ") == "equity"
    assert classify_instrument("") == "equity"


def test_every_classification_is_one_of_the_four_declared_types():
    for sym in ("NIFTY", "RELIANCE", "NIFTY25SEP24500CE", "NIFTY25SEPFUT", "ZZZ"):
        assert classify_instrument(sym) in INSTRUMENT_TYPES


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


def test_index_universe_is_always_offered():
    rows = index_universe()
    assert len(rows) == len(INDEX_UNIVERSE)
    assert all(r["instrument_type"] == "index" for r in rows)
    assert all(r["symbol"] and r["name"] for r in rows)


def test_equity_universe_file_loads():
    rows = load_equity_universe()
    assert len(rows) > 150
    assert all(r["symbol"] and r["name"] for r in rows)
    assert all(r["symbol"] == r["symbol"].upper() for r in rows)


def test_missing_universe_file_degrades_to_empty(tmp_path):
    """A missing file must not 500 — the picker falls back to the indices."""
    assert load_equity_universe(tmp_path / "nope.csv") == []


def test_bar_coverage_aggregates_and_orders_timeframes_finest_first(bar_engine):
    _seed_bars(
        bar_engine,
        [
            ("RELIANCE", "1day", "2024-01-05"),
            ("RELIANCE", "1day", "2024-01-06"),
            ("RELIANCE", "1day", "2024-01-07"),
            ("RELIANCE", "1min", "2024-01-05 09:15"),
            ("RELIANCE", "1min", "2024-01-05 09:16"),
            ("RELIANCE", "1min", "2024-01-05 09:17"),
            ("TCS", "1day", "2024-02-01"),
        ],
    )
    cov = load_bar_coverage(bar_engine)
    assert set(cov) == {"RELIANCE", "TCS"}
    assert cov["RELIANCE"].bars_count == 6
    assert cov["RELIANCE"].data_available is True
    # finest first, so a picker never offers 1D before the 1min that exists
    assert cov["RELIANCE"].timeframes == ["1min", "1day"]
    assert cov["RELIANCE"].from_date == "2024-01-05"
    assert cov["RELIANCE"].to_date == "2024-01-07"
    assert cov["TCS"].bars_count == 1


def test_timeframes_are_ordered_by_the_canonical_vocabulary(bar_engine):
    _seed_bars(
        bar_engine,
        [
            ("INFY", "1week", "2024-01-05"),
            ("INFY", "1hour", "2024-01-05 10:00"),
            ("INFY", "1day", "2024-01-05"),
            ("INFY", "5min", "2024-01-05 09:15"),
        ],
    )
    cov = load_bar_coverage(bar_engine)
    order = [tf for tf in cov["INFY"].timeframes]
    assert order == sorted(order, key=CANONICAL_TIMEFRAMES.index)


def test_bar_coverage_on_an_empty_table_is_empty_not_an_error(bar_engine):
    assert load_bar_coverage(bar_engine) == {}


def test_catalogue_reads_instruments(bar_engine):
    _seed_instruments(
        bar_engine,
        [
            ("RELIANCE", "Reliance Industries", "EQ", "NSE", "NSE"),
            ("NIFTY25SEP24500CE", "NIFTY 24500 CE", "CE", "OPTIDX", "NFO"),
            ("BANKNIFTY25SEP2026FUT", "BANKNIFTY FUT", "FUT", "FUTIDX", "NFO"),
        ],
    )
    rows = load_catalogue(bar_engine)
    by_symbol = {r["symbol"]: r for r in rows}
    assert by_symbol["RELIANCE"]["name"] == "Reliance Industries"
    assert by_symbol["RELIANCE"]["instrument_type"] == "equity"
    assert by_symbol["NIFTY25SEP24500CE"]["instrument_type"] == "options"
    assert by_symbol["BANKNIFTY25SEP2026FUT"]["instrument_type"] == "futures"


def test_absent_catalogue_table_is_not_an_error(engine):
    """The instruments table comes from a broker ingest, not a migration."""
    assert load_catalogue(engine) == []


# ---------------------------------------------------------------------------
# The union
# ---------------------------------------------------------------------------


def _by_symbol(report):
    return {r["symbol"]: r for r in report.instruments}


def test_symbols_with_no_data_are_still_listed():
    """The core of §1.3: a missing symbol is a prompt, not a disappearance."""
    report = build_coverage(universe=load_equity_universe())
    rows = _by_symbol(report)
    assert "RELIANCE" in rows
    assert rows["RELIANCE"]["data_available"] is False
    assert rows["RELIANCE"]["hint"] == NO_DATA_HINT
    assert rows["RELIANCE"]["bars_count"] == 0
    assert rows["RELIANCE"]["timeframes_available"] == []


def test_indices_are_listed_with_no_database_at_all():
    report = build_coverage()
    rows = _by_symbol(report)
    for symbol, _ in INDEX_UNIVERSE:
        assert symbol in rows, symbol
        assert rows[symbol]["instrument_type"] == "index"
        assert rows[symbol]["data_available"] is False


def test_a_symbol_with_bars_is_not_hinted():
    report = build_coverage(
        bars={"RELIANCE": BarCoverage(1247, "2020-01-01", "2024-12-31", ["1day"])}
    )
    row = _by_symbol(report)["RELIANCE"]
    assert row["data_available"] is True
    assert "hint" not in row, "a symbol that can be run must not carry a fetch prompt"
    assert row["bars_count"] == 1247
    assert row["timeframes_available"] == ["1day"]


def test_the_union_keeps_a_catalogue_symbol_that_has_no_bars(bar_engine):
    _seed_instruments(bar_engine, [("ZEE", "Zee Entertainment", "EQ", "NSE", "NSE")])
    _seed_bars(bar_engine, [("RELIANCE", "1day", "2024-01-01")])
    report = build_coverage(
        bars=load_bar_coverage(bar_engine), catalogue=load_catalogue(bar_engine)
    )
    rows = _by_symbol(report)
    assert rows["ZEE"]["data_available"] is False and rows["ZEE"]["hint"] == NO_DATA_HINT
    assert rows["RELIANCE"]["data_available"] is True
    assert rows["RELIANCE"]["symbol"] not in rows["ZEE"]["symbol"]


def test_bars_add_a_symbol_no_catalogue_mentions():
    report = build_coverage(bars={"ZZZNEW": BarCoverage(10, "2024-01-01", "2024-02-01", ["1min"])})
    assert _by_symbol(report)["ZZZNEW"]["data_available"] is True


def test_catalogue_name_wins_over_a_bare_symbol():
    report = build_coverage(
        bars={"RELIANCE": BarCoverage(5, "2024-01-01", "2024-01-05", ["1day"])},
        catalogue=[{"symbol": "RELIANCE", "name": "Reliance Industries Ltd."}],
    )
    assert _by_symbol(report)["RELIANCE"]["name"] == "Reliance Industries Ltd."


def test_derivatives_mark_their_underlying_not_a_second_row():
    report = build_coverage(
        catalogue=[
            {"symbol": "RELIANCE25SEP24500CE", "name": "24500 CE"},
            {"symbol": "RELIANCE24500PE", "name": "24500 PE"},
            {"symbol": "BANKNIFTY25SEP2026FUT", "name": "FUT"},
        ]
    )
    rows = _by_symbol(report)
    assert rows["RELIANCE"]["has_options"] is True
    assert rows["RELIANCE"]["has_futures"] is False
    assert rows["BANKNIFTY"]["has_futures"] is True
    assert rows["BANKNIFTY"]["has_options"] is False
    # the contracts themselves are listed too — a user may have their bars
    assert rows["RELIANCE25SEP24500CE"]["instrument_type"] == "options"


def test_a_contract_row_names_its_underlying():
    report = build_coverage(catalogue=[{"symbol": "NIFTY25SEP24500CE", "name": "x"}])
    assert _by_symbol(report)["NIFTY25SEP24500CE"]["underlying"] == "NIFTY"


def test_db_availability_is_reported_explicitly():
    """It is the caller's claim about the database, never inferred from rows."""
    assert build_coverage().db_available is False
    assert build_coverage(bars={"X": BarCoverage(1)}).db_available is False
    assert build_coverage(bars={"X": BarCoverage(1)}, db_available=True).db_available is True


def test_db_availability_can_be_unknown():
    """None = "not asked", distinct from False = "asked, not there".

    The names-only list never opens the database, and the picker renders
    ``db_available === false`` as "no data source connected" — so reporting the
    default False there would state something we never checked.
    """
    report = build_coverage(db_available=None, coverage_known=False)
    assert report.db_available is None
    assert report.db_available is not False


# ---------------------------------------------------------------------------
# Names-only mode — the static instrument list
# ---------------------------------------------------------------------------


def test_names_only_rows_are_not_claimed_to_lack_data():
    """`data_available` is None, never False: nobody looked.

    False would mean "has no bars" and would grey out the whole dropdown for a
    catalogue whose bars were simply not counted.
    """
    report = build_coverage(universe=load_equity_universe(), coverage_known=False)
    rows = _by_symbol(report)
    assert "RELIANCE" in rows
    assert rows["RELIANCE"]["data_available"] is None
    assert rows["RELIANCE"]["coverage_known"] is False


def test_names_only_rows_never_carry_the_fetch_prompt():
    """NO_DATA_HINT sends the user to fetch data. Without a lookup we do not
    know they need to, so the prompt must be absent."""
    report = build_coverage(universe=load_equity_universe(), coverage_known=False)
    for row in report.instruments:
        assert "hint" not in row, row["symbol"]


def test_names_only_rows_still_carry_every_key_consumers_index():
    """Shape stability: consumers read these without checking, and a missing
    key would surface as `undefined` instead of as "not asked"."""
    report = build_coverage(universe=load_equity_universe(), coverage_known=False)
    row = _by_symbol(report)["RELIANCE"]
    for key in ("symbol", "name", "instrument_type", "exchange", "curated",
                "data_available", "bars_count", "from_date", "to_date",
                "timeframes_stored", "timeframes_available",
                "bars_by_timeframe", "coverage_known"):
        assert key in row, key
    assert row["bars_count"] is None
    assert row["from_date"] is None and row["to_date"] is None
    assert row["timeframes_stored"] == [] and row["timeframes_available"] == []


def test_names_only_ignores_bars_even_when_they_are_supplied():
    """The flag is the contract, not the inputs: a caller that says "names
    only" gets no coverage, so a stale cache of bars cannot leak counts into a
    list that promised not to have them."""
    report = build_coverage(
        bars={"RELIANCE": BarCoverage(1247, "2020-01-01", "2024-12-31", ["1min"])},
        universe=load_equity_universe(),
        coverage_known=False,
    )
    row = _by_symbol(report)["RELIANCE"]
    assert row["bars_count"] is None
    assert row["data_available"] is None


def test_full_mode_still_marks_known_coverage():
    """The other side of the flag: coverage_known=True must keep saying so, or
    a consumer cannot tell the two modes apart."""
    report = build_coverage(bars={"RELIANCE": BarCoverage(1247)})
    row = _by_symbol(report)["RELIANCE"]
    assert row["coverage_known"] is True
    assert row["data_available"] is True
    assert row["bars_count"] == 1247


# ---------------------------------------------------------------------------
# Search / filter / page
# ---------------------------------------------------------------------------


@pytest.fixture()
def report():
    return build_coverage(
        bars={
            "RELIANCE": BarCoverage(100, "2024-01-01", "2024-06-01", ["1day"]),
            "NIFTY": BarCoverage(120, "2024-01-01", "2024-06-01", ["1day", "1min"]),
        },
        catalogue=[
            {"symbol": "RELIANCE25SEP24500CE", "name": "Reliance 24500 CE"},
            {"symbol": "TCS25SEP4000FUT", "name": "TCS FUT"},
        ],
        universe=[{"symbol": "ZEE", "name": "Zee Entertainment"}],
    )


def test_all_is_the_default(report):
    rows, total = filter_coverage(report)
    assert total == report.total


def test_index_tab(report):
    rows, _ = filter_coverage(report, types=["index"])
    assert {r["symbol"] for r in rows} >= {"NIFTY", "BANKNIFTY", "SENSEX", "INDIAVIX"}


def test_equity_tab_excludes_contracts(report):
    """Underlyings are equities; the option/future contracts are not."""
    rows, _ = filter_coverage(report, types=["equity"])
    assert {r["symbol"] for r in rows} == {"RELIANCE", "TCS", "ZEE"}
    assert "RELIANCE25SEP24500CE" not in {r["symbol"] for r in rows}


def test_a_listed_derivative_creates_its_underlying():
    """The exchange listing a TCS future is itself the claim that TCS trades."""
    rep = build_coverage(catalogue=[{"symbol": "TCS25SEP4000FUT", "name": "TCS FUT"}])
    rows = _by_symbol(rep)
    assert rows["TCS"]["instrument_type"] == "equity"
    assert rows["TCS"]["has_futures"] is True


def test_fno_tab_is_underlyings_with_derivatives(report):
    """The F&O tab is what the PRD's filter buttons mean by "F&O"."""
    rows, _ = filter_coverage(report, types=["fno"])
    assert {r["symbol"] for r in rows} == {"RELIANCE", "TCS"}


def test_types_can_be_combined(report):
    rows, _ = filter_coverage(report, types=["index", "equity"])
    assert "NIFTY" in {r["symbol"] for r in rows}
    assert "ZEE" in {r["symbol"] for r in rows}
    assert "TCS25SEP4000FUT" not in {r["symbol"] for r in rows}


def test_search_matches_symbol_or_name(report):
    rows, _ = filter_coverage(report, query="reliance")
    assert {r["symbol"] for r in rows} == {"RELIANCE", "RELIANCE25SEP24500CE"}
    rows, _ = filter_coverage(report, query="Zee")
    assert {r["symbol"] for r in rows} == {"ZEE"}
    assert filter_coverage(report, query="NOTHINGMATCHES")[1] == 0


def test_available_filter_drops_symbols_with_no_bars(report):
    rows, total = filter_coverage(report, available_only=True)
    assert total == 2
    assert {r["symbol"] for r in rows} == {"RELIANCE", "NIFTY"}


def test_paging_reports_the_unpaged_total(report):
    page1, total = filter_coverage(report, limit=2, offset=0)
    page2, _ = filter_coverage(report, limit=2, offset=2)
    assert total == report.total > 4
    assert len(page1) == len(page2) == 2
    assert {r["symbol"] for r in page1}.isdisjoint({r["symbol"] for r in page2})


def test_filters_compose(report):
    rows, total = filter_coverage(report, types=["fno"], available_only=True, query="RELIANCE")
    assert total == 1 and rows[0]["symbol"] == "RELIANCE"
