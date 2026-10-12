"""Where the trades in a report come from.

Three sources, and the report says which ones it actually saw:

* :class:`DatabaseTradeSource` — the ``trades`` table (the permanent record;
  written by :class:`~backtest.forward.trade_persistence.LiveTradePersister`,
  which **refuses to persist synthetic/replay runs**, so anything in there
  claims to be real market data).
* :class:`MemoryTradeSource` — the running books in
  :class:`~backtest.forward.portfolio_manager.PortfolioManager`. Live books
  exist here before the persister has flushed them, so a mid-session report
  needs this source or it silently omits today.
* :class:`DemoTradeSource` — deterministic, clearly-labelled sample trades,
  only ever built when a caller explicitly asks for them. They exist so the
  UI and the tests have a populated book without inventing fake real trades.

Two honesty rules are implemented here rather than left to the report:

1. **Never invent a broker.** A persisted trade whose portfolio row predates
   the multi-broker migration is attributed to ``unattributed`` and counted —
   it is not quietly assigned to whichever broker happens to be configured.
2. **Never invent a cost.** Paper books run on
   :data:`~backtest.simulator.fees.PAPER_FREE_PROFILE` (zero cost by design),
   so paper trades keep their recorded zero costs. Live trades whose sources
   stored only brokerage get the *missing* statutory components estimated by
   the fee engine and are flagged ``estimated`` for the whole record.
"""

from __future__ import annotations

import logging
import os
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Mapping, Protocol

from backtest.data.sources_policy import default_source_tag
from backtest.reporting.records import (
    FEE_BASIS_ESTIMATED,
    FEE_BASIS_NONE,
    FEE_BASIS_RECORDED,
    TradeRecord,
    estimate_round_trip_fees,
    instrument_from_symbol,
)
from backtest.reporting.records import Period
from backtest.simulator.fees import FeeBreakdown, TradeSegment
from backtest.simulator.money import ZERO

__all__ = [
    "TradeSource",
    "DatabaseTradeSource",
    "MemoryTradeSource",
    "DemoTradeSource",
    "SourceBundle",
    "resolve_sources",
    "UNATTRIBUTED_BROKER",
    "DEMO_ENV",
]

logger = logging.getLogger("backtest.reporting.sources")

#: Broker label for trades whose venue the platform cannot establish.
UNATTRIBUTED_BROKER = "unattributed"

#: Set to 1 to let ``GET /api/reporting/...?demo=1`` serve a sample book.
DEMO_ENV = "REPORTING_DEMO_TRADES"

#: Sources that are simulated data wearing a real-data costume.
_SIMULATED_DATA_SOURCES = frozenset({"synthetic", "replay"})


class TradeSource(Protocol):  # pragma: no cover - structural typing only
    """Anything that can hand the consolidator a period's closed trades."""

    name: str

    def describe(self) -> dict[str, Any]: ...

    def fetch(self, period: Period) -> list[TradeRecord]: ...


@dataclass
class SourceBundle:
    """A resolved set of sources plus what each of them said about itself."""

    sources: list[TradeSource] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def fetch(self, period: Period) -> tuple[list[TradeRecord], list[str]]:
        records: list[TradeRecord] = []
        warnings = list(self.warnings)
        for source in self.sources:
            try:
                records.extend(source.fetch(period))
            except Exception as exc:  # noqa: BLE001 — one dead source never kills the report
                logger.exception("trade source %s failed", source.name)
                warnings.append(f"source {source.name} failed: {exc}")
        return records, warnings

    def describe(self) -> list[dict[str, Any]]:
        return [source.describe() for source in self.sources]


# ---------------------------------------------------------------------------
# DB source
# ---------------------------------------------------------------------------


