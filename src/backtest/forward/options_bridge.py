"""Options bridge for the forward engine (Gap-Analysis G3.2).

Connects a strategy's :class:`~backtest.strategy.intent.MarketView` to the
options expression layer so an automated runner can trade structures — the
piece that was missing between ``DirectionalOptions.generate_market_view()``
(G3.1) and a live paper book:

    strategy view  →  bridge  →  chain + selector + structure
                              →  OptionPaperBroker.execute_structure()

Each runner owns an isolated bridge (its own
:class:`~backtest.options.paper_trading.OptionPaperBroker` and synthetic
quote feed), mirroring how equity runners get isolated portfolios, so
option and equity flows coexist without touching each other's books.

Expression config (``RunnerConfig.instrument["expression"]``)::

    {
        "type": "bull_call_spread",          # or {"BULLISH": ..., "BEARISH": ...}
        "strike_selection": "atm" | "delta",
        "delta_target": 0.35,
        "quantity": 1,                        # lots per leg
    }

Position lifecycle (task B1): one structure at a time. While it is open new
views do not pyramid — they are **exit signals**. Every bar the bridge asks
:class:`~backtest.options.exit_policy.ExitPolicy` whether to leave (stop,
target, days-to-expiry, time stop, signal flip, signal neutral) and closes
through the broker, recording ``exit_reason``. ``expression["exit"]``
configures the rules; ``reenter`` optionally reverses into the opposite
structure on a flip. Expiry settlement is B2: :meth:`OptionsBridge.on_bar`
drives the canonical :class:`~backtest.options.expiry.ExpiryManager` against
the bar clock, so a structure held into expiry is cash-settled at the
settlement spot on the expiry bar, and the next view re-enters on the next
monthly expiry (a roll).

Reporting (task C2): :meth:`OptionsBridge.open_structures_snapshot` turns the
book's open legs into flat, JSON-safe rows (structure, strikes, net premium,
mark, unrealized P&L, next expiry) so the portfolio matrix and the deep-dive
drawer can render an option runner with option-shaped columns instead of the
equity ones. ``summary()`` embeds it as ``open_structures_detail``.

Per-bar pricing (task A1): the bridge is also the forward loop's **market
clock**. :meth:`OptionsBridge.on_bar` moves the synthetic spot to each bar
close, pins the quote provider's pricing reference to the bar timestamp (so
theta follows the replay clock instead of the wall clock) and marks the book
to market. Without it a runner's legs keep their entry premium forever and
``unrealized_pnl`` stays pinned at zero.
"""

from __future__ import annotations

import copy
import logging
import time
from collections import deque
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Deque, Iterable, Optional

from backtest.data.sources_policy import SourceDisabledError, synthetic_chain_generator
from backtest.options.execution_gate import (
    PreTradeRiskGate,
    REASON_EXPIRY_TOO_NEAR,
    REASON_NO_CONTRACT,
    REASON_NO_EXPIRY,
    REASON_NO_FRESH_QUOTE,
    REASON_NO_LOT_SIZE,
    REASON_NO_STRIKE,
    ContractValidityError,
    V1_ALLOWED_STRUCTURES,
    check_structure_type,
)
from backtest.options.exit_policy import (
    EXIT_DTE,
    EXIT_MANUAL_STOP,
    EXIT_MANUAL_TARGET,
    EXIT_SIGNAL_FLIP,
    PRICE_MEASURED_REASONS,
    RISK_REASONS,
    ExitConfig,
    ExitDecision,
    ExitPolicy,
    _label,
)
from backtest.options.intent_journal import (
    KIND_DECIDED,
    KIND_EXECUTED,
    KIND_HALTED,
    KIND_REJECTED,
    decision_key,
    intent_key,
)
from backtest.options.greeks import BlackScholes
from backtest.options.expiry import EXIT_REASON_SETTLEMENT, ExpiryManager
from backtest.options.expiry_policy import NearestExpiryPolicy, create_expiry_policy
from backtest.options.paper_trading import (
    STALE_AFTER_SECONDS,
    OptionPaperBroker,
    PositionStatus,
    session_is_open,
)
from backtest.options.quote_providers import (
    SyntheticQuoteProvider,
)
from backtest.options.selector import ATMSelector, DeltaSelector, create_selector
from backtest.options.structures import (
    BearPutSpread,
    BullCallSpread,
    LongCall,
    LongPut,
    ShortStrangle,
)
from backtest.strategy.intent import Direction, MarketView

logger = logging.getLogger("backtest.forward.options_bridge")

#: Sentinel for "argument not supplied" in :meth:`OptionsBridge.set_manual_rule`
#: — distinct from an explicit ``None``, which CLEARS the level.
_UNSET: Any = object()


def _market_is_open(now_utc: Optional[datetime] = None) -> bool:
    """Whether the NSE session is trading right now (marks are only expected then).

    Session arithmetic rather than the holiday calendar: the DB-backed calendar
    used here put a retrying database connect on every mark, which stalls the
    runner when the database is unreachable.
    """
    return session_is_open(now_utc)


#: structure_type → (structure instance, option side of the chain)
STRUCTURES: dict[str, tuple[Any, str]] = {
    "long_call": (LongCall(), "CE"),
    "long_put": (LongPut(), "PE"),
    "bull_call_spread": (BullCallSpread(), "CE"),
    "bear_put_spread": (BearPutSpread(), "PE"),
    # Strangles need BOTH sides — the bridge materialises a two-sided
    # nested chain {strike: {"CE": c, "PE": c}} at execution time (the flat
    # V1 shape cannot hold two contracts at one strike).
    "strangle": (ShortStrangle(), "BOTH"),
}

#: One- vs two-strike structures.
_STRICKES_NEEDED = {
    "long_call": 1,
    "long_put": 1,
    "bull_call_spread": 2,
    "bear_put_spread": 2,
    "short_strangle": 2,
}

#: Risk-free rate used for the display-only Greeks (a dashboard column, not a
#: pricing input — premiums come from the quote provider).
DEFAULT_RISK_FREE_RATE = 0.06

#: Default expression: direction-aware long premium, ATM strikes, one lot.
#: V1 buys a call when bullish and a put when bearish — the same mapping the
#: builder's ``direction_map`` compiles to. The bridge's STRUCTURES registry is
#: deliberately wider (research profile); what it may *execute* is capped by
#: ``allowed_structures`` (backtest/options/execution_gate.py). A research run
#: that wants spreads passes the expression *and* the wider whitelist.
DEFAULT_EXPRESSION: dict[str, Any] = {
    "type": {"BULLISH": "long_call", "BEARISH": "long_put"},
    "strike_selection": "atm",
    "quantity": 1,
}


