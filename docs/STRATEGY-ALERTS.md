# Strategy Alerts — making a strategy react to portfolio alerts

The platform watches the whole portfolio and **broadcasts** alerts (see
[ALERTS-GUIDE.md](ALERTS-GUIDE.md)). A strategy can subscribe and decide for
itself what to do. The platform never acts on an alert: every pause or exit
below is the *strategy's* decision, executed by its own runner through the
normal engine path — the same fills, slippage, audit trail and signal log as a
stop-loss.

Reference implementation: `plugins/strategies/immediate_strangle.py`.

---

## 1. Subscribe

Declare the alert types on the class (registered when the runner is added to
the portfolio manager):

```python
from backtest.alerts import AlertType
from backtest.strategy.base import Strategy


class MyShortPremium(Strategy):
    name = "my_short_premium"
    subscribed_alerts = (
        AlertType.PORTFOLIO_GAMMA_CRITICAL.value,
        AlertType.VIX_REGIME_CHANGE.value,
    )
    regime_vix_range = (10.0, 15.0)   # optional: shown as "regime fit" in the Risk Board
```

…or at runtime (re-registers immediately if the runner is live):

```python
self.subscribe_to_alerts([AlertType.DATA_FEED_STALE])   # enum members or strings
self.unsubscribe_from_alerts(["data_feed_stale"])        # None = drop all runtime subscriptions
self.alert_subscriptions()                               # class-level ∪ runtime
```

Unknown alert types raise `ValueError`. `BaseStrategy` is an alias of
`Strategy`, so PRD-style `class X(BaseStrategy)` works too. Subscriptions are
per runner instance and are removed when the runner is removed; the alert
detail modal and `GET /api/alerts/subscriptions` show who listens to what.

Alert types: `portfolio_gamma_critical`, `portfolio_delta_warning`,
`concentration_high`, `strike_clustering`, `vix_regime_change`,
`oi_anomaly`, `correlation_spike`, `liquidity_dry_up`, `data_feed_stale`.

## 2. Handle

```python
    def on_alert(self, alert_type, alert_data):
        if alert_type == AlertType.PORTFOLIO_GAMMA_CRITICAL:
            self.pause_new_entries = True                     # stop opening new positions
            mine = alert_data.get("self_contribution") or {}
            if mine.get("share", 0) >= 0.5:                   # I'm the main culprit
                self.request_exit(1.0, reason="gamma_critical")

    def on_alert_resolved(self, alert_type, alert_data):
        if alert_type == AlertType.PORTFOLIO_GAMMA_CRITICAL:
            self.pause_new_entries = False
```

`AlertType` is a `str` enum, so `alert_type == "portfolio_gamma_critical"`
also works.

### `alert_data`

Every alert's metrics (see the per-type fields below) plus:

| Key | |
|---|---|
| `alert_id`, `severity`, `message` | identity and headline |
| `contributors` | list of `{instance_id, label, strategy, mode, gamma|delta, share, positions}` where the alert has contributors |
| `self_contribution` | *this* runner's entry from `contributors`, or `None` |
| `is_contributor` | `bool` |
| `instance_id` | this runner's id |

Per-type fields:

| Alert | Fields |
|---|---|
| `portfolio_gamma_critical` | `net_gamma`, `threshold`, `breach_pct`, `move_2pct_pnl`, `net_theta`, `units` |
| `portfolio_delta_warning` | `net_delta`, `threshold` |
| `concentration_high` | `underlying`, `pct`, `threshold_pct`, `exposure`, `total_exposure`, `sources` |
| `strike_clustering` | `key`, `underlying`, `strike`, `positions`, `threshold`, `exposure`, `sources` |
| `vix_regime_change` | `old_regime`, `new_regime` (`low_vol` / `moderate_vol` / `high_vol`), `vix`, `source`, `is_proxy`, `affected` |
| `correlation_spike` | `id_a`, `id_b` (runner ids), `strategy_a`, `strategy_b` (labels), `correlation`, `samples`, `threshold` |
| `data_feed_stale` | `source`, `age_s`, `threshold_s`, `symbols`, `runners` |
| `oi_anomaly` | `underlying`, `strike`, `option_type`, `oi_change`, `avg_change`, `multiplier`, `held` |
| `liquidity_dry_up` | `underlying`, `strike`, `option_type`, `spread`, `avg_spread`, `multiplier`, `held` |

### When you are called

* `on_alert` — once when the alert is created, and again if it **escalates**
  (warning → critical). Not on every refresh while the condition persists;
  not twice for the same alert key within 60 s (flapping guard).
* `on_alert_resolved` — when an alert you were notified about resolves:
  the condition cleared, an event alert expired, a newer regime transition
  superseded it, or someone resolved it manually. Dismissing in the UI does
  **not** call it (dismissal is a UI preference, the condition still holds).
* Both run on the alert evaluator thread **under your runner's lock** — the
  same lock bar processing uses, so there is no race with your `entries()` /
  `exits()`. Keep handlers fast and non-blocking (no I/O, no sleeps).
* Exceptions are caught, logged and counted; they never affect other
  strategies or the platform. The return value is ignored.
* Each notification is recorded in the runner's signal log (`ALERT` /
  `ALERT_RESOLVED`) and in the alert's `notified_strategies` (with
  `ok`/`error`).

## 3. Act — the two levers

### `pause_new_entries`

`self.pause_new_entries = True` makes the runner skip **new entries** (equity
entries and new option structures); exits, stops and targets keep working.
Skipped entries are logged as `BLOCKED` / `OPTION_BLOCKED` with reason
"paused by strategy". The flag is yours — the platform never sets or clears it.
If several conditions can pause you, track them separately (the strangle
keeps a set of reasons so a regime resume does not lift a gamma pause).

### `request_exit(fraction=1.0, position_key=None, reason="alert")`

Queues a request; the runner drains it on its **next bar** and closes through
the engine with exit reason `strategy_alert:<reason>`.

* `position_key=None` → every open position of this runner; otherwise one
  symbol / structure id.
* Equity positions support partial exits (`fraction=0.5` closes half,
  rounded down to whole units).
* Option structures close **atomically**: a partial fraction on a structure is
  refused and logged (`ALERT_EXIT_REFUSED`) instead of breaking a spread's
  legs apart.
* `fraction` must be in `(0, 1]`.

## 4. Declare your regime

`regime_vix_range = (lo, hi)` is purely informational: the Market Regime
section and the `vix_regime_change` alert show whether the current VIX suits
you ("Optimized for VIX 10–15", favourable/unfavourable). It does not pause
anything by itself — react in `on_alert` if you want to.

## 5. The reference strategy

`immediate_strangle` (v1.1):

| Param `alert_response` | On `portfolio_gamma_critical` |
|---|---|
| `pause` (default) | pause new strangles until the alert resolves |
| `exit` | pause, and request a full close **if this runner is ≥ 50% of the portfolio's short gamma** |
| `ignore` | carry on (the alert is still logged) |

On `vix_regime_change` it pauses when the new regime is `high_vol` and resumes
when it leaves it.

## 6. Testing your handler

```python
strategy = MyShortPremium()
strategy.on_alert("portfolio_gamma_critical",
                  {"net_gamma": -400, "self_contribution": {"share": 0.7}})
assert strategy.pause_new_entries
assert strategy.drain_exit_requests()[0]["fraction"] == 1.0
```

End-to-end examples driving a real `PortfolioManager` are in
`tests/intelligence/test_integration.py`.
