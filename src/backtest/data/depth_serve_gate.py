"""One serve gate for every venue's WebSocket depth cache.

Why this module exists
----------------------
A fill must never price off a row the venue cannot vouch for. mStock's trading
path has never read its wrapper's cache directly: ``MStockBroker.get_ws_quote``
interposed a gate — two independent clocks, subscription truth, socket
liveness, ladder TTL, fail-closed — and the 2026-11-03 "fabricated ₹71,639"
incident is what a raw cache accessor produces instead (see
``docs/OPTION-DEPTH-GARBAGE-ROOT-CAUSE-2026-11-03.md``). Promoting Upstox depth
to a primary source (``docs/plans/upstox-primary-depth-plan.md`` Phase 0) needs
the *same* rule, so it is extracted here once and both brokers call it — a
second copy is a second place for the two venues to disagree.

The two clocks, because a packet proves two different things
-----------------------------------------------------------
**Price freshness** (``max_age_seconds``, per token, with a socket-wide
fallback): distinguishes two regimes using ``ltp_mono``/``recv_mono`` +
``last_any_tick_mono()``:

1. **Live socket + quiet strike**: the socket received *any* frame within its
   liveness window *and* this token is in the subscription registry — a
   subscribed leg whose book has not printed in >ceiling stays valid
   (``liveness_rescue``; preserving a live two-sided Bid/Ask instead of falsely
   degrading). **This rescue is load-bearing, not a convenience**: mStock's own
   per-leg p99 frame gap is 16.2 s — *worse* than its 10 s ceiling
   (``tools/out/cadence_20261111.txt``) — and fills survive quiet legs only
   because of it. Removing it changes every quiet-leg serve.
2. **Disconnected/zombie socket**: socket closed, or neither the token nor the
   socket has seen frames inside their windows, or the row carries neither
   ``recv_mono`` nor a parseable ``timestamp`` — rejected immediately
   (``{}``), eliminating post-close frozen marks.

**Ladder trust** (``ladder_ttl_seconds``): an old price is still a price; an
old *book* is not depth. A row whose ladder stopped printing past its TTL —
including one a wrapper retained through blank frames, which carries a
``kept:`` label and its original stamp — is served with ``bid``/``ask`` zeroed
and ``ladder_status=expired:*``. Callers keep the LTP for marking and lose the
ability to claim a cross-the-spread fill. Rows that predate ladder labelling
(legacy/test dicts) are untouched.

Per-venue ceilings live only in :class:`ServeGatePolicy`. They are **not**
shared constants: the two venues' print cadences differ by an order of
magnitude (16.2 s vs 1.87 s per-leg p99, same-day measurement), and a ceiling
copied venue-to-venue is a guess (impl plan Phase 0 / design plan §1b).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from backtest.timekeeping import EXCHANGE_TZ, duration_seconds, now_utc

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ServeGatePolicy:
    """The ceilings and rules for one venue's serve gate.

    Frozen on purpose: a policy is a measurement (each constant cites the tape
    it came from), not a knob. Tests may construct their own; production code
    uses the two module constants below.
    """

    #: "mstock" | "upstox" — log identity only.
    venue: str
    #: Price-freshness ceiling per token (seconds). PROVISIONAL values pending
    #: the Phase-0 measurement session; both currently 10.0 from the tape of
    #: 2026-11-11 (``tools/out/cadence_20261111.txt``): mStock keeps its
    #: historical 10.0 (safe only with ``liveness_rescue=True`` — its per-leg
    #: p99 is 16.2 s), Upstox's is ~5x its measured 1.87 s p99.
    max_age_seconds: float
    #: Depth trust window (seconds) after the ladder last printed.
    ladder_ttl_seconds: float
    #: Socket-wide liveness window; None ⇒ same as ``max_age_seconds``.
    socket_liveness_seconds: float | None = None
    #: Whether a stale row may be rescued by "socket alive AND token
    #: subscribed". mStock: True — its cadence exceeds its own ceiling and this
    #: is what keeps quiet-leg fills alive. Upstox: False as the fail-closed
    #: provisional, decided for real by the deliberate-quiet-token measurement
    #: (impl plan T0.5-2).
    liveness_rescue: bool = True


#: mStock's gate, values unchanged from ``MStockBroker.get_ws_quote`` as
#: shipped. ``liveness_rescue=True`` is load-bearing (see module docstring).
MSTOCK_SERVE_POLICY = ServeGatePolicy(
    venue="mstock",
    max_age_seconds=10.0,
    ladder_ttl_seconds=120.0,  # == MStockBroker.WS_LADDER_TTL_SECONDS (pinned by test)
    liveness_rescue=True,
)

#: Upstox's gate. **PROVISIONAL** — every constant is re-derived at the
#: Phase-0 measurement (impl plan T0.5): ``max_age_seconds`` from a full-day
#: ``measure_feed_cadence.py`` re-run, ``liveness_rescue`` from the
#: deliberate-quiet-token decision tree. Fail-closed until measured: a stale
#: Upstox row is refused, never rescued on unexamined reasoning.
UPSTOX_SERVE_POLICY = ServeGatePolicy(
    venue="upstox",
    max_age_seconds=10.0,      # PROVISIONAL (~5x measured per-leg p99 1.87s)
    ladder_ttl_seconds=120.0,  # measured longest leg silence 41.5s fits inside
    liveness_rescue=False,     # PROVISIONAL — T0.5-2 decides
)


def stamp_age_seconds(ts: Any) -> float | None:
    """Age in seconds of a wall-clock feed stamp, or None if unreadable.

    Feed timestamps go through ``duration_seconds``/``now_utc`` (aware UTC) so
    the age never depends on the host's local clock. Naive strings are read as
    exchange time by pairing with today's session date; time-only stamps carry
    no date and are paired the same way.

    Moved verbatim from ``MStockBroker._parse_legacy_ws_timestamp_age`` — the
    formats below are the ones venues actually send (mStock's naive
    ``2026-11-11T09:19:14AM``-style strings and time-only clock stamps).
    """
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return duration_seconds(ts, now_utc())
    if isinstance(ts, str):
        text = ts.strip()
        if not text:
            return None
        try:
            return duration_seconds(text, now_utc())
        except ValueError:
            pass
        for fmt in ("%I:%M:%S%p", "%I:%M:%S %p", "%H:%M:%S"):
            try:
                parsed_t = datetime.strptime(text.upper(), fmt).time()
                stamp = datetime.combine(
                    datetime.now(EXCHANGE_TZ).date(), parsed_t, tzinfo=EXCHANGE_TZ
                )
                return max(0.0, (now_utc() - stamp).total_seconds())
            except ValueError:
                continue
    return None


def expire_ladder(row: dict[str, Any], key: Any, now_mono: float, ttl_seconds: float) -> None:
    """Downgrade an untrustworthy ladder in place; keep the price.

    Never raises — the price side of the row is still usable for marks.
    Moved verbatim from ``MStockBroker._expire_ladder`` (TTL already resolved
    by the caller from its policy).
    """
    status = row.get("ladder_status")
    if status is None:
        return
    text = str(status)
    if not row.get("bid") and not row.get("ask"):
        # Nothing to expire: no side of this row claims to be depth.
        return
    ttl = float(ttl_seconds)
    stamp = row.get("ladder_mono")
    age = None if stamp is None else max(0.0, now_mono - float(stamp))
    if age is not None and age <= ttl:
        return
    logger.warning(
        "ladder_expired token=%s status=%s age=%s ttl=%.0fs bid=%s ask=%s; "
        "serving price without depth",
        key,
        text,
        "unstamped" if age is None else f"{age:.1f}s",
        ttl,
        row.get("bid"),
        row.get("ask"),
    )
    row["bid"] = 0.0
    row["ask"] = 0.0
    row["depth"] = {}
    row["ladder_status"] = f"expired:{text}"


class _UsePolicy:
    """Sentinel: ``max_age_seconds`` omitted ⇒ the policy's ceiling.

    Distinct from an explicit ``None`` (⇒ aging disabled) and from a number
    (⇒ that ceiling), because ``MStockBroker.get_ws_quote`` historically
    spelled all three and its pinned suites depend on the distinction.
    """


USE_POLICY = _UsePolicy()


def serve_ws_row(
    *,
    row: dict[str, Any] | None,
    key: Any,
    now_mono: float,
    is_connected: bool,
    last_any_tick_mono: float,
    token_subscribed: bool,
    policy: ServeGatePolicy,
    max_age_seconds: float | None | _UsePolicy = USE_POLICY,
    socket_liveness_seconds: float | None = None,
    ladder_ttl_seconds: float | None = None,
) -> dict[str, Any]:
    """The gate: a validated copy of ``row``, or ``{}`` — fail-closed.

    Check order is ``MStockBroker.get_ws_quote``'s, preserved exactly:

    1. the cache has a row and the socket is connected;
    2. price freshness from ``ltp_mono`` (fallback ``recv_mono``): over the
       ceiling ⇒ refused, unless ``policy.liveness_rescue`` and the socket is
       alive (``last_any_tick_mono`` inside its window) and the token is in the
       subscription registry — the quiet-leg rescue;
    3. no monotonic stamps at all ⇒ the wall clock (``stamp_age_seconds``) must
       say the row is fresh: unreadable, future-skewed (< -5 s) or stale ⇒
       refused;
    4. the surviving row is copied, ``source`` defaults to ``"ws"``, and its
       ladder is expired in place past ``ladder_ttl_seconds``.

    Callers pass primitives gathered from their own wrapper/locks — this module
    imports no broker. Raises are the caller's problem: each broker wraps this
    in its historical ``except Exception → return {}`` net.
    """
    if not row:
        return {}
    if not is_connected:
        logger.debug(
            "WebSocket disconnected; rejecting cached quote for key %s", key
        )
        return {}

    ceiling = (
        policy.max_age_seconds if isinstance(max_age_seconds, _UsePolicy) else max_age_seconds
    )
    liveness_limit = (
        float(socket_liveness_seconds)
        if socket_liveness_seconds is not None
        else (
            float(policy.socket_liveness_seconds)
            if policy.socket_liveness_seconds is not None
            else float(ceiling) if ceiling is not None and ceiling > 0 else 0.0
        )
    )
    ttl = (
        float(ladder_ttl_seconds)
        if ladder_ttl_seconds is not None
        else float(policy.ladder_ttl_seconds)
    )

    if ceiling is not None and ceiling > 0:
        recv_mono = row.get("ltp_mono")
        if recv_mono is None:
            recv_mono = row.get("recv_mono")
        if recv_mono is not None:
            quote_age = max(0.0, now_mono - float(recv_mono))
            if quote_age > ceiling:
                socket_alive = last_any_tick_mono > 0.0 and (
                    now_mono - last_any_tick_mono
                ) <= liveness_limit
                if not (
                    policy.liveness_rescue and socket_alive and token_subscribed
                ):
                    logger.debug(
                        "Stale WS tick key=%s age=%.1fs alive=%s subscribed=%s; rejecting",
                        key,
                        quote_age,
                        socket_alive,
                        token_subscribed,
                    )
                    return {}
        else:
            # Legacy/injected row without recv_mono: fail closed if timestamp
            # is missing, unparseable, future-skewed, or stale.
            ts = row.get("timestamp")
            if ts is None:
                logger.debug(
                    "WS quote for key %s lacks recv_mono and timestamp; rejecting", key
                )
                return {}
            age = stamp_age_seconds(ts)
            if age is None or age < -5.0 or age > ceiling:
                logger.debug(
                    "Stale or unparseable WS timestamp for key %s (%r); rejecting",
                    key,
                    ts,
                )
                return {}

    out = dict(row)
    out.setdefault("source", "ws")
    expire_ladder(out, key, now_mono, ttl)
    return out


__all__ = [
    "MSTOCK_SERVE_POLICY",
    "ServeGatePolicy",
    "UPSTOX_SERVE_POLICY",
    "USE_POLICY",
    "expire_ladder",
    "serve_ws_row",
    "stamp_age_seconds",
]
