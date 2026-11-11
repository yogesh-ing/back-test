"""The shared serve gate: per-venue policy, the load-bearing rescue, stamp parsing.

Impl plan T0.4. The mStock behaviour this gate carries is pinned elsewhere
(``tests/test_option_ladder_gates.py::TestServeGate`` and
``tests/forward/test_phase2_ws_ticks_and_quotes.py`` must stay green unchanged);
these tests pin the *new* surface: the two venue policies cannot move each
other, the socket-liveness rescue is what keeps quiet-leg fills alive (not the
ceiling), and ``stamp_age_seconds`` reads every stamp shape the two venues
actually send.
"""

from __future__ import annotations

import time
from dataclasses import FrozenInstanceError

import pytest

from backtest.brokers.mstock import MStockBroker
from backtest.data.depth_serve_gate import (
    MSTOCK_SERVE_POLICY,
    UPSTOX_SERVE_POLICY,
    ServeGatePolicy,
    serve_ws_row,
    stamp_age_seconds,
)
from backtest.timekeeping import EXCHANGE_TZ, iso_utc, now_utc


def _row(age_s: float, *, ladder_age_s: float | None = None, stamp=None) -> dict:
    """An mStock/Upstox-shaped cache row whose price is ``age_s`` seconds old."""
    now_mono = time.monotonic()
    row = {
        "ltp": 340.0,
        "bid": 339.5,
        "ask": 341.0,
        "depth": {"bid": [[339.5, 65]], "ask": [[341.0, 65]]},
        "ladder_status": "ok",
        "recv_mono": now_mono - age_s,
        "ltp_mono": now_mono - age_s,
        "ladder_mono": now_mono - (ladder_age_s if ladder_age_s is not None else age_s),
    }
    if stamp is not None:
        row.pop("recv_mono")
        row.pop("ltp_mono")
        row["timestamp"] = stamp
    return row


def _gate(row, policy, **overrides) -> dict:
    kwargs = dict(
        row=row,
        key="NSE_FO|NIFTY|26NOV2026|25050|PE",
        now_mono=time.monotonic(),
        is_connected=True,
        last_any_tick_mono=time.monotonic() - 0.2,
        token_subscribed=True,
        policy=policy,
    )
    kwargs.update(overrides)
    return serve_ws_row(**kwargs)


class TestPolicyIndependence:
    def test_mstock_ladder_ttl_matches_the_broker_class_constant(self) -> None:
        """The policy cites the same measurement as the pinned class attr."""
        assert (
            MSTOCK_SERVE_POLICY.ladder_ttl_seconds == MStockBroker.WS_LADDER_TTL_SECONDS
        )

    def test_policies_are_frozen(self) -> None:
        with pytest.raises(FrozenInstanceError):
            MSTOCK_SERVE_POLICY.max_age_seconds = 999.0  # type: ignore[misc]

    def test_one_venues_policy_cannot_move_the_others(self) -> None:
        """Rebuilding Upstox's policy from a fresh measurement must leave
        mStock's constants byte-identical — per-venue ceilings, never shared."""
        before = MSTOCK_SERVE_POLICY
        rebuilt = ServeGatePolicy(
            venue="upstox", max_age_seconds=3.0, ladder_ttl_seconds=30.0
        )
        assert rebuilt.max_age_seconds == 3.0
        assert MSTOCK_SERVE_POLICY is before
        assert MSTOCK_SERVE_POLICY.max_age_seconds == 10.0
        assert MSTOCK_SERVE_POLICY.ladder_ttl_seconds == 120.0

    def test_upstox_provisional_policy_is_fail_closed(self) -> None:
        """Until T0.5-2 measures deliberate quiet tokens, a stale Upstox row is
        refused outright — never rescued on unexamined reasoning."""
        assert UPSTOX_SERVE_POLICY.liveness_rescue is False
        assert MSTOCK_SERVE_POLICY.liveness_rescue is True


class TestRescueIsLoadBearing:
    """2026-11-11 tape: mStock's per-leg p99 gap (16.2 s) EXCEEDS its 10 s
    ceiling — quiet-leg fills survive only through the socket-liveness rescue.
    These tests pin that fact so the rescue cannot be 'simplified' away."""

    def _quiet_leg(self) -> dict:
        return _row(age_s=40.0)

    def test_the_rescue_serves_the_quiet_leg(self) -> None:
        q = _gate(self._quiet_leg(), MSTOCK_SERVE_POLICY)
        assert (q.get("bid"), q.get("ask")) == (339.5, 341.0)

    def test_without_the_rescue_the_same_leg_is_refused(self) -> None:
        """The 10 s ceiling alone would strip depth from every quiet leg."""
        no_rescue = ServeGatePolicy(
            venue="mstock",
            max_age_seconds=MSTOCK_SERVE_POLICY.max_age_seconds,
            ladder_ttl_seconds=MSTOCK_SERVE_POLICY.ladder_ttl_seconds,
            liveness_rescue=False,
        )
        assert _gate(self._quiet_leg(), no_rescue) == {}

    def test_a_dead_socket_cannot_rescue_anything(self) -> None:
        q = _gate(
            self._quiet_leg(),
            MSTOCK_SERVE_POLICY,
            last_any_tick_mono=time.monotonic() - 400.0,
        )
        assert q == {}

    def test_an_unsubscribed_token_cannot_be_rescued(self) -> None:
        assert _gate(self._quiet_leg(), MSTOCK_SERVE_POLICY, token_subscribed=False) == {}


