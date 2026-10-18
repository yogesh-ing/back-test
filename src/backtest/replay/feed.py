"""One-bar-at-a-time replay feed, option chain reconstructor and quote adapter."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable

import pandas as pd

from backtest.instruments.option import OptionContract
from backtest.replay.clock import SimulatedClock
from backtest.replay.data import ReplayContract, ReplayDataError, ReplayDataset


class ReplayFeedError(RuntimeError):
    """Replay feed contract was violated."""


class NoFreshReplayQuote(ReplayFeedError):
    """The requested contract has no quote in the current candle."""

    code = "STRIKE_NO_FRESH_QUOTE"

    def __init__(self, instrument_token: str, timestamp: Any) -> None:
        self.instrument_token = str(instrument_token)
        self.timestamp = str(timestamp)
        super().__init__(
            f"{self.code}: no quote for {self.instrument_token} at {self.timestamp}; "
            "replay does not carry prices forward"
        )


@dataclass
class ReplayFaultPlan:
    """Data/order faults to author a Phase-1 failure-matrix replay.

    Timestamps are bar-open times. A dropped option row is absent from both
    chain reconstruction and quote lookup; a lot-size override affects only
    the contract metadata on that timestamp. Neither fault manufactures a
    price.
    """

    drop_quotes: set[tuple[str, str]] = field(default_factory=set)
    drop_contracts: set[tuple[str, str, str, str]] = field(default_factory=set)
    lot_size_overrides: dict[tuple[str, str], int] = field(default_factory=dict)
    reject_entry_bars: set[str] = field(default_factory=set)
    stale_windows: list[tuple[str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.drop_quotes = {(str(token), _timestamp_key(ts)) for token, ts in self.drop_quotes}
        self.drop_contracts = {
            (
                _timestamp_key(ts),
                str(expiry)[:10],
                str(Decimal(str(strike))),
                str(kind).upper(),
            )
            for ts, expiry, strike, kind in self.drop_contracts
        }
        self.lot_size_overrides = {
            (str(token), _timestamp_key(ts)): int(size)
            for (token, ts), size in self.lot_size_overrides.items()
        }
        if any(size < 1 for size in self.lot_size_overrides.values()):
            raise ReplayDataError("injected lot-size overrides must be positive")
        self.reject_entry_bars = {_timestamp_key(ts) for ts in self.reject_entry_bars}
        self.stale_windows = [
            (_timestamp_key(start), _timestamp_key(end)) for start, end in self.stale_windows
        ]

    def drops_quote(self, contract: ReplayContract, bar_open: pd.Timestamp) -> bool:
        key = _timestamp_key(bar_open)
        if (contract.instrument_token, key) in self.drop_quotes:
            return True
        contract_key = (
            key,
            contract.expiry.isoformat(),
            str(contract.strike),
            contract.option_type,
        )
        if contract_key in self.drop_contracts:
            return True
        return any(start <= key <= end for start, end in self.stale_windows)

    def lot_size(self, contract: ReplayContract, bar_open: pd.Timestamp, actual: int) -> int:
        return self.lot_size_overrides.get(
            (contract.instrument_token, _timestamp_key(bar_open)), int(actual)
        )

    def reject_entry(self, bar_open: pd.Timestamp) -> bool:
        return _timestamp_key(bar_open) in self.reject_entry_bars

    def stale_feed(self, bar_open: pd.Timestamp) -> bool:
        key = _timestamp_key(bar_open)
        return any(start <= key <= end for start, end in self.stale_windows)


@dataclass(frozen=True)
class ReplayBar:
    """A closed underlying candle and its bar-open coordinate."""

    symbol: str
    bar_open: pd.Timestamp
    bar_close: pd.Timestamp
    open: float
    high: float
    low: float
    close: float
    volume: float

    def as_runner_candle(self) -> dict[str, Any]:
        # StrategyRunner's timestamp is the close of the completed signal bar.
        return {
            "ts": self.bar_close.isoformat(),
            "bar_open_ts": self.bar_open.isoformat(),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
        }


class PointInTimeReplayFeed:
    """Reveals only the current completed row to a replay consumer.

    The normalized source frames remain private to the feed. Quote and chain
    access is exact-timestamp only; there is no last-value cache or forward
    fill. Each :meth:`advance` moves the simulated clock to the current bar's
    close before making its option observations visible.
    """

    def __init__(
        self,
        dataset: ReplayDataset,
        clock: SimulatedClock,
        fault_plan: ReplayFaultPlan | None = None,
    ) -> None:
        self.dataset = dataset
        self.clock = clock
        self.fault_plan = fault_plan or ReplayFaultPlan()
        self._current: ReplayBar | None = None
        self._current_quotes: dict[str, dict[str, Any]] = {}
        self._current_contracts: dict[str, ReplayContract] = {}

    @property
    def current_bar(self) -> ReplayBar | None:
        return self._current

    @property
    def current_quotes(self) -> dict[str, dict[str, Any]]:
        """A read-only-by-convention current-time snapshot; never future rows."""
        return {token: dict(row) for token, row in self._current_quotes.items()}

    @property
    def current_contracts(self) -> dict[str, ReplayContract]:
        return dict(self._current_contracts)

    def advance(self, index: int) -> ReplayBar:
        if index < 0 or index >= self.dataset.bar_count:
            raise IndexError(index)
        bar_open = pd.Timestamp(self.dataset.underlying.index[index])
        bar_close = self.dataset.bar_close(bar_open)
        self.clock.advance_to(bar_close)

        row = self.dataset.underlying.iloc[index]
        self._current = ReplayBar(
            symbol=self.dataset.underlying_symbol,
            bar_open=bar_open,
            bar_close=bar_close,
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row.get("volume", 0)),
        )
        self._current_quotes = {}
        self._current_contracts = {}
        # This is a point-in-time join, not a nearest/as-of join. A missing
        # minute stays missing, even when a previous quote exists.
        for token, contract in self.dataset.contracts.items():
            frame = self.dataset.option_frames.get(token)
            if frame is None or bar_open not in frame.index:
                continue
            if self.fault_plan.drops_quote(contract, bar_open):
                continue
            option_row = frame.loc[bar_open]
            lot_size = self.fault_plan.lot_size(contract, bar_open, int(option_row["lot_size"]))
            current_contract = ReplayContract(
                instrument_token=contract.instrument_token,
                trading_symbol=contract.trading_symbol,
                underlying=contract.underlying,
                expiry=contract.expiry,
                strike=contract.strike,
                option_type=contract.option_type,
                lot_size=lot_size,
            )
            values = option_row.to_dict()
            values["_bar_open"] = bar_open
            values["_bar_close"] = bar_close
            values["_lot_size"] = lot_size
            self._current_quotes[token] = values
            self._current_contracts[token] = current_contract

        if self.clock.now() < bar_close.to_pydatetime():
            raise ReplayFeedError("current bar became visible before its close")
        return self._current

    def available_quote(self, instrument_token: str) -> bool:
        return str(instrument_token) in self._current_quotes

    def missing_quotes(self, instrument_tokens: Iterable[str]) -> list[str]:
        return sorted(
            str(token) for token in instrument_tokens if str(token) not in self._current_quotes
        )

    def quote_row(self, instrument_token: str) -> dict[str, Any]:
        token = str(instrument_token)
        if self._current is None:
            raise ReplayFeedError("quote requested before the first replay bar")
        if self.clock.now() < self._current.bar_close.to_pydatetime():
            raise ReplayFeedError("quote requested before this candle closed")
        try:
            return self._current_quotes[token]
        except KeyError as exc:
            raise NoFreshReplayQuote(token, self.clock.now().isoformat()) from exc

    def get_candles(
        self,
        symbol: str,
        start: str | None = None,
        end: str | None = None,
        interval: str = "1min",
    ) -> pd.DataFrame:
        """DataSource-compatible, clock-filtered view for probes/tests.

        Unlike a loader, it returns only bars whose close is no later than the
        simulated clock. The existing StrategyRunner consumes individual
        bars, so it does not call this method in the replay path.
        """
        if str(symbol).strip().upper() != self.dataset.underlying_symbol:
            return pd.DataFrame(columns=self.dataset.underlying.columns)
        if self._current is None:
            return self.dataset.underlying.iloc[:0].copy()
        visible = self.dataset.underlying.loc[
            self.dataset.underlying.index + self.dataset.interval <= self.clock.now()
        ]
        if start:
            visible = visible.loc[visible.index >= _timestamp_key(start)]
        if end:
            visible = visible.loc[visible.index + self.dataset.interval <= _timestamp_key(end)]
        return visible.copy()


class ReplayChainGenerator:
    """Duck-typed chain generator backed by exact-time CSV contract rows."""

    def __init__(self, feed: PointInTimeReplayFeed) -> None:
        self.feed = feed
        self._spots: dict[str, float] = {}

    def set_spot(self, underlying: str, spot: float) -> None:
        self._spots[str(underlying).upper()] = float(spot)

    def get_spot(self, underlying: str) -> float:
        key = str(underlying).upper()
        if key in self._spots:
            return self._spots[key]
        if self.feed.current_bar is not None and key == self.feed.current_bar.symbol:
            return float(self.feed.current_bar.close)
        raise ReplayFeedError(f"no point-in-time spot for {key}")

    def available_expiries(
        self,
        underlying: str,
        count: int = 3,
        reference: date | None = None,
    ) -> list[date]:
        underlying = str(underlying).upper()
        expiries = sorted(
            {
                contract.expiry
                for contract in self.feed.current_contracts.values()
                if contract.underlying == underlying
                and (reference is None or contract.expiry >= reference)
            }
        )
        return expiries[: max(0, int(count))]

    def generate_chain(
        self,
        underlying: str,
        expiry: date | None = None,
        strikes_each_side: int | None = None,
        option_type: str = "CE",
    ) -> dict[Decimal, OptionContract]:
        del strikes_each_side  # the file, not a model, defines available strikes
        underlying = str(underlying).upper()
        option_type = str(option_type).upper()
        chain: dict[Decimal, OptionContract] = {}
        for token, current in self.feed.current_contracts.items():
            if current.underlying != underlying or current.option_type != option_type:
                continue
            if expiry is not None and current.expiry != expiry:
                continue
            if current.expiry is None:
                continue
            strike = current.strike
            if strike in chain:
                raise ReplayDataError(
                    f"duplicate fresh contract at {current.expiry} {strike} {option_type}; "
                    "resolve duplicate vendor symbols before replay"
                )
            chain[strike] = current.to_option_contract(current.lot_size)
        return chain


class ReplayQuoteProvider:
    """Option quote adapter using OHLC values from the current replay bar."""

    source_name = "replay:ohlcv"
    is_replay = True

    def __init__(self, feed: PointInTimeReplayFeed) -> None:
        self.feed = feed
        self.generator = ReplayChainGenerator(feed)
        self.phase = "close"
        self._exit_prices: dict[str, float] | None = None
        self.read_audit: list[dict[str, Any]] = []

    @property
    def current_fill_timestamp(self) -> datetime:
        if self.feed.current_bar is None:
            raise ReplayFeedError("cannot fill before a replay bar")
        return self.feed.current_bar.bar_open.to_pydatetime()

    def set_phase(self, phase: str) -> None:
        if phase not in {"open", "close", "high", "low", "exit"}:
            raise ValueError(f"unknown replay quote phase {phase!r}")
        self.phase = phase

    def set_exit_prices(self, prices: dict[str, float]) -> None:
        self._exit_prices = {str(token): float(price) for token, price in prices.items()}
        self.phase = "exit"

    def clear_exit_prices(self) -> None:
        self._exit_prices = None
        self.phase = "close"

    def should_reject_entry(self) -> bool:
        bar = self.feed.current_bar
        return bool(bar and self.feed.fault_plan.reject_entry(bar.bar_open))

    def validate_entry_quotes(self, intent: Any) -> None:
        """Require every leg's first quote to be at the next candle open.

        This keeps a resampled 5/15-minute candle from silently filling at a
        later, stale constituent observation while reporting the interval's
        nominal open.
        """
        bar = self.feed.current_bar
        if bar is None:
            raise ReplayFeedError("entry validation before the first replay bar")
        for leg in intent.legs:
            row = self.feed.quote_row(leg.instrument_token)
            current_contract = self.feed.current_contracts.get(leg.instrument_token)
            if current_contract is None:
                raise NoFreshReplayQuote(leg.instrument_token, bar.bar_open.isoformat())
            if current_contract.lot_size != leg.lot_size:
                raise ReplayDataError(
                    f"LOT_SIZE_CHANGED: {leg.trading_symbol} intent lot_size={leg.lot_size}, "
                    f"fill-bar lot_size={current_contract.lot_size}"
                )
            first = row.get("_source_first_ts")
            first_ts = pd.Timestamp(first) if first is not None else bar.bar_open
            if first_ts.tzinfo is None:
                first_ts = first_ts.tz_localize("UTC")
            first_ts = first_ts.tz_convert("UTC")
            if first_ts != bar.bar_open:
                raise NoFreshReplayQuote(
                    leg.instrument_token,
                    f"{bar.bar_open.isoformat()} (first observation "
                    f"{first_ts.isoformat()} was not at bar open)",
                )

    def register_chain(self, chain: dict[Decimal, OptionContract]) -> None:
        # Contract rows are already registered in the feed at this timestamp.
        del chain

    def set_reference(self, reference: datetime | None) -> None:
        # No model pricing clock. The selected OHLC row is the sole price source.
        del reference

    def get_quote(self, instrument_token: str) -> dict[str, Any]:
        token = str(instrument_token)
        if self.phase == "exit" and self._exit_prices is not None:
            if token not in self._exit_prices:
                raise NoFreshReplayQuote(token, self.feed.clock.now().isoformat())
            price = self._exit_prices[token]
            row = self.feed.quote_row(token)
            quote_ts = self.feed.current_bar.bar_close.isoformat()
        else:
            row = self.feed.quote_row(token)
            price_key = {
                "open": "open",
                "close": "close",
                "high": "high",
                "low": "low",
            }[self.phase]
            price = float(row[price_key])
            if self.phase == "open":
                quote_ts = str(
                    row.get("_source_first_ts") or self.feed.current_bar.bar_open.isoformat()
                )
            else:
                quote_ts = str(
                    row.get("_source_last_ts") or self.feed.current_bar.bar_close.isoformat()
                )
        result = {
            "ltp": float(price),
            "bid": (
                float(row["bid"])
                if row.get("bid") is not None and not pd.isna(row.get("bid"))
                else None
            ),
            "ask": (
                float(row["ask"])
                if row.get("ask") is not None and not pd.isna(row.get("ask"))
                else None
            ),
            "volume": float(row.get("volume", 0) or 0),
            "oi": int(row.get("oi", 0) or 0) if not pd.isna(row.get("oi", 0)) else 0,
            "source": "REPLAY",
            "phase": self.phase,
            "quote_timestamp": quote_ts,
            "bar_open": self.feed.current_bar.bar_open.isoformat(),
            "bar_close": self.feed.current_bar.bar_close.isoformat(),
        }
        self.read_audit.append(
            {
                "instrument_token": token,
                "phase": self.phase,
                "price": float(price),
                "quote_timestamp": quote_ts,
                "bar_open": self.feed.current_bar.bar_open.isoformat(),
                "bar_close": self.feed.current_bar.bar_close.isoformat(),
            }
        )
        return result

    def quote_ohlc(self, instrument_token: str) -> dict[str, Any]:
        token = str(instrument_token)
        row = self.feed.quote_row(token)
        values = {key: float(row[key]) for key in ("open", "high", "low", "close")}
        self.read_audit.append(
            {
                "instrument_token": token,
                "phase": "ohlc",
                "ohlc": values,
                "bar_open": self.feed.current_bar.bar_open.isoformat(),
                "bar_close": self.feed.current_bar.bar_close.isoformat(),
            }
        )
        return values

    def consume_read_audit(self) -> list[dict[str, Any]]:
        reads = list(self.read_audit)
        self.read_audit.clear()
        return reads


def _timestamp_key(value: Any) -> str:
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("Asia/Kolkata")
    return stamp.tz_convert("UTC").isoformat()


__all__ = [
    "NoFreshReplayQuote",
    "PointInTimeReplayFeed",
    "ReplayBar",
    "ReplayChainGenerator",
    "ReplayFaultPlan",
    "ReplayFeedError",
    "ReplayQuoteProvider",
]
