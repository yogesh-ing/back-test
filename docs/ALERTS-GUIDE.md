# Alerts Guide

What each portfolio alert means, when it fires, and how traders usually
respond. For the analytics behind the numbers see
[PORTFOLIO-INTELLIGENCE.md](PORTFOLIO-INTELLIGENCE.md); for making a strategy
react see [STRATEGY-ALERTS.md](STRATEGY-ALERTS.md).

> **Alerts inform; they never act.** No alert closes, resizes or blocks a
> position. Dismissing an alert only hides it from your widget: subscribed
> strategies already got it, and the condition keeps being monitored.

---

## The alert widget

A small panel fixed to the bottom-right corner of every page.

* **Minimized** — `🔔 3 alerts` with a severity breakdown (`🔴 1 · ⚠️ 1 ·
  ℹ️ 1`). The pill takes the colour of the most severe open alert; with nothing
  open it shows a quiet `🔔 No alerts`.
* **Expanded** (click the header; state remembered in `localStorage`) — a
  list, most severe first: icon, title, one-line message, age, and **View
  Details** / **Dismiss** buttons.
* **Toast** — a new critical or warning alert briefly pops up a toast (not on
  the initial page load).
* **Detail modal** (View Details) — current state (the metrics that tripped
  the rule and the threshold), *what this means*, *contributing strategies*
  (with their share of the metric), *typical responses*, and which strategies
  are subscribed / not subscribed to this alert type. There are deliberately
  **no action buttons** that touch positions — only *Mark as reviewed*,
  *Dismiss*, and a deep link (*View in Risk Board →*) to the relevant Risk
  Board section. `Esc` closes it.

The widget polls `/api/alerts/active` every 10 s (30 s while the browser tab
is hidden) and only re-renders when the alert set actually changed.

---

## Severity

| | Severity | Meaning |
|---|---|---|
| 🔴 | **critical** | Needs attention now. Never auto-dismissed. |
| ⚠️ | **warning** | Worth a look soon. |
| ℹ️ | **info** | Context. |

---

## Alert reference

Thresholds below are the defaults in `config/portfolio_intelligence.yaml`.

### 🔴 `portfolio_gamma_critical` — Portfolio gamma critical
**Fires when** net gamma (Δ change per 1% move, all buckets) `< gamma_critical`
(default −150).
**Means** the book is net short gamma: every move pushes delta against you and
losses accelerate the further the market travels (a 2% move costs roughly four
times a 1% move on the convexity term). Usually paired with positive theta:
you are being paid decay to carry this.
**Shows** net gamma, breach %, scenario P&L for a 2% move, net theta, and
contributing strategies ranked by gamma share.
**Typical responses** reduce the largest short-gamma structure · buy wings
(protective options) · size new short-premium entries smaller · monitor
without acting when expiry decay is the intended trade.
**Resolves** automatically when net gamma is back above the threshold.

### ⚠️ `portfolio_delta_warning` — Portfolio delta warning
**Fires when** `|net delta| > delta_warning_abs` (800 share-equivalents ≈ ₹800
per point).
**Means** a large directional lean, often several strategies leaning the same
way without any single one looking big.
**Typical responses** check whether the bias is intended · offset or reduce
the largest contributor · monitor (delta drifts with the market).

### ⚠️ `concentration_high` — Concentration high
**Fires when** one underlying holds more than 60% of gross exposure *and* the
book has at least 2 positions. One alert per underlying.
**Means** "different" strategies are really one bet on one underlying.
**Typical responses** deploy new capital elsewhere · trim the largest position
in that underlying · accept it knowingly (e.g. an index-only book).

### ℹ️ `strike_clustering` — Strike clustering
**Fires when** 3+ positions share one underlying/strike/option type.
**Means** exits compete for the same liquidity; pin risk concentrates at one
level near expiry.
**Typical responses** stagger strikes on new entries · stagger exits · no
action if sizes are small versus the strike's volume.

