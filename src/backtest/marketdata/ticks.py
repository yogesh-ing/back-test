"""Normalized market data types (Step 10).

Every broker speaks a different dialect — mStock says ``ltp``, others say
``last_price`` or ``c``. Everything downstream of this module speaks exactly
one: the standard tick format from the plan document::

    {'symbol', 'timestamp', 'bid', 'ask', 'last', 'volume',
     'open', 'high', 'low', 'close'}

Conventions (repo-wide, do not break):

* **Prices are Decimal.** Floats are converted through ``repr`` by
  :func:`backtest.simulator.money.to_decimal` so ``0.1`` stays ``0.1``.
* **Timestamps are timezone-aware** and stored in UTC. Naive input is
  interpreted in ``naive_tz`` (IST for mStock) — never silently as UTC.
* **Bar ``ts`` is the bar OPEN time**, aligned to the timeframe boundary,
  matching the ``market_data_cache.ts`` column comment. Never bar close.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from backtest.marketdata.errors import NormalizationError
from backtest.simulator.money import to_decimal

__all__ = ["Tick", "Bar", "normalize_tick", "parse_timestamp"]


# ---------------------------------------------------------------------------
# Key aliases: broker dialect → standard field
# ---------------------------------------------------------------------------

_SYMBOL_KEYS = ("symbol", "tradingsymbol", "trading_symbol", "instrument", "name")
_TIME_KEYS = ("timestamp", "ts", "time", "t", "ltt", "last_trade_time", "datetime", "exchange_time")
_LAST_KEYS = ("last", "ltp", "last_price", "lastprice", "price", "last_traded_price")
_BID_KEYS = ("bid", "bid_price", "best_bid", "bp", "buy_price")
_ASK_KEYS = ("ask", "ask_price", "best_ask", "ap", "offer", "sell_price")
_VOLUME_KEYS = ("volume", "v", "vol", "qty", "quantity", "last_quantity", "ltq")
_OPEN_KEYS = ("open", "o")
_HIGH_KEYS = ("high", "h")
_LOW_KEYS = ("low", "l")
_CLOSE_KEYS = ("close", "c")

#: Epoch values above this are treated as milliseconds, not seconds.
_EPOCH_MS_THRESHOLD = 1_000_000_000_000


def _first(raw: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in raw and raw[key] is not None:
            return raw[key]
    return None


def parse_timestamp(value: Any, naive_tz: str = "UTC") -> datetime:
    """Coerce ``value`` to a timezone-aware UTC :class:`datetime`.

    Accepts aware/naive datetimes, epoch seconds or milliseconds, ISO-8601
    strings (including a trailing ``Z``), and anything with a
    ``to_pydatetime()`` method (pandas Timestamps). Naive results are
    interpreted in ``naive_tz`` — a live NSE feed stamps in IST, and
    treating that as UTC would shift every bar by 5h30m.
    """
    if value is None:
        raise NormalizationError("timestamp is required", code="missing_timestamp")

    if hasattr(value, "to_pydatetime"):  # pandas.Timestamp
        value = value.to_pydatetime()

    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, bool):
        raise NormalizationError(f"timestamp must not be bool: {value!r}", code="bad_timestamp")
    elif isinstance(value, (int, float)):
        epoch = float(value)
        if epoch <= 0:
            raise NormalizationError(f"epoch timestamp must be positive: {value!r}", code="bad_timestamp")
        if epoch >= _EPOCH_MS_THRESHOLD:
            epoch /= 1000.0
        dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError as exc:
            raise NormalizationError(
                f"unparseable timestamp: {value!r}", code="bad_timestamp"
            ) from exc
    else:
        raise NormalizationError(f"unsupported timestamp type: {value!r}", code="bad_timestamp")

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(naive_tz))
    return dt.astimezone(timezone.utc)


def _positive_price(value: Any, name: str) -> Decimal:
    try:
        dec = to_decimal(value, field=name)
    except ValueError as exc:
        raise NormalizationError(str(exc), code="bad_price", field=name) from exc
    if dec <= 0:
        raise NormalizationError(
            f"{name} must be positive, got {dec}", code="non_positive_price", field=name
        )
    return dec


def _optional_price(raw: Mapping[str, Any], keys: tuple[str, ...], name: str) -> Decimal | None:
    value = _first(raw, keys)
    if value is None:
        return None
    return _positive_price(value, name)


# ---------------------------------------------------------------------------
# Tick
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Tick:
    """One normalized market data update. Immutable, like :class:`Fill`."""

    symbol: str
    timestamp: datetime  # tz-aware, UTC
    last: Decimal
    bid: Decimal | None = None
    ask: Decimal | None = None
    volume: int = 0
    open: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None
    close: Decimal | None = None
    source: str = ""

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe representation (Decimals as strings, ISO timestamps)."""

        def _s(value: Decimal | None) -> str | None:
            return None if value is None else str(value)

        return {
            "symbol": self.symbol,
            "timestamp": self.timestamp.isoformat(),
            "bid": _s(self.bid),
            "ask": _s(self.ask),
            "last": _s(self.last),
            "volume": self.volume,
            "open": _s(self.open),
            "high": _s(self.high),
            "low": _s(self.low),
            "close": _s(self.close),
            "source": self.source,
        }