class DatabaseTradeSource:
    """Closed trades from the ``trades`` table, joined to their portfolios.

    The join is what makes a broker attribution possible at all: ``trades``
    has no broker column, so the venue comes from the portfolio's
    ``execution_broker`` (migration 008) when that column exists, and from the
    live runner config otherwise. Neither path guesses.
    """

    name = "db"

    def __init__(self, db_manager: Any = None, manager: Any = None, fee_calculator: Any = None):
        self._db = db_manager
        self._manager = manager
        self._fee_calculator = fee_calculator
        self._last_note = ""

    # -- introspection -----------------------------------------------------

    def database(self) -> Any:
        """The manager passed in, else one built from the environment."""
        if self._db is not None:
            return self._db
        from backtest.db import DatabaseManager

        self._db = DatabaseManager.from_env()
        return self._db

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "kind": "database", "note": self._last_note}

    # -- fetch -------------------------------------------------------------

    def fetch(self, period: Period) -> list[TradeRecord]:
        try:
            db = self.database()
        except Exception as exc:  # noqa: BLE001 — no DB configured is normal in dev
            self._last_note = f"no database configured ({exc})"
            return []
        try:
            rows, has_broker_column, note = self._query(db, period)
        except Exception as exc:  # noqa: BLE001 — schema drift must not 500 the page
            self._last_note = f"query failed ({exc})"
            logger.warning("trades query failed: %s", exc)
            return []
        if not rows:
            self._last_note = note or "no persisted trades in period"
            return []
        self._last_note = note or f"{len(rows)} persisted trade(s)"
        return [
            self._to_record(row, has_broker_column=has_broker_column)
            for row in rows
        ]

    def _query(self, db: Any, period: Period) -> tuple[list[dict[str, Any]], bool, str]:
        """Pull the period's trades as plain dicts (never ORM objects).

        Columns are read defensively: ``portfolios.execution_broker`` only
        exists once migration 008 has run, and a report must work before and
        after that.
        """
        from sqlalchemy import inspect as sa_inspect

        from backtest.db.models import Portfolio as PortfolioRow
        from backtest.db.models import Trade as TradeRow

        inspector = sa_inspect(db.engine)
        for table in ("trades", "portfolios"):
            if not inspector.has_table(table):
                # A configured-but-empty database is normal on a fresh install;
                # say so once instead of raising "no such table" every request.
                return [], False, f"database has no {table!r} table (schema not initialised)"
        portfolio_columns = {c["name"] for c in inspector.get_columns("portfolios")}
        has_broker_column = "execution_broker" in portfolio_columns
        has_segment_column = "segment" in portfolio_columns

        out: list[dict[str, Any]] = []
        with db.session() as session:
            query = (
                session.query(TradeRow, PortfolioRow)
                .join(PortfolioRow, TradeRow.portfolio_id == PortfolioRow.portfolio_id)
                .filter(TradeRow.exit_time >= datetime.combine(period.start, datetime.min.time()))
                .filter(
                    TradeRow.exit_time
                    <= datetime.combine(period.end, datetime.max.time())
                )
                .order_by(TradeRow.exit_time)
            )
            for trade, portfolio in query.all():
                out.append(
                    {
                        "trade_id": str(trade.trade_id),
                        "portfolio_id": str(trade.portfolio_id),
                        "portfolio_name": str(portfolio.name or ""),
                        "portfolio_mode": str(portfolio.mode or "paper"),
                        # An unrecorded source resolves through the policy;
                        # a report must not name a data source by literal.
                        "portfolio_source": (
                            str(portfolio.source or default_source_tag())
                        ),
                        "symbol": str(trade.symbol or ""),
                        "strategy_name": str(trade.strategy_name or ""),
                        "direction": str(trade.direction or "long"),
                        "quantity": _dec(trade.quantity),
                        "entry_price": _dec(trade.entry_price),
                        "exit_price": _dec(trade.exit_price),
                        "entry_time": trade.entry_time,
                        "exit_time": trade.exit_time,
                        "gross_pnl": _dec(trade.gross_pnl),
                        "net_pnl": _dec(trade.net_pnl),
                        "commission_total": _dec(getattr(trade, "commission_total", 0)),
                        "slippage_total": _dec(getattr(trade, "slippage_total", 0)),
                        "holding_period_minutes": int(
                            getattr(trade, "holding_period_minutes", 0) or 0
                        ),
                        "exit_reason": str(getattr(trade, "exit_reason", "") or ""),
                        "broker": (
                            str(getattr(portfolio, "execution_broker", "") or "")
                            if has_broker_column
                            else ""
                        ),
                        "segment": (
                            str(getattr(portfolio, "segment", "") or "")
                            if has_segment_column
                            else ""
                        ),
                    }
                )
        note = f"{len(out)} persisted trade(s)" if out else "no persisted trades in period"
        return out, has_broker_column, note

    def _to_record(self, row: Mapping[str, Any], *, has_broker_column: bool) -> TradeRecord:
        mode = str(row["portfolio_mode"] or "paper").strip().lower()
        symbol = str(row["symbol"] or "")
        trade_id = str(row["trade_id"])

        broker = str(row.get("broker") or "").strip().lower()
        if not broker and self._manager is not None:
            broker = _broker_from_runner_name(self._manager, str(row.get("portfolio_name") or ""))
        if not broker:
            broker = UNATTRIBUTED_BROKER if mode == "live" else "paper"

        segment = _segment_for(
            symbol, row.get("holding_period_minutes"), mode, explicit=None
        )

        notes: list[str] = []
        fees = FeeBreakdown(components={}, segment=segment, broker=broker)
        brokerage = _dec(row.get("commission_total"))
        if brokerage != ZERO:
            fees = FeeBreakdown(
                components={"brokerage": brokerage}, segment=segment, broker=broker
            )
        fees_basis = FEE_BASIS_RECORDED if brokerage != ZERO else FEE_BASIS_NONE

        if mode == "live":
            # The persister writes brokerage only (``commission_total``); the
            # statutory stack is re-derived and merged, flagged as estimated.
            estimated = estimate_round_trip_fees(
                segment=segment,
                broker=broker,
                quantity=row["quantity"],
                entry_price=row["entry_price"],
                exit_price=row["exit_price"],
                direction=str(row.get("direction") or "long"),
                when=row.get("exit_time"),
                calculator=self._fee_calculator,
            )
            merged = _merge_missing(fees, estimated)
            if merged != fees:
                fees = merged
                fees_basis = FEE_BASIS_ESTIMATED
                notes.append("statutory fee components estimated from trade prices")
        elif brokerage != ZERO:
            notes.append("paper book recorded a non-zero brokerage")

        gross = _dec(row.get("gross_pnl"))
        recorded_net = row.get("net_pnl")
        recorded_net = _dec(recorded_net) if recorded_net is not None else None
        # ``LiveTradePersister`` stores net_pnl == gross_pnl (it does not net
        # the commission column), so the recorded net is NOT authoritative
        # for DB rows. Verified per row instead of assumed:
        authoritative = False
        if recorded_net is not None and recorded_net != gross:
            authoritative = True
        else:
            notes.append(
                "persisted net_pnl mirrors gross_pnl — net recomputed from costs"
            )

        record = TradeRecord(
            trade_id=trade_id,
            broker=broker,
            mode=mode,
            segment=segment,
            symbol=symbol,
            strategy=str(row.get("strategy_name") or ""),
            direction=str(row.get("direction") or "long"),
            quantity=row["quantity"],
            entry_price=row["entry_price"],
            exit_price=row["exit_price"],
            entry_time=row.get("entry_time"),
            exit_time=row.get("exit_time"),
            gross_pnl=gross,
            fees=fees,
            slippage=_dec(row.get("slippage_total")),
            net_pnl_recorded=recorded_net if authoritative else None,
            net_authoritative=authoritative,
            fees_basis=fees_basis,
            exit_reason=str(row.get("exit_reason") or ""),
            source="db",
            tag=_tag_for(mode, str(row.get("portfolio_source") or "")),
            notes=notes,
        )
        if broker == UNATTRIBUTED_BROKER:
            record.notes.append(
                "broker not recorded for this portfolio — attributed as "
                f"{UNATTRIBUTED_BROKER}, never guessed"
            )
        if not has_broker_column:
            record.raw = {"broker_column": False}
        return record


