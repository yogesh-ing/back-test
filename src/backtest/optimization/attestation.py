"""Data attestation — *what data ran, said before the run, kept after it.*

Implements PRD ``docs/backTest-enhance.md`` Part 2 §2. The complaint it
answers: *"Currently Optimize pulls candles from whatever source the app
started with, and there's no visible confirmation of this anywhere."*

The gap this closes is not the data. It is that nobody had to *say* which
data. A run that optimized 240 parameter combinations against RELIANCE daily
bars and a run that optimized them against a random walk produce numbers that
look identical on the page — the second one is not wrong, it just is not a
claim about RELIANCE, and nothing on screen said so.

Three moments, and they are deliberately different:

1. **Before the start** (:func:`attestation_preview`) — what the source will
   supply for the symbol and range just chosen. Advisory, except for
   synthetic, which the PRD says cannot proceed unacknowledged.
2. **On the run record** (:func:`attestation_record`) — the same facts measured
   on the candles the search *actually* loaded, which is the only version
   worth keeping: a symbol with gaps reports fewer bars than were asked for.
3. **On results and the audit trail** — read back, never recomputed.

The synthetic gate is the one hard stop in the Optimize flow, and it is here
rather than in the live gate because it is a statement about the *inputs* and
is knowable before a single backtest runs. Everything else warns and allows:
blocking a run because the cache is 40 days old would train operators to
ignore the box entirely.

Reuses :mod:`backtest.data.provenance` rather than restating it. That module
already answers "which source" for Backtest and Compare; two authorities for
one label is how a page ends up saying "Synthetic" in one place and "demo
data" in another.
"""

from __future__ import annotations

import datetime
from typing import Any

from backtest.data.provenance import (
    REAL_SOURCES,
    build_provenance,
    source_is_real,
    source_label,
)

__all__ = [
    "STALE_AFTER_DAYS",
    "SYNTHETIC_ACKNOWLEDGEMENT",
    "build_attestation",
    "attestation_preview",
    "attestation_record",
    "attestation_columns",
    "attestation_is_satisfied",
    "stale_days",
    "attestation_warnings",
]

#: A cached feed older than this is worth a sentence on the setup page. It is
#: a prompt to re-fetch, not a reason to refuse: the operator may be optimizing
#: a deliberately historical window, and only they know that.
STALE_AFTER_DAYS = 30

#: The exact sentence the operator ticks. Quoted into the run record verbatim
#: so the audit trail shows what was actually agreed to, not a paraphrase
#: written later by whatever renders it.
SYNTHETIC_ACKNOWLEDGEMENT = (
    "I understand this is synthetic data and results are not certification-grade"
)


def _today() -> datetime.date:
    return datetime.date.today()


def stale_days(fetch_date: Any, *, today: datetime.date | None = None) -> int | None:
    """Age of the feed in days, or ``None`` when it cannot be dated.

    ``None`` and ``0`` are different answers: ``0`` means fetched today, while
    ``None`` means the source cannot say. The second is the one that needs
    saying out loud.
    """
    if not fetch_date:
        return None
    try:
        stamp = datetime.date.fromisoformat(str(fetch_date)[:10])
    except (TypeError, ValueError):
        return None
    return max(0, ((today or _today()) - stamp).days)


def _coerce_bars(count: Any) -> int:
    try:
        return max(0, int(count))
    except (TypeError, ValueError):
        return 0


def build_attestation(
    *,
    source: Any,
    symbol: str = "",
    timeframe: str = "",
    start_date: Any = None,
    end_date: Any = None,
    bars: int | None = None,
    data_from: Any = None,
    data_to: Any = None,
    data_fetch_date: Any = None,
    source_obj: Any = None,
    today: datetime.date | None = None,
) -> dict[str, Any]:
    """The attestation record. JSON-native throughout, so it can be stored in
    a JSON column and returned by an API without a serialiser.

    ``bars=None`` means "not yet measured" (the pre-start preview); ``bars=0``
    means the source was asked and had nothing, which is a real and alarming
    condition the caller must be able to tell apart.
    """
    prov = build_provenance(
        source=source,
        symbol=symbol,
        timeframe=timeframe,
        start_date=start_date,
        end_date=end_date,
        bars=bars,
        data_from=data_from,
        data_to=data_to,
        data_fetch_date=data_fetch_date,
        source_obj=source_obj,
    )
    age = stale_days(prov.get("data_fetch_date"), today=today)
    real = bool(prov.get("data_source_real"))
    return {
        "data_source": prov["data_source"],
        "data_source_label": prov["data_source_label"],
        "data_source_real": real,
        "symbol": prov["symbol"],
        "timeframe": prov["timeframe"],
        "bars_count": prov["bars_count"],
        "date_from": prov["data_from"],
        "date_to": prov["data_to"],
        "data_fetch_date": prov["data_fetch_date"],
        "stale_days": age,
        "stale": age is not None and age > STALE_AFTER_DAYS,
        # Synthetic is the one input that cannot be defended after the fact, so
        # it carries its own gate. Real-but-stale warns; synthetic is refused.
        "requires_acknowledgement": not real,
        "acknowledgement_required": SYNTHETIC_ACKNOWLEDGEMENT if not real else None,
    }


