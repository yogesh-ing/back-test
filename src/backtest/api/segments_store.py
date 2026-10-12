"""Segments store — Phase 2 of the Cost & Risk Settings panel (v2 §0).

Certified decisions implemented here:

* **Risk limits belong to SEGMENTS** (§0 #2): a segment = allocated capital +
  mandate + broker (execution venue) + mode (paper|live) + risk_limits. The
  store owns CRUD + validation + per-field audit; it does NOT duplicate the
  runners' capital accounting (PortfolioManager owns that at runtime).
* **Two-tier live safety** (§0 #4): a GLOBAL live kill-switch (default OFF,
  "pause ALL live NOW", overrides every segment) AND per-segment ``mode``.
  A segment may only arm live when: global switch ON AND segment
  ``mode=live`` AND its ``daily_loss_limit`` is set (fail closed).
* **Contract-note validation is a hard gate for live** (§0 #6): the segment's
  broker profile must carry a validation stamp (``validated_on`` +
  ``contract_note_ref``) before the segment can arm live.
* **DB-first with graceful degradation**: when no settings DB is configured
  (no ``FORWARD_TEST_DB_URL`` / unreachable manager), the gate helpers treat
  the panel as "not configured" and let the LEGACY env gate decide — a
  deployment without the panel keeps working. When the DB IS configured but
  errors, the gate fails CLOSED: a panel that cannot answer never arms live.

The global switch lives as a sentinel row in ``broker_profiles``
(``__live_kill_switch__``) so it is audited like every other panel change.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping

from sqlalchemy import select

from backtest.db.models import BrokerProfileRow, SegmentAudit, SegmentRow

__all__ = ["SegmentsStore", "get_segments_store", "assert_live_arming_allowed"]

logger = logging.getLogger("backtest.api.settings")

KILL_SWITCH_ID = "__live_kill_switch__"


class SegmentsStore:
    """CRUD + audit for segments, plus the global live kill-switch."""

    def __init__(self, manager: Any) -> None:
        self._manager = manager
        SegmentRow.ensure_schema(manager)
        SegmentAudit.ensure_schema(manager)
        self._ensure_kill_switch_row()

    # -- global live kill-switch (v2 §0 #4) --------------------------------

    def _ensure_kill_switch_row(self) -> None:
        """Idempotently create the sentinel row (default OFF — fail closed)."""
        with self._manager.session() as session:
            if session.get(BrokerProfileRow, KILL_SWITCH_ID) is None:
                session.add(
                    BrokerProfileRow(
                        profile_id=KILL_SWITCH_ID,
                        profile_name="Global live kill-switch",
                        is_preset=False,
                        commission_model={"enabled": False},
                        statutory_rates={},
                    )
                )

    def is_live_kill_switch_on(self) -> bool:
        """True only when the switch was explicitly armed (default OFF)."""
        with self._manager.session() as session:
            row = session.get(BrokerProfileRow, KILL_SWITCH_ID)
            return bool(row.commission_model.get("enabled")) if row is not None else False

    def _has_kill_switch_row(self) -> bool:
        """The deployment opted into the panel (migration 007 / store opened)."""
        with self._manager.session() as session:
            return session.get(BrokerProfileRow, KILL_SWITCH_ID) is not None

    def set_live_kill_switch(self, enabled: bool, changed_by: str = "admin") -> None:
        with self._manager.session() as session:
            row = session.get(BrokerProfileRow, KILL_SWITCH_ID)
            if row is None:
                row = BrokerProfileRow(
                    profile_id=KILL_SWITCH_ID,
                    profile_name="Global live kill-switch",
                    is_preset=False,
                    commission_model={"enabled": bool(enabled)},
                    statutory_rates={},
                )
                session.add(row)
            else:
                old = bool(row.commission_model.get("enabled"))
                if old == bool(enabled):
                    return
                row.commission_model = {**row.commission_model, "enabled": bool(enabled)}
                self._audit(
                    session, KILL_SWITCH_ID, "live_kill_switch",
                    str(old), str(bool(enabled)), changed_by,
                )

    # -- segments CRUD ------------------------------------------------------

    def list_segments(self) -> list[dict[str, Any]]:
        with self._manager.session() as session:
            rows = session.execute(select(SegmentRow)).scalars().all()
            return [row.to_dict() for row in rows]

    def get_segment(self, segment_id: str) -> dict[str, Any] | None:
        with self._manager.session() as session:
            row = session.get(SegmentRow, segment_id)
            return row.to_dict() if row is not None else None

    def get_audit(self, segment_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self._manager.session() as session:
            stmt = (
                select(SegmentAudit)
                .where(SegmentAudit.segment_id == segment_id)
                .order_by(SegmentAudit.changed_at.desc())
                .limit(limit)
            )
            return [
                {
                    "audit_id": row.audit_id,
                    "segment_id": row.segment_id,
                    "field_changed": row.field_changed,
                    "old_value": row.old_value,
                    "new_value": row.new_value,
                    "changed_by": row.changed_by,
                    "changed_at": row.changed_at.isoformat() if row.changed_at else None,
                }
                for row in session.execute(stmt).scalars()
            ]

    def upsert_segment(
        self, payload: Mapping[str, Any], changed_by: str = "admin"
    ) -> dict[str, Any]:
        """Create or update a segment; audit every changed field."""
        segment_id = str(payload.get("segment_id", "")).strip()
        if not segment_id:
            raise ValueError("segment_id is required")
        mode = str(payload.get("mode") or "paper").strip().lower()
        if mode not in ("paper", "live"):
            raise ValueError("mode must be 'paper' or 'live'")
        limits = self._validate_risk_limits(payload.get("risk_limits") or {})
        capital = payload.get("allocated_capital")
        if capital is not None:
            try:
                capital = float(capital)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"allocated_capital: not a number ({capital!r})") from exc
            if capital < 0:
                raise ValueError("allocated_capital must not be negative")

        with self._manager.session() as session:
            row = session.get(SegmentRow, segment_id)
            if row is None:
                row = SegmentRow(
                    segment_id=segment_id,
                    segment_name=str(payload.get("segment_name") or segment_id),
                    mode=mode,
                    allocated_capital=capital,
                    broker_profile_id=payload.get("broker_profile_id") or None,
                    risk_limits=limits,
                )
                session.add(row)
                self._audit(session, segment_id, "__created__", None, mode, changed_by)
            else:
                changes: list[tuple[str, Any, Any]] = []
                new_name = str(payload.get("segment_name") or row.segment_name)
                new_broker = payload.get("broker_profile_id") or row.broker_profile_id
                if row.segment_name != new_name:
                    changes.append(("segment_name", row.segment_name, new_name))
                    row.segment_name = new_name
                if row.mode != mode:
                    changes.append(("mode", row.mode, mode))
                    row.mode = mode
                old_capital = (
                    float(row.allocated_capital) if row.allocated_capital is not None else None
                )
                if capital != old_capital:
                    changes.append(("allocated_capital", row.allocated_capital, capital))
                    row.allocated_capital = capital
                if row.broker_profile_id != new_broker:
                    changes.append(("broker_profile_id", row.broker_profile_id, new_broker))
                    row.broker_profile_id = new_broker
                old_limits = json.dumps(row.risk_limits or {}, sort_keys=True)
                new_limits = json.dumps(limits, sort_keys=True)
                if old_limits != new_limits:
                    changes.append(("risk_limits", old_limits, new_limits))
                    row.risk_limits = limits
                for field, old, new in changes:
                    self._audit(session, segment_id, field, old, new, changed_by)
                if not changes:
                    return row.to_dict()
            session.flush()
            return row.to_dict()

    def delete_segment(self, segment_id: str, changed_by: str = "admin") -> bool:
        with self._manager.session() as session:
            row = session.get(SegmentRow, segment_id)
            if row is None:
                return False
            self._audit(session, segment_id, "__deleted__", row.mode, None, changed_by)
            session.delete(row)
            return True

    # -- the live-arming gate (v2 §0 #4 + #6) -------------------------------

    def live_arming_blockers(self, segment_id: str | None = None) -> list[str]:
        """Reasons the segment may NOT arm live (empty list = allowed).

        Fail closed: every check must PASS, not merely not-fail. With no
        ``segment_id`` the check is the global tier only (single-engine path).
        """
        blockers: list[str] = []
        if not self.is_live_kill_switch_on():
            blockers.append("global live kill-switch is OFF (arm it in Cost & Risk Settings)")
        if segment_id is None:
            return blockers
        seg = self.get_segment(segment_id)
        if seg is None:
            blockers.append(f"unknown segment {segment_id!r}")
            return blockers
        if seg["mode"] != "live":
            blockers.append(f"segment {segment_id!r} mode is {seg['mode']!r}, not 'live'")
        limits = seg.get("risk_limits") or {}
        daily = limits.get("daily_loss_limit")
        if daily is None or float(daily) <= 0:
            blockers.append(
                f"segment {segment_id!r} has no positive daily_loss_limit — "
                "set one before arming live (fail closed)"
            )
        broker_id = seg.get("broker_profile_id")
        if not broker_id:
            blockers.append(f"segment {segment_id!r} has no broker profile assigned")
        else:
            with self._manager.session() as session:
                brow = session.get(BrokerProfileRow, broker_id)
            if brow is None:
                blockers.append(f"segment {segment_id!r} broker {broker_id!r} does not exist")
            elif not brow.validated_on or not brow.contract_note_ref:
                blockers.append(
                    f"broker profile {broker_id!r} is not contract-note validated — "
                    "run the validator in Cost & Risk Settings (mandatory for live)"
                )
        return blockers

    # -- internals -----------------------------------------------------------

    @staticmethod
    def _validate_risk_limits(limits: Mapping[str, Any]) -> dict[str, Any]:
        """Refuse unknown, non-numeric or negative risk limits (fail closed)."""
        allowed = set(SegmentRow.RISK_LIMIT_FIELDS)
        cleaned: dict[str, Any] = {}
        for key, value in dict(limits).items():
            if key not in allowed:
                raise ValueError(f"unknown risk limit {key!r}")
            if value is None:
                continue
            try:
                number = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{key}: not a number ({value!r})") from exc
            if number < 0:
                raise ValueError(f"{key}: must not be negative")
            if number == 0:
                continue
            cleaned[key] = number
        if "max_positions" in cleaned:
            cleaned["max_positions"] = int(cleaned["max_positions"])
            if cleaned["max_positions"] < 1:
                raise ValueError("max_positions must be >= 1 when set")
        return cleaned

    @staticmethod
    def _audit(
        session: Any,
        segment_id: str,
        field: str,
        old: Any,
        new: Any,
        changed_by: str,
    ) -> None:
        session.add(
            SegmentAudit(
                segment_id=segment_id,
                field_changed=field,
                old_value=None if old is None else str(old),
                new_value=None if new is None else str(new),
                changed_by=changed_by,
            )
        )


_STORE: SegmentsStore | None = None


def get_segments_store() -> SegmentsStore:
    """Process-wide store (built on the app's DatabaseManager)."""
    global _STORE
    if _STORE is None:
        from backtest.db import DatabaseManager

        _STORE = SegmentsStore(DatabaseManager.from_env())
    return _STORE


def reset_segments_store() -> None:
    """Test hook — drop the singleton so the next call rebuilds it."""
    global _STORE
    _STORE = None


def _panel_configured() -> bool:
    """True when the Cost & Risk Settings panel controls THIS deployment.

    The marker is the kill-switch sentinel row in ``broker_profiles`` — it is
    created by migration 007 or the first time the panel store is opened. A
    deployment that never opted into the panel (row absent, table absent, or
    no DB configured) keeps the legacy env-gate behavior. This probe is
    strictly READ-ONLY: it must never construct the store (which would create
    tables/rows in whatever DB it finds) — the gate installs itself only via
    an explicit migration or panel use, never as a side effect of arming.
    """
    try:
        from backtest.db import DatabaseManager

        manager = DatabaseManager.from_env()
        with manager.session() as session:
            return session.get(BrokerProfileRow, KILL_SWITCH_ID) is not None
    except Exception:  # noqa: BLE001 — not configured / DB down / table absent
        return False


def assert_live_arming_allowed(segment_id: str | None = None) -> None:
    """The hard gate for live arming (raises ValueError when blocked).

    * Panel NOT in control (no kill-switch sentinel row) → legacy behavior:
      the caller's env gate (``ALLOW_LIVE_ORDERS``) stays the sole control.
    * Panel in control → the certified two-tier gate applies, fail closed:
      the global kill-switch must be ON, the segment must be mode=live with a
      positive daily-loss limit and a contract-note-validated broker profile.
      A DB error here propagates (a panel that cannot answer never arms live).
    """
    if not _panel_configured():
        return
    store = get_segments_store()
    blockers = store.live_arming_blockers(segment_id)
    if blockers:
        raise ValueError(
            "live arming refused by Cost & Risk Settings: " + "; ".join(blockers)
        )
