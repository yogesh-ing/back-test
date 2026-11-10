"""Shadow-mode second depth feed: Upstox V3 beside the live mStock ladder.

Built for item F of the 2026-11-08 Muhurat follow-up
(``docs/OPTION-JUNK-ASK-INCIDENT-2026-11-08.md``). The books trade on mStock's
WebSocket ladder, so a single bad offer — ₹493.65 on a put whose last trade was
₹367.00 — stops a structure and nothing in the process can say "another broker
disagrees". This module asks the second broker and writes down the answer.

WHAT IT IS
----------
One Upstox V3 ``full`` socket per underlying, subscribed to the same contracts
mStock's hot ring holds, plus a slow loop that compares the two views and hands
each comparison to the ``ENABLE_FEED`` recorder as a ``shadow_diff`` row. The
Upstox packets themselves land in ``feed_upstox_<day>.jsonl`` because
:mod:`backtest.data.upstox_ws_wrapper` records them on ingest.

WHAT IT IS NOT
--------------
No gate of its own making, and not a price source: it has exactly one reader in
the trading path — :meth:`LiveChainProvider.peer_l1`, which the quote seam asks
before it believes a side of the book (the ``cross_broker_l1`` gate, item F
promoted) — and that reader only ever gets the peer's own L1 numbers back.
Nothing here can write into the mStock view, subscribe a token, or spend an HTTP
quote request. The gate defaults to ``shadow`` and abstains whenever this module
has no answer, so a second broker being logged out costs corroboration, never a
trade. The architect's instruction still binds: thresholds from a session of
these files rather than another hand-calibrated number
(``tools/out/measure_cross_broker_band.py``). Inert unless ``ENABLE_FEED`` is on;
with no Upstox session it stays a parked loop that opens its socket on the pass
that first finds a token — a missing second broker costs one log line, never a
trading halt.

The one effect it has on the trading path is that it reads mStock through the
same TTL-cached quote accessor the book uses, so a sweep can warm a cache entry
the book would otherwise have fetched itself. It carries broker prices either
way; it never invents one, and it never writes a price the book reads.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import date
from typing import Any, Callable, Optional

from backtest.data.feed_depth_recorder import _scalar, record_shadow_diff

logger = logging.getLogger("backtest.data.cross_broker_shadow")

#: Seconds between reconciliations of the subscription set and one diff sweep.
DEFAULT_INTERVAL_S = 10.0
#: Ceiling on contracts mirrored per underlying. mStock's ring is capped at 64
#: tokens and Upstox's ``full`` feeder allows 2000 keys, so this only bounds the
#: damage a misconfigured ring could do to the shadow socket.
MAX_MIRRORED_KEYS = 120
#: Settle before the first diff sweep: the socket needs to authorize, connect and
#: receive a frame before a comparison means anything.
_FIRST_SWEEP_DELAY_S = 8.0
#: How often to re-ask Upstox's instrument master while the terms index is empty.
_TERMS_RETRY_S = 300.0

_ACTIVE: dict[str, "CrossBrokerShadow"] = {}
_ACTIVE_LOCK = threading.Lock()


def _chain_of(broker: Any, underlying: str) -> list[Any]:
    """Upstox's option contracts for ``underlying`` (instrument master, no session)."""
    getter = getattr(broker, "get_option_chain", None)
    return list(getter(underlying)) if callable(getter) else []


def _terms_key(contract: Any) -> Optional[tuple[date, int, str]]:
    """``(expiry, strike, CE|PE)`` — the only identity two brokers agree on."""
    expiry = getattr(contract, "expiry", None)
    strike = getattr(contract, "strike", None)
    option_type = getattr(contract, "option_type", None)
    type_word = str(getattr(option_type, "value", option_type) or "").strip().upper()
    if expiry is None or strike is None or type_word not in ("CE", "PE"):
        return None
    try:
        return (expiry, int(float(strike)), type_word)
    except (TypeError, ValueError):
        return None


