"""Data quality validation for live market data (Step 11).

The Step 10 normalizer already rejects *structurally* broken payloads (no
price, crossed quote, negative volume). This layer catches data that is
well-formed but **wrong**: a fat-finger spike 40 standard deviations off the
rolling mean, a feed that silently stalled for ten minutes, a volume print
5x the recent average, bars whose high is below their low.

Severity model
--------------
Every check produces issues with a severity that depends on the configured
strictness (``lenient`` / ``normal`` / ``strict``):

* **ERROR** — the datum is not trusted. It is rejected, or repaired when
  ``on_bad_data: repair`` and the issue is price-level (interpolation
  substitutes the previous observed price).
* **WARNING / INFO** — recorded and counted, datum accepted.

Rejected data never pollutes the rolling statistics — otherwise one spike
would widen the standard deviation enough to hide the next one.

Regime changes vs. spikes
-------------------------
A genuine gap-up looks exactly like a spike, but *keeps* looking like one.
After ``alert_threshold`` consecutive rejections for a symbol the validator
fires its alert callbacks and resets that symbol's statistical window, so a
real regime change self-heals instead of rejecting data forever.
"""

from __future__ import annotations

import logging
import statistics
from collections import Counter, deque
from dataclasses import dataclass, field, replace as dc_replace
from datetime import datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping

from backtest.marketdata.bars import INTRADAY_MINUTES
from backtest.marketdata.errors import MarketDataError
from backtest.marketdata.ticks import Bar, Tick
from backtest.simulator.money import to_decimal

logger = logging.getLogger("backtest.marketdata.quality")

__all__ = [
    "Severity",
    "Strictness",
    "BadDataPolicy",
    "ValidationIssue",
    "ValidationResult",
    "QualityConfig",
    "DataValidator",
    "load_quality_config",
    "DEFAULT_QUALITY_CONFIG_PATH",
]

DEFAULT_QUALITY_CONFIG_PATH = (
    Path(__file__).resolve().parents[3] / "config" / "quality.yaml"
)


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"

    def __str__(self) -> str:  # pragma: no cover - convenience
        return self.value


class Strictness(str, Enum):
    LENIENT = "lenient"
    NORMAL = "normal"
    STRICT = "strict"


class BadDataPolicy(str, Enum):
    REJECT = "reject"
    REPAIR = "repair"


#: check code → (lenient, normal, strict) severity.
#: Hard structural violations are ERROR at every level — no strictness
#: setting should ever accept a negative price or an impossible bar.
_SEVERITY_TABLE: dict[str, tuple[Severity, Severity, Severity]] = {
    "non_positive_price": (Severity.ERROR, Severity.ERROR, Severity.ERROR),
    "price_out_of_range": (Severity.WARNING, Severity.ERROR, Severity.ERROR),
    "crossed_quote": (Severity.ERROR, Severity.ERROR, Severity.ERROR),
    "negative_volume": (Severity.ERROR, Severity.ERROR, Severity.ERROR),
    "ohlc_inconsistent": (Severity.ERROR, Severity.ERROR, Severity.ERROR),
    "price_spike": (Severity.WARNING, Severity.ERROR, Severity.ERROR),
    "last_outside_spread": (Severity.INFO, Severity.WARNING, Severity.ERROR),
    "out_of_order": (Severity.WARNING, Severity.WARNING, Severity.ERROR),
    "data_gap": (Severity.INFO, Severity.WARNING, Severity.ERROR),
    "volume_anomaly": (Severity.INFO, Severity.WARNING, Severity.ERROR),
    "source_divergence": (Severity.WARNING, Severity.WARNING, Severity.ERROR),
}

_STRICTNESS_INDEX = {Strictness.LENIENT: 0, Strictness.NORMAL: 1, Strictness.STRICT: 2}

#: Issues that interpolation can repair — all price-level. A broken bar or a
#: negative volume has no single obvious substitute, so those only reject.
_REPAIRABLE = {"price_spike", "non_positive_price", "price_out_of_range"}


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """One problem found in one datum. Detailed by design — a bare 'invalid'
    tells the 2 a.m. operator nothing."""

    code: str
    severity: Severity
    message: str
    field: str | None = None
    details: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "message": self.message,
            "field": self.field,
            "details": dict(self.details) if self.details else {},
        }


class Action(str, Enum):
    ACCEPTED = "accepted"
    REPAIRED = "repaired"
    REJECTED = "rejected"


