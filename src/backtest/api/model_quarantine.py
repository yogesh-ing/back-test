"""Server-side quarantine for model-priced backtests (plan V3 §5).

A model run (``pricing_feed='model_bs'``) prices options with Black-Scholes off
the underlying candles, not observed chain quotes. Its P&L is a structure
comparison, never a measurement of historical option returns — so it can never
be certified, optimized off, promoted to a preset, or deployed to paper as a
validated result.

The flag is read from the **stored ledger row**, never a client claim. A
browser that disables the Tune This button is a convenience; this module is the
control. Every certify/deploy door calls :func:`refuse_model_priced` with the
backtest run id it is about to act on.
"""

from __future__ import annotations

from typing import Any, Optional, Tuple

from flask import jsonify

__all__ = [
    "MODEL_PRICED_REASON",
    "stored_run_is_model_priced",
    "refuse_model_priced",
    "resolve_lineage_run_id",
    "refuse_lineage",
]

MODEL_PRICED_REASON = (
    "This backtest is model-priced: option premiums are Black-Scholes simulations "
    "off the underlying candles, not observed option-chain quotes. It cannot be "
    "certified, optimized, or deployed to paper. Re-run on real option-chain "
    "history to unlock those doors."
)


def stored_run_is_model_priced(run_id: Optional[str]) -> bool:
    """True when the named ledger run is model-priced (or explicitly uncertifiable).

    Reads the stored payload, so a client cannot talk its way past the door by
    omitting a flag. A run that cannot be read is not blocked here — the
    downstream import/spawn fails on its own missing-run path; this door's job
    is to stop a *known* model run, not to be the general run validator.
    """
    if not run_id:
        return False
    from backtest.api.backtest import _ledger

    ledger = _ledger()
    if ledger is None:
        return False
    try:
        record = ledger.get_run(str(run_id))
    except Exception:  # noqa: BLE001 — unreadable run is the import path's problem
        return False
    if not record:
        return False
    payload = record.get("payload") or {}
    if str(payload.get("pricing_feed") or "").strip().lower() == "model_bs":
        return True
    readiness = payload.get("readiness") or {}
    return readiness.get("certifiable") is False


def refuse_model_priced(run_id: Optional[str]) -> Optional[Tuple[Any, int]]:
    """A 403 response when the named run is model-priced, else ``None``."""
    if stored_run_is_model_priced(run_id):
        return (
            jsonify(
                {
                    "error": MODEL_PRICED_REASON,
                    "code": "model_priced_uncertifiable",
                    "certifiable": False,
                    "certification_reason": "model_pricing",
                }
            ),
            403,
        )
    return None


def resolve_lineage_run_id(data: dict) -> Optional[str]:
    """The backtest run a deploy request claims to descend from, if any.

    A runner can be spawned straight off a backtest (``preset_from_run`` /
    ``backtest_run_id``) or off a saved preset (``preset_id``), which itself
    names its origin run. We resolve all three to a single ledger id so the
    quarantine door reads the *stored* row, never a client flag. An unresolvable
    preset is not blocked here — the spawn path validates it on its own.
    """
    direct = data.get("preset_from_run") or data.get("backtest_run_id")
    if direct:
        return str(direct).strip() or None
    preset_id = data.get("preset_id")
    if not preset_id:
        return None
    try:
        from backtest.api.optimize import _service

        preset = _service().store.get_preset(str(preset_id))
    except Exception:  # noqa: BLE001 — store unavailable is the spawn path's problem
        return None
    if not preset:
        return None
    origin = preset.get("backtest_run_id")
    if not origin:
        return None
    return str(origin).strip() or None


def refuse_lineage(data: dict) -> Optional[Tuple[Any, int]]:
    """A 403 when the deploy request's lineage is a model-priced run, else None."""
    return refuse_model_priced(resolve_lineage_run_id(data))