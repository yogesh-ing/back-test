"""``GET /api/data/coverage`` — the shared symbol-picker endpoint (PRD §1.3).

Two properties matter most and are pinned here: the picker is NEVER empty
(an app with no database still offers the indices and the shipped universe,
marked as having no data), and it never claims a symbol has data it does not.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from backtest.api import data_manager
from backtest.web.app import create_app

_MDC_DDL = """
CREATE TABLE market_data_cache (
    data_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol    TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    ts        TEXT NOT NULL
)
"""


@pytest.fixture()
def app(tmp_path):
    """An app whose coverage endpoint reads a seeded SQLite cache."""
    application = create_app(source="synthetic")
    url = f"sqlite:///{tmp_path / 'coverage.db'}"
    eng = create_engine(url, echo=False)
    with eng.connect() as conn:
        conn.execute(text(_MDC_DDL))
        for symbol, timeframe, ts in [
            ("RELIANCE", "1day", "2024-01-05"),
            ("RELIANCE", "1day", "2024-01-08"),
            ("RELIANCE", "1min", "2024-01-05 09:15"),
            ("NIFTY", "1day", "2024-01-05"),
        ]:
            conn.execute(
                text("INSERT INTO market_data_cache (symbol, timeframe, ts) VALUES (:s,:t,:ts)"),
                {"s": symbol, "t": timeframe, "ts": ts},
            )
        conn.commit()
    eng.dispose()

    original = data_manager.DB_URL
    data_manager.DB_URL = url
    data_manager.invalidate_coverage_cache()
    yield application
    data_manager.DB_URL = original
    data_manager.invalidate_coverage_cache()


@pytest.fixture()
def client(app):
    return app.test_client()


def get(client, path="/api/data/coverage", **params):
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return client.get(f"{path}?{query}" if query else path)


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------


def test_endpoint_answers_with_the_full_contract(client):
    body = get(client).get_json()
    for key in (
        "instruments",
        "total",
        "returned",
        "known_total",
        "available_total",
        "db_available",
        "catalogue_source",
        "instrument_types",
        "hint",
        "generated_at",
    ):
        assert key in body, key
    assert body["instrument_types"] == ["equity", "index", "futures", "options"]


def test_a_symbol_with_bars_reports_its_real_coverage(client):
    rows = {r["symbol"]: r for r in get(client, q="RELIANCE").get_json()["instruments"]}
    row = rows["RELIANCE"]
    assert row["data_available"] is True
    assert row["bars_count"] == 3
    assert row["from_date"] == "2024-01-05"
    assert row["to_date"] == "2024-01-08"
    # §1.4 depends on this: only timeframes that really exist are offered
    assert row["timeframes_available"] == ["1min", "1day"]
    assert "hint" not in row


def test_a_symbol_with_no_bars_is_listed_and_explained(client):
    """The §1.3 complaint, verbatim: it used to silently disappear."""
    body = get(client).get_json()
    rows = {r["symbol"]: r for r in body["instruments"]}
    assert "TCS" in rows
    assert rows["TCS"]["data_available"] is False
    assert rows["TCS"]["bars_count"] == 0
    assert rows["TCS"]["timeframes_available"] == []
    assert rows["TCS"]["hint"] == body["hint"]


def test_indices_are_offered_whether_or_not_they_have_bars(client):
    rows = {r["symbol"]: r for r in get(client, types="index").get_json()["instruments"]}
    assert {"NIFTY", "BANKNIFTY", "SENSEX", "INDIAVIX", "MIDCPNIFTY"} <= set(rows)
    assert rows["NIFTY"]["data_available"] is True
    assert rows["BANKNIFTY"]["data_available"] is False


def test_totals_separate_known_from_runnable(client):
    body = get(client, limit=0).get_json()
    assert body["known_total"] > 100  # the shipped universe is always there
    assert body["available_total"] == 2  # only RELIANCE and NIFTY have bars
    assert body["returned"] == body["total"] == body["known_total"]


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


def test_search_filters_by_symbol_and_name(client):
    assert get(client, q="reliance").get_json()["total"] == 1
    assert get(client, q="Reliance").get_json()["total"] == 1
    assert get(client, q="ZZZZ").get_json()["total"] == 0


def test_equity_and_index_tabs_partition_the_list(client):
    equity = {r["symbol"] for r in get(client, types="equity", limit=0).get_json()["instruments"]}
    index = {r["symbol"] for r in get(client, types="index", limit=0).get_json()["instruments"]}
    assert equity and index
    assert equity.isdisjoint(index)


def test_available_filter_hides_symbols_that_cannot_run(client):
    body = get(client, available=1, limit=0).get_json()
    assert {r["symbol"] for r in body["instruments"]} == {"RELIANCE", "NIFTY"}


def test_available_filter_reports_how_many_symbols_it_hid(client):
    """issues.txt B1 (2026-10-01): a data-only dropdown must be able to say
    "N symbols hidden — load data first" instead of silently shrinking."""
    hidden = get(client, available=1, limit=0).get_json()
    assert hidden["hidden_total"] == hidden["known_total"] - hidden["available_total"]
    assert hidden["hidden_total"] > 100  # the shipped universe dwarfs 2 seeded symbols

    # Without available=1 nothing is filtered out, so nothing is "hidden".
    full = get(client, limit=0).get_json()
    assert full["hidden_total"] == 0

    # The count is scoped to the active tab: the Index tab hides only indices.
    index_hidden = get(client, types="index", available=1, limit=0).get_json()
    assert 0 < index_hidden["hidden_total"] < hidden["hidden_total"]

    # A search narrows it too: one no-data match hides exactly one symbol.
    one = get(client, q="TCS", available=1).get_json()
    assert one["total"] == 0 and one["hidden_total"] == 1


def test_paging_covers_the_whole_list(client):
    everything = get(client, limit=0).get_json()
    first = get(client, limit=2, offset=0).get_json()
    last = get(client, limit=2, offset=everything["total"] - 1).get_json()
    assert first["total"] == last["total"] == everything["total"]
    assert first["returned"] == 2 and last["returned"] == 1
    assert first["offset"] == 0 and last["offset"] == everything["total"] - 1


def test_bad_paging_arguments_are_400_not_a_stack_trace(client):
    assert get(client, limit="abc").status_code == 400
    assert get(client, offset="abc").status_code == 400
    assert "error" in get(client, limit="abc").get_json()


# ---------------------------------------------------------------------------
# Degradation + cache
# ---------------------------------------------------------------------------


def test_picker_is_never_empty_without_a_database(monkeypatch, app):
    """`--source synthetic` with no DB: still a usable list, honestly labelled."""
    monkeypatch.setattr(data_manager, "DB_URL", "postgresql+psycopg2://nobody@127.0.0.1:1/none")
    data_manager.invalidate_coverage_cache()
    body = app.test_client().get("/api/data/coverage").get_json()

    assert body["db_available"] is False
    assert body["catalogue_source"] == "builtin"
    assert body["known_total"] > 100
    assert body["available_total"] == 0
    assert body["warnings"], "an unreachable database must be reported, not hidden"
    assert all(r["data_available"] is False for r in body["instruments"])


def test_catalogue_absent_but_cache_present_still_serves(client):
    """`instruments` comes from a broker ingest; its absence is normal."""
    body = get(client, limit=0).get_json()
    assert body["db_available"] is True
    assert body["catalogue_source"] == "market_data_cache"
    assert body["available_total"] == 2


def test_repeated_reads_reuse_the_cached_report(client, monkeypatch):
    from backtest.data import coverage as coverage_mod

    calls = {"n": 0}
    original = coverage_mod.load_bar_coverage

    def counting(eng):
        calls["n"] += 1
        return original(eng)

    monkeypatch.setattr(coverage_mod, "load_bar_coverage", counting)
    data_manager.invalidate_coverage_cache()
    get(client)
    get(client)
    get(client)
    assert calls["n"] == 1, "three pages mounting a picker must not rescan the cache"


def test_refresh_bypasses_the_cache(client, monkeypatch):
    from backtest.data import coverage as coverage_mod

    calls = {"n": 0}
    original = coverage_mod.load_bar_coverage

    def counting(eng):
        calls["n"] += 1
        return original(eng)

    monkeypatch.setattr(coverage_mod, "load_bar_coverage", counting)
    data_manager.invalidate_coverage_cache()
    get(client)
    get(client, refresh=1)
    assert calls["n"] == 2


def test_fetch_job_invalidates_the_cached_report(client):
    get(client)
    assert data_manager._coverage_cache["report"] is not None
    data_manager.invalidate_coverage_cache()
    assert data_manager._coverage_cache["report"] is None


# ---------------------------------------------------------------------------
# names_only — the static instrument list (2026-10-06 re-architecture)
# ---------------------------------------------------------------------------


def test_names_only_lists_instruments_without_measuring_them(client):
    body = get(client, names_only=1, limit=0).get_json()
    rows = {r["symbol"]: r for r in body["instruments"]}
    assert "RELIANCE" in rows and "NIFTY" in rows
    row = rows["RELIANCE"]
    assert row["data_available"] is None, "null = not asked; false would mean 'no bars'"
    assert row["coverage_known"] is False
    assert row["bars_count"] is None
    assert row["from_date"] is None and row["to_date"] is None
    assert row["timeframes_stored"] == [] and row["timeframes_available"] == []
    assert "hint" not in row
    assert row["name"], "the label still needs a readable name"


def test_names_only_reports_availability_as_unknown_not_zero(client):
    body = get(client, names_only=1, limit=0).get_json()
    assert body["available_total"] is None, "0 would read as 'nothing has data'"
    assert body["hidden_total"] == 0
    assert body["known_total"] == body["total"]
    # The database was opened — to ask which NAMES exist, not how much data
    # they hold. So db_available is a yes, while availability stays unknown.
    assert body["db_available"] is True


def test_names_only_reports_an_unreachable_database_as_unknown_not_false(client, monkeypatch):
    """"Not asked" must not be dressed up as "asked, and there is none": the
    picker renders db_available=false as "no data source connected", which is a
    claim about the deployment that this path never checked."""
    monkeypatch.setattr(data_manager, "DB_URL", "sqlite:////nonexistent/nope.db")
    data_manager.invalidate_coverage_cache()
    body = get(client, names_only=1, limit=0).get_json()
    assert body["total"] > 0
    assert body["db_available"] is None
    assert body["warnings"] == [], "an unreachable DB is not a warning for a names list"


def test_names_only_wins_over_the_available_filter(client):
    """`available=1&names_only=1` contradicts itself — one says look at the
    bars, the other says do not. The cheap request wins, and the response must
    not pretend it filtered."""
    body = get(client, names_only=1, available=1, limit=0).get_json()
    assert body["available_total"] is None
    assert body["hidden_total"] == 0
    assert all(r["data_available"] is None for r in body["instruments"])
    # and it is not silently empty, which is what a literal reading would give
    assert body["total"] > 0


def test_names_only_does_not_query_the_bars_at_all(client, monkeypatch):
    from backtest.data import coverage as coverage_mod

    def explode(eng):
        raise AssertionError("names_only must not read market_data_cache")

    monkeypatch.setattr(coverage_mod, "load_bar_coverage", explode)
    data_manager.invalidate_coverage_cache()
    body = get(client, names_only=1, limit=0).get_json()
    assert body["total"] > 0


def test_names_only_never_touches_the_instruments_catalogue(client, monkeypatch):
    """The expensive half on a real database (~1.4s of scriptmaster rows that
    a name-only list can never show)."""
    from backtest.data import coverage as coverage_mod

    def explode(eng):
        raise AssertionError("names_only has no use for the 140k-row catalogue")

    monkeypatch.setattr(coverage_mod, "load_catalogue", explode)
    data_manager.invalidate_coverage_cache()
    body = get(client, names_only=1, limit=0).get_json()
    assert body["total"] > 0


def test_names_only_survives_a_missing_database(client, monkeypatch):
    """It should not even need one — the shipped universe is enough."""
    monkeypatch.setattr(data_manager, "DB_URL", "sqlite:////nonexistent/nope.db")
    data_manager.invalidate_coverage_cache()
    body = get(client, names_only=1, limit=0).get_json()
    assert body["total"] > 0
    assert body["warnings"] == []


def test_names_only_does_not_read_bar_counts(client, monkeypatch):
    """get_candles-loading must not run: the whole point is to skip the
    aggregation while still listing the symbols that have bars."""
    from backtest.data import coverage as coverage_mod

    def explode(eng):
        raise AssertionError("names_only must not aggregate bars")

    monkeypatch.setattr(coverage_mod, "load_bar_coverage", explode)
    data_manager.invalidate_coverage_cache()
    body = get(client, names_only=1, limit=0).get_json()
    assert body["returned"] > 0


def test_curated_only_skips_the_catalogue_but_keeps_real_coverage(client, monkeypatch):
    """The Data tab still shows per-symbol coverage — that is its job. It just
    no longer pays for catalogue rows it filters away."""
    from backtest.data import coverage as coverage_mod

    def explode(eng):
        raise AssertionError("a curated-only caller can never see a catalogue row")

    data_manager.invalidate_coverage_cache()
    full = get(client, curated=1, limit=0).get_json()
    monkeypatch.setattr(coverage_mod, "load_catalogue", explode)
    data_manager.invalidate_coverage_cache()
    lean = get(client, curated=1, limit=0).get_json()
    assert {r["symbol"] for r in lean["instruments"]} == {
        r["symbol"] for r in full["instruments"]
    }
    rows = {r["symbol"]: r for r in lean["instruments"]}
    assert rows["RELIANCE"]["bars_count"] == 3, "coverage is still measured"
    assert rows["RELIANCE"]["data_available"] is True


def test_the_narrow_path_still_tells_the_truth_about_what_it_did(client):
    """A caller must be able to see which mode answered, without guessing."""
    leaned = get(client, curated=1, limit=0).get_json()
    named = get(client, names_only=1, limit=0).get_json()
    full = get(client, limit=0).get_json()
    for body in (leaned, named, full):
        assert body["total"] > 0
    assert named["instruments"][0]["coverage_known"] is False
    assert full["instruments"][0]["coverage_known"] is True
    assert leaned["instruments"][0]["coverage_known"] is True


def test_names_only_includes_symbols_fetched_outside_the_shipped_universe(client):
    """Fetch-then-pick must keep working.

    A list built from the shipped universe alone (NIFTY 200 + indices) would
    silently omit anything fetched outside it — the §1.3 disappearance bug in
    new clothes. The endpoint fixture's cache holds NIFTY, which IS curated,
    so add a symbol that is not.
    """
    from sqlalchemy import create_engine, text as sqltext

    eng = create_engine(data_manager.DB_URL)
    with eng.begin() as conn:
        conn.execute(
            sqltext(
                "INSERT INTO market_data_cache (symbol, timeframe, ts) "
                "VALUES ('OBSCUREMIDCAP', '1day', '2024-01-05')"
            )
        )
    eng.dispose()
    data_manager.invalidate_coverage_cache()

    symbols = {r["symbol"] for r in get(client, names_only=1, limit=0).get_json()["instruments"]}
    assert "OBSCUREMIDCAP" in symbols, "a fetched symbol must be pickable"
    # ...and it is a NAME only, with no bar count smuggled in beside it.
    row = next(
        r for r in get(client, names_only=1, limit=0).get_json()["instruments"]
        if r["symbol"] == "OBSCUREMIDCAP"
    )
    assert row["bars_count"] is None and row["coverage_known"] is False


def test_names_only_lists_a_cached_symbol_exactly_once(client):
    data_manager.invalidate_coverage_cache()
    body = get(client, names_only=1, limit=0).get_json()
    symbols = [r["symbol"] for r in body["instruments"]]
    assert len(symbols) == len(set(symbols)), "a symbol in both sources must merge"
    assert body["total"] == len(symbols)


# ---------------------------------------------------------------------------
# include_catalogue=0 — the Data tab's shape
# ---------------------------------------------------------------------------


def test_the_universe_shape_skips_the_catalogue(client, monkeypatch):
    from backtest.data import coverage as coverage_mod

    def explode(eng):
        raise AssertionError("include_catalogue=0 must not read 140k contract rows")

    monkeypatch.setattr(coverage_mod, "load_catalogue", explode)
    data_manager.invalidate_coverage_cache()
    body = get(client, include_catalogue=0, limit=0).get_json()
    assert body["total"] > 0, "the shipped universe is still listed"


def test_the_universe_shape_still_measures_real_coverage(client):
    """It is not names-only: this is the Data tab, where the numbers are the point."""
    data_manager.invalidate_coverage_cache()
    rows = {
        r["symbol"]: r
        for r in get(client, include_catalogue=0, limit=0).get_json()["instruments"]
    }
    assert rows["RELIANCE"]["bars_count"] == 3
    assert rows["RELIANCE"]["data_available"] is True
    assert rows["RELIANCE"]["coverage_known"] is True
    assert rows["RELIANCE"]["from_date"] == "2024-01-05"
    assert rows["NIFTY"]["data_available"] is True


def test_the_universe_shape_keeps_a_symbol_outside_the_universe(client):
    """A symbol somebody fetched must not vanish from the tab that fetched it."""
    from sqlalchemy import create_engine, text as sqltext

    eng = create_engine(data_manager.DB_URL)
    with eng.begin() as conn:
        conn.execute(
            sqltext(
                "INSERT INTO market_data_cache (symbol, timeframe, ts) "
                "VALUES ('OFFUNIVERSE', '1min', '2024-01-05 09:15')"
            )
        )
    eng.dispose()
    data_manager.invalidate_coverage_cache()
    rows = {
        r["symbol"]: r
        for r in get(client, include_catalogue=0, limit=0).get_json()["instruments"]
    }
    assert "OFFUNIVERSE" in rows
    assert rows["OFFUNIVERSE"]["data_available"] is True


def test_the_universe_shape_says_so_in_catalogue_source(client):
    """A client must not read a missing symbol as 'it does not exist' when the
    cheapest shape was requested."""
    data_manager.invalidate_coverage_cache()
    body = get(client, include_catalogue=0, limit=0).get_json()
    assert body["catalogue_source"] == "universe+market_data_cache"


def test_the_default_shape_is_unchanged_by_the_new_flag(client):
    """Absent means 'as before' — the catalogue is still read for other callers."""
    called = {"n": 0}
    from backtest.data import coverage as coverage_mod

    original = coverage_mod.load_catalogue

    def counting(eng):
        called["n"] += 1
        return original(eng)

    coverage_mod.load_catalogue = counting
    try:
        data_manager.invalidate_coverage_cache()
        get(client, limit=0)
        assert called["n"] == 1
    finally:
        coverage_mod.load_catalogue = original


def test_per_timeframe_dates_ride_along(client):
    """The inventory table is per timeframe; these are the dates it shows, and
    they must come from the coverage aggregate rather than a second scan."""
    data_manager.invalidate_coverage_cache()
    rows = {
        r["symbol"]: r
        for r in get(client, include_catalogue=0, limit=0).get_json()["instruments"]
    }
    rel = rows["RELIANCE"]
    assert rel["dates_by_timeframe"]["1day"] == {"from": "2024-01-05", "to": "2024-01-08"}
    assert rel["bars_by_timeframe"] == {"1day": 2, "1min": 1}


def test_the_canonical_vocabulary_is_published(client):
    """Clients order a SET of timeframes without declaring their own copy."""
    from backtest.data.base import CANONICAL_TIMEFRAMES

    body = get(client, limit=0).get_json()
    assert body["timeframes"] == list(CANONICAL_TIMEFRAMES)


def test_inventory_is_served_from_the_cached_report_without_its_own_scan(client, monkeypatch):
    """The Data tab loads the list and the table together; the table must not
    run the same GROUP BY a second time."""
    from backtest.data import coverage as coverage_mod

    calls = {"n": 0}
    original = coverage_mod.load_bar_coverage

    def counting(eng):
        calls["n"] += 1
        return original(eng)

    monkeypatch.setattr(coverage_mod, "load_bar_coverage", counting)
    data_manager.invalidate_coverage_cache()
    get(client, include_catalogue=0, limit=0)
    inv = client.get("/api/data/inventory").get_json()
    assert calls["n"] == 1, "one aggregate for both views"
    assert inv["total_symbols"] > 0
    assert inv["total_bars"] > 0


def test_inventory_row_shape_is_unchanged(client):
    """Published contract, so the optimisation must not alter the payload."""
    data_manager.invalidate_coverage_cache()
    inv = client.get("/api/data/inventory").get_json()
    assert set(inv) == {"symbols", "total_symbols", "total_bars"}
    entries = inv["symbols"]["RELIANCE"]
    assert isinstance(entries, list)
    for entry in entries:
        assert set(entry) == {"timeframe", "bars", "earliest", "latest"}
    by_tf = {e["timeframe"]: e for e in entries}
    assert by_tf["1day"]["bars"] == 2
    assert by_tf["1day"]["earliest"] == "2024-01-05"
    assert by_tf["1day"]["latest"] == "2024-01-08"


def test_inventory_only_reports_symbols_that_have_bars(client):
    data_manager.invalidate_coverage_cache()
    inv = client.get("/api/data/inventory").get_json()
    # NIFTY has bars in this fixture; a universe symbol with none must not appear.
    with_bars = {s for s, entries in inv["symbols"].items() if entries}
    assert inv["total_symbols"] == len(with_bars)


# ---------------------------------------------------------------------------
# The symbol listing cache must expire with the coverage cache
# ---------------------------------------------------------------------------


def test_the_symbol_listing_cache_is_dropped_with_the_coverage_cache():
    """``symbols._CACHED_SYMBOLS`` was written once and cleared nowhere.

    The fetch job clears the coverage cache when new bars land, so the symbol
    listing outlived the fact it described: the Forward page kept showing the
    symbol set from boot and a symbol fetched minutes ago stayed invisible until
    the process restarted. Both caches describe the same rows, so they expire
    together.
    """
    from backtest.api import symbols as symbols_api

    symbols_api._CACHED_SYMBOLS["all:db"] = ["RELIANCE"]
    data_manager.invalidate_coverage_cache()
    assert symbols_api._CACHED_SYMBOLS == {}


def test_a_symbol_fetched_while_a_listing_was_cached_becomes_visible(client, monkeypatch):
    """The user-visible symptom, through the endpoint.

    The app fixture runs source=synthetic, where /api/symbols deliberately
    returns nothing — so the source is faked here to exercise the caching path
    without needing a second database.
    """
    from backtest.api import symbols as symbols_api

    stored = {"RELIANCE"}

    class FakeSource:
        def list_symbols(self, timeframe=None):
            return sorted(stored)

    monkeypatch.setattr(symbols_api, "DbSource", FakeSource)
    client.application.config["BACKTEST_SOURCE"] = "db"
    symbols_api._CACHED_SYMBOLS.clear()

    assert client.get("/api/symbols").get_json()["symbols"] == ["RELIANCE"]

    # A fetch finishes and writes a new symbol.
    stored.add("LATEFETCH")
    assert client.get("/api/symbols").get_json()["symbols"] == ["RELIANCE"], (
        "the listing is cached — that is fine, as long as it can be invalidated"
    )

    # ...and the fetch job's completion path does exactly this one call.
    data_manager.invalidate_coverage_cache()
    assert client.get("/api/symbols").get_json()["symbols"] == ["LATEFETCH", "RELIANCE"]


def test_invalidating_the_cache_never_raises(monkeypatch):
    """Housekeeping must not be able to break a fetch job."""
    import sys

    monkeypatch.setitem(sys.modules, "backtest.api.symbols", None)
    data_manager.invalidate_coverage_cache()  # must not raise