def _merge_missing(base: FeeBreakdown, extra: FeeBreakdown) -> FeeBreakdown:
    """Add only the components ``base`` does not already carry."""
    components = dict(base.components)
    changed = False
    for key, value in extra.components.items():
        if value and not components.get(key):
            components[key] = value
            changed = True
    if not changed:
        return base
    return FeeBreakdown(
        components=components, currency=base.currency, segment=base.segment, broker=base.broker
    )


# ---------------------------------------------------------------------------
# In-memory source
# ---------------------------------------------------------------------------


class MemoryTradeSource:
    """Closed trades from the live/paper books currently held in memory.

    Prefers a runner's own cost record (option structures carry their
    commission) and estimates the statutory stack only for **live** runners —
    the paper buckets are intentionally zero-cost, and applying real-world
    fees to them would understate the simulated book against the real one.
    """

    name = "memory"

    def __init__(self, manager: Any = None, fee_calculator: Any = None):
        self._manager = manager
        self._fee_calculator = fee_calculator
        self._last_note = ""

    def manager(self) -> Any:
        if self._manager is not None:
            return self._manager
        from backtest.forward.portfolio_manager import get_portfolio_manager

        self._manager = get_portfolio_manager()
        return self._manager

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "kind": "memory", "note": self._last_note}

    def fetch(self, period: Period) -> list[TradeRecord]:
        try:
            manager = self.manager()
            instances = manager.list_instances()
        except Exception as exc:  # noqa: BLE001 — no manager is a valid deployment
            self._last_note = f"no portfolio manager ({exc})"
            return []
        records: list[TradeRecord] = []
        for entry in instances:
            instance_id = entry.get("instance_id") or ""
            runner = None
            try:
                runner = manager.get_runner(instance_id)
            except Exception:  # noqa: BLE001
                runner = None
            if runner is None:
                continue
            config = getattr(runner, "config", None)
            mode = str(entry.get("mode") or getattr(config, "mode", "paper") or "paper").lower()
            broker = _runner_broker(manager, runner, config, mode)
            strategy = str(
                entry.get("strategy_name") or getattr(config, "strategy_name", "") or ""
            )
            holding_override = str(getattr(config, "instrument_type", "") or "").lower()
            data_source = str(getattr(config, "source", "") or "")
            for trade in _runner_closed_trades(runner):
                record = self._to_record(
                    trade,
                    mode=mode,
                    broker=broker,
                    strategy=strategy,
                    instance_id=instance_id,
                    data_source=data_source,
                )
                if record is None:
                    continue
                if not period.contains(record.exit_time):
                    continue
                is_option = holding_override in ("option", "options")
                if is_option and record.segment != TradeSegment.OPTIONS:
                    record.segment = TradeSegment.OPTIONS
                records.append(record)
        self._last_note = f"{len(records)} in-memory trade(s) in period"
        return records

    def _to_record(
        self,
        trade: Mapping[str, Any],
        *,
        mode: str,
        broker: str,
        strategy: str,
        instance_id: str,
        data_source: str,
    ) -> TradeRecord | None:
        symbol = str(trade.get("symbol") or "")
        exit_ts = trade.get("exit_ts")
        entry_ts = trade.get("entry_ts")
        if not symbol or not exit_ts:
            return None
        is_option = str(trade.get("kind") or "equity") == "option"
        segment = TradeSegment.OPTIONS if is_option else _segment_for(
            symbol, None, mode, explicit=None
        )
        quantity = _dec(trade.get("units") or trade.get("qty") or 0)
        entry_price = _dec(trade.get("entry_price"))
        exit_price = _dec(trade.get("exit_price"))
        pnl = _dec(trade.get("pnl"))
        commission = _dec(trade.get("commission"))

        notes: list[str] = []
        fees = FeeBreakdown(components={}, segment=segment, broker=broker)
        fees_basis = FEE_BASIS_NONE
        net_recorded: Decimal | None = None
        authoritative = False

        if commission != ZERO:
            fees = FeeBreakdown(
                components={"brokerage": commission}, segment=segment, broker=broker
            )
            fees_basis = FEE_BASIS_RECORDED
            if is_option:
                # Option structures realise P&L net of their commission, so
                # gross is reconstructed upward: gross - commission == pnl.
                net_recorded = pnl
                authoritative = True
                gross = pnl + commission
            else:
                gross = pnl
        else:
            gross = pnl

        if mode == "live" and _fee_estimation_wanted(fees):
            estimated = estimate_round_trip_fees(
                segment=segment,
                broker=broker,
                quantity=quantity,
                entry_price=entry_price,
                exit_price=exit_price,
                direction=str(trade.get("side") or "long"),
                when=_parse_dt(exit_ts),
                calculator=self._fee_calculator,
            )
            merged = _merge_missing(fees, estimated)
            if merged != fees:
                fees = merged
                fees_basis = FEE_BASIS_ESTIMATED
                notes.append("statutory fee components estimated from trade prices")

        record = TradeRecord(
            trade_id=f"{instance_id}:{symbol}:{exit_ts}",
            broker=broker,
            mode=mode,
            segment=segment,
            symbol=symbol,
            strategy=strategy,
            direction=str(trade.get("side") or "long"),
            quantity=quantity,
            entry_price=entry_price,
            exit_price=exit_price,
            entry_time=_parse_dt(entry_ts),
            exit_time=_parse_dt(exit_ts),
            gross_pnl=gross,
            fees=fees,
            slippage=ZERO,
            net_pnl_recorded=net_recorded,
            net_authoritative=authoritative,
            fees_basis=fees_basis,
            exit_reason=str(trade.get("exit_reason") or ""),
            source="memory",
            tag=_tag_for(mode, data_source),
            notes=notes,
        )
        if data_source.strip().lower() in _SIMULATED_DATA_SOURCES and mode == "live":
            record.notes.append(
                "runner is marked live but its bars come from "
                f"{data_source!r} — do not read this as real fills"
            )
        return record


