"""Option-chain snapshot capture — the P1.1 data asset (architect review §3.4).

Forward tests become real the day live chains are wired (P1.1), but options
*backtests* stay synthetic-BS forever unless chain snapshots are **stored**
from that same day: premiums, OI and the real strike/expiry lattice cannot be
reconstructed after the fact. This module is the smallest thing that fixes
that asymmetry permanently.

``ChainSnapshotRecorder.record(underlying)``:

1. **Contract terms** — ``broker.get_option_chain(underlying)``: the full
   instrument-master slice (strike, expiry, type, token, symbol, lot) in ONE
   API call. Always captured — it is the research lattice.
2. **L1 quotes** — for the nearest ``quote_expiries`` expiries,
   ``broker.get_option_chain_data(underlying, expiry_code, token)`` returns
   bid/ask/LTP/OI for the whole expiry in ONE call (never per-strike polling;
   the §5.2 rate-limit rule). Quote failures are per-expiry, logged, and
   never abort the run — terms rows still land.
3. Every row of one run shares one ``snapshot_ts`` (UTC, tz-aware); writes
   are append-only batches into ``option_chain_snapshots`` (model:
   :class:`~backtest.db.models.OptionChainSnapshot`).

Research reads: :func:`load_snapshots` — filter by underlying/expiry/type
and time range, plain dicts back.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Optional

from backtest.db.models import OptionChainSnapshot
from backtest.options.quote_providers import LiveChainProvider

logger = logging.getLogger("backtest.options.chain_snapshots")

__all__ = ["ChainSnapshotRecorder", "load_snapshots"]


def _dec(value: Any) -> Optional[Decimal]:
    """Best-effort Decimal (``None`` on absent/unparseable)."""
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _int(value: Any) -> Optional[int]:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _extract_chain_rows(payload: Any) -> list[dict[str, Any]]:
    """Pull per-strike rows out of a ``get_option_chain_data`` response.

    The endpoint's envelope has shifted across mStock versions, so accept
    every shape seen in the wild: a bare list, ``{"data": [...]}``,
    ``{"data": {"chain": [...]}}``, ``{"options": [...]}``. Unknown shapes
    yield ``[]`` (terms-only snapshot) rather than an exception.
    """
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("data", "options", "chain", "records"):
        inner = payload.get(key)
        if isinstance(inner, list):
            return [row for row in inner if isinstance(row, dict)]
        if isinstance(inner, dict):
            for nested_key in ("chain", "options", "data"):
                nested = inner.get(nested_key)
                if isinstance(nested, list):
                    return [row for row in nested if isinstance(row, dict)]
    return []


class ChainSnapshotRecorder:
    """Append chain snapshots for one or more underlyings to the database.

    Parameters
    ----------
    client:
        Anything with ``get_option_chain(underlying) -> list[OptionContract]``
        and ``get_option_chain_data(underlying, expiry_code, token) -> dict``
        — in practice an authenticated :class:`~backtest.brokers.mstock.MStockBroker`
        (tests use a fake).
    manager:
        A connected :class:`~backtest.db.manager.DatabaseManager`.
    """

    def __init__(self, client: Any, manager: Any) -> None:
        self.client = client
        self.manager = manager

    def ensure_schema(self) -> None:
        """Create ``option_chain_snapshots`` if absent (idempotent).

        Production databases get the table from migration; this keeps fresh
        SQLite dev/test databases working without a migration run (the
        ``trade_structures`` pattern).
        """
        self.manager.connect()
        from backtest.db.models import Base

        Base.metadata.create_all(
            self.manager.engine, tables=[OptionChainSnapshot.__table__]
        )

    # ------------------------------------------------------------------
    # Capture
    # ------------------------------------------------------------------

    def record(
        self,
        underlying: str,
        spot: float | None = None,
        quote_expiries: int = 1,
        snapshot_ts: datetime | None = None,
    ) -> int:
        """Snapshot one underlying; returns rows written.

        ``quote_expiries`` caps how many expiries get the (one-call-each)
        quote enrichment — default 1, the nearest expiry, which is where
        the traded liquidity lives.
        """
        underlying = str(underlying).upper()
        ts = snapshot_ts or datetime.now(timezone.utc)
        contracts = list(self.client.get_option_chain(underlying))
        if not contracts:
            logger.warning("[chain-snap] no contracts for %s — nothing recorded", underlying)
            return 0

        # Quote enrichment per expiry (one API call per expiry, best-effort).
        quotes: dict[str, dict[str, Any]] = {}  # instrument_token → raw quote row
        expiries = sorted({c.expiry for c in contracts if getattr(c, "expiry", None)})
        for expiry in expiries[: max(int(quote_expiries), 0)]:
            token = self._underlying_token(contracts)
            try:
                payload = self.client.get_option_chain_data(
                    underlying, LiveChainProvider._expiry_code(expiry), token or ""
                )
            except Exception as exc:  # noqa: BLE001 — quotes enrich, terms survive
                logger.warning(
                    "[chain-snap] quote fetch failed for %s %s: %s", underlying, expiry, exc
                )
                continue
            for row in _extract_chain_rows(payload):
                token_key = str(
                    row.get("instrument_token")
                    or row.get("token")
                    or ""
                )
                if token_key:
                    quotes[token_key] = row

        rows: list[OptionChainSnapshot] = []
        for c in contracts:
            raw = quotes.get(str(c.instrument_token), {})
            option_type = str(getattr(c.option_type, "value", c.option_type)).upper()
            option_type = "CE" if option_type.startswith("C") else "PE"
            rows.append(
                OptionChainSnapshot(
                    snapshot_ts=ts,
                    source="mstock",
                    underlying=underlying,
                    expiry=c.expiry,
                    strike=Decimal(str(c.strike)),
                    option_type=option_type,
                    instrument_token=str(c.instrument_token),
                    trading_symbol=str(c.trading_symbol),
                    lot_size=_int(getattr(c, "lot_size", None)),
                    ltp=_dec(raw.get("ltp", raw.get("last_price"))),
                    bid=_dec(raw.get("bid", raw.get("best_bid"))),
                    ask=_dec(raw.get("ask", raw.get("best_ask"))),
                    volume=_int(raw.get("volume")),
                    oi=_int(raw.get("oi", raw.get("open_interest"))),
                    spot=_dec(spot),
                )
            )

        with self.manager.session() as session:
            session.add_all(rows)
        logger.info(
            "[chain-snap] %s: %d rows (%d with quotes) at %s",
            underlying,
            len(rows),
            len(quotes),
            ts.isoformat(),
        )
        return len(rows)

    def record_all(
        self,
        underlyings: Iterable[str],
        spots: dict[str, float] | None = None,
        quote_expiries: int = 1,
        snapshot_ts: datetime | None = None,
    ) -> dict[str, int]:
        """Snapshot several underlyings; ``{underlying: rows_written}``."""
        spots = spots or {}
        out: dict[str, int] = {}
        for underlying in underlyings:
            out[str(underlying).upper()] = self.record(
                str(underlying),
                spot=spots.get(str(underlying).upper()),
                quote_expiries=quote_expiries,
                snapshot_ts=snapshot_ts,
            )
        return out

    @staticmethod
    def _underlying_token(contracts: list[Any]) -> str:
        """The underlying's own index token, if a contract carries one.

        ``get_option_chain_data`` addresses the expiry by the UNDERLYING's
        token, not the option's. mStock contract metadata carries it when
        present; otherwise the caller's client resolves it (empty string →
        the client-side default).
        """
        for c in contracts:
            meta = getattr(c, "metadata", None) or {}
            token = meta.get("underlying_token") if isinstance(meta, dict) else None
            if token:
                return str(token)
        return ""


# ---------------------------------------------------------------------------
# Research reads
# ---------------------------------------------------------------------------


def load_snapshots(
    manager: Any,
    underlying: str | None = None,
    expiry: date | None = None,
    option_type: str | None = None,
    limit: int = 10_000,
) -> list[dict[str, Any]]:
    """Snapshot rows as plain dicts (newest batch first) for research code."""
    from sqlalchemy import select

    stmt = select(OptionChainSnapshot).order_by(
        OptionChainSnapshot.snapshot_ts.desc(), OptionChainSnapshot.strike
    )
    if underlying:
        stmt = stmt.where(OptionChainSnapshot.underlying == str(underlying).upper())
    if expiry:
        stmt = stmt.where(OptionChainSnapshot.expiry == expiry)
    if option_type:
        stmt = stmt.where(OptionChainSnapshot.option_type == option_type.upper())
    stmt = stmt.limit(int(limit))

    out: list[dict[str, Any]] = []
    with manager.session() as session:
        for row in session.execute(stmt).scalars():
            out.append(
                {
                    "snapshot_ts": row.snapshot_ts,
                    "source": row.source,
                    "underlying": row.underlying,
                    "expiry": row.expiry,
                    "strike": row.strike,
                    "option_type": row.option_type,
                    "instrument_token": row.instrument_token,
                    "trading_symbol": row.trading_symbol,
                    "lot_size": row.lot_size,
                    "ltp": row.ltp,
                    "bid": row.bid,
                    "ask": row.ask,
                    "volume": row.volume,
                    "oi": row.oi,
                    "spot": row.spot,
                }
            )
    return out
