"""The shadow cross-broker witness: it mirrors, it records, it decides nothing.

Item F of ``docs/OPTION-JUNK-ASK-INCIDENT-2026-11-08.md`` — the second broker is
supposed to be *beside* the trading path, not inside it, so the contract these
tests hold is: right keys subscribed, one honest row per contract per sweep, and
no path from this module into an entry, an exit or a mark.
"""

from __future__ import annotations

import ast
import json
import logging
import time
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from backtest.data import cross_broker_shadow as shadow
from backtest.data.feed_depth_recorder import reset_recorder

EXPIRY = date(2026, 11, 23)


class FakeContract:
    def __init__(self, token, symbol, strike, otype, expiry=EXPIRY):
        self.instrument_token = token
        self.trading_symbol = symbol
        self.expiry = expiry
        self.strike = Decimal(str(strike))
        self.option_type = otype


class FakeRing:
    def __init__(self, tokens):
        self.tokens = list(tokens)

    def subscribed_tokens(self, underlying=None):
        return set(self.tokens)


class FakeProvider:
    """mStock's side: the hot ring plus the token → contract registry."""

    def __init__(self, contracts, rows):
        self._contracts = {c.instrument_token: c for c in contracts}
        self._rows = rows
        self.hot_ring = FakeRing(list(rows))
        self.quote_calls = 0

    def contract_for(self, token):
        return self._contracts.get(str(token))

    def get_quote(self, token):
        self.quote_calls += 1
        return self._rows.get(str(token)) or {}


class FakeWrapper:
    """Upstox's side, without a socket."""

    def __init__(self, rows=None):
        self._keys: list[str] = []
        self._rows = rows or {}
        self.started_with: list[str] = []
        self.closed = False

    def start(self, keys, mode=None):
        self.started_with = list(keys)
        self._keys = list(keys)

    def subscribed_keys(self):
        return list(self._keys)

    def subscribe(self, keys):
        self._keys = sorted(set(self._keys) | set(keys))

    def unsubscribe(self, keys):
        self._keys = [k for k in self._keys if k not in set(keys)]

    def get_quote(self, key):
        return self._rows.get(key) or {}

    def is_connected(self):
        return True

    def close(self):
        self.closed = True


def _row(ltp, bid, ask):
    mono = time.monotonic()
    return {
        "ltp": ltp,
        "bid": bid,
        "ask": ask,
        "ladder_status": "ok",
        "recv_mono": mono,
        "timestamp": "2026-11-09T06:30:00+00:00",
    }


MS_CONTRACTS = [
    FakeContract("64435", "NIFTY26NOV24900CE", 24900, "CE"),
    FakeContract("64436", "NIFTY26NOV24900PE", 24900, "PE"),
    FakeContract("64437", "NIFTY26NOV25050CE", 25050, "CE"),
]
UP_CONTRACTS = [
    FakeContract("NSE_FNO|24900CE", "NIFTY 24900 CE", 24900, "CE"),
    FakeContract("NSE_FNO|24900PE", "NIFTY 24900 PE", 24900, "PE"),
    FakeContract("NSE_FNO|25100CE", "NIFTY 25100 CE", 25100, "CE"),
]


def _provider(ms_rows):
    return FakeProvider(MS_CONTRACTS, ms_rows)


def _shadow(provider, wrapper, up_contracts=None):
    return shadow.CrossBrokerShadow(
        "NIFTY",
        provider,
        token_provider=lambda: "session-token",
        upstox_chain=lambda underlying: up_contracts if up_contracts is not None else UP_CONTRACTS,
        wrapper_factory=lambda _tokens: wrapper,
    )


# -- subscription set -------------------------------------------------------


def test_the_ring_set_is_mirrored_on_upstox_by_contract_terms():
    provider = _provider({"64435": _row(300.0, 299.5, 300.5), "64436": _row(280.0, 279.5, 280.5)})
    wrapper = FakeWrapper()
    instance = _shadow(provider, wrapper)

    keys = instance.reconcile()

    # Terms match on (expiry, strike, type): the ring's 24900 CE/PE map, and the
    # 25050 CE — which Upstox does not list — never enters the socket.
    assert keys == ["NSE_FNO|24900CE", "NSE_FNO|24900PE"]
    assert wrapper.started_with == keys


