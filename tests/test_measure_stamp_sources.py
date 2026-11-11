"""T0.5-1's measurement tool (impl plan Phase 0, measurement 1): which stamp
tracks receipt and which tracks exchange prints. The tool reports; the tests
pin the computation and the verdict rule (comparison, never a cutoff).
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "tools" / "out"
DAY = "2026-01-03"


def _load():
    spec = importlib.util.spec_from_file_location(
        "measure_stamp_sources", TOOLS / "measure_stamp_sources.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def tool():
    return _load()


def _frames(with_last_trade=True):
    """3 frames: ltp holds once then prints once.

    exchange_ts sits inside the receipt band but moves every frame (a frame
    clock); last_trade_ts is far from receipt and moves only when ltp moves
    (a print clock).
    """
    rows = []
    for i, ltp in enumerate((100.0, 100.0, 100.5)):
        row = {
            "kind": "packet", "token": "T1", "ltp": ltp,
            "recv_utc": f"2026-01-03T09:15:0{i}+00:00",
            "exchange_ts": f"2026-01-03T09:15:0{i}.100000+00:00",
            "bids": [[99.0, 1]], "asks": [[101.0, 1]], "bid": 99.0, "ask": 101.0,
        }
        if with_last_trade:
            # only frames 2 and 3 carry a NEW print time — all out of the band
            minute = "09:14:30" if i < 2 else "09:14:59"
            row["last_trade_ts"] = f"2026-01-03T{minute}+00:00"
        rows.append(row)
    return rows


def _tape(tmp_path, rows):
    path = tmp_path / f"feed_upstox_{DAY}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n",
                    encoding="utf-8")
    return path


def test_the_verdict_separates_the_frame_clock_from_the_print_clock(
    tool, tmp_path, capsys
):
    path = _tape(tmp_path, _frames())
    stats = tool.scan(str(path))
    assert stats["frames"] == 3
    assert stats["band"]["exchange_ts"] == 3     # within ±0.4 s of receipt
    assert stats["band"]["last_trade_ts"] == 0   # the exchange print clock
    assert tool._print_lock("last_trade_ts", stats) == 1.0  # moves with ltp
    assert tool._print_lock("exchange_ts", stats) == 0.5    # moves every frame

    rc = tool.report(DAY, "upstox", str(path), str(tmp_path / "stamps.txt"))
    out = capsys.readouterr().out
    assert rc == 0
    assert "verdict: exchange_ts tracks receipt (±0.4 s band)" in out
    assert "verdict: last_trade_ts tracks exchange prints" in out
    assert (tmp_path / "stamps.txt").exists()


def test_a_pre_t050_tape_says_so_instead_of_guessing(tool, tmp_path, capsys):
    path = _tape(tmp_path, _frames(with_last_trade=False))
    tool.report(DAY, "upstox", str(path), "")
    out = capsys.readouterr().out
    assert "before T0.5-0 cannot answer this" in out
    assert "no last_trade_ts on 3 frames" in out


def test_a_missing_tape_is_reported_not_crashed(tool, tmp_path, capsys):
    rc = tool.report(DAY, "upstox", str(tmp_path / "nope.jsonl"), "")
    assert rc == 1
    assert "no tape" in capsys.readouterr().out
