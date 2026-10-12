"""Broker profile store — DB-first persistence for the Cost & Risk Settings panel.

Phase 1 of the architect-certified design (docs/drafts/BROKER-CAPITAL-SETTINGS-PANEL.md §0):

* **DB-first**: panel edits land in ``broker_profiles`` (+ audit rows); the fee
  engine reads profiles from here at runner-construction time.
* **YAML bootstrap**: on first use the store seeds itself from
  ``config/brokers.yaml`` (or the built-in presets when the file is absent) so
  a fresh deployment starts with the same rates the yaml path produced.
* **YAML is also part of the catalogue**: a broker that is defined only in
  ``config/brokers.yaml`` (e.g. ``dhan``) is listed and editable, not invisible.
  Editing it writes a DB row — an audited override, exactly like a preset.
  Until then the yaml file stays the source of truth for it, so editing the
  file still takes effect (a seeded DB row would have frozen the old rates).
* **Running runners are never mutated** (v2 §0): profiles are snapshotted into
  a :class:`~backtest.simulator.fees.CommissionCalculator` at construction;
  edits apply to runs started afterwards.

Every mutation writes ``broker_profile_audit`` rows (who/when/old→new) — the
tax-audit trail the contract-note validator stamps into.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy import select

from backtest.db.models import BrokerProfileAudit, BrokerProfileRow
from backtest.simulator.fees import (
    BROKER_PRESETS,
    IndiaEquityFees,
)

__all__ = ["BrokerProfileStore", "get_broker_profile_store"]

logger = logging.getLogger("backtest.api.settings")

CANONICAL_BROKER_NAMES: dict[str, str] = {
    "zerodha": "Zerodha Kite",
    "mstock": "Mirae Asset mStock",
    "dhan": "Dhan HQ",
    "upstox": "Upstox",
    "groww": "Groww",
    "angelone": "Angel One",
    "india_full_service": "India Full Service (0.5%)",
    "india_zero": "India Zero Commission",
    "generic_discount": "Generic Discount",
    "zero": "Zero Commission",
    "ibkr": "Interactive Brokers (IBKR)",
    "td_ameritrade": "TD Ameritrade",
    "robinhood": "Robinhood",
}

TEST_FIXTURE_PROFILES: frozenset[str] = frozenset({"panel_broker", "test_broker", "expensive"})


def _yaml_broker_names() -> list[str]:
    """Broker ids defined in ``config/brokers.yaml`` (may exceed the presets)."""
    from backtest.simulator.fees import DEFAULT_BROKER_CONFIG_PATH

    path = Path(DEFAULT_BROKER_CONFIG_PATH)
    if not path.exists():
        return []
    try:
        import yaml

        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001 — an unreadable file is not a panel error
        logger.warning("could not read %s for the broker catalogue (%s)", path, exc)
        return []
    brokers = document.get("brokers") if isinstance(document, Mapping) else None
    return sorted(str(name) for name in (brokers or {}))


def _row_from_profile(name: str, profile: Any, *, origin: str) -> dict[str, Any]:
    """One catalogue row: the same shape whether it came from yaml or a preset."""
    schedule = profile.fee_schedule
    rates: dict[str, Any] = {}
    if isinstance(schedule, IndiaEquityFees):
        rates = {
            "stt_delivery": str(schedule.stt_delivery),
            "stt_intraday_sell": str(schedule.stt_intraday_sell),
            "exchange_txn_equity": str(schedule.exchange_txn_equity),
            "sebi_turnover": str(schedule.sebi_turnover),
            "ipft": str(schedule.ipft),
            "stamp_duty_delivery": str(schedule.stamp_duty_delivery),
            "stamp_duty_intraday": str(schedule.stamp_duty_intraday),
            "gst_rate": str(schedule.gst_rate),
            "dp_charges": str(schedule.dp_charges),
        }
    commission = profile.commission_model.to_dict()
    delivery = (
        profile.delivery_commission_model.to_dict()
        if profile.delivery_commission_model is not None
        else None
    )
    options = (
        profile.options_commission_model.to_dict()
        if profile.options_commission_model is not None
        else None
    )
    return {
        "profile_id": name,
        "profile_name": CANONICAL_BROKER_NAMES.get(name.lower(), name.replace("_", " ").title()),
        "is_preset": origin == "preset",
        "origin": origin,
        "currency": profile.currency,
        "default_segment": profile.default_segment,
        "commission_model": {
            "default": commission,
            **({"delivery": delivery} if delivery else {}),
            **({"options": options} if options else {}),
        },
        "statutory_rates": rates,
        "minimum_commission": (
            float(profile.minimum_commission)
            if profile.minimum_commission is not None
            else None
        ),
    }


def _yaml_rows() -> list[dict[str, Any]]:
    """Catalogue rows for brokers defined in ``config/brokers.yaml``.

    The yaml path bypasses the DB on purpose: this is what the *file* says, so
    the panel can offer a broker the fee engine already knows how to price even
    when no DB row exists yet (``dhan``, before its first edit).
    """
    from backtest.simulator.fees import DEFAULT_BROKER_CONFIG_PATH, load_broker_profile

    rows: list[dict[str, Any]] = []
    for name in _yaml_broker_names():
        try:
            profile = load_broker_profile(path=DEFAULT_BROKER_CONFIG_PATH, broker=name)
        except Exception as exc:  # noqa: BLE001 — one bad entry must not hide the rest
            logger.warning("could not read broker %r from yaml (%s)", name, exc)
            continue
        rows.append(_row_from_profile(name, profile, origin="yaml"))
    return rows


def _preset_rows() -> list[dict[str, Any]]:
    """Catalogue rows for every built-in preset."""
    return [
        _row_from_profile(name, factory(), origin="preset")
        for name, factory in sorted(BROKER_PRESETS.items())
    ]


def _seed_rows() -> list[dict[str, Any]]:
    """Rows for a fresh database: ``config/brokers.yaml`` wins where it speaks.

    The file is the thing a human edits, so a fresh deployment must start with
    what the file says rather than with a built-in default that silently
    disagreed with it. Brokers the file does not mention keep their preset.
    """
    rows = {row["profile_id"]: row for row in _preset_rows()}
    for row in _yaml_rows():
        merged = dict(row)
        merged["is_preset"] = merged["profile_id"] in BROKER_PRESETS
        rows[merged["profile_id"]] = merged
    return [rows[key] for key in sorted(rows)]


#: Actor recorded when the seed pass re-applies the file to a cached row.
YAML_SYNC_ACTOR = "yaml-sync"

#: Fields that decide whether a stored row still says what its source says.
_COMPARED_FIELDS = (
    "statutory_rates",
    "commission_model",
    "minimum_commission",
    "currency",
    "default_segment",
)


def _normalised(value: Any) -> Any:
    """Compare rates across yaml/DB without caring how a number was written."""
    if isinstance(value, dict):
        return {key: _normalised(item) for key, item in sorted(value.items())}
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return value
    return value


def _diverges(stored: Mapping[str, Any], reference: Mapping[str, Any] | None) -> bool:
    """True when a stored row disagrees with the file/preset it came from."""
    if not reference:
        return False
    return any(
        _normalised(stored.get(field)) != _normalised(reference.get(field))
        for field in _COMPARED_FIELDS
    )


class BrokerProfileStore:
    """CRUD + audit for broker cost-model profiles, DB-backed."""

    def __init__(self, manager: Any) -> None:
        self._manager = manager
        BrokerProfileRow.ensure_schema(manager)
        BrokerProfileAudit.ensure_schema(manager)

    # -- active broker selector (certified Phase 1) ------------------------

    def get_active_broker(self) -> str | None:
        """The panel-selected active broker, or None (yaml ``active_broker`` wins)."""
        with self._manager.session() as session:
            row = session.get(BrokerProfileRow, "__active__")
            if row is None or row.commission_model.get("active") is None:
                return None
            val = str(row.commission_model["active"]).strip()
            if not val or val in TEST_FIXTURE_PROFILES:
                return None
            return val

    def set_active_broker(self, profile_id: str, changed_by: str = "admin") -> None:
        """Record the panel's active-broker choice (audit-trailed).

        Uses a sentinel row (``__active__``) rather than an is_active flag so
        the choice itself is audited like any other profile change.
        """
        with self._manager.session() as session:
            row = session.get(BrokerProfileRow, profile_id)
            if row is None and self.get_catalogue_profile(profile_id) is None:
                # A broker the fee engine can price (yaml/preset) is selectable
                # even before it has a DB row; anything else is a typo.
                raise ValueError(f"unknown profile {profile_id!r}")
            marker = session.get(BrokerProfileRow, "__active__")
            old = marker.commission_model.get("active") if marker is not None else None
            if marker is None:
                marker = BrokerProfileRow(
                    profile_id="__active__",
                    profile_name="Active broker selection",
                    is_preset=False,
                    commission_model={"active": profile_id},
                    statutory_rates={},
                )
                session.add(marker)
            else:
                marker.commission_model = {**marker.commission_model, "active": profile_id}
            self._audit(session, "__active__", "active_broker", old, profile_id, changed_by)

    # -- bootstrap ---------------------------------------------------------

    def seed_from_yaml(self) -> int:
        """Bootstrap the catalogue; re-sync untouched rows with the file.

        Inserting the missing rows is only half the job. A row that was *seeded*
        and never edited is a cache of ``config/brokers.yaml``, not a decision,
        and DB-first resolution would keep pricing runs with the old numbers
        forever once the file moved on. So rows the file defines, that nobody
        has touched, are re-applied from the file here (audited as
        ``yaml-sync``). A panel edit or a validation stamp makes a row a
        decision: it is left alone and the panel reports that it wins over the
        file.

        Returns the number of rows inserted (re-syncs are logged and audited).
        """
        file_rows = {row["profile_id"]: row for row in _yaml_rows()}
        added = 0
        refreshed: list[str] = []
        with self._manager.session() as session:
            existing = {
                row.profile_id: row
                for row in session.execute(select(BrokerProfileRow)).scalars()
            }
            for payload in _seed_rows():
                profile_id = payload["profile_id"]
                row = existing.get(profile_id)
                if row is None:
                    session.add(
                        BrokerProfileRow(
                            **{k: v for k, v in payload.items() if k != "origin"}
                        )
                    )
                    added += 1
                elif self._sync_cached_row(session, row, file_rows.get(profile_id)):
                    refreshed.append(profile_id)
        if added or refreshed:
            logger.info(
                "broker profiles from presets/yaml: %d seeded, re-synced %s",
                added,
                ", ".join(refreshed) if refreshed else "none",
            )
        return added

    def _sync_cached_row(self, session: Any, row: Any, file_row: Mapping[str, Any] | None) -> bool:
        """Re-apply the file to a row nobody has decided anything with."""
        if file_row is None or not row.is_preset or row.validated_on is not None:
            return False
        if self._is_edited(session, row.profile_id):
            return False
        # ``is_preset`` is lineage, not a rate: the file must not rewrite it.
        desired = {
            key: value
            for key, value in file_row.items()
            if key not in ("origin", "profile_id", "is_preset")
        }
        changed = [key for key, value in desired.items() if getattr(row, key) != value]
        if not changed:
            return False
        for key in changed:
            session.add(
                BrokerProfileAudit(
                    profile_id=row.profile_id,
                    field_changed=key,
                    old_value=str(getattr(row, key)),
                    new_value=str(desired[key]),
                    changed_by=YAML_SYNC_ACTOR,
                )
            )
            setattr(row, key, desired[key])
        return True

    @staticmethod
    def _is_edited(session: Any, profile_id: str) -> bool:
        """True when the panel changed something beyond seeding the row."""
        rows = session.execute(
            select(BrokerProfileAudit.field_changed, BrokerProfileAudit.changed_by).where(
                BrokerProfileAudit.profile_id == profile_id
            )
        ).all()
        return any(
            field != "__created__" and actor != YAML_SYNC_ACTOR for field, actor in rows
        )

    # -- reads -------------------------------------------------------------

    def list_profiles(self) -> list[dict[str, Any]]:
        with self._manager.session() as session:
            rows = session.execute(select(BrokerProfileRow)).scalars().all()
            return [self._row_to_dict(row) for row in rows]

    def get_profile(self, profile_id: str) -> dict[str, Any] | None:
        """A stored profile (DB row only — see :meth:`get_catalogue_profile`)."""
        with self._manager.session() as session:
            row = session.get(BrokerProfileRow, profile_id)
            return self._row_to_dict(row) if row is not None else None

    def list_catalogue(self) -> list[dict[str, Any]]:
        """Everything the panel may show: stored profiles + yaml-only brokers.

        A broker defined in ``config/brokers.yaml`` but never edited here has no
        DB row, and used to be invisible in the panel — so it could not be
        edited or contract-note validated either, even though the fee engine
        priced it. Those come from the file with ``origin='yaml'``; stored rows
        report ``origin='db'`` (seeded/edited) so the UI can say where a number
        actually comes from.
        """
        stored = {
            row["profile_id"]: {**row, "origin": "db"}
            for row in self.list_profiles()
            # ``__active__`` / ``__live_kill_switch__`` are internal marker rows
            # that share this table; they are state, not brokers to trade.
            if not str(row["profile_id"]).startswith("__")
            and str(row["profile_id"]) not in TEST_FIXTURE_PROFILES
        }
        file_rows = {
            row["profile_id"]: row for row in _yaml_rows()
            if row["profile_id"] not in TEST_FIXTURE_PROFILES
        }
        for profile_id, row in stored.items():
            # A stored row is not automatically an override: the seed copies
            # the file, so say whether the numbers in force are still the
            # file's (``matches_file``) or something the panel changed.
            reference = file_rows.get(profile_id)
            row["matches_file"] = bool(reference) and not _diverges(row, reference)
        for profile_id, row in file_rows.items():
            stored.setdefault(profile_id, row)
        return sorted(stored.values(), key=lambda row: row["profile_id"])

    def get_catalogue_profile(self, profile_id: str) -> dict[str, Any] | None:
        """One profile for the editor: the stored row, else the yaml definition.

        Internal marker rows (``__active__``, ``__live_kill_switch__``) share the
        table but are state, not brokers — they are never editable, selectable
        or validatable as a cost model.
        """
        if str(profile_id or "").startswith("__"):
            return None
        stored = self.get_profile(profile_id)
        if stored is not None:
            return {**stored, "origin": "db"}
        for row in _yaml_rows():
            if row["profile_id"] == profile_id:
                return row
        return None

    def get_audit(self, profile_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self._manager.session() as session:
            stmt = (
                select(BrokerProfileAudit)
                .where(BrokerProfileAudit.profile_id == profile_id)
                .order_by(BrokerProfileAudit.changed_at.desc())
                .limit(limit)
            )
            return [
                {
                    "audit_id": row.audit_id,
                    "profile_id": row.profile_id,
                    "field_changed": row.field_changed,
                    "old_value": row.old_value,
                    "new_value": row.new_value,
                    "changed_by": row.changed_by,
                    "changed_at": row.changed_at.isoformat() if row.changed_at else None,
                }
                for row in session.execute(stmt).scalars()
            ]

    # -- writes ------------------------------------------------------------

    def upsert_profile(
        self, payload: Mapping[str, Any], changed_by: str = "admin"
    ) -> dict[str, Any]:
        """Create or update a profile; audit every changed field.

        Preset rows may be edited (the audit trail records the override) but
        ``is_preset`` itself is immutable — an override keeps its lineage.
        """
        profile_id = str(payload.get("profile_id", "")).strip()
        if not profile_id:
            raise ValueError("profile_id is required")
        statutory = dict(payload.get("statutory_rates") or {})
        self._validate_rates(statutory)

        with self._manager.session() as session:
            row = session.get(BrokerProfileRow, profile_id)
            if row is None:
                row = BrokerProfileRow(
                    profile_id=profile_id,
                    profile_name=str(payload.get("profile_name") or profile_id),
                    is_preset=bool(payload.get("is_preset", False)),
                    currency=str(payload.get("currency") or "INR"),
                    default_segment=str(payload.get("default_segment") or "equity_delivery"),
                    commission_model=dict(payload.get("commission_model") or {}),
                    statutory_rates=statutory,
                    minimum_commission=payload.get("minimum_commission"),
                )
                session.add(row)
                self._audit(session, profile_id, "__created__", None, "new profile", changed_by)
            else:
                changes = self._apply_update(row, payload, statutory, changed_by, session)
                if not changes:
                    return self._row_to_dict(row)
            session.flush()
            return self._row_to_dict(row)

    def mark_validated(
        self,
        profile_id: str,
        document_id: str,
        when: date | None = None,
        changed_by: str = "admin",
    ) -> None:
        """Stamp a profile with the contract-note validation (v2 §0 #6).

        A yaml-only broker (no DB row yet) is materialised first: a PASS against
        a real contract note is exactly the moment its rates stop being a
        suggestion, and the stamp has to be recorded somewhere. The row keeps
        ``is_preset=False`` so it reads as "your validated override".
        """
        with self._manager.session() as session:
            row = session.get(BrokerProfileRow, profile_id)
            if row is None:
                catalogue = self.get_catalogue_profile(profile_id)
                if catalogue is None:
                    raise ValueError(f"unknown profile {profile_id!r}")
                row = BrokerProfileRow(
                    profile_id=catalogue["profile_id"],
                    profile_name=catalogue["profile_name"],
                    is_preset=False,
                    currency=catalogue["currency"],
                    default_segment=catalogue["default_segment"],
                    commission_model=dict(catalogue["commission_model"]),
                    statutory_rates=dict(catalogue["statutory_rates"]),
                    minimum_commission=catalogue.get("minimum_commission"),
                )
                session.add(row)
                session.flush()
            row.validated_on = when or date.today()
            row.contract_note_ref = document_id
            self._audit(
                session,
                profile_id,
                "validated",
                row.contract_note_ref,
                document_id,
                changed_by,
            )

    def resolve(self, profile_id: str):
        """A live :class:`BrokerProfile` for runner construction (DB row first,
        built-in preset fallback so a wiped DB never blocks a spawn)."""
        stored = self.get_profile(profile_id)
        if stored is not None:
            from backtest.simulator.fees import BrokerProfile

            return BrokerProfile(**BrokerProfileRow(**self._db_shape(stored)).to_profile_kwargs())
        # Not in the DB yet: the yaml file may still define it (``dhan``), and
        # load_broker_profile falls through to the built-in preset after that.
        from backtest.simulator.fees import load_broker_profile

        return load_broker_profile(broker=profile_id)

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _db_shape(stored: dict[str, Any]) -> dict[str, Any]:
        return {
            "profile_id": stored["profile_id"],
            "profile_name": stored["profile_name"],
            "is_preset": stored["is_preset"],
            "currency": stored["currency"],
            "default_segment": stored["default_segment"],
            "commission_model": stored["commission_model"],
            "statutory_rates": stored["statutory_rates"],
            "minimum_commission": stored.get("minimum_commission"),
        }

    @staticmethod
    def _row_to_dict(row: BrokerProfileRow) -> dict[str, Any]:
        return {
            "profile_id": row.profile_id,
            "profile_name": row.profile_name,
            "is_preset": row.is_preset,
            "currency": row.currency,
            "default_segment": row.default_segment,
            "commission_model": row.commission_model,
            "statutory_rates": row.statutory_rates,
            "minimum_commission": (
                float(row.minimum_commission) if row.minimum_commission is not None else None
            ),
            "validated_on": row.validated_on.isoformat() if row.validated_on else None,
            "contract_note_ref": row.contract_note_ref,
        }

    @staticmethod
    def _validate_rates(rates: Mapping[str, Any]) -> None:
        """Refuse negative or non-numeric statutory rates (fail closed)."""
        allowed = {
            "stt_delivery",
            "stt_intraday_sell",
            "stt_futures_sell",
            "stt_options_sell",
            "exchange_txn_equity",
            "exchange_txn_futures",
            "exchange_txn_options",
            "sebi_turnover",
            "ipft",
            "stamp_duty_delivery",
            "stamp_duty_intraday",
            "stamp_duty_futures",
            "stamp_duty_options",
            "gst_rate",
            "dp_charges",
        }
        for key, value in rates.items():
            if key not in allowed:
                raise ValueError(f"unknown statutory rate {key!r}")
            try:
                number = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{key}: not a number ({value!r})") from exc
            if number < 0:
                raise ValueError(f"{key}: must not be negative")

    def _apply_update(
        self,
        row: BrokerProfileRow,
        payload: Mapping[str, Any],
        statutory: dict[str, Any],
        changed_by: str,
        session: Any,
    ) -> list[str]:
        changes: list[str] = []
        fields = {
            "profile_name": str(payload.get("profile_name") or row.profile_name),
            "currency": str(payload.get("currency") or row.currency),
            "default_segment": str(payload.get("default_segment") or row.default_segment),
            "minimum_commission": payload.get("minimum_commission", row.minimum_commission),
        }
        for name, new in fields.items():
            old = getattr(row, name)
            if old != new:
                self._audit(
                    session, row.profile_id, name,
                    str(old) if old is not None else None,
                    str(new) if new is not None else None, changed_by,
                )
                setattr(row, name, new)
                changes.append(name)
        old_rates = json.dumps(row.statutory_rates, sort_keys=True)
        new_rates = json.dumps(statutory, sort_keys=True)
        if old_rates != new_rates:
            self._audit(
                session, row.profile_id, "statutory_rates", old_rates, new_rates, changed_by
            )
            row.statutory_rates = statutory
            changes.append("statutory_rates")
        if payload.get("commission_model") is not None:
            old_model = json.dumps(row.commission_model, sort_keys=True)
            new_model = json.dumps(payload["commission_model"], sort_keys=True)
            if old_model != new_model:
                self._audit(
                    session, row.profile_id, "commission_model", old_model, new_model, changed_by
                )
                row.commission_model = dict(payload["commission_model"])
                changes.append("commission_model")
        return changes

    @staticmethod
    def _audit(
        session: Any,
        profile_id: str,
        field: str,
        old: Any,
        new: Any,
        changed_by: str,
    ) -> None:
        session.add(
            BrokerProfileAudit(
                profile_id=profile_id,
                field_changed=field,
                old_value=None if old is None else str(old),
                new_value=None if new is None else str(new),
                changed_by=changed_by,
            )
        )


_STORE: BrokerProfileStore | None = None


def get_broker_profile_store() -> BrokerProfileStore:
    """Process-wide store (built on the app's DatabaseManager)."""
    global _STORE
    if _STORE is None:
        from backtest.db import DatabaseManager

        _STORE = BrokerProfileStore(DatabaseManager.from_env())
        _STORE.seed_from_yaml()
    return _STORE
