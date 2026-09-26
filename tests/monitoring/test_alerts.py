"""Alert lifecycle: raise → escalate → (clearing) → resolve, plus ack."""

from __future__ import annotations

from backtest.monitoring import Alert, AlertBook


def _a(key="greeks:gamma:portfolio", sev="warning", cat="greeks"):
    return Alert(key=key, category=cat, severity=sev, title=key, message="m")


def _events(evs):
    return [e["event"] for e in evs]


def test_raise_once_then_count_occurrences():
    book = AlertBook(resolve_after=1)
    assert _events(book.reconcile("all", [_a()])) == ["raised"]
    assert book.reconcile("all", [_a()]) == []
    (rec,) = book.active("all")
    assert rec["occurrences"] == 2 and rec["status"] == "active"


def test_escalation_resets_ack_but_dips_do_not_re_page():
    book = AlertBook(resolve_after=1)
    book.reconcile("all", [_a(sev="warning")])
    book.acknowledge(book.active("all")[0]["id"])
    assert _events(book.reconcile("all", [_a(sev="critical")])) == ["escalated"]
    assert book.active("all")[0]["acknowledged"] is False
    book.acknowledge(book.active("all")[0]["id"])
    # Hovering at the threshold: critical → warning → critical is one lifetime.
    assert book.reconcile("all", [_a(sev="warning")]) == []
    assert book.reconcile("all", [_a(sev="critical")]) == []
    rec = book.active("all")[0]
    assert rec["acknowledged"] is True and rec["peak_severity"] == "critical"


def test_hysteresis_resolve_after():
    book = AlertBook(resolve_after=3)
    book.reconcile("all", [_a()])
    assert book.reconcile("all", []) == []
    assert book.active("all")[0]["misses"] == 1  # UI shows "clearing"
    assert book.reconcile("all", []) == []
    assert _events(book.reconcile("all", [])) == ["resolved"]
    assert book.active("all") == []
    assert book.history("all")[0]["status"] == "resolved"


def test_reappearing_resets_misses():
    book = AlertBook(resolve_after=2)
    book.reconcile("all", [_a()])
    book.reconcile("all", [])
    book.reconcile("all", [_a()])
    assert book.active("all")[0]["misses"] == 0
    assert book.reconcile("all", []) == []  # needs 2 fresh misses again


def test_categories_scope_resolution():
    book = AlertBook(resolve_after=1)
    book.reconcile("all", [_a(), _a(key="correlation:pair:A|B", cat="correlation")])
    evs = book.reconcile("all", [], categories=["greeks"])
    assert [e["key"] for e in evs] == ["greeks:gamma:portfolio"]
    assert [r["key"] for r in book.active("all")] == ["correlation:pair:A|B"]


def test_scopes_are_independent():
    book = AlertBook(resolve_after=1)
    book.reconcile("paper", [_a()])
    book.reconcile("live", [])
    assert len(book.active("paper")) == 1


def test_ack_unknown_and_counts():
    book = AlertBook(resolve_after=1)
    assert book.acknowledge("nope") is None
    book.reconcile("all", [_a(sev="critical"), _a(key="k2", sev="warning"),
                           _a(key="k3", sev="info")])
    c = book.counts("all")
    # Info findings are not ackable, so they never count as unacknowledged.
    assert (c["critical"], c["warning"], c["info"], c["unacknowledged"]) == (1, 1, 1, 2)
