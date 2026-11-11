"""The shadow cross-broker witness: it mirrors, it records, it prices nothing.

Item F of ``docs/OPTION-JUNK-ASK-INCIDENT-2026-11-08.md``. The second broker sits
beside the trading path as a tape, and now answers exactly one question from
the quote seam — "what is your own L1 for this contract right now" — which the
``cross_broker_l1`` gate may use to refuse a price, never to make one. The
contract these tests hold: right keys subscribed, one honest row per contract per
sweep, no path from this module into an entry, an exit or a mark, and no effect on
the mStock view.
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

    #: Plays the mStock venue — rows derive their primary label from this (B4).
    broker_name = "mstock"
    source_name = "live:mstock"

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
    assert row["secondary_key"] == "NSE_FNO|24900CE"
    assert row["symbol"] == "NIFTY26NOV24900CE"
    assert row["expiry"] == "2026-11-23"
    # Roles + the actual venue names (B5) — no mstock/upstox literals.
    assert row["primary_venue"] == "mstock"
    assert row["secondary_venue"] == "upstox"
    assert row["primary"]["ltp"] == 300.0 and row["primary"]["ladder_status"] == "ok"
    assert row["secondary"]["ltp"] == 221.65
    # The number the whole exercise is about: primary minus secondary.
    assert row["delta"]["ltp"] == pytest.approx(78.35)
    assert row["delta"]["ask"] == pytest.approx(78.80)
    assert row["secondary_connected"] is True
    assert "mstock" not in row and "upstox" not in row


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


def test_a_primary_never_mirrors_itself_in_either_direction(monkeypatch, caplog):
    """B5, symmetric: primary never mirrors itself — both directions.

    A witness that compares a broker with itself always reports agreement, so
    the refusal is an equality of venue names, not an Upstox special case:
    mstock/mstock and upstox/upstox are refused alike whichever side names the
    pair, and only a cross-venue pair may mirror.
    """
    caplog.set_level(logging.INFO, logger=shadow.logger.name)
    monkeypatch.setenv("ENABLE_FEED", "true")
    shadow.reset()
    provider = _provider({"64435": _row(300.0, 299.5, 300.5)})

    # Direction 1 (reachable today): an Upstox primary against the Upstox
    # mirror — refused before anything is constructed.
    assert shadow.maybe_start(provider, "NIFTY", source="upstox") is None
    assert "SHADOW_SELF_MIRROR" in caplog.text
    assert "primary=upstox" in caplog.text and "witness=upstox" in caplog.text
    assert shadow.active() == {}

    # The rule itself, both directions: same venue refused, cross venue allowed.
    assert shadow.mirrors_itself("upstox", "upstox") is True
    assert shadow.mirrors_itself("mstock", "mstock") is True
    assert shadow.mirrors_itself("mstock", "upstox") is False
    assert shadow.mirrors_itself("upstox", "mstock") is False
    # Direction 2: an mStock primary would refuse an mStock witness just the
    # same — the rule is the pair's equality, not the mirror's venue.
    assert shadow.mirrors_itself("mstock", shadow.MIRROR_VENUE) is False
    assert shadow.mirrors_itself("upstox", shadow.MIRROR_VENUE) is True


def test_the_mirror_off_when_capture_is_off(monkeypatch):
    assert shadow.shadow_enabled({}) is False
    assert shadow.shadow_enabled({"ENABLE_FEED": "true"}) is True
    assert shadow.shadow_enabled({"ENABLE_FEED": "true", "SHADOW_UPSTOX_DEPTH": "0"}) is False


# -- the shadow guarantee ---------------------------------------------------

TRADE_PATHS = [
    "src/backtest/forward/options_bridge.py",
    "src/backtest/options/exit_policy.py",
    "src/backtest/options/execution_gate.py",
]

#: The seam is the ONE trading path allowed to know the witness exists (item F
#: promoted to the ``cross_broker_l1`` gate, 2026-11-10), and only to ask it a
#: question — never to drive it or to price a leg with it.
CORROBORATING_PATH = "src/backtest/options/quote_providers.py"
#: Attributes of the mirror the seam may read. ``sweep``/``reconcile``/``start``
#: belong to the recorder's own loop; a pricing path that called them could open
#: a socket, spend Upstox HTTP, or rewrite the tape.
ALLOWED_MIRROR_ATTRS = {"active", "l1_for"}


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
        "corroboration lives in the quote seam, where every strategy passes — not "
        "in an exit policy that one future strategy would forget to call"
    )


def test_the_seam_asks_the_witness_but_never_drives_it():
    """``peer_l1`` is the only hand the fill seam has on the mirror."""
    repo_root = Path(shadow.__file__).resolve().parents[3]
    path = repo_root / CORROBORATING_PATH
    deps = _module_dependencies(path)
    reached = {
        name.split(".", 1)[1]
        for name in deps
        if "cross_broker_shadow" in name or name.startswith("mirror.")
    }
    assert reached <= ALLOWED_MIRROR_ATTRS, (
        f"the quote seam reaches {sorted(reached - ALLOWED_MIRROR_ATTRS)} on the "
        "mirror; it may read a peer's L1, never sweep, subscribe or start a socket"
    )


def test_sweeping_never_changes_the_mstock_view():
    rows = {"64435": _row(300.0, 299.5, 300.5)}
    provider = _provider(rows)
    before = json.dumps(provider.get_quote("64435"), sort_keys=True, default=str)
    instance = _shadow(provider, FakeWrapper({"NSE_FNO|24900CE": _row(1.0, 1.0, 1.0)}))
    instance.reconcile()
    instance.sweep()

    assert json.dumps(provider.get_quote("64435"), sort_keys=True, default=str) == before


def test_rows_under_different_primaries_are_distinguishable(capture):
    """Risk-register tripwire: flipping the switch must not flip a row's
    meaning — the actual venue names travel with the primary/secondary roles."""
    provider_a = _provider({"64435": _row(300.0, 299.5, 300.5)})
    provider_b = _provider({"64435": _row(300.0, 299.5, 300.5)})
    provider_b.broker_name = "dhan"
    provider_b.source_name = "live:dhan"

    shadow_a = _shadow(provider_a, FakeWrapper({"NSE_FNO|24900CE": _row(1.0, 1.0, 1.0)}))
    shadow_a.reconcile()
    shadow_a.sweep()
    shadow_b = _shadow(provider_b, FakeWrapper({"NSE_FNO|24900CE": _row(1.0, 1.0, 1.0)}))
    shadow_b.reconcile()
    shadow_b.sweep()

    rows = _shadow_lines(capture)
    assert [row["primary_venue"] for row in rows] == ["mstock", "dhan"]
    assert all(row["secondary_venue"] == "upstox" for row in rows)
    assert all(row["primary"]["ltp"] == 300.0 for row in rows)


# -- the seam's question: l1_for --------------------------------------------


def test_l1_for_answers_with_the_peers_own_numbers_and_its_age():
    instance = _shadow(
        _provider({"64435": _row(300.0, 299.5, 300.5)}),
        FakeWrapper({"NSE_FNO|24900CE": _row(300.4, 299.9, 300.9)}),
    )
    instance.reconcile()
    peer = instance.l1_for("64435")
    assert peer["ask"] == 300.9 and peer["bid"] == 299.9
    assert peer["broker"] == "upstox"
    assert 0.0 <= peer["age_s"] < 5.0


def test_the_peer_row_names_its_own_venue_whichever_is_accused():
    """The ee0372b corroboration reads the *peer* — the row carries the peer's
    actual venue name, so an mStock witness reads mStock back, not a literal."""
    instance = shadow.CrossBrokerShadow(
        "NIFTY",
        _provider({"64435": _row(300.0, 299.5, 300.5)}),
        token_provider=lambda: "session-token",
        upstox_chain=lambda underlying: UP_CONTRACTS,
        wrapper_factory=lambda _tp: FakeWrapper({"NSE_FNO|24900CE": _row(300.4, 299.9, 300.9)}),
        secondary_venue="mstock",
    )
    instance.reconcile()
    peer = instance.l1_for("64435")
    assert peer["broker"] == "mstock", "the peer names itself, whichever venue it is"


def test_l1_for_is_none_before_any_socket_or_mapping_exists():
    provider = _provider({"64435": _row(300.0, 299.5, 300.5)})
    never_started = _shadow(provider, FakeWrapper())
    assert never_started.l1_for("64435") is None          # no wrapper yet
    started = _shadow(provider, FakeWrapper())
    started.reconcile()
    assert started.l1_for("64437") is None                 # unmapped on Upstox terms
    assert started.l1_for("99999") is None                 # not even in the ring


def test_l1_for_costs_mstock_nothing_and_upstox_no_http():
    provider = _provider({"64435": _row(300.0, 299.5, 300.5)})
    wrapper = FakeWrapper({"NSE_FNO|24900CE": _row(1.0, 1.0, 1.0)})
    instance = _shadow(provider, wrapper)
    instance.reconcile()
    reads = len(wrapper.started_with)
    instance.l1_for("64435")
    assert provider.quote_calls == 0                       # mStock never touched
    assert len(wrapper.started_with) == reads              # no resubscribe


def test_a_peer_row_of_zeros_is_no_answer():
    instance = _shadow(
        _provider({"64435": _row(300.0, 299.5, 300.5)}),
        FakeWrapper({"NSE_FNO|24900CE": {"ltp": 0.0, "bid": 0.0, "ask": 0.0}}),
    )
    instance.reconcile()
    peer = instance.l1_for("64435")
    assert peer["ask"] == 0.0
    # The seam abstains on a zero side, so a blank peer cannot refuse a trade:
    # verified where the decision lives, in test_option_ladder_gates.py §6.
