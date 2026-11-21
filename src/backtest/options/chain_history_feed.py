"""Recorded-chain pricing feed — real broker premiums, never model output.

This is the ``chain_history`` side of the pricing-feed abstraction (plan V3
§3, Phase 4). Where :class:`~backtest.options.model_pricing.ModelBSFeed`
synthesizes a premium from Black-Scholes, this feed replays premiums that were
actually captured into ``option_chain_history`` (migration 026) and survived
the §7.1 parity/junk screen. It declares ``data_source_real = True`` and stamps
``source_tag="chain_history"`` on every quote, which is what unlocks the
certify/deploy doors that ``model_bs`` is quarantined from.

Two honesty rules separate it from the model feed:

* **No fabrication.** A strike with no recorded row at the bar is refused, not
  filled at a synthesized price. The refusal is :class:`ChainQuoteUnavailable`
  (a :class:`NoExecutableQuoteError`), which the adapter turns into a zero/error
  quote and the broker's existing phantom-price guard rejects — no driver or
  broker change is needed to make a missing strike untradeable.
* **Liquidity is a HARD filter** (plan §3 correction 7). The model feed always
  conjures a two-sided book; this feed refuses a dead strike — no crossable
  book, no positive last trade, or (when the venue reports it) zero open
  interest *and* zero volume. Greeks/IV are whatever the venue recorded; absent
  ones read 0.0, never a BS guess.

The feed holds no pricing math and no DB session: it delegates the asof lookup
to an injected ``reader`` callable, so the same class answers an in-memory
replay (tests, small runs) and a Postgres-backed reader (the API) without change.
"""

from __future__ import annotations

import bisect
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Callable, Optional

import pandas as pd

from backtest.options.model_pricing import settlement_price
from backtest.options.pricing_feed import LegQuote
from backtest.options.quote_providers import MIN_QUOTE_TICK, NoExecutableQuoteError
from backtest.timekeeping import EXCHANGE_TZ, from_market_clock

__all__ = [
    "SOURCE_TAG_CHAIN_HISTORY",
    "ChainQuoteUnavailable",
    "ChainHistoryFeed",
    "InMemoryChainReader",
]

SOURCE_TAG_CHAIN_HISTORY = "chain_history"

_TICK = Decimal(str(MIN_QUOTE_TICK))
_ZERO = Decimal("0")

#: reader(underlying, expiry, strike, option_type, bar_time) -> row dict | None.
#: The row carries whichever of bid/ask/ltp/iv/delta/gamma/theta/vega/oi/volume
#: the venue published; the feed abstains on absent fields, never invents them.
ChainReader = Callable[[str, date, float, str, datetime], Optional[dict]]


class ChainQuoteUnavailable(NoExecutableQuoteError):
    """No recorded, executable premium for one leg at one bar.

    Distinct reason code so a soak can count missing-chain refusals apart from
    the live-feed untrusted-quote class. Subclasses ``NoExecutableQuoteError``
    so the execution seam treats it as a client rejection, not a server fault.
    """

    reason = "NO_CHAIN_DATA"


def _quantize(value: float) -> Decimal:
    ticks = (Decimal(str(value)) / _TICK).to_integral_value(rounding=ROUND_HALF_UP)
    return ticks * _TICK


def _aware_exchange(bar_time: datetime) -> datetime:
    if bar_time.tzinfo is None:
        return from_market_clock(bar_time)
    return bar_time.astimezone(EXCHANGE_TZ)


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


class InMemoryChainReader:
    """Asof reader over an in-memory batch of recorded rows.

    Rows are grouped by contract ``(underlying, expiry, strike, option_type)``
    and sorted by ``bar_ts``; a query returns the last row at or before the bar
    (the same asof semantics the model feed uses for IV). Naive bar stamps are
    exchange time (the declared door); aware ones are converted, never guessed.
    """

    def __init__(self, rows: list[dict]) -> None:
        self._books: dict[tuple, list[tuple[datetime, dict]]] = {}
        for row in rows:
            ts = row.get("bar_ts")
            if ts is None:
                continue
            moment = _aware_exchange(ts)
            key = (
                str(row.get("underlying")),
                row.get("expiry"),
                float(row.get("strike")),
                str(row.get("option_type")).upper(),
            )
            self._books.setdefault(key, []).append((moment, row))
        for book in self._books.values():
            book.sort(key=lambda pair: pair[0])
        self._stamps = {key: [ts for ts, _ in book] for key, book in self._books.items()}

    def __call__(
        self,
        underlying: str,
        expiry: date,
        strike: float,
        option_type: str,
        bar_time: datetime,
    ) -> Optional[dict]:
        key = (str(underlying), expiry, float(strike), str(option_type).upper())
        stamps = self._stamps.get(key)
        if not stamps:
            return None
        idx = bisect.bisect_right(stamps, _aware_exchange(bar_time)) - 1
        if idx < 0:
            return None
        return self._books[key][idx][1]


