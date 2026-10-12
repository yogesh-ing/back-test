"""Tests for the mStock 502 retry path in the chunked fetch (data_manager).

Context (2026-10-03): the Data tab's ALKEM fetch reported "done, 0 failed"
while mStock's gateway 502'd ~half of the 41 chunk requests at the loop's
0.15 s cadence — manual re-probes seconds later returned the bars fine.
``_get_historical_with_retry`` must recover from transient 502/503/504, and
chunks that die even after retries must be counted, never silently swallowed.
"""

from __future__ import annotations

import pytest

from backtest.api import data_manager as dm


class _Resp:
    def __init__(self, status: int = 200, payload: dict | None = None):
        self.status_code = status
        self._payload = payload if payload is not None else {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise dm.requests.HTTPError(f"HTTP {self.status_code}", response=self)

    def json(self) -> dict:
        return self._payload


def _candles(n: int = 1) -> dict:
    return {"data": {"candles": [["t", 1.0, 2.0, 3.0, 4.0, 5] for _ in range(n)]}}


# ---------------------------------------------------------------------------
# _get_historical_with_retry
# ---------------------------------------------------------------------------


def test_transient_502_is_retried_and_recovers(monkeypatch):
    calls = {"n": 0}

    def fake_get(url, headers=None, params=None, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            return _Resp(502)
        return _Resp(200, _candles())

    slept: list[float] = []
    monkeypatch.setattr(dm.requests, "get", fake_get)
    monkeypatch.setattr(dm.time, "sleep", lambda s: slept.append(s))

    resp = dm._get_historical_with_retry("u", {}, {})
    assert resp.status_code == 200
    assert calls["n"] == 3
    assert len(slept) == 2  # backoff strictly between attempts, none after success


def test_persistent_failure_raises_after_bounded_attempts(monkeypatch):
    calls = {"n": 0}

    def fake_get(url, headers=None, params=None, timeout=None):
        calls["n"] += 1
        return _Resp(503)

    monkeypatch.setattr(dm.requests, "get", fake_get)
    monkeypatch.setattr(dm.time, "sleep", lambda s: None)

    with pytest.raises(dm.requests.RequestException) as ei:
        dm._get_historical_with_retry("u", {}, {}, attempts=3)
    assert calls["n"] == 3
    assert "503" in str(ei.value)


def test_4xx_is_not_retried(monkeypatch):
    calls = {"n": 0}

    def fake_get(url, headers=None, params=None, timeout=None):
        calls["n"] += 1
        return _Resp(401)

    monkeypatch.setattr(dm.requests, "get", fake_get)
    monkeypatch.setattr(dm.time, "sleep", lambda s: None)

    with pytest.raises(dm.requests.RequestException):
        dm._get_historical_with_retry("u", {}, {})
    assert calls["n"] == 1  # an auth failure will not fix itself — fail fast


# ---------------------------------------------------------------------------
# _fetch_bars_chunked wiring
# ---------------------------------------------------------------------------


def test_chunk_loop_counts_chunks_that_die_even_after_retries(monkeypatch):
    calls = {"n": 0}

    def fake_get(url, headers=None, params=None, timeout=None):
        calls["n"] += 1
        if params["from"] == "2026-06-01":
            return _Resp(502)  # first chunk always fails (retries exhausted)
        return _Resp(200, _candles(2))

    monkeypatch.setattr(dm.requests, "get", fake_get)
    monkeypatch.setattr(dm.time, "sleep", lambda s: None)

    bars, errors = dm._fetch_bars_chunked(
        "key", "tok", "11703", "2026-06-01", "2026-06-07", "NSE", "minute", 2
    )
    assert errors == 1  # surfaced, not silent
    assert len(bars) == 4  # the two surviving chunks' bars still land


def test_chunk_loop_recovers_transient_502_with_zero_errors(monkeypatch):
    seen: dict[str, int] = {}

    def fake_get(url, headers=None, params=None, timeout=None):
        key = params["from"]
        seen[key] = seen.get(key, 0) + 1
        if key == "2026-06-01" and seen[key] == 1:
            return _Resp(502)  # transient
        return _Resp(200, _candles(3))

    monkeypatch.setattr(dm.requests, "get", fake_get)
    monkeypatch.setattr(dm.time, "sleep", lambda s: None)

    bars, errors = dm._fetch_bars_chunked(
        "key", "tok", "11703", "2026-06-01", "2026-06-07", "NSE", "minute", 2
    )
    assert errors == 0
    assert len(bars) == 9  # all three chunks fully collected after the retry


# ---------------------------------------------------------------------------
# Adaptive pacing (2026-10-03): 0.5 s base, 2.0 s after 3 consecutive lost
# chunks, decay back after 3 clean ones — mStock bad hours cost less than a
# blanket pause, good hours stay fast.
# ---------------------------------------------------------------------------


def _pacing_get(fail_windows: set[str]):
    """Fake requests.get: the listed chunk windows 502 on EVERY attempt."""

    def fake_get(url, headers=None, params=None, timeout=None):
        if params["from"] in fail_windows:
            return _Resp(502)
        return _Resp(200, _candles(1))

    return fake_get


def test_healthy_run_paces_at_base(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(dm.requests, "get", _pacing_get(set()))
    monkeypatch.setattr(dm.time, "sleep", lambda s: sleeps.append(s))

    dm._fetch_bars_chunked(
        "key", "tok", "11703", "2026-06-01", "2026-06-10", "NSE", "minute", 2
    )
    assert sleeps == [dm.CHUNK_SLEEP_BASE] * 4  # no widening when all is well


def test_pace_widens_after_three_consecutive_failures_then_decays(monkeypatch):
    # 6 chunks (01,04,07,10,13,16-Jun); first three never recover.
    # workers=1 pins the completion order so the rhythm is deterministic.
    fails = {"2026-06-01", "2026-06-04", "2026-06-07"}
    sleeps: list[float] = []
    monkeypatch.setattr(dm.requests, "get", _pacing_get(fails))
    monkeypatch.setattr(dm.time, "sleep", lambda s: sleeps.append(s))

    bars, errors = dm._fetch_bars_chunked(
        "key", "tok", "11703", "2026-06-01", "2026-06-18", "NSE", "minute", 2,
        workers=1,
    )
    assert errors == 3
    assert len(bars) == 3  # chunks 4-6 collected

    pacing = [s for s in sleeps if s in (dm.CHUNK_SLEEP_BASE, dm.CHUNK_SLEEP_WIDE)]
    # chunks 1-2 fail one-by-one (below threshold) -> base; 3rd consecutive
    # failure widens; chunks 4,5 stay wide; 3rd clean success decays to base.
    assert pacing == [0.5, 0.5, 2.0, 2.0, 2.0, 0.5]


def test_chunks_are_fetched_in_parallel(monkeypatch):
    # 9 windows (01,04,...,22,25-Jun) over 2026-06-01..25; with 4 workers
    # several must be in flight at once — the old sequential loop can never
    # exceed 1.
    import threading

    state = {"cur": 0, "max": 0}
    lock = threading.Lock()

    def fake_get(url, headers=None, params=None, timeout=None):
        with lock:
            state["cur"] += 1
            state["max"] = max(state["max"], state["cur"])
        threading.Event().wait(0.05)  # real hold: time.sleep is patched away
        with lock:
            state["cur"] -= 1
        return _Resp(200, _candles(1))

    monkeypatch.setattr(dm.requests, "get", fake_get)
    monkeypatch.setattr(dm.time, "sleep", lambda s: None)

    bars, errors = dm._fetch_bars_chunked(
        "key", "tok", "11703", "2026-06-01", "2026-06-25", "NSE", "minute", 2,
        workers=4,
    )
    assert errors == 0
    assert len(bars) == 9
    assert state["max"] >= 2  # genuinely concurrent


# ---------------------------------------------------------------------------
# Stop during retry + job-level circuit breaker (operator report, 2026-10-03:
# an mStock outage must stop the job after minutes of failure, not days, and
# Stop must not mean "finish this symbol's remaining hundreds of chunks".)
# ---------------------------------------------------------------------------


def test_stop_lands_between_retry_attempts(monkeypatch):
    calls = {"n": 0}
    flags = {"cancel": False}

    def fake_get(url, headers=None, params=None, timeout=None):
        calls["n"] += 1
        if calls["n"] >= 2:
            flags["cancel"] = True
        return _Resp(502)

    monkeypatch.setattr(dm.requests, "get", fake_get)
    monkeypatch.setattr(dm.time, "sleep", lambda s: None)

    with pytest.raises(dm.FetchCancelled):
        dm._get_historical_with_retry(
            "u", {}, {}, attempts=4, should_cancel=lambda: flags["cancel"]
        )
    assert calls["n"] == 2  # abandoned at the next attempt, not after all 4


def test_cancelled_chunk_is_not_counted_as_error(monkeypatch):
    calls = {"n": 0}
    flags = {"cancel": False}

    def fake_get(url, headers=None, params=None, timeout=None):
        calls["n"] += 1
        flags["cancel"] = True  # Stop pressed during the very first request
        return _Resp(502)

    monkeypatch.setattr(dm.requests, "get", fake_get)
    monkeypatch.setattr(dm.time, "sleep", lambda s: None)

    bars, errors = dm._fetch_bars_chunked(
        "key", "tok", "11703", "2026-06-01", "2026-06-10", "NSE", "minute", 2,
        should_cancel=lambda: flags["cancel"],
        workers=1,
    )
    assert errors == 0  # user-chosen abandonment, not mStock failure
    assert bars == []
    # One request in flight, abandoned at the very next retry check; every
    # remaining window short-circuits on the cancel flag without a request.
    assert calls["n"] == 1


def test_breaker_needs_both_failure_count_and_duration():
    t = {"now": 0.0}
    b = dm._CircuitBreaker(min_failures=8, stall_seconds=180.0, clock=lambda: t["now"])
    for i in range(8):
        t["now"] = i * 10.0  # 70 s of failures: many, but below the stall window
        b.record(False)
    assert not b.tripped
    t["now"] = 200.0
    b.record(False)
    assert b.tripped  # >= 8 failures sustained >= 180 s with no clean chunk


def test_breaker_clean_chunk_resets_the_streak():
    t = {"now": 0.0}
    b = dm._CircuitBreaker(min_failures=3, stall_seconds=10.0, clock=lambda: t["now"])
    for i in range(3):
        t["now"] = i * 100.0
        b.record(False)
        b.record(True)  # the storm keeps landing occasional clean chunks
    assert not b.tripped  # a recovering gateway must never abort the job
    assert b.consecutive_failures == 0


def test_chunk_loop_stops_firing_requests_once_breaker_trips(monkeypatch):
    t = {"now": 0.0}
    breaker = dm._CircuitBreaker(min_failures=3, stall_seconds=0.0, clock=lambda: t["now"])
    calls = {"n": 0}

    def fake_get(url, headers=None, params=None, timeout=None):
        calls["n"] += 1
        t["now"] += 100.0
        return _Resp(502)  # full outage: every attempt fails

    monkeypatch.setattr(dm.requests, "get", fake_get)
    monkeypatch.setattr(dm.time, "sleep", lambda s: None)

    # 5 windows (01,04,07,10,13-Jun); the breaker trips inside window 3, so
    # windows 4-5 must short-circuit WITHOUT touching the network.
    bars, errors = dm._fetch_bars_chunked(
        "key", "tok", "11703", "2026-06-01", "2026-06-14", "NSE", "minute", 2,
        workers=1,
        breaker=breaker,
    )
    assert breaker.tripped
    assert calls["n"] == 12  # 3 fully-retried windows × 4 attempts, then silence
    assert errors == 3
    assert bars == []

