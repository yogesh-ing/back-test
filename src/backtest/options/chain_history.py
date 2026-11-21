"""Capture writer into ``option_chain_history`` (plan V3 Phase 4, §7.1/§7.2).

This is the durable home for recorded option chains — the asset that lets a
backtest replay real premiums (``chain_history``) instead of Black-Scholes. It
is source-agnostic: the HTTP :class:`~backtest.options.chain_snapshots.ChainSnapshotRecorder`
rides it through an optional hook, and the Upstox full-mode depth tape feeds it
through :func:`history_row_from_depth_packet`. Every batch passes the §7.1
parity/junk screen on the way in; rejects go to ``data/chain_rejected.log``,
never silently dropped, and only clean rows reach the table.

Two invariants govern the write:

* **Never fabricate.** A field the venue did not publish stays NULL — absent
  greeks are not a BS guess, an absent ladder is not an invented book. The
  writer only persists what was captured.
* **Idempotent.** The unique key ``(underlying, expiry, option_type, strike,
  bar_ts, source)`` means re-running a capture must not duplicate a bar. Rows
  whose key already exists are skipped and counted, not re-inserted.

Clock policy: ``bar_ts`` (the replay key) and ``snapshot_ts`` (capture time) are
stored aware UTC. A naive stamp enters through the declared exchange-time door
(:func:`from_market_clock`), never a guessed ``replace(tzinfo=...)``.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Optional

from backtest.db.models import OptionChainHistory
from backtest.options.chain_filter import (
    DEFAULT_REJECT_LOG,
    PARITY_MULT,
    ChainRejectLog,
    screen_chain_rows,
)
from backtest.timekeeping import EXCHANGE_TZ, from_market_clock, from_stored_utc, now_utc

logger = logging.getLogger("backtest.options.chain_history")

__all__ = [
    "ChainHistoryWriter",
    "history_rows_from_snapshots",
    "history_row_from_depth_packet",
    "load_chain_history",
    "chain_coverage",
]


def _dec(value: Any) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out or abs(out) == float("inf"):
        return None
    return out


def _aware_utc(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        # Naive capture stamps are exchange time — the declared door.
        value = from_market_clock(value)
    return value.astimezone(timezone.utc)


def _option_type(value: Any) -> Optional[str]:
    raw = str(getattr(value, "value", value) or "").upper()
    if raw.startswith("C"):
        return "CE"
    if raw.startswith("P"):
        return "PE"
    return None


def _expiry(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


class ChainHistoryWriter:
    """Screen a capture batch and persist the clean rows, idempotently."""

    def __init__(
        self,
        manager: Any,
        *,
        reject_log_path: str = DEFAULT_REJECT_LOG,
        risk_free: float = 0.065,
        parity_mult: float = PARITY_MULT,
    ) -> None:
        self.manager = manager
        self.reject_log_path = str(reject_log_path)
        self.risk_free = float(risk_free)
        self.parity_mult = float(parity_mult)

    def ensure_schema(self) -> None:
        self.manager.connect()
        from backtest.db.models import Base

        Base.metadata.create_all(
            self.manager.engine, tables=[OptionChainHistory.__table__]
        )

    def write_rows(
        self,
        rows: Iterable[dict],
        *,
        source: str,
        bar_ts: datetime | None = None,
    ) -> dict[str, int]:
        """Normalize → screen → persist. Returns accepted/rejected/written/dupes.

        ``source`` names the venue on every row (e.g. ``mstock``, ``upstox``).
        ``bar_ts`` is the default replay key for rows that carry none of their
        own; a row's own ``bar_ts`` wins when present.
        """
        default_bar = _aware_utc(bar_ts) or now_utc()
        snapshot_ts = now_utc()
        normalized: list[dict] = []
        for raw in rows:
            row = self._normalize(
                raw, source=source, default_bar=default_bar, snapshot_ts=snapshot_ts
            )
            if row is not None:
                normalized.append(row)

        sink = ChainRejectLog(self.reject_log_path)
        accepted, rejected = screen_chain_rows(
            normalized,
            risk_free=self.risk_free,
            parity_mult=self.parity_mult,
            reject_sink=sink.write,
        )

        written, dupes = self._persist(accepted)
        logger.info(
            "[chain-history] %s: %d accepted, %d rejected, %d written, %d duplicate",
            source, len(accepted), len(rejected), written, dupes,
        )
        return {
            "accepted": len(accepted),
            "rejected": len(rejected),
            "written": written,
            "duplicates": dupes,
        }

    def _normalize(
        self, raw: dict, *, source: str, default_bar: datetime, snapshot_ts: datetime
    ) -> Optional[dict]:
        underlying = str(raw.get("underlying") or "").upper()
        expiry = _expiry(raw.get("expiry"))
        strike = _dec(raw.get("strike"))
        option_type = _option_type(raw.get("option_type"))
        if not underlying or expiry is None or strike is None or option_type is None:
            # A row without contract identity cannot be a replay key — drop it
            # loudly rather than persist an unaddressable row.
            logger.warning("[chain-history] dropping row without contract identity: %r", raw)
            return None
        bar_ts = _aware_utc(raw.get("bar_ts")) or default_bar
        depth = raw.get("depth_json")
        if depth is None and (raw.get("bids") is not None or raw.get("asks") is not None):
            depth = {"bid": raw.get("bids") or [], "ask": raw.get("asks") or []}
        return {
            "underlying": underlying,
            "expiry": expiry,
            "strike": strike,
            "option_type": option_type,
            "bar_ts": bar_ts,
            "snapshot_ts": _aware_utc(raw.get("snapshot_ts")) or snapshot_ts,
            "source": str(raw.get("source") or source),
            "instrument_token": str(raw.get("instrument_token") or "") or None,
            "trading_symbol": str(raw.get("trading_symbol") or "") or None,
            "lot_size": _int(raw.get("lot_size")),
            "ltp": _dec(raw.get("ltp", raw.get("last_price"))),
            "bid": _dec(raw.get("bid", raw.get("best_bid"))),
            "ask": _dec(raw.get("ask", raw.get("best_ask"))),
            "iv": _float(raw.get("iv")),
            "delta": _float(raw.get("delta")),
            "gamma": _float(raw.get("gamma")),
            "theta": _float(raw.get("theta")),
            "vega": _float(raw.get("vega")),
            "volume": _int(raw.get("volume")),
            "oi": _int(raw.get("oi", raw.get("open_interest"))),
            "spot": _dec(raw.get("spot")),
            "depth_json": depth if isinstance(depth, dict) else None,
        }

    def _persist(self, accepted: list[dict]) -> tuple[int, int]:
        if not accepted:
            return 0, 0
        existing = self._existing_keys(accepted)
        to_add: list[OptionChainHistory] = []
        dupes = 0
        for row in accepted:
            key = (
                row["underlying"], row["expiry"], row["option_type"],
                row["strike"], from_stored_utc(row["bar_ts"]), row["source"],
            )
            if key in existing:
                dupes += 1
                continue
            existing.add(key)  # guard against dups within the same batch
            to_add.append(OptionChainHistory(**row))
        if to_add:
            with self.manager.session() as session:
                session.add_all(to_add)
        return len(to_add), dupes

    def _existing_keys(self, accepted: list[dict]) -> set:
        from sqlalchemy import select

        groups: dict[tuple, set] = {}
        for row in accepted:
            groups.setdefault((row["underlying"], row["source"]), set()).add(row["bar_ts"])
        existing: set = set()
        with self.manager.session() as session:
            for (underlying, source), stamps in groups.items():
                stmt = select(
                    OptionChainHistory.expiry,
                    OptionChainHistory.option_type,
                    OptionChainHistory.strike,
                    OptionChainHistory.bar_ts,
                ).where(
                    OptionChainHistory.underlying == underlying,
                    OptionChainHistory.source == source,
                    OptionChainHistory.bar_ts.in_(sorted(stamps)),
                )
                for expiry, option_type, strike, bar_ts in session.execute(stmt):
                    existing.add(
                        (underlying, expiry, option_type, strike, from_stored_utc(bar_ts), source)
                    )
        return existing


def history_rows_from_snapshots(
    rows: Iterable[Any], bar_ts: datetime, source: str
) -> list[dict]:
    """Adapt ``OptionChainSnapshot`` ORM rows (the HTTP L1 capture) to history rows.

    The HTTP chain endpoint publishes L1 only — no ladder, no greeks — so those
    fields stay NULL. That is honest: the writer never invents a depth the venue
    did not send.
    """
    out: list[dict] = []
    for r in rows:
        get = (lambda k: getattr(r, k, None)) if not isinstance(r, dict) else (lambda k: r.get(k))
        out.append(
            {
                "underlying": get("underlying"),
                "expiry": get("expiry"),
                "strike": get("strike"),
                "option_type": get("option_type"),
                "bar_ts": bar_ts,
                "snapshot_ts": get("snapshot_ts") or bar_ts,
                "source": source,
                "instrument_token": get("instrument_token"),
                "trading_symbol": get("trading_symbol"),
                "lot_size": get("lot_size"),
                "ltp": get("ltp"),
                "bid": get("bid"),
                "ask": get("ask"),
                "volume": get("volume"),
                "oi": get("oi"),
                "spot": get("spot"),
            }
        )
    return out


def history_row_from_depth_packet(
    packet: dict,
    *,
    contract: Any,
    spot: Any = None,
    source: str,
    bar_ts: datetime | None = None,
) -> dict:
    """Join one Upstox full-mode tape packet to its contract terms → a history row.

    The tape is token-keyed (``bids``/``asks`` five-level ladders, ``oi``,
    ``volume``, stamps); the strike/expiry/type/lot live in the instrument
    master, supplied here as ``contract``. ``depth_json`` carries the ladder
    verbatim (§7.2). Greeks ride along only if the packet published them.
    """
    get = (
        (lambda k: getattr(contract, k, None))
        if not isinstance(contract, dict)
        else (lambda k: contract.get(k))
    )
    moment = bar_ts or packet.get("exchange_ts") or packet.get("recv_utc")
    return {
        "underlying": get("underlying"),
        "expiry": get("expiry"),
        "strike": get("strike"),
        "option_type": get("option_type"),
        "bar_ts": moment,
        "source": source,
        "instrument_token": get("instrument_token") or packet.get("token"),
        "trading_symbol": get("trading_symbol") or packet.get("symbol"),
        "lot_size": get("lot_size"),
        "ltp": packet.get("ltp"),
        "bid": packet.get("bid"),
        "ask": packet.get("ask"),
        "iv": packet.get("iv"),
        "delta": packet.get("delta"),
        "gamma": packet.get("gamma"),
        "theta": packet.get("theta"),
        "vega": packet.get("vega"),
        "volume": packet.get("volume"),
        "oi": packet.get("oi"),
        "spot": spot,
        "depth_json": {"bid": packet.get("bids") or [], "ask": packet.get("asks") or []},
    }


# ---------------------------------------------------------------------------
# Research reads (the chain_history replay source)
# ---------------------------------------------------------------------------


def _day_bounds_utc(from_date: Any, to_date: Any) -> tuple[datetime, datetime]:
    """IST trading-day range → aware UTC instants for the bar_ts query."""
    start = date.fromisoformat(str(from_date)[:10])
    end = date.fromisoformat(str(to_date)[:10])
    lo = datetime.combine(start, time.min, tzinfo=EXCHANGE_TZ).astimezone(timezone.utc)
    hi = datetime.combine(end, time.max, tzinfo=EXCHANGE_TZ).astimezone(timezone.utc)
    return lo, hi


def load_chain_history(
    manager: Any,
    underlying: str,
    from_date: Any,
    to_date: Any,
    *,
    expiry: date | None = None,
    limit: int = 2_000_000,
) -> list[dict]:
    """Recorded chain rows for a replay range, as plain dicts.

    ``bar_ts`` is normalized to aware UTC through the stored-UTC door so the
    in-memory reader's exchange-time conversion is correct on both Postgres
    (aware) and SQLite (naive on read). Only clean rows ever landed here — the
    §7.1 screen ran at capture — so the replay does not re-screen.
    """
    from sqlalchemy import select

    lo, hi = _day_bounds_utc(from_date, to_date)
    stmt = (
        select(OptionChainHistory)
        .where(
            OptionChainHistory.underlying == str(underlying).upper(),
            OptionChainHistory.bar_ts >= lo,
            OptionChainHistory.bar_ts <= hi,
        )
        .order_by(OptionChainHistory.bar_ts)
        .limit(int(limit))
    )
    if expiry is not None:
        stmt = stmt.where(OptionChainHistory.expiry == expiry)
    out: list[dict] = []
    with manager.session() as session:
        for r in session.execute(stmt).scalars():
            out.append(
                {
                    "underlying": r.underlying,
                    "expiry": r.expiry,
                    "strike": float(r.strike) if r.strike is not None else None,
                    "option_type": r.option_type,
                    "bar_ts": from_stored_utc(r.bar_ts),
                    "bid": float(r.bid) if r.bid is not None else None,
                    "ask": float(r.ask) if r.ask is not None else None,
                    "ltp": float(r.ltp) if r.ltp is not None else None,
                    "iv": r.iv,
                    "delta": r.delta,
                    "gamma": r.gamma,
                    "theta": r.theta,
                    "vega": r.vega,
                    "volume": r.volume,
                    "oi": r.oi,
                    "spot": float(r.spot) if r.spot is not None else None,
                }
            )
    return out


def chain_coverage(manager: Any, underlying: str) -> dict[str, Any]:
    """Stored bar_ts range + row count for one underlying (the refusal message)."""
    from sqlalchemy import func, select

    stmt = select(
        func.count(OptionChainHistory.id),
        func.min(OptionChainHistory.bar_ts),
        func.max(OptionChainHistory.bar_ts),
    ).where(OptionChainHistory.underlying == str(underlying).upper())
    with manager.session() as session:
        count, lo, hi = session.execute(stmt).one()
    return {
        "count": int(count or 0),
        "from": from_stored_utc(lo).date().isoformat() if lo else None,
        "to": from_stored_utc(hi).date().isoformat() if hi else None,
    }
