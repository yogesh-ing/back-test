"""Options configuration — loads and validates the options expression config.

The config file ``options.yaml`` controls:

- Which selector to use (ATM, delta, fixed distance, target price)
- Which structures are allowed per strategy
- Expiry policy
- Risk limits (max positions, max margin)

Usage::

    from backtest.options.config import load_options_config, OptionsConfig

    config = load_options_config()  # loads options.yaml or defaults
    print(config.default_selector)  # "atm"
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]


@dataclass
class SelectorConfig:
    """Configuration for strike selection."""

    type: str = "atm"
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExpiryConfig:
    """Configuration for expiry selection."""

    policy: str = "nearest"
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class StructureConfig:
    """Configuration for option structures."""

    allowed: list[str] = field(
        default_factory=lambda: [
            "long_call",
            "long_put",
            "bull_call_spread",
            "bear_put_spread",
        ]
    )
    default_spread_width: int = 200  # points between strikes


@dataclass
class RiskConfig:
    """Configuration for risk limits."""

    max_positions: int = 10
    max_margin_per_trade: int = 500_000  # ₹5 lakh
    max_total_margin: int = 20_00_000  # ₹20 lakh
    max_loss_per_trade_pct: float = 2.0  # % of capital


@dataclass
class OptionsConfig:
    """Top-level options configuration."""

    enabled: bool = True
    underlying: str = "NIFTY"
    selector: SelectorConfig = field(default_factory=SelectorConfig)
    expiry: ExpiryConfig = field(default_factory=ExpiryConfig)
    structures: StructureConfig = field(default_factory=StructureConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)

    # Per-strategy overrides: {strategy_name: {selector: ..., structures: ...}}
    strategy_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)


def load_options_config(
    config_path: str | Path | None = None,
) -> OptionsConfig:
    """Load options configuration from YAML, or return defaults.

    Parameters
    ----------
    config_path:
        Path to ``options.yaml``.  If ``None``, searches:
        1. ``options.yaml`` in CWD
        2. ``config/options.yaml`` in CWD
        3. Returns defaults
    """
    if config_path is None:
        candidates = [
            Path("options.yaml"),
            Path("config/options.yaml"),
            Path("src/options.yaml"),
        ]
        for candidate in candidates:
            if candidate.exists():
                config_path = candidate
                break

    if config_path is None or not Path(config_path).exists():
        return OptionsConfig()  # all defaults

    if yaml is None:
        raise ImportError(
            "PyYAML is required to load options.yaml. "
            "Install with: pip install pyyaml"
        )

    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    return _parse_config(raw)


def _parse_config(raw: dict[str, Any]) -> OptionsConfig:
    """Parse a raw dict into an OptionsConfig."""
    config = OptionsConfig()

    config.enabled = raw.get("enabled", True)
    config.underlying = raw.get("underlying", "NIFTY")

    # Selector
    sel = raw.get("selector", {})
    config.selector = SelectorConfig(
        type=sel.get("type", "atm"),
        params=sel.get("params", {}),
    )

    # Expiry
    exp = raw.get("expiry", {})
    config.expiry = ExpiryConfig(
        policy=exp.get("policy", "nearest"),
        params=exp.get("params", {}),
    )

    # Structures
    struct = raw.get("structures", {})
    config.structures = StructureConfig(
        allowed=struct.get("allowed", [
            "long_call", "long_put", "bull_call_spread", "bear_put_spread",
        ]),
        default_spread_width=struct.get("default_spread_width", 200),
    )

    # Risk
    risk = raw.get("risk", {})
    config.risk = RiskConfig(
        max_positions=risk.get("max_positions", 10),
        max_margin_per_trade=risk.get("max_margin_per_trade", 500_000),
        max_total_margin=risk.get("max_total_margin", 20_00_000),
        max_loss_per_trade_pct=risk.get("max_loss_per_trade_pct", 2.0),
    )

    # Per-strategy overrides
    config.strategy_overrides = raw.get("strategy_overrides", {})

    return config
