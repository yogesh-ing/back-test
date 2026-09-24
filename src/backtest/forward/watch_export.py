"""PnL-vs-spot watch — per-bar series + end-of-day export (2026-09-24).

Owner request: "add instances of ATM instant buy for NIFTY and BANKNIFTY so we
can test the price fluctuation and PnL" and later "I will be away before the
market ends" — so the export must run **server-side without a browser open**.

What this module does:

* **Series capture** (in ``StrategyRunner.record_watch_point``): one row per
  closed bar while an option structure is open — ``ts, spot, option_pnl,
  equity``. Live (`source=mstock`) runners only; synthetic books are excluded
  per the owner's "synthetic stuff must vanish" rule.
* **Export**: :func:`export_runner_chart` writes a PNG (spot vs MTM PnL, twin
  axes) plus a CSV next to it into ``charts/<YYYY-MM-DD>/``, and
  :func:`export_all_watch_runners` sweeps every running live option runner.
* **Scheduler**: :class:`MarketCloseExporter` is a daemon thread that sleeps
  until 15:30 IST on trading days, fires the sweep once, and re-arms for the
  next day. Started by the web app; a missed close (app was down) is caught
  up on boot.
"""

from __future__ import annotations

import csv
import logging
import threading
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("backtest.forward.watch_export")

#: Project root (charts/ lives there).
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
CHARTS_DIR = _PROJECT_ROOT / "charts"

#: Market close (NSE) in IST — the auto-export fires at this time.
MARKET_CLOSE_HHMM = (15, 30)

#: IST offset (no DST in India).
_IST = timezone(timedelta(hours=5, minutes=30), name="IST")


def _ist_now() -> datetime:
    return datetime.now(_IST)


def charts_dir_for(day: Optional[Any] = None) -> Path:
    """``charts/<YYYY-MM-DD>/`` for a date (default: today in IST)."""
    d = day or _ist_now().date()
    return CHARTS_DIR / d.isoformat()


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def export_runner_chart(
    runner: Any,
    out_dir: Optional[Path] = None,
) -> Optional[Dict[str, Any]]:
    """Write PNG + CSV for one runner's watch series.

    Returns ``{"png": path, "csv": path, "points": n}`` or ``None`` when
    there is nothing to export (no series / no matplotlib). Never raises.
    """
    series = getattr(runner, "watch_series", None)
    if not series:
        return None

    name = getattr(runner.config, "name", "runner")
    safe = "".join(c if c.isalnum() or c in "-_ " else "_" for c in name).strip() or "runner"
    out = out_dir or charts_dir_for()
    try:
        out.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        logger.warning("charts dir creation failed: %s", out, exc_info=True)
        return None

    csv_path = out / f"{safe}.csv"
    try:
        with csv_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["ts", "spot", "option_pnl", "equity"])
            for row in series:
                writer.writerow([
                    row.get("ts", ""),
                    row.get("spot", ""),
                    row.get("option_pnl", ""),
                    row.get("equity", ""),
                ])
    except Exception:  # noqa: BLE001
        logger.warning("watch CSV export failed for %s", name, exc_info=True)
        return None

    png_path = out / f"{safe}.png"
    try:
        import matplotlib

        matplotlib.use("Agg")  # headless — no browser, no display
        import matplotlib.pyplot as plt

        ts = [row.get("ts", "") for row in series]
        spots = [row.get("spot") for row in series]
        pnls = [row.get("option_pnl") for row in series]

        fig, ax1 = plt.subplots(figsize=(11, 5.5))
        ax1.set_xlabel("time")
        ax1.set_ylabel("underlying spot", color="tab:blue")
        ax1.plot(ts, spots, color="tab:blue", linewidth=1.2, label="spot")
        ax1.tick_params(axis="x", rotation=45, labelsize=8)

        ax2 = ax1.twinx()
        ax2.set_ylabel("option MTM PnL (₹)", color="tab:red")
        ax2.plot(ts, pnls, color="tab:red", linewidth=1.4, label="option PnL")
        ax2.axhline(0, color="grey", linewidth=0.6, linestyle="--")

        leg = getattr(runner, "last_option_label", None) or ""
        fig.suptitle(f"{name} — spot vs option PnL {leg}".strip(), fontsize=11)
        fig.tight_layout()
        fig.savefig(png_path, dpi=110)
        plt.close(fig)
    except ImportError:
        logger.info("matplotlib unavailable — CSV-only export for %s", name)
        png_path = None  # type: ignore[assignment]
    except Exception:  # noqa: BLE001 — a plotting failure must not kill the sweep
        logger.warning("watch PNG export failed for %s", name, exc_info=True)
        png_path = None  # type: ignore[assignment]

    return {
        "png": str(png_path) if png_path else None,
        "csv": str(csv_path),
        "points": len(series),
    }


