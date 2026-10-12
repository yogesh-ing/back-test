from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Protocol, runtime_checkable

import pandas as pd

CANDLE_COLUMNS = ["open", "high", "low", "close", "volume"]

#: The ONE canonical timeframe vocabulary (ticket P4.3). Every layer — API,
#: config, DB (``market_data_cache.timeframe`` CHECK), UI, feeds — speaks
#: these names. Resolved with the lead as the descriptive set.
CANONICAL_TIMEFRAMES = (
    "1min", "5min", "10min", "15min", "30min", "1hour", "4hour", "1day", "1week"
)

#: Canonical timeframe -> mStock TypeA wire interval. Broker-specific
#: translation only; the rest of the codebase never speaks these names.
#: ``4hour`` has no mStock historical equivalent (intentionally absent).
MSTOCK_INTERVAL_MAP = {
    "1min": "minute",
    "5min": "5minute",
    "15min": "15minute",
    "1hour": "60minute",
    "1day": "day",
    "1week": "week",
}

#: NSE equity session length in minutes (09:15–15:30).
TRADING_MINUTES_PER_DAY = 375

#: The wall clock NSE/BSE bars are stamped on. A broker payload can arrive as a
#: naive session time, so anything writing a ``timestamptz`` column has to
#: declare this zone instead of letting the server pick one.
EXCHANGE_TZ = "Asia/Kolkata"

#: Trading days per year (NSE). The annualisation base for every daily-derived
#: number: Sharpe, CAGR, Sortino, volatility.
TRADING_DAYS_PER_YEAR = 252

#: Canonical timeframe -> bars in one NSE trading day. Weekly is the
#: exception: a week is not a fifth of a 252-day year, it is 52 of them.
BARS_PER_TRADING_DAY: dict[str, float] = {
    "1min": TRADING_MINUTES_PER_DAY,  # 375
    "5min": 75,
    "10min": 37,  # not in CANONICAL_TIMEFRAMES, but brokers ask for it
    "15min": 25,
    "30min": 12,  # idem
    "1hour": 6,
    "4hour": 2,
    "1day": 1,
    "1week": 1,
}

#: Aliases accepted from the UI and from broker payloads. Same spirit as
#: ``resolve_interval`` but a pure map, so the engine can ask for a period
#: count without pulling in the whole backtest module.
_TIMEFRAME_ALIASES: dict[str, str] = {
    "1m": "1min",
    "m1": "1min",
    "1minute": "1min",
    "minute": "1min",
    "5m": "5min",
    "m5": "5min",
    "5minute": "5min",
    "10m": "10min",
    "m10": "10min",
    "10minute": "10min",
    "15m": "15min",
    "m15": "15min",
    "15minute": "15min",
    "30m": "30min",
    "m30": "30min",
    "30minute": "30min",
    "1hour": "1hour",
    "h1": "1hour",
    "60min": "1hour",
    "60minute": "1hour",
    "hour": "1hour",
    "4h": "4hour",
    "h4": "4hour",
    "240min": "4hour",
    "4hour": "4hour",
    "1day": "1day",
    "1d": "1day",
    "day": "1day",
    "daily": "1day",
    "1daily": "1day",
    "1h": "1hour",
    "h1": "1hour",
    "60min": "1hour",
    "60minute": "1hour",
    "hour": "1hour",
    "4h": "4hour",
    "h4": "4hour",
    "240min": "4hour",
    "4hour": "4hour",
    "d": "1day",
    "1d": "1day",
    "day": "1day",
    "daily": "1day",
    "1daily": "1day",
    "1week": "1week",
    "1w": "1week",
    "week": "1week",
    "weekly": "1week",
}

#: Canonical timeframe -> minutes of MARKET time in one bar. ``1day`` is one NSE
#: session (375 minutes), not 1440, and ``1week`` is five sessions (1875). Same
#: convention as :data:`BARS_PER_TRADING_DAY` — and it is what turns "can daily
#: bars be built from 1-minute bars?" into a division instead of a guess.
TIMEFRAME_MINUTES: dict[str, int] = {
    "1min": 1,
    "5min": 5,
    "10min": 10,
    "15min": 15,
    "30min": 30,
    "1hour": 60,
    "4hour": 240,
    "1day": TRADING_MINUTES_PER_DAY,  # 375
    "1week": TRADING_MINUTES_PER_DAY * 5,  # 1875
}

#: Weeks per year — used for every ``1week`` annualisation.
WEEKS_PER_YEAR = 52


def normalize_timeframe(timeframe: str | None) -> str | None:
    """Map any accepted spelling onto the canonical name (``None`` if unknown).

    Case-insensitive and alias-tolerant, so the engine can be handed ``"1D"``,
    ``"day"`` or ``"1day"`` from three different layers and get one answer.
    """
    key = str(timeframe or "").strip().lower()
    if not key:
        return None
    if key in CANONICAL_TIMEFRAMES or key in BARS_PER_TRADING_DAY:
        return key
    return _TIMEFRAME_ALIASES.get(key)


def canonical_rank(timeframe: str | None) -> int | None:
    """Position of a timeframe in :data:`CANONICAL_TIMEFRAMES`, finest first.

    ``None`` when the name is not a canonical timeframe. Rank arithmetic is how
    the rest of this module asks "is this coarser than that?" without a second
    hand-maintained ordering that can drift out of step with the vocabulary.
    """
    key = normalize_timeframe(timeframe)
    if key is None:
        return None
    try:
        return CANONICAL_TIMEFRAMES.index(key)
    except ValueError:  # a BARS_PER_TRADING_DAY-only name (no canonical entry)
        return None


