"""P1.1 rider — the option-chain snapshot recorder (the research data asset).

Pins the contract of ``ChainSnapshotRecorder`` / ``load_snapshots``:

* contract terms land for EVERY contract in the instrument-master slice
  (one API call) even when quote enrichment fails entirely;
* L1 quotes come from ``get_option_chain_data`` — one call per quoted expiry,
  merged per contract token; the raw payload shape is parsed defensively
  (bare list / {"data": [...]} / nested / unknown → terms-only);
* one run = one ``snapshot_ts`` batch; runs append (never update);
* ``load_snapshots`` filters by underlying / expiry / option_type, newest
  batch first.

SQLite end-to-end — no server, no credentials.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from backtest.db.manager import DatabaseManager
from backtest.instruments.base import OptionType
from backtest.instruments.option import OptionContract
from backtest.options.quote_providers import LiveChainProvider
from backtest.options.chain_snapshots import (
    ChainSnapshotRecorder,
    _extract_chain_rows,
    load_snapshots,
)

EXP1 = date.today() + timedelta(days=30)
EXP2 = date.today() + timedelta(days=60)


def _contract(strike: int, expiry: date, ctype: OptionType = OptionType.CE) -> OptionContract:
    token = f"TOK-{expiry.strftime('%m%d')}-{strike}-{ctype.value}"
    return OptionContract(
        instrument_token=token,
        trading_symbol=f"NIFTY{expiry.strftime('%y%m')}{strike}{ctype.value}",
        underlying="NIFTY",
        expiry=expiry,
        strike=Decimal(strike),
        option_type=ctype,
        lot_size=75,
        metadata={},
    )


class FakeSnapshotClient:
    """Canned broker: chain terms + whole-expiry quote payloads."""

    def __init__(self, contracts, payloads=None, fail_expuries=()):
        self.contracts = list(contracts)
        self.payloads = dict(payloads or {})
        self.fail_expiries = set(fail_expuries)
        self.chain_calls = 0
        self.data_calls: list[tuple[str, str]] = []

    def get_option_chain(self, underlying):
        self.chain_calls += 1
        return list(self.contracts) if str(underlying).upper() == "NIFTY" else []

    def get_option_chain_data(self, underlying, expiry_code, token):
        self.data_calls.append((str(expiry_code), str(token)))
        if expiry_code in self.fail_expiries:
            raise RuntimeError("quote endpoint down")
        return self.payloads.get(expiry_code, {"data": []})


def _manager(tmp_path) -> DatabaseManager:
    return DatabaseManager.from_env(url=f"sqlite:///{tmp_path / 'snapshots.db'}")


@pytest.fixture()
def env(tmp_path):
    manager = _manager(tmp_path)
    manager.connect()
    contracts = [
        _contract(24_700, EXP1),
        _contract(24_800, EXP1),
        _contract(24_800, EXP1, OptionType.PE),
        _contract(24_800, EXP2),
    ]
    client = FakeSnapshotClient(
        contracts,
        payloads={
            LiveChainProvider._expiry_code(EXP1): {
                "data": [
                    {
                        "instrument_token": f"TOK-{EXP1.strftime('%m%d')}-24700-CE",
                        "ltp": "51.25", "bid": "50.5", "ask": "52.0",
                        "volume": "1200", "oi": "84000",
                    },
                    {
                        "instrument_token": f"TOK-{EXP1.strftime('%m%d')}-24800-CE",
                        "ltp": "18.9", "bid": "18.5", "ask": "19.3",
                    },
                    {
                        "instrument_token": f"TOK-{EXP1.strftime('%m%d')}-24800-PE",
                        "ltp": "96.1", "bid": "95.0", "ask": "97.2",
                    },
                ]
            },
            LiveChainProvider._expiry_code(EXP2): {
                "data": [
                    {
                        "instrument_token": f"TOK-{EXP2.strftime('%m%d')}-24800-CE",
                        "ltp": "77.7",
                    }
                ]
            },
        },
    )
    recorder = ChainSnapshotRecorder(client=client, manager=manager)
    recorder.ensure_schema()
    yield manager, client, recorder
    manager.dispose() if hasattr(manager, "dispose") else None


class TestRecord:
    def test_terms_and_quotes_land(self, env):
        manager, client, recorder = env
        ts = datetime(2026, 9, 17, 10, 0, tzinfo=timezone.utc)
        written = recorder.record("NIFTY", spot=24_812.5, snapshot_ts=ts)

        assert written == 4
        assert client.chain_calls == 1, "ONE instrument-master call"
        rows = {
            f"{int(r['strike'])}{r['option_type']}{r['expiry'].day}": r
            for r in load_snapshots(manager)
        }
        near = rows[f"24700CE{EXP1.day}"]
        assert near["ltp"] == Decimal("51.25") and near["oi"] == 84_000
        assert near["lot_size"] == 75 and near["spot"] == Decimal("24812.5")
        assert near["snapshot_ts"].replace(tzinfo=timezone.utc) == ts

        # quote_expiries=1 (default) → only the NEAREST expiry enriched
        far = rows[f"24800CE{EXP2.day}"]
        assert far["ltp"] is None

    def test_two_expiries_when_asked(self, env):
        manager, client, recorder = env
        recorder.record("NIFTY", quote_expiries=2)
        far = [r for r in load_snapshots(manager) if r["expiry"] == EXP2]
        assert far and far[0]["ltp"] == Decimal("77.7")
        assert len(client.data_calls) == 2

    def test_quote_failure_still_records_terms(self, env):
        manager, client, recorder = env
        client.fail_expiries = {LiveChainProvider._expiry_code(EXP1)}
        written = recorder.record("NIFTY")
        assert written == 4, "terms survive the quote outage"
        assert all(r["ltp"] is None for r in load_snapshots(manager))

    def test_no_contracts_records_nothing(self, env):
        manager, client, recorder = env
        client.contracts = []
        assert recorder.record("NIFTY") == 0
        assert load_snapshots(manager) == []

    def test_runs_append_as_separate_batches(self, env):
        manager, _, recorder = env
        t1 = datetime(2026, 9, 17, 9, 20, tzinfo=timezone.utc)
        t2 = datetime(2026, 9, 17, 15, 45, tzinfo=timezone.utc)
        recorder.record("NIFTY", snapshot_ts=t1)
        recorder.record("NIFTY", snapshot_ts=t2)
        rows = load_snapshots(manager)
        assert len(rows) == 8
        stamps = {r["snapshot_ts"].replace(tzinfo=timezone.utc) for r in rows}
        assert stamps == {t1, t2}
        assert rows[0]["snapshot_ts"].replace(tzinfo=timezone.utc) == t2, "newest first"

    def test_record_all(self, env):
        manager, _, recorder = env
        counts = recorder.record_all(["NIFTY", "BANKNIFTY"])
        assert counts == {"NIFTY": 4, "BANKNIFTY": 0}  # fake serves NIFTY contracts only


class TestLoadSnapshots:
    def test_filters(self, env):
        manager, _, recorder = env
        recorder.record("NIFTY", quote_expiries=2)
        puts = load_snapshots(manager, option_type="PE")
        assert len(puts) == 1 and puts[0]["option_type"] == "PE"
        far = load_snapshots(manager, expiry=EXP2)
        assert len(far) == 1 and far[0]["expiry"] == EXP2
        other = load_snapshots(manager, underlying="BANKNIFTY")
        assert other == []


class TestPayloadShapes:
    @pytest.mark.parametrize(
        ("payload", "expected"),
        [
            ([{"instrument_token": "X"}], 1),
            ({"data": [{"instrument_token": "X"}]}, 1),
            ({"data": {"chain": [{"instrument_token": "X"}]}}, 1),
            ({"options": [{"instrument_token": "X"}]}, 1),
            ({"data": "unexpected"}, 0),
            ("garbage", 0),
            (None, 0),
        ],
    )
    def test_extract_chain_rows(self, payload, expected):
        assert len(_extract_chain_rows(payload)) == expected
