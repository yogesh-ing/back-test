"""Cross-broker aggregation + execution-quality analytics (PRD-003).

Answers the four questions the per-strategy ``/analytics`` tab cannot:

1. **Which broker is best for which strategy?**  → :meth:`compare_brokers`
2. **Am I getting good execution quality?**     → :meth:`get_execution_quality`
3. **Should I move a strategy to another broker?** → :meth:`migration_impact`
4. **What is my TOTAL portfolio performance?**  → :meth:`get_summary`

Data provenance (read this before trusting a number)
----------------------------------------------------
*Trades* come from :meth:`AnalyticsService.collect_books` — the same merged
memory+DB history the ``/analytics`` overview already uses, tagged with the
owning runner's broker. *Orders* come from the command-center
:class:`~backtest.forward.paper_runner.OrderLedger`, which is **in-memory and
session-scoped** (a known limitation of the Orders tab too — see
``PROJECT-CONTEXT.md``). Every response therefore ships a ``data_quality``
block saying how many orders were scanned, whether the scan was truncated, and
whether any broker has enough data to compare. A broker-aware analytics panel
that silently mixes "all time" trades with "this session" orders and never
says so is worse than no panel at all.

Deliberate deviations from the PRD's JSON sketch
-----------------------------------------------
The PRD sketches fractions (``max_drawdown: -0.095``, ``win_rate: 0.58``).
This module returns **percentages with an explicit ``_pct`` suffix**
(``max_drawdown_pct: 9.5``, ``win_rate: 58.0``) because every other number on
``/analytics`` is a percentage and the wireframes render them as such; a page
that mixes the two is a page that silently reports 9.5% as "950% drawdown".
Slippage stays in basis points, as the PRD specifies.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from backtest.analytics.portfolio import AnalyticsService, compute_metrics_from_trades
from backtest.analytics.stats import (
    ALPHA,
    MIN_SAMPLE,
    describe,
    mann_whitney_u,
    rate_significance_block,
)
from backtest.logging_config import get_logger

log = get_logger(__name__)

#: Period label → day count. ``all_time`` is unbounded.
PERIOD_DAYS: Dict[str, Optional[int]] = {
    "7d": 7,
    "30d": 30,
    "90d": 90,
    "1y": 365,
    "all_time": None,
}

#: Upper bound on ledger rows pulled per request. The ledger itself caps at
#: ``MAX_LEDGER_ORDERS`` (100k); scanning all of it on every 30s dashboard
#: poll is not worth it, so we scan the newest N and SAY when we truncated.
MAX_LEDGER_SCAN = 50_000

#: An order still working after this long is "aged" (PRD-003 §3.3).
ORDER_AGING_S = 60.0

#: Slippage histogram buckets, in bps.
SLIPPAGE_BUCKETS: Tuple[Tuple[str, float, Optional[float]], ...] = (
    ("0-5 bps", 0.0, 5.0),
    ("5-10 bps", 5.0, 10.0),
    ("10-20 bps", 10.0, 20.0),
    ("20+ bps", 20.0, None),
)

#: Keys of :meth:`CrossBrokerAnalyticsService._execution_quality` that carry
#: raw per-order sample vectors. They feed the statistical tests and are
#: stripped before the block reaches a response payload.
_INTERNAL_QUALITY_KEYS = ("slippage_bps_samples", "fill_time_samples_values")

#: Brand display names. ``mStock`` is a brand, not a title-case of its slug.
_BROKER_LABELS = {"mstock": "mStock", "dhan": "Dhan", "paper": "Paper (sim)"}

#: A P&L change inside this band is noise, not a reason to move a strategy.
MIGRATION_NOISE_PCT = 2.0

#: Metric catalogue for ``/compare`` — key → (label, higher_is_better, unit).
COMPARABLE_METRICS: Dict[str, Tuple[str, bool, str]] = {
    "sharpe": ("Sharpe ratio", True, ""),
    "net_pnl": ("Net P&L", True, "₹"),
    "win_rate": ("Win rate", True, "%"),
    "max_drawdown_pct": ("Max drawdown", False, "%"),
    "avg_slippage_bps": ("Avg slippage", False, "bps"),
    "fill_rate_pct": ("Fill rate", True, "%"),
    "fill_time_sec": ("Avg fill time", False, "s"),
    "rejection_rate_pct": ("Rejection rate", False, "%"),
    "total_trades": ("Trades", True, ""),
}


def validate_metric_names(names: Sequence[str]) -> List[str]:
    """Validate a caller-supplied metric list, or raise ``ValueError``.

    The API layer uses this instead of trusting the request body: an unknown
    key that reached ``COMPARABLE_METRICS[key]`` would be a 500 on a typo.
    """
    unknown = [str(n) for n in names if str(n) not in COMPARABLE_METRICS]
    if unknown:
        raise ValueError(
            f"unknown metric(s): {', '.join(sorted(unknown))}; "
            f"valid: {', '.join(sorted(COMPARABLE_METRICS))}"
        )
    return [str(n) for n in names]


#: Execution-quality weights per trade frequency (PRD-003 Story 5).
_FREQUENCY_WEIGHTS: Dict[str, Dict[str, float]] = {
    "high": {
        "fill_rate_pct": 0.30,
        "fill_time_sec": 0.25,
        "avg_slippage_bps": 0.25,
        "rejection_rate_pct": 0.20,
    },
    "medium": {
        "fill_rate_pct": 0.20,
        "fill_time_sec": 0.10,
        "avg_slippage_bps": 0.40,
        "rejection_rate_pct": 0.30,
    },
    "low": {
        "fill_rate_pct": 0.10,
        "fill_time_sec": 0.00,
        "avg_slippage_bps": 0.55,
        "rejection_rate_pct": 0.35,
    },
}

#: Strategy-style nudges on top of the frequency weights (PRD-003 Story 5).
_STYLE_BOOSTS: Dict[str, Dict[str, float]] = {
    "scalper": {"fill_time_sec": 0.20, "fill_rate_pct": 0.10},
    "high_frequency": {"fill_time_sec": 0.20, "fill_rate_pct": 0.10},
    "swing": {"avg_slippage_bps": 0.15},
    "positional": {"avg_slippage_bps": 0.15},
    "options": {"avg_slippage_bps": 0.15, "rejection_rate_pct": 0.15},
    "option": {"avg_slippage_bps": 0.15, "rejection_rate_pct": 0.15},
    "intraday": {"fill_time_sec": 0.10, "avg_slippage_bps": 0.10},
}

#: Weight/metric key → the key the execution-quality block actually uses.
#: They differ in exactly one place (``fill_time_sec`` vs ``avg_fill_time_sec``),
#: which is the kind of drift that silently zeroes a scoring term.
_QUALITY_KEY_FOR_METRIC: Dict[str, str] = {
    "fill_rate_pct": "fill_rate_pct",
    "fill_time_sec": "avg_fill_time_sec",
    "avg_slippage_bps": "avg_slippage_bps",
    "rejection_rate_pct": "rejection_rate_pct",
}


def _round(value: Optional[float], digits: int = 2) -> Optional[float]:
    """Round for JSON, preserving ``None`` (never turns "no data" into 0.0)."""
    if value is None:
        return None
    try:
        fv = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(fv) or math.isinf(fv):
        return None
    return round(fv, digits)


def _safe_div(numerator: float, denominator: float, default: Optional[float] = None):
    """Division that returns ``default`` instead of raising or dividing by 0."""
    try:
        if not denominator:
            return default
        return numerator / denominator
    except (TypeError, ZeroDivisionError):
        return default


def _parse_ts(value: Any) -> Optional[datetime]:
    """Parse a ledger timestamp. Naive values are UTC (ledger convention)."""
    if not value:
        return None
    try:
        if isinstance(value, datetime):
            dt = value
        else:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _period_cutoff(period: str, now: Optional[datetime] = None) -> Optional[datetime]:
    """Absolute cutoff instant for a period label, ``None`` for all-time."""
    days = PERIOD_DAYS.get(str(period or "30d"), 30)
    if days is None:
        return None
    return (now or datetime.now(timezone.utc)) - timedelta(days=days)


class CrossBrokerAnalyticsService:
    """Broker-aware aggregation over runners, trades and the order ledger."""

    def __init__(self, mgr: Any = None):
        # One AnalyticsService per instance so the two views of the same
        # portfolio can never read different trade histories.
        self.analytics = AnalyticsService()
        if mgr is not None:
            self.analytics.mgr = mgr
        self.mgr = self.analytics.mgr

    # ------------------------------------------------------------------
    # Order ledger → broker-attributed, period-scoped rows
    # ------------------------------------------------------------------

    def _ledger(self) -> Any:
        return getattr(self.mgr, "ledger", None)

    def _runner_index(
        self, period: str = "all_time", mode: Optional[str] = None
    ) -> Dict[str, Dict[str, Any]]:
        """``instance_id`` → broker / strategy / segment / capital.

        Orders carry no broker of their own — the venue is a property of the
        runner that sent them, exactly as the Orders tab's ``broker`` column
        and the risk page's per-broker rollup already resolve it.
        """
        index: Dict[str, Dict[str, Any]] = {}
        for book in self.analytics.collect_books(period=period, mode=mode):
            index[book["instance_id"]] = {
                "broker": book["broker"],
                "execution_broker": book["execution_broker"],
                "segment": book["segment"],
                "strategy_name": book["strategy_name"],
                "name": book["name"],
                "allocated_capital": book["allocated_capital"],
            }
        return index

    def _orders(
        self,
        period: str = "30d",
        broker: Optional[str] = None,
        strategy: Optional[str] = None,
        mode: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """``(rows, provenance)`` — ledger orders in period, broker-attributed.

        ``provenance`` records the scan size, whether it was truncated and how
        many orders had no live runner (a removed runner's orders are NOT
        reassigned to another broker — they drop out, mirroring
        :meth:`PortfolioManager.list_orders`).
        """
        ledger = self._ledger()
        index = self._runner_index(period=period, mode=mode)
        provenance: Dict[str, Any] = {
            "source": "order_ledger",
            "session_scoped": True,
            "note": (
                "Order history is in-memory and session-scoped (a restart empties "
                "the ledger, exactly as the Orders tab reports). Trade metrics are "
                "merged memory+DB and survive restarts — the two are NOT the same "
                "history length."
            ),
            "ledger_total": 0,
            "scanned": 0,
            "truncated": False,
            "in_period": 0,
            "orphaned": 0,
        }
        if ledger is None:
            provenance["available"] = False
            return [], provenance

        provenance["available"] = True
        ledger_total = int(ledger.order_count)
        provenance["ledger_total"] = ledger_total
        raw = ledger.snapshot(limit=MAX_LEDGER_SCAN)
        provenance["scanned"] = len(raw)
        provenance["truncated"] = ledger_total > len(raw)

        cutoff = _period_cutoff(period, now=now)
        broker_key = str(broker).strip().lower() if broker else None
        strategy_key = str(strategy).strip().lower() if strategy else None

        rows: List[Dict[str, Any]] = []
        for order in raw:
            created = _parse_ts(order.get("created_ts"))
            if cutoff is not None and (created is None or created < cutoff):
                continue
            owner = index.get(order.get("instance_id"))
            if owner is None:
                provenance["orphaned"] += 1
                continue
            if broker_key and str(owner["broker"]).lower() != broker_key:
                continue
            if strategy_key and str(owner["strategy_name"] or "").lower() != strategy_key:
                continue
            row = dict(order)
            row["broker"] = owner["broker"]
            row["segment"] = owner["segment"]
            row["strategy_name"] = owner["strategy_name"]
            row["runner_name"] = owner["name"]
            row["_created"] = created
            row["_slippage_bps"] = _order_slippage_bps(order)
            row["_fill_time_sec"] = _order_fill_time_sec(order)
            row["_notional"] = _order_notional(order)
            rows.append(row)

        provenance["in_period"] = len(rows)
        return rows, provenance

    # ------------------------------------------------------------------
    # Execution quality
    # ------------------------------------------------------------------

    def _execution_quality(self, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Aggregate one set of order rows into the execution-quality block.

        Rate definitions (stated here because the PRD's sketch is ambiguous and
        a fill rate that moves while orders are in flight is a lie):

        * ``resolved`` = filled + cancelled + rejected — orders that reached a
          terminal state. ``fill_rate_pct`` and ``rejection_rate_pct`` are
          taken over ``resolved``, so a broker with 24 working orders right
          now is not penalised for them.
        * ``pending_orders`` is reported separately and is what the
          ``stale_order_pct`` (aging) figure is computed over.
        """
        counts = {"filled": 0, "rejected": 0, "cancelled": 0, "pending": 0, "other": 0}
        slippage_samples: List[float] = []
        fill_time_samples: List[float] = []
        notionals: List[float] = []
        ages: List[float] = []

        for row in rows:
            status = str(row.get("status") or "").upper()
            if status == "FILLED":
                counts["filled"] += 1
            elif status == "REJECTED":
                counts["rejected"] += 1
            elif status == "CANCELLED":
                counts["cancelled"] += 1
            elif status == "PENDING":
                counts["pending"] += 1
            else:
                counts["other"] += 1
            if row.get("_slippage_bps") is not None:
                slippage_samples.append(row["_slippage_bps"])
            if row.get("_fill_time_sec") is not None:
                fill_time_samples.append(row["_fill_time_sec"])
            if row.get("_notional"):
                notionals.append(row["_notional"])
            if status == "PENDING":
                ages.append(_age_seconds(row.get("created_ts")))

        resolved = counts["filled"] + counts["rejected"] + counts["cancelled"]
        slip_mean, slip_med, slip_std = describe(slippage_samples)
        time_mean, time_med, _ = describe(fill_time_samples)
        stale = [a for a in ages if a > ORDER_AGING_S]

        return {
            "total_orders": len(rows),
            "filled_orders": counts["filled"],
            "rejected_orders": counts["rejected"],
            "cancelled_orders": counts["cancelled"],
            "pending_orders": counts["pending"],
            "resolved_orders": resolved,
            "fill_rate_pct": _round(_safe_div(counts["filled"] * 100.0, resolved)),
            "rejection_rate_pct": _round(_safe_div(counts["rejected"] * 100.0, resolved)),
            "cancel_rate_pct": _round(_safe_div(counts["cancelled"] * 100.0, resolved)),
            "avg_slippage_bps": slip_mean,
            "median_slippage_bps": slip_med,
            "slippage_std_dev_bps": slip_std,
            "slippage_samples": len(slippage_samples),
            "avg_fill_time_sec": time_mean,
            "median_fill_time_sec": time_med,
            "fill_time_samples": len(fill_time_samples),
            "stale_order_pct": _round(_safe_div(len(stale) * 100.0, counts["pending"])),
            "stale_orders": len(stale),
            "avg_order_notional": _round(sum(notionals) / len(notionals)) if notionals else None,
            "slippage_bps_samples": [round(s, 4) for s in slippage_samples],
            "fill_time_samples_values": [round(s, 3) for s in fill_time_samples],
        }

    @staticmethod
    def _public_quality(quality: Dict[str, Any]) -> Dict[str, Any]:
        """Strip the raw per-order sample vectors from a response payload."""
        return {k: v for k, v in quality.items() if k not in _INTERNAL_QUALITY_KEYS}

    def _slippage_distribution(self, samples: Sequence[float]) -> List[Dict[str, Any]]:
        """Histogram of per-order slippage over the PRD's fixed bps buckets."""
        out: List[Dict[str, Any]] = []
        for label, low, high in SLIPPAGE_BUCKETS:
            if high is None:
                count = sum(1 for s in samples if s >= low)
            else:
                count = sum(1 for s in samples if low <= s < high)
            out.append({"bucket": label, "count": count})
        return out

    def _time_series(
        self, rows: List[Dict[str, Any]], max_points: int = 30
    ) -> List[Dict[str, Any]]:
        """Daily avg slippage + fill rate, oldest → newest, capped at ``max_points``."""
        by_day: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            created = row.get("_created")
            if created is None:
                continue
            day = created.date().isoformat()
            bucket = by_day.setdefault(day, {"slip": [], "filled": 0, "resolved": 0})
            status = str(row.get("status") or "").upper()
            if status in ("FILLED", "REJECTED", "CANCELLED"):
                bucket["resolved"] += 1
                if status == "FILLED":
                    bucket["filled"] += 1
            if row.get("_slippage_bps") is not None:
                bucket["slip"].append(row["_slippage_bps"])

        series = []
        for day in sorted(by_day):
            bucket = by_day[day]
            series.append(
                {
                    "date": day,
                    "avg_slippage_bps": (
                        _round(sum(bucket["slip"]) / len(bucket["slip"]))
                        if bucket["slip"]
                        else None
                    ),
                    "fill_rate_pct": _round(
                        _safe_div(bucket["filled"] * 100.0, bucket["resolved"])
                    ),
                    "orders": bucket["resolved"],
                }
            )
        return series[-max_points:]

    @staticmethod
    def _degradation(series: List[Dict[str, Any]], window: int = 7) -> Optional[Dict[str, Any]]:
        """Story 2: did fill rate drop over the last ``window`` days?

        Compares the most recent ``window`` days against the days before it
        and only fires on a real sample (≥ ``MIN_SAMPLE`` resolved orders per
        side) — a 2-order "92% → 0%" is not a degradation, it is noise.
        """
        if len(series) < 2:
            return None
        recent, prior = series[-window:], series[:-window] or series[:1]

        def _side(rows: List[Dict[str, Any]]) -> Tuple[Optional[float], int]:
            resolved = sum(int(r.get("orders") or 0) for r in rows)
            filled = sum(
                int(r.get("orders") or 0) * (r.get("fill_rate_pct") or 0) / 100.0 for r in rows
            )
            rate = _round(_safe_div(filled * 100.0, resolved))
            return rate, resolved

        recent_rate, recent_n = _side(recent)
        prior_rate, prior_n = _side(prior)
        if recent_rate is None or prior_rate is None:
            return None
        if recent_n < MIN_SAMPLE or prior_n < MIN_SAMPLE:
            return {
                "alert_type": "execution_sample_thin",
                "severity": "info",
                "message": (
                    f"Only {recent_n} resolved orders in the last {len(recent)} days "
                    f"(need ≥{MIN_SAMPLE}) — fill-rate trend is not yet meaningful."
                ),
                "recent_fill_rate_pct": recent_rate,
                "prior_fill_rate_pct": prior_rate,
            }
        delta = recent_rate - prior_rate
        if delta >= -2.0:
            return None
        return {
            "alert_type": "fill_rate_decline",
            "severity": "critical" if delta <= -5.0 else "warning",
            "message": (
                f"Fill rate fell {abs(delta):.1f}pp "
                f"({prior_rate:.1f}% → {recent_rate:.1f}%) over the last {len(recent)} days."
            ),
            "drop_pct": _round(delta),
            "recent_fill_rate_pct": recent_rate,
            "prior_fill_rate_pct": prior_rate,
            "recent_orders": recent_n,
            "prior_orders": prior_n,
        }

    def _by_strategy(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            groups.setdefault(str(row.get("strategy_name") or "unknown"), []).append(row)
        out = []
        for name, group in groups.items():
            quality = self._execution_quality(group)
            out.append(
                {
                    "strategy": name,
                    "broker": group[0].get("broker"),
                    "total_orders": quality["total_orders"],
                    "avg_slippage_bps": quality["avg_slippage_bps"],
                    "fill_rate_pct": quality["fill_rate_pct"],
                    "rejection_rate_pct": quality["rejection_rate_pct"],
                    "avg_fill_time_sec": quality["avg_fill_time_sec"],
                }
            )
        out.sort(key=lambda r: (-(r["total_orders"] or 0), str(r["strategy"])))
        return out

    def get_execution_quality(
        self,
        broker: Optional[str] = None,
        period: str = "30d",
        strategy: Optional[str] = None,
        mode: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Endpoint 2 — execution detail for one broker (or all of them)."""
        rows, provenance = self._orders(period=period, broker=broker, strategy=strategy, mode=mode)
        quality = self._execution_quality(rows)
        series = self._time_series(rows)
        degradation = self._degradation(series)

        brokers = sorted({str(r.get("broker")) for r in rows if r.get("broker")})
        payload: Dict[str, Any] = {
            "broker": str(broker).lower() if broker else None,
            "period": period,
            "strategy": strategy,
            "execution_quality": self._public_quality(quality),
            "slippage_distribution": self._slippage_distribution(quality["slippage_bps_samples"]),
            "by_strategy": self._by_strategy(rows),
            "time_series": series,
            "degradation_alert": degradation,
            "brokers": brokers,
            "data_quality": provenance,
            "notes": _execution_notes(quality),
        }

        if broker:
            all_rows, _ = self._orders(period=period, mode=mode)
            payload["peer_benchmarks"] = self._peer_benchmarks(all_rows, broker)
        return payload

    def _peer_benchmarks(self, rows: List[Dict[str, Any]], broker: str) -> List[Dict[str, Any]]:
        """The same quality block for every OTHER broker, side by side."""
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            groups.setdefault(str(row.get("broker")), []).append(row)
        out = []
        for name, group in groups.items():
            quality = self._execution_quality(group)
            out.append(
                {
                    "broker": name,
                    "total_orders": quality["total_orders"],
                    "avg_slippage_bps": quality["avg_slippage_bps"],
                    "fill_rate_pct": quality["fill_rate_pct"],
                    "rejection_rate_pct": quality["rejection_rate_pct"],
                    "avg_fill_time_sec": quality["avg_fill_time_sec"],
                    "is_subject": name == broker,
                }
            )
        out.sort(key=lambda r: str(r["broker"]))
        return out

    # ------------------------------------------------------------------
    # Endpoint 1 — cross-broker summary
    # ------------------------------------------------------------------

    def get_summary(self, period: str = "30d", mode: Optional[str] = None) -> Dict[str, Any]:
        """Portfolio totals, per-broker and per-segment rollups, rankings."""
        books = self.analytics.collect_books(period=period, mode=mode)
        rows, provenance = self._orders(period=period, mode=mode)

        all_trades: List[Dict[str, Any]] = []
        total_capital = 0.0
        for book in books:
            total_capital += book["allocated_capital"]
            all_trades.extend(book["trades"])

        portfolio = compute_metrics_from_trades(
            all_trades, allocated_capital=total_capital or 100_000.0
        )
        gross_pnl = sum(
            float(t.get("pnl") or 0.0) for t in all_trades if float(t.get("pnl") or 0) > 0
        )

        by_broker: List[Dict[str, Any]] = []
        execution_by_broker: Dict[str, Dict[str, Any]] = {}
        for name in sorted({b["broker"] for b in books}):
            broker_books = [b for b in books if b["broker"] == name]
            broker_trades = [t for b in broker_books for t in b["trades"]]
            capital = sum(b["allocated_capital"] for b in broker_books)
            metrics = compute_metrics_from_trades(
                broker_trades, allocated_capital=capital or 100_000.0
            )
            quality = self._execution_quality([r for r in rows if r.get("broker") == name])
            execution_by_broker[name] = quality
            by_broker.append(
                {
                    "broker": name,
                    "display_name": _broker_display(name),
                    "mode": _dominant(broker_books, "mode"),
                    "allocated_capital": round(capital, 2),
                    "runner_count": len(broker_books),
                    "net_pnl": metrics["total_pnl"],
                    "return_pct": metrics["total_return_pct"],
                    # ``pnl_pct`` (share of total P&L) is filled in below — it
                    # needs every broker's net P&L, so it cannot be computed
                    # inside this loop.
                    "pnl_pct": None,
                    "sharpe_ratio": metrics["sharpe_ratio"],
                    "sortino_ratio": metrics["sortino_ratio"],
                    "max_drawdown_pct": metrics["max_drawdown_pct"],
                    "total_trades": metrics["total_trades"],
                    "win_rate": metrics["win_rate"],
                    "profit_factor": metrics["profit_factor"],
                    "expectancy": metrics["expectancy"],
                    "segments": sorted({str(b["segment"]) for b in broker_books if b["segment"]}),
                    "strategies": sorted(
                        {str(b["strategy_name"]) for b in broker_books if b["strategy_name"]}
                    ),
                    "execution": self._public_quality(quality),
                    # Flattened for the table/compare paths (the wireframes
                    # read ``avg_slippage_bps`` straight off the broker row).
                    "avg_slippage_bps": quality["avg_slippage_bps"],
                    "fill_rate_pct": quality["fill_rate_pct"],
                    "avg_fill_time_sec": quality["avg_fill_time_sec"],
                    "rejection_rate_pct": quality["rejection_rate_pct"],
                    "total_orders": quality["total_orders"],
                }
            )

        net_total = sum(float(b["net_pnl"] or 0.0) for b in by_broker)
        for row in by_broker:
            row["pnl_pct"] = _round(_safe_div(float(row["net_pnl"] or 0.0) * 100.0, net_total))

        by_segment: List[Dict[str, Any]] = []
        for name in sorted({str(b["segment"]) for b in books if b["segment"]}):
            seg_books = [b for b in books if b["segment"] == name]
            seg_trades = [t for b in seg_books for t in b["trades"]]
            seg_capital = sum(b["allocated_capital"] for b in seg_books) or 100_000.0
            seg_metrics = compute_metrics_from_trades(seg_trades, allocated_capital=seg_capital)
            by_segment.append(
                {
                    "segment": name,
                    "broker": seg_books[0]["broker"],
                    "allocated_capital": round(seg_capital, 2),
                    "net_pnl": seg_metrics["total_pnl"],
                    "return_pct": seg_metrics["total_return_pct"],
                    "sharpe_ratio": seg_metrics["sharpe_ratio"],
                    "max_drawdown_pct": seg_metrics["max_drawdown_pct"],
                    "total_trades": seg_metrics["total_trades"],
                    "runner_count": len(seg_books),
                }
            )

        return {
            "period": period,
            "mode": mode or "all",
            "portfolio_total": {
                **portfolio,
                "gross_pnl": round(gross_pnl, 2),
                "total_capital_deployed": round(total_capital, 2),
                "broker_count": len(by_broker),
                "segment_count": len(by_segment),
                "strategy_count": len({str(b["strategy_name"]) for b in books}),
            },
            "by_broker": by_broker,
            "by_segment": by_segment,
            "broker_rankings": self._rankings(by_broker),
            "alerts": self._broker_alerts(by_broker, execution_by_broker),
            "insights": self._insights(by_broker),
            "data_quality": {
                **provenance,
                "trades_in_period": len(all_trades),
                "runners": len(books),
                "note": (
                    "Trade metrics merge in-memory + persisted history; order metrics "
                    "come from the session-scoped ledger. See /execution for detail."
                ),
            },
        }

    @staticmethod
    def _rankings(by_broker: List[Dict[str, Any]]) -> Dict[str, List[str]]:
        """Broker orderings. ``None`` values sort last — never as a real 0.0."""

        def _order(key: str, reverse: bool, min_trades: int = 1) -> List[str]:
            scored = [
                b["broker"]
                for b in by_broker
                if b.get(key) is not None and (b.get("total_trades") or 0) >= min_trades
            ]
            scored.sort(
                key=lambda name: next(b for b in by_broker if b["broker"] == name)[key],
                reverse=reverse,
            )
            scored.extend(b["broker"] for b in by_broker if b["broker"] not in scored)
            return scored

        def _quality_rank(key: str, reverse: bool) -> List[str]:
            eligible = [
                b
                for b in by_broker
                if b.get(key) is not None and (b.get("total_orders") or 0) >= MIN_SAMPLE
            ]
            eligible.sort(key=lambda b: b[key], reverse=reverse)
            names = [b["broker"] for b in eligible]
            names.extend(b["broker"] for b in by_broker if b["broker"] not in names)
            return names

        return {
            "by_sharpe": _order("sharpe_ratio", True),
            "by_net_pnl": _order("net_pnl", True),
            "by_execution_quality": _quality_rank("avg_slippage_bps", False),
            "by_fill_rate": _quality_rank("fill_rate_pct", True),
            "by_fill_time": _quality_rank("avg_fill_time_sec", False),
            "by_rejection_rate": _quality_rank("rejection_rate_pct", False),
        }

    @staticmethod
    def _broker_alerts(
        by_broker: List[Dict[str, Any]], execution: Dict[str, Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        alerts: List[Dict[str, Any]] = []
        for row in by_broker:
            quality = execution.get(row["broker"], {})
            total = quality.get("total_orders") or 0
            if total and (quality.get("fill_rate_pct") or 100.0) < 85.0:
                alerts.append(
                    {
                        "broker": row["broker"],
                        "severity": "warning",
                        "alert_type": "low_fill_rate",
                        "message": (
                            f"{_broker_display(row['broker'])}: fill rate "
                            f"{quality['fill_rate_pct']:.1f}% over {total} orders."
                        ),
                    }
                )
            if total and (quality.get("rejection_rate_pct") or 0.0) >= 5.0:
                alerts.append(
                    {
                        "broker": row["broker"],
                        "severity": "warning" if total >= MIN_SAMPLE else "info",
                        "alert_type": "high_rejection_rate",
                        "message": (
                            f"{_broker_display(row['broker'])}: "
                            f"{quality['rejection_rate_pct']:.1f}% of resolved orders rejected "
                            f"({quality.get('rejected_orders')} of {total})."
                        ),
                    }
                )
            if (quality.get("stale_orders") or 0) > 0:
                alerts.append(
                    {
                        "broker": row["broker"],
                        "severity": "warning",
                        "alert_type": "stale_orders",
                        "message": (
                            f"{_broker_display(row['broker'])}: {quality['stale_orders']} order(s) "
                            f"still working after {ORDER_AGING_S:.0f}s."
                        ),
                    }
                )
        return alerts

    @staticmethod
    def _insights(by_broker: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Actionable, evidence-carrying observations (wireframe §5.2)."""
        insights: List[Dict[str, Any]] = []
        rated = [b for b in by_broker if (b.get("total_orders") or 0) >= MIN_SAMPLE]
        if len(rated) < 2:
            return insights
        rated.sort(key=lambda b: b["avg_slippage_bps"])
        best, worst = rated[0], rated[-1]
        if worst["avg_slippage_bps"] > 0:
            ratio = (
                worst["avg_slippage_bps"] / best["avg_slippage_bps"]
                if best["avg_slippage_bps"]
                else None
            )
            if ratio and ratio >= 1.5:
                insights.append(
                    {
                        "kind": "slippage_gap",
                        "brokers": [worst["broker"], best["broker"]],
                        "message": (
                            f"{_broker_display(worst['broker'])} slippage is "
                            f"{ratio:.1f}× {_broker_display(best['broker'])}'s "
                            f"({worst['avg_slippage_bps']:.1f} vs "
                            f"{best['avg_slippage_bps']:.1f} bps) "
                            "— worth testing a move."
                        ),
                    }
                )
        fill_rated = [b for b in by_broker if b.get("fill_rate_pct") is not None]
        if len(fill_rated) >= 2:
            low = min(fill_rated, key=lambda b: b["fill_rate_pct"])
            high = max(fill_rated, key=lambda b: b["fill_rate_pct"])
            gap = high["fill_rate_pct"] - low["fill_rate_pct"]
            if gap >= 5.0:
                insights.append(
                    {
                        "kind": "fill_rate_gap",
                        "brokers": [low["broker"], high["broker"]],
                        "message": (
                            f"{_broker_display(high['broker'])} fills {gap:.1f}pp more orders than "
                            f"{_broker_display(low['broker'])} "
                            f"({high['fill_rate_pct']:.1f}% vs {low['fill_rate_pct']:.1f}%)."
                        ),
                    }
                )
        return insights

    # ------------------------------------------------------------------
    # Endpoint 3 — broker comparison
    # ------------------------------------------------------------------

    def compare_brokers(
        self,
        brokers: Sequence[str],
        metrics: Optional[Sequence[str]] = None,
        period: str = "30d",
        statistical_test: bool = True,
        mode: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Side-by-side metric rows with significance tests (PRD-003 §4.3)."""
        wanted = [str(b).strip().lower() for b in (brokers or []) if str(b).strip()]
        if len(wanted) < 2:
            raise ValueError("compare needs at least two brokers")
        keys = validate_metric_names(list(metrics or COMPARABLE_METRICS.keys()))

        books = self.analytics.collect_books(period=period, mode=mode)
        rows, provenance = self._orders(period=period, mode=mode)
        by_broker = {b["broker"]: b for b in books}

        profiles: Dict[str, Dict[str, Any]] = {}
        for name in wanted:
            if name not in by_broker:
                continue
            broker_books = [b for b in books if b["broker"] == name]
            trades = [t for b in broker_books for t in b["trades"]]
            capital = sum(b["allocated_capital"] for b in broker_books) or 100_000.0
            metrics_block = compute_metrics_from_trades(trades, allocated_capital=capital)
            quality = self._execution_quality([r for r in rows if r.get("broker") == name])
            profiles[name] = {
                "broker": name,
                "display_name": _broker_display(name),
                "sharpe": metrics_block["sharpe_ratio"],
                "net_pnl": metrics_block["total_pnl"],
                "win_rate": metrics_block["win_rate"],
                "max_drawdown_pct": metrics_block["max_drawdown_pct"],
                "total_trades": metrics_block["total_trades"],
                "avg_slippage_bps": quality["avg_slippage_bps"],
                "fill_rate_pct": quality["fill_rate_pct"],
                "fill_time_sec": quality["avg_fill_time_sec"],
                "rejection_rate_pct": quality["rejection_rate_pct"],
                # samples + counts used by the tests
                "_returns": [float(t.get("pnl") or 0.0) / capital for t in trades],
                "_slippage": quality["slippage_bps_samples"],
                "_fill_times": quality["fill_time_samples_values"],
                "_filled": quality["filled_orders"],
                "_resolved": quality["resolved_orders"],
                "_rejected": quality["rejected_orders"],
                "_wins": sum(1 for t in trades if float(t.get("pnl") or 0) > 0),
                "_trades": metrics_block["total_trades"],
            }

        missing = [b for b in wanted if b not in profiles]
        compared = [b for b in wanted if b in profiles]
        if len(compared) < 2:
            raise ValueError(
                f"no data for the requested brokers ({', '.join(missing) or 'none'}); "
                "a broker must have at least one runner to be compared"
            )

        first, second = compared[0], compared[1]
        comparison: List[Dict[str, Any]] = []
        for key in keys:
            comparison.append(
                self._compare_metric(key, profiles[first], profiles[second], statistical_test)
            )

        return {
            "period": period,
            "brokers": compared,
            "unknown_brokers": missing,
            "metrics": comparison,
            "profiles": {
                name: {k: v for k, v in prof.items() if not k.startswith("_")}
                for name, prof in profiles.items()
            },
            "recommendation": self._compare_recommendation(comparison, first, second),
            "data_quality": provenance,
        }

    def _compare_metric(
        self,
        key: str,
        first: Dict[str, Any],
        second: Dict[str, Any],
        statistical_test: bool,
    ) -> Dict[str, Any]:
        label, higher_is_better, unit = COMPARABLE_METRICS[key]
        a, b = first.get(key), second.get(key)
        difference = _safe_diff(a, b)
        reference = _safe_div(abs(difference) * 100.0, abs(b)) if b not in (None, 0) else None
        better = None
        if a is not None and b is not None and difference not in (0, None):
            better = first["broker"] if ((difference > 0) == higher_is_better) else second["broker"]

        # Counts stay integers in the payload — a "25.0 trades" column is a
        # formatting bug waiting to be copied into a report.
        cast = int if key == "total_trades" else _round
        row = {
            "metric": key,
            "label": label,
            "unit": unit,
            "higher_is_better": higher_is_better,
            first["broker"]: cast(a),
            second["broker"]: cast(b),
            "difference": cast(difference),
            "difference_pct": _round(reference),
            "better": better,
            "statistical_significance": _no_test("statistical_test=false — comparison not tested"),
        }
        if statistical_test:
            row["statistical_significance"] = self._test_metric(key, first, second, better)
        return row

    @staticmethod
    def _test_metric(
        key: str, first: Dict[str, Any], second: Dict[str, Any], better: Optional[str]
    ) -> Dict[str, Any]:
        """Pick the right test for the metric's data TYPE, not its label."""
        if key in ("net_pnl", "total_trades"):
            return _no_test(
                f"{COMPARABLE_METRICS[key][0]} is a rupee/count total, not normalised for "
                "capital or activity — compared descriptively only"
            )
        if key == "max_drawdown_pct":
            return _no_test(
                "Max drawdown is one aggregate per broker — there is no sample to run a "
                "significance test on"
            )
        if key in ("fill_rate_pct", "rejection_rate_pct", "win_rate"):
            # A RATE is tested on its 2x2 contingency table, not on two
            # separately-estimated percentages.
            event_key = {"fill_rate_pct": "_filled", "rejection_rate_pct": "_rejected"}.get(
                key, "_wins"
            )
            total_key = "_trades" if key == "win_rate" else "_resolved"
            block = rate_significance_block(
                first[event_key],
                first[total_key],
                second[event_key],
                second[total_key],
            )
            if block.get("testable") and better:
                block["direction"] = f"{_broker_display(better)} is better"
            return block
        samples = {
            "sharpe": "_returns",
            "avg_slippage_bps": "_slippage",
            "fill_time_sec": "_fill_times",
        }.get(key)
        if samples is None:
            return _no_test(f"no test defined for {key}")
        return _directional_block(
            mann_whitney_u(first[samples], second[samples], alternative="two-sided"),
            better,
        )

    @staticmethod
    def _compare_recommendation(
        comparison: List[Dict[str, Any]], first: str, second: str
    ) -> Dict[str, Any]:
        """Prefer the broker that wins the SIGNIFICANT metrics, not the most."""
        wins: Dict[str, int] = {first: 0, second: 0}
        tested = 0
        for row in comparison:
            sig = row.get("statistical_significance") or {}
            if sig.get("testable") and sig.get("significant") and row.get("better"):
                wins[row["better"]] += 1
                tested += 1
        winner = max(wins, key=lambda k: wins[k])
        decided = wins[first] != wins[second]
        reasoning = []
        for row in comparison:
            if not row.get("better"):
                continue
            sig = row.get("statistical_significance") or {}
            if sig.get("testable") and sig.get("significant"):
                reasoning.append(
                    f"{row['label']} favours {_broker_display(row['better'])} "
                    f"({_format_p(sig.get('p_value'))})"
                )
        if not reasoning:
            reasoning.append(
                "No metric difference reached statistical significance — treat the "
                "brokers as equivalent on the evidence available."
            )
        if not decided:
            return {
                "preferred_broker": None,
                "confidence": "none",
                "reasoning": reasoning,
                "summary": (
                    f"Neither {_broker_display(first)} nor {_broker_display(second)} is "
                    "measurably better on the tested metrics."
                ),
            }
        confidence = (
            "high"
            if tested >= 3 and wins[winner] >= tested - 1
            else ("medium" if tested >= 2 else "low")
        )
        return {
            "preferred_broker": winner,
            "confidence": confidence,
            "reasoning": reasoning,
            "summary": (
                f"{_broker_display(winner)} wins {wins[winner]} of {tested} significant "
                f"metric comparison(s)."
            ),
        }

    # ------------------------------------------------------------------
    # Endpoint 4 — strategy migration what-if
    # ------------------------------------------------------------------

    def migration_impact(
        self,
        strategy: str,
        from_broker: str,
        to_broker: str,
        period: str = "30d",
        mode: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Estimate what moving one strategy between brokers would do (Story 3)."""
        strategy_key = str(strategy or "").strip()
        src = str(from_broker or "").strip().lower()
        dst = str(to_broker or "").strip().lower()
        if not strategy_key:
            raise ValueError("strategy is required")
        if not src or not dst:
            raise ValueError("from_broker and to_broker are required")
        if src == dst:
            raise ValueError("from_broker and to_broker must differ")

        books = self.analytics.collect_books(period=period, mode=mode)
        rows, provenance = self._orders(period=period, mode=mode)

        source_books = [
            b
            for b in books
            if b["broker"] == src and str(b.get("strategy_name") or "").lower() == strategy_key
        ]
        if not source_books:
            raise LookupError(
                f"strategy {strategy_key!r} has no runner on broker {src!r} in the last {period}"
            )

        source_trades = [t for b in source_books for t in b["trades"]]
        source_capital = sum(b["allocated_capital"] for b in source_books) or 100_000.0
        source_metrics = compute_metrics_from_trades(
            source_trades, allocated_capital=source_capital
        )
        source_quality = self._execution_quality(
            [
                r
                for r in rows
                if r.get("broker") == src
                and str(r.get("strategy_name") or "").lower() == strategy_key
            ]
        )
        target_quality = self._execution_quality([r for r in rows if r.get("broker") == dst])

        current = {
            "broker": src,
            "broker_label": _broker_display(src),
            "sharpe": source_metrics["sharpe_ratio"],
            "net_pnl": source_metrics["total_pnl"],
            "total_trades": source_metrics["total_trades"],
            "win_rate": source_metrics["win_rate"],
            "max_drawdown_pct": source_metrics["max_drawdown_pct"],
            "avg_slippage_bps": source_quality["avg_slippage_bps"],
            "fill_rate_pct": source_quality["fill_rate_pct"],
            "avg_fill_time_sec": source_quality["avg_fill_time_sec"],
            "orders": source_quality["total_orders"],
        }

        history = self._migration_history(books, strategy_key, dst, period, mode=mode)
        estimated, impact, reasons = _estimate_migration(current, target_quality, src, dst)

        return {
            "strategy": strategy_key,
            "from_broker": src,
            "to_broker": dst,
            "period": period,
            "current_performance": current,
            "estimated_performance": {
                **estimated,
                "broker": dst,
                "broker_label": _broker_display(dst),
                "methodology": (
                    "Trade count scales by the fill-rate ratio; P&L then pays the extra "
                    "slippage on every retained round trip. Sharpe scales with P&L at "
                    "constant volatility."
                ),
            },
            "impact": {"reasons": reasons, **impact},
            "historical_data": history,
            "recommendation": _migration_recommendation(impact, current, estimated, reasons),
            "data_quality": provenance,
        }

    @staticmethod
    def _migration_history(
        books: List[Dict[str, Any]],
        strategy: str,
        broker: str,
        period: str,
        mode: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Has this strategy actually run on the target broker? (PRD Story 3)"""
        matches = [
            b
            for b in books
            if b["broker"] == broker and str(b.get("strategy_name") or "").lower() == strategy
        ]
        if not matches:
            return {
                "available": False,
                "note": (
                    f"{strategy} never ran on {_broker_display(broker)}; estimates are based "
                    "on that broker's averages across all of its other strategies."
                ),
            }
        trades = [t for b in matches for t in b["trades"]]
        capital = sum(b["allocated_capital"] for b in matches) or 100_000.0
        metrics = compute_metrics_from_trades(trades, allocated_capital=capital)
        return {
            "available": True,
            "note": (
                f"{strategy} ran on {_broker_display(broker)} in this period — these are "
                "OBSERVED numbers, not an estimate."
            ),
            "sharpe": metrics["sharpe_ratio"],
            "net_pnl": metrics["total_pnl"],
            "total_trades": metrics["total_trades"],
            "win_rate": metrics["win_rate"],
            "max_drawdown_pct": metrics["max_drawdown_pct"],
            "runner_count": len(matches),
        }

    # ------------------------------------------------------------------
    # Endpoint 5 — broker recommendation for a NEW strategy
    # ------------------------------------------------------------------

    def recommend_broker(
        self,
        strategy_type: str = "scalper",
        segment: Optional[str] = None,
        avg_trade_size: Optional[float] = None,
        trade_frequency: str = "high",
        period: str = "30d",
        mode: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Score brokers for a strategy that has not been deployed yet (Story 5)."""
        books = self.analytics.collect_books(period=period, mode=mode)
        rows, provenance = self._orders(period=period, mode=mode)

        candidates = books
        segment_note = None
        if segment:
            scoped = [b for b in books if str(b.get("segment") or "").lower() == segment]
            if scoped:
                candidates = scoped
            else:
                segment_note = (
                    f"No runner is currently deployed in segment {segment!r} — ranked every "
                    "broker with data instead of returning an empty answer."
                )

        quality: Dict[str, Dict[str, Any]] = {}
        for name in sorted({b["broker"] for b in candidates}):
            quality[name] = self._execution_quality([r for r in rows if r.get("broker") == name])

        ranked = [name for name, q in quality.items() if (q.get("total_orders") or 0) >= MIN_SAMPLE]
        if not ranked:
            return {
                "recommended_broker": None,
                "reasoning": [
                    f"No broker has ≥{MIN_SAMPLE} orders in the last {period} — there is "
                    "not enough execution history to recommend one."
                ],
                "rankings": [],
                "segment_note": segment_note,
                "data_quality": provenance,
            }

        weights = _scoring_weights(strategy_type, trade_frequency)
        scores: List[Dict[str, Any]] = []
        for name in ranked:
            sub = {
                k: _minmax(
                    quality,
                    name,
                    _QUALITY_KEY_FOR_METRIC[k],
                    higher_is_better=COMPARABLE_METRICS[k][1],
                )
                for k in weights
            }
            # Normalise by the weight that could actually be measured: a
            # broker with no fill-time history must not be scored as if it
            # scored zero on fill time.
            measured = {k: v for k, v in sub.items() if v is not None}
            measured_weight = sum(weights[k] for k in measured) or 1.0
            score_100 = sum(weights[k] * v for k, v in measured.items()) / measured_weight
            strengths = sorted(k for k, v in measured.items() if v >= 70.0)
            weaknesses = sorted(k for k, v in measured.items() if v < 40.0)
            scores.append(
                {
                    "broker": name,
                    "display_name": _broker_display(name),
                    "score": round(score_100 / 10.0, 2),
                    "score_basis_pct": _round(measured_weight * 100.0, 1),
                    "weights": weights,
                    "sub_scores": {k: _round(v, 1) for k, v in sub.items()},
                    "strengths": [COMPARABLE_METRICS.get(k, (k,))[0] for k in strengths],
                    "weaknesses": [COMPARABLE_METRICS.get(k, (k,))[0] for k in weaknesses],
                    "metrics": {
                        k: quality[name].get(k)
                        for k in (
                            "fill_rate_pct",
                            "avg_fill_time_sec",
                            "avg_slippage_bps",
                            "rejection_rate_pct",
                            "total_orders",
                            "avg_order_notional",
                        )
                    },
                }
            )
        scores.sort(key=lambda s: s["score"], reverse=True)

        winner = scores[0]
        runner = next(s for s in scores[1:]) if len(scores) > 1 else None
        reasoning = [
            f"{s['display_name']} ranks #1 for a {strategy_type} at {trade_frequency} frequency"
            for s in scores[:1]
        ]
        for s in scores[:3]:
            m = s["metrics"]
            if m.get("fill_rate_pct") is not None:
                reasoning.append(
                    f"{s['display_name']} fill rate {m['fill_rate_pct']:.1f}%, "
                    f"avg {m['avg_fill_time_sec']:.1f}s, slippage {m['avg_slippage_bps']:.1f} bps"
                )
        savings = self._estimated_savings(scores, period, avg_trade_size)
        if runner and savings.get("amount"):
            reasoning.append(
                f"Estimated slippage saving vs #{2} ({runner['display_name']}): "
                f"₹{savings['amount']:,.0f}/month"
            )

        return {
            "recommended_broker": winner["broker"],
            "recommended_broker_label": winner["display_name"],
            "confidence": _recommendation_confidence(scores),
            "reasoning": reasoning,
            "rankings": scores,
            "weights": weights,
            "segment_note": segment_note,
            "estimated_monthly_savings": savings,
            "data_quality": provenance,
        }

    @staticmethod
    def _estimated_savings(
        scores: List[Dict[str, Any]], period: str, avg_trade_size: Optional[float]
    ) -> Dict[str, Any]:
        """Slippage saving from running at the best broker instead of the average.

        Scaled by the OBSERVED order rate (orders in the period, normalised to
        30 days) — not by a guessed "150 trades/month". The trade size is the
        caller's ``avg_trade_size`` when given, else the observed average
        notional from the ledger.
        """
        if len(scores) < 2:
            return {
                "amount": None,
                "calculation": "needs at least two brokers with data to compare savings",
            }
        best = scores[0]
        others = scores[1:]
        best_slip = best["metrics"].get("avg_slippage_bps")
        other_slips = [
            s["metrics"]["avg_slippage_bps"]
            for s in others
            if s["metrics"].get("avg_slippage_bps") is not None
        ]
        if best_slip is None or not other_slips:
            return {"amount": None, "calculation": "slippage history missing on one or both sides"}
        avg_other = sum(other_slips) / len(other_slips)
        delta_bps = avg_other - best_slip
        if delta_bps <= 0:
            return {
                "amount": 0.0,
                "calculation": (
                    f"{best['display_name']} is already the cheapest venue on slippage — "
                    "no saving from moving."
                ),
            }
        notional = avg_trade_size or best["metrics"].get("avg_order_notional")
        total_orders = sum(s["metrics"].get("total_orders") or 0 for s in scores)
        days = PERIOD_DAYS.get(period) or 30
        monthly_orders = (total_orders / max(1, days)) * 30.0
        if not notional or not monthly_orders:
            return {"amount": None, "calculation": "no order notional / rate data to scale by"}
        # A round trip is two orders, each paying the extra slippage.
        amount = monthly_orders * 2.0 * (delta_bps / 10_000.0) * float(notional)
        return {
            "amount": round(amount, 2),
            "calculation": (
                f"{monthly_orders:.0f} orders/month × 2 sides × {delta_bps:.1f} bps × "
                f"₹{float(notional):,.0f} notional"
            ),
            "monthly_orders": round(monthly_orders, 1),
            "delta_bps": round(delta_bps, 2),
            "avg_order_notional": round(float(notional), 2),
        }


# ----------------------------------------------------------------------
# Module-level helpers
# ----------------------------------------------------------------------


def _order_slippage_bps(order: Dict[str, Any]) -> Optional[float]:
    """Adverse-positive slippage of one order, in bps.

    Prefers the ledger's own ``slippage_pct`` (already side-aware: a BUY above
    its request and a SELL below it are both money lost). Falls back to
    recomputing from ``requested_price`` / ``avg_fill_price`` with the same
    sign convention. ``None`` when there is no request price to compare
    against — a fill with no reference is NOT a zero-slippage fill.
    """
    pct = order.get("slippage_pct")
    if pct is not None:
        try:
            return round(float(pct) * 10_000.0, 4)
        except (TypeError, ValueError):
            pass
    requested = _as_float(order.get("requested_price"))
    filled = _as_float(order.get("avg_fill_price"))
    if not requested or filled is None:
        return None
    side = str(order.get("side") or "").upper()
    if side == "SELL":
        return round((requested - filled) / requested * 10_000.0, 4)
    return round((filled - requested) / requested * 10_000.0, 4)


def _order_fill_time_sec(order: Dict[str, Any]) -> Optional[float]:
    """Seconds from submit to fill; ``None`` when either stamp is missing."""
    created = _parse_ts(order.get("created_ts"))
    filled = _parse_ts(order.get("filled_ts"))
    if created is None or filled is None:
        return None
    return round(max(0.0, (filled - created).total_seconds()), 3)


def _order_notional(order: Dict[str, Any]) -> Optional[float]:
    """Requested notional (price × qty) — the base slippage is charged on."""
    price = _as_float(order.get("requested_price")) or _as_float(order.get("avg_fill_price"))
    qty = _as_float(order.get("quantity")) or _as_float(order.get("filled_qty"))
    if not price or not qty:
        return None
    return round(price * qty, 2)


def _age_seconds(created_ts: Any) -> float:
    created = _parse_ts(created_ts)
    if created is None:
        return 0.0
    return max(0.0, (datetime.now(timezone.utc) - created).total_seconds())


def _as_float(value: Any) -> Optional[float]:
    try:
        fv = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(fv) or math.isinf(fv):
        return None
    return fv


def _safe_diff(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b is None:
        return None
    return float(a) - float(b)


def _minmax(
    quality: Dict[str, Dict[str, Any]], name: str, key: str, higher_is_better: bool
) -> Optional[float]:
    """Scale one metric to 0–100 across brokers; ``None`` if this broker lacks it."""
    values = {other: q.get(key) for other, q in quality.items() if q.get(key) is not None}
    if name not in values or len(values) < 2:
        # One broker, or everyone equal: 50 is "no information", not 0/100.
        return 50.0 if name in values else None
    lo, hi = min(values.values()), max(values.values())
    if hi == lo:
        return 50.0
    scaled = (values[name] - lo) / (hi - lo) * 100.0
    return scaled if higher_is_better else 100.0 - scaled


def _scoring_weights(strategy_type: str, trade_frequency: str) -> Dict[str, float]:
    """Frequency weights, nudged by strategy style, renormalised to 1.0."""
    freq = str(trade_frequency or "medium").strip().lower()
    weights = dict(_FREQUENCY_WEIGHTS.get(freq, _FREQUENCY_WEIGHTS["medium"]))
    for key, boost in _STYLE_BOOSTS.get(str(strategy_type or "").strip().lower(), {}).items():
        if key in weights:
            weights[key] = weights[key] + boost
    total = sum(weights.values()) or 1.0
    return {k: round(v / total, 4) for k, v in weights.items() if v > 0}


def _no_test(reason: str) -> Dict[str, Any]:
    """A significance block for a comparison that deliberately is not tested."""
    return {
        "test": None,
        "p_value": None,
        "significant": False,
        "confidence": None,
        "testable": False,
        "reason": reason,
    }


def _directional_block(block: Dict[str, Any], better: Optional[str]) -> Dict[str, Any]:
    """Attach which side won to a rank-test result."""
    block["alpha"] = ALPHA
    if block.get("testable") and block.get("p_value") is not None:
        block["confidence"] = f"{(1.0 - block['p_value']) * 100.0:.1f}%"
        if better:
            block["direction"] = f"{_broker_display(better)} is better"
    return block


def _broker_display(name: str) -> str:
    """mstock → mStock, dhan → Dhan, 'paper' → Paper (simulated)."""
    key = str(name or "").strip().lower()
    if not key:
        return "Unassigned"
    if key in _BROKER_LABELS:
        return _BROKER_LABELS[key]
    return key[0].upper() + key[1:]


def _dominant(books: List[Dict[str, Any]], key: str) -> Optional[str]:
    """The most common value of ``key`` across a broker's runners."""
    values = [str(b.get(key)) for b in books if b.get(key)]
    if not values:
        return None
    return max(set(values), key=values.count)


def _estimate_migration(
    current: Dict[str, Any],
    target_quality: Dict[str, Any],
    src: str,
    dst: str,
) -> Tuple[Dict[str, Any], Dict[str, Any], List[str]]:
    """The what-if maths, in one auditable place.

    Three effects, each isolated so the UI can show its working:

    1. **Trade count** — scales by ``target_fill_rate / source_fill_rate``.
       A venue that fills 7pp fewer orders executes ~13% fewer round trips.
    2. **Slippage** — the extra bps are charged on the round trip (two sides)
       against the observed average order notional.
    3. **Sharpe** — scales with the P&L change at constant volatility. This is
       a linear approximation of the mean, not a re-estimation of the
       distribution; it is the honest ceiling of what ledger data supports.

    Every input that is missing short-circuits to ``None`` with a reason —
    an estimate built on a fill rate the destination broker has never
    recorded is a guess wearing a decimal point.
    """
    reasons: List[str] = []
    src_fill = current.get("fill_rate_pct")
    src_slip = current.get("avg_slippage_bps")
    dst_fill = target_quality.get("fill_rate_pct")
    dst_slip = target_quality.get("avg_slippage_bps")
    notional = target_quality.get("avg_order_notional")
    trades = int(current.get("total_trades") or 0)
    net_pnl = float(current.get("net_pnl") or 0.0)
    sharpe = current.get("sharpe")

    if dst_fill is None or src_fill in (None, 0):
        return (
            {
                "broker": dst,
                "estimated_net_pnl": None,
                "estimated_trades": trades,
                "avg_slippage_bps": dst_slip,
                "fill_rate_pct": dst_fill,
            },
            {
                "sharpe_change": None,
                "sharpe_change_pct": None,
                "pnl_change": None,
                "pnl_change_pct": None,
                "trade_count_change": 0,
            },
            [
                f"{_broker_display(dst)} has no recorded fill rate in this period — "
                "no estimate is possible. Trade the strategy briefly on "
                f"{_broker_display(dst)} first, or compare the brokers directly."
            ],
        )

    retention = float(dst_fill) / float(src_fill) if src_fill else 1.0
    est_trades = int(round(trades * retention))
    trade_delta = est_trades - trades

    slip_cost_per_trade = 0.0
    if dst_slip is not None and src_slip is not None and notional:
        delta_bps = float(dst_slip) - float(src_slip)
        slip_cost_per_trade = 2.0 * (delta_bps / 10_000.0) * float(notional)
        if abs(delta_bps) >= 0.1:
            reasons.append(
                f"{_broker_display(dst)} slippage is {abs(delta_bps):.1f} bps "
                f"{'higher' if delta_bps > 0 else 'lower'} → "
                f"{'costs' if delta_bps > 0 else 'saves'} "
                f"₹{abs(slip_cost_per_trade):,.0f} per round trip "
                f"(2 sides × ₹{float(notional):,.0f} notional)"
            )
    else:
        reasons.append(
            "Slippage notional unavailable — the slippage leg of the estimate is omitted "
            "rather than guessed."
        )

    if abs(retention - 1.0) >= 0.01:
        gap = abs(float(dst_fill) - float(src_fill))
        reasons.append(
            f"{_broker_display(dst)} fills {gap:.1f}pp "
            f"{'fewer' if trade_delta < 0 else 'more'} orders than "
            f"{_broker_display(src)} ({float(src_fill):.1f}% → {float(dst_fill):.1f}%) → "
            f"{abs(trade_delta)} {'fewer' if trade_delta < 0 else 'more'} round trips"
        )

    est_pnl = net_pnl * retention - slip_cost_per_trade * est_trades
    pnl_change = est_pnl - net_pnl
    pnl_change_pct = _safe_div(pnl_change * 100.0, abs(net_pnl)) if net_pnl else None

    est_sharpe = None
    if sharpe is not None and net_pnl:
        est_sharpe = round(float(sharpe) * (est_pnl / net_pnl), 2)
        reasons.append(
            "Sharpe scaled by the P&L change at constant volatility "
            "(a linear approximation, not a re-estimation)"
        )
    elif sharpe is None:
        reasons.append(
            f"{_broker_display(src)} has no tradable Sharpe history for this strategy — "
            "Sharpe impact cannot be estimated."
        )

    return (
        {
            "broker": dst,
            "estimated_net_pnl": round(est_pnl, 2),
            "estimated_trades": est_trades,
            "estimated_sharpe": est_sharpe,
            "avg_slippage_bps": dst_slip,
            "fill_rate_pct": dst_fill,
            "avg_fill_time_sec": target_quality.get("avg_fill_time_sec"),
        },
        {
            "estimated_sharpe": est_sharpe,
            "sharpe_change": (
                round(est_sharpe - float(sharpe), 2)
                if est_sharpe is not None and sharpe is not None
                else None
            ),
            "sharpe_change_pct": (
                _round(_safe_div((est_sharpe - float(sharpe)) * 100.0, abs(float(sharpe))))
                if est_sharpe is not None and sharpe
                else None
            ),
            "pnl_change": round(pnl_change, 2),
            "pnl_change_pct": _round(pnl_change_pct),
            "trade_count_change": trade_delta,
            "trade_retention": round(retention, 4),
        },
        reasons,
    )


def _migration_recommendation(
    impact: Dict[str, Any],
    current: Dict[str, Any],
    estimated: Dict[str, Any],
    reasons: List[str],
) -> Dict[str, Any]:
    """KEEP / MOVE / HOLD, with the evidence that produced it."""
    pnl_change_pct = impact.get("pnl_change_pct")
    if pnl_change_pct is None:
        return {
            "action": "INSUFFICIENT DATA",
            "confidence": "none",
            "reasoning": reasons or ["not enough execution data to compare the two brokers"],
        }
    if pnl_change_pct <= -MIGRATION_NOISE_PCT:
        action, headline = "KEEP", (
            f"staying costs ~{abs(pnl_change_pct):.1f}% of this strategy's P&L"
        )
    elif pnl_change_pct >= MIGRATION_NOISE_PCT:
        action, headline = "MOVE", (
            f"moving is worth ~{pnl_change_pct:.1f}% of this strategy's P&L"
        )
    else:
        action, headline = "HOLD", (
            f"the estimated difference ({pnl_change_pct:+.1f}%) is inside the "
            f"±{MIGRATION_NOISE_PCT:.0f}% noise band"
        )

    orders = int(current.get("orders") or 0)
    if orders < MIN_SAMPLE:
        confidence = "low"
        reasons.append(
            f"Only {orders} orders on {_broker_display(current['broker'])} "
            f"(need ≥{MIN_SAMPLE}) — treat this as indicative only."
        )
    else:
        confidence = "medium"
    return {
        "action": action,
        "confidence": confidence,
        "reasoning": [headline] + reasons,
        "note": (
            "This is an estimate from execution telemetry, not a backtest. Re-check with "
            "a short pilot run on the target broker before moving live capital."
        ),
    }


def _execution_notes(quality: Dict[str, Any]) -> List[str]:
    """Plain-language caveats a trader must see alongside the numbers."""
    notes: List[str] = []
    total = quality.get("total_orders") or 0
    if not total:
        notes.append("No orders recorded — every execution metric is undefined, not zero.")
        return notes
    filled = quality.get("filled_orders") or 0
    unsampled_fills = filled - (quality.get("slippage_samples") or 0)
    if unsampled_fills > 0:
        notes.append(
            f"{unsampled_fills} of {filled} filled order(s) have no request price to "
            "compare against, so they are excluded from slippage (never counted as "
            "zero-slippage)."
        )
    if (quality.get("pending_orders") or 0) > 0:
        notes.append(
            f"{quality['pending_orders']} order(s) are still working; fill and rejection "
            "rates are computed over resolved orders only so they cannot swing while "
            "orders are in flight."
        )
    if total < MIN_SAMPLE:
        notes.append(
            f"Fewer than {MIN_SAMPLE} orders in this period — do not compare brokers on "
            "this sample."
        )
    return notes


def _format_p(value: Optional[float]) -> str:
    """``0.0000123`` → ``p<0.0001``; a p-value is never printed as a flat 0.0."""
    if value is None:
        return "p n/a"
    if value < 0.0001:
        return "p<0.0001"
    return f"p={value:.4f}"


def _recommendation_confidence(scores: List[Dict[str, Any]]) -> str:
    """How much to trust a #1 ranking, from the margin over #2 and the evidence.

    A broker that edges out the field by a hair on a thin order book is not a
    "high confidence" recommendation, and saying so is the whole point.
    """
    if len(scores) < 2:
        return "low"
    top, second = scores[0], scores[1]
    margin = float(top["score"]) - float(second["score"])
    orders = min((s["metrics"].get("total_orders") or 0) for s in scores[:2])
    if margin >= 20.0 and orders >= 5 * MIN_SAMPLE:
        return "high"
    if margin >= 8.0 and orders >= 2 * MIN_SAMPLE:
        return "medium"
    return "low"