class OptionsBridge:
    """Turns market views into executed option structures on a paper book.

    Parameters
    ----------
    capital:
        Starting capital for the isolated option paper book.
    expression:
        Structure selection config (see module docstring). ``None`` gives
        the direction-aware default.
    option_broker / quote_provider:
        Injection points for tests; the broker defaults to a fresh paper book.
        The quote feed has no ungated default: with ``quote_provider=None`` the
        bridge builds a synthetic Black-Scholes feed only while the data-source
        policy enables synthetic, and raises ``SourceDisabledError`` otherwise.
    allowed_structures:
        Execution-gate whitelist for this bridge (see
        ``backtest/options/execution_gate.py``). Defaults to the V1 set: long
        call / long put. The check runs here *and* again in the broker, so a
        structure the contract forbids cannot open from either layer.
    bar_cadence_seconds:
        Width of one bar in market seconds, passed by the runner. Marks arrive
        once per bar (plus the 60 s R6 watchdog), so the freshness SLA may never
        be tighter than the cadence the book is actually marked on — a daily
        runner would otherwise read as stale minutes after every mark.
    """

    def __init__(
        self,
        capital: float,
        expression: Optional[dict[str, Any]] = None,
        option_broker: Optional[OptionPaperBroker] = None,
        quote_provider: Optional[Any] = None,
        allowed_structures: Optional[Iterable[str]] = None,
        bar_cadence_seconds: Optional[float] = None,
        intent_journal: Optional[Any] = None,
        fee_calculator: Any | None = None,
        mode: str = "paper",
        risk_gate: Optional[PreTradeRiskGate] = None,
    ) -> None:
        # Phase 1 (Plan v2.1 §6.2): strict mode is gated on the v2 marker.
        # v1 expressions keep the legacy path byte-for-byte (default merge,
        # cross-direction fallbacks) until Phase 4 removes it.
        self.strict: bool = bool(
            isinstance(expression, dict)
            and int(expression.get("expression_version", 0) or 0) == 2
        )
        self.mode: str = str(mode or "paper").strip().lower()
        self.risk_gate: Optional[PreTradeRiskGate] = None
        self._strict_rules: dict[Direction, Any] = {}
        self._active_rule: Any = None
        self._halted_codes: set[str] = set()
        self.last_block_code: Optional[str] = None
        if self.strict:
            from backtest.strategy.execution import (
                DirectionRule,
                ExecutionSpecError,
                parse_exit_plan_strict,
                validate_expression,
            )

            # The gate is permission; constructing it loads the Layer-1
            # rulebook fail-closed (a broken limits file refuses here,
            # never permits by default).
            self.risk_gate = risk_gate or PreTradeRiskGate(mode=self.mode)
            permitted = self.risk_gate.permitted_structures()
            if allowed_structures is not None:
                permitted = permitted & frozenset(allowed_structures)
            errors = validate_expression(expression, permitted_structures=permitted)
            if errors:
                raise ExecutionSpecError(
                    "strict (v2) expression refused: " + "; ".join(errors)
                )
            self.expression = copy.deepcopy(expression or {})
            self.allowed_structures = permitted
            type_cfg = self.expression.get("type")
            if isinstance(type_cfg, dict):
                for raw_direction, raw_rule in type_cfg.items():
                    from backtest.strategy.execution import canonical_direction

                    self._strict_rules[canonical_direction(raw_direction)] = (
                        DirectionRule.parse(raw_rule)
                    )
            elif isinstance(type_cfg, str):
                rule = DirectionRule.parse(type_cfg)
                self._strict_rules = {
                    Direction.BULLISH: rule,
                    Direction.BEARISH: rule,
                }
            # Strict exit parsing (legacy tolerant parser stays on the v1
            # path below): unknown keys or a missing hard stop refused at
            # construction, so an incomplete contract can never trade.
            self._strict_exit_plan = parse_exit_plan_strict(self.expression.get("exit"))
        else:
            self.expression: dict[str, Any] = {**DEFAULT_EXPRESSION, **(expression or {})}
            self.allowed_structures: frozenset[str] = frozenset(
                allowed_structures
                if allowed_structures is not None
                else V1_ALLOWED_STRUCTURES
            )
        #: Append-only intent journal (PRD decision 8). ``None`` records nothing
        #: — the runner wires it, so a decision is auditable without the paper
        #: path taking a dependency on a database it may not have.
        self.intent_journal: Optional[Any] = intent_journal
        self.option_broker = option_broker or OptionPaperBroker(
            capital=float(capital),
            allowed_structures=self.allowed_structures,
            fee_calculator=fee_calculator,
        )
        # Policy-gated feed construction (2026-09-30). This used to answer
        # ``quote_provider=None`` with a fresh Black-Scholes book AND attach a
        # generated chain to ANY provider that lacked a generator surface —
        # so a live-labelled runner priced strikes and expiries the market
        # never listed. The forward path hands in the provider that
        # ``option_quote_provider_for`` resolved (already gated, and a
        # ``LiveChainProvider`` exposes ``.generator`` = itself); with no
        # provider and synthetic off, there is nothing to price on and this
        # refuses instead of inventing one.
        self.quote_provider: Any = quote_provider
        if self.quote_provider is None:
            generated = synthetic_chain_generator("options bridge quote feed")
            if generated is None:
                raise SourceDisabledError(
                    "OptionsBridge was built with no quote provider while synthetic "
                    "chains are disabled in config/data_sources.yaml. Pass the "
                    "broker-backed provider from "
                    "option_quote_provider_for(source, underlying); a generated "
                    "feed only exists for a policy that enables synthetic (the "
                    "test profile)."
                )
            self.quote_provider = SyntheticQuoteProvider(generated)
        elif getattr(self.quote_provider, "generator", None) is None:
            generated = synthetic_chain_generator("options bridge chain surface")
            if generated is not None:
                self.quote_provider.generator = generated
        # -- exit policy (task B1) ------------------------------------------
        if self.strict:
            plan = self._strict_exit_plan
            self.exit_policy = ExitPolicy(
                ExitConfig(
                    signal_flip=plan.signal_flip,
                    neutral_bars=plan.neutral_bars,
                    stop_loss_pct=plan.stop_loss_pct,
                    take_profit_pct=plan.take_profit_pct,
                    stop_loss_points=plan.stop_loss_points,
                    take_profit_points=plan.take_profit_points,
                    max_loss_inr=plan.max_loss_inr,
                    max_bars=plan.max_bars,
                    min_days_to_expiry=plan.min_days_to_expiry,
                    reenter=plan.reenter,
                    max_reentries_per_day=plan.max_reentries_per_day,
                )
            )
        else:
            self.exit_policy = ExitPolicy(ExitConfig.from_expression(self.expression.get("exit")))
        #: Minutes before the close at which the canonical expiry pipeline
        #: squares a *same-day* expiry off at market (B2). 0 = never (ride to
        #: cash settlement). Daily forward bars land at 09:15, so this only
        #: fires for intraday bar cycles.
        self.squareoff_minutes_before: int = int(
            self.expression.get("squareoff_minutes_before", 30) or 0
        )
        #: Minimum days-to-expiry for ENTRY selection (2026-09-24): the bridge
        #: always picked the NEAREST expiry, so a structure spawned on expiry
        #: day was cash-settled on the very next bar (instant churn). With
        #: ``expression["expiry_min_days"] = N`` entry selects the nearest
        #: expiry at least N days out; a calendar that offers none halts the
        #: entry with EXPIRY_BELOW_MIN_DAYS rather than trading the near one.
        try:
            self.expiry_min_days: int = max(0, int(self.expression.get("expiry_min_days", 0) or 0))
        except (TypeError, ValueError):
            self.expiry_min_days = 0

        self.open_structure_id: Optional[str] = None
        self.executed_count: int = 0
        self.closed_count: int = 0
        #: Underlying of the most recent structure — the symbol whose spot
        #: drives pricing (set at entry, defaulted for the first bar).
        self.underlying: str = "NIFTY"
        #: Last mark-to-market book value, for runner heartbeats/UI.
        self.last_unrealized_pnl: Decimal = Decimal("0")
        self.last_spot: Optional[float] = None
        self.last_mtm_ts: Optional[str] = None
        #: Most recent close, for the runner's signal log / deep dive.
        self.last_exit: Optional[dict[str, Any]] = None
        #: Exits that happened outside :meth:`on_market_view` (the per-bar
        #: risk rules) — the runner drains these to log them.
        self._exit_events: Deque[dict[str, Any]] = deque(maxlen=32)

        # -- operator-set levels (Live Order Management) ---------------------
        #: Manual stop / target on the OPEN structure's net premium per unit.
        #: Expressed in premium (the same unit ``entry_price`` /
        #: ``current_price`` use on a structure row), so "stop at ₹80" means
        #: 80 per unit of the structure, not a ₹ P&L figure. Kept on the
        #: bridge because only one structure is open at a time (V1), and
        #: cleared the moment that structure closes — a stale level must never
        #: ambush the next trade.
        self.manual_stop_loss: Optional[float] = None
        self.manual_target: Optional[float] = None

        # -- bar clock / structure age bookkeeping ---------------------------
        self._bar_index: int = 0
        self._entry_bar_index: Optional[int] = None
        self._exit_bar_index: Optional[int] = None
        self._bars_without_view: int = 0
        self._bar_dt: Optional[datetime] = None
        self._structure_direction: Optional[Any] = None
        self._structure_expiry: Optional[Any] = None
        self._entry_premium: Decimal = Decimal("0")
        self._settlement_count: int = 0
        self._expiry_manager: Optional[ExpiryManager] = None
        # U2.2: re-entry tracking — max_reentries_per_day knob, default 2, next-bar only
        self._reentries_today: int = 0
        self._reentry_day: Optional[Any] = None

        # -- MTM Staleness Observability (R1, R4, R6) -----------------------
        # Freshness is a market-session SLA (PRD §4.1/§8.1), not a bar count: a
        # feed that stops delivering bars produces no failed marks at all.
        # Explicit ``stale_after_seconds`` in the expression wins; otherwise the
        # runner's bar cadence plus the module grace (which clears the 60 s R6
        # watchdog pass without ever flagging a healthy book).
        self.stale_after_seconds = self._resolve_stale_after_seconds(bar_cadence_seconds)
        #: True while the owning runner is RUNNING (default True for standalone
        #: bridge use; StrategyRunner syncs this to ``status == STATUS_RUNNING``
        #: so paused/stopped runners report ``mark_stale=False`` per §5).
        self.runner_running: bool = True
        self.runner_id: Optional[str] = None
        self.runner_name: Optional[str] = None
        #: R4 throttle window (seconds) between repeated quote-failure warnings
        #: for the same (runner, symbol). First failure logs immediately, then
        #: at most once per 10 minutes while it persists.
        self.warn_throttle_seconds: float = 600.0
        self._last_failure_warn_ts: dict[str, float] = {}
        #: R6/R7 edge-triggered staleness episode state (notify once per
        #: healthy → stale transition).
        self._in_stale_episode: bool = False
        self._stale_episode_id: int = 0
        self.stale_notifications: list[dict[str, Any]] = []
        self.last_refresh_result: Optional[dict[str, Any]] = None

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def on_market_view(self, view: MarketView | None, strategy_name: str) -> dict[str, Any] | None:
        """Consume one bar's view: manage an open structure, else open one.

        Called once per bar for single-symbol option runners, **including
        bars with no view** — a viewless bar is how the neutral/time rules and
        a flip out of a position are detected, so ``None`` is meaningful
        rather than a no-op.

        Returns:

        * ``{"exited": True, ...}`` when a structure was closed,
        * the entry summary dict when a structure was opened,
        * ``{"rejected": True, ...}`` on a soft rejection (e.g. margin),
        * ``None`` when nothing happened.
        """
        if self._has_open_structure():
            closed = self._manage_open_structure(view, strategy_name)
            if closed is None:
                # Still holding: a view against the position is an exit signal,
                # never an entry one (V1 does not pyramid).
                return None
            # U2.2: same-bar re-entry impossible — even flip+reenter must wait to next bar
            # should_reenter() now returns False on same-bar, so we always return closed here
            # Re-entry will happen on next bar via normal entry path, counted via _reentries_today
            if not self._should_reenter(view):
                return closed
            # If we reach here, it's next-bar re-entry (should_reenter true and not same-bar)
            # For safety, still check blocked_reentry
            if self._blocked_reentry(view):
                return closed
            # Flip + reenter → fall through and open the reverse structure (next-bar only).
            # Counting happens in the entry path below via is_reentry check.
        elif view is None:
            # Nothing open and no conviction — nothing to do.
            return None

        if self.strict:
            rule = self._transitional_direction_rule_for(view)
            if rule is None:
                # Silence (NEUTRAL/viewless) or an edge-triggered halt:
                # either way, no trade and no guessing.
                if self.last_block_code is not None:
                    code, self.last_block_code = self.last_block_code, None
                    return {"rejected": True, "reason": code, "code": code}
                return None
            structure_type = rule.structure
            try:
                assert self.risk_gate is not None
                self.risk_gate.check_opening(
                    structure_type,
                    quantity=int(self.expression.get("quantity", 1) or 1),
                    worst_case_loss_inr=(
                        float(self._strict_exit_plan.max_loss_inr)
                        if self._strict_exit_plan.max_loss_inr is not None
                        else None
                    ),
                )
            except Exception as exc:  # gate refusal: coded, halted, no trade
                code = str(getattr(exc, "reason", "") or type(exc).__name__)
                self._journal_halt_once(code, str(exc))
                return {"rejected": True, "reason": str(exc), "code": code}
        else:
            structure_type = self._structure_type_for(view)
        if structure_type not in STRUCTURES:
            logger.warning("[options-bridge] unknown structure type %r", structure_type)
            return None

        if self._blocked_reentry(view):
            logger.debug("[options-bridge] entry suppressed on the exit bar")
            return None

        # U2.2: detect re-entry on next bar (last exit flip + reenter true)
        # and enforce max_reentries_per_day
        is_reentry = False
        try:
            is_reentry = self._should_reenter(view)
        except Exception:
            is_reentry = False
        if is_reentry:
            # Already checked max in _should_reenter, but double-check and count
            try:
                self._reentries_today += 1
            except Exception:
                self._reentries_today = 1

        dte_block = self._entry_dte_block()
        if dte_block is not None:
            self._journal_halt(dte_block.get("reason"), code="dte_block")
            return dte_block

        try:
            return self._execute(view, structure_type, strategy_name)
        except Exception as exc:  # noqa: BLE001 — a bad bar must not kill the runner
            logger.warning("[options-bridge] %s execution failed: %s", structure_type, exc)
            self._journal_halt(exc, code=str(getattr(exc, "reason", "") or type(exc).__name__))
            return {"rejected": True, "reason": str(exc)}

    def pop_exit_event(self) -> Optional[dict[str, Any]]:
        """Drain one exit produced by the per-bar risk rules.

        The runner calls this after :meth:`on_bar`, since a stop/target exit
        happens inside the pricing hook and still needs a signal-log entry.
        """
        return self._exit_events.popleft() if self._exit_events else None

    # ------------------------------------------------------------------ #
    # Intent journal (PRD decision 8)
    # ------------------------------------------------------------------ #

    def _journal(
        self,
        kind: str,
        *,
        intent_id: str,
        structure_id: Optional[str] = None,
        detail: Optional[dict[str, Any]] = None,
        strategy_name: Optional[str] = None,
        structure_type: Optional[str] = None,
        underlying: Optional[str] = None,
    ) -> None:
        """Append one journal event — best-effort, never blocks a decision.

        With no journal wired this is a no-op: the paper path must run the same
        whether or not the durable record is attached, but a *failed* write is
        logged, because a missing event is a reconciliation hole.
        """
        if self.intent_journal is None:
            return
        try:
            self.intent_journal.append(
                intent_id=intent_id,
                kind=kind,
                structure_id=structure_id,
                strategy_name=strategy_name or None,
                structure_type=structure_type,
                underlying=underlying or self.underlying,
                detail=detail,
            )
        except Exception:  # noqa: BLE001 — journaling must not break trading
            logger.warning(
                "[options-bridge] intent journal write failed for %s", kind, exc_info=True
            )

    def _journal_intent(
        self,
        kind: str,
        intent: Any,
        *,
        structure_id: Optional[str] = None,
        detail: Optional[dict[str, Any]] = None,
    ) -> str:
        """Journal an event keyed by the decision itself; return the intent id."""
        intent_id = intent_key(intent, clock=self._bar_dt)
        self._journal(
            kind,
            intent_id=intent_id,
            structure_id=structure_id,
            detail=detail,
            strategy_name=intent.strategy_name,
            structure_type=intent.structure_type,
            underlying=str(intent.view.underlying),
        )
        return intent_id

    def _journal_halt(self, reason: Any, code: str = "") -> None:
        """Journal a pre-trade rejection that never produced an intent."""
        text = str(reason)
        self._journal(
            KIND_HALTED,
            intent_id=decision_key("halt", text, code=code, clock=self._bar_dt),
            detail={"code": code, "reason": text},
        )

    @property
    def bars_in_trade(self) -> int:
        """Bars since the open structure was entered (0 on the entry bar)."""
        if self._entry_bar_index is None:
            return 0
        return self._bar_index - self._entry_bar_index

    # ------------------------------------------------------------------ #
    # Operator-set levels + manual close (Live Order Management)
    # ------------------------------------------------------------------ #
    #
    # The exit policy is the *strategy's* opinion (configured once, in the
    # playbook). These are the *operator's* levels, set from the positions
    # table while the trade is live, and they must win: a human overriding a
    # stop is exactly what the dashboard is for.
    #
    # Both levels are expressed in the structure's **mark** — the signed net
    # premium per unit that ``open_structures_snapshot`` reports as
    # ``current_price``. P&L moves with that mark in the SAME direction for
    # debit and credit structures alike ((mark − entry) × units is the
    # structure's P&L), so "stop is below the mark, target above it" holds
    # without the bridge having to know which kind of structure is open.

    def manual_rules(self) -> dict[str, Optional[float]]:
        """The operator's stop/target for the open structure (``None`` = unset)."""
        return {"stop_loss": self.manual_stop_loss, "target": self.manual_target}

    def set_manual_rule(
        self,
        stop_loss: Any = _UNSET,
        target: Any = _UNSET,
    ) -> dict[str, Optional[float]]:
        """Arm, re-price or clear the operator's levels on the open structure.

        ``_UNSET`` leaves a level untouched; ``None`` clears it. Raises
        ``ValueError`` when nothing is open, when a level is not a positive
        number, or when the level is on the wrong side of the current mark
        (a stop above the mark would fire on the very next bar — that is a
        market exit, and the UI's Close action is the honest way to ask for
        one).
        """
        if not self._has_open_structure():
            raise ValueError("no open option structure to manage")
        mark = self._open_mark()
        # A net-debit structure profits as premium rises (stop below, target
        # above); a net-credit structure is the mirror image.
        stop_below = self._long_premium()
        if stop_loss is not _UNSET:
            self.manual_stop_loss = self._validated_level(
                stop_loss, "stop-loss", mark, must_be_below=stop_below
            )
        if target is not _UNSET:
            self.manual_target = self._validated_level(
                target, "target", mark, must_be_below=not stop_below
            )
        return self.manual_rules()

    def clear_manual_rules(self) -> None:
        """Drop both operator levels (used when the structure closes)."""
        self.manual_stop_loss = None
        self.manual_target = None

    def manual_exit(
        self,
        reason: str = "manual_close",
        strategy_name: str = "",
    ) -> Optional[dict[str, Any]]:
        """Close the open structure now, at the current mark.

        Bypasses the exit policy entirely — this is a human saying "get me
        out", so no rule may veto it. Returns the same event shape as a
        policy close (``{"exited": True, ...}``) or ``None`` when nothing is
        open. The event is queued for :meth:`pop_exit_event` as well, so a
        runner that does not read the return value still logs the exit.
        """
        if not self._has_open_structure():
            return None
        return self._exit_structure(
            ExitDecision(
                reason=str(reason or "manual_close"),
                detail="closed manually from the positions table",
            ),
            self._bar_ts(self._bar_dt),
            strategy_name,
            queue=True,
        )

    def _open_mark(self) -> Optional[float]:
        """Current net premium per unit of the open structure (``None`` if shut)."""
        structure_id = self.open_structure_id
        if structure_id is None:
            return None
        try:
            for row in self.open_structures_snapshot():
                if row.get("structure_id") == structure_id:
                    return float(row.get("current_price") or 0.0)
        except Exception as exc:  # noqa: BLE001 — a snapshot hiccup must not block control
            logger.warning("[options-bridge] mark lookup failed: %s", exc)
        return None

    def _long_premium(self) -> bool:
        """True when a RISING mark is profit, i.e. the structure was a net debit.

        A credit structure (net premium received) is the mirror image: the
        operator's stop sits ABOVE the mark and the target BELOW it, because
        premium rising is what costs money. Guessing this wrong would arm a
        level that fires on the very next bar.
        """
        structure_id = self.open_structure_id
        if structure_id is None:
            return True
        structure = self.option_broker.get_structure(structure_id)
        if structure is None:
            return True
        try:
            return float(structure.total_entry_cost) >= 0.0
        except (TypeError, ValueError):  # pragma: no cover — defensive
            return True

    @staticmethod
    def _validated_level(
        value: Any, label: str, mark: Optional[float], must_be_below: bool
    ) -> Optional[float]:
        """Coerce an operator level, or raise with a message the UI can show."""
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        try:
            level = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{label} must be a number, got {value!r}") from None
        if not (level > 0) or level != level or level in (float("inf"), float("-inf")):
            raise ValueError(f"{label} must be a finite price above 0, got {level}")
        if mark:
            wrong_side = (level >= mark) if must_be_below else (level <= mark)
            if wrong_side:
                relation = "at or above" if must_be_below else "at or below"
                raise ValueError(
                    f"{label} {level:.2f} is {relation} the current mark "
                    f"{mark:.2f} — it would exit immediately; use Close instead"
                )
        return round(level, 4)

    def summary(self) -> dict[str, Any]:
        """Compact book state for runner state payloads and tests."""
        broker = self.option_broker
        closed = broker.get_closed_structures()
        open_structures = broker.get_open_structures()
        wins = sum(1 for s in closed if s.total_realized_pnl >= 0)
        total_pnl = sum((s.total_realized_pnl for s in closed), Decimal("0"))
        stale_positions = sum(
            1
            for s in open_structures
            if self.runner_running and s.is_mark_stale(self.stale_after_seconds)
        )
        return {
            "open_positions": len(broker.get_open_positions()),
            "open_structures": len(open_structures),
            "executed_count": self.executed_count,
            "closed_count": self.closed_count,
            "capital": float(broker.capital),
            "equity": float(broker.total_equity),
            "realized_pnl": float(broker.total_realized_pnl),
            "unrealized_pnl": float(self.last_unrealized_pnl),
            "last_spot": self.last_spot,
            "last_mtm_ts": self.last_mtm_ts,
            "mark_ts": self.mark_ts,
            "mark_age_seconds": self.mark_age_seconds(),
            "stale_after_seconds": self.stale_after_seconds,
            "mark_stale": self.mark_stale,
            "quote_error": self.quote_error,
            "stale_positions": stale_positions,
            "stale_episode_id": self._stale_episode_id if self.mark_stale else 0,
            "last_refresh": self.last_refresh_result,
            "quote_source": str(getattr(self.quote_provider, "source_name", "unknown")),
            "last_exit": dict(self.last_exit) if self.last_exit else None,
            "settled_count": self._settlement_count,
            # -- closed-structure metrics (task B3) -------------------------
            "closed_structures": len(closed),
            "wins": wins,
            "losses": len(closed) - wins,
            "win_rate": (wins / len(closed)) if closed else 0.0,
            "total_pnl": float(total_pnl),
            "avg_pnl": (float(total_pnl) / len(closed)) if closed else 0.0,
            "costs_paid": float(broker.total_costs_paid),
            "exit_policy": self.exit_policy.config.to_dict(),
            # -- open-structure detail (task C2) ----------------------------
            "open_structures_detail": self.open_structures_snapshot(),
            "bars_in_trade": self.bars_in_trade if self.open_structure_id else 0,
        }

    def flatten_all(self, reason: str = "emergency_flatten") -> int:
        """Emergency-close any open structure at current mark. Returns 0 or 1."""
        if self.open_structure_id is None:
            return 0
        decision = ExitDecision(reason=reason, detail="operator emergency flatten")
        result = self._exit_structure(decision, ts=None, strategy_name="flatten", queue=False)
        return 1 if result else 0

    # ------------------------------------------------------------------ #
    # Book metrics (task A2) — the runner folds these into its own numbers
    # ------------------------------------------------------------------ #

    @property
    def mark_ts(self) -> Optional[str]:
        """ISO-8601 UTC timestamp of the last successful mark on the open book (R1)."""
        open_structures = self.option_broker.get_open_structures()
        stamps = [s.mark_ts for s in open_structures if s.mark_ts]
        return min(stamps) if stamps else None

    def _resolve_stale_after_seconds(self, bar_cadence_seconds: Optional[float]) -> float:
        """Freshness SLA width: expression override → one bar cadence + grace.

        The grace is the module default, sized for the 60 s R6 watchdog. Adding
        one bar's *market* width keeps a daily runner from reading stale during
        the overnight gap its own cadence demands, while still halting it a few
        minutes into the session after the next daily bar failed to arrive.
        """
        grace = float(STALE_AFTER_SECONDS)
        default = grace + max(0.0, float(bar_cadence_seconds or 0.0))
        raw = self.expression.get("stale_after_seconds")
        if raw is None:
            return default
        try:
            return max(1.0, float(raw))
        except (TypeError, ValueError):
            logger.warning(
                "[options-bridge] expression has a non-numeric stale_after_seconds "
                "(%r) — using %ss",
                raw,
                int(default),
            )
            return default

    def mark_age_seconds(self, now: Optional[datetime] = None) -> Optional[float]:
        """Market-session seconds since the oldest mark on the open book (§8.1)."""
        ages = [
            age
            for structure in self.option_broker.get_open_structures()
            for age in (structure.mark_age_seconds(now),)
            if age is not None
        ]
        return max(ages) if ages else None

    def _mark_untrusted(self, structure: Any) -> bool:
        """Whether price-driven decisions must not act on this book's marks.

        Either a failed mark (``mtm_failures``) or an SLA-exceeded age: the
        price the stop/target would compare against is not a price.
        """
        return bool(
            structure.mtm_failures > 0
            or structure.is_mark_stale(self.stale_after_seconds)
        )

    def _stale_now(self, now: Optional[datetime] = None) -> bool:
        """Freshness SLA check — the body of :attr:`mark_stale`, clock-injectable."""
        if not self.runner_running:
            return False
        if not _market_is_open(now):
            # Marks are only expected while the session is trading: the R6
            # watchdog goes silent at the close, so a book held overnight is
            # not a stale book — and flagging it would badge the UI and fire a
            # staleness episode every evening (MTM-Staleness PRD AC #9).
            return False
        age = self.mark_age_seconds(now)
        return age is not None and age > self.stale_after_seconds

    @property
    def mark_stale(self) -> bool:
        """True when a RUNNING runner holds a mark older than the freshness SLA.

        Market-session time, not a count of failed marks (§4.1/§8.1): a quote
        feed that stops delivering bars records no failures at all, so counting
        failures never sees the freeze this SLA exists to catch.
        """
        return self._stale_now()

    @property
    def quote_error(self) -> Optional[str]:
        """Last failure reason on any open structure leg, or ``None`` when healthy (R1)."""
        for s in self.option_broker.get_open_structures():
            if s.quote_error:
                return s.quote_error
        return None

    @property
    def net_pnl(self) -> Decimal:
        """Book P&L since inception: realized + unrealized − all costs.

        ``total_equity − capital`` — the exact amount a runner's equity must
        move by when this bridge's book is folded in.
        """
        broker = self.option_broker
        return broker.total_equity - broker.capital

    @property
    def unrealized_pnl(self) -> Decimal:
        """Open legs' mark-to-market P&L (as of the last bar)."""
        return self.option_broker.total_unrealized_pnl

    @property
    def premium_at_risk(self) -> Decimal:
        """Net premium paid for the structures still open.

        For every V1 structure (long call/put, bull call spread, bear put
        spread) the net debit **is** the maximum loss, so this is the capital
        genuinely committed — the option analogue of an equity runner's
        ``deployed_capital``. Credit structures (net premium received) report
        zero here rather than a negative commitment; their risk sits with the
        broker's margin model.
        """
        total = Decimal("0")
        for structure in self.option_broker.get_open_structures():
            debit = structure.total_entry_cost
            if debit > 0:
                total += debit
        return total

    # ------------------------------------------------------------------ #
    # Reporting (task C2) — the book as flat rows for the matrix/deep-dive
    # ------------------------------------------------------------------ #

    def open_structures_snapshot(self) -> list[dict[str, Any]]:
        """Every open structure as a flat, JSON-safe row.

        The portfolio matrix and the deep-dive drawer are equity-shaped: they
        read ``symbol``/``qty``/``entry_price``/``current_price``/
        ``unrealized_pnl``. Rather than teach three views about option legs,
        the legs are collapsed here into one row per structure, with the same
        keys **plus** the option-specific ones (strikes, per-leg detail, next
        expiry, bars held). ``entry_price``/``current_price`` are the signed
        net premium per unit — debit positive, credit negative — so
        ``(current − entry) × units`` reads as the structure's P&L, exactly
        like an equity position.

        The next expiry is the one the **rules** will act on: the structure's
        own expiry (B2 settles it there) or, if that is unknown, the policy's
        nearest expiry as of the bar clock. Never raises: a calendar hiccup
        leaves ``next_expiry`` as ``None`` rather than taking out the caller
        (this runs inside the 1 Hz SSE snapshot).
        """
        rows: list[dict[str, Any]] = []
        for structure in self.option_broker.get_open_structures():
            legs = list(structure.legs)
            units = max((leg.total_quantity for leg in legs), default=0)
            lots = max((leg.quantity for leg in legs), default=0)

            def _net_premium(price_attr: str) -> float:
                """Signed premium per unit: longs pay, shorts receive."""
                if not units:
                    return 0.0
                total = Decimal("0")
                for leg in legs:
                    price = getattr(leg, price_attr, Decimal("0")) or Decimal("0")
                    total += price * Decimal(str(leg.total_quantity)) * (1 if leg.is_long else -1)
                return float(total / Decimal(str(units)))

            leg_rows = []
            net_delta = 0.0
            net_theta = 0.0
            greeks_seen = False
            greeks_iv: Optional[float] = None
            for leg in legs:
                greeks = self._leg_greeks(leg)
                if greeks:
                    greeks_seen = True
                    greeks_iv = greeks.get("iv") if greeks_iv is None else greeks_iv
                    sign = 1.0 if leg.is_long else -1.0
                    net_delta += sign * float(greeks["delta"]) * float(leg.total_quantity)
                    net_theta += sign * float(greeks["theta"]) * float(leg.total_quantity)
                leg_rows.append(
                    {
                        "option_type": leg.option_type,
                        "side": "LONG" if leg.is_long else "SHORT",
                        "strike": float(leg.strike),
                        "trading_symbol": leg.trading_symbol,
                        "qty": leg.total_quantity,
                        "entry_price": float(leg.entry_price),
                        "current_price": float(leg.current_price),
                        "pnl": float(leg.unrealized_pnl),
                        # R1: per-leg mark freshness contract
                        "mark_ts": leg.mark_ts,
                        "mark_stale": bool(
                            self.runner_running and leg.is_mark_stale(self.stale_after_seconds)
                        ),
                        "quote_error": leg.last_quote_error,
                        "mtm_failures": leg.mtm_failures,
                        # Best-effort analytics: None (not 0.0!) when the spot,
                        # expiry or an implied vol for this contract is
                        # unavailable — an invented delta is worse than none.
                        "delta": round(greeks["delta"], 4) if greeks else None,
                        "theta": round(greeks["theta"], 4) if greeks else None,
                        "iv": round(greeks["iv"], 4) if greeks else None,
                    }
                )
            entry = _net_premium("entry_price")
            current = _net_premium("current_price")
            unrealized = float(structure.total_unrealized_pnl)
            structure_stale = bool(
                self.runner_running and structure.is_mark_stale(self.stale_after_seconds)
            )
            rows.append(
                {
                    # Equity-compatible shape (so existing renderers work) …
                    "symbol": f"{structure.underlying} {structure.structure_type}",
                    "label": f"{structure.underlying} {structure.structure_type} "
                    + " ".join(str(leg.strike) for leg in legs),
                    "side": "LONG" if legs and legs[0].is_long else "SHORT",
                    "qty": lots,
                    "units": units,
                    "lot_size": legs[0].lot_size if legs else 0,
                    "entry_price": round(entry, 2),
                    "current_price": round(current, 2),
                    "unrealized_pnl": round(unrealized, 2),
                    "entry_cost": round(entry * units, 2),
                    "open_pnl_pct": (
                        round(unrealized / (entry * units), 4) if entry * units else 0.0
                    ),
                    "entry_ts": structure.opened_at.isoformat() if structure.opened_at else None,
                    # R1: per-structure mark freshness contract
                    "mark_ts": structure.mark_ts,
                    "mark_stale": structure_stale,
                    "quote_error": structure.quote_error,
                    "mtm_failures": structure.mtm_failures,
                    # … plus the option-specific columns (C2).
                    "kind": "option",
                    "structure_id": structure.structure_id,
                    "structure_type": structure.structure_type,
                    "underlying": structure.underlying,
                    "strikes": [float(leg.strike) for leg in legs],
                    "legs": len(legs),
                    "legs_detail": leg_rows,
                    "expiry": self._structure_expiry_iso(structure),
                    "next_expiry": self._structure_expiry_iso(structure),
                    "bars_held": (
                        self._bar_index - self._entry_bar_index
                        if self._entry_bar_index is not None
                        else 0
                    ),
                    # -- Live Order Management (positions table) ------------
                    # The key the dashboard sends back for any manual action
                    # on this row: a structure id is unique for the life of
                    # the structure, unlike a symbol label.
                    "position_key": structure.structure_id,
                    "stop_loss": self._operator_level_for(structure.structure_id, self.manual_stop_loss),
                    "target": self._operator_level_for(structure.structure_id, self.manual_target),
                    # Net Greeks for the whole structure (units-scaled), or
                    # None when they cannot be computed honestly.
                    "net_delta": round(net_delta, 2) if greeks_seen else None,
                    "net_theta": round(net_theta, 2) if greeks_seen else None,
                    "greeks_source": (
                        f"bs:iv={greeks_iv:.2f}" if greeks_seen and greeks_iv else None
                    ),
                }
            )
        return rows

    def _structure_expiry_iso(self, structure: Any) -> Optional[str]:
        """The structure's expiry as an ISO date; nearest policy expiry as a
        fallback, and ``None`` if the calendar cannot be read."""
        expiry = getattr(structure, "expiry", None) or self._structure_expiry
        if expiry is None:
            try:
                expiry = self._select_expiry(self._generator(), self.underlying)
            except Exception:  # noqa: BLE001 — reporting must never break the feed
                return None
        if expiry is None:
            return None
        return expiry.isoformat() if hasattr(expiry, "isoformat") else str(expiry)

    # ------------------------------------------------------------------ #
    # Per-bar pricing (task A1)
    # ------------------------------------------------------------------ #

    def on_bar(self, symbol: str, price: float, ts: Any = None) -> Optional[Decimal]:
        """Mark the option book to market at one bar close.

        Called by :class:`~backtest.forward.paper_runner.StrategyRunner` for
        every closed bar (single **and** pool runners). Three things happen:

        1. the synthetic spot moves to ``price`` so premiums track the
           underlying the strategy just saw;
        2. the quote provider's pricing reference is pinned to ``ts`` — the
           bar's timestamp, not ``datetime.now()`` — so time decay advances at
           replay speed;
        3. every open leg is re-priced and its unrealized P&L recomputed;
        4. the **mechanical** exit rules (stop, target, days-to-expiry, time
           stop) are evaluated against the fresh mark. Signal rules — flip and
           neutral — need the strategy's view and are evaluated by
           :meth:`on_market_view` instead, so a viewless bar cannot close a
           position on a view it never saw. A close here is queued for
           :meth:`pop_exit_event`.

        Returns the book's total unrealized P&L (post-action), or ``None`` when
        there is nothing open (or the book is empty). Pricing failures are
        logged and swallowed: a bad bar must never kill the runner.
        """
        self._bar_index += 1
        bar_dt = self._bar_datetime(ts)
        if bar_dt is not None or self._bar_dt is None:
            self._bar_dt = bar_dt
        # U2.2: reset re-entries per day when day changes
        try:
            current_day = self._bar_dt.date() if self._bar_dt else None
            if current_day and self._reentry_day != current_day:
                self._reentries_today = 0
                self._reentry_day = current_day
        except Exception:
            pass
        if not self._has_open_structure():
            return None

        try:
            underlying = str(symbol or self.underlying or "NIFTY").upper()
            self.underlying = underlying
            self._sync_market(underlying, price, ts)
            # Restart recovery (2026-09-23): a restored structure's legs carry
            # REAL trading symbols (e.g. NIFTY26SEP23300CE) but the fresh
            # process's quote provider may not know their tokens — quotes
            # returned ltp 0 and legs froze at 0.00. Re-register the open
            # book's contracts (once per restore) so token→contract lookups
            # resolve again; unknown tokens fall back to the trading symbol.
            self._rebind_restored_contracts()
            unrealized = self.option_broker.update_mtm(self.quote_provider, self._bar_datetime(ts))
            self.last_unrealized_pnl = Decimal(str(unrealized))
            self.last_spot = float(price)
            self.last_mtm_ts = str(ts) if ts else None
            self._after_mtm_attempt(symbol=underlying)
        except Exception as exc:  # noqa: BLE001 — pricing must never kill the runner
            self._log_mtm_failure_throttled(
                str(symbol or self.underlying or "NIFTY").upper(),
                f"MTM failed for {symbol} @ {price}: {exc}",
            )
            self._update_stale_episode_state()
            return None

        # Operator levels first: a human's stop beats every configured rule,
        # and it must be evaluated against this bar's fresh mark (that is the
        # only reason a manual stop can be trusted — it never waits for a
        # strategy view that may never come).
        self._maybe_manual_exit()
        self._maybe_risk_exit(strategy_name="")
        self._maybe_expiry_settlement()
        self._update_stale_episode_state()
        return self.last_unrealized_pnl

    # ------------------------------------------------------------------ #
    # Expiry settlement (task B2)
    # ------------------------------------------------------------------ #

    def _expiry(self) -> ExpiryManager:
        """The canonical expiry pipeline, bound to this bridge's own book.

        Reused rather than reimplemented so the forward loop settles exactly
        like the backtest driver and the dashboard: cash settlement at the
        spot settlement price, ``expiry_settlement`` stamped on the structure,
        and the broker's close observers fired (persistence).
        """
        if self._expiry_manager is None:
            self._expiry_manager = ExpiryManager(
                self.option_broker,
                squareoff_minutes_before=self.squareoff_minutes_before,
            )
        return self._expiry_manager

    def _maybe_expiry_settlement(self) -> Optional[dict[str, Any]]:
        """Cash-settle a structure whose expiry the bar clock has reached.

        Runs on every bar after the mark-to-market, and settles **on** the
        expiry bar (``include_today=True``): index options settle on the
        expiry-day close, which is exactly the price the bar carries. Only
        relevant when the exit policy rides into settlement
        (``min_days_to_expiry: null``) — otherwise the DTE rule has already
        squared the position off the day before.
        """
        if self._bar_dt is None or not self._has_open_structure():
            return None
        structure = self.option_broker.get_structure(self.open_structure_id or "")
        if structure is None or structure.expiry is None:
            return None

        try:
            report = self._expiry().process_expiries(
                self.quote_provider,
                _GeneratorSettlementProvider(self._generator(), self.underlying),
                as_of=self._bar_dt,
                include_today=True,
            )
        except Exception as exc:  # noqa: BLE001 — a settlement failure must not kill the runner
            logger.warning("[options-bridge] expiry settlement failed: %s", exc)
            return None

        if not report.get("settled_count") and not report.get("squared_off_count"):
            return None
        return self._record_settlement(structure, report)

    def _record_settlement(
        self, structure: Any, report: dict[str, Any]
    ) -> Optional[dict[str, Any]]:
        """Turn a settlement report into the same event shape as a close."""
        settled = bool(report.get("settled_count"))
        realized = structure.total_realized_pnl
        event = {
            "exited": True,
            "structure_id": structure.structure_id,
            "structure_type": structure.structure_type,
            "underlying": structure.underlying,
            "direction": _label(self._structure_direction),
            "reason": EXIT_REASON_SETTLEMENT if settled else EXIT_DTE,
            "detail": (
                f"cash settled at expiry {structure.expiry}"
                if settled
                else "squared off into expiry"
            ),
            "pnl": float(realized),
            "bars_held": self.bars_in_trade,
            "strategy_name": "",
            "settled": settled,
        }

        self.open_structure_id = None
        self.closed_count += 1
        self._settlement_count += 1
        self.last_exit = event
        self._exit_bar_index = self._bar_index
        self._entry_bar_index = None
        self._bars_without_view = 0
        self._structure_direction = None
        self._structure_expiry = None
        self._entry_premium = Decimal("0")
        # Live Order Management: the operator's levels belong to the structure
        # that just left the book. Keeping them would arm the NEXT trade with
        # a stop nobody asked for.
        self.clear_manual_rules()
        self.last_unrealized_pnl = self.option_broker.total_unrealized_pnl
        self._exit_events.append(event)

        logger.info(
            "[options-bridge] %s %s — pnl ₹%.2f (%d settled, %d squared off)",
            "settled" if settled else "squared off",
            structure.structure_id[:8],
            float(realized),
            report.get("settled_count", 0),
            report.get("squared_off_count", 0),
        )
        return event

    def _maybe_manual_exit(self) -> Optional[dict[str, Any]]:
        """Fire an operator-set stop/target against the freshly marked book.

        Levels are premium-per-unit marks (see :meth:`set_manual_rule`), so
        the comparison is a plain below/above test on the structure's mark.
        Nothing open → nothing to do; both levels unset → no bookkeeping.
        """
        if self.manual_stop_loss is None and self.manual_target is None:
            return None
        structure = self.option_broker.get_structure(self.open_structure_id or "")
        if structure is not None and self._mark_untrusted(structure):
            self._log_mtm_failure_throttled(
                f"{self.underlying}:manual_exit",
                f"manual stop/target suppressed on {self.underlying} while mark is stale/failed",
            )
            return None
        mark = self._open_mark()
        if mark is None:
            return None
        level, reason, label = None, None, ""
        long_premium = self._long_premium()
        stop = self.manual_stop_loss
        target = self.manual_target
        if stop is not None and (
            (mark <= float(stop)) if long_premium else (mark >= float(stop))
        ):
            level, reason, label = stop, EXIT_MANUAL_STOP, "manual stop-loss"
        elif target is not None and (
            (mark >= float(target)) if long_premium else (mark <= float(target))
        ):
            level, reason, label = target, EXIT_MANUAL_TARGET, "manual target"
        if reason is None:
            return None
        return self._exit_structure(
            ExitDecision(
                reason=reason,
                detail=f"{label} {float(level):.2f} hit (mark {mark:.2f})",
            ),
            self._bar_ts(self._bar_dt),
            strategy_name="",
            queue=True,
        )

    def _maybe_risk_exit(self, strategy_name: str) -> Optional[dict[str, Any]]:
        """Apply the view-independent exit rules to the freshly marked book."""
        decision = self._evaluate_exit(view=None)
        if decision is None or decision.reason not in RISK_REASONS:
            return None
        return self._exit_structure(decision, self._bar_ts(self._bar_dt), strategy_name, queue=True)

    _restored_contracts_rebound: bool = False

    def _rebind_restored_contracts(self, force: bool = False) -> bool:
        """Register restored open structures with the quote provider (R3/R5).

        The registry lives on the provider (token → contract); a restarted
        process builds a fresh provider with an EMPTY registry, so restored
        legs' real mStock tokens looked up nothing and MTM'd to 0.

        R3 fail-loud rule: if the provider lacks ``register_contract``, log
        **ERROR** ("restored legs cannot rebind — <provider> lacks
        register_contract") instead of silently completing.
        """
        open_structures = self.option_broker.get_open_structures()
        if not open_structures:
            return False
        if getattr(self, "_restored_contracts_rebound", False) and not force:
            return True
        register = getattr(self.quote_provider, "register_contract", None)
        if not callable(register):
            logger.error(
                "[options-bridge] restored legs cannot rebind — %s lacks register_contract",
                type(self.quote_provider).__name__,
            )
            self._restored_contracts_rebound = True
            return False
        try:
            from backtest.instruments.option import OptionContract
            from backtest.instruments.base import ExerciseType, SettlementType

            for structure in open_structures:
                expiry = getattr(structure, "expiry", None) or date.today()
                for leg in structure.legs:
                    token = str(leg.instrument_token or leg.trading_symbol)
                    symbol = str(leg.trading_symbol or token)
                    contract = OptionContract(
                        instrument_token=token,
                        trading_symbol=symbol,
                        underlying=str(getattr(leg, "underlying", "") or structure.underlying),
                        exchange="NSE",
                        segment="NFO",
                        expiry=expiry,
                        strike=Decimal(str(getattr(leg, "strike", 0) or 0)),
                        option_type=str(getattr(leg, "option_type", "CE") or "CE"),
                        lot_size=int(getattr(leg, "lot_size", 1) or 1),
                        tick_size=Decimal("0.05"),
                        contract_type=ExerciseType.EUROPEAN,
                        settlement_type=SettlementType.CASH,
                    )
                    register(contract)
                    # Also register under trading_symbol if distinct from token
                    # so direct symbol re-quotes resolve on every provider type.
                    if symbol and symbol != token:
                        sym_contract = OptionContract(
                            instrument_token=symbol,
                            trading_symbol=symbol,
                            underlying=contract.underlying,
                            exchange="NSE",
                            segment="NFO",
                            expiry=expiry,
                            strike=contract.strike,
                            option_type=contract.option_type,
                            lot_size=contract.lot_size,
                            tick_size=Decimal("0.05"),
                            contract_type=ExerciseType.EUROPEAN,
                            settlement_type=SettlementType.CASH,
                        )
                        register(sym_contract)
            self._restored_contracts_rebound = True
            return True
        except Exception:  # noqa: BLE001 — recovery must never kill the bar
            logger.warning("[options-bridge] restored-contract rebind failed", exc_info=True)
            return False

    # ------------------------------------------------------------------ #
    # MTM Staleness Observability & Watchdog Repair (R4, R6)
    # ------------------------------------------------------------------ #

    def _log_mtm_failure_throttled(self, key: str, message: str) -> bool:
        """Rate-limited WARNING log: first failure immediately, then at most
        once per ``warn_throttle_seconds`` (10 min default) while it persists (R4).
        """
        now_mono = time.monotonic()
        last = self._last_failure_warn_ts.get(key)
        if last is None or (now_mono - last) >= self.warn_throttle_seconds:
            self._last_failure_warn_ts[key] = now_mono
            logger.warning("[options-bridge] %s", message)
            return True
        return False

    def _after_mtm_attempt(self, symbol: str) -> None:
        """Post-MTM hook: throttled failure logging + episode state update."""
        open_legs = self.option_broker.get_open_positions()
        failed_legs = [leg for leg in open_legs if leg.mtm_failures > 0]
        key = str(symbol or self.underlying or "NIFTY").upper()
        if failed_legs:
            leg_desc = ", ".join(
                f"{leg.trading_symbol or leg.instrument_token} "
                f"(failures={leg.mtm_failures}, err={leg.last_quote_error})"
                for leg in failed_legs
            )
            self._log_mtm_failure_throttled(
                key,
                f"MTM quote failed for {key} [{leg_desc}]",
            )
        else:
            self._last_failure_warn_ts.pop(key, None)
        self._update_stale_episode_state()

    def _update_stale_episode_state(self) -> bool:
        """Track healthy → stale transitions and fire single-shot notification (R6/AC #8).

        Returns True iff this call transitioned from healthy to stale.
        """
        now_stale = self.mark_stale
        subject = f"mtm:{self.runner_id or 'bridge'}"
        if now_stale and not self._in_stale_episode:
            self._in_stale_episode = True
            self._stale_episode_id += 1
            err = self.quote_error or "quote failed"
            ts_now = datetime.now(timezone.utc).isoformat()
            runner_label = self.runner_name or self.runner_id or self.underlying
            notice = {
                "episode_id": self._stale_episode_id,
                "ts": ts_now,
                "runner_id": self.runner_id,
                "runner": runner_label,
                "underlying": self.underlying,
                "mark_ts": self.mark_ts,
                "quote_error": err,
                "message": f"Stale option mark on {runner_label} ({self.underlying}): {err}",
            }
            self.stale_notifications.append(notice)
            try:
                from backtest.alerts.broker import get_alert_broker
                from backtest.alerts.types import AlertType

                get_alert_broker().raise_alert(
                    AlertType.DATA_FEED_STALE.value,
                    "warning",
                    notice["message"],
                    subject=subject,
                    data=notice,
                )
            except Exception:  # noqa: BLE001 — alerting never blocks trading
                logger.debug("[options-bridge] stale mark alert raise failed", exc_info=True)
            return True
        if not now_stale and self._in_stale_episode:
            self._in_stale_episode = False
            try:
                from backtest.alerts.broker import get_alert_broker
                from backtest.alerts.types import AlertType

                get_alert_broker().resolve_key(
                    AlertType.DATA_FEED_STALE.value,
                    subject,
                    reason="option mark refreshed",
                )
            except Exception:  # noqa: BLE001
                logger.debug("[options-bridge] stale mark alert resolve failed", exc_info=True)
        return False

    def repair_marks(
        self,
        *,
        force: bool = False,
        now_utc: Optional[datetime] = None,
        max_age_s: float = 120.0,
    ) -> dict[str, Any]:
        """Watchdog / manual mark-repair routine (R6).

        1. Re-binds every open leg via its stored ``trading_symbol`` (and
           invalidates any provider quote cache).
        2. Re-quotes each open structure atomically, falling back to direct
           ``get_quote(leg.trading_symbol)`` if ``leg.instrument_token`` still
           fails (bypassing a corrupted token→contract registry).
        3. If still failing on a stale-age leg, ensures ``mark_stale`` is set,
           keeps ``quote_error``, logs once (throttled), and notifies once per
           staleness episode.
        """
        now_dt = now_utc or datetime.now(timezone.utc)
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=timezone.utc)
        else:
            now_dt = now_dt.astimezone(timezone.utc)

        open_structures = self.option_broker.get_open_structures()
        open_legs = [
            leg
            for s in open_structures
            for leg in s.legs
            if leg.status == PositionStatus.OPEN
        ]
        if not open_legs:
            self._update_stale_episode_state()
            res = {
                "checked": 0,
                "repaired": 0,
                "still_failing": 0,
                "mark_stale": False,
                "mark_ts": None,
                "quote_error": None,
                "refreshed_at": now_dt.isoformat(),
                "message": "no open option positions",
            }
            self.last_refresh_result = res
            return res

        # Determine which legs need repair (all when force=True, else age > max_age_s or failing)
        needs_repair = force
        if not needs_repair:
            for leg in open_legs:
                lu = leg.last_updated
                if lu is None:
                    needs_repair = True
                    break
                lu_utc = (
                    lu.replace(tzinfo=timezone.utc)
                    if lu.tzinfo is None
                    else lu.astimezone(timezone.utc)
                )
                age_s = (now_dt - lu_utc).total_seconds()
                if age_s > max_age_s or leg.mtm_failures > 0:
                    needs_repair = True
                    break

        if not needs_repair:
            res = {
                "checked": 0,
                "repaired": 0,
                "still_failing": 0,
                "mark_stale": self.mark_stale,
                "mark_ts": self.mark_ts,
                "quote_error": self.quote_error,
                "refreshed_at": now_dt.isoformat(),
                "message": "marks are fresh",
            }
            return res

        # Step 1: re-bind legs via stored trading_symbol & invalidate caches
        self._rebind_restored_contracts(force=True)
        invalidate = getattr(self.quote_provider, "invalidate", None)
        if callable(invalidate):
            for leg in open_legs:
                invalidate(leg.instrument_token)
                if leg.trading_symbol:
                    invalidate(leg.trading_symbol)

        # Step 2: re-quote legs (with direct trading_symbol fallback if token lookup fails)
        repaired_count = 0
        failing_count = 0
        for structure in open_structures:
            s_legs = [leg for leg in structure.legs if leg.status == PositionStatus.OPEN]
            outcomes: list[tuple[Any, bool, Decimal, Optional[str]]] = []
            for leg in s_legs:
                quote: Any = None
                err_msg: Optional[str] = None
                try:
                    quote = self.quote_provider.get_quote(leg.instrument_token)
                except Exception as exc:  # noqa: BLE001
                    err_msg = str(exc)
                    quote = None

                is_fail = (
                    err_msg is not None
                    or not isinstance(quote, dict)
                    or not quote
                    or bool(quote.get("error"))
                )
                if is_fail and err_msg is None:
                    err_msg = (
                        str(quote.get("error"))
                        if isinstance(quote, dict) and quote.get("error")
                        else "empty quote row"
                    )

                # R6 step 1b: bypass token→contract registry by querying trading_symbol directly
                if (
                    is_fail
                    and leg.trading_symbol
                    and str(leg.trading_symbol) != str(leg.instrument_token)
                ):
                    try:
                        sym_quote = self.quote_provider.get_quote(str(leg.trading_symbol))
                        if (
                            isinstance(sym_quote, dict)
                            and sym_quote
                            and not sym_quote.get("error")
                        ):
                            quote = sym_quote
                            is_fail = False
                            err_msg = None
                    except Exception as exc:  # noqa: BLE001
                        err_msg = str(exc)

                price = (
                    Decimal(str(quote.get("ltp", 0) or 0))
                    if isinstance(quote, dict) and not is_fail
                    else Decimal("0")
                )
                outcomes.append((leg, is_fail, price, err_msg))

            if any(is_f for _, is_f, _, _ in outcomes):
                for leg, is_f, _, err_msg in outcomes:
                    if is_f:
                        failing_count += 1
                        leg.record_mtm_failure(err_msg)
                        # Step 3: the failure is surfaced now (``quote_error``,
                        # ``mtm_failures``, throttled log) and the SLA flags the
                        # mark stale once the last good price is older than
                        # ``stale_after_seconds`` — the watchdog runs every 60 s,
                        # so the book never hides a dead feed for a bar cycle.
                    else:
                        leg.last_quote_error = None
            else:
                for leg, _, price, _ in outcomes:
                    leg.update_mtm(price)
                    repaired_count += 1

        self.last_unrealized_pnl = self.option_broker.total_unrealized_pnl
        self._after_mtm_attempt(symbol=self.underlying)

        time_label = now_dt.astimezone().strftime("%H:%M:%S")
        if failing_count > 0:
            msg = f"refreshed {time_label} — still failing: {self.quote_error or 'quote failed'}"
        else:
            msg = f"refreshed {time_label} — {repaired_count} leg(s) updated"

        res = {
            "checked": len(open_legs),
            "repaired": repaired_count,
            "still_failing": failing_count,
            "mark_stale": self.mark_stale,
            "mark_ts": self.mark_ts,
            "quote_error": self.quote_error,
            "refreshed_at": now_dt.isoformat(),
            "message": msg,
        }
        self.last_refresh_result = res
        return res

    def _sync_market(self, underlying: str, spot: float, ts: Any = None) -> None:
        """Point the quote feed at a new spot / bar clock.

        ``set_reference`` exists on :class:`SyntheticQuoteProvider` (and wraps
        through ``CachedQuoteProvider.inner``); live providers have no clock
        to pin, so the call is skipped for them.
        """
        generator = self._generator()
        generator.set_spot(underlying, float(spot))
        provider = self.quote_provider
        set_reference = getattr(provider, "set_reference", None)
        if set_reference is None:
            set_reference = getattr(getattr(provider, "inner", None), "set_reference", None)
        if set_reference is not None:
            set_reference(self._bar_datetime(ts))

    @staticmethod
    def _bar_ts(bar_dt: Optional[datetime]) -> Optional[str]:
        """ISO timestamp for the bar clock (``None`` → provider wall clock)."""
        return bar_dt.isoformat() if bar_dt is not None else None

    @staticmethod
    def _bar_datetime(ts: Any) -> Optional[datetime]:
        """Parse a bar timestamp into a **naive UTC** datetime.

        Naive because ``SyntheticChainGenerator.price_contract`` builds its
        expiry as ``datetime.combine(expiry_date, midnight)`` — mixing a
        tz-aware reference with that would raise. ``None`` (unparseable or
        missing) tells the provider to fall back to the wall clock.
        """
        if not ts:
            return None
        text = str(ts).strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            logger.debug("[options-bridge] unparseable bar timestamp %r", ts)
            return None
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _has_open_structure(self) -> bool:
        if self.open_structure_id is None:
            return False
        structure = self.option_broker.get_structure(self.open_structure_id)
        if structure is None or not structure.is_open:
            self.open_structure_id = None
            return False
        return True

    # ------------------------------------------------------------------ #
    # Exits (task B1)
    # ------------------------------------------------------------------ #

    def _manage_open_structure(
        self, view: MarketView | None, strategy_name: str
    ) -> dict[str, Any] | None:
        """Evaluate the exit policy for the open structure and act on it.

        Returns the exit event when a structure was closed, else ``None``.
        Also maintains the neutral-bar counter (bars without a directional
        view), which the neutral rule needs.
        """
        if view is None or self._is_neutral_view(view):
            self._bars_without_view += 1
        else:
            self._bars_without_view = 0

        decision = self._evaluate_exit(view)
        if decision is None:
            return None
        # queue=False: this caller hands the event straight back to the runner,
        # so it must not also sit in the per-bar drain queue.
        return self._exit_structure(
            decision, self._bar_ts(self._bar_dt), strategy_name, queue=False
        )

    def _evaluate_exit(self, view: MarketView | None) -> Any:
        """Ask the policy; `None` means keep holding."""
        structure = self.option_broker.get_structure(self.open_structure_id or "")
        if structure is None:
            return None
        try:
            decision = self.exit_policy.evaluate(
                view=view,
                structure_direction=self._structure_direction,
                unrealized_pnl=structure.total_unrealized_pnl,
                basis=self._entry_premium,
                bars_held=self.bars_in_trade,
                bars_without_view=self._bars_without_view,
                bar_date=self._bar_dt.date() if self._bar_dt else None,
                expiry=structure.expiry,
            )
            if (
                decision is not None
                and decision.reason in PRICE_MEASURED_REASONS
                and self._mark_untrusted(structure)
            ):
                self._log_mtm_failure_throttled(
                    f"{self.underlying}:risk_exit",
                    f"price-driven {decision.reason} suppressed on {self.underlying} "
                    "while mark is stale/failed",
                )
                return None
            return decision
        except Exception as exc:  # noqa: BLE001 — a bad rule must not block trading
            logger.warning("[options-bridge] exit evaluation failed: %s", exc)
            return None

    def _exit_structure(
        self,
        decision: Any,
        ts: Optional[str],
        strategy_name: str,
        queue: bool = True,
    ) -> Optional[dict[str, Any]]:
        """Close the open structure at the current mark; record the event.

        ``queue=True`` (the per-bar risk path) leaves the event in
        :attr:`_exit_events` for :meth:`pop_exit_event`; ``queue=False`` (a
        caller that returns the event itself) does not.
        """
        structure_id = self.open_structure_id
        if structure_id is None:
            return None
        structure = self.option_broker.get_structure(structure_id)
        if structure is None:
            self.open_structure_id = None
            return None

        bars_held = self.bars_in_trade
        try:
            realized = self.option_broker.close_structure(
                structure_id,
                self.quote_provider,
                timestamp=self._bar_datetime(ts),
                reason=decision.reason,
            )
        except Exception as exc:  # noqa: BLE001 — a failed close must not kill the runner
            logger.warning("[options-bridge] close failed for %s: %s", structure_id[:8], exc)
            self._journal(
                KIND_HALTED,
                intent_id=decision_key(
                    "halt", f"close {structure_id}", code="close_failed", clock=self._bar_dt
                ),
                structure_id=structure_id,
                detail={"code": "close_failed", "reason": str(exc)},
                structure_type=structure.structure_type,
                underlying=structure.underlying,
            )
            return None

        self._journal(
            KIND_EXECUTED,
            intent_id=decision_key(
                "close", structure_id, code=decision.reason, clock=self._bar_dt
            ),
            structure_id=structure_id,
            detail={
                "simulated": True,
                "action": "close",
                "reason": decision.reason,
                "pnl": float(realized),
                "bars_held": bars_held,
            },
            strategy_name=strategy_name,
            structure_type=structure.structure_type,
            underlying=structure.underlying,
        )

        event = {
            "exited": True,
            "structure_id": structure_id,
            "structure_type": structure.structure_type,
            "underlying": structure.underlying,
            "direction": _label(self._structure_direction),
            "reason": decision.reason,
            "detail": decision.detail,
            "pnl": float(realized),
            "bars_held": bars_held,
            "strategy_name": strategy_name,
        }
        self.open_structure_id = None
        self.closed_count += 1
        self.last_exit = event
        self._exit_bar_index = self._bar_index
        self._entry_bar_index = None
        self._bars_without_view = 0
        self._structure_direction = None
        self._structure_expiry = None
        self._entry_premium = Decimal("0")
        # Live Order Management: the operator's levels belong to the structure
        # that just left the book. Keeping them would arm the NEXT trade with
        # a stop nobody asked for.
        self.clear_manual_rules()
        if queue:
            self._exit_events.append(event)
        # The legs are closed, so the open-leg mark is gone. Keep the summary
        # honest for the rest of this bar.
        self.last_unrealized_pnl = self.option_broker.total_unrealized_pnl
        self._update_stale_episode_state()

        logger.info(
            "[options-bridge] closed %s (%s) after %d bars — pnl ₹%.2f — %s",
            structure.structure_type,
            structure_id[:8],
            bars_held,
            float(realized),
            decision.detail or decision.reason,
        )
        return event

    def _select_expiry(self, generator: Any, underlying: str) -> Optional[Any]:
        """Nearest expiry **as of the bar clock** (falls back to today).

        Replay-anchored selection is what makes days-to-expiry rules and the
        expiry calendar mean anything in a forward test: on a historical bar
        the relevant expiry is the one then-current, and once the replay clock
        rolls past it the next month's expiry takes over automatically.
        """
        reference = self._bar_dt.date() if self._bar_dt is not None else None
        try:
            expiries = generator.available_expiries(underlying, reference=reference)
        except TypeError:
            # Custom generators may not accept ``reference`` — keep working.
            expiries = generator.available_expiries(underlying)
            reference = None
        # expiry_min_days (2026-09-24): skip expiries too close to the bar clock
        # so an entry on expiry day does not get settled one bar after it opens.
        # A calendar that cannot satisfy the knob is a halt, not a licence to
        # trade the too-near expiry anyway (PRD §4.2 — no substitution).
        floor_days = (
            int(self._active_rule.expiry_min_days)
            if (self.strict and self._active_rule is not None)
            else self.expiry_min_days
        )
        if floor_days and reference is not None:
            cutoff = reference + timedelta(days=floor_days)
            filtered = [e for e in expiries if e >= cutoff]
            if not filtered:
                raise ContractValidityError(
                    REASON_EXPIRY_TOO_NEAR,
                    f"{underlying}: no expiry at least {floor_days} days out "
                    f"on {reference} (calendar offers {expiries})",
                )
            expiries = filtered
        if self.strict and self._active_rule is not None:
            rule = self._active_rule
            kwargs: dict[str, Any] = {}
            if rule.expiry_policy == "fixed_days":
                kwargs["target_days"] = int(rule.expiry_min_days or 30)
            elif rule.expiry_policy == "minimum_days":
                kwargs["min_days"] = int(rule.expiry_min_days)
            policy = create_expiry_policy(rule.expiry_policy, **kwargs)
            return policy.select_expiry(expiries, reference_date=reference)
        return NearestExpiryPolicy().select_expiry(expiries, reference_date=reference)

    def _entry_dte_block(self) -> Optional[dict[str, Any]]:
        """Refuse to open a structure the expiry rule would immediately close.

        If the policy squares off within ``min_days_to_expiry`` days of expiry
        (or the expiry has already passed on the replay clock), opening a fresh
        structure there would be a one-bar trade — so say so instead.
        """
        min_dte = self.exit_policy.config.min_days_to_expiry
        if min_dte is None or self._bar_dt is None:
            return None
        generator = self._generator()
        try:
            expiry = self._select_expiry(generator, self.underlying)
        except ContractValidityError as exc:
            # A calendar that cannot satisfy expiry_min_days halts the entry —
            # reporting would otherwise swallow it and the trade would go out on
            # a contract the exit rule closes on the next bar.
            return {"rejected": True, "reason": str(exc)}
        except Exception:  # noqa: BLE001 — never block on a calendar hiccup
            return None
        if expiry is None:
            return None
        days = (expiry - self._bar_dt.date()).days
        if days > min_dte:
            return None
        return {
            "rejected": True,
            "reason": f"{days}d to expiry {expiry} ≤ {min_dte}d — new entries paused",
        }

    def _should_reenter(self, view: MarketView | None) -> bool:
        """Flip-close + ``reenter`` → open reverse NEXT bar only (never same-bar) — U2.2."""
        if view is None or self.last_exit is None:
            return False
        if self.last_exit.get("reason") != EXIT_SIGNAL_FLIP:
            return False
        # Next-bar only: same-bar re-entry impossible even if reenter=true
        if self._exit_bar_index is not None and self._exit_bar_index == self._bar_index:
            return False
        max_per_day = self.exit_policy.config.max_reentries_per_day
        if hasattr(self, "_reentries_today") and self._reentries_today >= max_per_day:
            return False
        return self.exit_policy.should_reenter(self.last_exit.get("direction"), view)

    def _blocked_reentry(self, view: MarketView | None = None) -> bool:
        """True on the bar a rule closed a structure — no same-bar re-entry.

        Without this, a stop-out would immediately re-open the same structure
        while the strategy is still bullish, which reads as "the stop did
        nothing". ``reenter`` only bypasses it for a genuine flip.
        """
        if self._exit_bar_index is None:
            return False
        # U2.2: same-bar re-entry impossible, ever — even flip+reenter must wait to next bar
        return self._exit_bar_index == self._bar_index

    # Phase 0 reserved names (Refactor Plan v2.1 §4.1/§6.2): Phase 1 adds
    # ``_transitional_direction_rule_for(view)`` here as the legacy-only
    # strict-mode lookup — deprecated at birth, deleted in Phase 4; the
    # end-state owner of direction->rule resolution is
    # ``TemplateBase.direction_rule_for`` (strategy/templates). The manual
    # level helper ``_rule_for`` below is renamed ``_operator_level_for``
    # in the same phase. Do not introduce other ``_rule_for`` variants.
    def _journal_halt_once(self, code: str, reason: str) -> bool:
        """Edge-triggered halt (plan §6.2): journal once per code per bridge.

        Returns True the first time a code fires; subsequent bars are
        silent so a standing refusal cannot spam the journal/logs.
        """
        if code in self._halted_codes:
            return False
        self._halted_codes.add(code)
        self.last_block_code = code
        self._journal_halt(reason, code=code)
        logger.warning("[options-bridge] OPTION_BLOCKED %s: %s", code, reason)
        return True

    def _transitional_direction_rule_for(self, view: MarketView) -> Optional[Any]:
        """TRANSITIONAL strict-mode direction lookup (plan §4.1/§6.2).

        Legacy-expression strategies only. Template-derived strategies
        resolve direction→rule in ``TemplateBase.direction_rule_for``
        (strategy side) and never call this. Deprecated at birth; deleted
        in Phase 4 with the last legacy caller. No cross-direction
        fallback exists here: an unmapped non-neutral direction is a
        coded, edge-triggered halt; NEUTRAL is silence and never opens.
        """
        if view is None or view.direction is Direction.NEUTRAL:
            return None
        rule = self._strict_rules.get(view.direction)
        if rule is None:
            code = "NO_DIRECTION_MAPPING"
            if self._journal_halt_once(
                code,
                f"{view.direction.name} has no structure rule in the v2 "
                "expression; refusing instead of falling back",
            ):
                pass
            return None
        self._active_rule = rule
        return rule

    def _structure_type_for(self, view: MarketView) -> str:
        type_cfg = self.expression.get("type", DEFAULT_EXPRESSION["type"])
        if isinstance(type_cfg, dict):
            if view.direction == Direction.BEARISH:
                return str(type_cfg.get("BEARISH", "bear_put_spread"))
            elif view.direction == Direction.NEUTRAL:
                # NEUTRAL direction gets its own mapping (e.g., long_call for ATM instant buy)
                return str(type_cfg.get("NEUTRAL", type_cfg.get("BULLISH", "bull_call_spread")))
            return str(type_cfg.get("BULLISH", "bull_call_spread"))
        return str(type_cfg)

    # ------------------------------------------------------------------ #
    # Position analytics (Greeks) — Live Order Management
    # ------------------------------------------------------------------ #

    def _operator_level_for(self, structure_id: str, level: Optional[float]) -> Optional[float]:
        """Report an operator level only against the structure it belongs to."""
        if level is None or self.open_structure_id != structure_id:
            return None
        return float(level)

    def _leg_greeks(self, leg: Any) -> Optional[dict[str, float]]:
        """Black-Scholes Greeks for one leg, or ``None`` when they can't be honest.

        Inputs: spot (last bar close), the leg's strike/expiry/option type and
        an implied vol. The vol comes from the chain contract's own metadata
        (the synthetic generator stamps ``vol`` on every contract it builds)
        or the generator's per-underlying default — deliberately NOT from a
        fresh quote lookup, because this runs inside the 1 Hz snapshot and a
        live provider would burn an API call per leg per second for a display
        column. No vol → no Greeks → the UI shows "—".
        """
        spot = self.last_spot
        expiry = getattr(leg, "expiry", None) or self._structure_expiry
        vol = self._leg_vol(leg)
        if not spot or not expiry or not vol:
            return None
        reference = self._bar_dt.date() if self._bar_dt else datetime.now(timezone.utc).date()
        try:
            years = (expiry - reference).days / 365.0
        except TypeError:  # mixed date/datetime expiry — not worth guessing
            return None
        if years <= 0:
            return None
        option_type = str(getattr(leg, "option_type", "CE") or "CE").upper()
        if option_type not in ("CE", "PE"):
            return None
        try:
            greeks = BlackScholes(
                risk_free_rate=DEFAULT_RISK_FREE_RATE, volatility=float(vol)
            ).greeks(
                spot=float(spot),
                strike=float(leg.strike),
                expiry_years=years,
                option_type=option_type,
            )
        except Exception as exc:  # noqa: BLE001 — analytics must never break a snapshot
            logger.debug(
                "[options-bridge] greeks failed for %s: %s",
                getattr(leg, "trading_symbol", "?"),
                exc,
            )
            return None
        # Theta is reported per DAY (the model returns it per year) because a
        # per-day number is what an operator reads off a position table.
        return {
            "delta": float(greeks.delta),
            "theta": float(greeks.theta) / 365.0,
            "iv": float(vol),
        }

    def leg_pricing_inputs(self, leg: Any) -> dict[str, Any]:
        """Market inputs for pricing one open leg (Portfolio Intelligence).

        The same inputs :meth:`_leg_greeks` uses — last bar spot, the leg's
        implied vol, its expiry and the bar-clock date — exposed publicly so
        the portfolio Greeks aggregator never reaches into bridge internals.
        Any input may be ``None``; the aggregator reports such legs as
        "Greeks unavailable" rather than inventing numbers.
        """
        return {
            "spot": float(self.last_spot) if self.last_spot else None,
            "iv": self._leg_vol(leg),
            "expiry": getattr(leg, "expiry", None) or self._structure_expiry,
            "reference_date": self._bar_dt.date() if self._bar_dt else None,
        }

    def _leg_vol(self, leg: Any) -> Optional[float]:
        """Implied vol for a leg: contract metadata first, generator default second."""
        token = str(getattr(leg, "instrument_token", "") or "")
        contracts = getattr(self.quote_provider, "_contracts", None)
        if token and isinstance(contracts, dict):
            contract = contracts.get(token)
            metadata = getattr(contract, "metadata", None) or {}
            vol = metadata.get("vol") or metadata.get("implied_volatility")
            if vol:
                try:
                    return float(vol)
                except (TypeError, ValueError):
                    return None
        underlying = str(getattr(leg, "underlying", "") or self.underlying or "").upper()
        default_vols = getattr(self._generator(), "VOL", None)
        if isinstance(default_vols, dict) and underlying in default_vols:
            try:
                return float(default_vols[underlying])
            except (TypeError, ValueError):
                return None
        return None

    def _generator(self) -> Any:
        """This bridge's chain surface — the broker's, else a policy-gated one.

        ``LiveChainProvider`` answers ``.generator`` with itself, so a runner
        on an authenticated broker feed never gets here. A provider that
        exposes no chain surface used to be silently fitted with a generated
        one; now that only happens when ``config/data_sources.yaml`` still
        enables synthetic, and otherwise the trade refuses with the reason.
        """
        generator = getattr(self.quote_provider, "generator", None) or getattr(
            getattr(self.quote_provider, "inner", None), "generator", None
        )
        if generator is None:
            generator = synthetic_chain_generator(f"options bridge ({self.underlying})")
            if generator is None:
                raise SourceDisabledError(
                    f"no option chain feed for {self.underlying}: the quote provider "
                    f"({type(self.quote_provider).__name__}) exposes no chain surface, "
                    f"and synthetic chains are disabled in config/data_sources.yaml. "
                    f"Authenticate the broker the runner trades on so the chain comes "
                    f"from the market."
                )
            self.quote_provider.generator = generator
        return generator

    def _execute(self, view: MarketView, structure_type: str, strategy_name: str) -> dict[str, Any]:
        from backtest.options.paper_trading import InsufficientMarginError

        check_structure_type(structure_type, self.allowed_structures)
        structure, option_type = STRUCTURES[structure_type]
        underlying = str(view.underlying or "NIFTY")
        generator = self._generator()
        self.underlying = underlying

        # Sync the synthetic market to the view's spot so premiums and
        # strikes line up with what the strategy saw. The entry is priced on
        # the view's own bar clock (when it carries one) so the premium is
        # consistent with the MTM that follows on the next bar (A1).
        if view.spot_price is not None:
            self._sync_market(underlying, float(view.spot_price), view.bar_timestamp)
        spot = Decimal(str(generator.get_spot(underlying)))

        expiry = self._select_expiry(generator, underlying)
        if expiry is None:
            raise ContractValidityError(
                REASON_NO_EXPIRY,
                f"{underlying}: the chain exposes no expiry as of "
                f"{self._bar_dt.date() if self._bar_dt else 'today'}",
            )
        if option_type == "BOTH":
            # Two-sided structure (e.g. short strangle): build a nested
            # {strike: {"CE": c, "PE": c}} map — the flat shape cannot hold
            # two contracts at one strike. A wide window is requested so
            # ±2–3% OTM strikes actually exist (the default ±10×50pt window
            # stops at ~2% on NIFTY).
            flat: dict[Decimal, dict[str, Any]] = {}
            for side in ("CE", "PE"):
                for strike, contract in generator.generate_chain(
                    underlying, expiry=expiry, option_type=side, strikes_each_side=30
                ).items():
                    flat.setdefault(strike, {})[side] = contract
            chain: Any = flat
        else:
            chain = generator.generate_chain(
                underlying, expiry=expiry, option_type=option_type
            )
        if hasattr(self.quote_provider, "register_chain"):
            # Nested two-sided chains are flattened to contracts for the
            # provider's token→contract registry.
            flat_contracts = (
                [c for side_map in chain.values() for c in side_map.values()]
                if option_type == "BOTH"
                else list(chain.values())
            )
            self.quote_provider.register_chain(flat_contracts)

        strikes = self._pick_strikes(structure_type, spot, chain, view.direction)
        if not strikes:
            raise ContractValidityError(
                REASON_NO_STRIKE,
                f"{underlying}: no strike on the {expiry} chain within tolerance of {spot}",
            )
        self._validate_entry_contracts(strikes, chain, option_type)
        intent = structure.build(view, strikes, chain, expiry, strategy_name=strategy_name)
        quantity = int(self.expression.get("quantity", 1) or 1)
        if quantity > 1:
            intent = self._scale_intent(intent, quantity)

        self._validate_entry_intent(intent)

        # Decision first, fill second: the journal must show a halt between the
        # two if the process dies, and both events share the decision's key so
        # replaying this bar cannot book the structure twice.
        self._journal_intent(KIND_DECIDED, intent)

        try:
            positions = self.option_broker.execute_structure(intent, self.quote_provider)
        except InsufficientMarginError as exc:
            self._journal_intent(
                KIND_REJECTED, intent, detail={"reason": str(exc)}
            )
            return {"rejected": True, "reason": str(exc)}

        self.open_structure_id = positions[0].structure_id if positions else None
        self.executed_count += 1
        self._journal_intent(
            KIND_EXECUTED,
            intent,
            structure_id=self.open_structure_id,
            detail={
                "simulated": True,
                "fills": [
                    {
                        "symbol": getattr(p, "trading_symbol", ""),
                        "side": getattr(p, "side", ""),
                        "quantity": getattr(p, "quantity", 0),
                        "price": str(getattr(p, "entry_price", "0")),
                    }
                    for p in positions
                ],
            },
        )
        # Entry bookkeeping for the exit rules (B1): age, direction, and the
        # net premium that percentage stops/targets are measured against.
        self._entry_bar_index = self._bar_index
        self._exit_bar_index = None
        self._bars_without_view = 0
        self._structure_direction = view.direction
        self._structure_expiry = expiry
        opened = self.option_broker.get_structure(self.open_structure_id or "")
        self._entry_premium = opened.total_entry_cost if opened is not None else Decimal("0")
        logger.info(
            "[options-bridge] opened %s on %s strikes=%s expiry=%s (view %s conf=%.2f)",
            structure_type,
            underlying,
            [str(s) for s in strikes],
            expiry,
            view.direction.value,
            float(view.confidence),
        )
        return {
            "structure_id": self.open_structure_id,
            "structure_type": structure_type,
            "underlying": underlying,
            "strikes": [str(s) for s in strikes],
            "expiry": str(expiry),
            "positions": len(positions),
            "direction": view.direction.value,
        }

    def _validate_entry_contracts(
        self, strikes: list[Any], chain: Any, option_type: str
    ) -> None:
        """The strikes the selector picked must name chain contracts with real terms.

        Checked before the intent is built: a leg with no lot size cannot even be
        constructed, and a guessed lot would size the premium outlay and every
        rupee stop in the book off a number the exchange never published.
        """
        for strike in strikes:
            contract = chain.get(strike)
            if option_type == "BOTH":
                contract = (contract or {}).get("CE") or (contract or {}).get("PE")
            if contract is None:
                raise ContractValidityError(
                    REASON_NO_CONTRACT,
                    f"{self.underlying} {strike}: the chain has no contract at this strike",
                )
            if int(getattr(contract, "lot_size", 0) or 0) <= 0:
                raise ContractValidityError(
                    REASON_NO_LOT_SIZE,
                    f"{getattr(contract, 'trading_symbol', strike)}: the chain row "
                    "carries no lot size — refusing to guess one",
                )

    def _validate_entry_intent(self, intent: Any) -> None:
        """Ask the market, right now, whether every leg is still quotable.

        A fill off whatever the provider's TTL cache happened to hold is not a
        decision — a dead broker session answers with zeros that a cache happily
        replays until the row expires (§4.2).
        """
        for leg in intent.legs:
            token = str(getattr(leg, "instrument_token", "") or "")
            symbol = str(getattr(leg, "trading_symbol", "") or "")
            self._require_fresh_quote(
                token or symbol, str(getattr(leg, "side", "BUY") or "BUY")
            )

    def _require_fresh_quote(self, token: str, side: str) -> None:
        """Re-read one leg's quote at decision time instead of filling off a cache.

        ``invalidate`` first: a row that is minutes old is no evidence the market
        still offers that price, and a dead broker session answers with zeros
        that a TTL cache happily replays.
        """
        invalidate = getattr(self.quote_provider, "invalidate", None)
        if callable(invalidate):
            invalidate(token)
        reader = getattr(self.quote_provider, "execution_quote", None)
        if callable(reader):
            # NoExecutableQuoteError when there is no side to cross against is
            # already a coded halt — let it stand rather than rename it.
            quote = reader(token, side)
        else:
            quote = self.quote_provider.get_quote(token)
        if not quote or quote.get("error") or float(quote.get("ltp") or 0.0) <= 0:
            reason = (quote or {}).get("error") or "empty row or zero ltp"
            raise ContractValidityError(
                REASON_NO_FRESH_QUOTE,
                f"{token}: no quote to {side} against ({reason})",
            )

    def _pick_strikes(
        self,
        structure_type: str,
        spot: Decimal,
        chain: dict[Decimal, Any],
        direction: Direction,
    ) -> list[Decimal]:
        strikes_sorted = sorted(chain.keys())
        if self.strict and self._active_rule is not None:
            rule = self._active_rule
            kwargs: dict[str, Any] = {}
            if rule.strike_selection == "delta":
                kwargs["delta_target"] = float(rule.delta_target or 0.35)
            elif rule.strike_selection == "fixed_distance":
                kwargs["distance_pct"] = float(rule.distance_pct or 0.0)
            selector = create_selector(rule.strike_selection, **kwargs)
        else:
            selection = str(self.expression.get("strike_selection", "atm")).lower()
            if selection == "delta":
                target = float(self.expression.get("delta_target", 0.35))
                selector: Any = DeltaSelector(delta_target=target)
            else:
                selector = ATMSelector()

        count = _STRICKES_NEEDED.get(structure_type, 1)
        if count == 1:
            picked = selector.pick_strike(spot, strikes_sorted, direction)
            return [picked] if picked is not None else []
        return selector.pick_strikes(spot, strikes_sorted, direction, count=count)

    @staticmethod
    def _is_neutral_view(view: MarketView) -> bool:
        """True when a view carries no direction (NEUTRAL counts as silence)."""
        direction = getattr(view, "direction", None)
        if direction is None:
            return True
        return _label(direction).lower() == "neutral"

    @staticmethod
    def _scale_intent(intent: Any, quantity: int) -> Any:
        from backtest.strategy.intent import OptionLeg, TradeIntent

        legs = tuple(
            OptionLeg(
                instrument_token=leg.instrument_token,
                trading_symbol=leg.trading_symbol,
                side=leg.side,
                quantity=leg.quantity * quantity,
                lot_size=leg.lot_size,
            )
            for leg in intent.legs
        )
        return TradeIntent(
            view=intent.view,
            structure_type=intent.structure_type,
            legs=legs,
            expiry=intent.expiry,
            strategy_name=intent.strategy_name,
            metadata=intent.metadata,
        )


class _GeneratorSettlementProvider:
    """Settlement price = the quote generator's current spot (B2).

    Mirrors ``engine/option_backtest_driver.py``'s provider of the same name:
    both replay loops settle against the spot the synthetic market is standing
    at on the expiry bar.
    """

    def __init__(self, generator: Any, underlying: str = "NIFTY") -> None:
        self.generator = generator
        self.underlying = underlying

    def get_settlement_price(self, underlying: str) -> float:
        return float(self.generator.get_spot(underlying or self.underlying))
