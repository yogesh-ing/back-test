# Market-status awareness — enhancement spec (no misleading timers)

**Approved by the operator 2026-10-02.** Implements the holiday-aware feed gate,
the `market_holidays` table, and the exact UI copy below. Implement the message
strings **verbatim** — they were agreed in discussion and are the contract.

Scope: server market-day-state (§4), feed polling gate on closed days (§5), SSE
payload (§4), app-top header chip and four timing displays' copy (§7), tests (§8).
Found in the QA sweep as finding #11 of `UI-WALKTHROUGH-AND-QA-2026-10-02.md`;
this file is the standalone implementation contract for that finding.

## 1. Problem

Today (02-Oct-2026) is an NSE trading holiday (Mahatma Gandhi Jayanti), yet the UI
behaves as if trading were live:

- Command center shows `⏱ next bar in 28s (bars every 60s, last 103592s ago)` —
  a client-side modulo countdown toward an event that cannot happen
  (`portfolio.js`, bar-clock block ~lines 774–797).
- Risk banner shows `Last update: 14:43:39` — when the SSE snapshot carries no
  timestamp, `risk_strip.js:190` **falls back to the browser's wall clock**, so
  "last update" refreshes even with no server data at all.
- Details panel is labeled `Daily loss breakdown · last 6 hours` (`base.html:131`)
  but the sparkline actually plots ≤60 client-memory points (~5 min since page
  load), reset on refresh (`risk_strip.js:12,155–160,206`).
- `api/forward.py:484` hardcodes `"market_open": True`.
- The backend calendar, `live/time_manager.py`, ships a *partial 2024* NSE holiday
  list and is not consulted by the web layer or the feed gate.

Principle: **a timer may only count toward an event that can actually happen, and
every timing string must derive from server truth (bar arrival, broker session,
market calendar), never from the browser clock.**

## 2. Source of truth — NSE holiday data (verified 2026-10-02)

- `GET https://www.nseindia.com/api/holiday-master?type=trading` — JSON keyed by
  segment (`CBM` = equity; plus derivative/currency/commodity sections); entries
  `{tradingDate, weekDay, description, morning_session, evening_session, Sr_no}`.
  `holiday-year-list` currently returns `{"year":["2026"]}`; NSE publishes the
  next year around mid-December.
- `GET https://www.nseindia.com/api/marketStatus` — live per-segment status:
  `{"market":"Capital Market","marketStatus":"Close","tradeDate":"01-Oct-2026
  15:30","marketStatusMessage":"Market is Closed", …}`.
- **Catch:** NSE sits behind Akamai; plain `requests`/`curl` gets empty/403
  responses. Therefore **the app must NOT call NSE at runtime.** The list is
  seeded into the DB once per year (manual fetch via browser, or a one-off
  script with a cookie handshake), and the table below becomes the source of truth.

## 3. `market_holidays` table schema

One row per exchange-declared holiday. Weekend Saturdays/Sundays are NOT stored
(weekday rule handles them); store only dates that would otherwise look tradable.

```sql
CREATE TABLE market_holidays (
    holiday_date  DATE PRIMARY KEY,            -- IST calendar date, market closed all day
    segment       TEXT NOT NULL DEFAULT 'equity', -- 'equity' | 'derivative' | 'currency' | 'commodity'
    description   TEXT NOT NULL,               -- e.g. 'Mahatma Gandhi Jayanti'
    is_trading_holiday BOOLEAN NOT NULL DEFAULT TRUE, -- FALSE for clearing-only holidays
    source        TEXT NOT NULL DEFAULT 'nse', -- provenance: 'nse' | 'manual'
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_market_holidays_year ON market_holidays (EXTRACT(year FROM holiday_date));
```

Wire it as a SQLAlchemy model in the same DB as the portfolio tables (Postgres via
`FORWARD_TEST_DB_URL`; remember `create_all`+stamp drift — ship an Alembic
migration, do not rely on `create_all`).

Seed for 2026 (equity/derivative trading holidays from the NSE table; weekday rows
only — weekend rows add nothing):