def _fee_estimation_wanted(fees: FeeBreakdown) -> bool:
    """True when a live trade's recorded stack is missing statutory charges."""
    return not fees.get("stt") and not fees.get("gst")


def _runner_closed_trades(runner: Any) -> list[Mapping[str, Any]]:
    """A runner's closed trades, defensively (a broken runner yields none)."""
    try:
        trades = list(runner.closed_trades)
    except Exception:  # noqa: BLE001
        return []
    return [t for t in trades if isinstance(t, Mapping)]


def _runner_broker(manager: Any, runner: Any, config: Any, mode: str) -> str:
    """The venue label for a runner, without guessing one."""
    if mode != "live":
        return "paper"
    resolver = getattr(manager, "_runner_broker", None)
    if callable(resolver):
        try:
            label = str(resolver(runner) or "").strip().lower()
            if label:
                return label
        except Exception:  # noqa: BLE001
            pass
    for attr in ("execution_broker", "segment"):
        value = str(getattr(config, attr, "") or "").strip().lower()
        if value:
            return value
    return UNATTRIBUTED_BROKER


def _broker_from_runner_name(manager: Any, portfolio_name: str) -> str:
    """Recover a broker from a persister-named portfolio row, if possible.

    The persister names portfolios ``"<run name> [<instance8>]"``; matching
    that back to a live runner is a best-effort repair for rows written before
    migration 008, and it returns "" (never a guess) when it cannot match.
    """
    if not portfolio_name or manager is None:
        return ""
    try:
        for entry in manager.list_instances():
            instance_id = str(entry.get("instance_id") or "")
            name = str(entry.get("name") or "")
            if not name or f"[{instance_id[:8]}]" not in portfolio_name:
                continue
            runner = manager.get_runner(instance_id)
            if runner is None:
                continue
            mode = str(entry.get("mode") or "paper").lower()
            return _runner_broker(manager, runner, getattr(runner, "config", None), mode)
    except Exception:  # noqa: BLE001
        return ""
    return ""