def finest_timeframe(stored: Iterable[str] | None) -> str | None:
    """The finest canonical timeframe in a stored set (``None`` when empty)."""
    ranks = [r for r in (canonical_rank(tf) for tf in (stored or [])) if r is not None]
    return CANONICAL_TIMEFRAMES[min(ranks)] if ranks else None


def derive_serviceable_timeframes(
    stored: Iterable[str] | None,
    *,
    finest_bars: int | None = None,
    min_bars: int = 2,
) -> list[str]:
    """Every canonical timeframe a stored set can actually serve, finest first.

    Resampling only goes one way: fine bars aggregate up into coarse ones and
    never the reverse. So a symbol stored at ``1min`` can answer a ``1day``
    request — :class:`~backtest.data.db_source.DbSource` resamples it — while a
    symbol stored at ``1day`` can never answer ``1min``, because that would be
    inventing prices rather than aggregating them.

    ``finest_bars`` (rows held at the finest stored granularity) caps the reach
    upward: a week of 1-minute bars cannot become a year of weekly bars, and
    advertising ``1week`` for a range that yields a single bar only sends the
    user into a run that must fail. Omit it to apply no cap.

    Stored timeframes are returned unconditionally — they are physically there,
    so a picker must be able to choose them no matter what the cap says. Only
    the *derived* entries are filtered.

    This is computed rather than persisted on purpose: it repairs instruments
    that were downloaded before the rule existed, with no re-fetch and no
    backfill migration.
    """
    stored_canonical = {tf for tf in (normalize_timeframe(t) for t in (stored or [])) if tf}
    if not stored_canonical:
        return []

    finest = min(canonical_rank(tf) for tf in stored_canonical)  # type: ignore[type-var]
    finest_minutes = TIMEFRAME_MINUTES.get(CANONICAL_TIMEFRAMES[finest], 0)

    out: list[str] = []
    for rank in range(finest, len(CANONICAL_TIMEFRAMES)):
        candidate = CANONICAL_TIMEFRAMES[rank]
        if candidate in stored_canonical:
            out.append(candidate)
            continue
        if finest_bars is not None and finest_minutes:
            step = TIMEFRAME_MINUTES.get(candidate, 0)
            if step > finest_minutes and (finest_bars * finest_minutes) / step < min_bars:
                continue  # not enough base bars to fill even one window
        out.append(candidate)
    return out


def bar_timestamp(value) -> datetime:
    """Resolve one bar's timestamp to an **aware UTC** datetime.

    ``market_data_cache.ts`` is ``timestamptz``. Binding a naive value hands
    the instant to PostgreSQL, which reads it in the *session* timezone — and
    the historical default here is ``Asia/Calcutta``, not UTC. That shift put
    every fetched intraday bar 5h30 early and split a single NSE session over
    two UTC calendar days, which is what broke daily rollups.

    A naive stamp is taken as the exchange wall clock (what the brokers send);
    an offset-bearing stamp keeps the instant it already declares.
    """
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize(EXCHANGE_TZ)
    return ts.tz_convert("UTC").to_pydatetime()


def periods_per_year(timeframe: str | None, default: int = TRADING_DAYS_PER_YEAR) -> int:
    """Periods in a year at this granularity — the annualisation factor.

    ``periods_per_year`` is what turns a per-bar mean and a per-bar standard
    deviation into an annual Sharpe and an annual CAGR. Scoring 1-minute bars
    with the daily factor of 252 understates volatility by ~15x and reports a
    Sharpe that is confidently wrong (PRD backTest-enhance §1.4).

    Unknown or missing timeframes fall back to the daily factor rather than
    raising: a caller that never said must not crash a run, and daily is the
    one assumption that is right for the overwhelmingly common case.
    """
    key = normalize_timeframe(timeframe)
    if key is None:
        return default
    if key == "1week":
        return WEEKS_PER_YEAR
    per_day = BARS_PER_TRADING_DAY.get(key)
    if per_day is None:
        return default
    return max(1, round(TRADING_DAYS_PER_YEAR * per_day))


@runtime_checkable
class DataSource(Protocol):
    def get_candles(
        self, symbol: str, start: str, end: str, interval: str = "1day"
    ) -> pd.DataFrame: ...


def normalize_candles(df: pd.DataFrame) -> pd.DataFrame:
    if df is None:
        raise ValueError("candles frame is required")

    out = df.copy()
    out.columns = [str(c).strip().lower() for c in out.columns]

    missing = [c for c in CANDLE_COLUMNS if c not in out.columns]
    if missing:
        raise ValueError(f"missing required columns: {missing}")

    if out.empty:
        raise ValueError("candles frame is empty")

    if not isinstance(out.index, pd.DatetimeIndex):
        raise ValueError("candles index must be DatetimeIndex")

    out = out.loc[:, CANDLE_COLUMNS]
    out = out[~out.index.duplicated(keep="last")]
    out = out.sort_index()
    out = out.dropna(subset=["close"])

    if out.empty:
        raise ValueError("candles frame is empty after cleaning")

    for col in CANDLE_COLUMNS:
        out[col] = pd.to_numeric(out[col], errors="raise")

    return out