def test_an_unmapped_contract_stays_single_broker_and_says_so(caplog):
    caplog.set_level(logging.INFO, logger=shadow.logger.name)
    provider = _provider({"64437": _row(120.0, 119.5, 120.0)})
    instance = _shadow(provider, FakeWrapper())

    assert instance.reconcile() == []
    assert "SHADOW_UNMAPPED" in caplog.text
    assert "64437" in caplog.text


def test_the_mirror_follows_the_ring_as_it_recentres():
    rows = {"64435": _row(300.0, 299.5, 300.5), "64436": _row(280.0, 279.5, 280.5)}
    provider = _provider(rows)
    wrapper = FakeWrapper()
    instance = _shadow(provider, wrapper)
    instance.reconcile()

    provider.hot_ring.tokens = ["64435"]  # the ring dropped the put
    instance.reconcile()

    assert wrapper.subscribed_keys() == ["NSE_FNO|24900CE"]
    assert instance.switches >= 1


# -- the witness row --------------------------------------------------------


@pytest.fixture()
def capture(tmp_path, monkeypatch):
    """Real recorder, real JSONL file, tmp directory."""
    monkeypatch.setenv("ENABLE_FEED", "true")
    monkeypatch.setenv("ENABLE_FEED_DIR", str(tmp_path))
    reset_recorder()
    yield tmp_path
    reset_recorder()


def _shadow_lines(tmp_path: Path):
    """Close the recorder (joins its writer thread), then read what it landed."""
    reset_recorder()
    files = sorted(tmp_path.glob("feed_shadow_*.jsonl"))
    assert files, f"no shadow file written; found {list(p.name for p in tmp_path.iterdir())}"
    return [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]


def test_a_sweep_writes_both_views_and_the_delta(capture):
    ms_rows = {"64435": _row(300.0, 299.5, 300.5)}
    provider = _provider(ms_rows)
    wrapper = FakeWrapper({"NSE_FNO|24900CE": _row(221.65, 221.60, 221.70)})
    instance = _shadow(provider, wrapper)
    instance.reconcile()

    assert instance.sweep() == 1
    (row,) = _shadow_lines(capture)

    assert row["kind"] == "shadow_diff"
    assert row["broker"] == "shadow"
    assert row["token"] == "64435"
    assert row["upstox_key"] == "NSE_FNO|24900CE"
    assert row["symbol"] == "NIFTY26NOV24900CE"
    assert row["expiry"] == "2026-11-23"
    assert row["mstock"]["ltp"] == 300.0 and row["mstock"]["ladder_status"] == "ok"
    assert row["upstox"]["ltp"] == 221.65
    # The number the whole exercise is about: what mStock says minus what Upstox says.
    assert row["delta"]["ltp"] == pytest.approx(78.35)
    assert row["delta"]["ask"] == pytest.approx(78.80)
    assert row["upstox_connected"] is True


def test_a_side_never_traded_yet_deltas_as_unknown_not_zero(capture):
    provider = _provider({"64435": _row(300.0, 299.5, 300.5)})
    wrapper = FakeWrapper({"NSE_FNO|24900CE": _row(0.0, 0.0, 0.0)})
    instance = _shadow(provider, wrapper)
    instance.reconcile()
    instance.sweep()

    (row,) = _shadow_lines(capture)

    assert row["delta"]["ltp"] is None  # no Upstox print ⇒ no claim of agreement


def test_a_dead_contract_cannot_end_the_sweep(capture):
    rows = {"64435": _row(300.0, 299.5, 300.5), "64436": _row(280.0, 279.5, 280.5)}
    provider = _provider(rows)

    def explode(token):
        if token == "64435":
            raise ValueError("quote fetch failed")
        return rows[token]

    provider.get_quote = explode  # type: ignore[method-assign]
    wrapper = FakeWrapper({"NSE_FNO|24900PE": _row(280.0, 279.5, 280.5)})
    instance = _shadow(provider, wrapper)
    instance.reconcile()

    assert instance.sweep() == 1  # the put still lands
    assert "ValueError" in (instance.last_error or "")


# -- staying inert ----------------------------------------------------------