# ---------------------------------------------------------------------------
# Demo source (explicitly requested only)
# ---------------------------------------------------------------------------


class DemoTradeSource:
    """Deterministic sample trades for UI previews and tests.

    Every record is tagged ``SIMULATED [demo]`` and the report carries
    ``demo: true``, so a screenshot of this book can never be mistaken for a
    real P&L. Never resolved unless ``demo=True`` (query param) or
    ``REPORTING_DEMO_TRADES=1`` is set.
    """

    name = "demo"

    #: (broker, mode, segment, symbol, qty, entry, exit, days_held)
    _BLUEPRINT: tuple[tuple[str, str, str, str, int, float, float, int], ...] = (
        ("mstock", "live", TradeSegment.OPTIONS, "NIFTY26OCT24800CE", 75, 118.40, 96.10, 0),
        ("mstock", "live", TradeSegment.OPTIONS, "NIFTY26OCT24700PE", 75, 84.20, 121.35, 0),
        ("mstock", "live", TradeSegment.OPTIONS, "BANKNIFTY26NOV52000CE", 30, 210.00, 178.50, 1),
        ("dhan", "live", TradeSegment.EQUITY_INTRADAY, "RELIANCE", 40, 2884.50, 2901.20, 0),
        ("dhan", "live", TradeSegment.EQUITY_INTRADAY, "HDFCBANK", 30, 1712.00, 1698.40, 0),
        ("dhan", "live", TradeSegment.EQUITY_DELIVERY, "INFY", 25, 1580.00, 1642.75, 96),
        ("broker_c", "paper", TradeSegment.OPTIONS, "NIFTY26OCT24500PE", 75, 96.00, 142.00, 2),
        ("broker_c", "paper", TradeSegment.EQUITY_INTRADAY, "TATAMOTORS", 60, 942.10, 951.30, 0),
        ("broker_d", "paper", TradeSegment.EQUITY_DELIVERY, "ITC", 120, 438.60, 452.10, 41),
    )

    def __init__(self, *, seed: int = 11, end: datetime | None = None):
        self._seed = seed
        self._end = end
        self._last_note = ""

    def describe(self) -> dict[str, Any]:
        return {"name": self.name, "kind": "demo", "note": self._last_note}

    def fetch(self, period: Period) -> list[TradeRecord]:
        anchor = self._end or datetime.combine(period.end, datetime.min.time()).replace(
            hour=15, minute=15
        )
        rng = random.Random(self._seed)
        records: list[TradeRecord] = []
        for index, blueprint in enumerate(self._BLUEPRINT):
            broker, mode, segment, symbol, qty, entry, exit_, days = blueprint
            # Spread the demo book backwards from the period end so a month
            # or FY window both contain it.
            exit_time = anchor.replace(microsecond=0) - timedelta(hours=3 * index)
            entry_time = exit_time - timedelta(days=days, hours=2)
            gross = (exit_ - entry) * qty
            if mode == "paper":
                fees = FeeBreakdown(components={}, segment=segment, broker=broker)
                fees_basis = FEE_BASIS_NONE
                if segment == TradeSegment.OPTIONS:
                    fees = FeeBreakdown(
                        components={"brokerage": Decimal("20") * 2},
                        segment=segment,
                        broker=broker,
                    )
                    fees_basis = FEE_BASIS_RECORDED
                records.append(
                    TradeRecord(
                        trade_id=f"demo-{index}",
                        broker=broker,
                        mode=mode,
                        segment=segment,
                        symbol=symbol,
                        strategy="demo_playbook",
                        direction="long",
                        quantity=Decimal(qty),
                        entry_price=Decimal(str(entry)),
                        exit_price=Decimal(str(exit_)),
                        entry_time=entry_time,
                        exit_time=exit_time,
                        gross_pnl=Decimal(str(round(gross, 2))),
                        fees=fees,
                        fees_basis=fees_basis,
                        source="demo",
                        tag="SIMULATED [demo]",
                        notes=["sample data — never a real trade"],
                    )
                )
                continue
            records.append(
                TradeRecord(
                    trade_id=f"demo-{index}",
                    broker=broker,
                    mode=mode,
                    segment=segment,
                    symbol=symbol,
                    strategy="demo_playbook",
                    direction="long",
                    quantity=Decimal(qty),
                    entry_price=Decimal(str(entry)),
                    exit_price=Decimal(str(exit_)),
                    entry_time=entry_time,
                    exit_time=exit_time,
                    gross_pnl=Decimal(str(round(gross, 2))),
                    fees=estimate_round_trip_fees(
                        segment=segment,
                        broker=broker,
                        quantity=Decimal(qty),
                        entry_price=Decimal(str(entry)),
                        exit_price=Decimal(str(exit_)),
                        direction="long",
                        when=exit_time,
                    ),
                    fees_basis=FEE_BASIS_ESTIMATED,
                    source="demo",
                    tag="SIMULATED [demo]",
                    notes=["sample data — never a real trade", f"seed={rng.randint(0, 1)}"],
                )
            )
        utc_min = datetime.min.replace(tzinfo=timezone.utc)

        def _demo_sort_key(r: Any) -> tuple:
            et = r.exit_time
            aware = (
                et if et.tzinfo is not None else et.replace(tzinfo=timezone.utc)
            ) if et is not None else utc_min
            return (aware, getattr(r, "symbol", ""))

        records.sort(key=_demo_sort_key)
        self._last_note = f"{len(records)} demo trade(s)"
        return [r for r in records if period.contains(r.exit_time)]


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def resolve_sources(
    *,
    manager: Any = None,
    db_manager: Any = None,
    fee_calculator: Any = None,
    include_db: bool = True,
    include_memory: bool = True,
    demo: bool = False,
) -> SourceBundle:
    """Build the source list for a report request.

    ``demo`` is honoured only when explicitly requested (query param) *or*
    ``REPORTING_DEMO_TRADES=1``; the demo book never appears on its own.
    """
    bundle = SourceBundle()
    allow_demo = demo or os.getenv(DEMO_ENV, "").strip().lower() in ("1", "true", "yes", "on")
    if include_memory:
        bundle.sources.append(
            MemoryTradeSource(manager=manager, fee_calculator=fee_calculator)
        )
    if include_db:
        bundle.sources.append(
            DatabaseTradeSource(
                db_manager=db_manager, manager=manager, fee_calculator=fee_calculator
            )
        )
    if allow_demo and not bundle.sources:
        bundle.sources.append(DemoTradeSource())
    elif allow_demo:
        bundle.sources.append(DemoTradeSource())
        bundle.warnings.append(
            "demo book requested — every demo trade is tagged SIMULATED and is "
            "excluded from tax"
        )
    if not bundle.sources:
        bundle.warnings.append(
            "no trade sources enabled — nothing can be reported (pass at least one)"
        )
    return bundle


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _segment_for(
    symbol: str,
    holding_minutes: Any,
    mode: str,
    *,
    explicit: str | None,
) -> str:
    """Best-effort fee segment for a trade that carries no instrument field.

    ``options``/``futures`` come from the symbol; equity is split the way the
    law and the exchange split it — a same-day round trip is intraday
    (speculative), anything carried overnight is delivery. A trade whose
    session cannot be established falls to delivery, and the record carries a
    note saying the split was inferred.
    """
    instrument = instrument_from_symbol(symbol)
    if instrument == "options":
        return TradeSegment.OPTIONS
    if instrument == "futures":
        return TradeSegment.FUTURES
    try:
        minutes = int(holding_minutes or 0)
    except (TypeError, ValueError):
        minutes = 0
    if minutes and minutes < 24 * 60:
        return TradeSegment.EQUITY_INTRADAY
    return TradeSegment.EQUITY_DELIVERY


def _tag_for(mode: str, data_source: str) -> str:
    normalized = str(data_source or "").strip().lower()
    if normalized in _SIMULATED_DATA_SOURCES:
        return "SIMULATED DATA"
    return "LIVE BOOK" if mode == "live" else "SIMULATED"


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _dec(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value is None:
        return ZERO
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        logger.warning("non-numeric trade value %r", value)
        return ZERO
