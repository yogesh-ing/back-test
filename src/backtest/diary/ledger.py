"""Canonical trade-ledger truth: single commission, reconciliation, and dedupe.

Implements the Slice 0 foundation rules from
``docs/PNL-DIARY-AND-EDGE-SUITE-PLAN.md`` and ``docs/OPEN-ITEMS-TRACKER.md``:

* **E1 (IST session scoping):** every closed trade's ``session_date_ist`` is
  derived from its ``exit_time`` on the NSE exchange clock (``+05:30``), never
  from a UTC calendar date.
* **E2 (Single Commission sum):** the UI and APIs expose ONE combined
  ``commission_total`` figure (``brokerage + statutory_fees``). Sub-component
  splits are never surfaced on UI tables or tiles.
* **B2 (Visible reconciliation):** ``gross_pnl - commission_total - slippage``
  must equal ``net_pnl`` within ``RECONCILE_TOLERANCE_INR`` (₹0.05). Rows that
  violate this identity are NEVER smoothed over — their recorded numbers are
  preserved and ``reconcile_ok=False`` + ``reconcile_diff`` flag the row.
* **B1 (Cross-restart & historical duplicate collapse):** both the DB writer
  (:class:`~backtest.forward.trade_persistence.LiveTradePersister`) and ledger
  readers share :func:`canonical_trade_signature`, keyed on UTC epoch seconds
  and persisted unit quantities so ``+05:30`` vs ``+00:00`` strings and RAM
  ``lots`` vs DB ``units`` match identically.
"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Optional

from backtest.timekeeping import (
    from_stored_utc,
    iso_exchange,
    iso_utc,
    session_date,
    to_exchange,
)

#: Allowed rounding drift (in INR) before flagging ``Gross - Commission != Net``.
RECONCILE_TOLERANCE_INR = Decimal("0.05")

_INSTANCE_SUFFIX_RE = re.compile(r"\[([0-9a-fA-F]{6,36})\]\s*$")
_OPTION_HINT_RE = re.compile(
    r"(\b(?:long_call|long_put|short_call|short_put|bull_call|bear_put|"
    r"straddle|strangle|iron_condor|iron_butterfly|spread|call|put)\b|"
    r"\d{2}[A-Z0-9]{3}\d+(?:CE|PE)$)",
    re.IGNORECASE,
)


def _to_decimal(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    if value is None or value == "":
        return default
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return default


def extract_instance_prefix(
    portfolio_name: Optional[str],
    fallback_id: Optional[str] = None,
) -> tuple[str, Optional[str]]:
    """Split ``'Runner Name [a1b2c3d4]'`` into ``('Runner Name', 'a1b2c3d4')``.

    When a legacy ``portfolios`` row carries no bracketed prefix, returns the
    trimmed name and ``None`` (callers fall back to ``fallback_id`` for keying).
    """
    raw = str(portfolio_name or "").strip()
    match = _INSTANCE_SUFFIX_RE.search(raw)
    if match:
        prefix = match.group(1).lower()
        clean_name = raw[: match.start()].strip() or raw
        return clean_name, prefix
    return (raw or str(fallback_id or "unknown")), None


def single_commission_decimal(record: Mapping[str, Any] | Any) -> Decimal:
    """Return the single combined ``Commission`` figure (brokerage + taxes).

    Per E2 (30-Oct-2026 operator decision), no per-component breakdown is
    emitted. Accepts either a mapping (runner ``closed_trades`` dict / API row)
    or an ORM ``Trade`` row with ``commission_total``.
    """
    if isinstance(record, Mapping):
        if record.get("commission_total") is not None:
            return _to_decimal(record.get("commission_total"))
        return _to_decimal(record.get("commission")) + _to_decimal(
            record.get("statutory_fees")
        )
    if getattr(record, "commission_total", None) is not None:
        return _to_decimal(getattr(record, "commission_total"))
    return _to_decimal(getattr(record, "commission", 0)) + _to_decimal(
        getattr(record, "statutory_fees", 0)
    )


def reconcile_trade_amounts(
    *,
    gross_pnl: Any,
    commission_total: Any,
    net_pnl: Any = None,
    slippage_total: Any = 0,
    tolerance: Decimal = RECONCILE_TOLERANCE_INR,
) -> dict[str, Any]:
    """Validate ``gross_pnl - commission_total - slippage_total == net_pnl``.

    When ``net_pnl`` is ``None`` (legacy in-memory dicts that supplied only
    ``pnl`` + ``commission``), ``net`` is derived as ``gross - commission -
    slippage``. When ``net_pnl`` IS recorded, it is preserved verbatim and any
    discrepancy exceeding ``tolerance`` sets ``reconcile_ok=False``.
    """
    gross_d = _to_decimal(gross_pnl)
    comm_d = _to_decimal(commission_total)
    slip_d = _to_decimal(slippage_total)
    expected_net = gross_d - comm_d - slip_d
    net_d = expected_net if net_pnl is None else _to_decimal(net_pnl)
    diff_d = expected_net - net_d
    reconcile_ok = abs(diff_d) <= tolerance

    return {
        "gross_pnl": round(float(gross_d), 2),
        "commission_total": round(float(comm_d), 2),
        "slippage_total": round(float(slip_d), 2),
        "net_pnl": round(float(net_d), 2),
        "reconcile_ok": bool(reconcile_ok),
        "reconcile_diff": round(float(diff_d), 2),
        "is_winner": bool(net_d > 0),
    }


def _epoch_sec_or_raw(value: Any) -> int | str:
    if value is None or value == "":
        return ""
    try:
        dt = from_stored_utc(value)
    except Exception:  # noqa: BLE001 — malformed timestamp falls back to string
        return str(value).strip()
    if dt is None:
        return ""
    return int(dt.timestamp())


def canonical_trade_signature(
    portfolio_key: str,
    *,
    symbol: Any,
    entry_ts: Any,
    exit_ts: Any,
    units_or_qty: Any,
    gross_pnl: Any,
) -> tuple[str, str, int | str, int | str, str, str]:
    """Deterministic natural key matching across RAM dicts and DB ``trades``.

    ``LiveTradePersister`` stores ``units or qty or 1`` in ``trades.quantity``
    and substitutes ``exit_ts`` when ``entry_ts`` is missing, so both rules are
    mirrored here to guarantee byte-identical signatures before and after
    persistence or server restart.
    """
    pkey = str(portfolio_key or "").strip().lower()
    sym = str(symbol or "?")[:64].strip().upper()
    exit_key = _epoch_sec_or_raw(exit_ts)
    entry_key = _epoch_sec_or_raw(entry_ts) if entry_ts not in (None, "") else exit_key
    qty_d = abs(_to_decimal(units_or_qty, Decimal("1")))
    if qty_d == 0:
        qty_d = Decimal("1")
    gross_d = _to_decimal(gross_pnl)
    return (
        pkey,
        sym,
        entry_key,
        exit_key,
        f"{qty_d:.4f}",
        f"{gross_d:.2f}",
    )


def format_holding_duration(seconds: Optional[float]) -> str:
    """Compact human duration for dense ledger cells (``42s``, ``4m 15s``, ``1h 08m``)."""
    if seconds is None:
        return "—"
    total = max(0, int(round(seconds)))
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s" if secs else f"{minutes}m"
    hours, mins = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {mins:02d}m"
    days, rem_h = divmod(hours, 24)
    return f"{days}d {rem_h}h"


def _format_ist_stamp(
    dt_utc: Optional[datetime],
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    if dt_utc is None:
        return None, None, None
    ist_dt = to_exchange(dt_utc)
    if ist_dt is None:
        return None, None, None
    s_date = session_date(ist_dt)
    return (
        iso_exchange(ist_dt),
        f"{ist_dt:%d-%b %H:%M:%S} IST",
        s_date.isoformat() if s_date is not None else None,
    )


def _infer_kind(symbol: str, explicit_kind: Optional[str] = None) -> str:
    k = str(explicit_kind or "").strip().lower()
    if k in ("equity", "option"):
        return k
    if _OPTION_HINT_RE.search(symbol or ""):
        return "option"
    return "equity"


def normalize_memory_trade_dict(
    trade: Mapping[str, Any],
    *,
    instance_id: Optional[str] = None,
    runner_name: Optional[str] = None,
    strategy_name: Optional[str] = None,
    mode: Optional[str] = None,
    portfolio_id: Optional[str] = None,
) -> dict[str, Any]:
    """Normalize an in-memory ``runner.closed_trades`` record to ledger shape."""
    gross_raw = trade.get("gross_pnl")
    if gross_raw is None:
        gross_raw = trade.get("pnl", 0)
    comm_d = single_commission_decimal(trade)
    amounts = reconcile_trade_amounts(
        gross_pnl=gross_raw,
        commission_total=comm_d,
        net_pnl=trade.get("net_pnl"),
        slippage_total=trade.get("slippage_total", 0),
    )

    entry_raw = trade.get("entry_ts") or trade.get("entry_time")
    exit_raw = trade.get("exit_ts") or trade.get("exit_time")
    entry_utc = None
    exit_utc = None
    try:
        entry_utc = from_stored_utc(entry_raw)
    except Exception:  # noqa: BLE001
        entry_utc = None
    try:
        exit_utc = from_stored_utc(exit_raw)
    except Exception:  # noqa: BLE001
        exit_utc = None

    _, entry_label, entry_sdate = _format_ist_stamp(entry_utc)
    exit_ist_iso, exit_label, exit_sdate = _format_ist_stamp(exit_utc)

    hold_secs: Optional[float] = None
    if entry_utc is not None and exit_utc is not None:
        hold_secs = max(0.0, (exit_utc - entry_utc).total_seconds())

    iid = str(instance_id or trade.get("runner_id") or "")
    pkey = iid[:8].lower() if iid else str(portfolio_id or runner_name or "mem")
    symbol = str(trade.get("symbol") or "?")
    units_or_qty = trade.get("units") or trade.get("qty") or 1
    sig = canonical_trade_signature(
        pkey,
        symbol=symbol,
        entry_ts=entry_utc or entry_raw,
        exit_ts=exit_utc or exit_raw,
        units_or_qty=units_or_qty,
        gross_pnl=gross_raw,
    )

    raw_side = str(trade.get("side") or trade.get("direction") or "LONG").upper()
    norm_side = "SHORT" if raw_side.startswith("SHORT") or raw_side == "SELL" else "LONG"

    out = dict(trade)
    out.pop("statutory_fees", None)
    out.pop("brokerage", None)
    out.update(
        {
            "symbol": symbol,
            "kind": _infer_kind(symbol, trade.get("kind")),
            "side": norm_side,
            "runner_id": iid or None,
            "instance_prefix": iid[:8].lower() if iid else None,
            "portfolio_id": portfolio_id,
            "runner_name": runner_name or trade.get("runner_name") or "",
            "strategy": strategy_name or trade.get("strategy") or "",
            "mode": str(mode or trade.get("mode") or "paper").lower(),
            "entry_ts": iso_utc(entry_utc) if entry_utc is not None else entry_raw,
            "exit_ts": iso_utc(exit_utc) if exit_utc is not None else exit_raw,
            "exit_ist_iso": exit_ist_iso,
            "entry_ist_label": entry_label,
            "exit_ist_label": exit_label,
            "session_date": exit_sdate or trade.get("session_date"),
            "entry_session_date": entry_sdate or trade.get("entry_session_date"),
            "exit_session_date": exit_sdate or trade.get("exit_session_date"),
            "holding_seconds": round(hold_secs, 1) if hold_secs is not None else None,
            "holding_display": format_holding_duration(hold_secs),
            "exit_reason": trade.get("exit_reason") or trade.get("reason"),
            "pnl": amounts["gross_pnl"],
            "gross_pnl": amounts["gross_pnl"],
            "commission": amounts["commission_total"],
            "commission_total": amounts["commission_total"],
            "slippage_total": amounts["slippage_total"],
            "net_pnl": amounts["net_pnl"],
            "reconcile_ok": amounts["reconcile_ok"],
            "reconcile_diff": amounts["reconcile_diff"],
            "is_winner": amounts["is_winner"],
            "win": amounts["is_winner"],
            "persisted": bool(trade.get("persisted", False)),
            "_dedupe_sig": sig,
        }
    )
    return out


def normalize_db_trade_row(
    trade_row: Any,
    portfolio_row: Any,
    *,
    active_runner_id: Optional[str] = None,
) -> dict[str, Any]:
    """Normalize a persisted ``(Trade, Portfolio)`` join row to ledger shape."""
    p_name = getattr(portfolio_row, "name", "") if portfolio_row is not None else ""
    p_id = (
        getattr(portfolio_row, "portfolio_id", None)
        or getattr(trade_row, "portfolio_id", None)
    )
    p_mode = (
        str(getattr(portfolio_row, "mode", "paper") or "paper").lower()
        if portfolio_row is not None
        else "paper"
    )
    p_cap = (
        round(float(_to_decimal(getattr(portfolio_row, "initial_capital", 0))), 2)
        if portfolio_row is not None and getattr(portfolio_row, "initial_capital", None) is not None
        else None
    )
    clean_name, prefix = extract_instance_prefix(p_name, fallback_id=p_id)

    entry_utc = from_stored_utc(getattr(trade_row, "entry_time", None))
    exit_utc = from_stored_utc(getattr(trade_row, "exit_time", None))
    _, entry_label, entry_sdate = _format_ist_stamp(entry_utc)
    exit_ist_iso, exit_label, exit_sdate = _format_ist_stamp(exit_utc)

    hold_secs: Optional[float] = None
    if entry_utc is not None and exit_utc is not None:
        hold_secs = max(0.0, (exit_utc - entry_utc).total_seconds())
    elif getattr(trade_row, "holding_period_minutes", None) is not None:
        hold_secs = max(0.0, float(trade_row.holding_period_minutes) * 60.0)

    comm_d = single_commission_decimal(trade_row)
    amounts = reconcile_trade_amounts(
        gross_pnl=getattr(trade_row, "gross_pnl", 0),
        commission_total=comm_d,
        net_pnl=getattr(trade_row, "net_pnl", None),
        slippage_total=getattr(trade_row, "slippage_total", 0),
    )

    symbol = str(getattr(trade_row, "symbol", "") or "?")
    kind = _infer_kind(symbol)
    qty_f = abs(float(_to_decimal(getattr(trade_row, "quantity", 0))))
    direction = str(getattr(trade_row, "direction", "long") or "long").lower()
    pkey = prefix or str(p_id or "db").lower()
    sig = canonical_trade_signature(
        pkey,
        symbol=symbol,
        entry_ts=entry_utc,
        exit_ts=exit_utc,
        units_or_qty=qty_f,
        gross_pnl=getattr(trade_row, "gross_pnl", 0),
    )

    return {
        "trade_id": getattr(trade_row, "trade_id", None),
        "portfolio_id": p_id,
        "runner_id": active_runner_id or prefix,
        "instance_prefix": prefix,
        "runner_name": clean_name,
        "portfolio_name": p_name,
        "allocated_capital": p_cap,
        "strategy": str(getattr(trade_row, "strategy_name", "") or ""),
        "mode": p_mode,
        "symbol": symbol,
        "label": symbol if kind == "option" else None,
        "kind": kind,
        "side": "LONG" if direction == "long" else "SHORT",
        "qty": qty_f,
        "units": int(round(qty_f)) if kind == "option" else None,
        "entry_price": round(float(_to_decimal(getattr(trade_row, "entry_price", 0))), 2),
        "exit_price": round(float(_to_decimal(getattr(trade_row, "exit_price", 0))), 2),
        "entry_ts": iso_utc(entry_utc),
        "exit_ts": iso_utc(exit_utc),
        "exit_ist_iso": exit_ist_iso,
        "entry_ist_label": entry_label,
        "exit_ist_label": exit_label,
        "session_date": exit_sdate,
        "entry_session_date": entry_sdate,
        "exit_session_date": exit_sdate,
        "carried_overnight": bool(
            entry_sdate and exit_sdate and entry_sdate != exit_sdate
        ),
        "holding_seconds": round(hold_secs, 1) if hold_secs is not None else None,
        "holding_display": format_holding_duration(hold_secs),
        "pnl": amounts["gross_pnl"],
        "gross_pnl": amounts["gross_pnl"],
        "commission_total": amounts["commission_total"],
        "commission": amounts["commission_total"],
        "slippage_total": amounts["slippage_total"],
        "net_pnl": amounts["net_pnl"],
        "reconcile_ok": amounts["reconcile_ok"],
        "reconcile_diff": amounts["reconcile_diff"],
        "is_winner": amounts["is_winner"],
        "win": amounts["is_winner"],
        "exit_reason": getattr(trade_row, "exit_reason", None),
        "persisted": True,
        "_dedupe_sig": sig,
    }


def dedupe_normalized_trades(
    records: Iterable[dict[str, Any]],
    *,
    strip_private: bool = True,
) -> tuple[list[dict[str, Any]], int]:
    """Collapse duplicate records by ``_dedupe_sig`` (preferring ``persisted``).

    Returns ``(deduped_records, duplicates_collapsed_count)``.
    """
    by_sig: dict[tuple[Any, ...], dict[str, Any]] = {}
    order: list[tuple[Any, ...]] = []
    collapsed = 0

    for rec in records:
        sig = rec.get("_dedupe_sig")
        if sig is None:
            pkey = str(
                rec.get("instance_prefix")
                or (str(rec.get("runner_id") or "")[:8])
                or rec.get("portfolio_id")
                or "row"
            ).lower()
            sig = canonical_trade_signature(
                pkey,
                symbol=rec.get("symbol"),
                entry_ts=rec.get("entry_ts"),
                exit_ts=rec.get("exit_ts"),
                units_or_qty=rec.get("units") or rec.get("qty") or 1,
                gross_pnl=rec.get("gross_pnl", rec.get("pnl", 0)),
            )
        if sig not in by_sig:
            by_sig[sig] = dict(rec)
            order.append(sig)
        else:
            collapsed += 1
            existing = by_sig[sig]
            # Prefer a rich RAM record's option metadata (lots/strikes/expiry)
            # merged with the persisted DB row's trade_id + persisted=True flag.
            if rec.get("persisted") and not existing.get("persisted"):
                merged = dict(existing)
                merged.update(
                    {
                        "trade_id": rec.get("trade_id"),
                        "portfolio_id": rec.get("portfolio_id") or existing.get("portfolio_id"),
                        "persisted": True,
                    }
                )
                by_sig[sig] = merged
            elif not rec.get("persisted") and existing.get("persisted"):
                merged = dict(existing)
                for extra_key in ("label", "structure_type", "strikes", "expiry", "legs"):
                    if rec.get(extra_key) and not merged.get(extra_key):
                        merged[extra_key] = rec[extra_key]
                if rec.get("kind") == "option" and rec.get("units") and rec.get("qty"):
                    merged["qty"] = rec["qty"]
                    merged["units"] = rec["units"]
                by_sig[sig] = merged

    result: list[dict[str, Any]] = []
    for sig in order:
        item = by_sig[sig]
        if strip_private:
            item.pop("_dedupe_sig", None)
        result.append(item)
    return result, collapsed
