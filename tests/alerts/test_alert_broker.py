"""AlertBroker — pub/sub, dedupe, lifecycle (PRD: Portfolio Intelligence & Alerts).

The broker is the information backbone: the platform publishes, strategies
subscribe, the trader dismisses/reviews. These tests pin the lifecycle rules
the widget and the strategies rely on:

* one alert per condition (dedupe on ``type:subject``), updated in place;
* subscribers notified on creation and on escalation only — never spammed;
* a failing subscriber never blocks the others (and the failure is recorded);
* auto-resolve when the condition clears, with a resolve callback delivered
  outside the broker lock;
* event alerts expire after their TTL; ignored non-critical alerts auto-dismiss
  after an hour, critical ones never silently vanish.
"""

from __future__ import annotations

import threading
from datetime import timedelta

import pytest

from backtest.alerts import AlertBroker, AlertType, Severity, get_alert_broker, reset_alert_broker
from backtest.alerts.alert_broker import AlertBroker as AliasBroker
from backtest.alerts.alert_broker import alert_broker
from backtest.alerts.catalog import CATALOG, context_for
from backtest.alerts.types import Alert, utcnow

GAMMA = AlertType.PORTFOLIO_GAMMA_CRITICAL.value
DELTA = AlertType.PORTFOLIO_DELTA_WARNING.value
OI = AlertType.OI_ANOMALY.value


@pytest.fixture()
def broker() -> AlertBroker:
    return AlertBroker(renotify_cooldown_s=60.0)


def test_prd_module_path_and_singleton():
    assert AliasBroker is AlertBroker
    b = reset_alert_broker()
    assert get_alert_broker() is b
    assert alert_broker() is b


def test_every_alert_type_has_catalog_context():
    for t in AlertType:
        ctx = context_for(t.value)
        assert ctx["title"] and ctx["what_it_means"] and ctx["typical_responses"]
        assert ctx["section"].startswith("pi-")
    assert set(CATALOG) == {t.value for t in AlertType}


def test_str_enum_compares_equal_to_plain_string():
    assert AlertType.PORTFOLIO_GAMMA_CRITICAL == "portfolio_gamma_critical"
    assert str(Severity.CRITICAL) == "critical"


def test_subscribe_publish_delivers_type_and_data(broker):
    got = []
    broker.subscribe(GAMMA, lambda t, d: got.append((t, d)), subscriber_id="s1")
    alert, created = broker.raise_alert(GAMMA, "critical", "gamma -1247", data={"net_gamma": -1247})
    assert created
    assert got == [(GAMMA, {"net_gamma": -1247, "alert_id": alert.alert_id,
                            "severity": "critical", "message": "gamma -1247"})]
    assert alert.notified_strategies[0]["subscriber_id"] == "s1"
    assert alert.notified_strategies[0]["ok"] is True


def test_prd_publish_api_accepts_alert_object(broker):
    got = []
    broker.subscribe(DELTA, lambda t, d: got.append(t))
    published = broker.publish(Alert(alert_type=DELTA, severity="warning", message="Δ 900"))
    assert got == [DELTA]
    assert broker.get(published.alert_id) is published


def test_dedupe_updates_in_place_and_notifies_once(broker):
    calls = []
    broker.subscribe(GAMMA, lambda t, d: calls.append(d["net_gamma"]))
    a1, c1 = broker.raise_alert(GAMMA, "critical", "m1", data={"net_gamma": -1100})
    a2, c2 = broker.raise_alert(GAMMA, "critical", "m2", data={"net_gamma": -1300})
    assert c1 and not c2
    assert a1 is a2
    assert a1.occurrences == 2
    assert a1.message == "m2" and a1.data["net_gamma"] == -1300
    assert calls == [-1100]
    assert len(broker.get_active_alerts()) == 1


def test_escalation_renotifies_and_resurfaces_a_dismissed_alert(broker):
    calls = []
    broker.subscribe(DELTA, lambda t, d: calls.append(d["severity"]))
    alert, _ = broker.raise_alert(DELTA, "warning", "Δ 850")
    broker.dismiss(alert.alert_id)
    assert broker.get_active_alerts() == []
    broker.raise_alert(DELTA, "critical", "Δ 2000")
    assert calls == ["warning", "critical"]
    assert [a.alert_id for a in broker.get_active_alerts()] == [alert.alert_id]


