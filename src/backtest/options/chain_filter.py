"""Parity/junk screen in front of the chain-history writer (plan V3 §7.1).

A recorded chain is only a research asset if its premiums are believable. This
module is the gate between a raw capture batch and ``option_chain_history``:
a row is rejected when

* **(a) put-call parity** — the CE and PE at one strike/expiry imply a forward
  that disagrees with spot by more than ``parity_mult`` × the bid-ask spread
  (default 2×, the plan's band). Both legs of a violating pair are rejected: the
  screen cannot tell which side printed the impossible number.
* **(b) LTP off the book** — last trade sits outside ``[bid, ask]`` by more than
  ``ltp_tolerance`` (default 5%). A little drift is timing; a lot is a bad print.
* **(c) impossible IV** — implied vol is negative or above 200%.

Rejects are never silently dropped: every one is handed to a reject sink (the
default appends JSONL to ``data/chain_rejected.log``) with its reason and the
numbers that convicted it, so a session can be audited and the band re-derived
from evidence rather than hand-picked.

The screen is pure — no clock, no broker, no I/O — so the same function answers
the capture writer and the evidence tests. Absent inputs abstain rather than
reject: a venue that publishes no IV is not thereby junk.
"""

from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any, Callable, Iterable, Optional

from backtest.timekeeping import EXCHANGE_TZ, iso_utc, now_utc

logger = logging.getLogger("backtest.options.chain_filter")

__all__ = [
    "DEFAULT_REJECT_LOG",
    "PARITY_MULT",
    "LTP_TOLERANCE",
    "IV_MAX",
    "ChainRejectLog",
    "screen_chain_rows",
]

#: Plan §7.1 bands. The parity multiplier is evidence-tunable: the capture tape's
#: reject log is what tells the operator whether 2× is too tight or too loose.
PARITY_MULT = 2.0
LTP_TOLERANCE = 0.05
IV_MAX = 2.0  # 200% absolute vol
DEFAULT_REJECT_LOG = os.path.join("data", "chain_rejected.log")

#: Index options settle 15:30 IST on expiry day; parity discounts the strike to
#: that moment. No dividends on the index, so C − P = S − K·e^(−rT).
_SETTLE_TIME = time(15, 30)
_SECONDS_PER_YEAR = 365.0 * 86400.0
_DEFAULT_RISK_FREE = 0.065