def _l1(row: Any) -> dict[str, Any]:
    """The four numbers a fill can be made of, plus how old the row is."""
    if not isinstance(row, dict):
        return {}
    mono = row.get("recv_mono")
    age_s = round(time.monotonic() - float(mono), 2) if isinstance(mono, (int, float)) else None
    return {
        "ltp": row.get("ltp"),
        "bid": row.get("bid"),
        "ask": row.get("ask"),
        "ladder_status": row.get("ladder_status"),
        "age_s": age_s,
        "exchange_ts": row.get("timestamp"),
    }


def _delta(primary: dict[str, Any], secondary: dict[str, Any]) -> dict[str, Any]:
    """mStock minus Upstox for the side a fill would cross. Absent price → None."""
    out: dict[str, Any] = {}
    for field in ("ltp", "bid", "ask"):
        try:
            first = float(primary.get(field) or 0.0)
            second = float(secondary.get(field) or 0.0)
        except (TypeError, ValueError):
            out[field] = None
            continue
        out[field] = round(first - second, 4) if first and second else None
    return out


class CrossBrokerShadow:
    """Mirror mStock's live option set on Upstox and record the disagreement."""

    def __init__(
        self,
        underlying: str,
        chain_provider: Any,
        *,
        token_provider: Callable[[], Optional[str]],
        upstox_chain: Callable[[str], list[Any]],
        interval_s: float = DEFAULT_INTERVAL_S,
        wrapper_factory: Optional[Callable[[Callable[[], Optional[str]]], Any]] = None,
    ) -> None:
        self.underlying = str(underlying).upper()
        self._provider = chain_provider
        self._token_provider = token_provider
        self._upstox_chain = upstox_chain
        self._interval_s = float(interval_s)
        self._wrapper_factory = wrapper_factory or self._default_wrapper
        self._wrapper: Any = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        #: mStock token → Upstox instrument key, for the contracts mapped so far.
        self._pairs: dict[str, str] = {}
        self._unmapped: set[str] = set()
        self._terms: dict[tuple[date, int, str], str] = {}
        self._terms_next_try_mono = 0.0
        self.switches = 0
        self.last_error: Optional[str] = None

    # -- lifecycle -----------------------------------------------------------

    @staticmethod
    def _default_wrapper(token_provider: Callable[[], Optional[str]]) -> Any:
        from backtest.data.upstox_ws_wrapper import MODE_FULL, UpstoxWebSocketWrapper

        # A provider, not a snapshot: the socket re-authorizes on reconnect and a
        # mid-session Upstox re-login must not strand it on a dead token.
        return UpstoxWebSocketWrapper(token_provider=token_provider, mode=MODE_FULL)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name=f"shadow-upstox-{self.underlying}", daemon=True
        )
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        wrapper = self._wrapper
        if wrapper is not None:
            try:
                wrapper.close()
            except Exception:  # noqa: BLE001 — shutdown best effort
                logger.debug("shadow upstox close() raised", exc_info=True)
        self._wrapper = None

    def _run(self) -> None:
        time.sleep(_FIRST_SWEEP_DELAY_S)
        while not self._stop.is_set():
            try:
                self.reconcile()
                self.sweep()
            except Exception as exc:  # noqa: BLE001 — a witness never outlives itself
                self.last_error = f"{type(exc).__name__}: {exc}"
                logger.debug("shadow upstox pass failed", exc_info=True)
            self._stop.wait(self._interval_s)

    # -- subscription set ----------------------------------------------------

    def _index_upstox_terms(self) -> None:
        """(expiry, strike, type) → Upstox instrument key, at most once per ``_TERMS_RETRY_S``."""
        now = time.monotonic()
        if self._terms or now < self._terms_next_try_mono:
            return
        # Rate-limited rather than once-ever: before the Upstox broker is wired the
        # chain answers empty, and a witness that decided "single-broker forever" on
        # its first pass would never notice the login the operator does later.
        self._terms_next_try_mono = now + _TERMS_RETRY_S
        for contract in self._upstox_chain(self.underlying) or []:
            key = _terms_key(contract)
            instrument_key = str(getattr(contract, "instrument_token", "") or "")
            if key is not None and instrument_key:
                self._terms.setdefault(key, instrument_key)
        if not self._terms:
            self.last_error = "upstox master offered no contracts for this underlying"
            logger.warning(
                "SHADOW_UPSTOX_INERT underlying=%s — %s", self.underlying, self.last_error
            )

    def live_tokens(self) -> list[str]:
        """The option tokens mStock currently holds a ladder for."""
        ring = getattr(self._provider, "hot_ring", None)
        getter = getattr(ring, "subscribed_tokens", None)
        if not callable(getter):
            return []
        return [str(token) for token in getter(self.underlying)]

    def reconcile(self) -> list[str]:
        """Map the live mStock set onto Upstox keys and subscribe the new ones."""
        if self._token_provider() is None:
            # No Upstox session: return before indexing, because the chain comes
            # from Upstox's instrument master and downloading a few MB for a
            # witness nobody logged into is not "observe only".
            return []
        self._index_upstox_terms()
        wanted: dict[str, str] = {}
        for token in self.live_tokens()[:MAX_MIRRORED_KEYS]:
            contract = self._provider.contract_for(token)
            key = _terms_key(contract)
            instrument_key = self._terms.get(key) if key is not None else None
            if instrument_key is None:
                if token not in self._unmapped:
                    self._unmapped.add(token)
                    logger.info(
                        "SHADOW_UNMAPPED underlying=%s token=%s terms=%s — the two "
                        "brokers share no such contract, so it stays single-broker",
                        self.underlying,
                        token,
                        "unknown" if key is None else f"{key[0]} {key[1]} {key[2]}",
                    )
                continue
            wanted[token] = instrument_key
        self._pairs = wanted
        keys = sorted(set(wanted.values()))
        if not keys:
            return []
        if self._wrapper is None:
            self._wrapper = self._wrapper_factory(self._token_provider)
            self._wrapper.start(keys)
            self.switches += 1
            logger.info(
                "SHADOW_UPSTOX_ON underlying=%s keys=%d — mirroring mStock's ring on "
                "Upstox V3 full depth, observe only",
                self.underlying,
                len(keys),
            )
            return keys
        held = set(self._wrapper.subscribed_keys())
        if keys != sorted(held):
            added = [k for k in keys if k not in held]
            dropped = [k for k in held if k not in set(keys)]
            if added:
                self._wrapper.subscribe(added)
            if dropped:
                self._wrapper.unsubscribe(dropped)
            if added or dropped:
                self.switches += 1
        return keys

    # -- the witness ---------------------------------------------------------

    def l1_for(self, token: str) -> Optional[dict[str, Any]]:
        """The second broker's L1 for one mStock ring token, or None.

        This is the same read :meth:`sweep` writes to the tape, exposed for the
        fill seam (:meth:`BidAskQuoteProvider._corroboration_problem`) instead of
        the recorder: a witness that is only consulted once per 10 s cannot answer
        a question asked at fill time. Returns ``None`` — abstention, never an
        opinion — when there is no socket yet, the token was never mapped onto
        Upstox terms, or the row is unreadable. It writes nothing and cannot make
        a price; the caller has to decide what a disagreement means.
        """
        wrapper = self._wrapper
        instrument_key = self._pairs.get(str(token))
        if wrapper is None or instrument_key is None:
            return None
        try:
            row = wrapper.get_quote(instrument_key)
        except Exception:  # noqa: BLE001 — a missing witness must not reach the trade
            logger.debug("shadow peer read failed for token=%s", token, exc_info=True)
            return None
        out = _l1(row)
        if not out:
            return None
        out["broker"] = "upstox"
        return out

    def sweep(self) -> int:
        """Write one ``shadow_diff`` row per mirrored contract. Returns rows written."""
        wrapper = self._wrapper
        if wrapper is None or not self._pairs:
            return 0
        written = 0
        for token, instrument_key in list(self._pairs.items()):
            try:
                contract = self._provider.contract_for(token)
                primary = _l1(self._provider.get_quote(token))
                secondary = _l1(wrapper.get_quote(instrument_key))
            except Exception as exc:  # noqa: BLE001 — one dead contract must not end the sweep
                self.last_error = f"{type(exc).__name__}: {exc}"
                continue
            if not primary and not secondary:
                continue
            record_shadow_diff(
                {
                    "underlying": self.underlying,
                    "token": token,
                    "symbol": getattr(contract, "trading_symbol", None),
                    "upstox_key": instrument_key,
                    "expiry": str(getattr(contract, "expiry", "") or ""),
                    "strike": _scalar(getattr(contract, "strike", None)),
                    "option_type": str(
                        getattr(getattr(contract, "option_type", None), "value", None)
                        or getattr(contract, "option_type", "")
                        or ""
                    ),
                    "mstock": primary,
                    "upstox": secondary,
                    "delta": _delta(primary, secondary),
                    "upstox_connected": bool(wrapper.is_connected()),
                }
            )
            written += 1
        return written


