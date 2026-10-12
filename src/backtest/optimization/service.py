"""Optimization service: job lifecycle, progress, apply-to-runner, presets.

One :class:`OptimizationService` per process (the Flask app builds it
lazily). A submitted run is validated, persisted as ``pending`` and executed
on a background thread; runs are serialized through a single slot so two
large grids never fight for the same CPUs (queued runs stay ``pending``).
Inside a run the backtests themselves fan out over a process pool.

Execution phases (``progress.phase``)::

    loading → baseline → search → sensitivity → walk_forward → analysis → saving

Live progress (tested/valid counts, best-so-far, recent results, ETA) is
held in memory and served by :meth:`status`; the ``optimization_runs`` row is
refreshed every couple of seconds so another process (or a restart) still
sees roughly where the run got to. A cancelled run keeps — and saves — every
result computed so far.

Apply-to-runner (PRD "Apply Parameters to Runner"): records an
``optimization_audit`` row + the portfolio manager's audit log, snapshots the
parameters as a preset, and optionally restarts/spawns a paper runner.
Live application is deliberately fail-closed: it requires an explicit
``confirm_live`` flag, is refused for overfitted runs, and — unless the
caller overrides — for runs without walk-forward validation.
"""

from __future__ import annotations

import dataclasses
import logging
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable

import pandas as pd

from backtest.data.base import periods_per_year as annualisation_factor
from backtest.data.sources_policy import default_backtest_source, default_broker_source
from backtest.engine.monte_carlo import DEFAULT_SIMULATIONS, monte_carlo_trade_order
from backtest.optimization import analysis as an
from backtest.optimization.attestation import (
    SYNTHETIC_ACKNOWLEDGEMENT,
    attestation_columns,
    attestation_is_satisfied,
    attestation_preview,
    attestation_record,
)
from backtest.optimization.config import (
    ConfigValidationError,
    OptimizationConfig,
    parse_config,
)
from backtest.optimization.deflation import deflated_sharpe, deflation_warning
from backtest.optimization.evaluator import (
    Cancelled,
    Evaluator,
    default_workers,
)
from backtest.optimization.methods import SearchSpace, run_method
from backtest.optimization.scoring import (
    FAILED_SCORE,
    check_constraints,
    compliance_report,
    objective_score,
    violation_label,
)
from backtest.optimization.store import OptimizationStore, clean_json
from backtest.optimization.walk_forward import make_splits

log = logging.getLogger("backtest.optimization.service")

#: Rough per-bar cost of one backtest by engine (ms) — used by estimates
#: before any run has been timed on this machine.
_MS_PER_BAR = {"driver": 0.095, "quick_screen": 0.03, "options": 0.4}
_BARS_PER_DAY = {
    "1min": 375,
    "5min": 75,
    "15min": 25,
    "1hour": 7,
    "4hour": 2,
    "1day": 1,
    "1week": 0.2,
}

DB_FLUSH_SECONDS = 2.0
HEARTBEAT_SECONDS = 30.0
RECENT_RESULTS = 25
SENSITIVITY_POINTS = 25
SENSITIVITY_ROUNDS = 3
APPLY_TARGETS = ("paper", "live", "ab_test", "none")

#: Not-probed marker for the lazy run-ledger (PRD R6). ``None`` means
#: "probed and unavailable" (or explicitly disabled) — the two must not
#: collide, or every baseline evaluation would re-open the database.
_LEDGER_UNPROBED = object()


class OptimizationError(Exception):
    """User-facing service error (maps to HTTP 4xx)."""

    def __init__(
        self,
        message: str,
        status: int = 400,
        *,
        code: str | None = None,
        **details: Any,
    ) -> None:
        super().__init__(message)
        self.status = status
        #: Machine-readable tag, so the UI can react to *which* refusal this is
        #: rather than pattern-matching the message text. A refusal whose
        #: meaning lives in a sentence is a refusal the next reword will break.
        self.code = code
        self.details = details


def _now() -> datetime:
    return datetime.now(timezone.utc)


def default_loader(cfg: OptimizationConfig) -> pd.DataFrame:
    """Load the run's candles once from the configured data source."""
    from backtest.runner import build_source

    bt = cfg.backtest
    # An unnamed source resolves through the policy (db in a real deployment,
    # synthetic only for a profile that enables it) instead of a literal.
    source = build_source(bt.source or default_backtest_source())
    return source.get_candles(bt.symbol, bt.start_date, bt.end_date, bt.timeframe)


def params_diff(old: dict | None, new: dict | None) -> dict[str, dict]:
    old, new = old or {}, new or {}
    out = {}
    for k in sorted(set(old) | set(new)):
        if old.get(k) != new.get(k):
            entry: dict[str, Any] = {"old": old.get(k), "new": new.get(k)}
            try:
                if old.get(k) is not None and new.get(k) is not None:
                    entry["change"] = round(float(new[k]) - float(old[k]), 6)
            except (TypeError, ValueError):
                pass
            out[k] = entry
    return out


class _Job:
    """In-memory state of one running optimization."""

    def __init__(self, run_id: str, cfg: OptimizationConfig) -> None:
        self.run_id = run_id
        self.cfg = cfg
        self.cancel = threading.Event()
        self.pause = threading.Event()
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.rows: list[dict[str, Any]] = []
        self.recent: deque = deque(maxlen=RECENT_RESULTS)
        self.best: dict[str, Any] | None = None
        self.valid = 0
        self.errors = 0
        self.phase = "queued"
        self.phase_detail = ""
        self.done_evals = 0
        self.planned_evals = 1
        self.started = time.monotonic()
        self.search_started: float | None = None
        self.paused_seconds = 0.0
        self._pause_began: float | None = None
        self.last_flush = 0.0
        self.wf_splits: list[dict] = []

    # progress snapshot served to the UI
    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            elapsed = time.monotonic() - self.started - self.paused_seconds
            if self._pause_began is not None:
                elapsed -= time.monotonic() - self._pause_began
            rate = self.done_evals / elapsed if elapsed > 0 and self.done_evals else None
            remaining = max(self.planned_evals - self.done_evals, 0)
            eta = remaining / rate if rate else None
            tested = len(self.rows)
            return {
                "phase": self.phase,
                "phase_detail": self.phase_detail,
                "tested": tested,
                "valid": self.valid,
                "errors": self.errors,
                "evaluations_done": self.done_evals,
                "evaluations_planned": self.planned_evals,
                "percent": round(
                    min(100.0, 100.0 * self.done_evals / max(self.planned_evals, 1)), 1
                ),
                "elapsed_seconds": round(elapsed, 1),
                "eta_seconds": None if eta is None else round(eta, 1),
                "rate_per_second": None if rate is None else round(rate, 2),
                "best": clean_json(self.best),
                "recent": clean_json(list(self.recent)),
                "walk_forward_splits": clean_json(self.wf_splits),
                "paused": self.pause.is_set(),
            }