```
2026-01-15  Municipal Corporation Election - Maharashtra
2026-01-26  Republic Day
2026-03-03  Holi
2026-03-26  Shri Ram Navami
2026-03-31  Shri Mahavir Jayanti
2026-04-03  Good Friday
2026-04-14  Dr. Baba Saheb Ambedkar Jayanti
2026-05-01  Maharashtra Day
2026-05-28  Bakri Id
2026-06-26  Muharram
2026-09-14  Ganesh Chaturthi
2026-10-02  Mahatma Gandhi Jayanti
2026-10-20  Dussehra
2026-11-08  Diwali Laxmi Pujan — trading holiday but Muhurat session only;
            store it: day_state=HOLIDAY gives CLOSED for normal bars
2026-11-24  Prakash Gurpurb Sri Guru Nanak Dev
2026-12-25  Christmas
```

## 4. Day-status contract (start / midnight check)

A single server-side resolver, `get_market_day_state(now_ist)`, computed at app
startup and **re-evaluated lazily whenever the IST date changes** (a server up
since yesterday must carry today's verdict, not the boot-time one — key the cache
on IST date, no scheduler needed):

```
state ∈ { HOLIDAY, WEEKEND, TRADING_DAY }
if ist.weekday() >= 5:                      WEEKEND
elif date in market_holidays (segment):     HOLIDAY  (+ description)
else:                                       TRADING_DAY
```

**Calendar-gap rule:** if `market_holidays` has no rows covering the current IST
year, fall back to the weekday rule only and surface
`⚠ Holiday calendar missing for <year> — weekday rule only` (chip state 6). Never
silently guess "holiday" from an empty table.

**Fail-open rule (the trap-guard):** inside 09:15–15:30 on a weekday, the system
may only claim "closed" if the DB says HOLIDAY. If the DB read fails, the verdict
is "not closed" → show STALE / BROKER_DOWN warnings instead of calm. Any calendar
exception degrades to today's behavior minus the false calm, never to a new
wrongness.

Expose in the SSE `portfolio` snapshot and `/api/portfolio/summary`:

```json
"market": { "day_state": "HOLIDAY", "session_state": "CLOSED",
            "holiday_name": "Mahatma Gandhi Jayanti", "next_open_ts": "2026-10-05T09:15+05:30" }
```

`session_state ∈ { PRE_OPEN, OPEN, CLOSED_TODAY, CLOSED_WEEKEND, CLOSED_HOLIDAY }`
(time-of-day within the day-state). Replace the hardcoded `"market_open": True` at
`api/forward.py:484` with this truth.

## 5. Feed gate — rest on holidays

- `feed_registry.py` `_poll_once()` (lines 471–495): today it checks only
  time-of-day (`mstock_live_feed._market_open`) and skips symbols already in
  `_last_ts` — on a holiday it still wakes every 60s, still does a first fetch per
  never-seen symbol, and dedups stale bars into silence. Change: when
  `day_state == HOLIDAY` (or WEEKEND), **skip polling fully** — no broker HTTP at
  all, including first-fetches — log `feed resting — <reason>` once per day, and
  re-arm automatically on the next TRADING_DAY at session start.
- `mstock_live_feed.py` `stream()` (lines 587–600): extend the existing
  `market_gate` sleep with the same day-state check so per-symbol generators rest
  identically.
- Keep both gates fail-open: an unreadable `market_holidays` table must never stop
  the feed (assume trading day, keep polling).
- **Do NOT kill the feed threads to save memory** — thread stacks are a few MB; the
  runners' warmup state must stay loaded regardless. The win is honesty + zero API
  calls, not RAM.
- Options path note: `ChainBus`/`option_quote_provider` poll underlying quotes on
  the same cadence; apply the identical holiday gate before any chain fetch.

## 6. UI state machine — five states

`HEALTHY` (open, bars flowing) · `STALE` (open, no bars > 2 periods — incident) ·
`BROKER_DOWN` (open, broker session lost) · `CLOSED` (weekend/holiday/pre-open/
post-close) · `UNKNOWN` (calendar unreadable — fail loud, never guess). Every
timing string below is chosen by these states, which come from the server
snapshot, not client inference.

## 7. Message tables (implement verbatim)

**1 · App-top header chip (new, global, `base.html`)**

