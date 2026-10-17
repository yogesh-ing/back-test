"""Internal deterministic replay harness built on the forward StrategyRunner.

This module is intentionally not a performance backtester. It wires the
existing option strategy → selector → structure → OptionPaperBroker path to
point-in-time CSV quotes, an exchange-time clock and deterministic fills. Runs
are ephemeral, labelled REPLAY and exportable to JSON; no database is opened.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping

import pandas as pd

from backtest.forward.options_bridge import OptionsBridge
from backtest.forward.paper_runner import OrderLedger, RunnerConfig, StrategyRunner
from backtest.options.paper_trading import OptionPaperBroker
from backtest.replay.clock import SimulatedClock
from backtest.replay.data import ReplayDataset
from backtest.replay.exits import OHLCConservativeExitResolver
from backtest.replay.feed import PointInTimeReplayFeed, ReplayFaultPlan, ReplayQuoteProvider
from backtest.strategy.registry import get_strategy

REPLAY_ENGINE_VERSION = "options-replay-v1"


class ReplayInvariantError(AssertionError):
    """A deterministic replay timing/accounting invariant failed."""


@dataclass
class ReplayConfig:
    """Governed configuration for an internal mechanics replay."""

    strategy_name: str = "directional_options"
    strategy_params: dict[str, Any] = field(default_factory=dict)
    expression: dict[str, Any] = field(default_factory=dict)
    initial_capital: float = 1_000_000.0
    commission_per_lot: float = 20.0
    max_drawdown_pct: float = 0.25
    daily_loss_limit_pct: float = 0.15
    underlying_symbol: str | None = None
    fill_policy: str = "ohlc_conservative"

    def __post_init__(self) -> None:
        self.strategy_name = str(self.strategy_name).strip()
        if not self.strategy_name:
            raise ValueError("strategy_name must not be empty")
        self.strategy_params = copy.deepcopy(self.strategy_params or {})
        # Phase 4: no default expression exists to inherit. A replay with
        # no explicit v2 expression resolves the strategy's declared spec
        # defaults at run time (and refuses if the strategy declares
        # none); explicit overrides must themselves be a v2 expression.
        self.expression = copy.deepcopy(self.expression or {})
        if self.initial_capital <= 0:
            raise ValueError("initial_capital must be positive")
        if self.commission_per_lot < 0:
            raise ValueError("commission_per_lot cannot be negative")
        if self.fill_policy != "ohlc_conservative":
            raise ValueError("V1 supports only fill_policy='ohlc_conservative'")
        if self.underlying_symbol is not None:
            self.underlying_symbol = str(self.underlying_symbol).strip().upper()

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_name": self.strategy_name,
            "strategy_params": _json_safe(self.strategy_params),
            "expression": _json_safe(self.expression),
            "initial_capital": float(self.initial_capital),
            "commission_per_lot": float(self.commission_per_lot),
            "max_drawdown_pct": float(self.max_drawdown_pct),
            "daily_loss_limit_pct": float(self.daily_loss_limit_pct),
            "underlying_symbol": self.underlying_symbol,
            "fill_policy": self.fill_policy,
        }


@dataclass
class ReplayResult:
    """Citable, JSON-safe mechanism evidence from one deterministic run."""

    run_id: str
    label: str
    engine_version: str
    strategy_name: str
    underlying: str
    timeframe: str
    data_fingerprint: str
    config_fingerprint: str
    source_fingerprints: dict[str, str]
    config: dict[str, Any]
    data_quality: dict[str, Any]
    journal: list[dict[str, Any]]
    trades: list[dict[str, Any]]
    open_structures: list[dict[str, Any]]
    equity_curve: list[dict[str, Any]]
    summary: dict[str, Any]
    fault_plan: dict[str, Any]
    no_lookahead: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return _json_safe(
            {
                "schema_version": 1,
                "label": self.label,
                "engine_version": self.engine_version,
                "run_id": self.run_id,
                "strategy_name": self.strategy_name,
                "underlying": self.underlying,
                "timeframe": self.timeframe,
                "data_fingerprint": self.data_fingerprint,
                "config_fingerprint": self.config_fingerprint,
                "source_fingerprints": self.source_fingerprints,
                "config": self.config,
                "data_quality": self.data_quality,
                "journal": self.journal,
                "trades": self.trades,
                "open_structures": self.open_structures,
                "equity_curve": self.equity_curve,
                "summary": self.summary,
                "fault_plan": self.fault_plan,
                "no_lookahead": self.no_lookahead,
                "limitations": [
                    "One-minute OHLCV cannot validate bid/ask fills or slippage.",
                    "When a candle touches both stop and target, the resolver assumes stop first.",
                    "The harness does not establish fill quality or live quote-stream behavior.",
                    "Restart recovery is not exercised; replay is process-local and ephemeral.",
                    "Live soak remains mandatory before any live trading gate can pass.",
                ],
                "evidence_boundary": {
                    "validates": [
                        "signal timing",
                        "truncation-based no-lookahead",
                        "contract selection",
                        "exit precedence",
                        "ledger/cash invariants",
                        "injected data/order failure handling",
                        "determinism/regression",
                    ],
                    "does_not_validate": [
                        "live fill quality or slippage",
                        "bid/ask execution",
                        "live quote-stream exits",
                        "broker quirks",
                        "restart recovery",
                        "strategy profitability or performance metrics",
                    ],
                },
            }
        )

    def export(self, path: str | Path) -> Path:
        """Atomically export this run as a portable JSON artifact."""
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{self.run_id}.tmp")
        payload = json.dumps(self.to_dict(), indent=2, sort_keys=True, ensure_ascii=False)
        temporary.write_text(payload + "\n", encoding="utf-8")
        os.replace(temporary, destination)
        return destination


class ReplayHarness:
    """Run existing forward option strategy logic against historical CSV bars."""

    def __init__(
        self,
        dataset: ReplayDataset,
        config: ReplayConfig | None = None,
        *,
        fault_plan: ReplayFaultPlan | None = None,
        strategy_factory: Callable[[], Any] | None = None,
        strategy: Any | None = None,
    ) -> None:
        self.dataset = dataset
        self.config = copy.deepcopy(config or ReplayConfig())
        if (
            self.config.underlying_symbol
            and self.config.underlying_symbol != dataset.underlying_symbol
        ):
            raise ValueError(
                f"configured underlying {self.config.underlying_symbol} does not match "
                f"dataset {dataset.underlying_symbol}"
            )
        self.fault_plan = copy.deepcopy(fault_plan or ReplayFaultPlan())
        self.strategy_factory = strategy_factory
        self._strategy_template = strategy
        self.config_fingerprint = _sha256_json(self._config_payload())
        self.run_id = (
            "replay_"
            + _sha256_json(
                {
                    "engine": REPLAY_ENGINE_VERSION,
                    "data": dataset.fingerprint,
                    "config": self.config_fingerprint,
                    "faults": self._fault_payload(),
                }
            )[:20]
        )

    def run(self, *, verify_no_lookahead: bool = False) -> ReplayResult:
        result = self._run_once(self.dataset)
        proof = None
        if verify_no_lookahead:
            proof = self.verify_no_lookahead()
        result.no_lookahead = proof
        return result

    def verify_no_lookahead(self) -> dict[str, Any]:
        """Compare the full run with every closed-bar truncation.

        One checkpoint per strategy-visible bar is exhaustive over possible
        decision times in this discrete feed. This is intentionally an
        opt-in CLI check because its cost grows with the number of bars.
        """
        full = self._run_once(self.dataset)
        full_decisions = _decision_signature(full.journal)
        checked = 0
        for count in range(1, self.dataset.bar_count + 1):
            prefix = self.dataset.prefix(count)
            truncated = self._run_once(prefix)
            cutoff = prefix.bar_close(prefix.underlying.index[-1]).isoformat()
            cutoff_decisions = [
                event
                for event in full_decisions
                if _as_utc_timestamp(event["ts"]) <= _as_utc_timestamp(cutoff)
            ]
            prefix_decisions = _decision_signature(truncated.journal)
            if cutoff_decisions != prefix_decisions:
                raise ReplayInvariantError(
                    "truncation proof failed at "
                    f"{cutoff}: full decisions={cutoff_decisions!r}, "
                    f"prefix decisions={prefix_decisions!r}"
                )
            checked += 1
        return {
            "passed": True,
            "method": "full replay vs every closed-bar truncation",
            "checkpoints": checked,
            "decision_count": len(full_decisions),
            "data_fingerprint": self.dataset.fingerprint,
        }

    def _run_once(self, dataset: ReplayDataset) -> ReplayResult:
        if dataset.bar_count == 0:
            raise ValueError("cannot replay an empty underlying frame")
        clock = SimulatedClock(dataset.bar_close(dataset.underlying.index[0]))
        feed = PointInTimeReplayFeed(dataset, clock, self.fault_plan)
        quotes = ReplayQuoteProvider(feed)
        broker = OptionPaperBroker(
            capital=float(self.config.initial_capital),
            # No bid/ask exists in OHLCV. Replay fills at recorded candle
            # prices without adding a synthetic spread or slippage model.
            slippage_pct=0.0,
            commission_per_lot=float(self.config.commission_per_lot),
        )
        expression = copy.deepcopy(self.config.expression)
        if not expression:
            _cls = get_strategy(self.config.strategy_name)
            _spec = _cls.execution_spec()
            if _spec is None:
                raise ValueError(
                    f"replay of {self.config.strategy_name}: strategy "
                    "declares no execution spec and no v2 expression was "
                    "supplied — nothing to resolve (Phase 4)"
                )
            expression = _spec.to_expression()
        if int(expression.get("expression_version", 0) or 0) == 2:
            # Phase 1 (plan §6.3): replay may not keep a private mapping
            # logic — a v2 expression goes through the same validation as
            # the forward path, gate entitlement included.
            from backtest.options.execution_gate import PreTradeRiskGate
            from backtest.strategy.execution import validate_expression

            _errs = validate_expression(
                expression,
                permitted_structures=PreTradeRiskGate(
                    mode="paper"
                ).permitted_structures(),
            )
            if _errs:
                raise ValueError("v2 replay expression refused: " + "; ".join(_errs))
        bridge = OptionsBridge(
            capital=float(self.config.initial_capital),
            expression=expression,
            option_broker=broker,
            quote_provider=quotes,
            chain_generator=quotes.generator,
            defer_entry_fills=True,
            intrabar_exit_resolver=OHLCConservativeExitResolver(),
            audit=True,
        )
        runner_config = RunnerConfig(
            name=f"REPLAY {self.config.strategy_name}",
            strategy_name=self.config.strategy_name,
            strategy_params=dict(self.config.strategy_params),
            allocated_capital=float(self.config.initial_capital),
            symbols=[dataset.underlying_symbol],
            timeframe=dataset.timeframe,
            max_drawdown_pct=float(self.config.max_drawdown_pct),
            daily_loss_limit_pct=float(self.config.daily_loss_limit_pct),
            instance_id=self.run_id,
            mode="paper",
            source="replay",
            instrument={"type": "option", "expression": expression},
        )
        runner = StrategyRunner(
            runner_config,
            ledger=OrderLedger(),
            strategy=self._new_strategy(),
            options_bridge=bridge,
            clock=clock.now,
            preserve_candle_timestamps=True,
        )
        runner.start()

        journal: list[dict[str, Any]] = []
        audit_cursor = 0
        previous_open: pd.Timestamp | None = None
        reconciliation_checks = 0
        for index in range(dataset.bar_count):
            bar = feed.advance(index)
            halt_reasons: list[dict[str, Any]] = []
            if self.fault_plan.stale_feed(bar.bar_open):
                halt_reasons.append(
                    {
                        "code": "STALE_FEED",
                        "detail": "authored stale-feed window; strategy disabled for this bar",
                    }
                )
            if previous_open is not None and _has_underlying_gap(
                previous_open,
                bar.bar_open,
                dataset.interval,
            ):
                gap_minutes = (
                    int((bar.bar_open - previous_open) / pd.Timedelta(minutes=1))
                    - dataset.interval_minutes
                )
                halt_reasons.append(
                    {
                        "code": "UNDERLYING_STALE_GAP",
                        "detail": (
                            f"{max(gap_minutes, 1)} missing underlying minute(s) "
                            f"before {bar.bar_open.isoformat()}"
                        ),
                    }
                )
            previous_open = bar.bar_open

            open_positions = broker.get_open_positions()
            open_tokens = [position.instrument_token for position in open_positions]
            missing = feed.missing_quotes(open_tokens)
            if missing:
                halt_reasons.append(
                    {
                        "code": "STRIKE_NO_FRESH_QUOTE",
                        "instrument_tokens": missing,
                        "detail": (
                            "held contract quote missing at this bar; last mark retained, "
                            "no carry-forward quote"
                        ),
                    }
                )
            changed_lots = [
                {
                    "instrument_token": position.instrument_token,
                    "position_lot_size": position.lot_size,
                    "feed_lot_size": feed.current_contracts[position.instrument_token].lot_size,
                }
                for position in open_positions
                if position.instrument_token in feed.current_contracts
                and feed.current_contracts[position.instrument_token].lot_size != position.lot_size
            ]
            if changed_lots:
                halt_reasons.append(
                    {
                        "code": "LOT_SIZE_CHANGED",
                        "contracts": changed_lots,
                        "detail": (
                            "held contract lot size changed; last mark retained, "
                            "strategy disabled"
                        ),
                    }
                )

            for reason in halt_reasons:
                journal.append(
                    {
                        "kind": "feed_halt",
                        "source": "REPLAY",
                        "ts": bar.bar_close.isoformat(),
                        "bar_open": bar.bar_open.isoformat(),
                        "bar_index": index + 1,
                        "dataset_index": index,
                        **reason,
                    }
                )
            feed_valid = not halt_reasons
            runner.process_candle_event(
                dataset.underlying_symbol,
                bar.as_runner_candle(),
                allow_trading=feed_valid,
            )
            for event in bridge.audit_events[audit_cursor:]:
                if event.get("kind") == "fill":
                    _assert_fill_timing(event)
                journal.append({"source": "REPLAY", **copy.deepcopy(event)})
            audit_cursor = len(bridge.audit_events)
            _assert_ledger_invariants(broker, runner, bar.bar_close)
            reconciliation_checks += 1

        runner.stop()
        open_structures = bridge.open_structures_snapshot()
        trades = []
        for trade in runner.closed_option_trades:
            record = dict(trade)
            record["source"] = "REPLAY"
            trades.append(record)
        for structure in broker.get_open_structures():
            journal.append(
                {
                    "kind": "open_at_end",
                    "source": "REPLAY",
                    "ts": (structure.opened_at.isoformat() if structure.opened_at else None),
                    "structure_id": structure.structure_id,
                    "structure_type": structure.structure_type,
                    "legs": [leg.trading_symbol for leg in structure.legs],
                    "reason": "run ended; open book was not force-closed",
                }
            )
        if bridge.pending_intent is not None:
            pending = bridge.pending_intent
            journal.append(
                {
                    "kind": "pending_at_end",
                    "source": "REPLAY",
                    "ts": _safe_iso(pending.get("intent_ts")),
                    "bar_index": pending.get("intent_bar_index"),
                    "structure_type": getattr(pending.get("intent"), "structure_type", None),
                    "reason": "run ended before the next-bar fill window",
                }
            )

        equity_curve = [
            {"ts": str(point["ts"]), "equity": float(point["equity"])}
            for point in runner.equity_curve
        ]
        expected_equity = (
            broker.capital
            + broker.total_realized_pnl
            + broker.total_unrealized_pnl
            - broker.total_costs_paid
        )
        cash_equity = broker.available_cash + _signed_open_mark_value(broker)
        ledger_difference = float(broker.total_equity - expected_equity)
        cash_difference = float(cash_equity - broker.total_equity)
        if abs(ledger_difference) > 0.01 or abs(cash_difference) > 0.01:
            raise ReplayInvariantError(
                "ledger/cash invariant failed: "
                f"formula delta={ledger_difference}, cash delta={cash_difference}"
            )

        summary = {
            "status": runner.status,
            "engine_error": runner.error,
            "bars_processed": runner.bars_processed,
            "decisions": sum(event.get("kind") == "decision" for event in journal),
            "intents": sum(event.get("kind") == "intent" for event in journal),
            "fills": sum(event.get("kind") == "fill" for event in journal),
            "exits": sum(event.get("kind") == "exit" for event in journal),
            "rejections": sum(event.get("kind") == "rejected" for event in journal),
            "feed_halts": sum(event.get("kind") == "feed_halt" for event in journal),
            "closed_trades": len(trades),
            "open_structures": len(open_structures),
            "pending_intent": bridge.pending_intent is not None,
            "initial_capital": float(broker.capital),
            "available_cash": float(broker.available_cash),
            "final_equity": float(broker.total_equity),
            "realized_pnl": float(broker.total_realized_pnl),
            "unrealized_pnl": float(broker.total_unrealized_pnl),
            "costs_paid": float(broker.total_costs_paid),
            "cash_ledger_delta": cash_difference,
            "equity_ledger_delta": ledger_difference,
            "ledger_invariants_passed": True,
            "ledger_reconciliation_checks": reconciliation_checks,
            "db_writes": 0,
        }
        return ReplayResult(
            run_id=self.run_id,
            label="REPLAY",
            engine_version=REPLAY_ENGINE_VERSION,
            strategy_name=self.config.strategy_name,
            underlying=dataset.underlying_symbol,
            timeframe=dataset.timeframe,
            data_fingerprint=dataset.fingerprint,
            config_fingerprint=self.config_fingerprint,
            source_fingerprints=dict(dataset.source_fingerprints),
            config=self._config_payload(),
            data_quality=copy.deepcopy(dataset.quality_report),
            journal=journal,
            trades=trades,
            open_structures=open_structures,
            equity_curve=equity_curve,
            summary=summary,
            fault_plan=self._fault_payload(),
        )

    def _new_strategy(self) -> Any:
        if self.strategy_factory is not None:
            return self.strategy_factory()
        if self._strategy_template is not None:
            return copy.deepcopy(self._strategy_template)
        return get_strategy(self.config.strategy_name)(**self.config.strategy_params)

    def _config_payload(self) -> dict[str, Any]:
        payload = self.config.to_dict()
        payload.update(
            {
                "engine_version": REPLAY_ENGINE_VERSION,
                "timeframe": self.dataset.timeframe,
                "timestamp_convention": (
                    "source candle starts; simulated clock advances at bar close"
                ),
                "entry_fill": "next available bar open",
                "exit_fill": "ohlc_conservative_stop_first",
                "fees": "OptionPaperBroker commission; no synthetic bid/ask or slippage",
                "restart_mode": "not persisted; process-local only",
            }
        )
        return payload

    def _fault_payload(self) -> dict[str, Any]:
        return {
            "drop_quotes": sorted(self.fault_plan.drop_quotes),
            "drop_contracts": sorted(self.fault_plan.drop_contracts),
            "lot_size_overrides": sorted(
                (token, timestamp, size)
                for (
                    token,
                    timestamp,
                ), size in self.fault_plan.lot_size_overrides.items()
            ),
            "reject_entry_bars": sorted(self.fault_plan.reject_entry_bars),
            "stale_windows": sorted(self.fault_plan.stale_windows),
        }


def _decision_signature(journal: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Only decisions/intent selection belong in the truncation comparison."""
    return [
        {
            key: event.get(key)
            for key in (
                "kind",
                "ts",
                "bar_index",
                "direction",
                "confidence",
                "spot",
                "view_ts",
                "structure_type",
                "underlying",
                "strikes",
                "expiry",
                "contracts",
            )
        }
        for event in journal
        if event.get("kind") in {"decision", "intent"}
    ]