### ⚠️ `vix_regime_change` — Volatility regime change (event)
**Fires when** VIX (or the realized-vol proxy, labelled as such) moves into a
different band — low `< 15`, moderate, high `≥ 22` — past the 0.5-point
hysteresis. The first reading after startup does not count as a change.
**Shows** old → new regime, value, source, and running strategies whose
declared VIX range is now unfavourable.
**Typical responses** review strategies flagged unfavourable · let subscribed
strategies decide · wait for confirmation. A newer transition supersedes
(resolves) the previous one.

### ℹ️ `oi_anomaly` — Open-interest anomaly (event)
**Fires when** |ΔOI| at a strike is more than 3× its rolling average change.
Flags whether you hold a position at that strike.
**Typical response** context, not a signal — OI does not say who is long or
short. Requires chains with real OI (synthetic chains have none).

### ⚠️ `correlation_spike` — Strategy correlation spike
**Fires when** two runners' P&L changes correlate above 0.8 (after at least
30 aligned samples).
**Means** the diversification the book appears to have is not there right
now; correlations rise exactly in stressed markets.
**Typical responses** size the pair as one risk unit · pause or shrink one of
them · monitor (short samples are noisy).

### ℹ️ `liquidity_dry_up` — Liquidity dry-up (event)
**Fires when** a contract's bid-ask spread is over 2× its rolling average.
**Typical responses** prefer limit orders there · delay non-urgent orders.

### 🔴 `data_feed_stale` — Data feed stale
**Fires when** a **running** runner's symbols have had no new bar for 120 s.
One alert per feed source (synthetic / replay / mstock). New runners get a
grace period; broker feeds are only checked during market hours; paused or
stopped runners are ignored.
**Means** marks, Greeks and stops are working from stale prices — a system
issue, not a market signal.
**Typical responses** check the broker session · check
`/api/broker/feed-quality` · consider pausing runners until it recovers.
**Resolves** as soon as a fresh bar arrives.

### 🔴 `risk_limit_breach` — Circuit breaker tripped
**Fires when** a portfolio risk limit is breached and the breaker latches:
one alert per halted scope — `risk_limit_breach:bucket:paper`,
`…:bucket:live`, or `…:portfolio` for the master kill. A bucket-caused
combined breach does not raise a duplicate master alert.
**Means** the platform stopped new entries for that scope (and flattened
positions on a drawdown trip) to stop a bad day turning into a terminal one.
The latch stays until an explicit reset, even if the metric recovers.
**Shows** limit type (daily loss vs drawdown), reason, halt mode (pause vs
flatten), equity, daily P&L and drawdown at trip time.
**Typical responses** review what caused the breach before resetting ·
`reset_circuit_breaker()` (scoped or master) from the Portfolio page once
acknowledged · keep it halted — a halted bucket is a decision, not a failure.
**Resolves** when the breaker is reset for that scope.

### ⚠️ `broker_session_expiring` — Broker login expiring
**Fires when** a broker session enters its last 30 minutes (raised by the
session expiry monitor, one alert per broker).
**Means** the live feeds, marks and order paths for that broker stop at the
expiry unless someone re-authenticates.
**Typical responses** click **Re-login** on the alert — it opens the broker
auth popup (password → TOTP) without leaving the page; the alert
auto-resolves when the re-login verifies.

### 🔴 `broker_session_expired` — Broker session expired
**Fires when** a broker session dies (the expiry monitor clears the token;
the expiring alert, if still open, is superseded).
**Means** live feeds, forward entries and portfolio marks for that broker
are paused — re-login (password + TOTP) is required; the session never
auto-renews.
**Typical responses** use the alert's **Re-login** button (same auth popup)
or the broker board; a verified re-login resolves the alert.
Both session alerts are also fanned out through the outbound notifier
(Telegram/email per `config/alerts.yaml` routing).

---

## Lifecycle