def test_no_upstox_session_parks_the_mirror_and_opens_no_socket(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=shadow.logger.name)
    monkeypatch.setenv("ENABLE_FEED", "true")
    shadow.reset()

    class NoUpstox:
        def get_broker(self, name):
            raise RuntimeError("upstox not wired")

    monkeypatch.setattr(
        "backtest.brokers.session_manager.get_session_manager", lambda: NoUpstox()
    )
    provider = _provider({"64435": _row(300.0, 299.5, 300.5)})

    instance = shadow.maybe_start(provider, "NIFTY")
    try:
        assert instance is not None
        assert "SHADOW_UPSTOX_WAITING" in caplog.text
        assert instance.reconcile() == []  # no token ⇒ no socket, no Upstox HTTP
        assert instance._wrapper is None
        assert instance.sweep() == 0
    finally:
        shadow.reset()
    assert shadow.active() == {}


def test_a_later_upstox_login_opens_the_socket_without_a_restart():
    """The operator logs in after boot, so the mirror must pick the session up live."""
    provider = _provider({"64435": _row(300.0, 299.5, 300.5)})
    wrapper = FakeWrapper()
    state = {"token": None}
    chain_calls = {"n": 0}

    def chain(_underlying):
        chain_calls["n"] += 1
        return UP_CONTRACTS

    instance = shadow.CrossBrokerShadow(
        "NIFTY",
        provider,
        token_provider=lambda: state["token"],
        upstox_chain=chain,
        wrapper_factory=lambda _tp: wrapper,
    )

    assert instance.reconcile() == []
    assert wrapper.started_with == []
    # Logged out means not even asked: the chain is a multi-MB instrument master.
    assert chain_calls["n"] == 0

    state["token"] = "a-token-logged-in-mid-session"

    assert instance.reconcile() == ["NSE_FNO|24900CE"]
    assert wrapper.started_with == ["NSE_FNO|24900CE"]
    assert chain_calls["n"] == 1


def test_an_empty_master_is_rate_limited_not_declared_final():
    provider = _provider({"64435": _row(300.0, 299.5, 300.5)})
    calls = {"n": 0}

    def chain(_underlying):
        calls["n"] += 1
        return [] if calls["n"] == 1 else UP_CONTRACTS

    instance = shadow.CrossBrokerShadow(
        "NIFTY",
        provider,
        token_provider=lambda: "session-token",
        upstox_chain=chain,
        wrapper_factory=lambda _tp: FakeWrapper(),
    )

    assert instance.reconcile() == []  # Upstox not wired yet
    assert calls["n"] == 1
    assert instance.reconcile() == []  # retried on a window, not on every pass
    assert calls["n"] == 1

    instance._terms_next_try_mono = 0.0  # the window has passed

    assert instance.reconcile() == ["NSE_FNO|24900CE"]
    assert calls["n"] == 2


def test_the_mirror_off_when_capture_is_off(monkeypatch):
    assert shadow.shadow_enabled({}) is False
    assert shadow.shadow_enabled({"ENABLE_FEED": "true"}) is True
    assert shadow.shadow_enabled({"ENABLE_FEED": "true", "SHADOW_UPSTOX_DEPTH": "0"}) is False


# -- the shadow guarantee ---------------------------------------------------

TRADE_PATHS = [
    "src/backtest/forward/options_bridge.py",
    "src/backtest/options/exit_policy.py",
    "src/backtest/options/quote_providers.py",
    "src/backtest/options/execution_gate.py",
]


def _module_dependencies(path: Path) -> set[str]:
    """Every module this file imports or reaches through a dotted attribute."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            out.add(f"{node.value.id}.{node.attr}")
    return out


@pytest.mark.parametrize("relative", TRADE_PATHS)
def test_no_trading_path_reads_the_shadow_feed(relative):
    repo_root = Path(shadow.__file__).resolve().parents[3]
    deps = _module_dependencies(repo_root / relative)

    offenders = {
        name
        for name in deps
        if "cross_broker_shadow" in name or name.endswith(("record_shadow_diff", "shadow_diff"))
    }
    assert not offenders, (
        f"{relative} now depends on the second-broker witness ({sorted(offenders)}): "
        "shadow mode means it may be recorded, never acted on"
    )


def test_sweeping_never_changes_the_mstock_view():
    rows = {"64435": _row(300.0, 299.5, 300.5)}
    provider = _provider(rows)
    before = json.dumps(provider.get_quote("64435"), sort_keys=True, default=str)
    instance = _shadow(provider, FakeWrapper({"NSE_FNO|24900CE": _row(1.0, 1.0, 1.0)}))
    instance.reconcile()
    instance.sweep()

    assert json.dumps(provider.get_quote("64435"), sort_keys=True, default=str) == before