def test_failing_callback_does_not_break_other_subscribers(broker):
    got = []

    def boom(t, d):
        raise RuntimeError("strategy bug")

    broker.subscribe(GAMMA, boom, subscriber_id="bad", meta={"runner": "Bad"})
    broker.subscribe(GAMMA, lambda t, d: got.append(t), subscriber_id="good")
    alert, _ = broker.raise_alert(GAMMA, "critical", "x")
    assert got == [GAMMA]
    results = {n["subscriber_id"]: n for n in alert.notified_strategies}
    assert results["bad"]["ok"] is False and "strategy bug" in results["bad"]["error"]
    assert results["good"]["ok"] is True
    assert broker.callback_failures == 1


def test_any_known_type_can_be_subscribed_by_strategies(broker):
    """AUDIENCE is display-only: a trader-primary alert still reaches subscribers."""
    got = []
    broker.subscribe(AlertType.DATA_FEED_STALE.value, lambda t, d: got.append(t))
    broker.raise_alert(AlertType.DATA_FEED_STALE.value, "critical", "stale", subject="mstock")
    assert got == ["data_feed_stale"]


def test_unsubscribe_by_subscriber_id(broker):
    got = []
    broker.subscribe(GAMMA, lambda t, d: got.append(1), subscriber_id="r1")
    broker.subscribe(DELTA, lambda t, d: got.append(2), subscriber_id="r1")
    assert broker.unsubscribe("r1") == 2
    broker.raise_alert(GAMMA, "critical", "x")
    assert got == []
    assert broker.subscribers_for(GAMMA) == []


def test_resolve_missing_resolves_cleared_subjects_and_calls_on_resolve(broker):
    resolved = []
    broker.subscribe(
        AlertType.CONCENTRATION_HIGH.value,
        lambda t, d: None,
        subscriber_id="r1",
        on_resolve=lambda t, d: resolved.append((t, d["resolved"], d["resolution"])),
    )
    broker.raise_alert("concentration_high", "warning", "NIFTY 78%", subject="NIFTY")
    broker.raise_alert("concentration_high", "warning", "BANKNIFTY 65%", subject="BANKNIFTY")
    done = broker.resolve_missing("concentration_high", ["BANKNIFTY"])
    assert [a.subject for a in done] == ["NIFTY"]
    assert resolved == [("concentration_high", True, "cleared")]
    assert {a.subject for a in broker.get_active_alerts()} == {"BANKNIFTY"}
    # A later re-detection is a NEW alert (new id), not a zombie of the old one.
    again, created = broker.raise_alert(
        "concentration_high", "warning", "NIFTY 70%", subject="NIFTY"
    )
    assert created and again.alert_id != done[0].alert_id


def test_on_resolve_runs_outside_the_broker_lock(broker):
    """A strategy callback takes its runner lock — it must never run while the
    broker lock is held (lock-order inversion with the feed thread)."""
    observed = []

    def on_resolve(t, d):
        # RLock: the *same* thread could re-acquire even if held, so probe
        # from another thread (acquire + release there).
        result = []

        def probe():
            ok = broker._lock.acquire(timeout=0.5)
            if ok:
                broker._lock.release()
            result.append(ok)

        th = threading.Thread(target=probe)
        th.start()
        th.join()
        observed.append(result[0])

    broker.subscribe(GAMMA, lambda t, d: None, subscriber_id="r1", on_resolve=on_resolve)
    broker.raise_alert(GAMMA, "critical", "x")
    broker.resolve_key(GAMMA)
    assert observed == [True]


def test_dismiss_hides_but_keeps_the_alert_open(broker):
    alert, _ = broker.raise_alert(DELTA, "warning", "Δ 900")
    broker.dismiss(alert.alert_id, by="trader")
    assert alert.status == "dismissed"
    assert broker.get_active_alerts() == []
    assert broker.get_active_alerts(include_dismissed=True) == [alert]
    # still open → a refresh does not create a duplicate
    _, created = broker.raise_alert(DELTA, "warning", "Δ 910")
    assert not created
    assert broker.counts()["total"] == 0