def normalize_tick(
    raw: Mapping[str, Any],
    symbol: str | None = None,
    naive_tz: str = "UTC",
    source: str = "",
) -> Tick:
    """Convert a broker-specific payload into a standard :class:`Tick`.

    Parameters
    ----------
    raw:
        Broker payload. Key dialects (``ltp``, ``last_price``, ``c`` …) are
        resolved through alias tables.
    symbol:
        Overrides / supplies the symbol when the payload lacks one.
    naive_tz:
        Timezone assumed for naive timestamps (mStock stamps in IST).
    source:
        Feed name recorded on the tick.

    Raises
    ------
    NormalizationError
        Missing symbol/timestamp/price, non-positive prices, negative
        volume, or a crossed quote (bid > ask).
    """
    if not isinstance(raw, Mapping):
        raise NormalizationError(
            f"payload must be a mapping, got {type(raw).__name__}", code="bad_payload"
        )

    sym = symbol or _first(raw, _SYMBOL_KEYS)
    if not sym or not str(sym).strip():
        raise NormalizationError("payload has no symbol", code="missing_symbol")
    sym = str(sym).strip().upper()

    timestamp = parse_timestamp(_first(raw, _TIME_KEYS), naive_tz=naive_tz)

    close = _optional_price(raw, _CLOSE_KEYS, "close")
    last_value = _first(raw, _LAST_KEYS)
    if last_value is not None:
        last = _positive_price(last_value, "last")
    elif close is not None:
        last = close  # bar-shaped payloads carry no separate last trade
    else:
        raise NormalizationError(
            "payload has no last/ltp/close price", code="missing_price", symbol=sym
        )

    bid = _optional_price(raw, _BID_KEYS, "bid")
    ask = _optional_price(raw, _ASK_KEYS, "ask")
    if bid is not None and ask is not None and bid > ask:
        raise NormalizationError(
            f"crossed quote: bid {bid} > ask {ask}", code="crossed_quote", symbol=sym
        )

    volume_value = _first(raw, _VOLUME_KEYS)
    if volume_value is None:
        volume = 0
    else:
        try:
            volume = int(to_decimal(volume_value, field="volume"))
        except ValueError as exc:
            raise NormalizationError(str(exc), code="bad_volume", symbol=sym) from exc
        if volume < 0:
            raise NormalizationError(
                f"volume must be non-negative, got {volume}", code="negative_volume", symbol=sym
            )

    return Tick(
        symbol=sym,
        timestamp=timestamp,
        last=last,
        bid=bid,
        ask=ask,
        volume=volume,
        open=_optional_price(raw, _OPEN_KEYS, "open"),
        high=_optional_price(raw, _HIGH_KEYS, "high"),
        low=_optional_price(raw, _LOW_KEYS, "low"),
        close=close,
        source=source,
    )


# ---------------------------------------------------------------------------
# Bar
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Bar:
    """One OHLCV bar, built from ticks by the aggregator.

    ``ts`` is the bar **open** time, aligned to the timeframe boundary —
    the same convention as the ``market_data_cache`` table. Mutable while
    building; ``complete=True`` marks it closed (no further updates).
    """

    symbol: str
    timeframe: str
    ts: datetime  # tz-aware bar OPEN time, aligned
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int = 0
    tick_count: int = 0
    complete: bool = False
    #: True for gap-fill bars fabricated by the aggregator (never persisted).
    synthetic: bool = False
    source: str = ""
    bid: Decimal | None = field(default=None, repr=False)
    ask: Decimal | None = field(default=None, repr=False)

    def update(self, tick: Tick) -> None:
        """Fold ``tick`` into this bar. The caller guards boundary logic."""
        if self.complete:
            raise ValueError(f"cannot update a completed bar: {self!r}")
        if tick.last > self.high:
            self.high = tick.last
        if tick.last < self.low:
            self.low = tick.last
        self.close = tick.last
        self.volume += tick.volume
        self.tick_count += 1
        if tick.bid is not None:
            self.bid = tick.bid
        if tick.ask is not None:
            self.ask = tick.ask

    def update_late(self, tick: Tick) -> None:
        """Fold a late (out-of-order) tick in without touching ``close``.

        A tick that happened *before* the current bar's window opened still
        contributes volume and can extend the extremes, but must not become
        the closing print — that would rewrite time.
        """
        if self.complete:
            raise ValueError(f"cannot update a completed bar: {self!r}")
        if tick.last > self.high:
            self.high = tick.last
        if tick.last < self.low:
            self.low = tick.last
        self.volume += tick.volume
        self.tick_count += 1

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe representation."""
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "ts": self.ts.isoformat(),
            "open": str(self.open),
            "high": str(self.high),
            "low": str(self.low),
            "close": str(self.close),
            "volume": self.volume,
            "tick_count": self.tick_count,
            "complete": self.complete,
            "synthetic": self.synthetic,
            "source": self.source,
        }

    @classmethod
    def from_tick(cls, tick: Tick, timeframe: str, ts: datetime) -> "Bar":
        """Open a new bar from the first tick of its window."""
        return cls(
            symbol=tick.symbol,
            timeframe=timeframe,
            ts=ts,
            open=tick.last,
            high=tick.last,
            low=tick.last,
            close=tick.last,
            volume=tick.volume,
            tick_count=1,
            source=tick.source,
            bid=tick.bid,
            ask=tick.ask,
        )
