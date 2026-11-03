"""Sprint C / Ticket C1 (#23) — Institutional Two-Layer Clock Law in OrderLedger.

Institutional OMS Standard:
  * Layer 1 (Engine, in-memory OrderLedger, and SQL persistence): 100% canonical
    aware UTC (``YYYY-MM-DDTHH:MM:SS...+00:00``) so lexicographical sorting,
    aging, latency, and hold-duration math never shift by +05:30 across mixed
    equity (wall-clock) and option-leg (bar-clock) orders.
  * Layer 2 (UI / Diary presentation): converts aware UTC to ``Asia/Kolkata``
    (IST ``+05:30``) strictly at the display boundary.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from backtest.forward.paper_runner import (
    ORDER_CANCELLED,
    ORDER_FILLED,
    ORDER_REJECTED,
    SIDE_BUY,
    SIDE_SELL,
    OrderLedger,
    OrderRequest,
)
from backtest.timekeeping import EXCHANGE_TZ, UTC, parse_utc_iso, to_exchange


def _assert_canonical_utc_iso(stamp: str | None) -> datetime:
    assert isinstance(stamp, str) and stamp.endswith("+00:00"), (
        f"OrderLedger timestamp must be canonical aware UTC (+00:00), got {stamp!r}"
    )
    assert "T" in stamp, f"ISO-8601 'T' separator required for lexicographic sort: {stamp!r}"
    parsed = parse_utc_iso(stamp)
    assert parsed is not None and parsed.tzinfo == UTC
    return parsed


class TestOrderLedgerTwoLayerClockLaw:
    def test_submit_cancel_reject_amend_all_stamp_canonical_utc(self):
        ledger = OrderLedger()

        # 1. submit -> amend -> cancel
        o_cancel = ledger.submit(
            "inst-eq",
            OrderRequest(
                symbol="RELIANCE",
                side=SIDE_BUY,
                quantity=10,
                order_type="LIMIT",
                limit_price=1250.0,
            ),
        )
        _assert_canonical_utc_iso(o_cancel.created_ts)

        amended = ledger.amend(o_cancel.client_order_id, limit_price=1255.0)
        _assert_canonical_utc_iso(amended.amended_ts)
        assert amended.updated_ts == amended.amended_ts

        assert ledger.cancel(o_cancel.client_order_id) is True
        _assert_canonical_utc_iso(o_cancel.updated_ts)
        assert o_cancel.status == ORDER_CANCELLED

        # 2. submit -> reject
        o_reject = ledger.submit(
            "inst-eq",
            OrderRequest(symbol="TCS", side=SIDE_SELL, quantity=5),
        )
        assert ledger.reject(o_reject.client_order_id, "RMS margin shortfall") is True
        _assert_canonical_utc_iso(o_reject.updated_ts)
        assert o_reject.status == ORDER_REJECTED

    @pytest.mark.parametrize(
        "raw_ts,expected_utc_prefix,expected_ist_hms",
        [
            # Naive market-bar string (IST +05:30 -> 04:00:00 UTC)
            ("2026-11-02 09:30:00", "2026-11-02T04:00:00+00:00", "09:30:00"),
            # Aware IST ISO string (+05:30 -> 04:00:00 UTC)
            ("2026-11-02T09:30:00+05:30", "2026-11-02T04:00:00+00:00", "09:30:00"),
            # Aware UTC 'Z' string -> canonical +00:00
            ("2026-11-02T04:00:00Z", "2026-11-02T04:00:00+00:00", "09:30:00"),
            # Aware UTC '+00:00' string -> unchanged instant
            ("2026-11-02T04:00:00+00:00", "2026-11-02T04:00:00+00:00", "09:30:00"),
        ],
    )
    def test_record_filled_and_apply_fill_normalize_every_clock_face_to_utc(
        self, raw_ts, expected_utc_prefix, expected_ist_hms
    ):
        ledger = OrderLedger()

        # Option-leg synchronous fill (`record_filled`)
        opt_order = ledger.record_filled(
            "inst-opt",
            symbol="NIFTY26NOV24500CE",
            side=SIDE_BUY,
            quantity=75,
            price=142.5,
            ts=raw_ts,
            tag={"kind": "option", "phase": "entry"},
        )
        assert opt_order.status == ORDER_FILLED
        assert opt_order.created_ts == expected_utc_prefix
        assert opt_order.filled_ts == expected_utc_prefix
        assert opt_order.updated_ts == expected_utc_prefix

        # Layer-2 display conversion back to IST yields exact 09:30:00 (no +5h30 double shift!)
        ist_dt = to_exchange(_assert_canonical_utc_iso(opt_order.filled_ts))
        assert ist_dt is not None and ist_dt.tzinfo == EXCHANGE_TZ
        assert ist_dt.strftime("%H:%M:%S") == expected_ist_hms

        # Equity routed fill (`apply_fill`)
        eq_order = ledger.submit(
            "inst-eq",
            OrderRequest(symbol="INFY", side=SIDE_BUY, quantity=20),
        )
        fill = ledger.apply_fill(eq_order.client_order_id, price=1580.0, ts=raw_ts)
        assert fill.ts == expected_utc_prefix
        assert eq_order.filled_ts == expected_utc_prefix
        assert eq_order.updated_ts == expected_utc_prefix

    def test_interleaved_option_and_equity_orders_sort_strictly_chronologically(
        self, monkeypatch
    ):
        """Regression test for #23:
        Previously, an option leg filled at 09:30 IST stored ``"2026-11-02T09:30:00+05:30"``
        while an equity order submitted 10 minutes later at 09:40 IST stored
        ``"2026-11-02T04:10:00+00:00"``. Lexicographical sort on ``created_ts``
        placed the 09:30 IST option fill 5h 20m AHEAD of the 09:40 IST equity order!
        """
        import backtest.forward.paper_runner as pr_mod

        ledger = OrderLedger()

        # 1) 09:30:00 IST option entry (bar clock = naive IST)
        opt_0930 = ledger.record_filled(
            "inst-opt",
            symbol="NIFTY26NOV24500CE",
            side=SIDE_BUY,
            quantity=75,
            price=120.0,
            ts="2026-11-02 09:30:00",
        )

        # 2) 09:40:00 IST (= 04:10:00 UTC) equity order submission
        t_0940_utc = datetime(2026, 11, 2, 4, 10, 0, tzinfo=UTC)
        monkeypatch.setattr(pr_mod, "now_utc", lambda: t_0940_utc)
        eq_0940 = ledger.submit(
            "inst-eq",
            OrderRequest(symbol="RELIANCE", side=SIDE_BUY, quantity=15),
        )

        # 3) 09:45:00 IST option exit (aware IST +05:30)
        opt_0945 = ledger.record_filled(
            "inst-opt",
            symbol="NIFTY26NOV24500CE",
            side=SIDE_SELL,
            quantity=75,
            price=138.0,
            ts="2026-11-02T09:45:00+05:30",
        )

        rows = ledger.snapshot(limit=10)
        assert [r["client_order_id"] for r in rows] == [
            opt_0945.client_order_id,  # 04:15:00+00:00 (newest)
            eq_0940.client_order_id,   # 04:10:00+00:00
            opt_0930.client_order_id,  # 04:00:00+00:00 (oldest)
        ]

    def test_reattach_order_and_malformed_ts_never_crash_the_ledger(self):
        ledger = OrderLedger()

        # Legacy naive UTC in persisted live-gateway spec -> aware +00:00
        reattached = ledger.reattach_order(
            "inst-live",
            "PRT-live-1-1",
            {
                "symbol": "HDFCBANK",
                "side": "BUY",
                "quantity": 10,
                "created_ts": "2026-11-02T04:05:00",
            },
            broker_order_id="BRK-99",
        )
        assert reattached.created_ts == "2026-11-02T04:05:00+00:00"

        # Malformed vendor timestamp falls back fail-soft to now_utc() instead of dropping fill
        surviving = ledger.record_filled(
            "inst-opt",
            symbol="BANKNIFTY26NOV52000PE",
            side=SIDE_BUY,
            quantity=30,
            price=210.0,
            ts="not-a-valid-timestamp",
        )
        _assert_canonical_utc_iso(surviving.filled_ts)