def test_review_archives_and_manual_resolve(broker):
    a, _ = broker.raise_alert(DELTA, "warning", "x")
    broker.review(a.alert_id)
    assert a.status == "reviewed"
    b, _ = broker.raise_alert(GAMMA, "critical", "y")
    broker.resolve_alert(b.alert_id)
    assert b.status == "resolved" and b.resolved
    assert broker.resolve_alert("nope") is None


def test_event_alerts_expire_after_ttl(broker):
    broker.event_ttl_s = 3600
    a, _ = broker.raise_alert(OI, "info", "OI spike", subject="NIFTY:23500CE")
    assert broker.sweep(now=utcnow() + timedelta(minutes=30))["expired"] == 0
    out = broker.sweep(now=utcnow() + timedelta(hours=1, seconds=1))
    assert out["expired"] == 1
    assert a.resolved and a.data["resolution"] == "expired"


def test_ignored_alerts_auto_dismiss_after_an_hour_but_critical_is_exempt(broker):
    warn, _ = broker.raise_alert(DELTA, "warning", "Δ 900")
    crit, _ = broker.raise_alert(GAMMA, "critical", "γ")
    # A state alert that keeps being re-observed still auto-dismisses by age.
    broker.raise_alert(DELTA, "warning", "Δ 905")
    out = broker.sweep(now=utcnow() + timedelta(hours=1, minutes=1))
    assert out["auto_dismissed"] == 1
    assert warn.dismissed_by == "auto"
    assert crit.status == "active"
    assert [a.alert_id for a in broker.get_active_alerts()] == [crit.alert_id]


def test_renotify_cooldown_guards_against_flapping(broker):
    calls = []
    broker.subscribe(DELTA, lambda t, d: calls.append(1))
    broker.raise_alert(DELTA, "warning", "x")
    broker.resolve_key(DELTA)
    broker.raise_alert(DELTA, "warning", "x again")  # inside 60s cooldown
    assert calls == [1]
    broker.renotify_cooldown_s = 0
    broker.resolve_key(DELTA)
    broker.raise_alert(DELTA, "warning", "x third")
    assert calls == [1, 1]


def test_active_alerts_sorted_most_severe_first_and_counts(broker):
    broker.raise_alert(OI, "info", "i", subject="a")
    broker.raise_alert(DELTA, "warning", "w")
    broker.raise_alert(GAMMA, "critical", "c")
    assert [a.severity for a in broker.get_active_alerts()] == ["critical", "warning", "info"]
    assert broker.counts() == {"total": 3, "critical": 1, "warning": 1, "info": 1}


def test_history_filters_and_version_bumps(broker):
    v0 = broker.version
    broker.raise_alert(DELTA, "warning", "w")
    broker.raise_alert(GAMMA, "critical", "c")
    assert broker.version > v0
    assert [a.alert_type for a in broker.history(alert_type="gamma")] == [GAMMA]
    assert [a.severity for a in broker.history(severity="warning")] == ["warning"]
    assert broker.history(since=utcnow() + timedelta(minutes=1)) == []


def test_listeners_receive_lifecycle_events_and_failures_are_swallowed(broker):
    events = []
    broker.add_listener(lambda e, a: events.append(e))
    broker.add_listener(lambda e, a: 1 / 0)
    a, _ = broker.raise_alert(DELTA, "warning", "w")
    broker.raise_alert(DELTA, "warning", "w2")
    broker.dismiss(a.alert_id)
    broker.resolve_alert(a.alert_id)
    assert events == ["created", "updated", "dismissed", "resolved"]


def test_to_dict_is_json_ready():
    import json

    a = Alert(alert_type=GAMMA, severity="critical", message="m", data={"x": 1})
    d = a.to_dict()
    json.dumps(d)
    assert d["status"] == "active" and d["audience"] == ["trader", "strategies"]
    assert d["is_event"] is False