def _assert_fill_timing(event: Mapping[str, Any]) -> None:
    """Prove each replay entry follows its signal and uses a next-bar open."""
    intent_index = int(event.get("intent_bar_index", -1))
    fill_index = int(event.get("fill_bar_index", -1))
    if intent_index < 0 or fill_index != intent_index + 1:
        raise ReplayInvariantError(
            f"fill {event.get('structure_id')} is not on the immediately following bar"
        )
    intent_ts = _as_utc_timestamp(event["intent_ts"])
    fill_ts = _as_utc_timestamp(event["ts"])
    if fill_ts < intent_ts:
        raise ReplayInvariantError("option fill timestamp precedes its closed-bar intent")
    reads = event.get("quote_reads") or []
    if not reads:
        raise ReplayInvariantError("replay fill has no auditable quote reference")
    expected_tokens = {
        str(leg["instrument_token"])
        for leg in event.get("legs", [])
        if leg.get("instrument_token") is not None
    }
    read_tokens = [str(read.get("instrument_token")) for read in reads]
    if (
        not expected_tokens
        or set(read_tokens) != expected_tokens
        or len(read_tokens) != len(expected_tokens)
    ):
        raise ReplayInvariantError(
            "entry fill quote audit does not cover each selected option leg exactly once"
        )
    entry_prices = {
        str(leg["instrument_token"]): Decimal(str(leg["entry_price"]))
        for leg in event.get("legs", [])
        if leg.get("instrument_token") is not None
    }
    for read in reads:
        if read.get("phase") != "open":
            raise ReplayInvariantError(
                f"entry fill used {read.get('phase')!r} instead of the next bar's open"
            )
        quote_ts = _as_utc_timestamp(read["quote_timestamp"])
        bar_open = _as_utc_timestamp(read["bar_open"])
        bar_close = _as_utc_timestamp(read["bar_close"])
        if quote_ts != bar_open or fill_ts != bar_open:
            raise ReplayInvariantError(
                "entry fill or quote did not use the next bar's first available open"
            )
        if quote_ts < intent_ts or not (bar_open <= quote_ts <= bar_close):
            raise ReplayInvariantError(
                "entry quote timestamp is outside the next bar or precedes intent: "
                f"quote={quote_ts}, intent={intent_ts}"
            )
        token = str(read["instrument_token"])
        if token not in entry_prices or Decimal(str(read["price"])) != entry_prices[token]:
            raise ReplayInvariantError(
                f"fill price for {token} differs from its audited open quote"
            )


