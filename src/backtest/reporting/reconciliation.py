"""Platform P&L vs the broker's contract note.

A consolidated P&L is only worth as much as its trustworthiness, and the only
external check a trader actually has is the broker's own paperwork. This module
compares the platform's numbers for one broker against the note's numbers and
refuses to paper over a difference:

* ``PASS`` — inside the rupee tolerance (rounding on STT/GST is normal).
* ``WARNING`` — small in absolute terms or under the percentage band; usually a
  missing component (DP charges, an SGST/IGST split) rather than a wrong trade.
* ``FAIL`` — outside both bands. Something is missing, duplicated or priced
  differently, and the report should not be filed until it is understood.

Component-level comparison is where the value is: "we are ₹40 different" is not
actionable, "we are ₹40 different and every rupee of it is DP charges" is.

Complements :meth:`~backtest.simulator.fees.CommissionCalculator.validate_against_contract_note`,
which validates the *fee model* on a single execution (and stamps the broker
profile when it passes). This module reconciles a *period's book* against a
transcribed note total.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from backtest.reporting.consolidator import PnLReport
from backtest.simulator.money import ZERO

__all__ = ["BrokerReconciliation", "ReconciliationResult", "load_reconciliation_thresholds"]

logger = logging.getLogger("backtest.reporting.reconciliation")

_STATUS_PASS = "PASS"
_STATUS_WARNING = "WARNING"
_STATUS_FAIL = "FAIL"

DEFAULT_THRESHOLDS = {
    #: Differences at or below this are rounding, not signal.
    "pass_tolerance": Decimal("10"),
    #: Differences below this (absolute) or under ``warn_pct`` are a warning.
    "warn_tolerance": Decimal("100"),
    "warn_pct": Decimal("1.0"),
}

_THRESHOLDS_ENV = "REPORTING_CONFIG_PATH"
_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "reporting.yaml"


def load_reconciliation_thresholds(path: str | Path | None = None) -> dict[str, Decimal]:
    """Read the ``reconciliation:`` block; fall back to the defaults."""
    candidate = Path(path) if path else Path(os.getenv(_THRESHOLDS_ENV) or _CONFIG_PATH)
    if not candidate.exists():
        return dict(DEFAULT_THRESHOLDS)
    try:
        import yaml

        document = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
        block = document.get("reconciliation") if isinstance(document, Mapping) else None
        if not isinstance(block, Mapping):
            return dict(DEFAULT_THRESHOLDS)
        out = dict(DEFAULT_THRESHOLDS)
        for key in out:
            if key in block and block[key] is not None:
                out[key] = Decimal(str(block[key]))
        return out
    except Exception as exc:  # noqa: BLE001 — config problems are not page errors
        logger.warning("could not read reconciliation thresholds (%s) — using defaults", exc)
        return dict(DEFAULT_THRESHOLDS)


@dataclass
class ReconciliationResult:
    """One broker, one period, platform vs contract note."""

    broker: str
    period: str
    status: str
    platform_pnl: Decimal
    broker_pnl: Decimal
    difference: Decimal
    difference_pct: float
    platform_fees: Decimal = ZERO
    broker_fees: Decimal | None = None
    fee_difference: Decimal | None = None
    component_breakdown: dict[str, dict[str, Any]] = field(default_factory=dict)
    likely_causes: list[str] = field(default_factory=list)
    note_ref: str = ""
    trades_compared: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.status == _STATUS_PASS

    def to_dict(self) -> dict[str, Any]:
        return {
            "broker": self.broker,
            "period": self.period,
            "status": self.status,
            "platform_pnl": float(self.platform_pnl),
            "broker_pnl": float(self.broker_pnl),
            "difference": float(self.difference),
            "difference_pct": round(self.difference_pct, 4),
            "platform_fees": float(self.platform_fees),
            "broker_fees": float(self.broker_fees) if self.broker_fees is not None else None,
            "fee_difference": (
                float(self.fee_difference) if self.fee_difference is not None else None
            ),
            "component_breakdown": self.component_breakdown,
            "likely_causes": self.likely_causes,
            "note_ref": self.note_ref,
            "trades_compared": self.trades_compared,
            "warnings": self.warnings,
        }


class BrokerReconciliation:
    """Reconciles a report's per-broker P&L against transcribed note figures."""

    def __init__(self, thresholds: Mapping[str, Any] | None = None):
        resolved = dict(DEFAULT_THRESHOLDS)
        for key, value in (thresholds or {}).items():
            if key in resolved:
                resolved[key] = Decimal(str(value))
        self.pass_tolerance = resolved["pass_tolerance"]
        self.warn_tolerance = resolved["warn_tolerance"]
        self.warn_pct = resolved["warn_pct"]

    # -- single broker -----------------------------------------------------

    def reconcile(
        self,
        report: PnLReport,
        broker: str,
        contract_note_pnl: Any,
        contract_note_fees: Mapping[str, Any] | None = None,
        *,
        note_ref: str = "",
        broker_fees_total: Any = None,
    ) -> ReconciliationResult:
        """Compare one broker's platform numbers with the broker's own.

        ``contract_note_pnl`` is required: a missing figure is an input error.
        Treating it as ₹0 would manufacture a difference equal to the whole
        platform P&L and report it as a reconciliation failure.
        """
        if contract_note_pnl is None or str(contract_note_pnl).strip() == "":
            raise ValueError(
                "contract_note_pnl is required — refusing to reconcile against an empty note"
            )
        name = str(broker or "").strip().lower()
        note_pnl = _dec(contract_note_pnl)
        trades = [t for t in report.trades if (t.broker or "").lower() == name]
        platform_pnl = _money(sum((t.net_pnl for t in trades), ZERO))
        platform_fees = _money(sum((t.total_costs for t in trades), ZERO))

        if not trades:
            note = f"no platform trades for broker {name!r} in this period"
            return ReconciliationResult(
                broker=name,
                period=report.period.label,
                status=_STATUS_FAIL,
                platform_pnl=ZERO,
                broker_pnl=note_pnl,
                difference=_money(-note_pnl),
                difference_pct=100.0 if note_pnl else 0.0,
                platform_fees=ZERO,
                broker_fees=_dec(broker_fees_total) if broker_fees_total is not None else None,
                likely_causes=[note],
                note_ref=note_ref,
                trades_compared=0,
                warnings=[note],
            )

        difference = _money(platform_pnl - note_pnl)
        difference_pct = (
            float(abs(difference) / abs(note_pnl) * Decimal("100")) if note_pnl else 0.0
        )
        status = self._status(difference, difference_pct)

        breakdown: dict[str, dict[str, Any]] = {}
        fee_difference: Decimal | None = None
        likely: list[str] = []
        if contract_note_fees:
            platform_components = _aggregate_components(trades)
            for key, raw in contract_note_fees.items():
                note_value = _dec(raw)
                platform_value = platform_components.get(key, ZERO)
                diff = _money(platform_value - note_value)
                breakdown[key] = {
                    "platform": float(platform_value),
                    "broker": float(note_value),
                    "difference": float(diff),
                }
                if abs(diff) > self.pass_tolerance:
                    likely.append(
                        f"{key}: platform ₹{platform_value:,.2f} vs note ₹{note_value:,.2f} "
                        f"({diff:+,.2f})"
                    )
            note_total = _dec(broker_fees_total) if broker_fees_total is not None else _money(
                sum((_dec(v) for v in contract_note_fees.values()), ZERO)
            )
            fee_difference = _money(platform_fees - note_total)
            for key in platform_components:
                if key not in breakdown:
                    breakdown[key] = {
                        "platform": float(platform_components[key]),
                        "broker": 0.0,
                        "difference": float(platform_components[key]),
                    }

        likely.extend(self._likely_causes(difference, difference_pct, breakdown, trades))
        warnings: list[str] = []
        estimated = sum(1 for t in trades if t.fees_basis == "estimated")
        if estimated:
            warnings.append(
                f"{estimated} of {len(trades)} platform trades carry ESTIMATED fees — a "
                "difference here may be the model, not the broker"
            )
        return ReconciliationResult(
            broker=name,
            period=report.period.label,
            status=status,
            platform_pnl=platform_pnl,
            broker_pnl=note_pnl,
            difference=difference,
            difference_pct=difference_pct,
            platform_fees=platform_fees,
            broker_fees=(
                _dec(broker_fees_total) if broker_fees_total is not None else None
            ),
            fee_difference=fee_difference,
            component_breakdown=breakdown,
            likely_causes=likely,
            note_ref=note_ref,
            trades_compared=len(trades),
            warnings=warnings,
        )

    # -- whole report ------------------------------------------------------

    def reconcile_report(
        self,
        report: PnLReport,
        notes: Mapping[str, Any],
    ) -> list[ReconciliationResult]:
        """Reconcile several brokers at once.

        ``notes`` maps a broker name to either a number (net P&L only) or a
        mapping ``{"pnl": …, "fees": {component: amount}, "fees_total": …,
        "note_ref": …}``. ``contract_note_pnl`` is accepted as a synonym for
        ``pnl`` so a caller can pass the API's field names straight through.
        """
        results: list[ReconciliationResult] = []
        for broker, payload in notes.items():
            if isinstance(payload, Mapping):
                note_pnl = payload.get("pnl", payload.get("contract_note_pnl"))
                results.append(
                    self.reconcile(
                        report,
                        broker,
                        contract_note_pnl=note_pnl,
                        contract_note_fees=payload.get("fees"),
                        broker_fees_total=payload.get("fees_total"),
                        note_ref=str(payload.get("note_ref") or ""),
                    )
                )
            else:
                results.append(self.reconcile(report, broker, contract_note_pnl=payload))
        return results

    # -- internals ---------------------------------------------------------

    def _status(self, difference: Decimal, difference_pct: float) -> str:
        magnitude = abs(difference)
        if magnitude <= self.pass_tolerance:
            return _STATUS_PASS
        if magnitude < self.warn_tolerance or difference_pct < float(self.warn_pct):
            return _STATUS_WARNING
        return _STATUS_FAIL

    def _likely_causes(
        self,
        difference: Decimal,
        difference_pct: float,
        breakdown: Mapping[str, Mapping[str, Any]],
        trades: list[Any],
    ) -> list[str]:
        causes: list[str] = []
        magnitude = abs(difference)
        if magnitude == ZERO:
            causes.append("exact match — rates, trade set and rounding all agree")
            return causes
        if magnitude <= self.pass_tolerance:
            causes.append(
                "within the rounding tolerance — typically paise-level STT/GST rounding"
            )
        missing = [
            key
            for key in ("dp_charges", "stamp_duty", "sebi_turnover")
            if key in breakdown and breakdown[key]["broker"] == 0
        ]
        if missing:
            causes.append(
                "note shows no " + ", ".join(missing) + " — either the broker bundled "
                "it into another line or the platform modelled a charge the note did not levy"
            )
        if difference_pct > 5.0:
            causes.append(
                "over 5% apart: check for a missing or duplicated day of trades before "
                "trusting either side"
            )
        if not breakdown:
            causes.append(
                "no component detail supplied — pass the note's itemised charges to "
                "localise the difference instead of just measuring it"
            )
        estimated = sum(1 for t in trades if getattr(t, "fees_basis", "") == "estimated")
        if estimated:
            causes.append(
                f"{estimated} platform trade(s) use estimated fees — compare those first"
            )
        return causes


def _aggregate_components(trades: list[Any]) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = {}
    for trade in trades:
        components = getattr(getattr(trade, "fees", None), "components", {}) or {}
        for key, value in components.items():
            if isinstance(value, Decimal) and not key.startswith("leg_"):
                totals[key] = totals.get(key, ZERO) + value
        slippage = getattr(trade, "slippage", ZERO)
        if slippage:
            totals["slippage"] = totals.get("slippage", ZERO) + slippage
    return {key: _money(value) for key, value in totals.items()}


def _dec(value: Any) -> Decimal:
    if value is None:
        return ZERO
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001
        logger.warning("non-numeric reconciliation value %r", value)
        return ZERO


def _money(value: Decimal) -> Decimal:
    return Decimal(value).quantize(Decimal("0.01"))