# -- module facade ---------------------------------------------------------


def shadow_enabled(env: Optional[dict[str, str]] = None) -> bool:
    """On with the capture flag; ``SHADOW_UPSTOX_DEPTH=0`` turns the mirror off."""
    source = os.environ if env is None else env
    if str(source.get("SHADOW_UPSTOX_DEPTH", "")).strip().lower() in {"0", "false", "off", "no"}:
        return False
    from backtest.data.feed_depth_recorder import flag_enabled

    return flag_enabled(source)


def maybe_start(
    chain_provider: Any, underlying: str, *, source: str = ""
) -> Optional[CrossBrokerShadow]:
    """Start the mirror for ``underlying`` once; None when capture is off.

    Called from the feed registry when a live option chain provider is built, with
    the canonical source that provider came from. An Upstox session is *not*
    required here: the operator logs in whenever the broker session dies, usually
    after the books have acquired their feed, so the mirror starts as a parked
    loop and opens its socket on the pass that first finds a token. No token, no
    socket, no Upstox HTTP.
    """
    key = str(underlying).upper()
    if not shadow_enabled():
        return None
    if "upstox" in str(source).strip().lower():
        # The books already trade on Upstox: a second Upstox socket would agree
        # with itself, and a witness that always reports zero disagreement is
        # worse than none — it retires the doubt instead of answering it.
        logger.info(
            "SHADOW_UPSTOX_SELF underlying=%s — the live feed is already Upstox, so "
            "there is no second broker to mirror onto",
            key,
        )
        return None
    with _ACTIVE_LOCK:
        if key in _ACTIVE:
            return _ACTIVE[key]
    try:
        from backtest.brokers.session_manager import get_session_manager

        manager = get_session_manager()

        def _upstox() -> Any:
            try:
                return manager.get_broker("upstox")
            except Exception:  # noqa: BLE001 — no Upstox wired is a normal state
                return None

        def token_provider() -> Optional[str]:
            broker = _upstox()
            getter = getattr(broker, "get_session_token", None)
            if not callable(getter):
                return None
            token = getter()
            return str(token) if token else None

        if token_provider() is None:
            logger.info(
                "SHADOW_UPSTOX_WAITING underlying=%s — no Upstox session yet, so the "
                "second depth view opens itself once one is logged in",
                key,
            )
        shadow = CrossBrokerShadow(
            key,
            chain_provider,
            token_provider=token_provider,
            upstox_chain=lambda underlying: _chain_of(_upstox(), underlying),
        )
        with _ACTIVE_LOCK:
            rival = _ACTIVE.setdefault(key, shadow)
            if rival is not shadow:
                return rival
        shadow.start()
        return shadow
    except Exception as exc:  # noqa: BLE001 — a witness must not break the feed
        logger.warning("shadow Upstox feed could not start for %s: %s", key, exc)
        return None


def active() -> dict[str, "CrossBrokerShadow"]:
    """The mirrors running in this process (health/read endpoint)."""
    with _ACTIVE_LOCK:
        return dict(_ACTIVE)


def reset() -> None:
    """Close and forget every mirror (tests; a flag change needs a restart anyway)."""
    with _ACTIVE_LOCK:
        shadows = list(_ACTIVE.values())
        _ACTIVE.clear()
    for shadow in shadows:
        shadow.close()


__all__ = ["CrossBrokerShadow", "active", "maybe_start", "reset", "shadow_enabled"]
