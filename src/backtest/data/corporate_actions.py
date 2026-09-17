"""Corporate-action adjustment policy + daily-bar quality gate (review §3.3, 12c).

The landmine: mStock TypeA history is RAW. One unadjusted 10:1 split produces
a −90% "drawdown", poisons every metric computed over it, and any walk-forward
split that crosses the ex-date is trained on fiction. This module is the
project's single answer:

* **The store** — :class:`CorporateAction` records (split / bonus / dividend)
  loaded from a CSV, from rows, or from the ``corporate_actions`` DB table.
* **The policy** — back-adjust at READ time, never in storage. Raw bars stay
  raw in ``market_data_cache`` / CSVs; :class:`AdjustedSource` scales history
  (OHLC × factor, volume ÷ factor) so the latest bars keep their traded
  prices. No path enabled → byte-identical old behaviour.
* **The gate** — :func:`daily_return_outliers` flags |1-day close-to-close
  returns| beyond ``split_suspect_return_pct`` (default ±40% — NSE bands make
  anything larger a corporate action until proven otherwise) as SUSPECTED
  unadjusted actions for manual confirm.

Factor convention (documented once, everywhere the same):

* ``adjustment_factor`` multiplies prices BEFORE the ex-date; bars on/after
  the ex-date are untouched (standard back-adjustment).
* Split face ``old → new``: factor = ``new / old`` (10 → 1 ⇒ 0.10).
* Bonus ``a`` for every ``b`` held: factor = ``b / (a + b)`` (1:1 ⇒ 0.50).
* Volume divides by the same factor (10:1 split: volume ×10).
* Dividends are RECORDED but price-neutral (factor 1.0) unless an operator
  supplies an explicit total-return factor — the project does not claim
  total-return series.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Protocol

import pandas as pd

from backtest.logging_config import get_logger

log = get_logger(__name__)

#: Action vocabulary — mirrored by the ``corporate_actions.kind`` CHECK.
ACTION_KINDS = ("split", "bonus", "dividend")

#: Default config anchor — same convention as the live validator.
DEFAULT_POLICY_PATH = Path(__file__).resolve().parents[3] / "config" / "data_quality.yaml"

#: Default suspect threshold (review §3.3: flag |return| > ±40%).
DEFAULT_SPLIT_SUSPECT_RETURN_PCT = 40.0

#: A suspect whose date sits within ±this many days of a known action is
#: "explained" (info log) rather than warned about.
_KNOWN_ACTION_WINDOW_DAYS = 7


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CorporateAction:
    """One corporate action. ``adjustment_factor`` scales pre-ex-date prices."""

    symbol: str
    ex_date: date
    kind: str
    adjustment_factor: float = 1.0
    #: Dividend rupees per share — informational (policy: no auto adjustment).
    amount: Optional[float] = None
    note: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", str(self.symbol).strip().upper())
        if not self.symbol:
            raise ValueError("corporate action needs a symbol")
        if isinstance(self.ex_date, datetime):
            object.__setattr__(self, "ex_date", self.ex_date.date())
        if self.kind not in ACTION_KINDS:
            raise ValueError(f"kind must be one of {ACTION_KINDS}, got {self.kind!r}")
        if float(self.adjustment_factor) <= 0:
            raise ValueError(f"adjustment_factor must be > 0, got {self.adjustment_factor}")
        object.__setattr__(self, "adjustment_factor", float(self.adjustment_factor))


def split_action(
    symbol: str, ex_date: date, old_face: float, new_face: float, note: str = ""
) -> CorporateAction:
    """Face-value split (₹10 → ₹1 ⇒ factor 0.10; ₹10 → ₹5 ⇒ factor 0.50)."""
    old_face, new_face = float(old_face), float(new_face)
    if old_face <= 0 or new_face <= 0:
        raise ValueError("face values must be positive")
    return CorporateAction(symbol, ex_date, "split", new_face / old_face, None, note)


def bonus_action(
    symbol: str, ex_date: date, bonus_shares: float, held_shares: float, note: str = ""
) -> CorporateAction:
    """``bonus_shares`` for every ``held_shares`` held (1:1 ⇒ factor 0.50)."""
    bonus_shares, held_shares = float(bonus_shares), float(held_shares)
    if bonus_shares <= 0 or held_shares <= 0:
        raise ValueError("bonus/held share counts must be positive")
    factor = held_shares / (bonus_shares + held_shares)
    return CorporateAction(symbol, ex_date, "bonus", factor, None, note)


def dividend_action(
    symbol: str, ex_date: date, amount: float, factor: float = 1.0, note: str = ""
) -> CorporateAction:
    """Record a dividend. Price-neutral (factor 1.0) unless ``factor`` given."""
    if float(amount) <= 0:
        raise ValueError("dividend amount must be positive")
    return CorporateAction(symbol, ex_date, "dividend", factor, float(amount), note)


# ---------------------------------------------------------------------------
# The gate — pure ±threshold rule, no calendar needed
# ---------------------------------------------------------------------------


def daily_return_outliers(
    df: pd.DataFrame, threshold_pct: float = DEFAULT_SPLIT_SUSPECT_RETURN_PCT
) -> List[Dict[str, Any]]:
    """1-day close-to-close moves beyond ``threshold_pct`` (suspected splits).

    Returns records: ``ts, prev_close, close, return_pct, implied_factor``
    (``close / prev_close`` — the factor an unadjusted split would imply).
    """
    if df is None or len(df) < 2 or "close" not in df.columns:
        return []
    close = df["close"].astype(float)
    prev = close.shift(1)
    ret_pct = (close / prev - 1.0) * 100.0
    mask = ret_pct.abs() > float(threshold_pct)
    out: List[Dict[str, Any]] = []
    for ts in df.index[mask]:
        pos = df.index.get_loc(ts)
        prev_close = float(close.iloc[pos - 1])
        cur_close = float(close.iloc[pos])
        out.append(
            {
                "ts": ts,
                "prev_close": prev_close,
                "close": cur_close,
                "return_pct": float(ret_pct.loc[ts]),
                "implied_factor": cur_close / prev_close if prev_close else None,
            }
        )
    return out


# ---------------------------------------------------------------------------
# The calendar
# ---------------------------------------------------------------------------


class CorporateActionCalendar:
    """Symbol → chronologically sorted actions; applies read-time adjustments."""

    def __init__(self) -> None:
        self._actions: Dict[str, List[CorporateAction]] = {}

    # -- population ---------------------------------------------------------

    def add(self, action: CorporateAction) -> "CorporateActionCalendar":
        bucket = self._actions.setdefault(action.symbol, [])
        if any(a.ex_date == action.ex_date and a.kind == action.kind for a in bucket):
            raise ValueError(
                f"{action.symbol}: duplicate {action.kind} on {action.ex_date}"
            )
        bucket.append(action)
        bucket.sort(key=lambda a: a.ex_date)
        return self

    def add_all(self, actions: Iterable[CorporateAction]) -> "CorporateActionCalendar":
        for action in actions:
            self.add(action)
        return self

    def update(self, other: "CorporateActionCalendar") -> "CorporateActionCalendar":
        """Merge every action of ``other`` (exact duplicates raise, as ``add``)."""
        for symbol in other.symbols():
            for action in other.actions_for(symbol):
                self.add(action)
        return self

    # -- queries ------------------------------------------------------------

    def actions_for(self, symbol: str) -> List[CorporateAction]:
        return list(self._actions.get(str(symbol).strip().upper(), []))

    def symbols(self) -> List[str]:
        return sorted(self._actions)

    def __len__(self) -> int:
        return sum(len(v) for v in self._actions.values())

    def __bool__(self) -> bool:  # empty calendar is falsy → wrappers no-op
        return len(self) > 0

    def factor_for(self, symbol: str, ts: Any) -> float:
        """Cumulative pre-ex-date factor for a bar timestamp (1.0 = untouched)."""
        bar_day = ts.date() if isinstance(ts, datetime) else ts
        factor = 1.0
        for action in self.actions_for(symbol):
            if action.ex_date > bar_day:
                factor *= action.adjustment_factor
        return factor

    # -- the adjustment -----------------------------------------------------

    def adjust_candles(self, df: pd.DataFrame, symbol: str) -> pd.DataFrame:
        """Back-adjust a COPY: OHLC × cumulative factor, volume ÷ factor.

        Bars on/after the last ex-date stay exactly as traded. With no
        actions for ``symbol`` the input frame is returned untouched
        (same object — guaranteed zero-cost zero-change path).
        """
        actions = self.actions_for(symbol)
        if not actions:
            return df

        index = df.index
        if isinstance(index, pd.DatetimeIndex) and index.tz is not None:
            # tz-aware bars: compare in the bars' own zone (ex-date midnight).
            cutoffs = [pd.Timestamp(a.ex_date, tz=index.tz) for a in actions]
        else:
            cutoffs = [pd.Timestamp(a.ex_date) for a in actions]

        factors = pd.Series(1.0, index=index)
        for action, cutoff in zip(actions, cutoffs):
            factors[index < cutoff] *= action.adjustment_factor

        out = df.copy()
        for col in ("open", "high", "low", "close"):
            if col in out.columns:
                out[col] = out[col].astype(float) * factors
        if "volume" in out.columns:
            out["volume"] = out["volume"].astype(float) / factors
        return out

    def suspects(
        self,
        df: pd.DataFrame,
        symbol: str,
        threshold_pct: float = DEFAULT_SPLIT_SUSPECT_RETURN_PCT,
    ) -> List[Dict[str, Any]]:
        """Outliers annotated with whether the calendar already explains them."""
        known = self.actions_for(symbol)
        records = daily_return_outliers(df, threshold_pct)
        for rec in records:
            rec_day = (
                rec["ts"].date() if isinstance(rec["ts"], datetime) else rec["ts"]
            )
            rec["known_action"] = any(
                abs((rec_day - a.ex_date).days) <= _KNOWN_ACTION_WINDOW_DAYS for a in known
            )
            nearby = [
                a for a in known
                if abs((rec_day - a.ex_date).days) <= _KNOWN_ACTION_WINDOW_DAYS
            ]
            rec["nearby_action"] = (
                f"{nearby[0].kind} factor={nearby[0].adjustment_factor:g} "
                f"ex={nearby[0].ex_date.isoformat()}"
                if nearby
                else None
            )
        return records


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

_CSV_FIELDS = ("symbol", "ex_date", "kind", "adjustment_factor", "amount", "note")


def calendar_from_rows(rows: Iterable[Dict[str, Any]]) -> CorporateActionCalendar:
    """Rows with ``symbol, ex_date, kind, adjustment_factor[, amount, note]``."""
    calendar = CorporateActionCalendar()
    for i, raw in enumerate(rows):
        try:
            ex_date = raw["ex_date"]
            if isinstance(ex_date, str):
                ex_date = date.fromisoformat(ex_date)
            calendar.add(
                CorporateAction(
                    symbol=str(raw["symbol"]),
                    ex_date=ex_date,
                    kind=str(raw["kind"]).strip().lower(),
                    adjustment_factor=float(raw.get("adjustment_factor", 1.0) or 1.0),
                    amount=(float(raw["amount"]) if raw.get("amount") not in (None, "") else None),
                    note=str(raw.get("note") or ""),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"corporate-actions row {i} invalid: {raw!r} ({exc})") from exc
    return calendar


def calendar_from_csv(path: str | Path) -> CorporateActionCalendar:
    """CSV with header ``symbol,ex_date,kind,adjustment_factor[,amount,note]``.

    ``ex_date`` ISO (YYYY-MM-DD); ``adjustment_factor`` is the pre-ex-date
    price multiplier (see module docstring for split/bonus arithmetic).
    """
    with open(path, newline="", encoding="utf-8") as fh:
        rows = [
            {
                k: (v.strip() if isinstance(v, str) else v)
                for k, v in row.items()
                if k in _CSV_FIELDS
            }
            for row in csv.DictReader(fh)
        ]
    return calendar_from_rows(rows)


def calendar_from_db(manager: Any) -> CorporateActionCalendar:
    """Load every action from the ``corporate_actions`` table (ops upkeep path)."""
    from backtest.db.models import CorporateActionRecord

    CorporateActionRecord.ensure_schema(manager)
    with manager.session() as session:
        rows = session.query(CorporateActionRecord).order_by(
            CorporateActionRecord.symbol, CorporateActionRecord.ex_date
        ).all()
        return calendar_from_rows(
            [
                {
                    "symbol": r.symbol,
                    "ex_date": r.ex_date,
                    "kind": r.kind,
                    "adjustment_factor": float(r.factor),
                    "amount": float(r.amount) if r.amount is not None else None,
                    "note": r.note or "",
                }
                for r in rows
            ]
        )


def calendar_from_config(path: str | Path | None = None) -> Optional["CorporateActionCalendar"]:
    """Policy-driven calendar: ``daily_bar.corporate_actions`` in data_quality.yaml.

    Returns ``None`` when disabled / unconfigured / nothing loadable — callers
    treat that as "policy off, behaviour unchanged". Never raises: source
    construction must not break because of a policy file.
    """
    config_path = Path(path) if path else DEFAULT_POLICY_PATH
    try:
        import yaml

        doc = yaml.safe_load(config_path.read_text()) or {}
    except FileNotFoundError:
        return None
    except Exception:  # noqa: BLE001 — a broken policy file must not block runs
        log.warning("[corporate-actions] unreadable policy file %s — policy off", config_path)
        return None

    section = (doc.get("daily_bar") or {}).get("corporate_actions") or {}
    if not bool(section.get("enabled", False)):
        return None

    calendar = CorporateActionCalendar()
    csv_path = section.get("actions_csv")
    if csv_path:
        try:
            calendar.update(calendar_from_csv(csv_path))
        except FileNotFoundError:
            log.warning("[corporate-actions] enabled but no actions file at %s", csv_path)
        except ValueError as exc:
            log.error("[corporate-actions] %s has a bad row: %s", csv_path, exc)

    inline_rows = section.get("actions", []) or []
    if inline_rows:
        try:
            calendar.update(calendar_from_rows(inline_rows))
        except ValueError as exc:
            log.error("[corporate-actions] inline action invalid: %s", exc)

    return calendar if calendar else None


# ---------------------------------------------------------------------------
# The read-time wrapper — policy application point
# ---------------------------------------------------------------------------


class _InnerSource(Protocol):
    def get_candles(
        self, symbol: str, start: str, end: str, interval: str = "1day"
    ) -> pd.DataFrame: ...


class AdjustedSource:
    """DataSource decorator applying the corporate-action policy on read.

    * Bars are back-adjusted via the calendar (storage stays raw — the
      wrapper never writes back, so applying the policy is idempotent at
      the architecture level: every read starts from raw bars).
    * The RAW frame is scanned for split-suspect returns: an outlier near a
      known action logs at INFO ("adjusted"), an unexplained one logs a
      WARNING naming the implied factor — the manual-confirm loop from
      review §3.3.
    * An empty calendar means the wrapper is a transparent pass-through.
    """

    def __init__(
        self,
        inner: _InnerSource,
        calendar: CorporateActionCalendar,
        threshold_pct: float = DEFAULT_SPLIT_SUSPECT_RETURN_PCT,
    ) -> None:
        self._inner = inner
        self.calendar = calendar
        self.threshold_pct = float(threshold_pct)

    def get_candles(
        self, symbol: str, start: str, end: str, interval: str = "1day"
    ) -> pd.DataFrame:
        raw = self._inner.get_candles(symbol, start, end, interval)
        actions = self.calendar.actions_for(symbol)
        adjusted = self.calendar.adjust_candles(raw, symbol)

        suspects = self.calendar.suspects(raw, symbol, self.threshold_pct)
        for rec in suspects:
            if rec["known_action"]:
                log.info(
                    "[corporate-actions] %s %s move %+.1f%% matches %s — adjusted",
                    symbol, rec["ts"].date(), rec["return_pct"], rec["nearby_action"],
                )
            else:
                log.warning(
                    "[corporate-actions] %s %s move %+.1f%% (implied factor %.4g) has "
                    "NO known corporate action within %d days — suspected unadjusted "
                    "split/bonus, add it and re-run (raw bars are untouched in storage)",
                    symbol, rec["ts"].date(), rec["return_pct"], rec["implied_factor"] or 0,
                    _KNOWN_ACTION_WINDOW_DAYS,
                )
        if actions and adjusted is not raw:
            log.debug(
                "[corporate-actions] %s: %d action(s) applied, latest factor %.4g",
                symbol, len(actions), self.calendar.factor_for(symbol, raw.index[-1]),
            )
        return adjusted

    def __repr__(self) -> str:  # pragma: no cover
        return f"<AdjustedSource {self._inner!r} actions={len(self.calendar)}>"
