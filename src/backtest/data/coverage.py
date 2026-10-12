"""Data coverage — which instruments exist, and which of them have bars.

Implements PRD ``docs/backTest-enhance.md`` Part 1 §1.3 (symbol picker). The
complaint it answers: *symbols silently disappear if no data is cached, indices
are not shown, and nothing explains why*. The fix is one endpoint that every
picker reads, returning the **union** of what is known and what is stored, so a
symbol with no bars is still listed — greyed out, with the reason.

Three inputs, none of which is allowed to be the only one:

1. ``market_data_cache`` — what has actually been fetched (bars, coverage,
   the timeframes that really exist per symbol).
2. The ``instruments`` catalogue (mStock scriptmaster, ~154k rows) — the
   exchange's own view of what is tradable, including F&O. Optional: the
   table only exists on a database that has run the ingest.
3. ``stock-list/nse_ind_nifty200list.csv`` + the built-in index universe — a
   version-controlled floor that works with no database at all, so the picker
   is never empty just because the app is running ``--source synthetic``.

**154k rows is not a payload.** The catalogue is reduced here, on the server,
to the things a backtest form can actually be pointed at: one row per
*tradable symbol* (underlyings, not every option contract), plus any contract
that has its own cached bars. Paging and search happen in :mod:`backtest.api`
on top of this.

Layering: no Flask here, and the engine is a parameter — the API layer owns
connection policy, this module owns the answer.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import text

from backtest.data.base import (
    CANONICAL_TIMEFRAMES,
    derive_serviceable_timeframes,
    finest_timeframe,
)
from backtest.logging_config import get_logger

log = get_logger(__name__)

__all__ = [
    "INSTRUMENT_TYPES",
    "INDEX_UNIVERSE",
    "NO_DATA_HINT",
    "BarCoverage",
    "CoverageReport",
    "classify_instrument",
    "index_universe",
    "load_bar_coverage",
    "load_catalogue",
    "load_equity_universe",
    "build_coverage",
    "filter_coverage",
]

#: The four instrument types the picker filters on. A row is always ONE of
#: these; a listed *underlying* that also has derivatives is still ``equity``
#: or ``index`` and carries ``has_futures`` / ``has_options`` instead.
INSTRUMENT_TYPES = ("equity", "index", "futures", "options")

#: The indices a backtest is actually run on, with their display names. Always
#: offered whether or not they have bars: an index with no data is a fetch
#: prompt, not a missing option (PRD §1.3).
INDEX_UNIVERSE: tuple[tuple[str, str], ...] = (
    ("NIFTY", "NIFTY 50"),
    ("BANKNIFTY", "NIFTY BANK"),
    ("FINNIFTY", "NIFTY NEXT 50"),
    ("MIDCPNIFTY", "NIFTY MIDCAP 150"),
    ("SENSEX", "S&P BSE SENSEX"),
    ("INDIAVIX", "INDIA VIX"),
    # Sectoral indices
    ("NIFTYIT", "NIFTY IT"),
    ("NIFTYAUTO", "NIFTY AUTO"),
    ("NIFTYPHARMA", "NIFTY PHARMA"),
    ("NIFTYMETAL", "NIFTY METAL"),
    ("NIFTYREALTY", "NIFTY REALTY"),
    ("NIFTYFMCG", "NIFTY FMCG"),
    ("NIFTYENERGY", "NIFTY ENERGY"),
    ("NIFTYMEDIA", "NIFTY MEDIA"),
    ("NIFTYPSUBANK", "NIFTY PSU BANK"),
    ("NIFTYINFRA", "NIFTY INFRA"),
)

#: Tooltip on a row that has no bars. Points at the tab that fixes it.
NO_DATA_HINT = "No data loaded. Go to Data tab → fetch data for this symbol."

#: Shipped equity universe (NSE NIFTY 200 list). Version-controlled, so the
#: picker has a floor without a database.
_UNIVERSE_CSV = Path(__file__).resolve().parents[3] / "stock-list" / "nse_ind_nifty200list.csv"

#: mStock/Indian-exchange contract shapes. Conservative by design: a row that
#: does not match confidently is left unclassified rather than guessed into
#: the F&O tab, because a wrong classification sends a user to fetch bars for
#: something that is not tradable. The optional expiry token (``25SEP``) is
#: what separates ``NIFTY25SEP24500CE`` from a bare underlying; the strike and
#: the ``CE``/``PE``/``FUT`` suffix are what make a contract a contract.
_OPT_RE = re.compile(
    r"^(?P<u>[A-Z0-9&]{1,20}?)(?:\d{1,2}[A-Z]{3})?(?P<strike>\d{4,6})(?P<side>CE|PE)$"
)
_FUT_RE = re.compile(r"^(?P<u>[A-Z0-9&]{1,20}?)(?:\d{1,2}[A-Z]{3}\d{0,4})?(?P<kind>FUT|FUTNR)$")

#: ``instrument_type`` / ``segment`` values the scriptmaster uses. Matched
#: case-insensitively; a type wins over a segment when both are present.
_TYPE_MAP = {
    "CE": "options",
    "PE": "options",
    "OPTIDX": "options",
    "OPTEQ": "options",
    "EQ": "equity",
    "EQUITY": "equity",
    "ETF": "equity",
    "IDX": "index",
    "INDEX": "index",
    "NSEIDX": "index",
    "BSEIDX": "index",
    "FUT": "futures",
    "FUTIDX": "futures",
    "FUTEQ": "futures",
    "FUTID": "futures",
}


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass
class BarCoverage:
    """What ``market_data_cache`` actually holds for one symbol.

    ``timeframes_stored`` is the literal truth — the granularities somebody
    downloaded. ``timeframes`` is what the symbol can *serve*, which is the
    stored set plus everything derivable from the finest of them by resampling
    (see :func:`backtest.data.base.derive_serviceable_timeframes`). A picker
    offers ``timeframes``; the Data tab reports ``timeframes_stored`` so "what
    did I download?" and "what can I run?" stop being the same question.
    """

    bars_count: int = 0
    from_date: str | None = None
    to_date: str | None = None
    timeframes: list[str] = field(default_factory=list)
    timeframes_stored: list[str] = field(default_factory=list)
    bars_by_timeframe: dict[str, int] = field(default_factory=dict)
    #: Per stored granularity: ``{tf: {"from": date, "to": date}}``. The Data
    #: tab's inventory table is per symbol AND timeframe, and the aggregate
    #: below already reads these MIN(ts)/MAX(ts) — before this they were folded
    #: into one pair per symbol and thrown away, so the table had to re-scan the
    #: whole cache to get them back. See ``inventory()``.
    dates_by_timeframe: dict[str, dict[str, str | None]] = field(default_factory=dict)

    @property
    def data_available(self) -> bool:
        return self.bars_count > 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "data_available": self.data_available,
            "bars_count": self.bars_count,
            "from_date": self.from_date,
            "to_date": self.to_date,
            # Finest first (canonical order), so a picker never offers a
            # coarser timeframe before the finer one that is really stored.
            "timeframes_available": list(self.timeframes),
            # What was literally downloaded, and how much of it: the Data tab's
            # "source of truth" columns, and the evidence behind the derived
            # list above.
            "timeframes_stored": list(self.timeframes_stored),
            "bars_by_timeframe": dict(self.bars_by_timeframe),
            "dates_by_timeframe": {k: dict(v) for k, v in self.dates_by_timeframe.items()},
            # Present on every row so a consumer can tell "no bars" apart from
            # "nobody looked" without inferring it from a null bar count.
            "coverage_known": True,
        }


@dataclass
class CoverageReport:
    """The answer to "what can I pick, and what do I get if I do?"."""

    instruments: list[dict[str, Any]] = field(default_factory=list)
    #: ``None`` means UNKNOWN — nobody asked (the names-only list never touches
    #: the database). False means asked and found missing. The UI must not read
    #: unknown as "no data source connected".
    db_available: bool | None = False
    catalogue_source: str = "builtin"
    sources: list[str] = field(default_factory=list)
    generated_at: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.instruments)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def _is_index_symbol(symbol: str) -> bool:
    return any(symbol == sym for sym, _ in INDEX_UNIVERSE)


def classify_instrument(
    symbol: str,
    instrument_type: str | None = None,
    segment: str | None = None,
    name: str | None = None,
) -> str:
    """Classify a trading symbol into one of :data:`INSTRUMENT_TYPES`.

    Order matters: an explicit catalogue type wins, then the known index
    universe (authoritative for symbols the catalogue spells oddly), then the
    contract shape. Returns ``"equity"`` as the last resort — a bare NSE
    symbol is a share, which is the right default for a backtest form.
    """
    sym = str(symbol or "").strip().upper()
    if not sym:
        return "equity"

    if _is_index_symbol(sym):
        return "index"

    for raw in (instrument_type, segment):
        key = str(raw or "").strip().upper()
        if key in _TYPE_MAP:
            return _TYPE_MAP[key]
        if key and ("OPT" in key or key in ("CE", "PE")):
            return "options"
        if key and "FUT" in key:
            return "futures"
        if key and "IDX" in key:
            return "index"

    if _OPT_RE.match(sym):
        return "options"
    if _FUT_RE.match(sym):
        return "futures"

    if name and ("NIFTY 50" in name.upper() or "INDEX" in name.upper()):
        return "index"
    return "equity"


def _derivative_underlying(symbol: str) -> tuple[str, str] | None:
    """``(underlying, kind)`` when a trading symbol is an F&O contract."""
    sym = str(symbol or "").strip().upper()
    m = _OPT_RE.match(sym)
    if m:
        return m.group("u"), "options"
    m = _FUT_RE.match(sym)
    if m:
        return m.group("u"), "futures"
    return None


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


def index_universe() -> list[dict[str, Any]]:
    """The built-in index rows (never filtered out for lacking data)."""
    return [
        {"symbol": sym, "name": name, "instrument_type": "index", "exchange": "NSE"}
        for sym, name in INDEX_UNIVERSE
    ]


def load_equity_universe(path: Path | None = None) -> list[dict[str, Any]]:
    """The shipped NIFTY 200 list, as catalogue rows.

    A missing or malformed file yields an empty list: the endpoint degrades to
    "only the indices", it does not 500.
    """
    csv_path = path or _UNIVERSE_CSV
    rows: list[dict[str, Any]] = []
    try:
        with csv_path.open(newline="", encoding="utf-8-sig") as fh:
            for rec in csv.DictReader(fh):
                symbol = str(rec.get("Symbol") or "").strip().upper()
                if not symbol:
                    continue
                rows.append(
                    {
                        "symbol": symbol,
                        "name": str(rec.get("Company Name") or "").strip() or symbol,
                        "instrument_type": classify_instrument(symbol),
                        "exchange": "NSE",
                    }
                )
    except FileNotFoundError:
        log.warning("[coverage] equity universe file missing: %s", csv_path)
    except (OSError, csv.Error) as exc:  # noqa: BLE001 — a bad file must not 500
        log.warning("[coverage] equity universe unreadable (%s): %s", csv_path, exc)
    log.debug("[coverage] equity universe: %d rows", len(rows))
    return rows


def load_bar_coverage(engine: Any) -> dict[str, BarCoverage]:
    """Aggregate ``market_data_cache`` into one row per symbol.

    One grouped scan for the whole cache (the existing ``/api/data/inventory``
    query) rather than a per-symbol count — a picker that asks about 200
    symbols must not issue 200 queries.
    """
    sql = text(
        """
        SELECT symbol, timeframe, COUNT(*) AS bars, MIN(ts) AS earliest, MAX(ts) AS latest
        FROM market_data_cache
        GROUP BY symbol, timeframe
        """
    )
    out: dict[str, BarCoverage] = {}
    with engine.connect() as conn:
        rows = conn.execute(sql).mappings().all()

    order = {tf: i for i, tf in enumerate(CANONICAL_TIMEFRAMES)}
    for row in rows:
        symbol = str(row["symbol"]).strip().upper()
        if not symbol:
            continue
        cov = out.setdefault(symbol, BarCoverage())
        bars = int(row["bars"] or 0)
        cov.bars_count += bars
        tf = str(row["timeframe"] or "").strip()
        if tf and tf not in cov.timeframes_stored:
            cov.timeframes_stored.append(tf)
        if tf:
            cov.bars_by_timeframe[tf] = cov.bars_by_timeframe.get(tf, 0) + bars
        earliest = _iso_date(row["earliest"])
        latest = _iso_date(row["latest"])
        if tf:
            cov.dates_by_timeframe[tf] = {"from": earliest, "to": latest}
        # Totals span every stored granularity, so widen rather than overwrite.
        cov.from_date = _min_date(cov.from_date, earliest)
        cov.to_date = _max_date(cov.to_date, latest)

    for cov in out.values():
        cov.timeframes_stored.sort(key=lambda tf: order.get(tf, len(order)))
        # Offer everything this symbol can SERVE, not only what it literally
        # holds. A symbol fetched once at 1min could always answer 5min/1hour/
        # 1day — DbSource has resampled since P4.3 — but the picker was handed
        # only the stored list, so it hid eight of nine timeframes.
        finest = finest_timeframe(cov.timeframes_stored)
        cov.timeframes = derive_serviceable_timeframes(
            cov.timeframes_stored,
            finest_bars=cov.bars_by_timeframe.get(finest) if finest else None,
        )
    log.info(
        "[coverage] bar coverage: %d symbols, %d bars",
        len(out),
        sum(c.bars_count for c in out.values()),
    )
    return out


def load_cached_symbols(engine: Any) -> list[dict[str, Any]] | None:
    """Symbols that have bars, as names only — no counts, no dates, no types.

    Returns ``None`` when the symbol list could not be READ (no database, no
    cache table); ``[]`` is reserved for "read it, and it is empty". The
    distinction is what lets a caller report the database as unknown rather
    than as connected — collapsing the two would turn "we could not look" into
    "there is nothing there", which is the failure mode this whole change is
    about.

    The point of this function is what it does NOT read. ``load_bar_coverage``
    answers "how much, over which dates, at which granularities" by grouping
    every bar in the table and then deriving each symbol's serviceable
    timeframes; that is the right question for the Data tab and the wrong one
    for a dropdown, which only needs to know the symbol exists. Selecting the
    distinct symbols still touches the same rows, but it neither aggregates
    nor classifies, and the cost is a scan instead of a scan plus a merge.

    It exists so that "fetch a symbol, then pick it" keeps working. A list
    built from the shipped universe alone (NIFTY 200 + indices) would silently
    omit anything fetched outside that list — the §1.3 disappearance bug in
    new clothes.
    """
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT DISTINCT symbol FROM market_data_cache")
            ).fetchall()
    except Exception as exc:  # noqa: BLE001 — absent cache table is survivable
        log.info("[coverage] cached symbols unavailable: %s", exc.__class__.__name__)
        return None

    out: list[dict[str, Any]] = []
    for row in rows:
        symbol = str(row[0] or "").strip().upper()
        if symbol:
            # The symbol is its own name: naming it properly would mean reading
            # the 140k-row instruments catalogue, which is the cost this path
            # exists to avoid. The picker already falls back to the symbol.
            out.append({"symbol": symbol, "name": symbol})
    return out


def load_catalogue(engine: Any, *, limit: int | None = None) -> list[dict[str, Any]]:
    """Read the ``instruments`` table into catalogue rows.

    Returns ``[]`` (and logs) when the table does not exist — it is created by
    the scriptmaster ingest, not by a migration, so its absence is normal.
    """
    sql = text(
        """
        SELECT tradingsymbol, name, instrument_type, segment, exchange
        FROM instruments
        """
    )
    if limit:
        sql = text(
            """
            SELECT tradingsymbol, name, instrument_type, segment, exchange
            FROM instruments LIMIT :limit
            """
        )
    try:
        with engine.connect() as conn:
            rows = conn.execute(sql, {"limit": limit} if limit else {}).mappings().all()
    except Exception as exc:  # noqa: BLE001 — absent table is expected offline
        log.info("[coverage] instruments catalogue unavailable: %s", exc.__class__.__name__)
        return []

    out: list[dict[str, Any]] = []
    for row in rows:
        symbol = str(row["tradingsymbol"] or "").strip().upper()
        if not symbol:
            continue
        out.append(
            {
                "symbol": symbol,
                "name": str(row["name"] or "").strip() or symbol,
                "instrument_type": classify_instrument(
                    symbol, row["instrument_type"], row["segment"], row["name"]
                ),
                "exchange": str(row["exchange"] or "").strip().upper() or None,
            }
        )
    log.info("[coverage] instruments catalogue: %d rows", len(out))
    return out


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


def _iso_date(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (datetime,)):
        return value.strftime("%Y-%m-%d")
    text_value = str(value).strip()
    return text_value[:10] if text_value else None


def _min_date(a: str | None, b: str | None) -> str | None:
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def _max_date(a: str | None, b: str | None) -> str | None:
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


def build_coverage(
    *,
    bars: dict[str, BarCoverage] | None = None,
    catalogue: Iterable[dict[str, Any]] | None = None,
    universe: Iterable[dict[str, Any]] | None = None,
    db_available: bool | None = False,
    coverage_known: bool = True,
) -> CoverageReport:
    """Merge every input into one picker-ready list.

    The union is the whole point: a symbol is listed if ANY source knows it.
    ``data_available`` then says whether choosing it will actually produce
    bars, and a row without them carries :data:`NO_DATA_HINT` so the UI can
    explain the gap instead of hiding the option.

    ``coverage_known=False`` is the NAMES-ONLY mode: the caller wants a static
    list of instruments and has not asked what is stored. Rows then carry
    ``coverage_known: False`` and NO :data:`NO_DATA_HINT`, because an
    unevaluated symbol is not a symbol without data — claiming otherwise would
    grey out the entire list and tell the user to go fetch things that are
    already on disk. Availability for these is answered when a backtest is
    actually requested, where the error can name the dates that DO exist.
    """
    report = CoverageReport(
        db_available=db_available, generated_at=datetime.now().strftime("%Y-%m-%d")
    )
    bar_map = dict(bars or {})
    universe_rows = list(universe) if universe is not None else []

    #: The curated, human-readable universe: the built-in index list plus the
    #: shipped equity universe (NIFTY 200), both with display names. The Data
    #: tab's fetch picker lists ONLY these — the full scriptmaster catalogue
    #: carries ~150k raw broker rows (contract ids, ISINs) that are neither
    #: readable nor fetchable by that tab. See ``curated`` on each row.
    curated_symbols = {str(r.get("symbol") or "").strip().upper() for r in index_universe()}
    curated_symbols |= {str(r.get("symbol") or "").strip().upper() for r in universe_rows}

    # symbol -> row, first source wins the name/type, every source sets flags
    merged: dict[str, dict[str, Any]] = {}
    derivatives: dict[str, set[str]] = {}

    def absorb(rows: Iterable[dict[str, Any]], source: str) -> None:
        count = 0
        for row in rows:
            symbol = str(row.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            existing = merged.get(symbol)
            kind = row.get("instrument_type") or classify_instrument(symbol)
            if existing is None:
                merged[symbol] = {
                    "symbol": symbol,
                    "name": row.get("name") or symbol,
                    "instrument_type": kind,
                    "exchange": row.get("exchange"),
                }
            else:
                # A later source may supply a real name for a symbol first seen
                # as a bare ticker, or a more specific type.
                if existing["name"] == symbol and row.get("name"):
                    existing["name"] = row["name"]
                if existing["instrument_type"] == "equity" and kind != "equity":
                    existing["instrument_type"] = kind
                if not existing.get("exchange") and row.get("exchange"):
                    existing["exchange"] = row["exchange"]
            # F&O contracts contribute their UNDERLYING, not another 150k rows.
            underlying = _derivative_underlying(symbol)
            if underlying and kind in ("options", "futures"):
                derivatives.setdefault(underlying[0], set()).add(kind)
                # The exchange listing a TCS future is itself the statement
                # that TCS is tradable — so the underlying is a known symbol
                # even if no other source happened to mention it.
                if underlying[0] not in merged:
                    merged[underlying[0]] = {
                        "symbol": underlying[0],
                        "name": underlying[0],
                        "instrument_type": classify_instrument(underlying[0]),
                        "exchange": row.get("exchange"),
                    }
            count += 1
        report.sources.append(source)

    absorb(index_universe(), "index_universe")
    if universe_rows:
        absorb(universe_rows, "nifty200")
    if catalogue is not None:
        absorb(catalogue, "instruments")
    if bar_map:
        absorb(({"symbol": s} for s in bar_map), "market_data_cache")

    rows: list[dict[str, Any]] = []
    for symbol, row in merged.items():
        cov = bar_map.get(symbol)
        entry: dict[str, Any] = {
            "symbol": symbol,
            "name": row["name"],
            "instrument_type": row["instrument_type"],
            "exchange": row["exchange"],
            "curated": symbol in curated_symbols,
        }
        if coverage_known:
            entry.update(cov.as_dict() if cov else BarCoverage().as_dict())
        else:
            # Same KEYS, explicitly unknown. Shape stability matters: every
            # consumer indexes these without checking, and a missing key would
            # surface as `undefined` in the UI rather than as "not asked".
            entry.update({
                "data_available": None,
                "bars_count": None,
                "from_date": None,
                "to_date": None,
                "timeframes_stored": [],
                "timeframes_available": [],
                "bars_by_timeframe": {},
                "coverage_known": False,
            })
        if coverage_known and not entry["data_available"]:
            entry["hint"] = NO_DATA_HINT
        kinds = derivatives.get(symbol)
        if kinds:
            entry["has_futures"] = "futures" in kinds
            entry["has_options"] = "options" in kinds
        contract = _derivative_underlying(symbol)
        if contract:
            # Name the underlying so a row with no bars of its own can say
            # WHY, instead of implying the instrument itself is fetchable.
            entry["underlying"] = contract[0]
        rows.append(entry)

    rows.sort(key=lambda r: (r["symbol"]))
    report.instruments = rows
    return report


def filter_coverage(
    report: CoverageReport,
    *,
    query: str = "",
    types: Iterable[str] | None = None,
    available_only: bool = False,
    curated_only: bool = False,
    limit: int | None = None,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """Search / filter / page a report (PRD §1.3's All · Equity · Index · F&O).

    ``types`` accepts the four instrument types; ``fno`` is a UI-level filter
    meaning "has derivatives", resolved here so the page does not have to
    know how derivatives are marked. ``curated_only`` keeps just the built-in
    universe rows (NIFTY 200 + indices) — the readable list the Data tab's
    fetch picker shows.
    """
    wanted = {t.strip().lower() for t in types} if types else set()
    fno_only = "fno" in wanted
    if fno_only:
        wanted.discard("fno")

    needle = query.strip().upper()
    rows = []
    for row in report.instruments:
        if curated_only and not row.get("curated"):
            continue
        if wanted and row["instrument_type"] not in wanted:
            continue
        if fno_only and not (row.get("has_futures") or row.get("has_options")):
            continue
        if available_only and not row["data_available"]:
            continue
        if needle and needle not in row["symbol"] and needle not in str(row["name"]).upper():
            continue
        rows.append(row)

    total = len(rows)
    start = max(0, offset)
    if limit is not None:
        rows = rows[start : start + max(0, limit)]
    elif start:
        rows = rows[start:]
    return rows, total