def attestation_preview(
    source: Any,
    *,
    symbol: str = "",
    timeframe: str = "",
    start_date: Any = None,
    end_date: Any = None,
    source_obj: Any = None,
    data_fetch_date: Any = None,
    today: datetime.date | None = None,
) -> dict[str, Any]:
    """Step 1 — the setup page's confirmation box.

    Bar count and actual coverage are deliberately left unknown here: they are
    not knowable until the candles are fetched, and a box that guesses would
    be a box that lies. The page shows them as "—" and the run record fills
    them in.
    """
    return build_attestation(
        source=source,
        symbol=symbol,
        timeframe=timeframe,
        start_date=start_date,
        end_date=end_date,
        bars=None,
        source_obj=source_obj,
        data_fetch_date=data_fetch_date,
        today=today,
    )


def attestation_record(
    source: Any,
    candles: Any = None,
    *,
    symbol: str = "",
    timeframe: str = "",
    start_date: Any = None,
    end_date: Any = None,
    source_obj: Any = None,
    data_fetch_date: Any = None,
    today: datetime.date | None = None,
) -> dict[str, Any]:
    """Step 2 — measured on the candles the search actually loaded.

    This is the only version worth persisting. Asking a source for four years
    of a symbol it has two years of returns two years, and a record built
    from the *request* would certify a run over data that was never there.
    """
    count: int | None = None
    first: Any = None
    last: Any = None
    if candles is not None:
        try:
            count = int(len(candles))
        except TypeError:
            count = None
        if count:
            index = getattr(candles, "index", None)
            if index is not None:
                try:
                    first, last = index[0], index[-1]
                except (IndexError, TypeError, KeyError):
                    first = last = None
    return build_attestation(
        source=source,
        symbol=symbol,
        timeframe=timeframe,
        start_date=start_date,
        end_date=end_date,
        bars=count,
        data_from=first,
        data_to=last,
        source_obj=source_obj,
        data_fetch_date=data_fetch_date,
        today=today,
    )


def attestation_is_satisfied(attestation: dict[str, Any] | None) -> bool:
    """May this run start?

    The single hard gate in the Optimize flow. Everything else — staleness, a
    thin bar count, a source that will not date itself — is a warning the
    operator can read and overrule, because a box that refuses work for
    reasons the operator cannot act on trains them to ignore it.
    """
    if not attestation:
        return False
    if attestation.get("data_source_real"):
        return True
    if attestation.get("acknowledged"):
        return True
    return not attestation.get("requires_acknowledgement", False)


def attestation_warnings(attestation: dict[str, Any] | None) -> list[dict[str, str]]:
    """Advisory lines for the setup box, worst first. Advisory is the operative
    word: none of these stop a run."""
    if not attestation:
        return []
    out: list[dict[str, str]] = []
    label = attestation.get("data_source_label") or "this source"
    if not attestation.get("data_source_real"):
        out.append(
            {
                "level": "error",
                "code": "synthetic_data",
                "message": (
                    f"{label} data. Results from this run are a demonstration of the "
                    "method, not a claim about the instrument."
                ),
            }
        )
    age = attestation.get("stale_days")
    if age is None and attestation.get("data_source_real"):
        out.append(
            {
                "level": "warning",
                "code": "unknown_fetch_date",
                "message": (
                    f"{label} cannot say when this data was last fetched. "
                    "Freshness cannot be confirmed."
                ),
            }
        )
    elif attestation.get("stale"):
        out.append(
            {
                "level": "warning",
                "code": "stale_data",
                "message": (
                    f"Data was last fetched {age} days ago "
                    f"(over {STALE_AFTER_DAYS}). Consider re-fetching before optimizing."
                ),
            }
        )
    bars = attestation.get("bars_count")
    if bars is not None and _coerce_bars(bars) == 0:
        out.append(
            {
                "level": "error",
                "code": "no_bars",
                "message": f"No bars available from {label} for this symbol and range.",
            }
        )
    return out


def _as_date(value: Any) -> datetime.date | None:
    """``YYYY-MM-DD`` → a real ``date`` for the Date columns.

    The JSON record keeps strings (it has to survive a round trip through a
    JSON column and an HTTP response); the flat columns are typed ``Date`` so
    a run list can be filtered on them. Passing the string straight through
    works on PostgreSQL and raises on SQLite, which is the kind of difference
    that only shows up on a developer machine.
    """
    if value in (None, ""):
        return None
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    try:
        return datetime.date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def attestation_columns(attestation: dict[str, Any] | None) -> dict[str, Any]:
    """The flat column set for ``optimization_runs`` (migration 015).

    Kept as one function so the columns and the model cannot drift apart: a
    column added here without a model attribute is a column nobody can read
    back, which is worse than not having it.
    """
    att = attestation or {}
    bars = att.get("bars_count")
    return {
        "data_source": att.get("data_source"),
        "data_fetch_date": _as_date(att.get("data_fetch_date")),
        "bars_count": None if bars is None else _coerce_bars(bars),
        "symbol": att.get("symbol") or None,
        "timeframe": att.get("timeframe") or None,
        "date_from": _as_date(att.get("date_from")),
        "date_to": _as_date(att.get("date_to")),
        "data_attestation": att or None,
    }


def describe_source_briefly(name: Any) -> str:
    """Small helper for error messages that only need the label."""
    return source_label(name) if name else "unknown"


def is_real_source(name: Any) -> bool:
    """Re-exported so callers need not import two modules to ask one question."""
    return source_is_real(name) or str(name or "").strip().lower() in REAL_SOURCES