| State | Chip text | Color |
|---|---|---|
| Session live | `● MARKET OPEN · till 15:30 IST` | green |
| Pre-open (09:00–09:15, optional) | `◐ PRE-OPEN · opens 09:15` | amber |
| After close, trading day | `○ CLOSED · next open tomorrow 09:15` | grey |
| Weekend | `○ WEEKEND · opens Mon 05-Oct 09:15` | grey |
| Holiday (from DB) | `○ HOLIDAY · Mahatma Gandhi Jayanti · opens Mon 05-Oct 09:15` | grey |
| Calendar gap | `⚠ No holiday data for <year> — weekday rule only` | amber |

**2 · Command center bar-clock line (`portfolio.js`)** — countdown exists only in
HEALTHY; everything else is a static statement (no ticking on closed days):

| Condition | Message |
|---|---|
| Open + bars flowing | `⏱ next bar in 28s · bars every 60s` |
| Open + no bars >2 periods | `⚠ market OPEN — no bar for 2m 14s (expected every 60s)` — red |
| Open + broker not logged in | `🔴 broker session not logged in — live feed paused · log in at /settings` — red |
| Closed (any reason) | `○ Market closed (Gandhi Jayanti) — no bars today · last bar 15:29 IST, 01-Oct` — grey, static |

**3 · Risk banner "Last update" (`risk_strip.js`)** — freshness, never browser
clock. Remove the `new Date().toLocaleTimeString()` fallback at line 190:

| Condition | Message |
|---|---|
| Snapshot <15s old | `Updated 14:43:39 · live` |
| Snapshot stale, market open | `Updated 14:43:39 · ⚠ 3m stale` |
| Closed day | `Figures final as of 01-Oct 15:30 IST (market closed)` |
| No timestamp from server | `Updated —` (never fabricate) |

**4 · Daily-loss sparkline (`base.html:131` label + `risk_strip.js`)** — v1 relabel
honest, v2 server-sourced series:

| Version | Label / message |
|---|---|
| v1 (UI-only fix, do now) | `Daily loss · since page load (~5 min live sample)` |
| v2 (server series, follow-up) | during session: `Daily loss · today 09:15 → 14:43 · peak 2.1%, now 0.8%` · closed day: `Daily loss · 01-Oct session complete — ended at 0.3% of limit` · holiday: `No session today (Gandhi Jayanti) — showing 01-Oct, last trading day` |

v2 backend: sample `daily_loss_pct` every 1–5 min during the session into the
existing portfolio state (server timestamps), serve as `GET /api/risk/history`,
plot the full session. The client `riskHistory` array then goes away.

**5 · Risk strip / halt pill on closed days** — `HALTED` vs `NO DATA` must stay
visually distinct when figures are old: on CLOSED days render the strip figures in
"frozen" styling (dimmed + the "figures final" line from table 3) so nobody reads
yesterday's close as today's live risk. The halt pill keeps its own state
(`halted`/`danger`/`warn`/`safe`) and does NOT fire stale-state colors purely
because a holiday froze the numbers.

## 8. Acceptance criteria / tests

- Unit: `get_market_day_state` for 02-Oct-2026 (HOLIDAY), a Saturday (WEEKEND), a
  normal Thursday (TRADING_DAY), and an empty-table 2027-01-04 (weekday fallback +
  gap warning, never HOLIDAY).
- Unit: inside 09:15–15:30 weekday with the DB read raising → session_state OPEN
  (fail-open), STALE path reachable.
- Feed: with day_state HOLIDAY, mock broker client records **zero** HTTP calls
  across ≥3 poll cycles, including a newly subscribed symbol; re-arms next TRADING_DAY.
- UI (jsdom or manual): no element matching the countdown pattern renders on a
  CLOSED snapshot; "Last update" never uses `Date.now()` when the server omits
  `timestamp`.
- Copy check: assert the five tables' strings appear verbatim (guards against
  silent paraphrase).

Related: finding #11 and the full page-by-page sweep in
`UI-WALKTHROUGH-AND-QA-2026-10-02.md`; NSE endpoints verified 2026-10-02
(holiday-master `type=trading`, marketStatus — Akamai-gated, seed offline only).
