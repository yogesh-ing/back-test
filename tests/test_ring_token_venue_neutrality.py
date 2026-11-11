"""One subscription token format per venue, one code path for both (Phase 1, item 1).

WHY THIS EXISTS
``HotStrikeRing`` and the quote providers spoke ``int``: ``pin_tokens`` called
``int(str(raw))`` and skipped whatever refused, ``recenter`` tested
``raw_tok.isdigit()`` and skipped the rest, ``ring_book`` called ``int(token)``.
On a venue whose contracts are named ``NSE_FO|NIFTY|…`` that is not a type
preference, it is a silent amputation — the ring subscribes nothing, every leg
answers LTP-only, and the fill seam refuses without ever naming the cause. This
is the B2 family from the depth plan, and it is the same class of defect as the
one that froze option books on 2026-10-26: a token registry that quietly loses
the keys it cannot parse.

WHAT IT PINS
* the numeric venue is bit-for-bit what it was — ints in the ring, ints handed to
  the broker, ints coming back;
* a key-shaped venue's contracts *enter* the ring instead of being dropped;
* a broker's own spelling of its subscription set can never make the ring churn;
* a mixed ring (a migrated book) sorts, evicts and unsubscribes both spellings;
* a ring that selects nothing is a log line, not a silence — and the return dict
  keeps exactly the two keys the pinned suites compare it by equality.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
import logging
from typing import Any

import pytest

from backtest.instruments.base import OptionType
from backtest.instruments.option import OptionContract
from backtest.options.quote_providers import (
    HotStrikeRing,
    LiveChainProvider,
    normalize_token,
    token_sort_key,
)

EXPIRY = date.today() + timedelta(days=7)


class _VenueBroker:
    """A broker that speaks exactly the token format its contracts were built with.

    The numeric venue and the key-shaped venue differ only in what ``instrument_token``
    holds, so one fake covers both — which is the point of the change.
    """

    broker_name = "venue"

    def __init__(self) -> None:
        self.broker_keys: list[Any] = []
        self.mode_calls: list[tuple[str, list[Any]]] = []
        self.rows: dict[str, dict[str, Any]] = {}
        self._ws_feed = object()  # has_ws_ring_support() reads __dict__["_ws_feed"]
        self._ws_subscribed_tokens: set[Any] = set()

    def subscribe_tokens_ws(
        self, tokens: Any, mode: str = "full", *, wait_timeout: float = 0.0
    ) -> list[Any]:
        toks = list(tokens)
        self.mode_calls.append((mode, toks))
        self._ws_subscribed_tokens.update(toks)
        return toks

    def unsubscribe_tokens_ws(self, tokens: Any) -> list[Any]:
        toks = list(tokens)
        for tok in toks:
            self._ws_subscribed_tokens.discard(tok)
        return toks

    def ws_subscribed_tokens(self) -> set[Any]:
        return set(self._ws_subscribed_tokens)

    def pin_ws_tokens(self, tokens: Any, mode: str = "full") -> list[Any]:
        return self.subscribe_tokens_ws(tokens, mode=mode)

    def unpin_ws_tokens(self, tokens: Any) -> list[Any]:
        return list(tokens)

    def get_ws_quote(self, token: Any, max_age_seconds: float = 10.0, **_: Any) -> dict:
        self.broker_keys.append(token)
        return self.rows.get(str(token), {})

    def get_option_quote(self, key: Any) -> dict[str, Any]:
        self.broker_keys.append(key)
        return self.rows.get(str(key), {})


def _chain(
    underlying: str,
    center: int,
    step: int,
    sides: int,
    *,
    key_format: str = "numeric",
) -> list[OptionContract]:
    """A CE/PE chain whose tokens are spelled the way ``key_format`` wants."""

    def token_for(offset: int, suffix: str) -> str:
        if key_format == "blank":  # a master that offered no token at all
            return ""
        if key_format == "numeric":
            return str(50000 + offset * 2 + (1 if suffix == "PE" else 0))
        strike = center + offset * step
        return f"NSE_FO|{underlying}|25DEC99|{strike}|{suffix}"

    contracts: list[OptionContract] = []
    for offset in range(-sides, sides + 1):
        strike = center + offset * step
        for opt_type in (OptionType.CE, OptionType.PE):
            suffix = "CE" if opt_type is OptionType.CE else "PE"
            contracts.append(
                OptionContract(
                    instrument_token=token_for(offset, suffix),
                    trading_symbol=f"{underlying}26DEC{strike}{suffix}",
                    underlying=underlying,
                    expiry=EXPIRY,
                    strike=Decimal(strike),
                    option_type=opt_type,
                    lot_size=75,
                    metadata={},
                )
            )
    return contracts


# ---------------------------------------------------------------------------
# The ring itself
# ---------------------------------------------------------------------------


def test_numeric_venue_still_carries_ints_end_to_end() -> None:
    """The mStock spelling is untouched: ints in the ring, ints to the broker."""
    broker = _VenueBroker()
    ring = HotStrikeRing(broker, max_ring_tokens=64)
    res = ring.recenter("NIFTY", 25010.0, _chain("NIFTY", 25000, 50, 15))

    assert len(res["subscribed"]) == 26
    assert all(isinstance(tok, int) for tok in res["subscribed"])
    assert all(isinstance(tok, int) for tok in ring.subscribed_tokens("NIFTY"))
    assert all(isinstance(tok, int) for tok in broker._ws_subscribed_tokens)


def test_key_shaped_contracts_are_subscribed_not_dropped() -> None:
    """The defect: a key-shaped chain used to produce an empty, silent ring."""
    broker = _VenueBroker()
    ring = HotStrikeRing(broker, max_ring_tokens=64)
    chain = _chain("NIFTY", 25000, 50, 15, key_format="instrument_key")
    res = ring.recenter("NIFTY", 25010.0, chain)

    assert len(res["subscribed"]) == 26, "a key-shaped venue must get the same ring"
    assert set(res["subscribed"]) == {
        c.instrument_token
        for c in chain
        if abs(float(c.strike) - 25000.0) <= 6 * 50 + 1e-6
    }
    assert all(tok.startswith("NSE_FO|") for tok in broker._ws_subscribed_tokens)


def test_a_mixed_ring_sorts_evicts_and_unsubscribes_both_spellings() -> None:
    """A migrated book holds numeric legs while its fresh legs are keys."""
    broker = _VenueBroker()
    ring = HotStrikeRing(broker, max_ring_tokens=64)
    numeric = [
        c
        for c in _chain("NIFTY", 25000, 50, 8)
        if str(c.option_type.value).upper().startswith("C")
    ]
    keyed = [
        c
        for c in _chain("NIFTY", 25000, 50, 8, key_format="instrument_key")
        if str(c.option_type.value).upper().startswith("P")
    ]
    mixed = numeric + keyed

    first = ring.recenter("NIFTY", 25010.0, mixed)
    assert len(first["subscribed"]) == 26
    assert any(isinstance(t, int) for t in broker._ws_subscribed_tokens)
    assert any(isinstance(t, str) for t in broker._ws_subscribed_tokens)

    # Jump ATM by 10 steps: everything in the old band is outside hysteresis.
    moved = ring.recenter("NIFTY", 25510.0, mixed)
    assert moved["unsubscribed"], "both spellings must be evictable"
    assert any(isinstance(t, int) for t in moved["unsubscribed"])
    assert any(isinstance(t, str) for t in moved["unsubscribed"])


def test_a_brokers_own_spelling_cannot_make_the_ring_churn() -> None:
    """Membership is compared in the ring's spelling, not the broker's.

    A broker that answers ``{"50000"}`` for a ring holding ``50000`` would look
    like a socket that lost every token, and recenter would re-subscribe the
    whole ring on every bar.
    """
    broker = _VenueBroker()
    ring = HotStrikeRing(broker, max_ring_tokens=64)
    chain = _chain("NIFTY", 25000, 50, 15)
    assert len(ring.recenter("NIFTY", 25010.0, chain)["subscribed"]) == 26

    broker._ws_subscribed_tokens = {str(t) for t in broker._ws_subscribed_tokens}
    assert ring.recenter("NIFTY", 25012.0, chain) == {
        "subscribed": [],
        "unsubscribed": [],
    }, "a re-spelled socket snapshot is not a lost subscription"


def test_pin_and_unpin_keep_the_venues_own_type() -> None:
    broker = _VenueBroker()
    ring = HotStrikeRing(broker, max_ring_tokens=64)
    numeric_tok = 51234
    keyed_tok = "NSE_FO|NIFTY|25DEC99|25000|CE"

    pinned = ring.pin_tokens([numeric_tok, keyed_tok, "", None])

    assert pinned == [numeric_tok, keyed_tok], "ints stay ints; keys stay keys"
    assert ring._pinned_tokens[numeric_tok] == 1
    assert ring._pinned_tokens[keyed_tok] == 1
    assert ring.unpin_tokens([numeric_tok, keyed_tok]) == [numeric_tok, keyed_tok]
    assert not ring._pinned_tokens


# ---------------------------------------------------------------------------
# The empty-ring tripwire
# ---------------------------------------------------------------------------


def test_a_ring_that_selects_nothing_says_so(caplog: pytest.LogCaptureFixture) -> None:
    """Silent zero-subscription is the failure mode this whole slice exists to kill."""
    broker = _VenueBroker()
    ring = HotStrikeRing(broker, max_ring_tokens=64)
    blind_chain = _chain("NIFTY", 25000, 50, 15, key_format="blank")

    with caplog.at_level(logging.WARNING, logger="backtest.options.quotes"):
        res = ring.recenter("NIFTY", 25010.0, blind_chain)
        # A book with no tokens recenters every bar: the report must be once per
        # underlying, not once per bar, or the one line that matters drowns.
        ring.recenter("NIFTY", 25012.0, blind_chain)

    assert res == {"subscribed": [], "unsubscribed": []}, (
        "the return dict is compared by equality in pinned suites — it may not "
        "gain a diagnostic key"
    )
    lines = [r.getMessage() for r in caplog.records if "HOT_RING_EMPTY" in r.getMessage()]
    assert len(lines) == 1, "an empty ring is loud exactly once per underlying"
    assert "contracts=62" in lines[0] and "tokenless=62" in lines[0]

    # A ring that fills clears the report, so a later empty round is loud again.
    # The spot has to move a full step for recenter to look at all — that is the
    # hysteresis contract, and it is why an empty ring only re-reports on ATM moves.
    filled = ring.recenter("NIFTY", 25060.0, _chain("NIFTY", 25000, 50, 15))
    assert len(filled["subscribed"]) == 26
    ring.recenter("NIFTY", 25110.0, blind_chain)
    assert len([r for r in caplog.records if "HOT_RING_EMPTY" in r.getMessage()]) == 2


# ---------------------------------------------------------------------------
# The read side: parity book + quote routing
# ---------------------------------------------------------------------------


def _provider_with_ring(key_format: str) -> tuple[LiveChainProvider, _VenueBroker, list]:
    broker = _VenueBroker()
    provider = LiveChainProvider(broker, quote_ttl_seconds=5, chain_ttl_seconds=900)
    chain = _chain("NIFTY", 25000, 50, 15, key_format=key_format)
    provider.register_chain(chain)
    provider.hot_ring.recenter("NIFTY", 25010.0, chain)
    rows = []
    for contract in chain:
        row = {
            "ltp": 100.0,
            "bid": 99.0,
            "ask": 101.0,
            "depth": {"bid": [[99.0, 150]], "ask": [[101.0, 150]]},
        }
        broker.rows[str(contract.instrument_token)] = row
        rows.append(row)
    return provider, broker, chain


def test_ring_book_asks_a_key_shaped_peer_by_its_own_key() -> None:
    """``int(token)`` used to raise here and the ``except`` swallowed it."""
    provider, broker, chain = _provider_with_ring("instrument_key")
    broker.broker_keys.clear()

    book = provider.ring_book("NIFTY")

    assert len(book) == 26, "every peer leg must reach the parity reference"
    assert all(k.startswith("NSE_FO|") for k in broker.broker_keys)
    assert all(isinstance(k, str) for k in broker.broker_keys)


def test_ring_book_still_asks_a_numeric_peer_by_int() -> None:
    provider, broker, chain = _provider_with_ring("numeric")
    broker.broker_keys.clear()

    book = provider.ring_book("NIFTY")

    assert len(book) == 26
    assert all(isinstance(k, int) for k in broker.broker_keys)


def test_parity_siblings_offers_the_key_shaped_peers_of_a_leg() -> None:
    provider, _broker, chain = _provider_with_ring("instrument_key")
    leg = chain[0].instrument_token

    siblings = provider.parity_siblings(leg)

    assert siblings, "a key-shaped venue still has CE/PE peers at the same expiry"
    assert all(str(peer.instrument_token) != str(leg) for peer, _row in siblings)


def test_get_quote_hands_the_key_shaped_token_down_as_the_depth_key() -> None:
    """The contract knows its own token; only an unregistered key needs the digit test."""
    provider, broker, chain = _provider_with_ring("instrument_key")
    leg = chain[3]
    broker.broker_keys.clear()

    provider.get_quote(leg.instrument_token)

    assert broker.broker_keys[0] == leg.instrument_token, (
        "the WS depth key must be asked before the symbol, whatever the format"
    )


def test_price_contract_prices_a_key_shaped_leg_from_its_symbol_row() -> None:
    provider, broker, chain = _provider_with_ring("instrument_key")
    leg = chain[5]
    del broker.rows[str(leg.instrument_token)]  # force the symbol row to answer
    broker.rows[str(leg.trading_symbol)] = {"ltp": 137.5, "bid": 0.0, "ask": 0.0}
    broker.broker_keys.clear()

    assert provider.price_contract(leg) == 137.5
    assert broker.broker_keys[0] == leg.instrument_token


def test_sort_and_normalize_helpers_agree_with_the_venues() -> None:
    assert normalize_token("50001") == 50001
    assert normalize_token(50001) == 50001
    assert normalize_token("NSE_FO|NIFTY|25DEC99|25000|CE") == "NSE_FO|NIFTY|25DEC99|25000|CE"
    assert normalize_token(None) == ""
    # The whole point of token_sort_key: this list has no ordering by type, yet
    # sorted() must not raise and must keep numbers below keys.
    mixed = ["NSE_FO|A", 7, "NSE_FO|B", 300]
    assert sorted(mixed, key=token_sort_key) == [7, 300, "NSE_FO|A", "NSE_FO|B"]
