"""Point-in-time CSV data model for the after-market options replay sandbox.

The CSV layer is deliberately separate from the existing backtest loaders:
option contracts are market data, not synthetic model inputs.  Timestamps in
source files identify the **start** of a candle.  Naive timestamps are
interpreted in ``Asia/Kolkata``; internal timestamps are UTC-aware.

Only complete underlying bars are emitted when resampling.  Option bars are
aggregated from observations actually present in the interval; an absent
contract/interval remains absent and is never forward-filled.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

import pandas as pd

from backtest.instruments.base import OptionType
from backtest.instruments.option import OptionContract

NSE_TIMEZONE = ZoneInfo("Asia/Kolkata")
UTC = "UTC"
SUPPORTED_INTERVALS = {"1min": 1, "5min": 5, "15min": 15}
_SESSION_OPEN = time(9, 15)
_SESSION_CLOSE = time(15, 30)
_REQUIRED_OHLC = ("open", "high", "low", "close")
_TIMESTAMP_ALIASES = ("timestamp", "datetime", "ts", "date")


class ReplayDataError(ValueError):
    """Invalid or internally inconsistent replay input."""


@dataclass(frozen=True)
class ReplayContract:
    """Static identity for one option contract; lot size may vary by row."""

    instrument_token: str
    trading_symbol: str
    underlying: str
    expiry: date
    strike: Decimal
    option_type: str
    lot_size: int

    def __post_init__(self) -> None:
        underlying = str(self.underlying).strip().upper()
        option_type = str(self.option_type).strip().upper()
        token = str(self.instrument_token).strip()
        symbol = str(self.trading_symbol).strip()
        try:
            strike = Decimal(str(self.strike))
        except (InvalidOperation, TypeError) as exc:
            raise ReplayDataError(f"invalid strike for {token!r}: {self.strike!r}") from exc
        if not token:
            raise ReplayDataError("option contract instrument_token must not be empty")
        if not symbol:
            raise ReplayDataError(f"option contract {token!r} needs a trading_symbol")
        if underlying not in {"NIFTY", "BANKNIFTY"}:
            raise ReplayDataError(f"V1 replay supports NIFTY or BANKNIFTY, got {underlying!r}")
        if option_type not in {"CE", "PE"}:
            raise ReplayDataError(f"option_type must be CE or PE, got {option_type!r}")
        if strike <= 0:
            raise ReplayDataError(f"strike must be positive for {token!r}")
        if int(self.lot_size) < 1:
            raise ReplayDataError(f"lot_size must be positive for {token!r}")
        object.__setattr__(self, "instrument_token", token)
        object.__setattr__(self, "trading_symbol", symbol)
        object.__setattr__(self, "underlying", underlying)
        object.__setattr__(self, "expiry", _coerce_date(self.expiry))
        object.__setattr__(self, "strike", strike)
        object.__setattr__(self, "option_type", option_type)
        object.__setattr__(self, "lot_size", int(self.lot_size))

    def to_option_contract(self, lot_size: int | None = None) -> OptionContract:
        """Build the existing engine's contract object from this CSV identity."""
        return OptionContract(
            instrument_token=self.instrument_token,
            trading_symbol=self.trading_symbol,
            underlying=self.underlying,
            expiry=self.expiry,
            strike=self.strike,
            option_type=OptionType(self.option_type),
            lot_size=int(lot_size if lot_size is not None else self.lot_size),
            metadata={"source": "REPLAY"},
        )


