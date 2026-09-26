"""Portfolio Intelligence configuration (``config/portfolio_intelligence.yaml``).

Defaults live on :class:`IntelligenceConfig`; the YAML file overrides them and
``PI_<KEY>`` environment variables override both. Unknown keys are ignored
with a warning rather than crashing the app.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("backtest.intelligence")

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "portfolio_intelligence.yaml"

DEFAULT_SCENARIOS: List[Dict[str, Any]] = [
    {"key": "spot_down_2pct", "label": "Market -2%", "spot_pct": -0.02, "vol_pts": 0.0},
    {"key": "spot_up_2pct", "label": "Market +2%", "spot_pct": 0.02, "vol_pts": 0.0},
    {"key": "vol_up_5pts", "label": "IV +5 pts", "spot_pct": 0.0, "vol_pts": 5.0},
    {"key": "vol_down_5pts", "label": "IV -5 pts", "spot_pct": 0.0, "vol_pts": -5.0},
    {"key": "crash", "label": "Market -2% & IV +5", "spot_pct": -0.02, "vol_pts": 5.0},
]


@dataclass
class IntelligenceConfig:
    # Greeks alerts
    gamma_critical: float = -150.0
    delta_warning_abs: float = 800.0
    # Concentration
    concentration_max_pct: float = 0.60
    concentration_min_positions: int = 2
    strike_cluster_min: int = 3
    # Correlation
    correlation_warning: float = 0.80
    correlation_min_samples: int = 30
    correlation_window: int = 240
    correlation_cache_s: float = 300.0
    # Regime
    regime_low_max: float = 15.0
    regime_high_min: float = 22.0
    regime_hysteresis: float = 0.5
    regime_sample_s: float = 300.0
    regime_benchmark: str = "NIFTY"
    realized_vol_window: int = 60
    vix_symbols: List[str] = field(default_factory=lambda: ["INDIAVIX", "INDIA VIX", "VIX"])
    vix_stale_s: float = 900.0
    # Market activity
    oi_spike_multiplier: float = 3.0
    oi_min_history: int = 3
    spread_multiplier: float = 2.0
    # Feed
    feed_stale_s: float = 120.0
    # Scenarios
    scenarios: List[Dict[str, Any]] = field(default_factory=lambda: list(DEFAULT_SCENARIOS))
    # Alert lifecycle
    event_alert_ttl_s: float = 3600.0
    ignore_ttl_s: float = 3600.0
    auto_dismiss_exempt: List[str] = field(default_factory=lambda: ["critical"])
    renotify_cooldown_s: float = 60.0
    # Cadence
    greeks_cache_s: float = 1.0
    evaluate_interval_s: float = 1.0
    greeks_history_s: float = 60.0
    risk_free_rate: float = 0.06

    def to_dict(self) -> Dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    def update(self, values: Dict[str, Any]) -> None:
        known = {f.name: f for f in fields(self)}
        for key, raw in (values or {}).items():
            if key not in known:
                logger.warning("[intelligence] unknown config key ignored: %s", key)
                continue
            current = getattr(self, key)
            try:
                if isinstance(current, bool):
                    value: Any = str(raw).strip().lower() in {"1", "true", "yes", "on"}
                elif isinstance(current, int) and not isinstance(current, bool):
                    value = int(float(raw))
                elif isinstance(current, float):
                    value = float(raw)
                elif isinstance(current, list):
                    if isinstance(raw, str):
                        value = [s.strip() for s in raw.split(",") if s.strip()]
                    else:
                        value = list(raw)
                else:
                    value = str(raw)
            except (TypeError, ValueError):
                logger.warning("[intelligence] bad value for %s: %r (kept %r)", key, raw, current)
                continue
            setattr(self, key, value)


def load_config(
    path: Optional[os.PathLike] = None,
    env: Optional[Dict[str, str]] = None,
    overrides: Optional[Dict[str, Any]] = None,
) -> IntelligenceConfig:
    """Defaults ← YAML file ← ``PI_*`` env ← explicit overrides."""
    cfg = IntelligenceConfig()
    cfg_path = Path(path) if path else Path(
        os.environ.get("PORTFOLIO_INTELLIGENCE_CONFIG", DEFAULT_CONFIG_PATH)
    )
    if cfg_path.exists():
        try:
            import yaml

            data = yaml.safe_load(cfg_path.read_text()) or {}
            if isinstance(data, dict):
                cfg.update(data)
        except Exception:  # noqa: BLE001 — a bad file must not take the app down
            logger.exception("[intelligence] failed to read %s — using defaults", cfg_path)
    env = os.environ if env is None else env
    env_values = {}
    for f in fields(cfg):
        if f.name == "scenarios":
            continue
        raw = env.get(f"PI_{f.name.upper()}")
        if raw is not None:
            env_values[f.name] = raw
    cfg.update(env_values)
    if overrides:
        cfg.update(overrides)
    return cfg
