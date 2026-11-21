"""Evidence tests for the §7.1 chain screen (plan V3).

A detector is only worth wiring into the capture path once it has been shown,
on fixtures, to (1) catch the known-bad prints, (2) leave a clean chain alone,
and (3) name its own blind spot. These tests are that evidence: clean pairs are
built to satisfy put-call parity exactly (risk_free=0 isolates the parity logic
from discounting), and each junk class is injected one at a time so the catch
rate is attributable. The blind spot — a uniform mispricing that preserves
parity — is asserted explicitly so it is a documented limit, not a surprise.
"""

from __future__ import annotations

import json
import math
from datetime import date, datetime, time

import pytest

from backtest.options.chain_filter import (
    ChainRejectLog,
    Rejection,
    screen_chain_rows,
)
from backtest.timekeeping import EXCHANGE_TZ

EXP = date(2024, 1, 25)
BAR = datetime(2024, 1, 25, 10, 0, tzinfo=EXCHANGE_TZ)
SPOT = 25000.0


def leg(strike, otype, mid, *, spread=2.0, ltp=None, iv=0.16, spot=SPOT, **kw):
    row = {
        "underlying": "NIFTY",
        "expiry": EXP,
        "strike": float(strike),
        "option_type": otype,
        "bar_ts": BAR,
        "bid": mid - spread / 2.0,
        "ask": mid + spread / 2.0,
        "ltp": mid if ltp is None else ltp,
        "iv": iv,
        "spot": spot,
        "volume": 1000,
        "oi": 5000,
    }
    row.update(kw)
    return row


def clean_chain():
    """Five strikes around ATM, CE/PE pairs that satisfy C − P = S − K exactly."""
    rows = []
    for strike in (24800, 24900, 25000, 25100, 25200):
        intrinsic = SPOT - strike  # C − P target with df=1
        ce_mid = 150.0 + intrinsic / 2.0
        pe_mid = 150.0 - intrinsic / 2.0
        rows.append(leg(strike, "CE", ce_mid))
        rows.append(leg(strike, "PE", pe_mid))
    return rows


# --- clean side: false-positive rate must be zero -------------------------


def test_clean_chain_fully_accepted():
    rows = clean_chain()
    accepted, rejected = screen_chain_rows(rows, risk_free=0.0)
    assert len(accepted) == len(rows) == 10
    assert rejected == []


def test_ltp_within_tolerance_outside_book_is_tolerated():
    # ask 151, ltp 153 → 1.3% above ask, inside the 5% band: timing, not junk.
    rows = [leg(25000, "CE", 150.0, ltp=153.0), leg(25000, "PE", 150.0)]
    accepted, rejected = screen_chain_rows(rows, risk_free=0.0)
    assert rejected == []
    assert len(accepted) == 2


def test_iv_at_200pct_boundary_accepted():
    rows = [leg(25000, "CE", 150.0, iv=2.0), leg(25000, "PE", 150.0, iv=0.0)]
    accepted, rejected = screen_chain_rows(rows, risk_free=0.0)
    assert rejected == []


def test_absent_iv_and_book_abstain_not_reject():
    ce = leg(25000, "CE", 150.0)
    pe = leg(25000, "PE", 150.0)
    for r in (ce, pe):
        r["iv"] = None
        r["bid"] = None
        r["ask"] = None
        r["ltp"] = None
    accepted, rejected = screen_chain_rows([ce, pe], risk_free=0.0)
    assert rejected == []
    assert len(accepted) == 2


# --- known-bad: catch rate must be 100% per class -------------------------


def test_parity_violation_rejects_both_legs():
    rows = clean_chain()
    # Convict the ATM pair: CE printed 150 pts too rich, parity residual 150.
    rows[4] = leg(25000, "CE", 300.0)  # index 4 = 25000 CE
    accepted, rejected = screen_chain_rows(rows, risk_free=0.0)
    reasons = {r.reason for r in rejected}
    assert reasons == {"put_call_parity"}
    assert len(rejected) == 2  # both legs of the pair
    assert len(accepted) == 8
    convicted = {r.row["option_type"] for r in rejected}
    assert convicted == {"CE", "PE"}


def test_ltp_far_above_ask_rejected():
    rows = [leg(25000, "CE", 150.0, ltp=200.0), leg(25000, "PE", 150.0)]
    accepted, rejected = screen_chain_rows(rows, risk_free=0.0)
    assert len(rejected) == 1
    assert rejected[0].reason == "ltp_off_book"
    assert rejected[0].row["option_type"] == "CE"
    assert len(accepted) == 1


def test_ltp_far_below_bid_rejected():
    rows = [leg(25000, "CE", 150.0, ltp=100.0), leg(25000, "PE", 150.0)]
    _, rejected = screen_chain_rows(rows, risk_free=0.0)
    assert len(rejected) == 1
    assert rejected[0].reason == "ltp_off_book"


def test_negative_iv_rejected():
    rows = [leg(25000, "CE", 150.0, iv=-0.05), leg(25000, "PE", 150.0)]
    _, rejected = screen_chain_rows(rows, risk_free=0.0)
    assert len(rejected) == 1
    assert rejected[0].reason == "iv_impossible"