```
condition true ──► CREATED ──► broadcast: widget · subscribed strategies · audit log (DB)
                     │
     condition still true: refreshed in place (message/metrics, occurrence count)
     severity escalates (warning → critical): re-surfaced + subscribers re-notified
                     │
   ┌─────────────────┼──────────────────────┬──────────────────────────┐
   ▼                 ▼                      ▼                          ▼
 DISMISSED        REVIEWED            AUTO-DISMISSED              RESOLVED
 (you, widget)    (you, modal)        after 1 h un-actioned       condition cleared,
 hidden from      stays visible,      (not for critical)          event expired (1 h),
 the widget       marked reviewed                                 superseded, or manual
```

* **Deduplication.** One open alert per type + subject (e.g. per underlying,
  per runner pair, per feed source). A condition that stays true refreshes
  the same alert rather than creating new ones.
* **Condition alerts** (Greeks, concentration, clustering, correlation, feed)
  resolve on their own when the condition clears. **Event alerts** (regime
  change, OI, liquidity) describe something that *happened*; they expire after
  `event_alert_ttl_s` (1 h).
* **Dismissing is personal.** It hides the alert in the widget; strategies
  are unaffected and the rule keeps running. If the condition clears and
  comes back later, that is a new alert.
* **Flapping guard.** A strategy is not re-notified for the same key within
  `renotify_cooldown_s` (60 s).
* **Audit.** With a database configured every transition is written to the
  `alerts` table; `GET /api/alerts/history?from=7d` reads it back.
  `?include_dismissed=1` on `/api/alerts/active` shows dismissed-but-open
  alerts.

---

## Outbound notifications (Telegram / email)

The widget is in-app; the notifier (`backtest.alerts.notifier`) is the way
alerts leave the process — for when nobody is watching the dashboard
(overnight breaker trips, stale feeds). It subscribes to every alert type
and never blocks the trading loop: messages are queued and a background
thread does the network I/O.

**Channels.** `log` is always on (a misconfigured deployment still records
alerts; it is also the routing floor). **Telegram** switches on when
`TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` are set; **email** (SMTP,
STARTTLS) when `ALERT_SMTP_HOST` + `ALERT_TO_EMAILS` are set. Routing,
minimum severity, quiet hours (IST, `22:00–07:00`, critical exempt) and
hourly rate limits come from `config/alerts.yaml` (profile via
`ALERT_PROFILE`) — see `.env.example` for every variable. `ALERT_NOTIFIER=0`
disables all outbound channels.

**Behaviour worth knowing**

* Critical alerts bypass quiet hours and rate limits — breaker trips are
  exactly the messages that must not be throttled.
* A message template is used only when every `{placeholder}` can be filled
  from the alert's data; otherwise a plain `[SEVERITY] type — message`
  fallback is sent.
* Slack / Discord / SMS / desktop entries in the YAML are accepted but not
  implemented; they are skipped (the log channel is the floor).
* One failing channel never blocks the others.

**Try it without real credentials:** set `ALERT_PROFILE=dev` for log-only
routing, or run with `ALERT_NOTIFIER=0` to keep everything in-app.

---

## FAQ

**Why didn't the gamma alert fire at −1000 as in the PRD?** The default is
−150 because of the unit used (Δ per 1% move); see
[PORTFOLIO-INTELLIGENCE.md](PORTFOLIO-INTELLIGENCE.md#configuration). Set
`PI_GAMMA_CRITICAL` or edit the YAML.

**The regime says "realized-vol proxy".** No VIX print has been received in
the last 15 minutes, so the regime is estimated from NIFTY bars. Post a VIX
value (`POST /api/market/vix {"value": 17.2}`) or feed an `INDIAVIX` symbol.

**Can I turn it all off?** Start the app with
`--disable-portfolio-intelligence`: no evaluator, no widget, the API returns
503. Note the outbound notifier still starts (breaker trips are safety
messages, independent of the intelligence layer) — set `ALERT_NOTIFIER=0`
to silence that too.
