"""Single risk-config loader (BUG-27).

One source of truth for the *portfolio-level* circuit-breaker limits that the
web ``/risk`` page shows and the :class:`RiskSupervisor` enforces:
``config/forward_testing.yaml``.

Before this module the same numbers were hardcoded in several places (the
``GlobalRiskConfig`` class defaults, ``PortfolioManager.__init__`` and the
YAML), so the page could display one drawdown while the engine ran another.
The loader collapses the global breakers to the YAML: ``PortfolioManager``
builds both mode configs from it and reports ``limit_source`` so the API and
page can state where the numbers came from.

Deliberately NOT loaded here:

* The Layer-1 pre-trade rulebook (``config/risk_limits.yaml``) — per-trade /
  per-runner ceilings + structure entitlement. A separate authority with its
  own fail-closed loader (``backtest.risk_limits.load_risk_limits``).
* Per-runner spawn breakers (``RunnerConfig.max_drawdown_pct`` /
  ``daily_loss_limit_pct``) — instance-level defaults, not portfolio-level.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from backtest.forward.risk_supervisor import GlobalRiskConfig

logger = logging.getLogger("backtest.forward.risk_config")

DEFAULT_FORWARD_CONFIG_PATH = (
    Path(__file__).resolve().parents[3] / "config" / "forward_testing.yaml"
)

#: Short labels surfaced on the API/page so an operator can see the source.
LIMIT_SOURCE_YAML = "forward_testing.yaml"
LIMIT_SOURCE_DEFAULTS = "builtin_defaults"
LIMIT_SOURCE_EXPLICIT = "explicit"


@dataclass
class LoadedRiskConfig:
    """The per-mode configs plus provenance for the API/page."""

    paper: GlobalRiskConfig
    live: GlobalRiskConfig
    source: str  # absolute path, or "explicit" / "builtin_defaults"
    limit_source: str  # short label


def _to_float(value: Any, default: float) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _build_config(doc: dict) -> GlobalRiskConfig:
    """Map the YAML ``risk:`` + ``portfolio:`` sections onto a config.

    ``max_drawdown_pct`` is a fraction (0.10 == 10%), matching the engine's
    ``RiskConfig``. ``daily_loss_limit_pct`` is a fraction of capital, so the
    absolute rupee limit is ``pct * initial_capital``.
    """
    risk = (doc or {}).get("risk") or {}
    portfolio = (doc or {}).get("portfolio") or {}

    initial_capital = _to_float(portfolio.get("initial_capital"), 100_000.0)
    daily_loss_pct = _to_float(risk.get("daily_loss_limit_pct"), 0.02)
    max_drawdown = _to_float(risk.get("max_drawdown_pct"), 0.10)
    max_leverage = _to_float(risk.get("max_leverage"), 1.0)

    daily_loss_limit = abs(daily_loss_pct) * abs(initial_capital)

    return GlobalRiskConfig(
        daily_loss_limit=daily_loss_limit,
        max_drawdown_pct=min(max(max_drawdown, 0.001), 0.999),
        max_leverage=max_leverage,
    )


def _defaults() -> LoadedRiskConfig:
    # Two independent instances — mutating one mode must never move the other.
    return LoadedRiskConfig(
        paper=GlobalRiskConfig(),
        live=GlobalRiskConfig(),
        source=LIMIT_SOURCE_DEFAULTS,
        limit_source=LIMIT_SOURCE_DEFAULTS,
    )


def load_risk_config_from_yaml(path: Optional[Path] = None) -> LoadedRiskConfig:
    """Load the global risk config from ``forward_testing.yaml``.

    Fail-soft: a missing/unreadable file (or missing PyYAML) yields the
    ``GlobalRiskConfig`` defaults with ``limit_source == "builtin_defaults"`` —
    the page then shows that label instead of silently pretending the YAML
    value is in force.
    """
    config_path = Path(path) if path is not None else DEFAULT_FORWARD_CONFIG_PATH

    if not config_path.exists():
        logger.info("No risk config at %s, using builtin defaults", config_path)
        return _defaults()

    try:
        import yaml

        doc = yaml.safe_load(config_path.read_text()) or {}
    except ModuleNotFoundError:
        logger.warning("PyYAML unavailable; using builtin risk defaults")
        return _defaults()
    except Exception as exc:  # noqa: BLE001 — loud log, but never fatal at boot
        logger.warning(
            "Could not parse %s (%s); using builtin risk defaults", config_path, exc
        )
        return _defaults()

    cfg = _build_config(doc)
    logger.info(
        "Loaded risk config from %s (limit_source=%s): daily_loss=%.0f drawdown=%.1f%%",
        config_path,
        LIMIT_SOURCE_YAML,
        cfg.daily_loss_limit,
        cfg.max_drawdown_pct * 100,
    )
    return LoadedRiskConfig(
        paper=cfg,
        live=GlobalRiskConfig(
            daily_loss_limit=cfg.daily_loss_limit,
            max_drawdown_pct=cfg.max_drawdown_pct,
            max_leverage=cfg.max_leverage,
            breach_mode=cfg.breach_mode,
            correlation_warning_threshold=cfg.correlation_warning_threshold,
        ),
        source=str(config_path),
        limit_source=LIMIT_SOURCE_YAML,
    )