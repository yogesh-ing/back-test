"""Segment model — capital partitions mapped to brokers (PRD-001 Phase B).

A **Segment** is a user-defined partition of capital with a dedicated
broker and risk envelope, loaded from ``config/segments.yaml``:

* 1:1 mapping to a bucket-like risk envelope (the per-bucket breaker
  machinery is reused with the segment name as the bucket key — Phase D);
* runners are created INTO a segment (or with an explicit
  ``execution_broker`` override);
* paper mode ignores the broker assignment for execution (paper broker
  everywhere) — the broker field still names where the capital will live
  once the segment flips to live.

The loader is fail-soft at boot (a broken YAML never blocks the platform;
it logs and reports errors through :attr:`SegmentsConfig.errors`) but the
VALIDATION itself is fail-closed: an unknown broker or a bad mode is an
error, and runner creation into an invalid/unknown segment is refused.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

__all__ = [
    "SegmentConfig",
    "SegmentsConfig",
    "load_segments",
    "get_segments_config",
    "reset_segments_config",
    "resolve_execution_broker",
    "known_broker_names",
    "SegmentError",
]

logger = logging.getLogger("backtest.brokers.segments")

#: <root>/config/segments.yaml (this file is <root>/src/backtest/brokers/…).
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_PATH = _PROJECT_ROOT / "config" / "segments.yaml"

_ENV_PATH = "SEGMENTS_CONFIG_PATH"

VALID_MODES = ("paper", "live")


class SegmentError(ValueError):
    """A segment definition or reference is invalid (fail-closed)."""


def known_broker_names() -> set[str]:
    """Broker names the platform can execute through (session registry)."""
    try:
        from backtest.brokers.session_manager import _BROKER_REGISTRY

        return set(_BROKER_REGISTRY)
    except Exception:  # noqa: BLE001 — segments must load even without brokers
        return set()


@dataclass(frozen=True)
class SegmentConfig:
    """One capital partition: broker + mode + capital + risk envelope."""

    name: str
    display_name: str
    broker: str
    mode: str = "paper"
    allocated_capital: float = 0.0
    risk: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "broker": self.broker,
            "mode": self.mode,
            "allocated_capital": self.allocated_capital,
            "risk": dict(self.risk),
        }


@dataclass(frozen=True)
class SegmentsConfig:
    """Parsed ``config/segments.yaml``: segments + data-broker routing."""

    segments: Mapping[str, SegmentConfig] = field(default_factory=dict)
    data_primary: Optional[str] = None
    data_fallback: Optional[str] = None
    errors: tuple = ()
    path: Optional[str] = None

    def get(self, name: str | None) -> Optional[SegmentConfig]:
        if not name:
            return None
        return self.segments.get(str(name).strip().lower())

    def for_broker(self, broker: str) -> list[SegmentConfig]:
        return [s for s in self.segments.values() if s.broker == broker]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "segments": [s.to_dict() for s in self.segments.values()],
            "data": {"primary": self.data_primary, "fallback": self.data_fallback},
            "errors": list(self.errors),
        }


def validate_segment(name: str, raw: Mapping[str, Any]) -> SegmentConfig:
    """Validate one raw segment mapping. Raises :class:`SegmentError`."""
    key = str(name or "").strip().lower()
    if not key:
        raise SegmentError("segment name required")
    if not isinstance(raw, Mapping):
        raise SegmentError(f"segment {key!r}: definition must be a mapping")

    broker = str(raw.get("broker") or "").strip().lower()
    if not broker:
        raise SegmentError(f"segment {key!r}: broker is required")
    known = known_broker_names()
    if known and broker not in known:
        raise SegmentError(
            f"segment {key!r}: unknown broker {broker!r} (known: {sorted(known)})"
        )

    mode = str(raw.get("mode") or "paper").strip().lower()
    if mode not in VALID_MODES:
        raise SegmentError(f"segment {key!r}: mode must be one of {VALID_MODES}, got {mode!r}")

    try:
        capital = float(raw.get("allocated_capital") or 0.0)
    except (TypeError, ValueError):
        raise SegmentError(f"segment {key!r}: allocated_capital must be a number")
    if capital < 0:
        raise SegmentError(f"segment {key!r}: allocated_capital must be >= 0")

    risk = raw.get("risk") or {}
    if not isinstance(risk, Mapping):
        raise SegmentError(f"segment {key!r}: risk must be a mapping")

    return SegmentConfig(
        name=key,
        display_name=str(raw.get("display_name") or key.replace("_", " ").title()),
        broker=broker,
        mode=mode,
        allocated_capital=capital,
        risk=dict(risk),
    )


def _config_path(path: str | os.PathLike | None = None) -> Path:
    if path is not None:
        return Path(path)
    override = os.getenv(_ENV_PATH)
    return Path(override) if override else DEFAULT_PATH


def load_segments(path: str | os.PathLike | None = None) -> SegmentsConfig:
    """Parse the segments file. Fail-soft: bad entries land in ``errors``.

    A missing file is a valid empty config (single-broker V1 behaviour).
    """
    resolved = _config_path(path)
    if not resolved.exists():
        return SegmentsConfig(path=str(resolved))

    try:
        import yaml

        raw = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001 — a broken file must not block boot
        logger.error("segments config unreadable: %s (%s)", resolved, exc)
        return SegmentsConfig(errors=(f"unreadable: {exc}",), path=str(resolved))

    if not isinstance(raw, Mapping):
        return SegmentsConfig(errors=("top level must be a mapping",), path=str(resolved))

    segments: Dict[str, SegmentConfig] = {}
    errors: list[str] = []
    for name, entry in (raw.get("segments") or {}).items():
        try:
            seg = validate_segment(name, entry)
            segments[seg.name] = seg
        except SegmentError as exc:
            errors.append(str(exc))
            logger.error("segments config: %s", exc)

    data = raw.get("data") or {}
    primary = str(data.get("primary") or "").strip().lower() or None
    fallback = str(data.get("fallback") or "").strip().lower() or None
    known = known_broker_names()
    for label, value in (("data.primary", primary), ("data.fallback", fallback)):
        if value and known and value not in known:
            errors.append(f"{label}: unknown broker {value!r}")
            logger.error("segments config: %s: unknown broker %r", label, value)

    return SegmentsConfig(
        segments=segments,
        data_primary=primary,
        data_fallback=fallback,
        errors=tuple(errors),
        path=str(resolved),
    )


# ---------------------------------------------------------------------------
# Process-wide cached config
# ---------------------------------------------------------------------------

_cached: SegmentsConfig | None = None
_cache_lock = threading.Lock()


def get_segments_config(refresh: bool = False) -> SegmentsConfig:
    """The cached segments config (loaded once; ``refresh=True`` reloads)."""
    global _cached
    with _cache_lock:
        if _cached is None or refresh:
            _cached = load_segments()
        return _cached


def reset_segments_config() -> None:
    """Drop the cache (tests / config edits)."""
    global _cached
    with _cache_lock:
        _cached = None


# ---------------------------------------------------------------------------
# Execution-broker resolution (PRD §4.3 rules)
# ---------------------------------------------------------------------------


def resolve_execution_broker(
    mode: str,
    segment: str | None = None,
    execution_broker: str | None = None,
    config: SegmentsConfig | None = None,
) -> Optional[str]:
    """The broker name a runner's orders must route to (fail-closed).

    Rules (PRD-001 §4.3):

    * ``mode=paper``                      → ``None`` (PaperBroker, broker ignored)
    * ``mode=live`` + ``execution_broker``→ that broker (explicit override wins)
    * ``mode=live`` + ``segment``         → the segment's broker
    * anything unknown                    → :class:`SegmentError` — NEVER a
      silent fallback to another broker.
    """
    mode = str(mode or "paper").strip().lower()
    cfg = config if config is not None else get_segments_config()

    seg_key = str(segment).strip().lower() if segment else None
    if seg_key:
        if cfg.get(seg_key) is None:
            raise SegmentError(f"unknown segment: {segment!r}")

    broker = str(execution_broker).strip().lower() if execution_broker else None
    if broker:
        known = known_broker_names()
        if known and broker not in known:
            raise SegmentError(f"unknown execution broker: {execution_broker!r}")

    if mode != "live":
        return None
    if broker:
        return broker
    if seg_key:
        return cfg.get(seg_key).broker
    return None