@dataclass
class ReplayDataset:
    """Normalized underlying and per-contract option candles for one index.

    Construct with :meth:`from_csv` for vendor files or :meth:`from_frames`
    for tests and programmatic feeds.  ``underlying`` and each option frame
    are indexed by candle-start time in UTC.  The engine is never handed these
    full frames: :class:`~backtest.replay.feed.PointInTimeReplayFeed` reveals
    one closed candle at a time.
    """

    underlying: pd.DataFrame
    option_frames: dict[str, pd.DataFrame]
    contracts: dict[str, ReplayContract]
    underlying_symbol: str
    timeframe: str
    source_fingerprints: dict[str, str]
    fingerprint: str
    quality_report: dict[str, Any]

    @classmethod
    def from_csv(
        cls,
        underlying_csv: str | Path,
        option_csvs: Iterable[str | Path] | None = None,
        *,
        options_dir: str | Path | None = None,
        contract_metadata: Mapping[str, Mapping[str, Any]] | None = None,
        underlying_symbol: str = "NIFTY",
        timeframe: str = "1min",
        timestamp_kind: str = "open",
        naive_timezone: str = "Asia/Kolkata",
    ) -> "ReplayDataset":
        """Load one underlying CSV and one CSV per option contract.

        Contract identity may be repeated as constant columns in each file
        (``underlying, expiry, strike, option_type, lot_size`` and optional
        ``instrument_token, trading_symbol``), or supplied in
        ``contract_metadata`` keyed by instrument token or CSV stem.  Changing
        ``lot_size`` by row is supported and is carried into each point-in-time
        chain snapshot.
        """
        underlying_path = Path(underlying_csv)
        if not underlying_path.is_file():
            raise FileNotFoundError(underlying_path)

        paths = [Path(path) for path in (option_csvs or [])]
        if options_dir is not None:
            root = Path(options_dir)
            if not root.is_dir():
                raise NotADirectoryError(root)
            paths.extend(sorted(root.glob("*.csv")))
        paths = sorted({path.resolve() for path in paths})
        if not paths:
            raise ReplayDataError("at least one per-contract option CSV is required")

        option_frames: dict[str, pd.DataFrame] = {}
        contracts: dict[str, ReplayContract] = {}
        fingerprints: dict[str, str] = {
            _stable_file_label(underlying_path): _sha256_file(underlying_path)
        }
        supplied_metadata = dict(contract_metadata or {})

        for path in paths:
            if not path.is_file():
                raise FileNotFoundError(path)
            raw = pd.read_csv(path)
            raw.columns = [str(column).strip().lower() for column in raw.columns]
            token_values = _single_values(raw, ("instrument_token", "token"))
            token_hint = str(token_values or path.stem)
            candidates = supplied_metadata.get(
                str(path),
                supplied_metadata.get(
                    path.stem,
                    supplied_metadata.get(token_hint, {}),
                ),
            )
            metadata = dict(candidates or {})
            token = str(metadata.get("instrument_token") or token_values or path.stem)
            metadata.setdefault("instrument_token", token)
            metadata.setdefault(
                "trading_symbol",
                _single_values(raw, ("trading_symbol", "symbol")) or path.stem,
            )
            metadata.setdefault(
                "underlying",
                _single_values(raw, ("underlying",)) or underlying_symbol,
            )
            metadata.setdefault("expiry", _single_values(raw, ("expiry", "expiry_date")))
            metadata.setdefault("strike", _single_values(raw, ("strike",)))
            metadata.setdefault(
                "option_type",
                _single_values(raw, ("option_type", "right", "type")),
            )
            metadata.setdefault("lot_size", _first_value(raw, ("lot_size", "lotsize")))
            missing = [
                key
                for key in ("expiry", "strike", "option_type", "lot_size")
                if metadata.get(key) is None
            ]
            if missing:
                raise ReplayDataError(
                    f"{path}: missing contract metadata {missing}; add constant columns "
                    "or pass contract_metadata"
                )
            contract = ReplayContract(
                instrument_token=str(metadata["instrument_token"]),
                trading_symbol=str(metadata["trading_symbol"]),
                underlying=str(metadata["underlying"]),
                expiry=_coerce_date(metadata["expiry"]),
                strike=Decimal(str(metadata["strike"])),
                option_type=str(metadata["option_type"]),
                lot_size=int(float(metadata["lot_size"])),
            )
            if contract.instrument_token in contracts:
                raise ReplayDataError(
                    f"duplicate option instrument_token {contract.instrument_token!r} in {path}"
                )
            contracts[contract.instrument_token] = contract
            frame = raw.copy()
            # Static contract metadata is retained only where useful. The
            # contract's lot size is inserted if the file has no row value.
            if "lot_size" not in frame and "lotsize" in frame:
                frame["lot_size"] = frame["lotsize"]
            if "lot_size" not in frame:
                frame["lot_size"] = contract.lot_size
            frame["instrument_token"] = contract.instrument_token
            frame["trading_symbol"] = contract.trading_symbol
            option_frames[contract.instrument_token] = frame
            source_label = f"option:{contract.instrument_token}:{path.name}"
            fingerprints[source_label] = _sha256_file(path)

        return cls.from_frames(
            pd.read_csv(underlying_path),
            option_frames,
            contracts,
            underlying_symbol=underlying_symbol,
            timeframe=timeframe,
            timestamp_kind=timestamp_kind,
            naive_timezone=naive_timezone,
            source_fingerprints=fingerprints,
        )

    @classmethod
    def from_frames(
        cls,
        underlying: pd.DataFrame,
        option_frames: Mapping[str, pd.DataFrame],
        contracts: Mapping[str, ReplayContract | Mapping[str, Any]],
        *,
        underlying_symbol: str = "NIFTY",
        timeframe: str = "1min",
        timestamp_kind: str = "open",
        naive_timezone: str = "Asia/Kolkata",
        source_fingerprints: Mapping[str, str] | None = None,
    ) -> "ReplayDataset":
        """Normalize in-memory OHLCV frames using the same rules as CSV loads."""
        symbol = str(underlying_symbol).strip().upper()
        if symbol not in {"NIFTY", "BANKNIFTY"}:
            raise ReplayDataError(f"V1 replay supports NIFTY or BANKNIFTY, got {symbol!r}")
        interval = _interval_minutes(timeframe)
        kind = str(timestamp_kind).strip().lower()
        if kind not in {"open", "close"}:
            raise ReplayDataError("timestamp_kind must be 'open' or 'close'")
        minute_delta = pd.Timedelta(minutes=1)

        under = _normalize_frame(
            underlying,
            label=f"{symbol} underlying",
            naive_timezone=naive_timezone,
            option=False,
        )
        if kind == "close":
            under.index = under.index - minute_delta
        under = _keep_session_rows(under)
        if under.empty:
            raise ReplayDataError("underlying has no rows inside NSE regular-session hours")

        normalized_contracts: dict[str, ReplayContract] = {}
        normalized_options: dict[str, pd.DataFrame] = {}
        for token, identity in contracts.items():
            contract = (
                identity if isinstance(identity, ReplayContract) else ReplayContract(**identity)
            )
            key = str(token)
            if key != contract.instrument_token:
                raise ReplayDataError(
                    f"contract map key {key!r} does not match token {contract.instrument_token!r}"
                )
            if contract.underlying != symbol:
                raise ReplayDataError(
                    f"contract {key!r} underlying {contract.underlying!r} does not match {symbol!r}"
                )
            if key not in option_frames:
                raise ReplayDataError(f"missing quote frame for contract {key!r}")
            frame = _normalize_frame(
                option_frames[key],
                label=f"option {key}",
                naive_timezone=naive_timezone,
                option=True,
            )
            if kind == "close":
                frame.index = frame.index - minute_delta
            if "lot_size" not in frame:
                frame["lot_size"] = contract.lot_size
            frame["lot_size"] = pd.to_numeric(frame["lot_size"], errors="raise").astype(int)
            if (frame["lot_size"] < 1).any():
                raise ReplayDataError(f"option {key!r} has non-positive lot_size")
            frame = _keep_session_rows(frame)
            if frame.empty:
                continue
            normalized_contracts[key] = contract
            normalized_options[key] = frame
        if not normalized_options:
            raise ReplayDataError("no option quote rows remain inside regular-session hours")

        if interval > 1:
            under = _resample_frame(under, interval, require_complete=True)
            normalized_options = {
                token: _resample_frame(frame, interval, require_complete=False)
                for token, frame in normalized_options.items()
            }
            normalized_options = {
                key: frame for key, frame in normalized_options.items() if not frame.empty
            }
        if under.empty:
            raise ReplayDataError(f"underlying has no complete {timeframe} bars")
        if not normalized_options:
            raise ReplayDataError(f"no option quotes remain after {timeframe} resampling")

        under = under.sort_index()
        normalized_options = {key: frame.sort_index() for key, frame in normalized_options.items()}
        fingerprints = dict(source_fingerprints or {})
        if not fingerprints:
            fingerprints = {"underlying:memory": _frame_fingerprint(under)}
            fingerprints.update(
                {
                    f"option:{token}": _frame_fingerprint(frame)
                    for token, frame in normalized_options.items()
                }
            )
        fingerprint_payload = {
            "schema": 1,
            "symbol": symbol,
            "timeframe": timeframe,
            "timestamp_kind": kind,
            "naive_timezone": naive_timezone,
            "contracts": {
                token: {
                    "trading_symbol": contract.trading_symbol,
                    "underlying": contract.underlying,
                    "expiry": contract.expiry.isoformat(),
                    "strike": str(contract.strike),
                    "option_type": contract.option_type,
                    "lot_size": contract.lot_size,
                }
                for token, contract in sorted(normalized_contracts.items())
            },
            "source_fingerprints": sorted(fingerprints.items()),
        }
        fingerprint = hashlib.sha256(
            json.dumps(
                fingerprint_payload,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        report = _quality_report(under, normalized_options, normalized_contracts, interval)
        return cls(
            underlying=under,
            option_frames=normalized_options,
            contracts=normalized_contracts,
            underlying_symbol=symbol,
            timeframe=timeframe,
            source_fingerprints=fingerprints,
            fingerprint=fingerprint,
            quality_report=report,
        )

    @property
    def interval_minutes(self) -> int:
        return _interval_minutes(self.timeframe)

    @property
    def interval(self) -> pd.Timedelta:
        return pd.Timedelta(minutes=self.interval_minutes)

    @property
    def bar_count(self) -> int:
        return len(self.underlying)

    @property
    def bar_opens(self) -> pd.DatetimeIndex:
        return pd.DatetimeIndex(self.underlying.index)

    def bar_close(self, bar_open: pd.Timestamp) -> pd.Timestamp:
        return pd.Timestamp(bar_open) + self.interval

    def prefix(self, bar_count: int) -> "ReplayDataset":
        """Return the first ``bar_count`` bars for an isolated replay run."""
        count = max(0, min(int(bar_count), self.bar_count))
        under = self.underlying.iloc[:count].copy()
        if count:
            last_open = under.index[-1]
            option_frames = {
                token: frame.loc[frame.index <= last_open].copy()
                for token, frame in self.option_frames.items()
            }
        else:
            option_frames = {
                token: frame.iloc[:0].copy() for token, frame in self.option_frames.items()
            }
        return _dataset_copy(self, under, option_frames)

    def prefix_through(self, cutoff: Any) -> "ReplayDataset":
        """Return bars whose simulated close is at or before ``cutoff``."""
        when = _timestamp_utc(cutoff, "Asia/Kolkata")
        closes = self.underlying.index + self.interval
        count = int((closes <= when).sum())
        return self.prefix(count)


def _dataset_copy(
    source: ReplayDataset,
    underlying: pd.DataFrame,
    option_frames: dict[str, pd.DataFrame],
) -> ReplayDataset:
    clone = ReplayDataset(
        underlying=underlying,
        option_frames=option_frames,
        contracts=source.contracts,
        underlying_symbol=source.underlying_symbol,
        timeframe=source.timeframe,
        source_fingerprints=source.source_fingerprints,
        fingerprint=source.fingerprint,
        quality_report=_quality_report(
            underlying, option_frames, source.contracts, source.interval_minutes
        ),
    )
    return clone


def _normalize_frame(
    frame: pd.DataFrame,
    *,
    label: str,
    naive_timezone: str,
    option: bool,
) -> pd.DataFrame:
    if frame is None or not isinstance(frame, pd.DataFrame) or frame.empty:
        raise ReplayDataError(f"{label} CSV/frame is empty")
    out = frame.copy()
    out.columns = [str(column).strip().lower() for column in out.columns]
    timestamp_column = next(
        (name for name in _TIMESTAMP_ALIASES if name in out.columns),
        None,
    )
    if not any(name in out.columns for name in ("timestamp", "datetime", "ts")) and {
        "date",
        "time",
    }.issubset(out.columns):
        date_values = out.pop("date").astype(str).str.strip()
        time_values = out.pop("time").astype(str).str.strip()
        combined = date_values + " " + time_values
        timestamps = _normalize_timestamps(combined, naive_timezone, label)
    elif timestamp_column is None:
        if not isinstance(out.index, pd.DatetimeIndex):
            raise ReplayDataError(f"{label} needs a timestamp column or DatetimeIndex")
        timestamps = _normalize_timestamps(out.index, naive_timezone, label)
    else:
        timestamps = _normalize_timestamps(out.pop(timestamp_column), naive_timezone, label)
    out.index = timestamps
    out.index.name = "timestamp"

    aliases = {"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"}
    for old, new in aliases.items():
        if old in out.columns and new not in out.columns:
            out = out.rename(columns={old: new})
    missing = [column for column in _REQUIRED_OHLC if column not in out.columns]
    if missing:
        raise ReplayDataError(f"{label} missing required OHLC columns: {missing}")
    if "volume" not in out.columns:
        out["volume"] = 0

    if out.index.has_duplicates:
        duplicates = out.index[out.index.duplicated()].unique()[:3]
        raise ReplayDataError(f"{label} has duplicate timestamps: {[str(ts) for ts in duplicates]}")
    out = out.sort_index()
    keep = list(_REQUIRED_OHLC) + ["volume"]
    if option:
        keep.extend(
            column
            for column in ("bid", "ask", "bid_size", "ask_size", "oi", "lot_size")
            if column in out
        )
    out = out.loc[:, keep]
    for column in _REQUIRED_OHLC + ("volume",):
        out[column] = pd.to_numeric(out[column], errors="raise")
        values = out[column].astype(float)
        if not values.map(math.isfinite).all():
            raise ReplayDataError(f"{label} contains non-finite {column} values")
    if (
        (out["open"] <= 0).any()
        or (out["high"] <= 0).any()
        or (out["low"] <= 0).any()
        or (out["close"] <= 0).any()
    ):
        raise ReplayDataError(f"{label} OHLC values must be positive")
    invalid = (
        (out["low"] > out[["open", "close"]].min(axis=1))
        | (out["high"] < out[["open", "close"]].max(axis=1))
        | (out["high"] < out["low"])
    )
    if invalid.any():
        sample = out.index[invalid][:3]
        raise ReplayDataError(f"{label} violates OHLC invariants at {[str(ts) for ts in sample]}")
    if option:
        for column in ("bid", "ask", "bid_size", "ask_size", "oi", "lot_size"):
            if column in out:
                out[column] = pd.to_numeric(out[column], errors="raise")
    return out


def _normalize_timestamps(
    values: Any,
    naive_timezone: str,
    label: str,
) -> pd.DatetimeIndex:
    try:
        parsed = pd.to_datetime(values, errors="raise")
        index = pd.DatetimeIndex(parsed)
    except Exception as exc:  # noqa: BLE001
        raise ReplayDataError(f"{label} has invalid timestamps: {exc}") from exc
    if index.tz is None:
        try:
            index = index.tz_localize(
                ZoneInfo(naive_timezone),
                ambiguous="raise",
                nonexistent="raise",
            )
        except Exception as exc:  # noqa: BLE001
            raise ReplayDataError(
                f"{label} naive timestamps cannot be localized to " f"{naive_timezone}: {exc}"
            ) from exc
    return index.tz_convert(UTC)


def _timestamp_utc(value: Any, naive_timezone: str) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize(ZoneInfo(naive_timezone))
    return stamp.tz_convert(UTC)


def _keep_session_rows(frame: pd.DataFrame) -> pd.DataFrame:
    local = frame.index.tz_convert(NSE_TIMEZONE)
    is_weekday = pd.Series(local.dayofweek < 5, index=frame.index)
    minute = pd.Series(local.hour * 60 + local.minute, index=frame.index)
    start = _SESSION_OPEN.hour * 60 + _SESSION_OPEN.minute
    end = _SESSION_CLOSE.hour * 60 + _SESSION_CLOSE.minute
    mask = is_weekday & (minute >= start) & (minute < end)
    return frame.loc[mask.to_numpy()].copy()


def _resample_frame(
    frame: pd.DataFrame,
    interval_minutes: int,
    *,
    require_complete: bool,
) -> pd.DataFrame:
    """Resample within NSE sessions without crossing boundaries or carrying.

    Underlying bars must have every 1-minute constituent. Options aggregate
    only observations present in the period; if there are none, no bar exists.
    The option output carries observation coverage so consumers can distinguish
    a dense bar from one based on only one trade.
    """
    if frame.empty:
        return frame.copy()
    local = frame.copy()
    local.index = local.index.tz_convert(NSE_TIMEZONE)
    rows: list[dict[str, Any]] = []
    indices: list[pd.Timestamp] = []
    session_start_minutes = _SESSION_OPEN.hour * 60 + _SESSION_OPEN.minute
    for day, day_frame in local.groupby(local.index.date, sort=True):
        starts = day_frame.index.hour * 60 + day_frame.index.minute
        bucket_ids = ((starts - session_start_minutes) // interval_minutes).astype(int)
        for bucket_id, group in day_frame.groupby(bucket_ids, sort=True):
            bucket_start = pd.Timestamp(datetime.combine(day, _SESSION_OPEN), tz=NSE_TIMEZONE)
            bucket_start += pd.Timedelta(minutes=int(bucket_id) * interval_minutes)
            bucket_end = bucket_start + pd.Timedelta(minutes=interval_minutes)
            if bucket_start.time() < _SESSION_OPEN or bucket_end.time() > _SESSION_CLOSE:
                continue
            if require_complete:
                expected = pd.date_range(
                    bucket_start,
                    periods=interval_minutes,
                    freq="1min",
                    tz=NSE_TIMEZONE,
                )
                if len(group) != interval_minutes or not expected.isin(group.index).all():
                    continue
            if group.empty:
                continue
            record: dict[str, Any] = {
                "open": float(group.iloc[0]["open"]),
                "high": float(group["high"].max()),
                "low": float(group["low"].min()),
                "close": float(group.iloc[-1]["close"]),
                "volume": float(group["volume"].sum()),
                "_source_count": int(len(group)),
                "_source_first_ts": group.index[0].tz_convert("UTC").isoformat(),
                "_source_last_ts": group.index[-1].tz_convert("UTC").isoformat(),
            }
            for column in group.columns:
                if column in _REQUIRED_OHLC or column == "volume":
                    continue
                if column == "lot_size":
                    values = pd.to_numeric(group[column], errors="raise").dropna().unique()
                    if len(values) > 1:
                        raise ReplayDataError(
                            f"lot_size changes inside {interval_minutes}-minute bar "
                            f"at {bucket_start}"
                        )
                    record[column] = int(values[-1]) if len(values) else None
                else:
                    record[column] = group.iloc[-1][column]
            rows.append(record)
            indices.append(bucket_start.tz_convert(UTC))
    if not rows:
        return pd.DataFrame(columns=list(frame.columns), index=pd.DatetimeIndex([], tz=UTC))
    result = pd.DataFrame(rows, index=pd.DatetimeIndex(indices, tz=UTC))
    result.index.name = "timestamp"
    return result.sort_index()


def _quality_report(
    underlying: pd.DataFrame,
    option_frames: Mapping[str, pd.DataFrame],
    contracts: Mapping[str, ReplayContract],
    interval_minutes: int,
) -> dict[str, Any]:
    gaps: list[dict[str, Any]] = []
    if len(underlying) > 1:
        local = underlying.index.tz_convert(NSE_TIMEZONE)
        for previous, current in zip(local[:-1], local[1:]):
            if previous.date() == current.date():
                elapsed = int((current - previous).total_seconds() // 60)
                if elapsed > interval_minutes:
                    gaps.append(
                        {
                            "from": (previous + pd.Timedelta(minutes=interval_minutes)).isoformat(),
                            "to": current.isoformat(),
                            "missing_intervals": max(0, elapsed // interval_minutes - 1),
                        }
                    )
    option_rows = sum(len(frame) for frame in option_frames.values())
    bid_ask_rows = 0
    if option_rows:
        bid_ask_rows = sum(
            int((frame["bid"].notna() & frame["ask"].notna()).sum())
            for frame in option_frames.values()
            if "bid" in frame and "ask" in frame
        )
    return {
        "underlying_bars": int(len(underlying)),
        "underlying_first_open": (underlying.index[0].isoformat() if len(underlying) else None),
        "underlying_last_open": (underlying.index[-1].isoformat() if len(underlying) else None),
        "underlying_gaps": gaps,
        "option_contracts": int(len(contracts)),
        "option_quote_rows": int(option_rows),
        "option_bid_ask_rows": int(bid_ask_rows),
        "option_bid_ask_coverage": (round(bid_ask_rows / option_rows, 6) if option_rows else 0.0),
        "timeframe": f"{interval_minutes}min",
        "session_timezone": "Asia/Kolkata",
        "timestamp_convention": "candle_open",
    }


def _interval_minutes(value: str) -> int:
    try:
        return SUPPORTED_INTERVALS[str(value).strip().lower()]
    except KeyError as exc:
        raise ReplayDataError(
            f"replay timeframe must be one of {tuple(SUPPORTED_INTERVALS)}, got {value!r}"
        ) from exc


def _coerce_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except (TypeError, ValueError) as exc:
        raise ReplayDataError(f"invalid option expiry date {value!r}") from exc


def _single_values(frame: pd.DataFrame, names: tuple[str, ...]) -> Any:
    column = next((name for name in names if name in frame.columns), None)
    if column is None:
        return None
    values = frame[column].dropna().astype(str).str.strip().unique()
    if len(values) > 1:
        raise ReplayDataError(f"contract column {column!r} changes inside one CSV")
    return values[0] if len(values) else None


def _first_value(frame: pd.DataFrame, names: tuple[str, ...]) -> Any:
    column = next((name for name in names if name in frame.columns), None)
    if column is None:
        return None
    values = frame[column].dropna()
    return values.iloc[0] if len(values) else None


def _stable_file_label(path: Path) -> str:
    """Fingerprint label excludes machine-specific absolute directory paths."""
    return path.name


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _frame_fingerprint(frame: pd.DataFrame) -> str:
    digest = hashlib.sha256()
    digest.update("|".join(map(str, frame.columns)).encode())
    digest.update(pd.util.hash_pandas_object(frame, index=True).values.tobytes())
    return digest.hexdigest()


__all__ = ["ReplayContract", "ReplayDataError", "ReplayDataset", "SUPPORTED_INTERVALS"]
