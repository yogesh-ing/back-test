"""Ladder-order hygiene shared by both WebSocket wrappers.

Each wrapper sanitizes the five-level book (a level survives only with a positive
price and positive quantity) and then prices a side from its *first* surviving
level, deliberately not sorting it — taking ``max``/``min`` would invent a better
price than the venue put at level 0. That choice leaves one hole, and the
2026-11-09 dual-broker capture shows it is real: packets whose levels arrive out
of order, so ``bids[0]`` is not the best bid and ``asks[0]`` not the best ask,
while the row still claims a two-sided book and gets labelled ``ok``. Ten such
packets came off mStock in one session (87,819 ladders read, 0.0114 %) and none
off Upstox (48,008 ladders). The worst flagged one, 09:34:48 UTC on token 64442,
printed bids ``[330.05, 330.00, 329.95, 1640.62, 1640.57]`` against a last trade
of ₹329.45 — the two deep levels are five times the price of the three shallow
ones, which no book does.

The rule is the one measured against those ten before being written (see
``tools/out/test_ladder_detector_20261109.py``, and
``tools/out/verify_ladder_rule_on_tape.py`` to re-run it over the tape with the
shipped code): bids may only step down with depth, asks only up. Equal adjacent
levels are allowed, a fold-back is not. The second rule tested alongside it —
level 0 must be the ladder's extreme — flagged a strict subset of the same ten,
because on a ladder that passes this check it is implied, so it is not re-checked
here.

What this rule cannot see, and must not be trusted to: a one-level-per-side book
with an absurd price is perfectly ordered. mStock sent exactly that at 09:34:17 on
token 64436 — bid ₹120,79,748.62 and ask ₹1,82,95,034.88 against a last trade of
₹167.65, labelled ``ok`` by the ladder, caught only downstream by the seam's
``ltp_distance`` gate. Magnitude belongs to that gate; shape belongs here.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

#: Reason labels land in ``ladder_status`` as ``unsorted`` rows and in the
#: capture file, so a session's tape can be counted per failure shape.
BID_NOT_MONOTONE = "bid_not_monotone"
ASK_NOT_MONOTONE = "ask_not_monotone"


def _prices(levels: Iterable[Any]) -> list[float]:
    """Prices of sanitized levels, in the order the venue sent them."""
    out: list[float] = []
    for level in levels or []:
        try:
            out.append(float(level["price"]))
        except (TypeError, ValueError, KeyError, IndexError):
            continue
    return out


def _folds_back(prices: list[float], *, deepest_must_be_lower: bool) -> bool:
    """True when a step moves back towards the touch instead of away from it."""
    for i in range(1, len(prices)):
        if deepest_must_be_lower:
            if prices[i] > prices[i - 1]:
                return True
        elif prices[i] < prices[i - 1]:
            return True
    return False


def order_problem(bids: Iterable[Any], asks: Iterable[Any]) -> Optional[str]:
    """``(bid|ask)_not_monotone`` for the first side that breaks, else ``None``.

    Takes the *already sanitized* level lists from either wrapper. Pure: no
    clock, no broker, no lock.
    """
    if _folds_back(_prices(asks), deepest_must_be_lower=False):
        return ASK_NOT_MONOTONE
    if _folds_back(_prices(bids), deepest_must_be_lower=True):
        return BID_NOT_MONOTONE
    return None


__all__ = ["ASK_NOT_MONOTONE", "BID_NOT_MONOTONE", "order_problem"]
