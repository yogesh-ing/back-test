"""Shared options-test support: the research structure profile.

Production construction defaults stay on the V1 execution whitelist
(``backtest/options/execution_gate.py``) — a runner, the bridge, the paper book
and the live trader all refuse short-option and multi-leg intents.

Tests that exercise multi-leg *mechanics* (leg repricing, spread exits,
rollback) need those structures, so they widen an already-built object with
:func:`research_profile` at the call site. Nothing in ``src/`` reads this
module, so it cannot widen a deployment.
"""

from __future__ import annotations

from backtest.options.execution_gate import RESEARCH_STRUCTURES


def research_profile(target):
    """Widen a ``StrategyRunner``, ``OptionsBridge``, book or trader. Returns it."""
    bridge = getattr(target, "options_bridge", target)
    if hasattr(bridge, "allowed_structures"):
        bridge.allowed_structures = set(RESEARCH_STRUCTURES)
    book = getattr(bridge, "option_broker", None)
    if book is not None and hasattr(book, "allowed_structures"):
        book.allowed_structures = set(RESEARCH_STRUCTURES)
    return target
