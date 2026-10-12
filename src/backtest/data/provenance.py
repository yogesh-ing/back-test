"""Run provenance — *which* engine and *which* data produced these numbers.

Implements PRD ``docs/backTest-enhance.md`` Part 1 §1.1 (engine consistency)
and §1.2 (data-source visibility). Two numbers on screen are only meaningful
together with their provenance: a Sharpe of 1.4 from the fill-exact engine on
real prices is a different claim from the same Sharpe from the quick-screen
engine on a random walk.

Single authority for the labels — the API stamps the payload, the templates
and JS render it, and no page re-declares "Synthetic"/"Real (PostgreSQL)"
itself. Mirrors :mod:`backtest.data.source_tags`, which answers a different
question (the three-way run-classification taxonomy, not what the operator
needs to read on a results page).

Every payload is a plain dict of JSON-native values, so it can ride along in
the ``to_all()`` result and in a stored run row without a serialiser.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

__all__ = [
    "ENGINE_FILL_EXACT",
    "ENGINE_QUICK_SCREEN",
    "ENGINE_MIXED",
    "ENGINE_OPTIONS",
    "ENGINE_LABELS",
    "ENGINE_TIERS",
    "ENGINE_ALIASES",
    "SOURCE_LABELS",
    "REAL_SOURCES",
    "APPROXIMATE_WARNING",
    "MIXED_ENGINE_WARNING",
    "OPTIONS_ENGINE_NOTE",
    "normalize_engine",
    "engine_label",
    "engine_is_canonical",
    "describe_engine",
    "source_label",
    "source_is_real",
    "describe_source",
    "build_provenance",
]


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

#: Canonical engine — the driver over ``simulator/`` (next-bar-open fills,
#: Decimal-exact accounting). The same loop the forward paper run uses, so a
#: backtest and a paper run of one strategy cannot disagree.
ENGINE_FILL_EXACT = "backtest_driver"

#: Legacy vectorized screen (prev-close fills, fractional sizing, built-in
#: costs). Kept as an explicit opt-in "Fast Preview" only.
ENGINE_QUICK_SCREEN = "quick_screen"

#: Compare slots that did not all run on the same engine. Not an engine — a
#: statement that the per-slot stamps must each be read on their own, because
#: the numbers are not like-for-like.
ENGINE_MIXED = "mixed"

#: Multi-leg options driver (``optimization.config.ENGINES``). Real fills on
#: its own broker, but a different loop from the equity driver, so it is
#: labelled rather than claimed canonical.
ENGINE_OPTIONS = "options"

#: Config-level engine names (``backtestConfig.engine``) → provenance ids.
#: ``driver`` IS the fill-exact driver; only the spelling differs.
ENGINE_ALIASES: dict[str, str] = {
    "driver": ENGINE_FILL_EXACT,
    "quick_screen": ENGINE_QUICK_SCREEN,
}

#: engine id -> what the operator reads on the badge.
ENGINE_LABELS: dict[str, str] = {
    ENGINE_FILL_EXACT: "Fill-Exact (Canonical)",
    ENGINE_QUICK_SCREEN: "Quick-Screen (Approximate)",
    ENGINE_MIXED: "Mixed (see each result)",
    ENGINE_OPTIONS: "Options Engine (Multi-Leg)",
}

#: Certainty tier behind ``engine_canonical`` — canonical (fill-exact),
#: approximate (vectorized screen), mixed, options (own fill model) or unknown.
ENGINE_TIERS: dict[str, str] = {
    ENGINE_FILL_EXACT: "canonical",
    ENGINE_QUICK_SCREEN: "approximate",
    ENGINE_MIXED: "mixed",
    ENGINE_OPTIONS: "options",
}

APPROXIMATE_WARNING = (
    "These numbers are approximate. Switch to Full Engine for certification-grade results."
)

MIXED_ENGINE_WARNING = (
    "Some results ran on different engines — the comparison is not like-for-like. "
    "Re-run every slot on the Full Engine."
)

OPTIONS_ENGINE_NOTE = (
    "Options engine — multi-leg fills come from the options driver, not the shared "
    "fill-exact equity loop."
)


def normalize_engine(engine: Any) -> str:
    """Map any caller-level engine name onto the provenance vocabulary.

    An empty/unset engine means "whatever the default is", which IS the
    fill-exact driver — so it resolves to that rather than to a blank that
    would then read as an unknown engine.
    """
    key = str(engine or "").strip().lower()
    return ENGINE_ALIASES.get(key, key) or ENGINE_FILL_EXACT


def engine_label(engine: Any) -> str:
    """Human label for an engine id (unknown engines are labelled, not hidden)."""
    key = normalize_engine(engine)
    return ENGINE_LABELS.get(key, key or "Unknown")


def engine_is_canonical(engine: Any) -> bool:
    """True when the numbers came from the fill-exact engine."""
    return normalize_engine(engine) == ENGINE_FILL_EXACT


def describe_engine(engine: Any) -> dict[str, Any]:
    """Engine half of a provenance record."""
    key = normalize_engine(engine)
    return {
        "engine_used": key,
        "engine_label": engine_label(key),
        "engine_canonical": key == ENGINE_FILL_EXACT,
        "engine_tier": ENGINE_TIERS.get(key, "unknown"),
    }


# ---------------------------------------------------------------------------
# Data source
# ---------------------------------------------------------------------------

#: ``build_source`` name -> what the operator reads on the badge. Keys are the
#: app-level vocabulary (``app.config["BACKTEST_SOURCE"]``); taxonomy tags for
#: state files come from :data:`backtest.data.source_tags.APP_SOURCE_TAGS`.
SOURCE_LABELS: dict[str, str] = {
    "db": "Real (PostgreSQL)",
    "mstock": "Live (mStock)",
    "dhan": "Live (Dhan)",
    "synthetic": "Synthetic",
    "csv": "CSV",
}

#: Sources whose prices are real observations of a real instrument. Everything
#: else is a filter, not a certification.
REAL_SOURCES: frozenset[str] = frozenset({"db", "mstock"})


def source_label(name: Any) -> str:
    """Human label for a data source name."""
    key = str(name or "").strip().lower()
    return SOURCE_LABELS.get(key, key or "Unknown")


def source_is_real(name: Any) -> bool:
    """True when the run used real (broker or cached broker) price data."""
    return str(name or "").strip().lower() in REAL_SOURCES


def describe_source(name: Any) -> dict[str, Any]:
    """Data half of a provenance record."""
    # An unnamed source is labelled "Unknown", never defaulted to synthetic —
    # a provenance badge that guesses is worse than one that admits a gap.
    key = str(name or "").strip().lower()
    real = source_is_real(key)
    return {
        "data_source": key,
        "data_source_label": source_label(key),
        "data_source_real": real,
    }


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------


def _iso_date(value: Any) -> str | None:
    """``YYYY-MM-DD`` for a Timestamp/date/ISO string; ``None`` when unknown."""
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.strftime("%Y-%m-%d")
    text = str(value).strip()
    return text[:10] or None if text else None


def _last_ingested_at(source_obj: Any, symbol: str, timeframe: str | None) -> str | None:
    """Ask the source when its data was last fetched (``None`` if it can't say).

    Duck-typed on purpose: only :class:`~backtest.data.db_source.DbSource` can
    answer (it owns ``market_data_cache.ingested_at``), and a source that
    raises must degrade to "unknown", never fail a completed backtest.
    """
    getter = getattr(source_obj, "last_ingested_at", None)
    if not callable(getter):
        return None
    try:
        return _iso_date(getter(symbol, timeframe))
    except Exception:  # noqa: BLE001 — provenance is best-effort by contract
        return None


def build_provenance(
    *,
    source: Any,
    engine: Any = ENGINE_FILL_EXACT,
    symbol: str = "",
    timeframe: str = "",
    start_date: Any = None,
    end_date: Any = None,
    bars: int | None = None,
    data_from: Any = None,
    data_to: Any = None,
    data_fetch_date: Any = None,
    source_obj: Any = None,
) -> dict[str, Any]:
    """Build the provenance block attached to a result payload.

    ``start_date``/``end_date`` are what the operator *asked* for; ``data_from``/
    ``data_to`` are what the candles actually covered — they differ whenever a
    symbol has gaps, and the audit trail needs both. ``bars`` is the bar count
    the metrics were computed over.

    Returns JSON-native values only.
    """
    record: dict[str, Any] = {}
    record.update(describe_source(source))
    record.update(describe_engine(engine))
    record["symbol"] = str(symbol or "")
    record["timeframe"] = str(timeframe or "")
    record["date_range"] = {
        "from": _iso_date(start_date),
        "to": _iso_date(end_date),
    }
    record["data_from"] = _iso_date(data_from) or record["date_range"]["from"]
    record["data_to"] = _iso_date(data_to) or record["date_range"]["to"]
    # ``None`` means "this record does not cover bars of its own" (a Compare
    # shared block); ``0`` would claim the run had no bars, which is a real and
    # alarming condition the page must be able to distinguish.
    record["bars_count"] = None if bars is None else int(bars)

    fetch_date = _iso_date(data_fetch_date)
    if fetch_date is None and source_obj is not None:
        fetch_date = _last_ingested_at(source_obj, record["symbol"], record["timeframe"])
    if fetch_date is None and record["data_source"] in REAL_SOURCES:
        # A cached-feed result is exactly as fresh as its newest bar; saying
        # "unknown" would understate what the operator actually has.
        fetch_date = record["data_to"]
    record["data_fetch_date"] = fetch_date

    record["warnings"] = _warnings(record)
    return record


def _warnings(record: dict[str, Any]) -> list[dict[str, str]]:
    """Advisory banners the result page must show, worst first.

    Advisory only — nothing here blocks a run (hard blocks live in the Optimize
    gate and the Paper→Live gate).
    """
    out: list[dict[str, str]] = []
    if not record.get("data_source_real"):
        label = record.get("data_source_label") or "Non-real"
        out.append(
            {
                "level": "error",
                "code": "non_real_data",
                "message": (
                    f"Results based on {label} data. Real-data certification required "
                    "before paper testing."
                ),
            }
        )
    engine = record.get("engine_used")
    if engine == ENGINE_QUICK_SCREEN:
        out.append(
            {"level": "warning", "code": "approximate_engine", "message": APPROXIMATE_WARNING}
        )
    elif engine == ENGINE_MIXED:
        out.append({"level": "warning", "code": "mixed_engine", "message": MIXED_ENGINE_WARNING})
    elif engine == ENGINE_OPTIONS:
        out.append({"level": "info", "code": "options_engine", "message": OPTIONS_ENGINE_NOTE})
    elif not record.get("engine_canonical"):
        out.append(
            {
                "level": "warning",
                "code": "unknown_engine",
                "message": f"Unknown engine '{engine}' — these numbers are not reproducible.",
            }
        )
    return out