class TestStampAges:
    """Every stamp shape the two venues actually send (cadence_20261111.txt:
    mStock naive no-offset strings + time-only clocks + Go-zero sentinels;
    Upstox aware-UTC ISO from _epoch_iso)."""

    def test_aware_iso(self) -> None:
        age = stamp_age_seconds(iso_utc(now_utc()))
        assert age is not None and 0.0 <= age < 5.0

    def test_naive_iso_is_read_as_exchange_time(self) -> None:
        naive = now_utc().astimezone(EXCHANGE_TZ).replace(tzinfo=None).isoformat()
        age = stamp_age_seconds(naive)
        assert age is not None and -1.0 <= age < 6.0

    def test_time_only_clock_stamps(self) -> None:
        now_local = now_utc().astimezone(EXCHANGE_TZ)
        for fmt, text in (
            ("%I:%M:%S%p", now_local.strftime("%I:%M:%S%p")),
            ("%H:%M:%S", now_local.strftime("%H:%M:%S")),
        ):
            age = stamp_age_seconds(text)
            assert age is not None, fmt
            assert -1.0 <= age < 6.0, fmt

    def test_go_zero_sentinel_is_stale_not_unreadable(self) -> None:
        """``1980-01-01T12:00:00`` is mStock's 'nothing to publish' stamp — it
        must age out like any ancient price, not crash the parser."""
        age = stamp_age_seconds("1980-01-01T12:00:00")
        assert age is not None and age > 60.0

    def test_unreadable_stamps_return_none(self) -> None:
        for junk in (None, "", "  ", "not-a-stamp", object()):
            assert stamp_age_seconds(junk) is None

    def test_epoch_iso_form_written_by_the_upstox_wrapper(self) -> None:
        """_epoch_iso writes aware-UTC ISO; the wall-clock fallback must read it."""
        age = stamp_age_seconds(iso_utc(now_utc()))
        assert age is not None and age < 5.0


class TestServeWsRowContract:
    def test_fresh_row_is_served_with_ws_source(self) -> None:
        q = _gate(_row(age_s=1.0), MSTOCK_SERVE_POLICY)
        assert q["source"] == "ws" and q["bid"] == 339.5

    def test_an_explicit_source_is_kept(self) -> None:
        row = _row(age_s=1.0)
        row["source"] = "ws:probe"
        assert _gate(row, MSTOCK_SERVE_POLICY)["source"] == "ws:probe"

    def test_missing_row_is_fail_closed(self) -> None:
        assert _gate(None, MSTOCK_SERVE_POLICY) == {}
        assert _gate({}, MSTOCK_SERVE_POLICY) == {}

    def test_disconnected_socket_serves_nothing(self) -> None:
        assert _gate(_row(age_s=1.0), MSTOCK_SERVE_POLICY, is_connected=False) == {}

    def test_legacy_stamp_path_stale_refused(self) -> None:
        old_stamp = "1980-01-01T12:00:00"
        assert _gate(_row(0.0, stamp=old_stamp), MSTOCK_SERVE_POLICY) == {}

    def test_legacy_stamp_path_fresh_served(self) -> None:
        fresh = iso_utc(now_utc())
        q = _gate(_row(0.0, stamp=fresh), MSTOCK_SERVE_POLICY)
        assert q["bid"] == 339.5

    def test_future_skewed_stamp_is_refused(self) -> None:
        from datetime import timedelta

        future = iso_utc(now_utc() + timedelta(seconds=30))
        assert _gate(_row(0.0, stamp=future), MSTOCK_SERVE_POLICY) == {}

    def test_no_stamp_at_all_is_refused(self) -> None:
        row = _row(0.0, stamp=None)
        row.pop("recv_mono", None)
        row.pop("ltp_mono", None)
        assert _gate(row, MSTOCK_SERVE_POLICY) == {}

    def test_expired_ladder_keeps_price_loses_depth(self) -> None:
        q = _gate(_row(age_s=1.0, ladder_age_s=400.0), MSTOCK_SERVE_POLICY)
        assert q["ltp"] == 340.0
        assert q["bid"] == 0.0 and q["ask"] == 0.0
        assert q["depth"] == {}
        assert q["ladder_status"].startswith("expired:")

    def test_no_ageing_when_the_ceiling_is_disabled(self) -> None:
        """max_age_seconds=None (or <=0) disables the check — mStock's
        historical contract, kept for both venues. Ladder TTL still binds."""
        for disabled in (None, 0, -1.0):
            q = _gate(
                _row(age_s=400.0, ladder_age_s=1.0),
                MSTOCK_SERVE_POLICY,
                max_age_seconds=disabled,
            )
            assert q.get("bid") == 339.5, disabled