class OptimizationService:
    """Process-wide optimization engine (thread-safe)."""

    def __init__(
        self,
        store: OptimizationStore,
        *,
        workers: int | None = None,
        loader: Callable[[OptimizationConfig], pd.DataFrame] | None = None,
        default_source: str | None = None,
        manager_getter: Callable[[], Any] | None = None,
        fail_stale: bool = True,
        backtest_ledger: Any = _LEDGER_UNPROBED,
    ) -> None:
        self.store = store
        self.workers = workers if workers is not None else default_workers()
        self.loader = loader or default_loader
        self.default_source = default_source
        self._manager_getter = manager_getter
        self._bt_ledger = backtest_ledger
        self._jobs: dict[str, _Job] = {}
        self._jobs_lock = threading.Lock()
        self._slot = threading.Semaphore(1)
        self._timing: dict[str, float] = {}  # engine -> measured ms/bar
        if fail_stale:
            try:
                n = store.fail_stale_runs()
                if n:
                    log.warning("[optimize] marked %d interrupted run(s) as failed", n)
            except Exception:  # noqa: BLE001 - never block app start
                log.warning("[optimize] could not reconcile stale runs", exc_info=True)

    # ------------------------------------------------------------------
    # Config / estimate
    # ------------------------------------------------------------------

    def parse(self, doc: dict) -> OptimizationConfig:
        return parse_config(doc, default_source=self.default_source)

    def estimate(self, cfg: OptimizationConfig) -> dict[str, Any]:
        """Evaluation counts + wall-clock estimate for the setup page."""
        bt = cfg.backtest
        days = (
            datetime.strptime(bt.end_date, "%Y-%m-%d")
            - datetime.strptime(bt.start_date, "%Y-%m-%d")
        ).days + 1
        trading_days = days * 252 / 365
        bars = max(1, int(trading_days * _BARS_PER_DAY.get(bt.timeframe, 1)))
        ms_bar = self._timing.get(bt.engine, _MS_PER_BAR.get(bt.engine, 0.1))
        per_eval = max(ms_bar * bars, 2.0) / 1000.0 + 0.004  # + IPC overhead
        search = cfg.planned_evaluations()
        sens = sum(min(len(p.values()), SENSITIVITY_POINTS) for p in cfg.optimized)
        wf_evals, splits = 0, 0
        if cfg.walk_forward.enabled:
            wf = cfg.walk_forward
            sp = make_splits(
                bt.start_date, bt.end_date, wf.train_period_days, wf.test_period_days, wf.step_days
            )
            splits = len(sp)
            per_split = min(cfg.grid_size(), wf.max_evals_per_split)
            wf_bars_frac = (wf.train_period_days + wf.test_period_days) / max(days, 1)
            # WF backtests are shorter than the full period
            wf_evals = int(splits * (per_split + 1) * max(wf_bars_frac, 0.05) * 1.3)
            wf_evals = max(wf_evals, splits)
        total = search + sens + wf_evals + 1
        workers = max(1, self.workers)
        seconds = total * per_eval / workers * (1.0 if workers == 1 else 1.15)
        return {
            "grid_size": cfg.grid_size(),
            "method": cfg.method,
            "search_evaluations": search,
            "sensitivity_evaluations": sens,
            "walk_forward_splits": splits,
            "walk_forward_evaluations_equiv": wf_evals,
            "total_evaluations": total,
            "estimated_bars": bars,
            "workers": workers,
            "estimated_seconds": round(seconds, 1),
            "warnings": list(cfg.warnings),
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def attestation_for(self, doc: dict, *, candles: Any = None) -> dict[str, Any]:
        """PRD Part 2 §2 — the data confirmation for a setup document.

        With ``candles`` this is the measured record kept on the run; without
        it, the preview the setup page shows before anything is fetched.
        """
        return self._attestation_from_cfg(self.parse(doc), doc, candles)

    def _attestation_from_cfg(
        self, cfg: OptimizationConfig, doc: dict | None = None, candles: Any = None
    ) -> dict[str, Any]:
        """Build the attestation from an already-parsed config.

        Split out from :meth:`attestation_for` so the run thread can re-measure
        the record from the config it already holds, instead of re-parsing the
        document it was never given.
        """
        bt = cfg.backtest
        att = (
            attestation_record(
                bt.source,
                candles,
                symbol=bt.symbol,
                timeframe=bt.timeframe,
                start_date=bt.start_date,
                end_date=bt.end_date,
            )
            if candles is not None
            else attestation_preview(
                bt.source,
                symbol=bt.symbol,
                timeframe=bt.timeframe,
                start_date=bt.start_date,
                end_date=bt.end_date,
            )
        )
        # The operator's tick travels on the document, not on the attestation
        # we just rebuilt — it is a statement about this submission, and
        # rebuilding the record must not silently carry it forward.
        claimed = (doc or {}).get("dataAttestation") or {}
        if claimed.get("acknowledged") and not att["data_source_real"]:
            att["acknowledged"] = True
            att["acknowledged_at"] = claimed.get("acknowledged_at") or _now().isoformat()
        return att

    def submit(
        self, doc: dict, *, created_by: str | None = None, start: bool = True
    ) -> dict[str, Any]:
        cfg = self.parse(doc)
        # PRD Part 2 §2 — synthetic is the one input that cannot be defended
        # after the fact, so it carries a real gate. Checked here rather than in
        # the browser because a gate only the browser enforces is a suggestion.
        # Everything else in the attestation warns and allows.
        attestation = self.attestation_for(doc)
        if not attestation_is_satisfied(attestation):
            raise OptimizationError(
                "synthetic data requires an explicit acknowledgement: "
                f"'{SYNTHETIC_ACKNOWLEDGEMENT}'",
                409,
                code="synthetic_data_not_acknowledged",
                data_source=attestation["data_source"],
            )
        run_id = self.store.create_run(
            strategy_id=cfg.strategy_id,
            objective=cfg.objective,
            method=cfg.method,
            param_space=[p.to_dict() for p in cfg.parameters],
            constraints=[c.to_dict() for c in cfg.constraints],
            backtest_config={
                **cfg.to_dict()["backtestConfig"],
                "methodSettings": cfg.method_settings.to_dict(),
                "warnings": list(cfg.warnings),
            },
            walk_forward_enabled=cfg.walk_forward.enabled,
            walk_forward_config=cfg.to_dict()["walkForward"],
            total_combinations=cfg.planned_evaluations(),
            bucket_id=cfg.bucket_id,
            created_by=created_by,
            status="pending" if start else "draft",
            baseline_params=cfg.baseline_params,
            **attestation_columns(attestation),
        )
        log.info(
            "[optimize] run %s created: %s %s over %d combos (%s)",
            run_id[:8],
            cfg.method,
            cfg.strategy_id,
            cfg.grid_size(),
            cfg.objective,
        )
        if start:
            self._launch(run_id, cfg)
        return self.status(run_id)

    def start(self, run_id: str) -> dict[str, Any]:
        run = self._require(run_id)
        if run["status"] != "draft":
            raise OptimizationError(f"run is {run['status']} — only drafts can be started", 409)
        cfg = self.config_from_run(run)
        self.store.update_run(run_id, status="pending")
        self._launch(run_id, cfg)
        return self.status(run_id)

    def rerun(
        self, run_id: str, *, created_by: str | None = None, overrides: dict | None = None
    ) -> dict[str, Any]:
        """New run with the same config (optionally overridden)."""
        run = self._require(run_id)
        doc = self.config_doc_from_run(run)
        for key, val in (overrides or {}).items():
            if isinstance(val, dict) and isinstance(doc.get(key), dict):
                doc[key] = {**doc[key], **val}
            else:
                doc[key] = val
        return self.submit(doc, created_by=created_by)

    def _launch(self, run_id: str, cfg: OptimizationConfig) -> None:
        job = _Job(run_id, cfg)
        with self._jobs_lock:
            self._jobs[run_id] = job
        job.thread = threading.Thread(
            target=self._run_guarded, args=(job,), name=f"optimize-{run_id[:8]}", daemon=True
        )
        job.thread.start()

    def cancel(self, run_id: str) -> dict[str, Any]:
        run = self._require(run_id)
        job = self._jobs.get(run_id)
        if job is None or run["status"] not in ("pending", "running", "paused"):
            if run["status"] == "draft":
                self.store.update_run(run_id, status="cancelled", completed_at=_now())
                return self.status(run_id)
            raise OptimizationError(f"run is {run['status']} — nothing to cancel", 409)
        job.cancel.set()
        job.pause.clear()
        return self.status(run_id)

    def pause(self, run_id: str) -> dict[str, Any]:
        job = self._active_job(run_id)
        with job.lock:
            if not job.pause.is_set():
                job.pause.set()
                job._pause_began = time.monotonic()
        self.store.update_run(run_id, status="paused")
        return self.status(run_id)

    def resume(self, run_id: str) -> dict[str, Any]:
        job = self._active_job(run_id)
        with job.lock:
            if job.pause.is_set():
                job.pause.clear()
                if job._pause_began is not None:
                    job.paused_seconds += time.monotonic() - job._pause_began
                    job._pause_began = None
        self.store.update_run(run_id, status="running")
        return self.status(run_id)

    def wait(self, run_id: str, timeout: float | None = None) -> dict[str, Any]:
        """Block until the run's thread finishes (tests / CLI)."""
        job = self._jobs.get(run_id)
        if job and job.thread:
            job.thread.join(timeout)
        return self.status(run_id)

    def delete(self, run_id: str) -> bool:
        run = self._require(run_id)
        if run["status"] in ("pending", "running", "paused"):
            raise OptimizationError("cancel the run before deleting it", 409)
        with self._jobs_lock:
            self._jobs.pop(run_id, None)
        return self.store.delete_run(run_id)

    def _active_job(self, run_id: str) -> _Job:
        self._require(run_id)
        job = self._jobs.get(run_id)
        if job is None or job.thread is None or not job.thread.is_alive():
            raise OptimizationError("run is not active", 409)
        return job

    def _require(self, run_id: str) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if run is None:
            raise OptimizationError("optimization run not found", 404)
        return run

    # ------------------------------------------------------------------
    # Status / reads
    # ------------------------------------------------------------------

    def status(self, run_id: str) -> dict[str, Any]:
        run = self._require(run_id)
        job = self._jobs.get(run_id)
        out = dict(run)
        if job is not None and job.thread is not None and job.thread.is_alive():
            out["progress"] = job.snapshot()
            out["live"] = True
        else:
            out["live"] = False
            out["progress"] = None
        return out

    @staticmethod
    def config_doc_from_run(run: dict) -> dict[str, Any]:
        bt = dict(run.get("backtest_config") or {})
        method_settings = bt.pop("methodSettings", None) or {}
        bt.pop("warnings", None)
        return {
            "strategyId": run["strategy_id"],
            "objectiveFunction": run["objective_function"],
            "method": run["method"],
            "parameters": run.get("param_space") or [],
            "constraints": run.get("constraints") or [],
            "backtestConfig": bt,
            "walkForward": run.get("walk_forward_config") or {"enabled": False},
            "methodSettings": {
                "nSamples": method_settings.get("n_samples"),
                "nCalls": method_settings.get("n_calls"),
                "nInitial": method_settings.get("n_initial"),
                "population": method_settings.get("population"),
                "generations": method_settings.get("generations"),
                "seed": method_settings.get("seed"),
            },
            "bucketId": run.get("bucket_id"),
            # PRD Part 2 §2. Re-running a synthetic run is the same operator
            # repeating the same decision, so the acknowledgement travels with
            # the configuration. Without this, "Rerun" on any synthetic run
            # would be silently impossible — the single most confusing way for
            # a gate to behave.
            "dataAttestation": {
                k: v
                for k, v in (run.get("data_attestation") or {}).items()
                if k in ("acknowledged", "acknowledged_at")
            },
        }

    def config_from_run(self, run: dict) -> OptimizationConfig:
        return self.parse(self.config_doc_from_run(run))

    def monte_carlo_best(
        self, run_id: str, *, simulations: int = DEFAULT_SIMULATIONS
    ) -> dict[str, Any]:
        """PRD Part 2 §4 — Monte Carlo on the single best result.

        Not on all 50,000 candidates: the winner is the one that would be
        applied to paper, and the question is whether *its* trade sequence is a
        lucky ordering. Walk-forward already asked whether the parameters
        generalise across time; this asks whether the order of the trades that
        produced them was luck. Different questions, and together they are the
        strongest check available without real trading.

        This re-runs the winner once over the run's own candles and its own
        config, then calls the same
        :func:`~backtest.engine.monte_carlo.monte_carlo_trade_order` the
        Backtest page uses — so the two paths cannot drift. One extra backtest
        per click is the price, and it is a price worth paying: a stored copy
        of every candidate's trade list would be tens of thousands of rows per
        run to answer a question asked once.
        """
        run = self.store.get_run(run_id)
        if not run:
            raise OptimizationError("run not found", 404)
        if run.get("status") != "completed":
            raise OptimizationError(
                "Monte Carlo runs on a completed run — this one has not finished", 409
            )
        params = run.get("best_params")
        if not params:
            raise OptimizationError("This run has no valid result to test.", 409)

        cfg = self.config_from_run(run)
        candles = self.loader(cfg)
        if candles is None or len(candles) == 0:
            raise OptimizationError(
                f"no candles for {cfg.backtest.symbol} "
                f"{cfg.backtest.start_date}→{cfg.backtest.end_date}",
                404,
            )
        settings = {
            "capital": cfg.backtest.initial_capital,
            "symbol": cfg.backtest.symbol,
            "engine": cfg.backtest.engine,
            "timeframe": cfg.backtest.timeframe,
            "selector_type": cfg.backtest.selector_type,
        }
        from backtest.optimization.evaluator import evaluate  # local: avoids a cycle

        result = evaluate(candles, settings, cfg.strategy_id, dict(params), keep_pnls=True)
        pnls = result.get("trade_pnls") or []
        mc = monte_carlo_trade_order(pnls, cfg.backtest.initial_capital, simulations=simulations)
        mc["params"] = dict(params)
        mc["run_id"] = run_id
        return mc

    def heatmap(
        self,
        run_id: str,
        x: str,
        y: str,
        *,
        metric: str = "score",
        agg: str = "max",
        compliant_only: bool = False,
    ) -> dict[str, Any]:
        run = self._require(run_id)
        rows = self._rows_for(run_id)
        if not rows:
            raise OptimizationError("no results yet", 404)
        names = {k for r in rows for k in r["params"]}
        unknown = [n for n in (x, y) if n not in names]
        if unknown:
            raise OptimizationError(f"unknown parameter(s): {', '.join(unknown)}")
        anchor = run.get("best_params") if agg == "slice" else None
        return an.heatmap(
            rows, x, y, metric=metric, agg=agg, anchor=anchor, compliant_only=compliant_only
        )

    def _rows_for(self, run_id: str) -> list[dict[str, Any]]:
        job = self._jobs.get(run_id)
        if job is not None and job.thread is not None and job.thread.is_alive():
            with job.lock:
                return [dict(r) for r in job.rows]
        return self.store.analysis_rows(run_id)

    def live_results(self, run_id: str) -> list[dict[str, Any]] | None:
        job = self._jobs.get(run_id)
        if job is not None and job.thread is not None and job.thread.is_alive():
            with job.lock:
                return [dict(r) for r in job.rows]
        return None

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def _heartbeat(self, job: _Job, stop: threading.Event) -> None:
        while not stop.wait(HEARTBEAT_SECONDS):
            try:
                self.store.touch_run(job.run_id)
            except Exception:  # noqa: BLE001 - heartbeat is best-effort
                log.debug("[optimize] heartbeat failed", exc_info=True)

    def _run_guarded(self, job: _Job) -> None:
        stop = threading.Event()
        threading.Thread(
            target=self._heartbeat,
            args=(job, stop),
            daemon=True,
            name=f"optimize-hb-{job.run_id[:8]}",
        ).start()
        try:
            self._run_slot(job)
        finally:
            stop.set()

    def _run_slot(self, job: _Job) -> None:
        with self._slot:
            if job.cancel.is_set():
                self.store.update_run(job.run_id, status="cancelled", completed_at=_now())
                return
            try:
                self._execute(job)
            except Cancelled:
                self._finish_cancelled(job)
            except Exception as exc:  # noqa: BLE001 - surfaced on the run row
                log.exception("[optimize] run %s failed", job.run_id[:8])
                try:
                    self._persist_rows(job)
                except Exception:  # noqa: BLE001
                    log.warning("[optimize] could not save partial results", exc_info=True)
                self.store.update_run(
                    job.run_id,
                    status="failed",
                    completed_at=_now(),
                    error_message=f"{exc.__class__.__name__}: {exc}"[:2000],
                    tested_combinations=len(job.rows),
                    valid_combinations=job.valid,
                )

    def _set_phase(self, job: _Job, phase: str, detail: str = "") -> None:
        with job.lock:
            job.phase, job.phase_detail = phase, detail
        log.info("[optimize] run %s → %s %s", job.run_id[:8], phase, detail)

    def _score_payload(self, job: _Job, payload: dict) -> dict[str, Any]:
        cfg = job.cfg
        if payload.get("error"):
            return {
                "params": payload["params"],
                "metrics": {},
                "score": FAILED_SCORE,
                "constraints_met": False,
                "violations": [{"error": payload["error"]}],
                "error": payload["error"],
                "elapsed_ms": payload.get("elapsed_ms"),
            }
        metrics = payload["metrics"]
        violations = check_constraints(metrics, cfg.constraints)
        return {
            "params": payload["params"],
            "metrics": metrics,
            "score": objective_score(metrics, cfg.objective),
            "constraints_met": not violations,
            "violations": violations,
            "error": None,
            "elapsed_ms": payload.get("elapsed_ms"),
        }

    def _regime_breakdown_for(
        self, cfg: OptimizationConfig, best: dict[str, Any], candles: Any
    ) -> dict[str, Any] | None:
        """PRD Part 2 §6.1 — how the winner behaved in each named period.

        Returns None rather than raising: a run that cannot be split by
        calendar band still completed, and failing it at the last step would
        throw away the search that produced a real result.
        """
        from backtest.optimization.evaluator import evaluate  # local: avoids a cycle

        settings = {
            "capital": cfg.backtest.initial_capital,
            "symbol": cfg.backtest.symbol,
            "engine": cfg.backtest.engine,
            "timeframe": cfg.backtest.timeframe,
            "selector_type": cfg.backtest.selector_type,
        }
        try:
            result = evaluate(
                candles, settings, cfg.strategy_id, dict(best["params"]), keep_regimes=True
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("[regimes] could not split the winner: %s", exc)
            return None
        return result.get("regimes")

    # -- run ledger (PRD R6) ---------------------------------------------------

    def _backtest_ledger(self) -> Any:
        """Lazy ledger handle over the SAME DatabaseManager as the optimize store.

        The service runs on background threads with no Flask app context, so it
        cannot use the API's ``_ledger()``. Probed once and cached — including
        the ``None`` result, so an offline database costs one failed connection
        for the process, not one per baseline evaluation.
        """
        if self._bt_ledger is not _LEDGER_UNPROBED:
            return self._bt_ledger
        self._bt_ledger = None
        try:
            from backtest.api.backtest_run_store import BacktestRunLedger

            ledger = BacktestRunLedger(self.store.db)
            ledger.ensure_schema()
            self._bt_ledger = ledger
        except Exception:  # noqa: BLE001 — the audit row is best-effort
            log.warning(
                "[run-ledger] optimizer baseline persistence disabled", exc_info=True
            )
        return self._bt_ledger

    def _record_baseline(
        self, cfg: OptimizationConfig, run_id: str, baseline: dict, attestation: dict
    ) -> None:
        """One immutable ledger row per baseline EVALUATION (PRD R6).

        Only the fresh path reaches this: the imported path re-quotes an
        existing backtest — which already owns a ledger row — instead of
        evaluating anything here, and recording a re-quote as an evaluation
        would be a fabricated audit fact. Grid candidates never do; they stay
        in ``optimization_results``. Failure is logged + alerted, never fatal
        to the running job.
        """
        ledger = self._backtest_ledger()
        if ledger is None:
            return
        bt = cfg.backtest
        try:
            from backtest.strategy.registry import get_strategy

            strategy_cls = get_strategy(cfg.strategy_id)
        except Exception:  # noqa: BLE001 — fingerprint is best-effort
            strategy_cls = None
        config = {
            "strategy": cfg.strategy_id,
            "symbol": bt.symbol,
            "timeframe": bt.timeframe,
            "from_date": bt.start_date,
            "to_date": bt.end_date,
            "strategy_params": dict(cfg.baseline_params or {}),
            "engine": bt.engine,
            "capital": bt.initial_capital,
        }
        where = f"optimizer-baseline/{run_id[:8]}"
        try:
            row_id = ledger.save_optimizer_baseline(
                optimization_run_id=run_id,
                config=config,
                metrics=baseline.get("metrics") or {},
                provenance=attestation or {},
                strategy_cls=strategy_cls,
            )
        except Exception as exc:  # noqa: BLE001 — the job already has its baseline
            ledger.fail_persist(where, exc)
        else:
            log.info(
                "[run-ledger] baseline row %s recorded for optimize run %s",
                row_id[:8],
                run_id[:8],
            )

    def _execute(self, job: _Job) -> None:
        cfg = job.cfg
        run_id = job.run_id
        self.store.update_run(run_id, status="running", started_at=_now(), error_message=None)
        self._set_phase(job, "loading", f"{cfg.backtest.symbol} {cfg.backtest.timeframe}")
        candles = self.loader(cfg)
        # PRD Part 2 §2. The preview written at submit time described what was
        # ASKED for; this is what the search actually got. A symbol with gaps
        # returns fewer bars and a narrower range, and the record kept on the
        # run has to be the second one or it is certifying data that was never
        # there.
        #
        # The measured record REPLACES the preview wholesale, so the synthetic
        # acknowledgement has to be carried across explicitly. It is the one
        # field not derived from the data — a person ticked it at submit time,
        # and nothing in the candles can confirm or re-earn it.
        try:
            measured = self._attestation_from_cfg(cfg, candles=candles)
            previous = (self.store.get_run(run_id) or {}).get("data_attestation") or {}
            if previous.get("acknowledged") and not measured.get("data_source_real"):
                measured["acknowledged"] = True
                measured["acknowledged_at"] = previous.get("acknowledged_at")
            self.store.update_run(run_id, **attestation_columns(measured))
            attestation = measured
        except Exception:  # noqa: BLE001
            log.warning("[attestation] could not record data provenance", exc_info=True)
            attestation = {}
        if candles is None or len(candles) == 0:
            raise ValueError(
                f"no candles for {cfg.backtest.symbol} {cfg.backtest.start_date}→"
                f"{cfg.backtest.end_date} ({cfg.backtest.timeframe})"
            )
        settings = {
            "capital": cfg.backtest.initial_capital,
            "symbol": cfg.backtest.symbol,
            "engine": cfg.backtest.engine,
            "timeframe": cfg.backtest.timeframe,
            "selector_type": cfg.backtest.selector_type,
        }
        space = SearchSpace.from_config(cfg)
        sens_evals = sum(min(len(p.values()), SENSITIVITY_POINTS) for p in cfg.optimized)
        wf_evals = 0
        if cfg.walk_forward.enabled:
            wf = cfg.walk_forward
            splits = make_splits(
                cfg.backtest.start_date,
                cfg.backtest.end_date,
                wf.train_period_days,
                wf.test_period_days,
                wf.step_days,
            )
            wf_evals = len(splits) * (min(space.total, wf.max_evals_per_split) + 1)
        with job.lock:
            job.planned_evals = 1 + cfg.planned_evaluations() + sens_evals + wf_evals

        workers = self.workers if cfg.planned_evaluations() > 4 else 1
        with Evaluator(
            candles,
            settings,
            cfg.strategy_id,
            workers=workers,
            cancel_event=job.cancel,
            pause_event=job.pause,
        ) as ev:
            # -- baseline (current params) ---------------------------------
            # PRD Part 2 §1: the origin's own baseline can be skipped when it
            # already ran the canonical fill-exact engine on real data over the
            # same range — re-running the identical evaluation is wasted work
            # the user waits on. The claim is RE-CHECKED here against what this
            # run actually loaded, not trusted from the browser: a flag only
            # the client sets is a suggestion. Anything that fails the check
            # (approximate engine, synthetic data, source mismatch) runs the
            # baseline as usual, and the run records which path it took.
            origin_bt = (self.store.get_run(run_id) or {}).get("backtest_config") or {}
            origin_claims_fill_exact = bool(cfg.backtest.baseline_imported)
            origin_fill_exact = (
                settings.get("engine") == "driver"
                and bool(attestation.get("data_source_real"))
            )
            if origin_claims_fill_exact and origin_fill_exact:
                quoted = dict(cfg.backtest.baseline_metrics or {})
                baseline = {
                    "params": dict(cfg.baseline_params or {}),
                    "metrics": quoted
                    or {"imported_from_backtest": origin_bt.get("sourceBacktestId")},
                    "score": None,
                    "constraints_met": True,
                    "violations": [],
                    "error": None,
                    "elapsed_ms": 0,
                    "imported": True,
                }
                log.info(
                    "[optimize] run %s: baseline imported from backtest %s "
                    "(fill-exact on real data) — re-run skipped",
                    run_id[:8],
                    origin_bt.get("sourceBacktestId") or "unknown",
                )
            else:
                self._set_phase(job, "baseline", "current parameters")
                base_payload = ev.evaluate_batch([cfg.baseline_params], keep_curve=True)[0]
                baseline = self._score_payload(job, base_payload)
                if origin_claims_fill_exact:
                    log.info(
                        "[optimize] run %s: baseline import requested but origin "
                        "did not qualify (engine=%s real_data=%s) — baseline re-run",
                        run_id[:8],
                        settings.get("engine"),
                        attestation.get("data_source_real"),
                    )
            with job.lock:
                job.done_evals += 1
            self.store.update_run(
                run_id,
                baseline_metrics=baseline["metrics"] or {"error": baseline["error"]},
                baseline_score=None if baseline["error"] else baseline["score"],
            )
            if not baseline.get("error") and not baseline.get("imported"):
                self._record_baseline(cfg, run_id, baseline, attestation or {})

            # -- main search -------------------------------------------------
            self._set_phase(job, "search", cfg.method)
            job.search_started = time.monotonic()
            seen_keys: set[tuple] = set()

            def evaluate(points: list[dict]) -> list[tuple[dict, float]]:
                payloads = ev.evaluate_batch(points, on_result=lambda p, fresh: None)
                out = []
                for p in payloads:
                    row = self._score_payload(job, p)
                    key = tuple(sorted(p["params"].items()))
                    if key not in seen_keys:
                        seen_keys.add(key)
                        self._record_row(job, row)
                    out.append(
                        (p["params"], row["score"] if row["constraints_met"] else FAILED_SCORE)
                    )
                self._maybe_flush(job)
                return out

            run_method(cfg, space, evaluate, batch_size=max(1, workers * 2))

            # -- sensitivity sweeps around the best (+ local refinement) -----
            # One-at-a-time sweeps through the best point. If a sweep finds a
            # better compliant point (common for random/Bayesian/GA, which do
            # not visit every neighbour), re-centre on it and sweep again —
            # at most SENSITIVITY_ROUNDS times; the cache makes repeats free.
            best = self._best_row(job)
            sensitivity: dict[str, dict] = {}
            rounds = 0
            while best is not None and rounds < SENSITIVITY_ROUNDS:
                rounds += 1
                self._set_phase(
                    job,
                    "sensitivity",
                    "1-D sweeps around the best"
                    + (f" (refinement {rounds})" if rounds > 1 else ""),
                )
                sensitivity = {}
                for spec in cfg.optimized:
                    values = an.sweep_values(spec, SENSITIVITY_POINTS)
                    points = [{**best["params"], spec.name: v} for v in values]
                    payloads = ev.evaluate_batch(points)
                    scores = []
                    for p in payloads:
                        row = self._score_payload(job, p)
                        scores.append(None if row["error"] else row["score"])
                        key = tuple(sorted(p["params"].items()))
                        if key not in seen_keys:
                            seen_keys.add(key)
                            row["origin"] = "sensitivity"
                            self._record_row(job, row, count_eval=False)
                    with job.lock:
                        if rounds == 1:
                            job.done_evals += len(points)
                    sensitivity[spec.name] = an.sensitivity_curve(
                        values, scores, best["params"][spec.name]
                    )
                    self._maybe_flush(job)
                refined = self._best_row(job)
                if refined is None or refined["params"] == best["params"]:
                    break
                best = refined

            best_curve = None
            if best is not None:
                best_payload = ev.evaluate_batch([best["params"]], keep_curve=True)[0]
                best_curve = best_payload.get("curve")

            # -- walk-forward -------------------------------------------------
            wf_report = None
            if cfg.walk_forward.enabled:
                from backtest.optimization.walk_forward import run_walk_forward

                self._set_phase(job, "walk_forward", "optimizing each train window")
                before = ev.evaluations

                def on_split(row: dict, n: int, total: int) -> None:
                    with job.lock:
                        job.wf_splits.append(row)
                        job.phase_detail = f"split {n}/{total}"
                    self._maybe_flush(job, force=True)

                wf_report = run_walk_forward(
                    cfg, ev, batch_size=max(1, workers * 2), on_split=on_split
                )
                with job.lock:
                    job.done_evals += ev.evaluations - before
            eval_seconds = ev.eval_seconds
            evaluations = ev.evaluations

        # -- analysis ------------------------------------------------------------
        self._set_phase(job, "analysis")
        self._finalize(
            job,
            baseline,
            base_payload.get("curve"),
            best,
            best_curve,
            sensitivity,
            wf_report,
            evaluations,
            eval_seconds,
            candles,
        )

    def _record_row(self, job: _Job, row: dict, count_eval: bool = True) -> None:
        with job.lock:
            row["seq"] = len(job.rows) + 1
            row.setdefault("origin", "search")
            job.rows.append(row)
            if count_eval:
                job.done_evals += 1
            if row["error"]:
                job.errors += 1
            if row["constraints_met"]:
                job.valid += 1
                if job.best is None or row["score"] > job.best["score"]:
                    job.best = {
                        "params": row["params"],
                        "score": row["score"],
                        "metrics": row["metrics"],
                        "seq": row["seq"],
                    }
            m = row["metrics"]
            job.recent.appendleft(
                {
                    "seq": row["seq"],
                    "params": row["params"],
                    "score": None if row["error"] else row["score"],
                    "sharpe": m.get("sharpe"),
                    "total_return": m.get("total_return"),
                    "max_drawdown": m.get("max_drawdown"),
                    "total_trades": m.get("total_trades"),
                    "win_rate": m.get("win_rate"),
                    "constraints_met": row["constraints_met"],
                    "violation": row["error"]
                    or violation_label([v for v in row["violations"] if "metric" in v]),
                }
            )

    def _best_row(self, job: _Job) -> dict | None:
        with job.lock:
            compliant = [r for r in job.rows if r["constraints_met"]]
        if not compliant:
            return None
        return max(compliant, key=lambda r: r["score"])

    def _maybe_flush(self, job: _Job, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - job.last_flush < DB_FLUSH_SECONDS:
            return
        job.last_flush = now
        snap = job.snapshot()
        best = snap["best"]
        fields: dict[str, Any] = {
            "tested_combinations": snap["tested"],
            "valid_combinations": snap["valid"],
        }
        if best:
            fields.update(
                best_params=best["params"], best_score=best["score"], best_metrics=best["metrics"]
            )
        try:
            self.store.update_run(job.run_id, **fields)
        except Exception:  # noqa: BLE001 - progress flush is best-effort
            log.warning("[optimize] progress flush failed", exc_info=True)

    def _ranked_rows(self, job: _Job) -> list[dict]:
        with job.lock:
            rows = list(job.rows)
        compliant = sorted(
            (r for r in rows if r["constraints_met"]), key=lambda r: r["score"], reverse=True
        )
        for i, r in enumerate(compliant, start=1):
            r["rank"] = i
        for r in rows:
            if not r["constraints_met"]:
                r["rank"] = None
        return rows

    def _persist_rows(self, job: _Job) -> int:
        return self.store.save_results(job.run_id, self._ranked_rows(job))

    def _finish_cancelled(self, job: _Job) -> None:
        self._set_phase(job, "saving", "partial results")
        n = self._persist_rows(job)
        best = self._best_row(job)
        fields: dict[str, Any] = {
            "status": "cancelled",
            "completed_at": _now(),
            "tested_combinations": len(job.rows),
            "valid_combinations": job.valid,
            "error_message": f"cancelled by user after {len(job.rows)} evaluations",
        }
        if best:
            fields.update(
                best_params=best["params"], best_score=best["score"], best_metrics=best["metrics"]
            )
        self.store.update_run(job.run_id, **fields)
        log.info("[optimize] run %s cancelled — %d partial results saved", job.run_id[:8], n)

    def _finalize(
        self,
        job: _Job,
        baseline: dict,
        baseline_curve: list | None,
        best: dict | None,
        best_curve: list | None,
        sensitivity: dict,
        wf_report: dict | None,
        evaluations: int,
        eval_seconds: float,
        candles: pd.DataFrame,
    ) -> None:
        cfg = job.cfg
        rows = self._ranked_rows(job)
        cluster = an.top_cluster(rows, cfg.optimized)
        robust = an.robustness_score(
            sensitivity, wf_report, cluster, best["metrics"] if best else None
        )
        # PRD Part 2 §3. Every combination tried is a trial, including the
        # ones that failed constraints or errored — they were chances taken,
        # and leaving them out flatters the result.
        deflated = deflated_sharpe(
            [float(r.get("metrics", {}).get("sharpe") or 0.0) for r in rows],
            (best or {}).get("metrics", {}).get("sharpe") if best else None,
            trials=max(len(rows), job.planned_evals) if rows else None,
            observations=len(candles),
            periods_per_year=annualisation_factor(cfg.backtest.timeframe),
        )
        # PRD Part 2 §6.1 — one extra evaluation of the winner, because the
        # regime table needs the FULL-resolution equity series. The curve
        # already in hand is downsampled to 400 points for drawing, and a
        # per-period Sharpe taken from a sampled curve is a sampling artefact
        # wearing a number. One backtest against a search of thousands.
        regimes = self._regime_breakdown_for(cfg, best, candles) if best else None
        warnings = an.warning_signs(cfg, best, sensitivity, wf_report, baseline)
        gap = deflation_warning(
            deflated, (best or {}).get("metrics", {}).get("sharpe") if best else None
        )
        if gap:
            warnings.append(gap)
        comparison = None
        if best is not None:
            comparison = {
                "baseline": {
                    "params": baseline["params"],
                    "metrics": baseline["metrics"],
                    "score": None if baseline["error"] else baseline["score"],
                    "error": baseline["error"],
                },
                "optimized": {
                    "params": best["params"],
                    "metrics": best["metrics"],
                    "score": best["score"],
                },
                "params_diff": params_diff(baseline["params"], best["params"]),
            }
        elapsed = time.monotonic() - job.started - job.paused_seconds
        bars = len(candles)
        if evaluations and bars:
            per_bar = eval_seconds * 1000.0 / evaluations / bars
            self._timing[cfg.backtest.engine] = round(per_bar, 5)
        analysis = {
            "sensitivity": sensitivity,
            "cluster": cluster,
            "robustness": robust,
            "deflated_sharpe": deflated,
            "regimes": regimes,
            "warnings": warnings,
            "comparison": comparison,
            "compliance": compliance_report(best["metrics"], cfg.constraints) if best else [],
            "curves": {"baseline": baseline_curve or [], "optimized": best_curve or []},
            "heatmap_pairs": [list(p) for p in an.heatmap_pairs(cfg.optimized)],
            "optimized_params": [p.name for p in cfg.optimized],
            "stats": {
                "evaluations": evaluations,
                "unique_results": len(rows),
                "errors": job.errors,
                "elapsed_seconds": round(elapsed, 2),
                "backtest_seconds": round(eval_seconds, 2),
                "workers": self.workers,
                "bars": bars,
                "data_from": candles.index[0].strftime("%Y-%m-%d"),
                "data_to": candles.index[-1].strftime("%Y-%m-%d"),
            },
            "config_warnings": list(cfg.warnings),
        }
        self._set_phase(job, "saving", f"{len(rows)} results")
        self.store.save_results(job.run_id, rows)
        fields: dict[str, Any] = {
            "status": "completed",
            "completed_at": _now(),
            "tested_combinations": len(rows),
            "valid_combinations": sum(1 for r in rows if r["constraints_met"]),
            "analysis": analysis,
            "robustness_score": robust.get("score"),
            "deflated_sharpe": deflated.get("deflated_sharpe"),
        }
        if best:
            fields.update(
                best_params=best["params"], best_score=best["score"], best_metrics=best["metrics"]
            )
        else:
            fields.update(best_params=None, best_score=None, best_metrics=None)
        if wf_report is not None:
            fields.update(
                walk_forward_results=wf_report,
                overfitted=wf_report["overfitted"],
                avg_train_score=wf_report["avg_train_score"],
                avg_test_score=wf_report["avg_test_score"],
            )
        self.store.update_run(job.run_id, **fields)
        log.info(
            "[optimize] run %s completed: %d results, %d valid, best=%s (%.1fs)",
            job.run_id[:8],
            len(rows),
            fields["valid_combinations"],
            None if not best else round(best["score"], 4),
            elapsed,
        )

    # ------------------------------------------------------------------
    # Apply / rollback / presets
    # ------------------------------------------------------------------

    def _manager(self) -> Any:
        if self._manager_getter is not None:
            return self._manager_getter()
        from backtest.forward.portfolio_manager import get_portfolio_manager

        return get_portfolio_manager()

    def apply(
        self,
        run_id: str,
        *,
        target: str = "none",
        params: dict | None = None,
        instance_id: str | None = None,
        confirm_live: bool = False,
        allow_unvalidated: bool = False,
        capital: float | None = None,
        name: str | None = None,
        user_id: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        notes: str | None = None,
        monte_carlo_acknowledged: bool = False,
        monte_carlo_profit_probability: float | None = None,
    ) -> dict[str, Any]:
        """Apply the best (or given) params. See module docstring for gates."""
        run = self._require(run_id)
        if target not in APPLY_TARGETS:
            raise OptimizationError(f"target must be one of {', '.join(APPLY_TARGETS)}")
        if run["status"] not in ("completed", "cancelled"):
            raise OptimizationError(f"run is {run['status']} — wait for it to finish", 409)
        new_params = dict(params or run.get("best_params") or {})
        if not new_params:
            raise OptimizationError("run has no valid parameter set to apply", 409)
        cfg = self.config_from_run(run)
        allowed = {p.name for p in cfg.parameters}
        unknown = set(new_params) - allowed
        if unknown:
            raise OptimizationError(f"unknown parameter(s): {', '.join(sorted(unknown))}")
        strategy_params = {k: v for k, v in new_params.items() if not k.startswith("engine.")}

        if target == "live":
            if not confirm_live:
                raise OptimizationError(
                    "applying to LIVE requires confirm_live=true (real capital at risk)", 400
                )
            if run.get("overfitted"):
                raise OptimizationError(
                    "walk-forward flagged this run as overfitted — refusing to apply to live", 409
                )
            if not run.get("walk_forward_enabled") and not allow_unvalidated:
                raise OptimizationError(
                    "run was not walk-forward validated — enable walk-forward or pass "
                    "allow_unvalidated=true",
                    409,
                )

        manager = None
        old_params: dict | None = None
        runner_restarted = False
        # PRD §6 reverse flow: the three-link chain, assembled where the
        # runner id is already known. `origin` is a SESSION handle from the
        # Backtest page, not a stored backtest record — the UI labels it that
        # way, because an audit entry pointing at a backtest that cannot be
        # opened is worse than one that admits it was a handle.
        origin = (run.get("backtest_config") or {}).get("sourceBacktestId") or None
        details: dict[str, Any] = {"target": target, "notes": notes}
        # PRD Part 2 §1: the audit row names the originating backtest so the
        # full chain — Backtest [id] → Optimize [id] → Runner [id] — is
        # readable from the audit trail alone, without opening the run page.
        details["originated_from_backtest_id"] = origin
        # PRD Part 2 §4's gate is a flag, not a block — the browser requires
        # the acknowledgement. Recording both the fact and the number means the
        # audit trail can show later that the check WAS run and what it said,
        # rather than only that someone said they had read it.
        details["monte_carlo"] = {
            "acknowledged": bool(monte_carlo_acknowledged),
            "profit_probability_pct": monte_carlo_profit_probability,
        }
        mode = {"paper": "paper", "live": "live", "ab_test": "paper"}.get(target)
        if target != "none":
            manager = self._manager()
            if target in ("paper", "live") and instance_id:
                runner = manager.get_runner(instance_id)
                if runner is None:
                    raise OptimizationError(f"runner {instance_id} not found", 404)
                old_cfg = runner.config
                if old_cfg.strategy_name != run["strategy_id"]:
                    raise OptimizationError(
                        f"runner {instance_id} runs {old_cfg.strategy_name}, not "
                        f"{run['strategy_id']}",
                        409,
                    )
                if old_cfg.mode != mode:
                    raise OptimizationError(
                        f"runner {instance_id} is a {old_cfg.mode} runner — target {target} "
                        "does not match",
                        409,
                    )
                old_params = dict(old_cfg.strategy_params or {})
                new_cfg = dataclasses.replace(
                    old_cfg, strategy_params=strategy_params, instance_id=None
                )
                try:
                    manager.control_runner(instance_id, "flatten")
                except Exception:  # noqa: BLE001 - a flat runner may refuse
                    log.info("[optimize] flatten before restart skipped", exc_info=True)
                manager.remove_runner(instance_id)
                new_id = manager.add_runner(new_cfg, start=True)
                runner_restarted = True
                details.update(
                    previous_instance_id=instance_id, new_instance_id=new_id, action="restart"
                )
            else:
                if mode == "live" and not instance_id:
                    raise OptimizationError(
                        "live apply needs an existing live runner (instance_id)", 400
                    )
                from backtest.forward.paper_runner import RunnerConfig

                bt = run.get("backtest_config") or {}
                default_name = f"opt-{run['strategy_id']}-{run_id[:6]}"
                if target == "ab_test":
                    default_name += "-B"
                new_cfg = RunnerConfig(
                    name=name or default_name,
                    strategy_name=run["strategy_id"],
                    allocated_capital=float(capital or bt.get("initialCapital") or 100000),
                    symbols=[str(bt.get("symbol") or "DEMO")],
                    timeframe=str(bt.get("timeframe") or "1day"),
                    strategy_params=strategy_params,
                    mode="paper",
                    # A runner is a trading path: an unnamed source means the
                    # broker feed, not a literal generated-data one.
                    source=str(bt.get("source") or default_broker_source()),
                )
                new_id = manager.add_runner(new_cfg, start=True)
                details.update(
                    new_instance_id=new_id,
                    action="spawn",
                    ab_control_instance_id=instance_id if target == "ab_test" else None,
                )
            try:
                manager._audit_log(
                    f"OPTIMIZE_APPLY {run['strategy_id']}",
                    scope=mode or "paper",
                    instance_id=details.get("new_instance_id"),
                    detail=f"run={run_id} target={target} "
                    f"origin_backtest={origin or 'none'} params={strategy_params}",
                )
            except Exception:  # noqa: BLE001
                pass

        preset = self.store.create_preset(
            strategy_id=run["strategy_id"],
            name=f"Applied {datetime.now(timezone.utc):%Y-%m-%d %H:%M}",
            params=new_params,
            source="optimization",
            description=f"Applied from optimization {run_id[:8]} ({target})",
            optimization_run_id=run_id,
            backtest_metrics=run.get("best_metrics"),
            created_by=user_id,
        )
        self.store.mark_preset_applied(preset["preset_id"])
        details["preset_id"] = preset["preset_id"]
        baseline_metrics = run.get("baseline_metrics") or {}
        best_metrics = run.get("best_metrics") or {}
        expected = {
            k: {"before": baseline_metrics.get(k), "after": best_metrics.get(k)}
            for k in ("sharpe", "total_return", "max_drawdown", "win_rate", "total_trades")
        }
        chain = [
            {"step": "backtest", "id": origin, "label": "Backtest result"},
            {"step": "optimize", "id": run_id, "label": "Optimize run"},
            {
                "step": "runner",
                "id": details.get("new_instance_id") or instance_id,
                "label": "Paper runner" if mode != "live" else "Live runner",
            },
        ]
        details["chain"] = chain
        # PRD Part 2 §2 — the audit row is the last place a run's data can be
        # checked, long after the results page has scrolled away. Copied in
        # rather than re-derived: the attestation is a fact about the candles
        # the search loaded, and those are not here to measure again.
        details["data_attestation"] = run.get("data_attestation")
        audit = self.store.add_audit(
            run_id=run_id,
            strategy_id=run["strategy_id"],
            action="apply",
            action_details=details,
            old_params=old_params or run.get("baseline_params"),
            new_params=new_params,
            params_diff=params_diff(old_params or run.get("baseline_params"), new_params),
            applied_to_bucket=run.get("bucket_id") or (mode if mode else None),
            applied_to_mode=mode,
            runner_restarted=runner_restarted,
            expected_impact=expected,
            requires_approval=target == "live",
            approved_by=user_id if target == "live" else None,
            approved_at=_now() if target == "live" else None,
            user_id=user_id,
            ip_address=ip_address,
            user_agent=(user_agent or "")[:255],
        )
        log.info(
            "[optimize] applied run %s to %s (audit %s)", run_id[:8], target, audit["audit_id"][:8]
        )
        return {"audit": audit, "preset": preset, "instance_id": details.get("new_instance_id")}

    def rollback(
        self,
        audit_id: str,
        *,
        user_id: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> dict:
        """Undo an ``apply``: restore old params / remove a spawned runner."""
        entry = self.store.get_audit(audit_id)
        if entry is None:
            raise OptimizationError("audit entry not found", 404)
        if entry["action"] != "apply":
            raise OptimizationError("only apply actions can be rolled back", 409)
        details = entry.get("action_details") or {}
        if details.get("rolled_back"):
            raise OptimizationError("already rolled back", 409)
        action = details.get("action")
        new_id = details.get("new_instance_id")
        result: dict[str, Any] = {"action": "record_only"}
        if action in ("restart", "spawn") and new_id:
            manager = self._manager()
            runner = manager.get_runner(new_id)
            if runner is None:
                raise OptimizationError(f"runner {new_id} no longer exists", 409)
            if action == "restart":
                old_cfg = dataclasses.replace(
                    runner.config,
                    strategy_params=dict(entry.get("old_params") or {}),
                    instance_id=None,
                )
                try:
                    manager.control_runner(new_id, "flatten")
                except Exception:  # noqa: BLE001
                    pass
                manager.remove_runner(new_id)
                restored = manager.add_runner(old_cfg, start=True)
                result = {"action": "restart", "instance_id": restored}
            else:
                try:
                    manager.control_runner(new_id, "flatten")
                except Exception:  # noqa: BLE001
                    pass
                manager.remove_runner(new_id)
                result = {"action": "removed", "instance_id": new_id}
            try:
                manager._audit_log(
                    f"OPTIMIZE_ROLLBACK {entry['strategy_id']}",
                    scope=entry.get("applied_to_mode") or "paper",
                    instance_id=result.get("instance_id"),
                    detail=f"audit={audit_id}",
                )
            except Exception:  # noqa: BLE001
                pass
        self.store.update_audit(
            audit_id,
            action_details={**details, "rolled_back": True, "rolled_back_at": _now().isoformat()},
        )
        audit = self.store.add_audit(
            run_id=entry.get("run_id"),
            strategy_id=entry["strategy_id"],
            action="rollback",
            action_details={"rollback_of": audit_id, **result},
            old_params=entry.get("new_params"),
            new_params=entry.get("old_params"),
            params_diff=params_diff(entry.get("new_params"), entry.get("old_params")),
            applied_to_bucket=entry.get("applied_to_bucket"),
            applied_to_mode=entry.get("applied_to_mode"),
            runner_restarted=result["action"] == "restart",
            user_id=user_id,
            ip_address=ip_address,
            user_agent=(user_agent or "")[:255],
        )
        return {"audit": audit, **result}

    def save_preset_from_run(
        self,
        run_id: str,
        *,
        name: str,
        description: str | None = None,
        params: dict | None = None,
        created_by: str | None = None,
    ) -> dict:
        run = self._require(run_id)
        chosen = params or run.get("best_params")
        if not chosen:
            raise OptimizationError("run has no valid parameter set", 409)
        preset = self.store.create_preset(
            strategy_id=run["strategy_id"],
            name=name,
            params=chosen,
            source="optimization",
            description=description,
            optimization_run_id=run_id,
            backtest_metrics=run.get("best_metrics") if params is None else None,
            created_by=created_by,
        )
        self.store.add_audit(
            run_id=run_id,
            strategy_id=run["strategy_id"],
            action="save_preset",
            new_params=chosen,
            action_details={"preset_id": preset["preset_id"], "name": preset["name"]},
            user_id=created_by,
        )
        return preset


def build_default_service(app_config: dict | None = None) -> OptimizationService | None:
    """Attach to the configured database; ``None`` if unavailable.

    ``OPTIMIZATION_DB=off`` disables the feature (API answers 503).
    """
    import os

    if os.getenv("OPTIMIZATION_DB", "auto").strip().lower() in ("0", "off", "false", "no"):
        return None
    try:
        from backtest.db import DatabaseManager

        manager = DatabaseManager.from_env()
        manager.connect()
        store = OptimizationStore(manager)
        store.ensure_schema()
        source = (app_config or {}).get("BACKTEST_SOURCE") or default_backtest_source()
        service = OptimizationService(store, default_source=source)
        log.info(
            "[optimize] engine attached (%s, %d workers)", manager.config.safe_url, service.workers
        )
        return service
    except Exception:  # noqa: BLE001 - optional feature
        log.warning("[optimize] database unavailable — optimization disabled", exc_info=True)
        return None


__all__ = [
    "APPLY_TARGETS",
    "ConfigValidationError",
    "OptimizationError",
    "OptimizationService",
    "build_default_service",
    "default_loader",
    "params_diff",
]