def export_all_watch_runners(manager: Any = None) -> List[Dict[str, Any]]:
    """Export every running live option runner. Returns per-runner results."""
    if manager is None:
        from backtest.forward.portfolio_manager import get_portfolio_manager

        manager = get_portfolio_manager()
    results: List[Dict[str, Any]] = []
    try:
        runners = list(manager._runners.values())
    except Exception:  # noqa: BLE001
        logger.warning("watch export sweep failed to list runners", exc_info=True)
        return results
    for runner in runners:
        cfg = getattr(runner, "config", None)
        if cfg is None:
            continue
        is_live_option = (
            str(getattr(cfg, "source", "")).lower() == "mstock"
            and str((getattr(cfg, "instrument", {}) or {}).get("type", "")) == "option"
        )
        if not is_live_option:
            continue
        result = export_runner_chart(runner)
        if result:
            results.append({"runner": cfg.name, **result})
    if results:
        logger.info("watch export: %d runner(s) exported to %s", len(results), charts_dir_for())
    return results


# ---------------------------------------------------------------------------
# Scheduler — fires once at market close IST, then re-arms
# ---------------------------------------------------------------------------


class MarketCloseExporter:
    """Daemon thread: sleep until 15:30 IST, export, re-arm for the next day."""

    def __init__(self, manager: Any = None, check_interval_s: float = 60.0) -> None:
        self._manager = manager
        self._check_interval_s = float(check_interval_s)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_fired_day: Optional[str] = None

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> bool:
        """Start the scheduler thread (idempotent)."""
        if self._thread is not None and self._thread.is_alive():
            return False
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="watch-market-close-exporter", daemon=True
        )
        self._thread.start()
        logger.info(
            "market-close exporter armed: fires %02d:%02d IST daily (charts/)",
            *MARKET_CLOSE_HHMM,
        )
        return True

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5.0)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -- loop ---------------------------------------------------------------

    def _loop(self) -> None:
        # Boot catch-up: if the app started after the close on a trading day
        # (e.g. a restart at 16:00), export today's series once now.
        self._maybe_fire(catchup=True)
        while not self._stop.is_set():
            self._maybe_fire()
            self._stop.wait(self._check_interval_s)

    def _maybe_fire(self, catchup: bool = False) -> bool:
        now = _ist_now()
        today = now.date().isoformat()
        if today == self._last_fired_day:
            return False
        target = dtime(*MARKET_CLOSE_HHMM)
        due = now.time() >= target if not catchup else False
        # Catch-up: fired only when today's close already passed (weekend is
        # harmless — there will be no series to export).
        if catchup and now.time() < target:
            return False
        if not due and not catchup:
            return False
        self._last_fired_day = today
        try:
            results = export_all_watch_runners(self._manager)
            logger.info(
                "market-close export fired for %s: %d runner(s)", today, len(results)
            )
            return True
        except Exception:  # noqa: BLE001 — the scheduler survives anything
            logger.exception("market-close export failed for %s", today)
            return False
