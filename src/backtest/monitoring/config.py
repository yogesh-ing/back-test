"""Monitor configuration: limits, thresholds and strategy regime profiles.

Loaded from ``config/monitoring.yaml`` (override the path with
``MONITORING_CONFIG_PATH``). Every field has a default here, so a missing or
partial file degrades to sane limits rather than a crash.

**Greek limits are ratios of portfolio equity, in money terms.** Raw Greeks
cannot be summed across underlyings (one NIFTY delta ≠ one RELIANCE share),
and an absolute "gamma −1,000" means different things on a ₹5L and a ₹5Cr
account. Each limit therefore reads "this much P&L, as a fraction of equity,
for this move" — see ``config/monitoring.yaml`` for the per-limit units.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("backtest.monitoring.config")

_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG_PATH = _REPO_ROOT / "config" / "monitoring.yaml"


@dataclass
class Threshold:
    """A warn/critical pair. Values are positive magnitudes."""

    warn: float
    critical: float

    def severity(self, magnitude: float) -> Optional[str]:
        if magnitude >= self.critical:
            return "critical"
        if magnitude >= self.warn:
            return "warning"
        return None

    def to_dict(self) -> Dict[str, float]:
        return {"warn": self.warn, "critical": self.critical}


@dataclass
class GreekLimits:
    #: |₹ P&L for a 1% move in every underlying| / equity
    delta_1pct: Threshold = field(default_factory=lambda: Threshold(0.01, 0.02))
    #: convexity LOSS on a ±2% move (short gamma only) / equity
    gamma_2pct: Threshold = field(default_factory=lambda: Threshold(0.005, 0.01))
    #: |₹ P&L per 1 vol-point IV change| / equity
    vega_1pt: Threshold = field(default_factory=lambda: Threshold(0.0075, 0.015))
    #: time decay PAID per calendar day (negative theta only) / equity
    theta_day: Threshold = field(default_factory=lambda: Threshold(0.003, 0.006))


@dataclass
class ScenarioConfig:
    spot_moves_pct: List[float] = field(
        default_factory=lambda: [-3.0, -2.0, -1.0, -0.5, 0.5, 1.0, 2.0, 3.0]
    )
    iv_shifts_pts: List[float] = field(default_factory=lambda: [-5.0, -2.0, 2.0, 5.0])
    #: Combined shocks: [{name, spot_pct, iv_pts, days}]
    stress: List[Dict[str, Any]] = field(
        default_factory=lambda: [
            {"name": "Crash: −2% & IV +5", "spot_pct": -2.0, "iv_pts": 5.0, "days": 0},
            {"name": "Gap down: −3% & IV +8", "spot_pct": -3.0, "iv_pts": 8.0, "days": 0},
            {"name": "Rally: +2% & IV −2", "spot_pct": 2.0, "iv_pts": -2.0, "days": 0},
            {"name": "Melt-up: +3% & IV +3", "spot_pct": 3.0, "iv_pts": 3.0, "days": 0},
        ]
    )
    #: Worst modelled scenario loss / equity.
    worst_loss: Threshold = field(default_factory=lambda: Threshold(0.03, 0.05))


@dataclass
class ConcentrationConfig:
    #: Share of gross notional in one underlying.
    underlying_pct: Threshold = field(default_factory=lambda: Threshold(0.50, 0.70))
    #: Share of gross notional in one correlated group (e.g. Indian indices).
    group_pct: Threshold = field(default_factory=lambda: Threshold(0.70, 0.85))
    #: Legs clustered at one underlying/strike/expiry (liquidity risk).
    strike_cluster_positions: int = 3
    #: Distinct strategies stacked on one underlying (hidden concentration).
    strategies_per_underlying: int = 3
    #: Percentage alerts need at least this many distinct exposures
    #: (structures / equity positions) …
    min_positions: int = 2
    #: … held by at least this many strategies. One strategy's own
    #: single-underlying design is not *hidden* concentration.
    min_strategies: int = 2
    #: Correlated underlying families — any symbol not listed is its own group.
    groups: Dict[str, List[str]] = field(
        default_factory=lambda: {
            "INDIA_INDEX": ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"],
        }
    )


@dataclass
class CorrelationConfig:
    #: Number of most recent aligned P&L observations used.
    lookback: int = 300
    #: Fewer overlapping observations → the pair is reported as insufficient.
    min_observations: int = 20
    #: Positive correlation of strategy P&L changes (losses cluster).
    positive: Threshold = field(default_factory=lambda: Threshold(0.70, 0.85))
    #: Correlation at or below −this is reported as an offsetting pair (info).
    negative_info: float = 0.70


@dataclass
class RegimeConfig:
    #: Vol-index / realized-vol boundaries in annualised vol points.
    low_vol_max: float = 15.0
    high_vol_min: float = 25.0
    realized_low_max: float = 12.0
    realized_high_min: float = 20.0
    realized_window: int = 20
    transition_lookback: int = 5
    transition_change_pct: float = 20.0
    range_window: int = 20
    range_expansion_mult: float = 1.5
    #: Minutes in one NSE cash session — annualises intraday realized vol.
    session_minutes: float = 375.0
    trading_days: float = 252.0
    benchmark: str = "NIFTY"
    #: A symbol the data source may carry for the vol index (DB: INDIAVIX).
    vol_index_symbol: str = "INDIAVIX"


@dataclass
class StrategyProfile:
    optimal_regime: Optional[str] = None  # low_vol | moderate_vol | high_vol
    optimal_vol_range: Optional[List[float]] = None  # [lo, hi] vol points
    tags: List[str] = field(default_factory=list)
    source: str = "config"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "optimal_regime": self.optimal_regime,
            "optimal_vol_range": self.optimal_vol_range,
            "tags": list(self.tags),
            "source": self.source,
        }


@dataclass
class MonitorConfig:
    greeks: GreekLimits = field(default_factory=GreekLimits)
    scenarios: ScenarioConfig = field(default_factory=ScenarioConfig)
    concentration: ConcentrationConfig = field(default_factory=ConcentrationConfig)
    correlation: CorrelationConfig = field(default_factory=CorrelationConfig)
    regime: RegimeConfig = field(default_factory=RegimeConfig)
    #: strategy kind / runner name → profile (overrides inference).
    strategy_profiles: Dict[str, StrategyProfile] = field(default_factory=dict)
    risk_free_rate: float = 0.06
    default_iv: float = 0.15
    #: Money symbol for alert text — the app sets this from its --currency.
    currency_symbol: str = "₹"
    #: Snapshot cache TTL for API reads (the UI polls at 1 Hz).
    cache_ttl_s: float = 1.0
    #: Run the alert sweep every N complete feed ticks (0 disables).
    sweep_every_ticks: int = 5

    def to_dict(self) -> Dict[str, Any]:
        def _conv(obj: Any) -> Any:
            if isinstance(obj, (Threshold, StrategyProfile)):
                return obj.to_dict()
            if hasattr(obj, "__dataclass_fields__"):
                return {f.name: _conv(getattr(obj, f.name)) for f in fields(obj)}
            if isinstance(obj, dict):
                return {k: _conv(v) for k, v in obj.items()}
            if isinstance(obj, list):
                return [_conv(v) for v in obj]
            return obj

        return _conv(self)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _threshold(raw: Any, default: Threshold) -> Threshold:
    if not isinstance(raw, dict):
        return default
    try:
        warn = float(raw.get("warn", default.warn))
        critical = float(raw.get("critical", default.critical))
    except (TypeError, ValueError):
        logger.warning("[monitor-config] bad threshold %r — using default", raw)
        return default
    if critical < warn:
        logger.warning("[monitor-config] critical < warn in %r — swapping", raw)
        warn, critical = critical, warn
    return Threshold(warn, critical)


def _apply(section: Any, raw: Any) -> None:
    """Overlay a YAML mapping onto a config dataclass, type by type."""
    if not isinstance(raw, dict):
        return
    for f in fields(section):
        if f.name not in raw:
            continue
        current = getattr(section, f.name)
        value = raw[f.name]
        if isinstance(current, Threshold):
            setattr(section, f.name, _threshold(value, current))
        elif isinstance(current, bool):
            setattr(section, f.name, bool(value))
        elif isinstance(current, int) and not isinstance(current, bool):
            try:
                setattr(section, f.name, int(value))
            except (TypeError, ValueError):
                logger.warning("[monitor-config] %s must be an int", f.name)
        elif isinstance(current, float):
            try:
                setattr(section, f.name, float(value))
            except (TypeError, ValueError):
                logger.warning("[monitor-config] %s must be a number", f.name)
        elif isinstance(current, (list, dict, str)) or current is None:
            setattr(section, f.name, value)


def config_from_dict(raw: Dict[str, Any]) -> MonitorConfig:
    cfg = MonitorConfig()
    if not isinstance(raw, dict):
        return cfg
    _apply(cfg.greeks, raw.get("greek_limits"))
    _apply(cfg.scenarios, raw.get("scenarios"))
    _apply(cfg.concentration, raw.get("concentration"))
    _apply(cfg.correlation, raw.get("correlation"))
    _apply(cfg.regime, raw.get("regime"))
    for key in ("risk_free_rate", "default_iv", "cache_ttl_s"):
        if key in raw:
            try:
                setattr(cfg, key, float(raw[key]))
            except (TypeError, ValueError):
                logger.warning("[monitor-config] %s must be a number", key)
    if "sweep_every_ticks" in raw:
        try:
            cfg.sweep_every_ticks = int(raw["sweep_every_ticks"])
        except (TypeError, ValueError):
            logger.warning("[monitor-config] sweep_every_ticks must be an int")
    for name, prof in (raw.get("strategy_profiles") or {}).items():
        if not isinstance(prof, dict):
            continue
        vol_range = prof.get("optimal_vol_range")
        cfg.strategy_profiles[str(name).lower()] = StrategyProfile(
            optimal_regime=prof.get("optimal_regime"),
            optimal_vol_range=[float(v) for v in vol_range] if vol_range else None,
            tags=[str(t) for t in prof.get("tags") or []],
            source="config",
        )
    return cfg


def load_monitor_config(path: Optional[str | Path] = None) -> MonitorConfig:
    """Load the monitor config; any failure falls back to defaults (logged)."""
    target = Path(path or os.environ.get("MONITORING_CONFIG_PATH") or DEFAULT_CONFIG_PATH)
    if not target.exists():
        logger.info("[monitor-config] %s not found — using defaults", target)
        return MonitorConfig()
    try:
        import yaml

        with target.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
    except Exception:  # noqa: BLE001 — a bad config must not take the app down
        logger.exception("[monitor-config] failed to read %s — using defaults", target)
        return MonitorConfig()
    return config_from_dict(raw)
