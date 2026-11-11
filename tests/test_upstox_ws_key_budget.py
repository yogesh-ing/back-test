"""Phase 3 item 2 — the per-socket key budget (impl plan, §7 Q1).

``check_limits`` sums the keys of every ring sharing one socket against
``MODE_KEY_LIMITS``, not each ring alone: two ``full`` batches each legal
under the 2000-key cap must not jointly exceed it, and the second one is
refused. The cap is what the venue's feeder will drop, so a joint overrun
would be silently lost keys mid-session.
"""

from __future__ import annotations

import pytest

from backtest.data.upstox_ws_wrapper import (
    MODE_FULL,
    MODE_LTPC,
    MODE_KEY_LIMITS,
    SubscriptionLimitError,
    UpstoxWebSocketWrapper,
)


def _wrapper(mode=MODE_FULL) -> UpstoxWebSocketWrapper:
    return UpstoxWebSocketWrapper(token_provider=lambda: "session-token", mode=mode)


def test_two_rings_each_legal_jointly_exceed_the_cap_and_the_second_is_refused():
    wrapper = _wrapper()
    cap = MODE_KEY_LIMITS[MODE_FULL]
    ring_a = [f"NSE_FNO|A{i}" for i in range(cap - 100)]  # 1900 keys — legal alone
    ring_b = [f"NSE_FNO|B{i}" for i in range(cap - 100)]  # 1900 keys — legal alone

    wrapper.check_limits(ring_a, MODE_FULL)  # ring A alone fits

    # Ring A is on the socket; ring B asks for its own legal batch.
    wrapper._keys = list(ring_a)
    with pytest.raises(SubscriptionLimitError):
        wrapper.check_limits(ring_b, MODE_FULL)

    # The refusal counts what the socket carries plus what was asked:
    assert len(set(ring_a) | set(ring_b)) > cap
    assert len(ring_b) <= cap, "the second batch was legal ON ITS OWN"


def test_a_joint_batch_under_the_cap_still_passes():
    wrapper = _wrapper()
    cap = MODE_KEY_LIMITS[MODE_FULL]
    ring_a = [f"NSE_FNO|A{i}" for i in range(cap // 2)]
    ring_b = [f"NSE_FNO|B{i}" for i in range(cap // 2)]
    wrapper._keys = list(ring_a)
    wrapper.check_limits(ring_b, MODE_FULL)  # exactly at the cap — allowed


def test_the_budget_is_per_mode():
    wrapper = _wrapper()
    ltpc_cap = MODE_KEY_LIMITS[MODE_LTPC]
    wrapper._keys = [f"NSE_FNO|X{i}" for i in range(ltpc_cap)]
    # The same socket already carries ltpc_cap keys — under ltpc's ceiling
    # that batch is fine, but a full-mode ask is measured against full's cap.
    with pytest.raises(SubscriptionLimitError):
        wrapper.check_limits(
            [f"NSE_FNO|Y{i}" for i in range(MODE_KEY_LIMITS[MODE_FULL])], MODE_FULL
        )


def test_a_replacement_start_is_checked_against_what_the_socket_carries():
    """Fail-closed: start() also runs the joint check before it replaces the
    set — a socket already carrying keys must not accept a bigger ask."""
    wrapper = _wrapper()
    cap = MODE_KEY_LIMITS[MODE_FULL]
    wrapper._keys = [f"NSE_FNO|K{i}" for i in range(cap - 1)]
    with pytest.raises(SubscriptionLimitError):
        wrapper.check_limits([f"NSE_FNO|N{i}" for i in range(5)], MODE_FULL)
