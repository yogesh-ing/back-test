"""Tests for the Phase-4 capture writer into option_chain_history.

Pins: clean rows persist; §7.1 rejects are logged and never stored; re-running
a capture is idempotent on the unique contract/bar/source key; naive stamps
store aware UTC; depth+greeks ride the WS normalizer; absent fields stay NULL
(no fabrication); and the HTTP ChainSnapshotRecorder rides the writer through
its optional hook without changing the L1 snapshot write.

SQLite end-to-end — no server, no credentials.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from backtest.db.manager import DatabaseManager
from backtest.db.models import OptionChainHistory, OptionChainSnapshot
from backtest.instruments.base import OptionType
from backtest.instruments.option import OptionContract
from backtest.options.chain_history import (
    ChainHistoryWriter,
    history_row_from_depth_packet,
    history_rows_from_snapshots,
)
from backtest.options.chain_snapshots import ChainSnapshotRecorder
from backtest.options.quote_providers import LiveChainProvider
from backtest.timekeeping import EXCHANGE_TZ

EXP = date.today() + timedelta(days=0)  # same-day expiry keeps df≈1 for clean parity
BAR = datetime.combine(EXP, datetime.min.time(), tzinfo=EXCHANGE_TZ).replace(hour=10)
SPOT = 25000.0


def leg(strike, otype, mid, *, spread=2.0, iv=0.16, spot=SPOT, **kw):
    row = {
        "underlying": "NIFTY", "expiry": EXP, "strike": Decimal(str(strike)),
        "option_type": otype, "bar_ts": BAR, "bid": mid - spread / 2,
        "ask": mid + spread / 2, "ltp": mid, "iv": iv, "spot": spot,
        "oi": 5000, "volume": 1000, "instrument_token": f"TOK-{strike}-{otype}",
        "trading_symbol": f"NIFTY{strike}{otype}", "lot_size": 75,
    }
    row.update(kw)
    return row


def clean_batch():
    rows = []
    for strike in (24900, 25000, 25100):
        intrinsic = SPOT - strike
        rows.append(leg(strike, "CE", 150.0 + intrinsic / 2))
        rows.append(leg(strike, "PE", 150.0 - intrinsic / 2))
    return rows


@pytest.fixture()
def manager(tmp_path):
    mgr = DatabaseManager.from_env(url=f"sqlite:///{tmp_path / 'chain.db'}")
    mgr.connect()
    return mgr


@pytest.fixture()
def writer(manager, tmp_path):
    w = ChainHistoryWriter(manager, reject_log_path=str(tmp_path / "chain_rejected.log"))
    w.ensure_schema()
    return w


def _count(manager):
    with manager.session() as session:
        return len(session.execute(select(OptionChainHistory)).scalars().all())


def test_clean_batch_persisted(writer, manager):
    result = writer.write_rows(clean_batch(), source="upstox", bar_ts=BAR)
    assert result["accepted"] == 6
    assert result["rejected"] == 0
    assert result["written"] == 6
    assert _count(manager) == 6


def test_junk_rejected_logged_not_stored(writer, manager, tmp_path):
    rows = clean_batch()
    rows.append(leg(25200, "CE", 150.0, iv=5.0))  # impossible IV
    rows.append(leg(25300, "CE", 150.0, ltp=900.0))  # ltp far off book
    result = writer.write_rows(rows, source="upstox", bar_ts=BAR)
    assert result["rejected"] == 2
    assert result["written"] == 6
    assert _count(manager) == 6
    log = (tmp_path / "chain_rejected.log").read_text(encoding="utf-8").strip().splitlines()
    assert len(log) == 2
    reasons = {json.loads(line)["reason"] for line in log}
    assert reasons == {"iv_impossible", "ltp_off_book"}


def test_rewrite_is_idempotent(writer, manager):
    batch = clean_batch()
    writer.write_rows(batch, source="upstox", bar_ts=BAR)
    again = writer.write_rows(batch, source="upstox", bar_ts=BAR)
    assert again["written"] == 0
    assert again["duplicates"] == 6
    assert _count(manager) == 6


def test_distinct_source_is_distinct_row(writer, manager):
    batch = clean_batch()
    writer.write_rows(batch, source="upstox", bar_ts=BAR)
    other = writer.write_rows(batch, source="mstock", bar_ts=BAR)
    assert other["written"] == 6
    assert _count(manager) == 12


def test_naive_bar_ts_stored_aware_utc(writer, manager):
    from backtest.options.chain_history import _aware_utc

    # The writer normalizes a naive exchange stamp to aware UTC at the boundary.
    normalized = _aware_utc(BAR.replace(tzinfo=None))
    assert normalized.tzinfo is not None
    assert normalized.utcoffset() == timedelta(0)
    assert normalized.hour == 4 and normalized.minute == 30  # 10:00 IST → 04:30 UTC

    row = leg(25000, "CE", 150.0)
    row["bar_ts"] = BAR.replace(tzinfo=None)
    writer.write_rows([row], source="upstox")
    with manager.session() as session:
        stored = session.execute(select(OptionChainHistory)).scalars().first()
    # SQLite drops tz on read; the stored UTC wall-clock is the proof of conversion.
    assert stored.bar_ts.hour == 4 and stored.bar_ts.minute == 30


def test_absent_greeks_stay_null(writer, manager):
    row = leg(25000, "CE", 150.0)
    for k in ("iv", "delta", "gamma", "theta", "vega"):
        row.pop(k, None)
    row["oi"] = None
    row["volume"] = None
    writer.write_rows([row], source="upstox", bar_ts=BAR)
    with manager.session() as session:
        stored = session.execute(select(OptionChainHistory)).scalars().first()
    assert stored.iv is None
    assert stored.delta is None
    assert stored.depth_json is None


def test_row_without_identity_dropped(writer, manager):
    result = writer.write_rows([{"bid": 1, "ask": 2, "ltp": 1.5}], source="upstox")
    assert result["accepted"] == 0
    assert result["written"] == 0
    assert _count(manager) == 0


def test_depth_packet_normalizer_persists_ladder_and_greeks(writer, manager):
    contract = {
        "underlying": "NIFTY", "expiry": EXP, "strike": Decimal("25000"),
        "option_type": "CE", "instrument_token": "UP-25000-CE",
        "trading_symbol": "NIFTY2400025000CE", "lot_size": 75,
    }
    packet = {
        "token": "UP-25000-CE", "ltp": 150.0, "bid": 149.0, "ask": 151.0,
        "bids": [[149.0, 75], [148.5, 150]], "asks": [[151.0, 75], [151.5, 150]],
        "oi": 5000, "volume": 1000, "iv": 0.17, "delta": 0.51,
        "gamma": 0.001, "theta": -12.0, "vega": 8.0,
        "recv_utc": BAR.astimezone(timezone.utc).isoformat(),
    }
    row = history_row_from_depth_packet(packet, contract=contract, spot=SPOT, source="upstox", bar_ts=BAR)
    result = writer.write_rows([row], source="upstox", bar_ts=BAR)
    assert result["written"] == 1
    with manager.session() as session:
        stored = session.execute(select(OptionChainHistory)).scalars().first()
    assert stored.depth_json["bid"][0] == [149.0, 75]
    assert stored.depth_json["ask"][1] == [151.5, 150]
    assert stored.iv == pytest.approx(0.17)
    assert stored.delta == pytest.approx(0.51)


def test_snapshot_bridge_and_recorder_ride(manager, tmp_path):
    # Clean ATM pair plus one junk leg; the recorder must screen on the way in.
    contracts = [
        OptionContract(
            instrument_token=f"TOK-25000-{t}", trading_symbol=f"NIFTY25000{t}",
            underlying="NIFTY", expiry=EXP, strike=Decimal("25000"),
            option_type=OptionType.CE if t == "CE" else OptionType.PE,
            lot_size=75, metadata={},
        )
        for t in ("CE", "PE")
    ]
    contracts.append(
        OptionContract(
            instrument_token="TOK-25200-CE", trading_symbol="NIFTY25200CE",
            underlying="NIFTY", expiry=EXP, strike=Decimal("25200"),
            option_type=OptionType.CE, lot_size=75, metadata={},
        )
    )
    code = LiveChainProvider._expiry_code(EXP)
    payload = {"data": [
        {"instrument_token": "TOK-25000-CE", "ltp": "150", "bid": "149", "ask": "151", "oi": "5000", "volume": "1000"},
        {"instrument_token": "TOK-25000-PE", "ltp": "150", "bid": "149", "ask": "151", "oi": "5000", "volume": "1000"},
        {"instrument_token": "TOK-25200-CE", "ltp": "900", "bid": "149", "ask": "151", "oi": "5000", "volume": "1000"},
    ]}

    class Client:
        broker_name = "mstock"

        def get_option_chain(self, underlying):
            return contracts

        def get_option_chain_data(self, underlying, expiry_code, token):
            return payload

    writer = ChainHistoryWriter(manager, reject_log_path=str(tmp_path / "rej.log"))
    writer.ensure_schema()
    recorder = ChainSnapshotRecorder(Client(), manager, history_writer=writer)
    recorder.ensure_schema()
    written = recorder.record("NIFTY", spot=SPOT, snapshot_ts=BAR)
    assert written == 3  # L1 snapshot write unchanged: all three terms rows
    # History asset: the junk ltp-off-book leg is screened out, the clean pair lands.
    assert _count(manager) == 2
    with manager.session() as session:
        snaps = len(session.execute(select(OptionChainSnapshot)).scalars().all())
    assert snaps == 3


def test_recorder_without_history_writer_unchanged(manager):
    contracts = [
        OptionContract(
            instrument_token="TOK-25000-CE", trading_symbol="NIFTY25000CE",
            underlying="NIFTY", expiry=EXP, strike=Decimal("25000"),
            option_type=OptionType.CE, lot_size=75, metadata={},
        )
    ]

    class Client:
        broker_name = "mstock"

        def get_option_chain(self, underlying):
            return contracts

        def get_option_chain_data(self, underlying, expiry_code, token):
            return {"data": []}

    recorder = ChainSnapshotRecorder(Client(), manager)  # no history_writer
    recorder.ensure_schema()
    assert recorder.record("NIFTY", spot=SPOT, snapshot_ts=BAR) == 1
    # option_chain_history was never created/written by the plain recorder.
    from sqlalchemy import inspect

    assert inspect(manager.engine).has_table("option_chain_history") is False


def test_load_chain_history_range_and_aware_utc(writer, manager):
    from backtest.options.chain_history import chain_coverage, load_chain_history

    writer.write_rows(clean_batch(), source="upstox", bar_ts=BAR)
    # A bar outside the requested day is excluded.
    other = leg(25000, "CE", 150.0)
    other["bar_ts"] = BAR + timedelta(days=3)
    writer.write_rows([other], source="upstox")

    day = EXP.isoformat()
    rows = load_chain_history(manager, "NIFTY", day, day)
    assert len(rows) == 6
    assert all(r["bar_ts"].tzinfo is not None for r in rows)  # normalized aware UTC
    assert all(r["bar_ts"].utcoffset() == timedelta(0) for r in rows)

    cov = chain_coverage(manager, "NIFTY")
    assert cov["count"] == 7
    assert cov["from"] == day
    assert cov["to"] == (EXP + timedelta(days=3)).isoformat()


def test_load_chain_history_empty_range(writer, manager):
    from backtest.options.chain_history import chain_coverage, load_chain_history

    writer.write_rows(clean_batch(), source="upstox", bar_ts=BAR)
    future = (EXP + timedelta(days=30)).isoformat()
    assert load_chain_history(manager, "NIFTY", future, future) == []
    assert chain_coverage(manager, "NIFTY")["count"] == 6
    assert chain_coverage(manager, "BANKNIFTY")["count"] == 0