def _num(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out or abs(out) == float("inf"):
        return None
    return out


def _years_to_expiry(bar_ts: Optional[datetime], expiry: Optional[date]) -> Optional[float]:
    if bar_ts is None or expiry is None:
        return None
    moment = bar_ts if bar_ts.tzinfo else bar_ts.replace(tzinfo=EXCHANGE_TZ)
    settle = datetime.combine(expiry, _SETTLE_TIME, tzinfo=EXCHANGE_TZ)
    return max((settle - moment.astimezone(EXCHANGE_TZ)).total_seconds() / _SECONDS_PER_YEAR, 0.0)


@dataclass(frozen=True)
class Rejection:
    row: dict
    reason: str
    detail: str


class ChainRejectLog:
    """Append-only JSONL sink for rejected chain rows (never silently dropped)."""

    def __init__(self, path: str = DEFAULT_REJECT_LOG) -> None:
        self.path = str(path)
        self.count = 0

    def write(self, rejection: Rejection) -> None:
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            line = {
                "recv_utc": iso_utc(now_utc()),
                "reason": rejection.reason,
                "detail": rejection.detail,
                "row": _jsonable(rejection.row),
            }
            with open(self.path, "a", encoding="utf-8") as handle:  # noqa: SIM115
                handle.write(json.dumps(line, separators=(",", ":"), default=str) + "\n")
            self.count += 1
        except OSError as exc:
            # A disk fault must not abort the capture; it is logged, not hidden.
            logger.warning("[chain-filter] reject-log write failed: %s", exc)


def _jsonable(row: dict) -> dict:
    out = {}
    for key, value in row.items():
        if isinstance(value, (datetime, date)):
            out[key] = value.isoformat()
        elif isinstance(value, (str, int, float, bool)) or value is None:
            out[key] = value
        else:
            out[key] = str(value)
    return out


def _ltp_off_book(row: dict) -> Optional[str]:
    ltp, bid, ask = _num(row.get("ltp")), _num(row.get("bid")), _num(row.get("ask"))
    if ltp is None or bid is None or ask is None or bid <= 0 or ask <= 0:
        return None
    if ltp < bid:
        dev = (bid - ltp) / bid
        if dev > LTP_TOLERANCE:
            return f"ltp {ltp:.2f} below bid {bid:.2f} by {dev * 100:.1f}%"
    elif ltp > ask:
        dev = (ltp - ask) / ask
        if dev > LTP_TOLERANCE:
            return f"ltp {ltp:.2f} above ask {ask:.2f} by {dev * 100:.1f}%"
    return None


def _iv_impossible(row: dict) -> Optional[str]:
    iv = _num(row.get("iv"))
    if iv is None:
        return None
    if iv < 0:
        return f"negative iv {iv:.4f}"
    if iv > IV_MAX:
        return f"iv {iv * 100:.0f}% above {IV_MAX * 100:.0f}%"
    return None


def screen_chain_rows(
    rows: Iterable[dict],
    *,
    risk_free: float = _DEFAULT_RISK_FREE,
    parity_mult: float = PARITY_MULT,
    reject_sink: Optional[Callable[[Rejection], None]] = None,
) -> tuple[list[dict], list[Rejection]]:
    """Split a capture batch into (accepted, rejected).

    ``rows`` are per-leg dicts carrying at least strike/expiry/option_type and
    whichever of bid/ask/ltp/iv/spot/bar_ts the venue published. Per-row checks
    (b, c) run first; then CE/PE pairs at one strike/expiry are parity-screened
    (a). A parity violation rejects both legs. ``reject_sink`` receives every
    rejection (the caller wires :class:`ChainRejectLog`); without one the
    rejections are still returned, never discarded.
    """
    rows = list(rows)
    rejected: list[Rejection] = []
    bad_ids: set[int] = set()

    def _reject(idx: int, reason: str, detail: str) -> None:
        if idx in bad_ids:
            return
        bad_ids.add(idx)
        rej = Rejection(row=rows[idx], reason=reason, detail=detail)
        rejected.append(rej)
        if reject_sink is not None:
            reject_sink(rej)

    # (b) LTP off the book, (c) impossible IV — per row.
    for idx, row in enumerate(rows):
        detail = _ltp_off_book(row)
        if detail:
            _reject(idx, "ltp_off_book", detail)
            continue
        detail = _iv_impossible(row)
        if detail:
            _reject(idx, "iv_impossible", detail)

    # (a) same-strike put-call parity on the surviving pairs.
    pairs: dict[tuple, dict[str, int]] = {}
    for idx, row in enumerate(rows):
        if idx in bad_ids:
            continue
        strike = _num(row.get("strike"))
        expiry = row.get("expiry")
        otype = str(row.get("option_type") or "").upper()
        if strike is None or expiry is None or otype not in ("CE", "PE"):
            continue
        pairs.setdefault((strike, expiry), {})[otype] = idx

    for (strike, expiry), book in pairs.items():
        if "CE" not in book or "PE" not in book:
            continue
        ce_idx, pe_idx = book["CE"], book["PE"]
        ce, pe = rows[ce_idx], rows[pe_idx]
        spot = _num(ce.get("spot")) or _num(pe.get("spot"))
        ce_bid, ce_ask = _num(ce.get("bid")), _num(ce.get("ask"))
        pe_bid, pe_ask = _num(pe.get("bid")), _num(pe.get("ask"))
        if spot is None or None in (ce_bid, ce_ask, pe_bid, pe_ask):
            continue  # cannot evaluate parity — abstain, do not reject
        if ce_bid <= 0 or ce_ask <= 0 or pe_bid <= 0 or pe_ask <= 0:
            continue
        ce_mid = (ce_bid + ce_ask) / 2.0
        pe_mid = (pe_bid + pe_ask) / 2.0
        years = _years_to_expiry(ce.get("bar_ts") or pe.get("bar_ts"), expiry)
        discount = math.exp(-risk_free * years) if years is not None else 1.0
        residual = abs((ce_mid - pe_mid) - (spot - strike * discount))
        spread = ((ce_ask - ce_bid) + (pe_ask - pe_bid)) / 2.0
        band = parity_mult * spread
        if residual > band:
            detail = (
                f"parity residual {residual:.2f} > {parity_mult:g}x spread {spread:.2f} "
                f"(band {band:.2f}); C_mid {ce_mid:.2f} P_mid {pe_mid:.2f} spot {spot:.2f}"
            )
            _reject(ce_idx, "put_call_parity", detail)
            _reject(pe_idx, "put_call_parity", detail)

    accepted = [row for idx, row in enumerate(rows) if idx not in bad_ids]
    return accepted, rejected
