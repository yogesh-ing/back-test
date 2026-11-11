"""The shadow-day scorecard tool (plan §5 Phase 4) and the shared coverage
vocabulary it runs on (plan-review §10.4). The tool is stdlib-only and reads
the day's tapes; these tests feed it synthetic days and pin every split rule.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools" / "out"
DAY = "2026-01-02"


def _load(name):
    spec = importlib.util.spec_from_file_location(name[:-3], TOOLS / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def scorecard():
    return _load("score_shadow_day.py")


def _packet(bids, asks, bid=None, ask=None):
    return {
        "kind": "packet", "token": "T1",
        "recv_utc": f"{DAY}T09:15:00+00:00",
        "bids": bids, "asks": asks, "bid": bid, "ask": ask, "ltp": 100.5,
    }


@pytest.fixture()
def day_dir(tmp_path):
    """One synthetic session: primary 4 frames (2 two-sided of which 1 is
    crossed, 1 one-sided, 1 no-book); witness 2 frames (1 two-sided,
    1 one-sided). Plus the recorder rows S3-S7 classify."""
    upstox = [
        _packet([[100.0, 1]], [[101.0, 1]], 100.0, 101.0),      # two-sided
        _packet([[102.0, 1]], [[101.0, 1]], 102.0, 101.0),      # crossed
        _packet([[100.0, 1]], [], 100.0, None),                 # one-sided
        _packet([], [], None, None),                            # no book
    ]
    mstock = [
        _packet([[99.0, 1]], [[100.0, 1]], 99.0, 100.0),        # two-sided
        _packet([], [[100.0, 1]], None, 100.0),                 # one-sided
    ]
    shadow = [
        # S4: one self-consistent vote, one impossible, one unexplained
        {"kind": "sanity_flag", "reason": "cross_broker_l1", "mode": "shadow",
         "bids": [[100.0, 1]], "asks": [[101.0, 1]], "bid": 100.0, "ask": 101.0},
        {"kind": "sanity_flag", "reason": "cross_broker_l1", "mode": "shadow",
         "bids": [[102.0, 1]], "asks": [[101.0, 1]], "bid": 102.0, "ask": 101.0},
        {"kind": "sanity_flag", "reason": "cross_broker_l1", "mode": "shadow"},
        # S3: one refusal the venue caused, one honest empty book
        {"kind": "sanity_flag", "reason": "pair_parity", "mode": "enforce",
         "bids": [[100.0, 1]], "asks": [[101.0, 1]], "bid": 100.0, "ask": 101.0},
        {"kind": "sanity_flag", "reason": "pair_parity", "mode": "enforce"},
        # S5/S7: a trip with a differing peer, an ok fill the peer matches,
        # and a witness-less evaluation
        {"kind": "peer_mark", "decision": "trip", "side": "BUY",
         "symbol": "NIFTY_PE", "fill_price": 105.0, "mark": 100.0,
         "peer": {"broker": "mstock", "bid": 102.0, "ask": 105.5, "ltp": 104.0}},
        {"kind": "peer_mark", "decision": "ok", "side": "BUY",
         "symbol": "NIFTY_CE", "fill_price": 50.0, "mark": 50.0,
         "peer": {"broker": "mstock", "bid": 49.5, "ask": 50.0, "ltp": 49.8}},
        {"kind": "peer_mark", "decision": "ok", "side": "SELL",
         "symbol": "NIFTY_PE", "fill_price": 30.0, "mark": 30.0, "peer": None},
    ]

    def write(name, rows):
        path = tmp_path / f"feed_{name}_{DAY}.jsonl"
        path.write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
        )

    write("upstox", upstox)
    write("mstock", mstock)
    write("shadow", shadow)
    (tmp_path / "app.log").write_text(
        "EXIT_COLLAR_SHADOW structure=x leg=NIFTY_PE\n", encoding="utf-8"
    )
    return tmp_path


def test_the_coverage_vocabulary_is_shared_with_the_cadence_tool(day_dir):
    ladders = _load("count_impossible_ladders.py")
    stats, worst = ladders.scan(str(day_dir / f"feed_upstox_{DAY}.jsonl"))
    assert stats["two_sided"] == 2
    assert stats["one_sided"] == 1
    assert stats["no_book"] == 1
    assert stats["impossible_packets"] == 1 and len(worst) == 1
    assert "empty_ladder" not in stats  # the drift is gone


def test_s1_compares_both_sides_of_the_same_session(scorecard, day_dir, capsys):
    scorecard.score(DAY, "upstox", "mstock", str(day_dir), "")
    out = capsys.readouterr().out
    assert "two_sided/frames" in out
    assert "verdict: primary 0.500 vs mstock baseline 0.500 -> PASS" in out


def test_s3_and_s4_split_by_proof_not_by_threshold(scorecard, day_dir, capsys):
    scorecard.score(DAY, "upstox", "mstock", str(day_dir), "")
    out = capsys.readouterr().out
    assert "venue-attributable" in out and "target 0" in out
    assert "venue basis (venues differ)" in out
    assert "garbage (own ladder impossible)" in out
    assert "unexplained" in out
    # one each: a self-consistent vote, a crossed one, an empty one
    lines = [ln for ln in out.splitlines() if "venue basis (venues differ)" in ln
             or "garbage (own ladder impossible)" in ln or "unexplained" in ln]
    assert all("     1 / 3" in ln for ln in lines), lines
    # S3: one venue refusal, one empty book out of two enforced
    s3 = [ln for ln in out.splitlines() if "venue-attributable" in ln
          or "empty book (allowed)" in ln]
    assert all("     1 / 2" in ln for ln in s3), s3


def test_s5_uses_the_log_denominator_and_s7_compares_the_same_side(
    scorecard, day_dir, capsys
):
    scorecard.score(DAY, "upstox", "mstock", str(day_dir),
                    str(day_dir / "app.log"))
    out = capsys.readouterr().out
    assert "trips journaled / trips logged" in out
    assert "     1 / 1" in out  # the trip has its peer mark
    # S7: only the BUY cover whose peer ask (105.5) differs from 105.0 counts;
    # the matched 50.0 pair and the witness-less row do not.
    assert "would have priced differently" in out
    assert "     1 / 2" in out
    assert "NIFTY_PE" in out and "peer_ask=105.5" in out