@dataclass(slots=True)
class ValidationResult:
    """Outcome of validating one tick or bar."""

    action: Action
    issues: list[ValidationIssue] = field(default_factory=list)
    #: The datum to use downstream — the original, or the repaired copy.
    tick: Tick | None = None
    bar: Bar | None = None

    @property
    def ok(self) -> bool:
        """True when the datum is usable (accepted or repaired)."""
        return self.action is not Action.REJECTED

    @property
    def rejected(self) -> bool:
        return self.action is Action.REJECTED

    @property
    def repaired(self) -> bool:
        return self.action is Action.REPAIRED

    @property
    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity is Severity.ERROR]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity is Severity.WARNING]

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "issues": [issue.to_dict() for issue in self.issues],
        }


@dataclass(frozen=True)
class QualityConfig:
    """Validation rules. YAML-loadable via :func:`load_quality_config`."""

    strictness: Strictness = Strictness.NORMAL
    on_bad_data: BadDataPolicy = BadDataPolicy.REJECT

    #: Absolute price sanity range (NSE equities: paise to lakhs).
    min_price: Decimal = Decimal("0.01")
    max_price: Decimal = Decimal("10000000")

    #: Spike detection: |z-score| of a price against the rolling window.
    spike_zscore_threshold: float = 3.0
    spike_window: int = 50
    spike_min_samples: int = 10

    #: Feed-stall detection between consecutive ticks of one symbol.
    max_gap_seconds: int = 300

    #: Volume anomaly: multiple of the rolling average volume.
    volume_anomaly_multiple: float = 5.0
    volume_window: int = 50
    volume_min_samples: int = 5

    #: Consecutive rejections per symbol before alert + window reset.
    alert_threshold: int = 5

    #: Cross-source comparison tolerance, in percent of price.
    source_divergence_pct: float = 0.5

    def __post_init__(self) -> None:
        object.__setattr__(self, "strictness", Strictness(self.strictness))
        object.__setattr__(self, "on_bad_data", BadDataPolicy(self.on_bad_data))
        object.__setattr__(self, "min_price", to_decimal(self.min_price, "min_price"))
        object.__setattr__(self, "max_price", to_decimal(self.max_price, "max_price"))
        if self.min_price <= 0:
            raise ValueError("min_price must be positive")
        if self.max_price <= self.min_price:
            raise ValueError("max_price must exceed min_price")
        if self.spike_zscore_threshold <= 0:
            raise ValueError("spike_zscore_threshold must be positive")
        if self.spike_window < 2:
            raise ValueError("spike_window must be >= 2")
        if not 2 <= self.spike_min_samples <= self.spike_window:
            raise ValueError("spike_min_samples must be in [2, spike_window]")
        if self.max_gap_seconds < 1:
            raise ValueError("max_gap_seconds must be >= 1")
        if self.volume_anomaly_multiple <= 1:
            raise ValueError("volume_anomaly_multiple must be > 1")
        if self.volume_window < 1:
            raise ValueError("volume_window must be >= 1")
        if not 1 <= self.volume_min_samples <= self.volume_window:
            raise ValueError("volume_min_samples must be in [1, volume_window]")
        if self.alert_threshold < 1:
            raise ValueError("alert_threshold must be >= 1")
        if self.source_divergence_pct <= 0:
            raise ValueError("source_divergence_pct must be positive")