def _assert_ledger_invariants(
    broker: OptionPaperBroker,
    runner: StrategyRunner,
    timestamp: pd.Timestamp,
) -> None:
    expected = (
        broker.capital
        + broker.total_realized_pnl
        + broker.total_unrealized_pnl
        - broker.total_costs_paid
    )
    cash_equity = broker.available_cash + _signed_open_mark_value(broker)
    if abs(broker.total_equity - expected) > Decimal("0.01"):
        raise ReplayInvariantError(f"book equity formula diverged at {timestamp}")
    if abs(cash_equity - broker.total_equity) > Decimal("0.01"):
        raise ReplayInvariantError(f"cash/mark ledger diverged at {timestamp}")
    if abs(Decimal(str(runner.equity())) - broker.total_equity) > Decimal("0.01"):
        raise ReplayInvariantError(f"forward StrategyRunner equity diverged at {timestamp}")


def _signed_open_mark_value(broker: OptionPaperBroker) -> Decimal:
    value = Decimal("0")
    for position in broker.get_open_positions():
        sign = Decimal("1") if position.is_long else Decimal("-1")
        value += position.current_price * Decimal(str(position.total_quantity)) * sign
    return value


def _has_underlying_gap(
    previous_open: pd.Timestamp,
    current_open: pd.Timestamp,
    interval: pd.Timedelta,
) -> bool:
    previous_local = previous_open.tz_convert("Asia/Kolkata")
    current_local = current_open.tz_convert("Asia/Kolkata")
    return previous_local.date() == current_local.date() and current_open - previous_open > interval


def _as_utc_timestamp(value: Any) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        # Bridge audit timestamps are naive by contract and represent UTC.
        stamp = stamp.tz_localize("UTC")
    return stamp.tz_convert("UTC")


def _safe_iso(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    try:
        return pd.Timestamp(value).isoformat()
    except Exception:  # noqa: BLE001
        return str(value)


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if pd.isna(value) or value in (float("inf"), float("-inf")):
            return None
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (pd.Timestamp, datetime)) or hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except (TypeError, ValueError):
            pass
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "value"):
        return _json_safe(value.value)
    try:
        return value.item()
    except (AttributeError, ValueError):
        return str(value)


def _sha256_json(value: Any) -> str:
    payload = json.dumps(
        _json_safe(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = [
    "REPLAY_ENGINE_VERSION",
    "ReplayConfig",
    "ReplayHarness",
    "ReplayInvariantError",
    "ReplayResult",
]