def test_iv_above_200pct_rejected():
    rows = [leg(25000, "CE", 150.0, iv=2.5), leg(25000, "PE", 150.0)]
    _, rejected = screen_chain_rows(rows, risk_free=0.0)
    assert len(rejected) == 1
    assert rejected[0].reason == "iv_impossible"


def test_mixed_junk_batch_catch_rate():
    rows = clean_chain()  # 10 clean
    rows.append(leg(25300, "CE", 150.0, ltp=400.0))  # ltp off book
    rows.append(leg(25400, "CE", 150.0, iv=3.0))  # iv impossible
    rows.append(leg(25500, "CE", 150.0, iv=-0.2))  # iv negative
    accepted, rejected = screen_chain_rows(rows, risk_free=0.0)
    assert len(rejected) == 3
    assert len(accepted) == 10
    assert {r.reason for r in rejected} == {"ltp_off_book", "iv_impossible"}


# --- partition invariant: nothing silently dropped ------------------------


def test_accepted_plus_rejected_partition_input():
    rows = clean_chain()
    rows[4] = leg(25000, "CE", 300.0)
    rows.append(leg(25300, "CE", 150.0, iv=5.0))
    accepted, rejected = screen_chain_rows(rows, risk_free=0.0)
    assert len(accepted) + len(rejected) == len(rows)


def test_reject_sink_receives_every_rejection():
    seen: list[Rejection] = []
    rows = clean_chain()
    rows[4] = leg(25000, "CE", 300.0)
    rows.append(leg(25300, "CE", 150.0, iv=5.0))
    screen_chain_rows(rows, risk_free=0.0, reject_sink=seen.append)
    assert len(seen) == 3  # parity pair (2) + iv (1)


def test_chain_reject_log_writes_jsonl(tmp_path):
    path = tmp_path / "chain_rejected.log"
    sink = ChainRejectLog(str(path))
    rows = [leg(25000, "CE", 150.0, iv=5.0), leg(25000, "PE", 150.0)]
    screen_chain_rows(rows, risk_free=0.0, reject_sink=sink.write)
    assert sink.count == 1
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["reason"] == "iv_impossible"
    assert record["row"]["strike"] == 25000.0
    assert "recv_utc" in record


# --- discounting is actually applied --------------------------------------


def test_parity_uses_discounted_strike():
    # With a real rate and ~10 days to expiry, ATM C − P must equal S(1 − df),
    # not 0. A pair built for df=1 (C=P) should now read as a parity violation.
    bar = datetime(2024, 1, 15, 10, 0, tzinfo=EXCHANGE_TZ)
    expiry = date(2024, 1, 25)
    years = (datetime(2024, 1, 25, 15, 30, tzinfo=EXCHANGE_TZ) - bar).total_seconds() / (365 * 86400)
    df = math.exp(-0.065 * years)
    target = SPOT * (1 - df)  # ≈ 44
    ce = leg(25000, "CE", 150.0, bar_ts=bar, expiry=expiry)
    pe = leg(25000, "PE", 150.0, bar_ts=bar, expiry=expiry)
    _, rejected = screen_chain_rows([ce, pe], risk_free=0.065)
    assert len(rejected) == 2  # C − P = 0 vs target ≈ 44 > band 4
    # Rebuild the pair to the discounted target → clean.
    ce2 = leg(25000, "CE", 150.0 + target / 2, bar_ts=bar, expiry=expiry)
    pe2 = leg(25000, "PE", 150.0 - target / 2, bar_ts=bar, expiry=expiry)
    accepted2, rejected2 = screen_chain_rows([ce2, pe2], risk_free=0.065)
    assert rejected2 == []
    assert len(accepted2) == 2


# --- documented blind spot ------------------------------------------------


def test_blind_spot_uniform_mispricing_preserves_parity():
    """A book shifted up by the SAME amount on both legs keeps C − P intact.

    The screen is a consistency witness, not a fair-value oracle: it cannot
    catch a uniform mispricing (e.g. a stale spot feeding both legs) because
    parity, LTP-on-book and IV all still pass. This is the named limit.
    """
    shift = 500.0
    rows = []
    for strike in (24900, 25000, 25100):
        intrinsic = SPOT - strike
        rows.append(leg(strike, "CE", 150.0 + intrinsic / 2 + shift))
        rows.append(leg(strike, "PE", 150.0 - intrinsic / 2 + shift))
    accepted, rejected = screen_chain_rows(rows, risk_free=0.0)
    assert rejected == []  # blind spot: every leg passes despite +500 junk
    assert len(accepted) == 6


def test_blind_spot_unpaired_leg_skips_parity():
    # A lone CE with no PE partner cannot be parity-screened; only (b)/(c) apply.
    rows = [leg(25000, "CE", 9999.0)]  # absurd premium, but on-book & sane iv
    accepted, rejected = screen_chain_rows(rows, risk_free=0.0)
    assert rejected == []
    assert len(accepted) == 1