def load_quality_config(
    path: str | Path | None = None,
    profile: str | None = None,
) -> QualityConfig:
    """Load :class:`QualityConfig` from YAML (``config/quality.yaml``).

    Same layout as every other simulator config: ``default`` section plus
    named ``profiles``; ``active_profile`` picks one when ``profile`` is
    not given.
    """
    import yaml

    config_path = Path(path) if path is not None else DEFAULT_QUALITY_CONFIG_PATH
    with open(config_path, "r", encoding="utf-8") as fh:
        document = yaml.safe_load(fh) or {}

    settings: dict[str, Any] = dict(document.get("default") or {})
    profiles: Mapping[str, Any] = document.get("profiles") or {}
    chosen = profile or document.get("active_profile")
    if chosen:
        if chosen not in profiles:
            raise ValueError(
                f"unknown quality profile {chosen!r}; available: {sorted(profiles)}"
            )
        settings.update(profiles[chosen] or {})

    valid = {f.name for f in QualityConfig.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    unknown = set(settings) - valid
    if unknown:
        raise ValueError(f"unknown quality config keys: {sorted(unknown)}")
    return QualityConfig(**settings)


class _SymbolState:
    """Rolling statistics for one symbol. Only *accepted* data enters."""

    __slots__ = ("prices", "volumes", "last_ts", "consecutive_rejects", "last_bar_ts")

    def __init__(self, price_window: int, volume_window: int) -> None:
        self.prices: deque[Decimal] = deque(maxlen=price_window)
        self.volumes: deque[int] = deque(maxlen=volume_window)
        self.last_ts: datetime | None = None
        self.consecutive_rejects = 0
        #: last bar open time per timeframe (bar chronology is per timeframe).
        self.last_bar_ts: dict[str, datetime] = {}


class DataValidator:
    """Statistical and semantic quality gate for ticks and bars.

    Stateless checks (:meth:`validate_ohlc_relationship`,
    :meth:`check_for_gaps`) are usable standalone; the stateful ones build
    per-symbol rolling windows as data is accepted.
    """

    def __init__(self, config: QualityConfig | None = None) -> None:
        self.config = config or QualityConfig()
        self._symbols: dict[str, _SymbolState] = {}
        self._alert_callbacks: list[Callable[[str, int, list[ValidationIssue]], None]] = []
        self._totals = Counter(checked=0, accepted=0, repaired=0, rejected=0, warnings=0)
        self._by_code: Counter[str] = Counter()
        self._by_symbol: dict[str, Counter] = {}

    # ------------------------------------------------------------------
    # Severity resolution
    # ------------------------------------------------------------------

    def _issue(self, code: str, message: str, field: str | None = None, **details: Any) -> ValidationIssue:
        severity = _SEVERITY_TABLE[code][_STRICTNESS_INDEX[self.config.strictness]]
        return ValidationIssue(code=code, severity=severity, message=message, field=field, details=details)

    def _state(self, symbol: str) -> _SymbolState:
        state = self._symbols.get(symbol)
        if state is None:
            state = _SymbolState(self.config.spike_window, self.config.volume_window)
            self._symbols[symbol] = state
        return state

    # ------------------------------------------------------------------
    # Standalone checks (the plan's named methods)
    # ------------------------------------------------------------------

    def validate_ohlc_relationship(
        self, open: Any, high: Any, low: Any, close: Any  # noqa: A002 - plan-mandated names
    ) -> list[str]:
        """Problems with an OHLC quadruple; empty list means consistent."""
        o, h, l, c = (to_decimal(v, n) for v, n in
                      ((open, "open"), (high, "high"), (low, "low"), (close, "close")))
        problems: list[str] = []
        if h < l:
            problems.append(f"high {h} < low {l}")
        if h < o:
            problems.append(f"high {h} < open {o}")
        if h < c:
            problems.append(f"high {h} < close {c}")
        if l > o:
            problems.append(f"low {l} > open {o}")
        if l > c:
            problems.append(f"low {l} > close {c}")
        for name, value in (("open", o), ("high", h), ("low", l), ("close", c)):
            if value <= 0:
                problems.append(f"{name} {value} is not positive")
        return problems

    def check_for_spikes(
        self, price: Any, symbol: str, threshold: float | None = None
    ) -> tuple[bool, float | None]:
        """Is ``price`` a spike against ``symbol``'s rolling window?

        Returns ``(is_spike, zscore)``; zscore is None when the window has
        too few samples or zero variance. Does not mutate the window.
        """
        limit = threshold if threshold is not None else self.config.spike_zscore_threshold
        state = self._symbols.get(symbol.upper())
        if state is None or len(state.prices) < self.config.spike_min_samples:
            return False, None
        values = [float(p) for p in state.prices]
        mean = statistics.fmean(values)
        stdev = statistics.pstdev(values)
        if stdev == 0:
            # Flat window: any different price is technically infinite sigma.
            # Only flag it when it is materially different (> threshold %).
            move_pct = abs(float(to_decimal(price, "price")) - mean) / mean * 100 if mean else 0.0
            return move_pct > limit, None
        zscore = (float(to_decimal(price, "price")) - mean) / stdev
        return abs(zscore) > limit, zscore

    def check_for_gaps(
        self,
        timestamp: datetime,
        last_timestamp: datetime | None,
        max_gap_seconds: int | None = None,
    ) -> tuple[bool, float]:
        """Did the feed stall between two consecutive updates?

        Returns ``(is_gap, gap_seconds)``. Never a gap when there is no
        previous timestamp.
        """
        if last_timestamp is None:
            return False, 0.0
        limit = max_gap_seconds if max_gap_seconds is not None else self.config.max_gap_seconds
        gap = (timestamp - last_timestamp).total_seconds()
        return gap > limit, gap

    def check_volume_anomaly(
        self,
        volume: int,
        avg_volume: float | None = None,
        threshold: float | None = None,
        symbol: str | None = None,
    ) -> tuple[bool, float | None]:
        """Is ``volume`` anomalously large vs. the average?

        ``avg_volume`` may be supplied directly; otherwise it comes from
        ``symbol``'s rolling window. Returns ``(is_anomaly, ratio)``.
        """
        limit = threshold if threshold is not None else self.config.volume_anomaly_multiple
        if avg_volume is None:
            if symbol is None:
                raise ValueError("either avg_volume or symbol is required")
            state = self._symbols.get(symbol.upper())
            if state is None or len(state.volumes) < self.config.volume_min_samples:
                return False, None
            nonzero = [v for v in state.volumes if v > 0]
            if not nonzero:
                return False, None
            avg_volume = statistics.fmean(nonzero)
        if avg_volume <= 0:
            return False, None
        ratio = volume / avg_volume
        return ratio > limit, ratio

    def compare_sources(self, tick_a: Tick, tick_b: Tick) -> ValidationIssue | None:
        """Cross-source sanity: do two feeds agree on the price?

        Returns an issue when the divergence exceeds
        ``source_divergence_pct``, else None.
        """
        reference = (tick_a.last + tick_b.last) / 2
        if reference <= 0:
            return None
        divergence_pct = abs(tick_a.last - tick_b.last) / reference * 100
        if float(divergence_pct) <= self.config.source_divergence_pct:
            return None
        return self._issue(
            "source_divergence",
            f"{tick_a.symbol}: sources disagree by {divergence_pct:.3f}% "
            f"({tick_a.source or 'a'}={tick_a.last}, {tick_b.source or 'b'}={tick_b.last})",
            field="last",
            divergence_pct=float(divergence_pct),
        )

    # ------------------------------------------------------------------
    # Tick validation
    # ------------------------------------------------------------------

    def validate_tick(self, tick: Tick) -> ValidationResult:
        """Full quality gate for one normalized tick.

        Accepted/repaired ticks update the rolling windows; rejected ones
        do not (a spike must not widen the very deviation that flags the
        next spike).
        """
        config = self.config
        state = self._state(tick.symbol)
        issues: list[ValidationIssue] = []

        # -- price range -------------------------------------------------
        if tick.last <= 0:
            issues.append(self._issue(
                "non_positive_price", f"{tick.symbol}: last {tick.last} is not positive",
                field="last", price=str(tick.last),
            ))
        elif not (config.min_price <= tick.last <= config.max_price):
            issues.append(self._issue(
                "price_out_of_range",
                f"{tick.symbol}: last {tick.last} outside [{config.min_price}, {config.max_price}]",
                field="last", price=str(tick.last),
            ))

        # -- quote sanity --------------------------------------------------
        if tick.bid is not None and tick.ask is not None:
            if tick.bid > tick.ask:
                issues.append(self._issue(
                    "crossed_quote", f"{tick.symbol}: bid {tick.bid} > ask {tick.ask}",
                    field="bid", bid=str(tick.bid), ask=str(tick.ask),
                ))
            elif not (tick.bid <= tick.last <= tick.ask):
                issues.append(self._issue(
                    "last_outside_spread",
                    f"{tick.symbol}: last {tick.last} outside [{tick.bid}, {tick.ask}] "
                    "(stale quote or off-exchange print?)",
                    field="last", bid=str(tick.bid), ask=str(tick.ask), last=str(tick.last),
                ))

        # -- volume --------------------------------------------------------
        if tick.volume < 0:
            issues.append(self._issue(
                "negative_volume", f"{tick.symbol}: volume {tick.volume} is negative",
                field="volume", volume=tick.volume,
            ))
        else:
            anomalous, ratio = self.check_volume_anomaly(tick.volume, symbol=tick.symbol)
            if anomalous:
                issues.append(self._issue(
                    "volume_anomaly",
                    f"{tick.symbol}: volume {tick.volume} is {ratio:.1f}x the rolling average",
                    field="volume", ratio=round(ratio, 2),
                ))

        # -- chronology and gaps --------------------------------------------
        if state.last_ts is not None and tick.timestamp < state.last_ts:
            issues.append(self._issue(
                "out_of_order",
                f"{tick.symbol}: timestamp {tick.timestamp.isoformat()} precedes "
                f"last seen {state.last_ts.isoformat()}",
                field="timestamp",
            ))
        else:
            gap, gap_seconds = self.check_for_gaps(tick.timestamp, state.last_ts)
            if gap:
                issues.append(self._issue(
                    "data_gap",
                    f"{tick.symbol}: {gap_seconds:.0f}s since previous tick "
                    f"(max {config.max_gap_seconds}s) — feed stall?",
                    field="timestamp", gap_seconds=gap_seconds,
                ))

        # -- statistical spike ----------------------------------------------
        if tick.last > 0:
            spike, zscore = self.check_for_spikes(tick.last, tick.symbol)
            if spike:
                issues.append(self._issue(
                    "price_spike",
                    f"{tick.symbol}: last {tick.last} is "
                    + (f"{zscore:+.1f} std devs from the rolling mean" if zscore is not None
                       else "far off a flat rolling window"),
                    field="last", zscore=None if zscore is None else round(zscore, 2),
                ))

        return self._resolve_tick(tick, state, issues)

    def _resolve_tick(
        self, tick: Tick, state: _SymbolState, issues: list[ValidationIssue]
    ) -> ValidationResult:
        errors = [i for i in issues if i.severity is Severity.ERROR]
        self._record(tick.symbol, issues)

        if not errors:
            self._observe(tick, state)
            self._count(tick.symbol, "accepted")
            return ValidationResult(action=Action.ACCEPTED, issues=issues, tick=tick)

        # Interpolation: substitute the previous observed price, but only
        # when every error is price-level and a previous price exists.
        if (
            self.config.on_bad_data is BadDataPolicy.REPAIR
            and state.prices
            and all(e.code in _REPAIRABLE for e in errors)
        ):
            substitute = state.prices[-1]
            repaired = dc_replace(tick, last=substitute)
            logger.warning(
                "repaired %s tick: last %s -> %s (%s)",
                tick.symbol, tick.last, substitute, ", ".join(e.code for e in errors),
            )
            self._observe(repaired, state)
            self._count(tick.symbol, "repaired")
            return ValidationResult(action=Action.REPAIRED, issues=issues, tick=repaired)

        self._count(tick.symbol, "rejected")
        state.consecutive_rejects += 1
        for error in errors:
            logger.warning("rejected %s tick: %s", tick.symbol, error.message)
        if state.consecutive_rejects >= self.config.alert_threshold:
            self._fire_alert(tick.symbol, state.consecutive_rejects, issues)
            # Regime reset: if "bad" data keeps coming, it is probably the
            # new reality. Start learning it instead of rejecting forever.
            state.prices.clear()
            state.volumes.clear()
            state.consecutive_rejects = 0
        return ValidationResult(action=Action.REJECTED, issues=issues, tick=tick)

    def _observe(self, tick: Tick, state: _SymbolState) -> None:
        state.prices.append(tick.last)
        state.volumes.append(tick.volume)
        if state.last_ts is None or tick.timestamp > state.last_ts:
            state.last_ts = tick.timestamp
        state.consecutive_rejects = 0

    # ------------------------------------------------------------------
    # Bar validation
    # ------------------------------------------------------------------

    def validate_bar(self, bar: Bar) -> ValidationResult:
        """Quality gate for one closed bar.

        Structural checks only — spikes and volume anomalies are tick-level
        concerns and were already screened upstream. Bars are never
        repaired: there is no single obvious substitute for a broken OHLC
        quadruple.
        """
        state = self._state(bar.symbol)
        issues: list[ValidationIssue] = []

        problems = self.validate_ohlc_relationship(bar.open, bar.high, bar.low, bar.close)
        if problems:
            issues.append(self._issue(
                "ohlc_inconsistent",
                f"{bar.symbol} {bar.timeframe} bar @ {bar.ts.isoformat()}: " + "; ".join(problems),
            ))

        if bar.volume < 0:
            issues.append(self._issue(
                "negative_volume",
                f"{bar.symbol} {bar.timeframe} bar @ {bar.ts.isoformat()}: volume {bar.volume}",
                field="volume", volume=bar.volume,
            ))

        last_bar_ts = state.last_bar_ts.get(bar.timeframe)
        if last_bar_ts is not None:
            if bar.ts <= last_bar_ts:
                issues.append(self._issue(
                    "out_of_order",
                    f"{bar.symbol} {bar.timeframe} bar @ {bar.ts.isoformat()} does not "
                    f"advance on previous bar @ {last_bar_ts.isoformat()}",
                    field="ts",
                ))
            elif bar.timeframe in INTRADAY_MINUTES:
                expected_seconds = INTRADAY_MINUTES[bar.timeframe] * 60
                gap, gap_seconds = self.check_for_gaps(
                    bar.ts, last_bar_ts, max_gap_seconds=2 * expected_seconds
                )
                if gap:
                    issues.append(self._issue(
                        "data_gap",
                        f"{bar.symbol} {bar.timeframe}: {gap_seconds:.0f}s since previous "
                        f"bar open (expected {expected_seconds}s) — missing bars?",
                        field="ts", gap_seconds=gap_seconds,
                    ))

        errors = [i for i in issues if i.severity is Severity.ERROR]
        self._record(bar.symbol, issues)
        if errors:
            self._count(bar.symbol, "rejected")
            for error in errors:
                logger.warning("rejected %s bar: %s", bar.symbol, error.message)
            return ValidationResult(action=Action.REJECTED, issues=issues, bar=bar)

        state.last_bar_ts[bar.timeframe] = bar.ts
        self._count(bar.symbol, "accepted")
        return ValidationResult(action=Action.ACCEPTED, issues=issues, bar=bar)

    # ------------------------------------------------------------------
    # Alerts
    # ------------------------------------------------------------------

    def on_alert(
        self, callback: Callable[[str, int, list[ValidationIssue]], None]
    ) -> Callable[[str, int, list[ValidationIssue]], None]:
        """Register ``callback(symbol, consecutive_rejects, last_issues)``.

        Fired when a symbol crosses ``alert_threshold`` consecutive
        rejections. Returns the callback for later removal.
        """
        if not callable(callback):
            raise ValueError("callback must be callable")
        self._alert_callbacks.append(callback)
        return callback

    def remove_alert_callback(self, callback: Callable[..., None]) -> None:
        try:
            self._alert_callbacks.remove(callback)
        except ValueError:
            pass

    def _fire_alert(self, symbol: str, count: int, issues: list[ValidationIssue]) -> None:
        logger.error(
            "ALERT: %d consecutive rejected updates for %s (last: %s)",
            count, symbol, ", ".join(i.code for i in issues) or "none",
        )
        for callback in list(self._alert_callbacks):
            try:  # a broken alert channel must not break validation
                callback(symbol, count, issues)
            except Exception:  # noqa: BLE001
                logger.exception("alert callback %r failed for %s", callback, symbol)

    # ------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------

    def _count(self, symbol: str, outcome: str) -> None:
        self._totals["checked"] += 1
        self._totals[outcome] += 1
        per_symbol = self._by_symbol.setdefault(symbol, Counter())
        per_symbol["checked"] += 1
        per_symbol[outcome] += 1

    def _record(self, symbol: str, issues: list[ValidationIssue]) -> None:
        for issue in issues:
            self._by_code[issue.code] += 1
            if issue.severity is Severity.WARNING:
                self._totals["warnings"] += 1

    @property
    def quality_score(self) -> float:
        """Fraction of checked data that was usable (accepted + repaired)."""
        checked = self._totals["checked"]
        if checked == 0:
            return 1.0
        return (self._totals["accepted"] + self._totals["repaired"]) / checked

    def report(self) -> dict[str, Any]:
        """Data quality statistics: totals, per-code and per-symbol."""
        by_symbol = {}
        for symbol, counts in sorted(self._by_symbol.items()):
            usable = counts["accepted"] + counts["repaired"]
            by_symbol[symbol] = {
                **{k: counts[k] for k in ("checked", "accepted", "repaired", "rejected")},
                "quality_score": usable / counts["checked"] if counts["checked"] else 1.0,
            }
        return {
            "totals": {k: self._totals[k]
                       for k in ("checked", "accepted", "repaired", "rejected", "warnings")},
            "quality_score": self.quality_score,
            "by_code": dict(sorted(self._by_code.items())),
            "by_symbol": by_symbol,
        }

    def reset_statistics(self) -> None:
        """Clear counters (not the rolling windows)."""
        self._totals = Counter(checked=0, accepted=0, repaired=0, rejected=0, warnings=0)
        self._by_code.clear()
        self._by_symbol.clear()
