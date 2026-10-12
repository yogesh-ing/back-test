"""Segments API — capital partitions mapped to brokers (PRD-001 Phase B).

* ``GET  /api/segments``           — list configured segments + data routing
* ``POST /api/segments``           — validate (and optionally persist) one
                                     segment definition (V1: config-file CRUD)
* ``POST /api/segments/reload``    — re-read ``config/segments.yaml``

V1 keeps the config file as the source of truth; ``POST`` validates the
payload with the same fail-closed rules the loader uses (unknown broker →
refusal) and, when ``persist: true``, writes it back to the YAML file.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from backtest.brokers.segments import (
    SegmentError,
    get_segments_config,
    reset_segments_config,
    validate_segment,
)
from backtest.logging_config import get_logger

__all__ = ["segments_bp"]

logger = get_logger(__name__)

segments_bp = Blueprint("segments_api", __name__)


@segments_bp.get("/api/segments")
def list_segments() -> tuple:
    """Every configured segment + the data-broker routing block."""
    cfg = get_segments_config()
    payload = cfg.to_dict()
    payload["success"] = True
    return jsonify(payload), 200


@segments_bp.post("/api/segments/reload")
def reload_segments() -> tuple:
    """Re-read the segments config file (after a manual edit)."""
    reset_segments_config()
    cfg = get_segments_config(refresh=True)
    return jsonify({"success": True, **cfg.to_dict()}), 200


@segments_bp.post("/api/segments")
def upsert_segment() -> tuple:
    """Validate one segment definition; persist it when ``persist: true``.

    Body::

        {
          "segment_id": "custom_segment",
          "display_name": "My Custom Segment",
          "broker": "mstock",
          "mode": "paper",
          "allocated_capital": 500000,
          "risk": {"daily_loss_limit": 25000, "max_drawdown_pct": 10},
          "persist": false
        }

    Fail-closed: an unknown broker or bad mode is a 400 — a segment that
    cannot route orders is never accepted.
    """
    data = request.get_json(silent=True) or {}
    name = str(data.get("segment_id") or data.get("name") or "").strip().lower()
    if not name:
        return jsonify({"success": False, "message": "segment_id is required"}), 400
    raw = {
        "display_name": data.get("display_name"),
        "broker": data.get("broker"),
        "mode": data.get("mode", "paper"),
        "allocated_capital": data.get("allocated_capital", 0),
        "risk": data.get("risk") or data.get("risk_config") or {},
    }
    try:
        seg = validate_segment(name, raw)
    except SegmentError as exc:
        return jsonify({"success": False, "message": str(exc)}), 400

    persisted = False
    if bool(data.get("persist")):
        try:
            persisted = _persist_segment(seg)
        except Exception:  # noqa: BLE001 — a persist failure is reported, not raised
            logger.exception("segment persist failed for %s", name)
            return (
                jsonify(
                    {
                        "success": False,
                        "message": "segment is valid but writing config/segments.yaml failed",
                        "segment": seg.to_dict(),
                    }
                ),
                500,
            )
        reset_segments_config()
        get_segments_config(refresh=True)

    return (
        jsonify({"success": True, "segment": seg.to_dict(), "persisted": persisted}),
        201 if persisted else 200,
    )


def _persist_segment(seg) -> bool:
    """Write/replace one segment entry in the YAML config (atomic-ish)."""
    import yaml

    from backtest.brokers.segments import _config_path

    path = _config_path()
    raw = {}
    if path.exists():
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raw = {}
    raw.setdefault("segments", {})
    raw["segments"][seg.name] = {
        "display_name": seg.display_name,
        "broker": seg.broker,
        "mode": seg.mode,
        "allocated_capital": seg.allocated_capital,
        **({"risk": dict(seg.risk)} if seg.risk else {}),
    }
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8")
    tmp.replace(path)
    logger.info("segment %s persisted to %s", seg.name, path)
    return True
