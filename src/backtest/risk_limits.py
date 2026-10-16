"""Layer-1 risk limits — the Risk Gate's rulebook (Plan v2.1 §7.1, Phase 0).

Home: ``config/risk_limits.yaml``, loaded and validated once at process
start. **Fail closed:** an unreadable/invalid file raises
:class:`RiskLimitsError`; Phase 1 wires that to "option runners refuse to
spawn". The loader also performs the *overlap check*: keys that would
shadow the existing drawdown/daily-loss surfaces (``GlobalRiskConfig``,
bucket limits in ``config/forward_testing.yaml``) are refused — one
value lives in exactly one file.

This module only *loads policy*. Enforcement is the Pre-Trade Risk
Gate's job (Phase 1, evolving ``options/execution_gate.py``).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "RiskLimitsError",
    "RiskLimits",
    "DEFAULT_RISK_LIMITS_PATH",
    "load_risk_limits",
]

DEFAULT_RISK_LIMITS_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "risk_limits.yaml"
)

_SCHEMA_VERSION = 1

#: Keys owned by other surfaces; refusing them here prevents two files
#: silently disagreeing about the same limit (plan §7.1 overlap rule).
_OVERLAP_KEYS = frozenset(
    {
        "max_drawdown_pct",
        "daily_loss_limit_pct",
        "daily_loss_limit",
        "max_drawdown",
        "buckets",
        "bucket_limits",
        "global_risk",
    }
)

_TOP_LEVEL_KEYS = frozenset(
    {"schema_version", "per_trade_max_loss_inr", "per_runner", "entitlement", "kill_switch"}
)

_KNOWN_STRUCTURES = frozenset(
    {
        "long_call",
        "long_put",
        "bull_call_spread",
        "bear_put_spread",
        "straddle",
        "strangle",
        "iron_condor",
        "calendar_spread",
    }
)


class RiskLimitsError(ValueError):
    """The risk-limits file is unreadable, invalid, or overlapping. Fail closed."""


@dataclass(frozen=True)
class RiskLimits:
    per_trade_max_loss_inr: float
    per_runner_max_lots: int
    per_runner_max_order_value_inr: float
    entitlement: dict[str, frozenset[str]]  # mode -> permitted structures
    kill_switch_engaged_on_boot: bool
    sha256: str  # of the file as loaded (stamped into spawn snapshots, §8)
    schema_version: int = _SCHEMA_VERSION

    def permitted_structures(self, mode: str) -> frozenset[str]:
        """Structures this mode may trade; an unlisted mode permits nothing."""
        return self.entitlement.get(str(mode).strip().lower(), frozenset())


def _positive(value: Any, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise RiskLimitsError(f"{name} must be a number, got {value!r}") from exc
    if number <= 0:
        raise RiskLimitsError(f"{name} must be > 0, got {value!r}")
    return number


def load_risk_limits(path: str | Path | None = None) -> RiskLimits:
    """Load + validate ``config/risk_limits.yaml``. Raises RiskLimitsError."""
    limits_path = Path(path) if path is not None else DEFAULT_RISK_LIMITS_PATH
    try:
        raw_bytes = limits_path.read_bytes()
    except OSError as exc:
        raise RiskLimitsError(
            f"risk limits file unreadable at {limits_path}: {exc} — failing closed"
        ) from exc
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - PyYAML is a hard requirement
        raise RiskLimitsError("PyYAML is required to load risk limits") from exc
    try:
        data = yaml.safe_load(raw_bytes.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 — any parse failure is fail-closed
        raise RiskLimitsError(f"risk limits file is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise RiskLimitsError("risk limits file must contain a YAML mapping")

    overlap = set(data) & _OVERLAP_KEYS
    if overlap:
        raise RiskLimitsError(
            f"risk limits file may not set {sorted(overlap)}: those limits live "
            "in GlobalRiskConfig / config/forward_testing.yaml (one value, one file)"
        )
    unknown = set(data) - _TOP_LEVEL_KEYS
    if unknown:
        raise RiskLimitsError(f"unknown risk-limits keys: {sorted(unknown)}")
    if int(data.get("schema_version", 0) or 0) != _SCHEMA_VERSION:
        raise RiskLimitsError(
            f"risk limits schema_version must be {_SCHEMA_VERSION}, "
            f"got {data.get('schema_version')!r}"
        )
    if "per_trade_max_loss_inr" not in data:
        raise RiskLimitsError("risk limits require per_trade_max_loss_inr")
    per_trade = _positive(data["per_trade_max_loss_inr"], "per_trade_max_loss_inr")

    per_runner = data.get("per_runner") or {}
    if not isinstance(per_runner, dict):
        raise RiskLimitsError("per_runner must be a mapping")
    unknown_runner = set(per_runner) - {"max_lots_per_trade", "max_order_value_inr"}
    if unknown_runner:
        raise RiskLimitsError(f"unknown per_runner keys: {sorted(unknown_runner)}")
    max_lots = per_runner.get("max_lots_per_trade")
    if max_lots is None or int(max_lots) < 1:
        raise RiskLimitsError("per_runner.max_lots_per_trade must be >= 1")
    max_order = _positive(
        per_runner.get("max_order_value_inr"), "per_runner.max_order_value_inr"
    )

    entitlement_raw = data.get("entitlement")
    if not isinstance(entitlement_raw, dict) or not entitlement_raw:
        raise RiskLimitsError("entitlement must map mode -> [structures] and be non-empty")
    entitlement: dict[str, frozenset[str]] = {}
    for mode, structures in entitlement_raw.items():
        if not isinstance(structures, (list, tuple)) or not structures:
            raise RiskLimitsError(f"entitlement.{mode} must be a non-empty list")
        names = frozenset(str(s).strip().lower() for s in structures)
        bad = names - _KNOWN_STRUCTURES
        if bad:
            raise RiskLimitsError(
                f"entitlement.{mode} names unknown structures: {sorted(bad)}"
            )
        entitlement[str(mode).strip().lower()] = names

    kill = data.get("kill_switch") or {}
    if not isinstance(kill, dict) or set(kill) - {"engaged_on_boot"}:
        raise RiskLimitsError("kill_switch may only set engaged_on_boot")

    return RiskLimits(
        per_trade_max_loss_inr=per_trade,
        per_runner_max_lots=int(max_lots),
        per_runner_max_order_value_inr=max_order,
        entitlement=entitlement,
        kill_switch_engaged_on_boot=bool(kill.get("engaged_on_boot", False)),
        sha256=hashlib.sha256(raw_bytes).hexdigest(),
    )