class ChainHistoryFeed:
    """Replay recorded broker chains. ``data_source_real`` is always True.

    ``reader`` resolves a contract+bar to its recorded row (see
    :class:`InMemoryChainReader`). ``liquidity_gate`` (default on) refuses a
    strike the venue reports as having neither open interest nor volume; the
    two-sided-book and positive-LTP requirements always apply.
    """

    source_tag = SOURCE_TAG_CHAIN_HISTORY
    data_source_real = True

    def __init__(
        self,
        reader: ChainReader,
        *,
        liquidity_gate: bool = True,
        risk_free: float = 0.065,
    ) -> None:
        self._reader = reader
        self.liquidity_gate = bool(liquidity_gate)
        self.risk_free = float(risk_free)
        self._reference: datetime | None = None

    def set_reference(self, bar_time: datetime) -> None:
        self._reference = _aware_exchange(bar_time)

    def _refusal(self, row: Optional[dict], label: str) -> Optional[str]:
        if row is None:
            return f"no recorded chain for {label}"
        bid, ask, ltp = _num(row.get("bid")), _num(row.get("ask")), _num(row.get("ltp"))
        if bid is None or ask is None or ltp is None:
            return f"incomplete book/ltp for {label}"
        if bid <= 0 or ask <= 0:
            return f"zero book for {label} (bid {bid}, ask {ask})"
        if ask < bid:
            return f"crossed book for {label} (bid {bid} > ask {ask})"
        if ltp <= 0:
            return f"zero last trade for {label}"
        if self.liquidity_gate:
            oi, vol = _num(row.get("oi")), _num(row.get("volume"))
            if (oi is not None or vol is not None) and (oi or 0) <= 0 and (vol or 0) <= 0:
                return f"no open interest or volume for {label}"
        return None

    def quote(
        self,
        *,
        underlying: str,
        strike: float,
        expiry: date,
        option_type: str,
        spot: float,
        bar_time: datetime | None = None,
    ) -> LegQuote:
        if option_type not in ("CE", "PE"):
            raise ValueError(f"option_type must be CE or PE, got {option_type!r}")
        moment = bar_time if bar_time is not None else self._reference
        if moment is None:
            raise ValueError("no bar_time given and no reference pinned")
        moment = _aware_exchange(moment)
        label = f"{underlying} {strike:g}{option_type} {expiry.isoformat()} @ {moment.isoformat()}"
        row = self._reader(underlying, expiry, float(strike), option_type, moment)
        refusal = self._refusal(row, label)
        if refusal:
            raise ChainQuoteUnavailable(refusal)

        bid = _quantize(_num(row.get("bid")))
        ask = _quantize(_num(row.get("ask")))
        mid = _quantize((float(bid) + float(ask)) / 2.0)
        if mid <= _ZERO:
            mid = max(bid, _TICK)

        def _greek(key: str) -> float:
            value = _num(row.get(key))
            return float(value) if value is not None else 0.0

        return LegQuote(
            mid=mid,
            bid=bid,
            ask=ask,
            iv=_greek("iv"),
            delta=_greek("delta"),
            gamma=_greek("gamma"),
            theta=_greek("theta"),
            vega=_greek("vega"),
            source_tag=self.source_tag,
            bar_time=moment,
        )

    def expiry_settlement(
        self,
        session_closes: pd.Series,
        volumes: pd.Series | None = None,
    ) -> Decimal:
        # Settlement is the index's own VWAP from real candles — the same
        # exchange convention the model feed uses; only the option premiums
        # differ between the two feeds.
        return _quantize(settlement_price(session_closes, volumes))
