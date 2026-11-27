# Live-Market Paper QA — Bug Report, 28 Oct 2026

**Runbook:** `docs/QA-LIVE-PAPER-PORTFOLIO-PLAN.md` (TC-1.x → TC-8.x + §5 P1–P4)
**Build under test:** `feature/options-trading-dev` @ `85791e7` ("Merge PR #46 … into the clock-policy line"), served from `C:\learning\back-test` on `:5000`
**Session:** live NSE, 12:44–13:55+ IST (cutover restart off a pre-merge build at 12:44; mStock WS session auto-restored, 8/8 runners restored then resumed, 6 QA runners spawned mid-session → 14 paper runners live). EOD and the TC-4.6 stress test were pre-registered at 14:33 and measured after the close — results in §8: **zero square-offs (P3-EOD FAIL: the three monthly structures rode into 29 Oct), the 15:30 chart export ran correctly, and TC-4.6 injected ₹1,23,280.90 of phantom loss at 16:41 while the breaker stayed `NORMAL` at 156 % of its own limit and re-baselined the persisted day anchor.**
**Method:** black-box over `/api/portfolio/stream` (SSE), REST, `tools/out/qa_server_20261028_cutover.log`, and direct read-only Postgres queries against `forward_test`.
**Constraints honoured:** **no code was changed during this run** — every item below is a finding with a proposed direction only. No LIVE runner was armed, no `ALLOW_LIVE_ORDERS`/`confirm_live_orders` flag was touched, no bid/ask was fabricated.
**Known limitation:** the in-app Browser panel reported `viewport=0x0, visibilityState=hidden`, so pointer-level and pixel checks (tooltips, disabled-button styling, screenshots) were replaced by DOM/template + JS-source verification plus the wire payloads. Marked `UI-NOT-VERIFIED` where that materially weakens the claim.
**Attribution note:** 20 `Traceback (most recent call last)` blocks appear in the cutover log between 12:57 and 13:10. They are `File "<stdin>", line 6, in <module>` → `UnicodeEncodeError: 'charmap' codec can't encode character '\ufffd'` from **my own probe scripts** writing structure labels (which contain `·`) through cp1252 stdout, not server faults — the server process never restarted and no request failed at those timestamps. Recorded so nobody files them as product bugs.

Evidence files: `tools/out/QA_CADENCE.txt`, `tools/out/QA_CADENCE_RAW.json`, `tools/out/QA_DB_ACTIVITY.txt` + `.jsonl`, `tools/out/QA_STRANGLE_CHURN_STATS.json`, `tools/out/QA_EQUITY_FEE_RESTATE.json`, `tools/out/QA_CAPITAL_DRIFT.jsonl` + `.log` (5-min SSE breaker/P&L census, 13:14→15:32 IST), `tools/out/QA_EOD_SQUAREOFF.txt`, `tools/out/_qa_trades.json`, `tools/out/_qa_orders.json`, `tools/out/_qa_positions.json`, `tools/out/_qa_sse.txt`, `tools/out/qa_cadence_probe.py`, `tools/out/qa_ring_probe.py`, `tools/out/qa_db_activity.py`, `tools/out/qa_capital_drift.py`, `tools/out/qa_eod_watch.py`, `tools/out/QA_SUMMARY_1504.json`, `tools/out/QA_INTEL_ALERTS_1500.json`, `tools/out/QA_ALERT_HISTORY_1500.json`, `tools/out/QA_POSTCLOSE_OPEN_1512.json`, `tools/out/qa_eod_collect.py` -> `tools/out/QA_EOD_RESULTS.json` + `tools/out/QA_EOD_COLLECT.log` (written ~15:37 IST).

---

## 1. Verdict

The plumbing PR #46 promised is largely **there and honest**: real two-sided WS depth drives marks, no synthetic quotes appear anywhere in this build's log, timestamps are aware-UTC at rest, the clock-policy suite is clean, paper/live buckets stay isolated in the DB, and the manual LOM (arm/refuse/partial/close-while-paused) behaves exactly as specified.

What is **not** acceptable for a paper-accuracy demo is the trading behaviour itself: **the timeframe chosen at spawn is ignored for option strategies, so the three `immediate_strangle` runners churned 205 structures between 12:44 and 13:52 for −₹34,226 gross / −₹73,831 net, every one of them with `bars_held: 0`.** Counted across all six option runners the session made **206 round trips: −₹35,125 gross, −₹74,462 net, of which ₹39,337 is fees** — 90 % of the trades lost less than their own transaction cost, the win rate was 2.5 % (198 stops vs 5 targets), and **all five take-profit exits were losers after fees**, because the ₹100 bracket is 52 % of the ₹193 round-trip fee. The surfaces that read the payload's `total_capital` / `total_equity` — the landing bucket cards and the `/risk` dashboard — are also wrong, by ₹82.5 lakh and ₹10 lakh respectively (the seven metric cards *on* `/portfolio/paper` escape it: they are bucket-scoped, see BUG-03), no option leg in the runner payload carries a Greeks value even though the risk engine is computing portfolio delta, and 80.5 % of option fills today booked off the ₹0.05 exchange tick (the quote chosen was on-grid; the slippage multiply is not rounded back).

39 findings: **7 at S1, 14 at S2, 11 at S3, 7 at S4** — plus 35 checks that passed and are listed in §5 so the fixes do not re-break them.
**2 of the S4 items are PARKED by decision (BUG-31, BUG-32, plus the playbook clauses of BUG-30 and §6 item 9) — see §1.1. They stay filed; they are not on the fix queue.**

The S1 set is now: the ignored timeframe (BUG-01), the sub-cost SL/TP bracket (BUG-02), the ₹82.5 L capital over-report (BUG-03), three P&L definitions in one row (BUG-04), **equity paper trading pays nothing** (BUG-26) — found by re-costing the day's own fills with the repository's own fee schedule, which is what makes `ema_pullback` look like the only profitable strategy on the board at +₹7,517 when it is really −₹13,434 — and the stress-test endpoint that rewrites the live circuit breaker and persists the re-baselined day (BUG-28) — measured at 16:41 IST in §8, where it injected ₹1,23,280.90 (48 % of the whole paper book) with no market behind it, left the breaker at `NORMAL` while its own `daily_loss_pct` read 1.5566, and rewrote `day_start_equity` in `portfolio_state.json`. A demo-day footgun rather than a data bug, and now a proven one. The seventh item is behavioural rather than arithmetic: **there is no end-of-day exit for a structure whose expiry is not today (BUG-39)** — §8 proves three monthly legs rode into 29 Oct unmarked, and BUG-18 already shows what that costs the next session's P&L.

Severity buckets: **S1 = blocks the demo / wrong money numbers**, **S2 = wrong or misleading data in a user-visible surface**, **S3 = contract/robustness gaps**, **S4 = cosmetic**.

| # | Sev | Title | Where it hurts |
|---|-----|-------|----------------|
| BUG-01 | **S1** | Selected timeframe is ignored for option runners → per-minute (per-tick) entry churn | Positions, Orders, Equity, Daily P&L |
| BUG-02 | **S1** | `±₹100` strangle SL/TP is inside the bid-ask bounce → 100 % same-bar exits | Strategy viability verdict is fake |
| BUG-03 | **S1** | `total_capital` over-reports by ₹82,50,000 (unscoped accumulator) and `total_equity` by ₹10,00,000 (an *empty* dashboard book added to equity but not capital) | Landing bucket cards + `/risk` dashboard + every `total_*` consumer; the scoped page's own cards are clean (§5), but "Realized" card ≠ matrix (BUG-04) |
| BUG-04 | **S1** | `realized_pnl` is gross of fees while `daily_pnl`/`equity` are net; `daily_pnl ≠ realized + open` in 8/14 runners | Every P&L column |
| BUG-05 | **S2** | Greeks are `null` on every option leg → Gamma/Vega/Theta read `0.00` under live bias labels | TC-2.1, TC-4.2 |
| BUG-06 | **S2** | The quote the engine chooses is always exchange-possible (910/910 `requested_price` on grid), but **733/910 booked fills (80.5 %) land off the ₹0.05 tick** — spread **and** 10 bps slippage both charged, quantised to paise | Paper fills not reproducible live |
| BUG-07 | **S2** | Topbar "Flatten all" is visible on `/portfolio/paper` and flattens **all buckets** | Safety design contradiction |
| BUG-08 | **S2** | Orders tab and Trade history render raw UTC (`IST − 5h30m`) while Positions renders IST | UX-4, still open |
| BUG-09 | **S2** | `#risk-mode` dropdown splits one tab across two clocks (Intelligence pinned to page, Risk board not) | UX-2/UX-5, still open |
| BUG-10 | **S2** | `instance_id` is silently ignored by `/api/portfolio/trades/aggregated` | TC-5.2 deep-dive per-runner list |
| BUG-11 | **S2** | Trade history drops `entry_ts` + hold duration although the API now returns both | UX-1, still open |
| BUG-12 | **S3** | `structure_id` is runner-local → same ID in two runners at the same minute | Any cross-runner lookup |
| BUG-13 | **S3** | `option_intent_events` has 0 rows: journal is never injected on the paper path | P2 audit trail absent |
| BUG-14 | **S3** | `positions` table has 0 rows; DB `trades.net_pnl == gross_pnl` for 3,078/3,078 rows; `exit_reason` NULL on 88 % | Reconciliation at rest |
| BUG-15 | **S3** | `CE_instant_buy·BANKNIFTY·1hour` can never trade (worst-case ₹27,814 > ₹25,000 ceiling) — blocked 11,410×, still shows 🟢 RUNNING | TC-1.4 silent-dead runner |
| BUG-16 | **S3** | Tick-cadence entries are stamped with the *minute* bar time while exits keep microseconds | Hold durations biased to the minute |
| BUG-17 | **S3** | Concentration treats an option as `spot × contracts` (₹14.78 L for one 65-lot) → false "HIGH 63.16 %" alert | TC-4.3 |
| BUG-18 | **S3** | Overnight "ride to settlement" structures settle in the *next* session (hold 1,275 min, +₹10,627 booked on 28 Oct); entries were taken at 15:30:30 IST on 27 Oct | Session attribution, post-close trading |
| BUG-19 | **S4** | Live-spawn refusal repeats "confirm_live=True is required" even when the request sends it; entitlement gate unreachable | P4-3 message is unactionable |
| BUG-20 | **S4** | "Download Watchlist CSV" writes `charts/<date>/*.csv|png` and returns absolute server paths; `/api/portfolio/watch/series` returns `[]` | TC-4.7, TC-6.2 |
| BUG-21 | **S2** | Runner warmup holds the shared pool on 13.7–23.6 s `market_data_cache` scans (and a 3.3 s `INSERT INTO portfolios` queues behind them) | Live tick path, spawn latency |
| BUG-22 | **S3** | Restart → cold ring → 3× WARNING + 1× **CRITICAL** `data_feed_stale` sent outbound 3.5 min after boot, self-healed | On-call trust, alert fatigue |
| BUG-23 | **S3** | `alerts` table missing on the live DB: alert persistence disabled for the whole session (migration 005 unapplied) | Post-session audit |
| BUG-24 | **S4** | `watch_export.py` defines its own IST offset + `datetime.now(_IST)` outside `timekeeping.py`; watch `ts` strings are naive | Clock-policy hole |
| BUG-25 | **S4** | `HotStrikeRing` recenter/pin/evict has no log line — the runbook's own P2-RING method is unexecutable | Verifiability of the depth ring |
| BUG-26 | **S1** | Equity paper books **₹0 costs** on 259/259 trades; re-costed with the repo's own schedule, the only green strategy (`ema_pullback` +₹7,517) becomes **−₹13,434** | Equity P&L, Trades tab, strategy ranking |
| BUG-27 | **S2** | Five risk-limit surfaces, one enforced: the live breaker is a dataclass default in **no** config file, while `forward_testing.yaml`'s 0.10/0.02 is display-only | Risk tab gauges, "what would halt me today?" |
| BUG-28 | **S1** | `/api/portfolio/test/breach` (TC-4.6) permanently rewrites the live breaker through an unvalidated attribute write, persists a re-baselined day P&L, and injects a fabricated −25 % bar into every runner's real OHLC buffer | Safety demo, day attribution, strategy inputs |
| BUG-29 | **S4** | `PAUSED` has no recorded cause — operator pause, breaker pause and fail-closed boot are indistinguishable on the wire and in state | Runner matrix status |
| BUG-30 | **S3** | Activity log is a 1,000-entry in-memory deque (wiped by every restart) and records `SPAWN` for the side-effect-free preview call | TC-8.1 audit trail |
| BUG-31 | **S4 · PARKED** | `POST /api/playbooks` accepts `structure_type: "banana"` / `""` with 201; validation happens only later, at option-runner create | TC-7.1 playbook CRUD |
| BUG-32 | **S4 · PARKED** | Option playbook deployed onto an equity strategy returns 200 with `expression: null` and `playbook_selections: null` — playbook fingerprint recorded, playbook behaviour silently dropped | TC-7.1 Deploy to Paper |
| BUG-33 | **S2** | `day_start_equity` is ₹61,49,577.50 on a ₹1.02 crore book → the same payload claims a **+38.8 % day** and −₹1,08,876.50 at once; the anchor is never re-baselined when capital is added mid-session | Equity chart baseline, `drawdown_pct`, "+X % today" |
| BUG-34 | **S2** | All 3 open option legs get `IV assumed 15%` and are totalled into `net_theta`/`net_vega` while `missing_greeks: []` claims completeness | Risk & intelligence Γ/Θ/ν cards, TC-4.5 stress grid |
| BUG-35 | **S2** | Segment breakers sit on an empty dimension: `runner_count: 0` on both segments while 14 runners live, and the sole `options_index` member is a 50-symbol **equity** runner | Risk tab segment cards, TC-4.4 limit edits |
| BUG-36 | **S2** | The durable audit trail is dark and says nothing about it: 213 alerts fired today, `alerts` has 0 rows (persistence latched off at 12:43:03 and never re-checked), `orders`/`positions` have 0 rows against 2,184 orders, and `/api/alerts/history` answers `source: "memory"` with `success: true` | TC-8.1 audit trail, post-session reconciliation |
| BUG-37 | **S2** | A test fixture spawns runners into the production Postgres book (13 `LIVE-STRANGLE [..]` rows, ₹5,00,000 each), and `portfolios` is a third capital accumulator: 39 `active` rows / ₹2,15,00,000 of which 27 rows / ₹1,33,00,000 point at runners that no longer exist | Capital accounting, risk pages, "live money" labelling |
| BUG-39 | **S1** | No end-of-day exit exists for a structure whose expiry is not today: 3 monthly legs rode into 29 Oct with `mark_ts` frozen, no square-off, no overnight risk control (runbook P3-EOD = FAIL) | Overnight risk, next-day session attribution (BUG-18) |
| BUG-38 | **S3** | The alembic chain is forked: two files both declare `revision = "020"` on the same parent, `alembic heads` warns "Revision 020 is present more than once" and prints two heads, `current` is stuck at 019, yet a 020-era table (`option_intent_events`) already exists in the live DB | Every future schema fix, incl. BUG-14/BUG-23 remediation |

### 1.1 Parked by decision — the playbook layer (28 Oct)

**BUG-31 and BUG-32 are PARKED, together with BUG-30's "preview logs a SPAWN" half and the playbook clause of §6 item 9.** The findings stand as written and the evidence stays where it is; they are simply taken off the fix queue. Reworking the playbook layer is a separate build of its own, and it is not worth doing before the base trading loop is correct, because a playbook trades *through* that loop and inherits every bug in it.

The prime intention of this system is one sentence: **take a trade as the strategy defines it, then take whichever of stop-loss or profit arrives first.** Parked work resumes when that sentence is demonstrably true on a live-market paper session. The gate, in report terms:

1. entries honour the timeframe selected at spawn — **BUG-01**;
2. stop-loss and target are both live, i.e. larger than the bid-ask spread plus the round-trip fee the engine itself charges — **BUG-02**;
3. whichever of the two is hit first is the exit that gets taken, on the strategy's own cadence, with the exit reason recorded — **BUG-18 / BUG-24** cover the accounting half;
4. the P&L that comes out is fee-honest on both segments, option and equity — **BUG-04** and **BUG-26**;
5. nothing that should have been squared off rides across the close into the next day — **BUG-39**.

Until 1–5 hold, validating `structure_type` at create or refusing the option-playbook-on-equity pairing cannot be measured as an improvement: the same deploy would still churn 0-bar structures and book net-negative "wins". Everything else in this report — the money numbers, the safety surfaces, the at-rest cluster — stays in front of them.

---

## 2. S1 findings in detail

### BUG-01 — the timeframe you pick is not the cadence the runner uses (TC-1.3 / TC-1.4 / §5 P1 FAIL)

Measured over a clean 5-minute window (13:05:30 → 13:10:30 IST, `tools/out/QA_CADENCE.txt`):

| runner | declared tf | bars Δ in 5 min | expected | last-bar stamp | verdict |
|---|---|---|---|---|---|
| `QA14_directional_BANKNIFTY_5min` | 5 min | **1** | 1 | 12:55 → 13:00 | OK |
| `QA13_ema_pool_5min` (49-symbol pool) | 5 min | 45 | 49 (per-symbol) | 13:00 → 13:05 | OK (counter is per-symbol) |
| `QA13_sma_TCS_15min` | 15 min | 0 | 0.33 | 12:45 held | OK |
| `P3TICK_strangle_NIFTY_15min` | 15 min | **5** | 0.33 | **13:09:00** | **VIOLATION** |
| `immediate_strangle·NIFTY·1hour` | 1 hour | **5** | 0.08 | **13:09:00** | **VIOLATION** |
| `CE_instant_buy·BANKNIFTY·1hour` | 1 hour | **5** | 0.08 | **13:09:00** | **VIOLATION** |

A "1 hour" runner processed five bars in five minutes and stamped its last bar `13:09:00` — a minute that is not an hourly boundary. `QA14_directional_BANKNIFTY_5min`, the control, did it right in the same window, so the aggregator is not broken globally; it is bypassed.

Confirmed at the census level at 13:55 IST — **every option trade closed today, bucketed by whether its own `entry_ts` sits on a boundary of the timeframe declared at spawn** (`/api/portfolio/runner/<id>.trades`):

| runner | declared tf | option trades today | entries on a valid boundary | **off-boundary** | distinct entry minutes |
|---|---|---|---|---|---|
| `immediate_strangle·NIFTY·1hour` | 1 hour | 76 | 2 | **74 (97 %)** | 55 |
| `P3TICK_strangle_NIFTY_15min` | 15 min | 74 | 5 | **69 (93 %)** | 53 |
| `QA14_strangle_NIFTY_1min` | 1 min | 68 | **68** | 0 | 49 |

The violation scales with the declared timeframe and vanishes at `1min` — the same strategy, same template, same tick path, only the timeframe differing. Sample off-boundary entry minutes for the hourly runner: `07:15, 07:16, 07:17, 07:18, 07:19, 07:20, 07:21 …` (UTC) = 12:45–13:01 IST, ten consecutive minutes of new strangles from a runner that is supposed to evaluate once an hour. From the log: **218 `ENTRY_FILLED` events across 63 distinct bar-minute stamps in 62 minutes** for `immediate_strangle` alone.

**Root cause (read, not changed):** `paper_runner.py:276 infer_evaluation_cadence()` — strategies whose template is `instant_long_option` or `structure` (`CE_instant_buy`, `PE_instant_buy`, `immediate_strangle`) are hard-defaulted to `evaluation_cadence="tick"`. The runner payload confirms it: `{"strategy": "immediate_strangle", "timeframe": "1hour", "evaluation_cadence": "tick"}`. Then `paper_runner.py:2532-2538` enters on *every* tick whenever `evaluation_cadence == "tick"`, `target_type == SINGLE_SYMBOL`, and no structure is open — there is no timeframe gate and no re-entry cooldown in that branch. The spawn form's Timeframe dropdown therefore only controls `bar_cadence_seconds` (used for mark staleness) for these three strategies.

Full census of the two fields in the live payload (14:41:20 IST, all 14 runners) — the split is clean and it is decided by the *strategy*, never by the spawn request:

| declared `timeframe` | `evaluation_cadence` | runners |
|---|---|---|
| 1min | `tick` | `CE_instant_buy·NIFTY`, `PE_instant_buy·NIFTY`, `QA14_strangle_NIFTY` |
| **1hour** | **`tick`** | `immediate_strangle·NIFTY`, `CE_instant_buy·BANKNIFTY` |
| **15min** | **`tick`** | `P3TICK_strangle_NIFTY` |
| 1min | `bar_close` | `banknifty_ema_momentum·BANKNIFTY`, both `persistence_probe_*`, `QA12_hf_ICICIBANK` |
| 5min | `bar_close` | `QA13_ema_pool_5min`, `QA14_directional_BANKNIFTY` |
| 15min | `bar_close` | `ema_pullback·NIFTY50`, `QA13_sma_TCS_15min` |

So the payload states the contradiction itself (`timeframe: "1hour"` + `evaluation_cadence: "tick"`), which means the UI could show it and does not. Note also that `bars_processed` is `172` on **every** `tick` runner regardless of strategy or timeframe = the number of minutes since the 12:44 cutover, while the `bar_close` runners carry real per-symbol counts (`1078`, `105`, `22`) — so the same column means "minutes elapsed" for one class and "bars evaluated" for the other.

**Secondary:** `stale_after_seconds` is derived from the declared timeframe (`1hour` → 3,780 s; `15min` → 1,080 s; `1min` → 240 s). A tick-cadence hourly runner will therefore tolerate an **hour-old mark** before flagging it stale — the two cadence systems disagree with each other inside one runner.

**Also:** `bars_processed` is not a bar count for tick/index runners — `ema_pullback·NIFTY50·15min` went 42,556 → 42,584 (+28 in 4 min) while `last_bar_ts` stayed on a correct 15-min boundary, so the counter mixes tick evaluations with bars.

**Direction:** either honour the declared timeframe for entries on tick strategies (gate `_process_option_tick_entry` on the same boundary check the bar path uses), or make the spawn form refuse `tick`-cadence strategies on `5min/15min/1hour` and say why. Do not let `stale_after_seconds` come from a timeframe the runner ignores.

### BUG-02 — `±₹100` on a 2-leg short strangle is smaller than the spread, so exits are random (TC-1.4, TC-9 priorities)

From the wire for one 5-minute stretch, three strangle runners:

```
13:00:08 OPTION_EXIT  strangle closed after 0 bars — stop -173 ≤ -100 — pnl -196
13:01:11 OPTION_EXIT  strangle closed after 0 bars — stop -119 ≤ -100 — pnl -144
13:02:35 OPTION_EXIT  strangle closed after 0 bars — target 105 ≥ 100 — pnl +81
13:02:59 OPTION_EXIT  strangle closed after 0 bars — stop -103 ≤ -100 — pnl -127
13:11:34 entry 07:11:00 → exit 07:11:34  (hold 34 s)
min hold across 200 trades = 0.04 min (2.4 s); median 1.10 min; 48/88 strangle trades held < 1 min
```

`bars_held: 0` on **every** exit. The ₹100 threshold is on the whole 2-leg structure; with NIFTY lot size 65 that is **₹1.54 per leg**, which is inside the bid-ask spread of these strikes (observed leg marks: CE bid 197.15 vs entry 206.24, PE 188.30 vs 179.77 — a ₹9/leg swing in five minutes). So the stop and the target both fire on quote bounce, not on market direction.

DB truth for the session (`trades` where `entry_time >= 28 Oct 00:00 IST`, queried ~13:30 — the churn was still running; superseded for the option book by the 13:52 census below):

| strategy | round trips today | gross P&L |
|---|---|---|
| `immediate_strangle` | **125** | **−₹20,992.40** |
| `ema_pullback` | 178 | +₹7,517.52 |
| `hf_equity_scalp` | 36 | −₹603.50 |
| `one_min_scalp` | 25 | −₹942.50 |
| `PE_instant_buy` | 1 | −₹1,186.90 (my manual SL — see PASS list) |
| `sma_crossover` | 1 | +₹922.20 (my partial + close) |

Order churn: `fill_count` 181 → 341 in the 13:05–13:10 window (~32 orders/min at peak), and the runner's own fee stack shows how much of this is self-inflicted: `immediate_strangle·NIFTY·1hour` = gross −₹5,952.70, commission ₹2,680.00, statutory ₹8,052.67 → net −₹14,566.60. **Fees are 144 % of the trading result.**

Full-session option book, re-counted at 13:52 IST from `/api/portfolio/runner/<id>.trades` (all rows with `kind: "option"`, `entry_ts` on 28 Oct — 206 trades, 6 runners; `tools/out/QA_STRANGLE_CHURN_STATS.json`):

| measure | value |
|---|---|
| gross P&L, all option trades | **−₹35,125.35** (mean −₹172.18, median −₹161.20) |
| fees on those trades (commission + statutory) | **₹39,336.55** (mean ₹192.83) |
| net P&L | **−₹74,461.90** — 53 % of the reported loss is transaction cost |
| exit reasons | `stop_loss` 198, `take_profit` 5, `manual_stop_loss` 1 (my TC-2.3 test) |
| win rate | **5 / 204 = 2.5 %** |
| hold time | min 2.5 s, median 49.4 s, p90 100.8 s, max 1,521 s |
| trades whose `\|gross\|` ≤ their own fees | **184 / 204 = 90.2 %** |

The last row is the whole argument: the bracket is smaller than the cost of using it. Both take-profit levels the strategy ever reached are **losers after fees** —

```
NIFTY strangle 22250/23200  gross +108.55  fees 193.59  net −85.04
NIFTY strangle 22300/23200  gross  +80.60  fees 193.12  net −112.52
```

so `immediate_strangle` as configured has **no winning trade available**: the maximum gross win (₹100 bracket) is 52 % of the round-trip fee (₹193). The three strangle runners lost ₹34,225 gross / ₹73,831 net between them in ~70 minutes on ₹10 L each of paper capital.

**Direction:** express strangle SL/TP in *per-leg points relative to the observed spread* (or as a % of credit) and add a minimum hold / re-entry cooldown for `tick` cadence. `max_reentries_per_day: 2` is present in `exit_policy` and does **not** stop this — re-entry after a stop clearly counts differently than intended. Worth re-validating as its own item.

### BUG-03 — the summary payload's headline capital/equity are wrong by ₹82.5 L and ₹10 L (landing cards, `/risk`, SSE, any report)

One SSE frame, all four numbers from the same instant:

```
sum of the 14 paper runners' allocated_capital = 1,02,00,000
snapshot.buckets.paper.capital                 = 1,02,00,000   (agrees)
snapshot.total_capital                         = 1,84,50,000   (+82,50,000 phantom)
sum of runners' equity                          = 1,01,12,634.45
snapshot.total_equity                           = 1,11,12,634.45 (+10,00,000 phantom)
deployed / daily_pnl / realized / open_positions  all match exactly
```

**Root cause — two independent mechanisms, one per field:**

1. **Total Capital (+₹82,50,000): the unscoped accumulator.** `portfolio_manager.py:2973-2984` — when a mode is requested, `total_capital` is a fresh `sum(s["allocated_capital"])`; when the snapshot is unscoped (`mode=None`, which is what the stream sends) it falls back to `self.total_capital`, an accumulator maintained by `+= config.allocated_capital` at line 1022 and `-= …` at line 1131. That accumulator has drifted +₹82.5 L and never re-derives: re-measured through 14:35 IST it is still **exactly** `18,450,000.00` unscoped vs `10,200,000.00` scoped, and the 5-minute sampler (`tools/out/QA_CAPITAL_DRIFT.jsonl`, 13:14→15:32) shows the same constant ₹82,50,000 delta on every one of its samples — i.e. the 27 Oct state-loss deletes are the obvious suspect, and nothing re-derives it after.
2. **Total Equity (+₹10,00,000): not drift at all — a second book is added to the numerator and not the denominator.** `:3024-3041` computes `combined_equity = equity + dashboard_equity` (and `combined_daily`, `combined_realized`, `combined_positions`) from `get_dashboard_book_summary()`, which returns the process-wide **`OptionPaperBroker` dashboard singleton** (`_get_dashboard_book()`, `:565-580` — `backtest.web.options_api.get_option_broker`, the "second execution engine" behind the standalone Options dashboard). Its state at 14:23 was:
   ```
   dashboard_book = {"exists": true, "capital": 1000000.0, "equity": 1000000.0,
                     "realized_pnl": 0.0, "unrealized_pnl": 0.0,
                     "open_positions": 0, "open_structures": 0, "positions": [], "structures": []}
   total_equity  1,10,24,970.88  −  dashboard_equity 10,00,000  =  1,00,24,970.88  = Σ paper runner equity ✓
   total_capital   1,02,00,000   (dashboard capital excluded — the denominator never sees it)
   ```
   So the phantom is an **empty** ₹10 L book — no positions, no P&L — folded into `total_equity` on **both** scoped and unscoped summaries (`?mode=paper` included, verified). This is why the delta looked like a constant rather than drift, and it is worse than drift in one respect: it is *by design* (`:3008-3011` "include dashboard book in summary so portfolio renders per-structure option trades"), so it will keep re-appearing, and once that book does hold structures the same lines push its unrealized P&L into `daily_pnl` and its realized into `realized_pnl` on a page labelled `Simulated fills`.

**Which surface actually shows it (corrected at 14:45 IST, after reading the render path rather than assuming it).** The seven metric cards on `/portfolio/paper` do **not** read these fields: `bucketMetrics()` (`portfolio.js:43-65`, the T2.1/"ticket P4.1 · C4 · AC-15" change) overwrites `total_capital`/`total_equity` with `buckets.paper.capital`/`.equity` whenever `#portfolio-page[data-mode]` is non-empty, and `renderMetrics()` is called through it at exactly one site (`:764`) — and the served HTML sets `data-mode="paper"` on `/portfolio/paper` and `data-mode="live"` on `/portfolio`. So on those two pages the cards are clean (PASS, §5), and BUG-03's real blast radius is every other reader of the payload:

| consumer | field it reads | value today | honest value |
|---|---|---|---|
| `components/landing_risk.js:159` (landing bucket cards, via `/api/portfolio/summary?mode=paper` → `data.portfolio`) | `total_equity`, `total_capital` | equity `1,10,07,656` / allocated `1,02,00,000` | `1,00,07,656` (the same call returns `buckets.paper.equity` correct, 4 lines below) |
| `components/landing_risk.js:166` + `updateGlobalHeader` (unscoped summary) | `total_capital` | `1,84,50,000` | `1,02,00,000` |
| `components/risk_page.js:124` (`/risk` dashboard) | `current.total_equity` | `1,10,11,111.37` | `1,00,11,111.37` |
| `/api/portfolio/stream` SSE frame, `/reporting`, any script (my own sampler included) | `total_capital`, `total_equity` | both phantoms | — |

The unscoped pair is the worst case because the two phantoms do not scale together: `total_equity 1,10,07,656 / total_capital 1,84,50,000 = 0.5966`, i.e. the landing header implies the book has lost **40 % of its capital** (₹74.4 L) on a day the honest number is −₹1.28 L. The implied return on the correctly-scoped pair (`1,00,07,656 / 1,02,00,000 = 0.9811`, −1.9 %) is the one that agrees with the runner rows.

Constant-not-drift proof, from the 5-minute sampler across all **10** samples 14:09→14:34: `total_capital − Σ runner capital = {8,250,000.00}` and `total_equity − Σ bucket equity = {1,000,000.00}` — a one-element set each, no movement, exactly as expected from an accumulator that left the correct value and an addition that is unconditional.

**Both phantoms visible in one response, from two adjacent fields (14:35:41 IST, `tools/out/QA_RISK_PAYLOAD_1435.json`):** `GET /api/portfolio/risk` carries the correct scoped bucket aggregate and the unscoped `current` block side by side —

```
current.total_capital  1,84,50,000.00   vs   buckets.paper.capital  1,02,00,000.00   -> delta exactly 82,50,000.00
current.total_equity   1,10,11,111.37   vs   buckets.paper.equity   1,00,11,111.37   -> delta exactly 10,00,000.00
```

so the Risk tab's own header (which renders `current`) disagrees with the bucket table 200 px below it, on the same page, from the same payload — the two blocks do not even have to come from different endpoints to disagree.

**Direction:** compute the unscoped totals from `get_bucket_aggregates()` (already correct) instead of the accumulator, re-derive the accumulator on restore, and make the equity total follow the same book set as the capital total — either show the dashboard book as its own row (it is not a paper runner) or exclude it from both cards, never from one.

### BUG-04 — three different P&L definitions in one matrix row

| runner | `daily_pnl` | `realized_pnl` | `open_pnl` | realized + open | equity check |
|---|---|---|---|---|---|
| `P3TICK_strangle_NIFTY_15min` | −12,729.52 | −5,772.00 | 0.00 | −5,772.00 | 9,87,270.48 = 10 L + **−12,729.52** |
| `QA14_strangle_NIFTY_1min` | −11,168.25 | −5,063.50 | 20.15 | −5,043.35 | 9,88,831.75 |
| `immediate_strangle·NIFTY·1hour` | −13,884.46 | −5,952.70 | 20.15 | −5,932.55 | 9,83,334.78 |
| `QA13_sma_TCS_15min` (live observation) | 493.00 | 429.20 | 493.00 | **922.20** | 3,00,922.20 = 3 L + **922.20** |

Two separate defects:

1. **`realized_pnl` is gross; `daily_pnl` and `equity` are net of commission + statutory fees.** P3TICK's `realized_pnl` (−5,772.00) is byte-identical to `options.gross_realized_pnl`, while `net_realized_pnl` is −12,729.52 — the ₹6,957.52 fee stack is invisible in the "Realized P&L" column and in the `realized_pnl` metric card, which is the bucket sum of that gross field (−₹60,564.03 vs the honest −₹82 k-ish; the card *does* match the rows — see §5 — both are simply gross).
2. **`daily_pnl` does not equal realized + open, and its definition differs per asset class.** TCS is the controlled experiment: 116 shares entered at 2445.90, half closed at 2453.30 (realized +₹429.20, confirmed by the API response `{"qty_closed":58.0,"realized_pnl":429.2,"remaining_qty":58.0}`), remaining 58 still marked +₹493.00 — `daily_pnl` read **493.00**, i.e. booking the profit *removed* it from the day's P&L. Its `equity` was right (+922.20) the whole time. Aggregate: `Σ daily_pnl = −22,793.68` vs `Σ(realized+open) = −58,890.13`.

**Direction:** one definition, stated in the payload (net of fees, realized+unrealized, session-scoped) and have the cards/matrix read it.

Refined at 13:58 IST — the runbook's `daily = realized + open` invariant is **only meaningful for runners spawned today**, because `realized_pnl` is lifetime while `daily_pnl` is session-scoped. Splitting the 14 runners on `created_ts` makes the picture much sharper than "8/14 mismatch":

| runner (created 28 Oct) | daily | realized | open | gap = daily − (realized + open) |
|---|---|---|---|---|
| `QA12_hf_ICICIBANK` (equity) | 413.60 | 413.60 | 0.00 | **0.00** |
| `QA13_sma_TCS_15min` (equity) | 922.20 | 922.20 | 0.00 | **0.00** |
| `QA13_ema_pool_5min` (equity) | −177.40 | 0.00 | −177.40 | **0.00** |
| `QA14_directional_BANKNIFTY_5min` | 0.00 | 0.00 | 0.00 | 0.00 |
| `QA14_strangle_NIFTY_1min` (option) | −25,549.89 | −11,835.20 | 0.00 | **−13,714.69** |
| `P3TICK_strangle_NIFTY_15min` (option) | −27,869.91 | −12,994.15 | 0.00 | **−14,875.76** |

So the invariant is *not* broken randomly: it holds exactly on every equity runner and fails on every option runner by almost precisely its own fee stack (₹12,364 and ₹13,525 respectively, plus a constant ₹1,350.37 in both). Two different reasons hide behind the same column: **options are excluded from `realized_pnl` because it is gross, equities are exact because they pay nothing at all** — see BUG-26.

Re-measured at 14:22 IST (bracketed by the 14:19/14:24 samples in `QA_CAPITAL_DRIFT.jsonl`) inside a **single** `GET /api/portfolio/equity/snapshot?mode=paper` payload, so no cross-endpoint or cross-time effect can explain it: `session.net_today −108,876.53` = `Σ per_runner.daily_pnl −108,876.53` ✓, but `Σ realized_pnl −99,301.43 + Σ open_pnl +898.40 = −98,403.03` — a **₹10,473.50 gap between two columns of one object**. The gap grows with every fee the day books, so the "Today" and "Realized" columns on the Equity tab drift apart as the session runs.

Re-check at 14:30:28 IST: the two aggregates stay internally consistent (`Σ per_runner.daily_pnl −119,021.53 = session.net_today` to the paisa; `Σ per_runner.equity 1,00,16,669.26 = session.equity` to the paisa), while the per-runner invariant has degraded further — only **3/14** runners now satisfy `daily = realized + open` (`QA12_hf_ICICIBANK`, `QA13_sma_TCS_15min`, `banknifty_ema_momentum`), and the gaps now run in **both** directions (`CE_instant_buy·NIFTY·1min +26,971.25`, `immediate_strangle −25,157.56`), so the column is not even consistently biased. `QA14_directional_BANKNIFTY_5min` now shows `daily +257.90` with `realized 0.00` **and** `open 0.00` — a day's P&L with no source column at all.

### BUG-26 — equity paper books zero costs, and it is the only "profitable" strategy today (S1)

259 equity round trips closed today (ema_pullback 178, hf_equity_scalp 48, one_min_scalp 32, sma_crossover 1) carry `commission_total = 0.00` on **259/259 DB rows**, `net_pnl = gross_pnl`, and `exit_reason` NULL on all of them — the fee model that exists in `config/brokers.yaml` (`mstock: commission flat ₹20/order`, STT intraday sell 0.025 %, exchange 0.00297 %, SEBI/IPFT ₹10/crore, GST 18 %, stamp 0.003 % buy) is never invoked on the equity path. The option path does invoke it (₹9,360 of commission booked on 234 strangle rows), so this is an asset-class gap, not a missing feature.

**Re-read against the DB at 14:55:30 IST (read-only, `forward_test`), and the gap is bigger than "today":**

| bucket (paper, `portfolios.mode='paper'`) | rows | `commission_total = 0` | `exit_reason` NULL | `net_pnl = gross_pnl` | commission booked |
|---|---|---|---|---|---|
| equity strategies closed today (`ema_pullback`, `hf_equity_scalp`, `one_min_scalp`, `sma_crossover`) | **306** | **306 (100 %)** | **306 (100 %)** | 306 (100 %) | ₹0.00 |
| option strategies closed today (`immediate_strangle` 461, `CE_instant_buy` 2, `PE_instant_buy` 2) | **465** | 0 | 0 | **465 (100 %)** | **₹18,520.00** |
| equity strategies, lifetime | **2,749** | **2,749 (100 %)** | 2,749 | 2,749 | ₹0.00 |

Two different holes, both at the write boundary: the equity path never computes a fee **or** an exit reason, while the option path computes both, books the commission into its own column, and **still** persists `net_pnl = gross_pnl` — so ₹18,520 of commission the engine definitely charged in memory is absent from the DB's own net figure on 465/465 rows (that half belongs to BUG-14; the option book's honest net today is ≈ −₹1.22 L against the −₹74,263.15 gross/net the table reports identically).

Re-costing today's actual fills through the repository's **own** `CommissionCalculator.for_broker("mstock")` (`tools/out/QA_EQUITY_FEE_RESTATE.json`):

| strategy | trades | gross P&L as shown | fees the repo already knows | true net |
|---|---|---|---|---|
| `ema_pullback·NIFTY50·15min` | 178 | **+₹7,517.52** | ₹20,951.36 | **−₹13,433.84** |
| `hf_equity_scalp` | 48 | −₹131.70 | ₹4,313.26 | −₹4,444.96 |
| `one_min_scalp` | 32 | −₹1,145.00 | ₹2,662.50 | −₹3,807.50 |
| `sma_crossover` (my TC-2.4 trade) | 1 | +₹922.20 | ₹97.67 | +₹824.53 |

**The one strategy the board shows green flips to a ₹13.4 k loss**, and the equity book as a whole is overstated by ₹28,024 today. The intraday rates above are the *cheapest* defensible assumption: `config/brokers.yaml` sets `mstock.default_segment: equity_delivery`, which prices the same 178 trades at ₹81,413 (net −₹73,896) because delivery pays STT on both sides plus ₹15.34/scrip DP. Which one applies cannot be recovered after the fact — `trades` records no `segment`, and `paper_runner.py:1514-1532` builds the equity trade row with `pnl` only (no `commission`, no `gross_pnl`/`net_pnl`, no `exit_reason`), so the drawer's Trades tab renders blank cost columns for every equity row by construction.

For a demo whose bar is "paper accuracy", a scalp of 138 ICICIBANK shares for ₹82.80 gross (QA12's real trade) printed as a *win* while ₹118 of costs go unbooked is the single most misleading number on the page after BUG-03. **Direction:** route equity fills through `CommissionCalculator` the way options already do, record `segment` + the fee stack on the trade row, and show "gross / costs / net" instead of one unlabeled P&L.

### BUG-28 — the circuit-breaker stress test is a one-way door that mutates the live breaker and destroys the day's P&L at rest (S1)

`POST /api/portfolio/test/breach` (runbook TC-4.6) is not a sandbox call on a live book. Four separate hazards, all in `manager.stress_test()` → `reset_daily_anchors()` + `simulate_crash()`:

1. **`tighten_limits` defaults to `true`.** `api/portfolio.py:1148` reads `data.get("tighten_limits", True)`, so the runbook's own curl — `{"crash_pct":0.25,"tighten_limits":false}` works, but a body that simply *omits* the key (`{"crash_pct":0.25}`) silently rewrites the account breaker.
2. **The rewrite is permanent for the process lifetime and bypasses the sanctioned path.** `portfolio_manager.py:3278-3281` assigns `self.supervisor.config.daily_loss_limit = 1_000.0` / `.max_drawdown_pct = 0.05` as raw attributes. `GlobalRiskConfig.update_limits()` (`risk_supervisor.py:60-91`) is documented as "the sanctioned path for mutating a live `GlobalRiskConfig`" precisely because it validates through the constructor; nothing here restores the old values, so a demo that runs TC-4.6 and then keeps paper trading is running the rest of the session with a **₹1,000** daily-loss breaker on a book losing ~₹1,227/min (measured 14:04). Verified in a scratch process, not against the server:
   ```
   update_limits({"max_drawdown_pct": 5.0})  -> ValueError: max_drawdown_pct must be between 0 and 1
   cfg.max_drawdown_pct = 5.0                 -> accepted; cfg now fails its own __post_init__
   ```
   i.e. the direct-write path accepts states the class forbids. A `max_drawdown_pct ≥ 1` written this way makes `check_portfolio_max_drawdown` (`:145-155`) unreachable — the drawdown breaker is silently *off*, with no log line.
3. **It splits the config into two disagreeing objects.** `stress_test` mutates `supervisor.config`; `_risk_configs["paper"]` (`:99`) is a *different* `GlobalRiskConfig`. `/api/portfolio/risk` renders `supervisor.config` (`api/portfolio.py:187-190`) while `/api/portfolio/risk/config` renders `get_all_risk_configs()` → `_risk_configs` (`:500-501`). After a tightened stress run the two endpoints report ₹1,000 and ₹250,000 as the daily-loss limit on the same tab; a subsequent UI save (`set_risk_config`, `:2337-2338`) re-points the supervisor back at `_risk_configs["paper"]` and silently un-tightens it. Not triggered today — deliberately, since proving it live would burn the day's numbers.
4. **`reset_daily_anchors()` persists.** `:2006-2017` re-baselines every runner and bucket and then calls `self._persist_state()`. So the *session's* `daily_pnl`/`daily_loss_used` are re-zeroed **in `portfolio_state.json`**, not just in RAM — after TC-4.6 the pre-crash day P&L is unrecoverable and P4-RESTORE's "match pre-restart numbers to the rupee" assertion can no longer be evaluated for 28 Oct.
5. **`simulate_crash()` injects synthetic price into the live buffers, across every bucket.** `:3251-3256` loops `self._runners.values()` with **no mode filter** (paper *and* live), and `paper_runner.apply_markdown()` (`:2641-2670`) **overwrites the last bar in the runner's own OHLC buffer** (`self._bars[symbol][-1] = new_bar` with `close = price×0.75`), then re-marks the portfolio and the option book from it. The strategies keep trading on that buffer, so EMA/SMA/velocity inputs after a stress run are computed from a fabricated candle — this build's own rule is that market data must come from the broker, and the risk page's "Export/verify" story assumes the marks are real. It also stamps the injected bar with a **naive** UTC string: `ts = (datetime.now(timezone.utc) + timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")` (`:3249`) — an aware clock formatted into a value that no longer states its own clock, which is the exact failure class the 2026-10-27 policy exists to stop and which the AST ratchet does not cover (see BUG-24's note).

**Direction:** make the endpoint non-destructive by default (explicit `confirm: true`), scope it to one bucket, snapshot-and-restore the limits it tightens, re-baseline anchors into a shadow view instead of the persisted one, and drive the markdown from a synthetic feed rather than rewriting a runner's real bar buffer.

---

## 3. S2 findings

### BUG-05 — no Greeks anywhere on the live path (TC-2.1, TC-4.2 FAIL)
Every option leg in `/api/portfolio/positions` carries `"delta": null, "theta": null, "iv": null, "greeks_source": null` and every structure `"net_delta": null, "net_theta": null`. At 13:40 IST the option book was momentarily flat, so `/api/portfolio/greeks?mode=paper` returned `"gamma": 0.0, "vega": 0.0, "theta": 0.0` on **every** row and `delta` was the share count of the open cash positions — while the same payload still emitted interpretation labels:

```json
"bias": {"delta":"long bias","gamma":"long γ","theta":"paying decay","vega":"long vol"}
```

— which is what the tab can do with nothing on the book. That observation was made on an empty option book and is **revised by the 14:47 IST measurement below**: with 3 option legs open, the endpoint does populate (per-strategy Γ 2.95/2.97/6.46, ν 1,712/1,723/1,567, θ −373/−638/−334, `net_delta 422.55`, `legs_priced 10/10`) and the bias labels are then arithmetically consistent with those values. What BUG-05 actually is: **the leg-level nulls never fill in** — `positions[]._greeks_source`, `legs[].delta/theta`, `net_delta/net_theta` are `null` on the runner payload at the same instant the same server returns numbers for the same `source_id` (the join key is there: `breakdown_by_strategy[].source_id` == `instance_id`), and the leg carries **no `gamma`/`vega` key at all**, so the Positions tab has nothing to render and the UI reads `0.00`. (This also blocks BUG-17's proper fix: `by_underlying.delta_notional` cannot be used while the leg delta is null.) And note BUG-34: the numbers on the Risk tab are real model output, but every option leg in them is priced at an **assumed** 15 % IV.

**Correction found at 13:40 IST — the Greeks are computed, they just never reach the runner.** The live log shows the risk engine firing on real values 12× during the session:

```
12:56:01 WARNING portfolio_delta_warning — Portfolio delta +815 (limit ₹800) — long bias
13:05:04 WARNING portfolio_delta_warning — Portfolio delta +832 (limit ₹800) — long bias
13:07:05 WARNING portfolio_delta_warning — Portfolio delta +868 (limit ₹800) — long bias
12:45:03 WARNING backtest.db — slow query: 485.1ms — INSERT INTO portfolio_greeks_history
         (snapshot_id, timestamp, net_delta, net_gamma, net_vega, net_theta, greeks_by_strategy,
          concentration_by_underlying, concentration_by_strike) VALUES (...)
```

So a portfolio-level delta exists and is written to `portfolio_greeks_history`, while `legs_detail[].delta/theta/iv` and `structures[].net_delta/net_theta/greeks_source` are `null` on the very same structures at the same instant (`PE_instant_buy`, `struct_20261028074000_0002`, marked 0.06 s earlier). This narrows the fix to a surfacing/join gap between the greeks snapshot and the bridge book — it is **not** a "broker gives no Greeks" data limitation, which is how BUG-05 read from the UI alone.

### BUG-06 — fills are not exchange-possible prices (TC-1.4 partially FAIL)
129 priced fills parsed from the log; **82 (64 %) are not multiples of ₹0.05** — e.g. `NIFTY26NOV23200CE SELL x1 @207.04`, `NIFTY26NOV22250PE SELL x1 @177.97`, `@193.11`. NSE option premiums tick at ₹0.05, so no real fill can print those numbers.

**Self-correction, 14:55 IST — the 100 % figure in the paragraph above was measured on the wrong object, and the true rate is lower but still damning.** `trades/aggregated` prices are *net structure premiums* (`legs: 2` strangle rows carry e.g. `entry_price: -376.12` = the two legs summed, sign-coded for the short side), and a two-leg net has no obligation to land on the exchange grid, so testing it against ₹0.05 is meaningless. The valid test is the **per-leg order**, and the orders ledger answers it directly at 14:49 IST (`/api/portfolio/orders?mode=paper`, 910 option orders):

| field | on the ₹0.05 grid | off it |
|---|---|---|
| `requested_price` (the quote the engine chose) | **910 / 910 (100 %)** | 0 |
| `avg_fill_price` (what the book booked) | 177 (19.5 %) | **733 (80.5 %)** |

One row makes the mechanism visible end to end: `NIFTY26NOV22200PE`, `requested_price 177.90` (exchange-possible), `slippage 0.18`, `slippage_pct 0.0010118`, `avg_fill_price 178.08` — the nearest legal prices are 178.05 and 178.10. Same row also shows `order_type: MARKET`, `created_ts == filled_ts` to the microsecond and `quantity == filled_qty == 65`. So the claim becomes: **every quote the engine picks is a real exchange price; 80.5 % of the prices it books are not**, because the 10 bps slippage is applied after the choice and quantised to paise. On the single-leg trade rows (the only ones where the trade table is per-leg: `legs == 1`, 85 rows) the same split shows 60/85 entries and 35/62 exits off grid.

**Root cause:** `paper_trading.py:640-643` takes the side-aware quote (`quote_providers.py:760 execution_quote()` → bid for SELL, ask for BUY, verified working: `_side_price` raises `NoExecutableQuoteError` otherwise) and then applies `self._apply_slippage()` = `price × (1 ± 0.001)` quantized to 0.01 (`paper_trading.py:1228`). Check: CE leg bid 206.45 × 0.999 = **206.24** ✓ matches the log; PE 179.95 × 0.999 = **179.77** ✓. So the paper book charges the **full spread and then another 10 bps on top**, and rounds to paise.

Corollary — the Orders tab's "Slippage" column is circular: `orders.summary.avg_slippage_pct = 0.000915`, i.e. it always reports the slippage model constant, never a market observation. Every row is `MARKET`/`FILLED` with `created_ts == filled_ts` to the microsecond.

Note the asymmetry that feeds BUG-02: entry basis = bid × 0.999 for a short leg; the mark path (`quote_providers.py:767 mark_quote()`) uses the **opposite side with no slippage** — so a structure opens at an instant adverse mark ≈ spread × contracts.

### BUG-07 — "Flatten all" is on the paper page and is cross-bucket
`_portfolio_center.html:34` renders `#btn-emergency` and `portfolio.js:773-775` correctly hides it on the paper page (`emergencyBtn.hidden = (PAGE_MODE === "paper")`) — **TC-1.1 passes on that button**. But `base.html:154` renders a second, global `#risk-strip-flatten` ("Flatten all") on *every* page, and `risk_strip.js:392-397` POSTs `{"reason": "sticky_strip_emergency"}` with **no `mode`**, which hits `emergency_flatten_all(mode=None)` → `portfolio_manager.py:1947`: `targets = list(self._runners.values())` = all runners, all buckets, plus the manager halt and every per-bucket latch. The confirm button reads "🔴 YES, FLATTEN EVERYTHING". So the page-level safety intent ("paper has no real capital at risk, hide the big red button") is undone by a global big red button one row above it that also flattens LIVE. Fix direction: gate `#risk-strip-flatten` on `PAGE_MODE` and send `mode` in the body (the endpoint already supports bucket scoping).

### BUG-08 — two clocks in one tab (UX-4 still open, confirmed on the wire)
All persisted/API stamps are now correctly aware UTC (`"created_ts": "2026-10-28T07:42:32.752608+00:00"`, Postgres `timestamp with time zone`) — **the 2026-10-27 clock policy holds at rest.** The bug is display-only but user-visible: `orders_tab.js:86 clock()` regex-extracts `T(\d{2}:\d{2}:\d{2})` from the ISO string and prints it unchanged (**UTC**), while `portfolio.js:424 execTimeLabel()` converts to browser-local (**IST**). Same fill, two times, 5h30m apart, one screen apart. Trade history has the same defect via `risk_board.js:253` `(t.exit_ts || "").slice(0,16)`.
Related, at the boundary rather than in the UI: `/api/broker/status` returns `"expires_at": "2026-10-28T18:06:13.702227"` (naive, IST) and `/api/market/status` returns `"time": "13:25:22"` (naive) — both should state their own clock per the policy.

### BUG-09 — `#risk-mode` cannot control the tab it lives in (UX-2 / UX-5 still open)
`portfolio_intelligence.js:52-53` — `const pageMode = page.dataset.mode; if (pageMode) return pageMode;` — so on `/portfolio/paper` the dropdown is ignored by Greeks / Concentration / Correlation / Regime / Activity. `risk_board.js:216,231` — `state.mode || $("risk-mode").value` — never consults `data-mode`, so the 4 risk gauges **and Trade history** start scoped to *all* buckets on a paper-only page. Result: one dropdown, two scopes, same tab.

### BUG-10 — `instance_id` filter is ignored (TC-5.2 FAIL)
```
/api/portfolio/trades/aggregated?mode=paper&limit=1                                        → total 551, first row immediate_strangle·NIFTY·1hour
/api/portfolio/trades/aggregated?instance_id=e2f0322f…&mode=paper&limit=1                   → total 551, first row immediate_strangle·NIFTY·1hour
/api/portfolio/trades/aggregated?instance_id=9d90b87e…&mode=paper&limit=1                   → total 551, first row immediate_strangle·NIFTY·1hour
```
Three different runners, identical result set — the parameter is accepted and dropped. (Earlier `mode=paper&limit=200` reported `total: 497`; it is now 551 because of BUG-01's churn.) Deep-dive → Trades therefore cannot show one runner's history unless the client filters after the fact.

### BUG-11 — Trade history hides the entry time the API already sends (UX-1 still open)
`/api/portfolio/trades/aggregated` returns both `entry_ts` and `exit_ts` on every row (`"entry_ts": "2026-10-28T07:41:00+00:00", "exit_ts": "2026-10-28T07:41:34.918814+00:00"`), and `DB trades.holding_period_minutes` exists. `risk_board.js:253` renders only `exit_reason + exit_ts.slice(0,16)`. So the operator sees "stop_loss 2026-10-28T07:41" and can neither see when the trade opened nor compute the 34-second hold that is the whole story here.

### BUG-27 — five risk-limit surfaces, one of them enforced, and the enforced one is in no config file (S2)

The Risk tab presents limits as if they were the operator's settings. Census of every daily-loss / drawdown number the running process exposes or reads, taken 14:02 IST:

| surface | daily-loss | drawdown | read by the live paper path? |
|---|---|---|---|
| `GlobalRiskConfig` dataclass default (`risk_supervisor.py:46-47`) | **₹250,000** | **0.80** | **yes — this is the only one that can halt paper today** (`_evaluate_risk` → `supervisor.evaluate`) |
| `_risk_configs["paper"]` (`portfolio_manager.py:99`) | ₹250,000 (default) | 0.80 (hardcoded) | shown by `/api/portfolio/risk/config`; supervisor is re-pointed here only on a UI save |
| `_risk_configs["live"]` (`:100-102`) | ₹50,000 (hardcoded) | 0.25 (hardcoded) | live bucket only — no live runners exist, so untestable today |
| `config/forward_testing.yaml:30-31` | `daily_loss_limit_pct: 0.02` | `max_drawdown_pct: 0.10` | **no** — loaded by `ForwardTestingConfig`/`engine.py`, i.e. the standalone `python -m backtest.forward.engine` CLI and `bucket_risk.py` defaults, not the `:5000` runner path. `grep` shows the only consumers of `BUCKET_RISK_LIMITS` are `api/portfolio.py:181,518` and `web/app.py` — display |
| `config/segments.yaml:31,41` → `/api/portfolio/risk` `segments.*` | equity_intraday ₹30,000 / options_index ₹50,000 | 12 % / 15 % | by design **live-only**: `_segment_runners()` (`:379-392`) filters `self._runner_bucket(r) == "live"`, and its docstring says a paper runner must never trip a segment breaker |
| per-instance (`api/portfolio.py:957-965`, `paper_runner.py:990-991`) | 15 % of allocation | 25 % (API) / 80 % (dataclass) | yes, `paper_runner.py:3840-3856` pauses the individual runner |

Consequences, in the order they mislead:

1. **The number the operator can edit is not the number that runs.** Nothing reads `config/forward_testing.yaml` into `GlobalRiskConfig`, and `config/risk_limits.py:37-47` explicitly *refuses* those keys ("those limits live in GlobalRiskConfig / config/forward_testing.yaml — one value, one file"). Both files it names as the authority are unread by this path, so the live breaker's real value is a constructor default that exists in no config file at all. Editing the YAML to tighten risk on the running server changes nothing, silently.
2. **The live book is running 8× looser than the config claims.** Drawdown 0.80 of `peak_equity 10,144,488` = an ₹81.1 L allowance; the YAML's 0.10 = ₹10.1 L. Daily loss ₹250,000 vs the YAML's 2 % of the ₹1.02 crore actually deployed = ₹204,000. Today's measured trajectory (`tools/out/QA_CAPITAL_DRIFT.jsonl`, 5-min SSE samples): −₹67,567.52 at 13:49 → −₹74,137.42 at 13:54 → −₹81,211.77 at 13:59 → **−₹87,344.91 at 14:04**, i.e. **−₹1,227/min**, projected **≈ −₹192,800 at 15:30** — under both, so today neither the real breaker nor the intended one halts the session, and the difference is invisible either way. At 14:02 the paper bucket was already at 33.8 % of its ₹250 k latch and the UI showed `halt_state: NORMAL` with no gauge.
3. **Per-runner breakers are decorative at today's sizes.** All 14 runners were spawned with the API defaults (0.25 / 0.15 — the dataclass's 0.80 is unreachable via HTTP but wins for any runner built in-process or restored from a snapshot lacking the field, so the same "instance breaker" means two different things depending on how the runner was made). Worst instance loss today is `immediate_strangle` at −₹38,267 against a ₹1,50,000 latch = 25 % of its own trip point.
4. **`config/risk.yaml` is a fourth file with the same key names** and a header note that the forward engine does not read it (`:13-18`) — correct, but three same-named limit files with no pointer from the UI is a footgun for whoever tightens risk before a demo.

**Direction:** one loader that builds `GlobalRiskConfig` from `config/forward_testing.yaml` (or a new `risk.yaml` profile) at boot, log the resolved limits at startup, label every displayed limit with its source (config / default / segment / instance), and make `paper_runner`'s defaults match the API spawn defaults so "instance breaker" has one meaning.

### BUG-33 — the Equity tab's session strip shows a day that began at ₹61.5 L on a ₹1.02 crore book (S2)

`GET /api/portfolio/equity/snapshot?mode=paper` at 14:22 IST (the `net_today −1,08,876.53` sample bracketed by `QA_CAPITAL_DRIFT.jsonl`) returns a `session` block whose three headline numbers cannot be true at the same time:

```
day_start_equity  61,49,577.50      <- implies the book started the day at ₹61.5 L
equity           1,00,26,666.96
net_today          −1,08,876.53     <- = Σ per_runner.daily_pnl (correct)
allocated_capital 1,02,00,000.00
peak_equity       1,01,44,488.44    drawdown_pct 0.0116  (both consistent with equity, not with day_start)
```

`equity − day_start_equity = **+₹38,77,089.46**`, i.e. the strip simultaneously claims a **+38.8 % day** (`day_start_equity` → `equity`) and a **−₹1.09 L day** (`net_today`). The runbook's TC-3.1 assertion "session summary metrics … match the Strategy Instances matrix" therefore fails on the session block while passing on the per-runner table (see §5 — 14/14 rows match to the paisa). The anchor is stale rather than mis-derived: `_day_start_equity` is captured at boot/`reset_daily_anchors()` (`portfolio_manager.py:2011`) and is **not re-baselined when runners are spawned or their capital is added mid-session** — today 8 runners existed at the 12:44 cutover and 6 more (₹39 L of allocation) were added between 13:0x and 13:3x, so the "day start" still reflects the pre-spawn book. Note the same anchor feeds the breaker maths (`daily_pnl = equity − day_start` in `_evaluate_risk`), which is why `net_today` is right for the *bucket* while `day_start_equity` is wrong as a *displayed* value: the per-runner `daily_pnl` values are session-scoped at their own spawn, so summing them gives the honest −₹1.09 L, whereas the bucket-level anchor never moved.

**This is not only a display bug — the anchor is load-bearing for a percentage on the Risk tab.** Simultaneous read at 14:37:19 IST (`tools/out/QA_RISK_PAYLOAD_1435.json` + `equity/snapshot`):

```
risk.buckets.paper.daily_pnl      −124,432.12
risk.buckets.paper.daily_pnl_pct    −0.020234
daily_pnl / day_start_equity      = −0.0202342551   <- exact match: the gauge divides by the stale anchor
daily_pnl / allocated_capital     = −0.0121992274   <- the honest −1.22 %
```

so the Risk board reports the day as **66 % worse in percentage terms than it is** (−2.02 % vs −1.22 %), while the *other* gauge in the same bucket object (`drawdown_pct 0.013`) is computed against `peak_equity 1,01,44,488.44` and is arithmetically correct — one payload, two denominators, one of them broken. Re-check of the same pair against `current` (14:35 sample): `daily_loss_used 123,327.56 / daily_loss_limit 250,000 = 0.4933` = `daily_loss_pct` ✓, and `(peak − equity)/peak = 0.013038` = `drawdown_pct` ✓, so the loss-consumption and drawdown gauges deserve a PASS (§5); only the return percentage inherits BUG-33.

**Direction:** re-baseline the displayed day anchor when the bucket's allocated capital changes (or report `day_start_equity` as `allocated + prior_net`), and show `net_today` next to the derived return so the two cannot disagree silently.

---

### BUG-34 — every option Greek on the Risk tab is computed at an assumed 15 % IV, and the payload reports nothing missing (S2)

`GET /api/portfolio/greeks?mode=paper` at 14:47:30 IST, with 3 open option positions on the book:

```
model: "black_scholes_full_revaluation"    legs 10/10 priced    compute_ms 0.43
net_delta 422.55   net_gamma 12.39   net_theta −1,345.00   net_vega 5,002.27
missing_greeks: []
warnings: ["IV unknown for 3 leg(s) — assumed 15%"]
scenarios: {"crash": 27,643.0, "spot_down_2pct": 4,276.0, "spot_up_2pct": 14,079.0,
            "vol_down_5pts": -24,943.0, "vol_up_5pts": 25,037.0}
bias: {"delta":"long bias","gamma":"long γ","theta":"paying decay","vega":"long vol"}
```

The three legs that got the assumed IV are **all three option legs** (`PE_instant_buy·NIFTY` θ −334.13 / ν 1,566.96, `CE_instant_buy·BANKNIFTY` θ −637.72 / ν 1,722.90, `QA14_directional_BANKNIFTY` θ −373.15 / ν 1,712.40 — the equity legs correctly carry 0 for γ/θ/ν because they are cash). Vega is by construction linear in σ and theta quadratic in it, so −₹1,345/day of decay and +₹5,002 per IV point are not measurements of this book; they are Black–Scholes evaluated at a constant nobody chose (`DEFAULT_IV = 0.15`, `intelligence/greeks.py:42`, substituted at `:168` — `vol = float(leg.iv if leg.iv and leg.iv > 0 else self.default_iv)`).

Two engines read the same book and take opposite honesty stances, and the code says so out loud:

| engine | when `leg.iv` is null | provenance it emits |
|---|---|---|
| the runner's own bridge (`options_bridge.py:1036-1040`) | refuses — comment in full: *"Best-effort analytics: None (not 0.0!) when the spot, expiry or an implied vol for this contract is unavailable — **an invented delta is worse than none**"* | `delta: null, theta: null, iv: null, greeks_source: null` (this is BUG-05) |
| the intelligence greeks engine (`intelligence/greeks.py:168`) | substitutes 15 % and totals it into the Risk tab | `missing_greeks: []` — i.e. *nothing is missing* — with the admission buried in `warnings` |

So the two halves of one contradiction: the Positions tab shows `—`/`0.00` where a modelled number could be labelled as modelled, and the Risk tab shows a confident `−₹1,345.00 / ₹1 IV pt` where the honest answer is "IV unknown". Neither is what the project decided: `_leg_vol()` (`:2240-2258`) already looks for broker `vol`/`implied_volatility` in the contract metadata and, failing that, refuses (`greeks_source` stays `None`); the memory-noted rule for this repo is that market inputs come from the broker and are never fabricated, and the mStock chain metadata evidently supplies no IV today (hence 3/3 legs on the fallback). The scenario grid the runbook's TC-4.5 stress table reads (`crash +₹27,643`, `vol_up_5pts +₹25,037`) is the same numbers squared, because it re-prices ±5 vol points around an assumed centre.

**Direction:** carry the assumed-IV flag per leg into the totals payload (`assumed_iv_legs: 3/3`, and a `net_theta_modelled: true`), label the Risk-tab cards with `IV assumed 15 %` when it applies, and either source IV from the broker chain (the chain surface exists; check whether the option-quote frame carries `implied_volatility` at all — if it does not, this is a broker limitation and the label is mandatory) or drop vega/theta from the totals and keep delta, which barely moves with σ. Fixing BUG-05's join must not silently import BUG-34's assumption into the runner rows: the leg should carry `greeks_source: "bs:iv=assumed-15%"` so the UI can tell them apart.

---

### BUG-35 — the segment dimension the risk tab enforces per-segment limits on is empty, and its one member is an equity runner (S2)

`GET /api/portfolio/risk` at 14:58:20 IST returns both segment breakers as zero-population rows:

```json
"segments": {
  "equity_intraday": {"allocated_capital": 800000.0, "broker": "dhan",  "daily_pnl": 0.0, "deployed": 0,
                      "equity": 0, "halted": false, "runner_count": 0,
                      "risk": {"daily_loss_limit": 30000, "max_deployed_capital": 800000, "max_drawdown_pct": 12}},
  "options_index":   {"allocated_capital": 1200000.0, "broker": "mstock", "daily_pnl": 0.0, "deployed": 0,
                      "equity": 0, "halted": false, "runner_count": 0,
                      "risk": {"daily_loss_limit": 50000, "max_deployed_capital": 1200000, "max_drawdown_pct": 15}}
}
```

Against the live book (same instant, `/api/portfolio/summary`):

| observation | value |
|---|---|
| runners carrying any `segment` | **1 of 14** — `ema_pullback·NIFTY50·15min` |
| that runner's `instrument.type` | **`equity`** (`target_type SYMBOL_UNIVERSE`, 50 cash symbols: RELIANCE, TCS, …, LTIM) |
| runners with `segment: null` | 13 — including **all 8 option runners** |
| `segments.*.runner_count` | 0 and 0, while `options_index` is that one equity runner |
| `runner.broker` on all 14 | `paper` (not a broker); `segments.equity_intraday.broker` says `dhan` while the runner's `source` is `mstock` |
| `concentration.by_segment` | one row: `options_index`, `runner_count 1`, `allocated 1,000,000`, `daily_pnl +3,758.76` — i.e. the equity runner again, correctly summed |
| `concentration.by_broker` | one row: `name "paper"` — the **mode**, in a field the UI labels by broker |

So the per-segment limits the runbook's §2/TC-4.4 edits (`₹30,000` / `₹50,000` daily loss, 12 % / 15 % drawdown, `max_deployed_capital`) are attached to a dimension that carries no population: with `runner_count: 0` and `equity: 0` the supervisor has nothing to measure, which is the mechanical reason (already noted as designed at `portfolio_manager.py:380-385` in BUG-27) those limits cannot trip for paper — **and** it means the labels are wrong even where the plumbing does run: the only runner inside `options_index` trades cash equities, and the segment's `broker: "dhan"` does not match any runner on the book. The Risk tab therefore renders two breaker cards at `₹0 / 0 runners` next to limits that read as active protection.

**Direction:** decide whether `segment` is a spawn-time capital bucket or a derived property of `instrument.type`; today it is neither consistently (one equity runner says `options_index`, eight option runners say nothing). Derive it at create from the instrument type + underlying class, backfill the state on restore, and either drop the segment cards when `runner_count == 0` or show them as unpopulated — a breaker card with no members is a misleading metric by the runbook's own rule. Also stop putting `mode` in `by_broker.name` and `dhan` in a segment that has no dhan runners.

---

### BUG-36 — the durable audit trail is dark, and every surface that should say so says nothing (S2)

Measured at 15:09 IST against the live `forward_test` database (read-only queries) and the running server's own log:

| table | rows | what the same instant reports in memory |
|---|---|---|
| `alerts` | **0** | 213 `[alert]` lines logged today (`grep -ac "[alert] "` on `tools/out/qa_server_20261028_cutover.log`), 3 still active, >=50 in the 48-h broker history |
| `orders` | **0** | `/api/portfolio/summary` at 15:04: `order_count: 2184`, `fill_count: 2184` |
| `positions` | **0** | `open_positions: 8` (10 rows via `/api/portfolio/positions`, banked in `tools/out/QA_POSTCLOSE_OPEN_1512.json`) |
| `strategy_signals` | 53 lifetime, **0 in the last 24 h** | newest row is 2026-10-22, while 8 restored + 6 QA runners evaluated all day |
| `portfolio_greeks_history` | 13,625 (5 written in the last 5 min, newest 15:12:57 IST) | - |

The greeks row is the important one: it proves the persister **is attached and healthy in the serving process** (one PID owns `:5000` — `netstat` -> 20664), so the empty `alerts` table is not "no database". The cause is the latch described in the BUG-23 re-check: `_disabled` is written once and consulted forever (`if table in self._disabled: return`), so the 213 alerts raised since 12:43:03 were each accepted, notified (`notified_strategies` shows `AlertNotifier` ok), displayed, and dropped.

Then the API hides it. `GET /api/alerts/history?from=48h` -> `{"success": true, "count": 50, "source": "memory"}` (`tools/out/QA_ALERT_HISTORY_1500.json`). `api/intelligence.py:225-240` calls `intel.persister.alert_history()`, which returns `None` for a disabled table, and on `None` falls through to `intel.broker.history()` — the volatile deque — and still returns 200 with `success: true`. The `source` field is the only hint and nothing in the UI reads it. So the operator asking "what alerts fired this week?" gets a confident answer that a restart erases, which is precisely the runbook's TC-8.1 audit-trail assertion.

The `orders` half is not a latch, it is a missing write. The paper path never inserts an order row:

- `paper_runner.submit()` (`forward/paper_runner.py:355-363`) builds the simulator's in-memory `Order` object and registers it in the runner; nothing touches the DB.
- The only code on this path that references the row model is `strategy_adapter._save_signal_to_db()` (`:1391-1400`), and it only *checks* whether the order row exists — finding none every time, it sets `effective_order_id = None` "to avoid failure". That branch has fired on **53/53** rows: `SELECT count(*) FROM strategy_signals WHERE order_id IS NULL` -> 53 of 53.
- The only real writer is `simulator/portfolio.py:1166 _upsert_order()`, i.e. the **backtest** save-state path, which paper never calls.

**Direction (no code was changed in this run):** three separable fixes. (1) `_disabled` must not be a session-lifetime decision — retry on a backoff, and expose `persister.disabled_tables` in `/api/portfolio/intelligence` and `/health`. (2) `_guarded` must log the exception — the current message asserts a cause ("apply migration 005") it cannot know, and that wrong assertion is what let this sit all session; the 12:43:03 line is the only trace of a whole day of lost alerts. (3) `/api/alerts/history` should answer `durable: false` (or 503) when it had to fall back, so the UI can label the list "in-memory, lost on restart" instead of presenting it as history. And decide explicitly whether paper orders belong in `orders` at all: if yes, `submit()` needs the write the simulator already has; if no, delete the table's promise from the docstring so nobody plans a reconciliation against it.

---

### BUG-37 — the test suite spawns runners into the production book, and `portfolios` is a third capital accumulator nobody reaps (S2)

At 15:04 IST three different numbers in this system claim to be "the capital":

| source | value |
|---|---|
| `/api/portfolio/summary` -> `total_capital` (what the landing cards and `/risk` read) | **₹1,84,50,000** |
| Sum of `allocated_capital` over the 14 live runners (honest) | **₹1,02,00,000** |
| `SELECT sum(initial_capital) FROM portfolios WHERE status = 'active'` | **₹2,15,00,000** (39 rows) |

The payload's ₹82,50,000 over-report is BUG-03. The at-rest number is **₹1,13,00,000** above the honest book and is *not* the same set of rows (orphan capital in the table is ₹1,33,00,000), so this is a second, independent accumulator leak — fixing BUG-03 in memory will not change what the database says about this account.

How fixture rows get in. `tests/forward/test_phase3_subsecond_options_and_ticks.py:873-900` does not mock the manager — it constructs a real `PortfolioManager(...)`, then `mgr.add_runner(RunnerConfig(name="LIVE-STRANGLE", allocated_capital=500_000, mode="paper", source="mstock", instrument={"type": "option", ...}), start=True)`. Anything that then persists resolves `DatabaseManager.from_env()`, i.e. the `.env` URL — the same Postgres database the live server is reading and writing this minute. `"LIVE-STRANGLE"` appears nowhere else in the repository except that fixture, and the DB holds **13 rows** with it, each `status='active'`, each ₹5,00,000:

```
2026-10-24 15:28:41 -> 17:57:46 IST        9 rows   ₹45,00,000
2026-10-28 11:57:44 IST                    1 row    ₹ 5,00,000
2026-10-28 12:49:06 / 12:58:11 / 12:59:53 IST   3 rows   ₹15,00,000
```

Three of today's four land inside this QA session (the cutover restart was 12:42-12:44); I did not invoke pytest during the run and cannot attribute the trigger from here — that needs the shell history around those four minutes. The blast radius is contained in one respect worth recording: those 13 portfolio rows carry **0 trade rows** (`LEFT JOIN trades`, 15:20 IST), so no fixture P&L is mixed into the ledger — it is the capital row and the name that leak, not fake fills.

Two consequences beyond arithmetic:

- **Nothing reaps them.** 39/39 rows are `active`; matching each row's `[8-hex]` suffix (the convention `analytics/portfolio.py:476` uses) against the live `instance_id` set leaves **27 orphans worth ₹1,33,00,000**, the oldest from 2026-10-01. Strategies that stopped existing keep holding capital on the books.
- **The names read as real money.** `LIVE-STRANGLE [6f33be4a]` is indistinguishable on any portfolio/risk list from a live strangle book, and the same table also holds `RoundTrip`, `BacktestTest`, `F01Accept`, `DryRunTest`, `BarTest`, `RealStrat_sma_crossover` — artefacts of earlier test runs. `_save_signal_to_db` (`strategy_adapter.py:1352-1372`) is a second door for the same thing: when a signal's portfolio row is missing it **creates one** with `initial_capital=Decimal("100000")` and `status="active"` purely to satisfy a FK, so a journal side effect grows the capital table.

**Direction:** give the test suite its own DB URL or an outer transaction that rolls back (`start_portfolio_intelligence`'s own docstring says "Tests never call this", so the isolation intent exists but is enforced nowhere); reap `portfolios` rows whose id is not in the live runner registry at boot, or mark them `status='closed'`; and give FK-satisfying rows a distinct status so a book created to satisfy a constraint can never be summed as allocated capital.

---

## 4. S3 / S4 findings

- **BUG-12 `structure_id` collisions.** At 12:46:11 two different runners booked the same key: `ENTRY_FILLED strategy=PE_instant_buy structure_id=struct_20261028071500_0001` and `ENTRY_FILLED strategy=immediate_strangle structure_id=struct_20261028071500_0001`. The scheme is `<minute><per-runner sequence>`, unique only within a runner, yet it is the value that lands in `orders.tag.structure_id`, `positions.position_key`, and the LOM request body. Any cross-runner query on it can hit the wrong book.
- **BUG-13 Intent journal is dark.** `SELECT count(*) FROM option_intent_events` → **0**, while the log shows 4,283 `INTENT_RECEIVED`. Cause: `paper_runner.py:1269 OptionsBridge(...)` never passes `intent_journal=`, so `options_bridge.py:593 if self.intent_journal is None: return` makes the whole journal a no-op on the paper path (the docstring says this is deliberate). The runbook's disputed-fill audit trail does not exist for paper — decide and document, don't leave it half-wired.
- **BUG-14 Persistence gaps at rest.** `SELECT count(*) FROM positions` → **0** (open positions live only in `portfolio_state.json`); `trades.net_pnl = gross_pnl` for **3,166/3,166** rows (census at 13:52 IST; 437/437 of them option-strategy rows) although `commission_total`/`slippage_total` are populated on every row; **`position_id` is NULL on 3,166/3,166 rows**, so a persisted trade cannot be tied back to its `structure_id`; `exit_reason` is NULL for **2,711/3,078 (88 %)** overall and **241/363 (66 %)** of today's — and the 88 % is not scattered: queried per strategy at 13:58 IST, `exit_reason` is NULL on **every equity row closed today** (ema_pullback 178, hf_equity_scalp 48, one_min_scalp 32, sma_crossover 1 = 259 rows) and on **0 of the 235 option rows** (immediate_strangle 234 + PE_instant_buy 1). So the missing exit reason is entirely the equity trade row (`paper_runner.py:1514-1532` emits `pnl`, `win`, `coid`, nothing else), not a general persistence leak; `trades` has no `mode`/`session_date`/`kind`/`structure_id`/statutory-fee column (bucket isolation works only via `portfolios.mode` — verified: **0** live-mode trade rows today, so isolation itself holds).
  **The root cause is the write boundary, not the fee model.** The same closed structure is netted correctly in memory before it is persisted — `/api/portfolio/runner/<id>.trades[0]`:
  ```json
  {"gross_pnl": -1186.90, "commission": 20.0, "statutory_fees": 97.15, "net_pnl": -1304.05}
  ```
  −1,186.90 − 117.15 = **−1,304.05 exactly**. The row that lands in `trades` for that same trade is `gross_pnl = net_pnl = -1186.90`, `commission_total = 40.00`, and the ₹97.15 of statutory fees is dropped because there is no column for it. Any report built by reading the DB (reconciliation, month-end, the `/reporting` page) is therefore systematically better than the engine's own numbers by the whole statutory-fee stack — ₹39,336.55 today.
- **BUG-15 A runner that can never trade, shown as 🟢 RUNNING.** `MAX_LOSS_EXCEEDED: worst-case loss ₹27,814 exceeds the per-trade ceiling ₹25,000` × **11,410** for `CE_instant_buy·BANKNIFTY·1hour`. The BANKNIFTY ATM call can never fit the ₹25 k per-trade ceiling, so the runner is permanently inert while the matrix says RUNNING, the metric cards count its ₹10 L capital, and it emits ~285 log lines/minute. Surface an "cannot trade — risk ceiling below minimum position size" state at spawn and in the row.
- **BUG-16 Entry timestamps quantised, exits not.** Tick-cadence entries persist `entry_ts` at exact minute boundaries (`07:41:00+00:00`) because the stamp comes from `self._bar_dt`, while exits keep microseconds (`07:41:34.918814`). Hold durations are therefore systematically biased, and a 34-second hold looks like a 0-second one after rounding. The same split leaks into the *identity* scheme: consecutive `structure_id`s from one strategy in one minute are `struct_20261028071600_0002` (bar-minute) and `struct_20261028072142_0001` (wall-tick, 07:21:42), so the id's own clock depends on which path opened the structure — which also makes BUG-12's collision window a minute wide for one path and a second wide for the other.
- **BUG-17 Concentration uses strike/spot notional.** `/api/portfolio/concentration?mode=paper` labels itself honestly — `"measure": "gross underlying notional (|units| × spot)"` — and then uses that measure for a 60 % alert. Re-measured 14:55:10 IST with 3 option positions open: `by_strike.BANKNIFTY_54200.exposure = 1,628,730` and `BANKNIFTY_54300.exposure = 1,628,730` (30 lots × spot 54,291 each) for two legs whose combined premium cost is ₹62,659, giving `by_underlying.BANKNIFTY = {exposure: 3,257,460, pct: 60.18, high: true}` → `{"pct": 60.18, "threshold_pct": 60.0, "type": "high_concentration", "underlying": "BANKNIFTY"}`. The alert moved from NIFTY to BANKNIFTY between the 13:5x and 14:55 samples as the book turned over — it tracks whichever strike was last touched, not risk. **The fix is already in the same object**: each `by_underlying` row carries `delta_notional` beside `exposure` (BANKNIFTY Γ-notional 188,390 vs spot notional 3,257,460 — 17× apart; book totals 1,560,330 vs 5,412,652), and on that basis BANKNIFTY is **12.14 %**, i.e. no alert at all. For the 7 cash underlyings the two fields are identical, so switching the basis is safe for equities and only changes options. Earlier notes said `delta_notional` was unusable while leg-level Greeks were null (BUG-05) — that is superseded: the intelligence engine populates Γ-notional per underlying today (at an assumed IV, see BUG-34), so the field is available even before BUG-05's join lands.
- **BUG-18 Session attribution across the overnight gap.** Three structures from 27 Oct were cash-settled at 12:45:59 IST on 28 Oct: `hold 1,275.5 min`, `exit_reason expiry_settlement`, P&L −6.50 / +2,239.25 / +8,395.50 → **+₹10,628 booked into 28 Oct's `session_date`** for trades opened on the 27th. Their entries were stamped `2026-10-27T10:00:30+00:00` = **15:30:30 IST**, i.e. 30 seconds after the close, so the pre-close guard let a bar through after 15:30. (Positive control: `count(*) where holding_period_minutes between 325 and 335` = **0** — the old +5h30m clock artifact is genuinely gone.)
- **BUG-19 Live gate refuses for an unactionable reason.** `POST /api/portfolio/runner/create {"mode":"LIVE", …}` → `400 live order gateway refused: confirm_live=True is required`. Sending `"confirm_live": true` in the body returns **the identical error** — the flag is a boot-time constructor argument (`portfolio_manager.py:257` ← `confirm_live_orders`, `live_gateway.py:73`), not a request field. Consequence: P4-3's real assertion ("live entitlement allows only `long_call`/`long_put`, so a live strangle is refused") **could not be exercised** — `.env` has no `ALLOW_LIVE_ORDERS`/`CONFIRM_LIVE_ORDERS`, so the request never reaches `risk/limits.entitlement.live = ["long_call","long_put"]`. Bucket-blocking works (0 LIVE runners exist, all 14 rows `mode=paper`), but the entitlement layer is untested.
- **BUG-20 Export & series surfaces are not what the UI claims.** `POST /api/portfolio/watch/export` → `200 application/json` listing `C:\learning\back-test\charts\2026-10-28\<runner>.csv|.png` and `points: 6` for the hourly strangle; nothing is downloaded and absolute server paths are exposed to the browser. `GET /api/portfolio/watch/series/<iid>?limit=5` → `{"label": null, "series": []}` for a runner that demonstrably has trades. `/api/portfolio/equity/snapshot` `day_closes` has only 3 dates (2026-09-29, 10-27, 10-28), so the equity-curve canvas draws three points.

- **BUG-21 Warmup holds the shared pool on 14–24 s scans of `market_data_cache` while ticks flow (S2).** Spawning runners during the session produced the four worst queries of the day, back to back (`tools/out/qa_server_20261028_cutover.log`, 19 slow-query warnings total):
  ```
  12:51:54 slow query: 13741.0ms — SELECT symbol, ts, open, high, low, close, volume FROM market_data_cache WHERE timeframe…
  12:52:29 slow query:  3306.0ms — INSERT INTO portfolios (portfolio_id, name, initial_capital, …)
  12:52:30 slow query: 23567.0ms — SELECT … FROM market_data_cache WHERE timeframe…
  12:52:37 slow query: 22148.9ms — SELECT … FROM market_data_cache …
  12:52:31 slow query: 22469.9ms — SELECT … FROM market_data_cache …
  ```
  `WARMUP_COMPLETE … tf=5min target=60 quorum=48` for four runners inside 12:51:41→12:52:38. Each new runner re-reads ~29k 1-minute rows from a 46M-row Timescale hypertable on the **same shared pool** P1 introduced, and a 3.3 s `INSERT INTO portfolios` queued behind it. The SSE kept ticking (warmup is async — TC-1.3 passes), but a spawn costs the box ~24 s of pool time, and four spawns in a minute is a self-inflicted stall. Also note `market_holidays` re-reads at 280–589 ms five times in the session — a calendar table that changes yearly.
- **BUG-22 A restart pages "CRITICAL data feed stale" 3.5 minutes later (S3).** Cut-over at 12:42:55, and at 12:46:01–12:46:05:
  ```
  WARNING  data_feed_stale — Stale option mark on CE_instant_buy·NIFTY·1min (NIFTY): empty quote row
  WARNING  data_feed_stale — Stale option mark on PE_instant_buy·NIFTY·1min (NIFTY): empty quote row
  WARNING  data_feed_stale — Stale option mark on CE_instant_buy·BANKNIFTY·1hour (BANKNIFTY): empty quote row
  CRITICAL data_feed_stale — Data feed stale (mstock): no bar for 188s (limit 120s)
  ```
  All four went to `backtest.alerts.outbound`, i.e. off-box. The ring is cold for the first ~90 s after a re-login/restart (`9` empty-quote-row marks), and the bar-silence SLA counts from process start, so any restart after ~13:00 can produce a CRITICAL that is an artifact of the restart, not of the feed. Fix direction: suppress feed-staleness alerting until warmup/quorum reports READY (or measure "no bar since last known bar", not "since boot"). The alerts did self-heal by 12:46:05 and `stale_marks_count` stayed 0 for the rest of the session, so this is an alerting-quality bug, not a data bug.
- **BUG-23 Alert history is not persisted on this database (S3).** At 12:43:03, once per boot: `WARNING backtest.intelligence.persistence — [intelligence] table alerts missing — apply migration 005; persistence for it disabled this session`. Every alert fired today (25 WARNING + 1 CRITICAL + 50 INFO strike-clustering + 12 delta-breach) exists only in the log file. The runbook's "check the alert log afterwards" verification has no table to query, and this is the third live-DB schema-drift finding on this box after the `trades` column gap (BUG-14). Migration 005 needs applying to `forward_test`.
  **Re-check at 15:09 IST — the diagnosis in that log line is wrong, and the log line is all we get.** `alerts` exists (13/13 columns match `db/models.py` exactly) and `alembic_version` is `019`, so revision 005 is long applied; yet the table still held **0 rows** while **213** `[alert]` lines were logged today, because `_guarded()` (`intelligence/persistence.py:149-167`) treats *any* exception whose text contains `does not exist` as "table missing", latches `self._disabled[table]` **for the life of the process**, and logs a fixed sentence that never includes the real exception. Whatever actually failed at 12:43:03 cannot be recovered from the log, nothing re-checks, and `/api/alerts/history` still answers `source: "memory"` (see BUG-36, BUG-38).
- **BUG-24 A second IST clock outside `timekeeping.py` decides where today's charts go (S4).** `watch_export.py:41` defines `_IST = timezone(timedelta(hours=5, minutes=30))` and `:45` returns `datetime.now(_IST)` to name `charts/<YYYY-MM-DD>/`, without importing `backtest.timekeeping` (which owns `EXCHANGE_TZ` / `session_date`). `AGENTS.md` says timekeeping is "the only place a clock gets decided", and the AST ratchet cannot catch this because the call is already aware — so the policy has a hole exactly where a directory name is decided. Same module family, same class of issue: `watch_series[].ts` is persisted into the drawer as a naive string (`"2026-10-28 12:46:00"`, no offset) while every other stamp on the payload carries `+00:00`.
- **BUG-25 Ring recentering is not observable, so the runbook's own P2-RING method cannot be executed as written (S4).** The pass criterion is "watch server logs … observe ring recentering while an open option leg stays pinned". There is **no log statement in the recenter/pin path**: `quote_providers.py` logs only at `:379` (synthetic unknown token), `:574`, `:916`, `:1557` — the ±6-strike re-center, the pin refcount, the budget eviction (`:1182-1204`) and `release_hot_ring` (`:1246`) are all silent. The only visible trace is the generic broker line `Subscribed N WS token(s) (mode=full): [tokens]`, which does not say *why* (recenter vs new entry vs post-relogin replay), does not name the underlying/strike/ATM, and prints tokens a reader cannot resolve without the instrument master. I could therefore only verify P2-RING *behaviourally* (0 stale marks, 0 mtm_failures, 0.06 s mark age) and infer recentering from subscribe/unsubscribe churn. One INFO line per recenter — `ring recenter NIFTY atm 22700→22750 added=[…] evicted=[…] pinned=[…]` — would make the runbook testable.
- **BUG-30 The Activity log is a process-lifetime ring buffer, and it logs deployments that never happened (S3 — the buffer stays in the queue; the preview/deployment split is PARKED with the playbooks, §1.1).** TC-8.1's "audit trail" is `deque(maxlen=1000)` in memory (`portfolio_manager.py:201`), appended at `:523` and read back at `:540` — no table (`grep` of `db/models.py` finds no audit model), and `state_store.py` writes no audit key, so **the trail is erased by every restart**. Today's 12:44 cutover already did that to the previous session's control actions; the whole post-restart trail is 39 `[AUDIT]` lines in the log and 39 rows on `/api/portfolio/audit`. Worse for the *content*: `POST /api/playbooks/<id>/spawn` is documented as "returns runner config; caller creates (**no side effects**)" (`api/playbooks.py:141-157`) yet still calls `_audit_log("SPAWN", …)` at `:212`. I called it 6 times to test the round trip and got **6 `SPAWN playbook pb_qa7_strangle` rows with zero runners created** (runner count stayed 14, verified). An operator reading the Activity tab after a demo cannot tell a deployment from a preview — the one thing that tab exists to answer. Split the verb (`SPAWN_PREVIEW` vs `CREATE`) and persist the trail.
- **BUG-31 `POST /api/playbooks` validates nothing about the structure it is told to trade (S4) — PARKED (§1.1).** All four of these returned **201 Created**: `structure_type: "banana"`, `""` (empty), `"iron_condor"`, `"short_straddle"` — `Playbook.from_dict` (`playbooks/models.py:144-171`) takes `data.get("structure_type", <default map>)` and never checks it against a known set, then `to_expression()`/`to_selections()` pass it straight through as `expr["type"]`. The repository already has the vocabulary — `risk_limits.py:53-66 _KNOWN_STRUCTURES` and each strategy's `execution_spec()` menus — and the refusal only happens later, at runner create, for a strategy that declares a spec. A playbook created through the UI (6 chained `prompt()` dialogs, `playbooks.js:240-252`) can therefore sit in the picker carrying a structure that no strategy can execute, and the operator finds out only when Deploy returns a 400.
- **BUG-32 An option playbook deployed onto an equity strategy silently loses its entire content (S4) — PARKED (§1.1).** `/api/playbooks/pb_qa7_strangle/spawn` with `strategy: "sma_crossover"` returns **200** and a `runner_config` whose `expression` is `null` **and** whose `playbook_selections` is `null` — the same call with `immediate_strangle` or `directional_options` correctly produced `{"type":"strangle","quantity":1,"exit":{…,"max_loss_inr":5000.0,…}}`. Reason: `api/playbooks.py:194-205` only attaches selections when `get_strategy(name).execution_spec()` exists, and `api/portfolio.py:912-923`'s refusal branch is inside the `instrument.type == "option"` gate, so an equity target accepts the create with `playbook_id`/`playbook_version`/`playbook_snapshot` recorded and **zero playbook behaviour applied**. The drawer then shows a playbook fingerprint for a runner that never used it. This is reachable from the UI's own Deploy fallback (`playbooks.js:170` prompts the operator to *type* a strategy name), so the mismatch is a typo away.
- **BUG-29 A runner can sit PAUSED with no recorded reason (S4).** `QA13_sma_TCS_15min` is `PAUSED` at 14:05 and `GET /api/portfolio/summary` returns only `{"name":…, "status":"PAUSED", "warmup_reason": null}` for it — no `pause_reason`, no `halt_reason`, and the same field is absent from its `portfolio_state.json` entry. PAUSED is overloaded: operator pause (TC-1.5, which I did fire manually on this runner), breaker pause (`paper_runner.py:3840-3856`), circuit-breaker pause (`_evaluate_risk` → `runner.pause()`), and the fail-closed boot state (`_restore_state` docstring: "Runners saved as RUNNING come back PAUSED"). After a restart an operator cannot tell "you stopped this" from "the risk engine stopped this" from "it came back asleep and needs a Resume", and a silently-asleep runner is a silent no-trade runner. Add a `pause_source` + `paused_at` + `paused_by` triple to the runner payload and show it in the row tooltip.
- **BUG-24 addendum — the clock ratchet's blind spot is a formatting call, not a constructor call.** `tests/test_timekeeping_policy.py:268-293` sweeps four AST patterns (`utcnow`, bare `now()`, `replace(tzinfo=…)`, a redefined IST). An already-aware `datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")` matches none of them yet produces the same class of damage — a stamp that no longer states its own clock. `portfolio_manager.py:3249` does exactly this for the stress-test bar, and the same shape appears in `watch_export.py:45`. Direction: add a fifth ratchet pattern for `.strftime(` on an aware datetime outside `timekeeping.py` (or require `iso_utc()` for any stamp that leaves the process), so the policy "serialize with `iso_utc()`" is enforced rather than advised.

- **Latent, not observed today (S4) — export filenames are derived from `config.name`, not `instance_id`.** `watch_export.py:72-73` sanitises the runner *name* into `<safe>.csv/.png`; two runners sharing a display name would overwrite each other's EOD chart. No collision among today's 8 export-eligible runners (checked), but this build has previously run duplicate same-name runners (the duplicate EMA runners flagged on 23 Oct), and the matrix's own `duplicate_of` field proves the app tracks that state.

---
- **BUG-38 The migration chain is forked, so the schema cannot be fixed by the documented route (S3).** `db/alembic/versions/20261008_1200_020_option_intent_journal.py` and `db/alembic/versions/20261013_1200_020_add_upstox_source_to_portfolios.py` both declare `revision = "020"` with the same `down_revision = "019"`. Read-only probe at 15:23 IST: `alembic heads` emits `UserWarning: Revision 020 is present more than once` and prints **two** heads, and `alembic current` against `FORWARD_TEST_DB_URL` is **`019`**. The drift is already real in both directions — `option_intent_events`, which only revision 020 creates, exists in the database while the version row says 019 (the model is `db/models.py OptionIntentEvent`; BUG-13 shows the table has 0 rows). I deliberately did **not** run `upgrade head` on a live session database. The QA point is that this is the mechanism behind BUG-14's missing columns and BUG-23/BUG-36's dead alert table: the fix anyone would attempt first — "apply the migration" — is ambiguous on this box, and there is no CI check that `alembic heads` returns exactly one. Re-number one of the two revisions (the intent-journal one is already applied in substance) and reconcile with `stamp`.
- **BUG-39 There is no end-of-day exit for a non-expiry structure — proven at the close, not inferred (S1).** The runbook's P3-EOD assertion fails both ways: open option positions are neither squared off at EOD nor carried under an explicit, enforced "ride to settlement" rule. The mechanism was read before the close (the pre-registered table in this section): `_maybe_intraday_squareoff` (`forward/options_bridge.py:1408-1445`) keys its square-off to `squareoff_minutes_before` **on the expiry day**, and none of the three resolver inputs that knob needs is present in any of the 8 live option expressions, so the 15:10 path cannot fire. The close confirmed it — `grep -ci squareoff` = **0** over the whole day's log, the EOD watcher file empty, and at 15:37:40 three `expiry: 2026-11-23` structures still open with `mark_ts` frozen at 15:37:41 IST and +₹2,021.25 unrealised between them. So the book carries option delta/gamma/theta across an unmeasured overnight gap with no mark, no stop evaluation (the supervisor evaluates on ticks, and §8's TC-4.6 result shows what "no ticks" means for the breaker), no square-off, and no persisted state that says the carry was chosen. BUG-18 is the invoice for this: an overnight-held structure settled in the *next* session and booked +₹10,627 into 28 Oct. **Direction:** make the choice explicit per structure in the execution spec (`ride_to_settlement` vs `square_off_at_eod`), enforce it with an EOD pass driven by the *session* clock rather than only the expiry clock, and when a leg is deliberately carried, mark it and label it on the Positions tab so overnight exposure is visible instead of implied.

## 5. What passed (keep this, it is the useful half)

| TC | Assertion | Evidence |
|---|---|---|
| TC-1.1 | page-level `#btn-emergency` hidden on paper | `portfolio.js:773-775` (but see BUG-07 for the global twin) |
| TC-1.3 | warmup is asynchronous, non-blocking, and honesty-flagged | `QA13_ema_pool_5min` → `warmup_status=DEGRADED`, seeded 2,923 bars across **49/50** symbols in 64.7 s; TCS `READY` 60/29.3 s; BANKNIFTY `READY` 60/24.1 s; UI stayed responsive |
| TC-1.4 / §5 P2 | fills come from real two-sided WS depth | `quote_source: "live:mstock+bidask"` on all 14 runners; `synthetic:bs` **0** occurrences in the cutover log; `QUOTE_ONE_SIDED` **2**, `NO_EXECUTION_QUOTE` **3** (both on genuinely one-sided strikes, correctly refused rather than priced off LTP) |
| TC-1.4 (cards) | Bucket scoping of the metric cards works | `bucketMetrics()` (`portfolio.js:43-65`) replaces `total_capital`/`total_equity` with `buckets.<mode>` before `renderMetrics()`, and it is the **only** call site (`:764`); served HTML sets `data-mode="paper"` on `/portfolio/paper`, `data-mode="live"` on `/portfolio`. So the 7 cards on those pages show `1,02,00,000` / `1,00,07,656` — the correct scoped pair — even though the payload underneath still carries both phantoms (BUG-03). **Card-vs-DOM rendering itself remains `UI-NOT-VERIFIED`; this row asserts the code path + payload, read at 14:45 IST.** |
| TC-1.4 / TC-3.1 (aggregates) | Bucket aggregate == sum of runner rows, to the paisa | 5-minute sampler, all **10** samples 14:09→14:34 IST: `buckets.paper.daily_pnl − Σ per_runner.daily_pnl = 0.00` and `buckets.paper.realized_pnl − Σ per_runner.realized_pnl = 0.00` on every sample (max abs delta 0.00 over the set), plus `capital`/`equity` in the 14:30 snapshot. The roll-up is not the problem; the *definition* of the fields being rolled up is (BUG-04). |
| TC-4.1/4.2 (Greeks engine) | The greeks endpoint is fast, complete and internally consistent | 14:47:30 IST: `model black_scholes_full_revaluation`, `legs 10 / legs_priced 10`, `compute_ms 0.43`, `missing_greeks []`, per-strategy rows keyed by `source_id` = the runner `instance_id`, cash legs correctly at Γ=ν=θ=0 with `delta` = share count, option legs Γ 2.95/2.97/6.46 and ν 1,712/1,723/1,567, and the four `bias` labels agree in sign with `net_delta 422.55` / `net_gamma 12.39` / `net_theta −1,345` / `net_vega 5,002`. Do not break the `source_id` join when fixing BUG-05, and do not break the honest zeros for equity when fixing BUG-34. |
| TC-4.1/4.2 (gauge maths) | Risk gauges are arithmetically self-consistent | 14:35:41 IST, one `/api/portfolio/risk` payload: `daily_loss_used 123,327.56 / global_config.daily_loss_limit 250,000 = 0.4933` = `daily_loss_pct` ✓; `(peak_equity 1,01,44,488.44 − equity)/peak = 0.013038` = `drawdown_pct` ✓; `deployed_pct 0.046` consistent with `deployed_capital/capital`. Limits echoed truthfully (`breach_mode PAUSE_AND_HOLD`, `max_drawdown_pct 0.8`) — which is what makes BUG-27's “those limits are in no config file” finding land. The one gauge that lies is `daily_pnl_pct`, and it lies because of BUG-33's anchor. |
| §5 P2 (quote legality) | the price the engine *chooses* is always exchange-possible | **910/910** option orders have `requested_price` on the ₹0.05 grid (14:49 IST census) and the side-aware rule holds (`BUY` at ask, `SELL` at bid; `NoExecutableQuoteError` fires otherwise). The off-grid problem is confined to the post-choice slippage multiply — so a fix belongs in `_apply_slippage`, not in `execution_quote()` (BUG-06). |
| TC-1.5 | pause / filter / status transitions | `{"action":"pause"}` → `status: PAUSED`, positions row gains `"stale": true`, footnote path intact |
| TC-2.2 | frozen-mark honesty | paused TCS row: `"stale": true, "status": "PAUSED"`, position still listed, `mark_ts` retained |
| TC-2.3 | through-the-market stop refused | `modify_stop_loss 400` on a LONG option marked 323.10 → **409** `"stop-loss 400.00 is at or above the current price 323.10 — it would exit immediately; use Close instead"` |
| TC-2.3 | valid stop arms, then triggers | `322.5` armed → fired at mark `322.35`, `exit_reason: manual_stop_loss`, `pnl −1,186.90`, rule cleared from `position_rules`, **persisted to `trades`** |
| TC-2.4 | multi-leg structures close atomically | `close_fraction 0.5` on a strangle → **409** `"option structures close atomically (all legs together) — use Close All, or 100%"` |
| TC-2.4 | target validation mirrors stop | `modify_target 2400` on LONG equity at 2453.30 → **409** with a readable reason |
| TC-2.4 | partial close keeps the armed stop | TCS 116 → `qty_closed 58, realized 429.20, remaining 58`; `position_rules {"TCS":{"stop_loss":2440.0}}` still armed |
| TC-2.4 | full close works while paused | `close_all` on PAUSED TCS → 58 @ 2454.40, `realized_pnl 922.20`, rules cleared |
| TC-3.1 (data) | Equity tab's aggregation is paisa-exact | `GET /api/portfolio/equity/snapshot?mode=paper` returns **14/14** runners, one row each with `{instance_id, name, strategy, target, mode, status, equity, daily_pnl, realized_pnl, open_pnl, trades, trades_today, win_rate, bars_processed}`; `Σ per_runner.equity 1,00,16,669.26 == session.equity` and `Σ per_runner.daily_pnl −1,19,021.53 == session.net_today`, both to the paisa at 14:30:28 (and again at 14:22). The runners are the same 14 as the matrix — no runner is dropped or double-counted. What is wrong is the *definition* inside the rows (BUG-04) and the session anchor (BUG-33), not the sum. **Tab clicking itself is `UI-NOT-VERIFIED` (hidden viewport).** |
| TC-3.2 | Orders tab has the data the runbook wants | `tag: {kind:"option", lot_size:65, lots:1, structure_id, position_id, phase:"entry"/"exit"}`; `quantity=filled_qty=65`; `broker:"paper"`; 2-leg strangles do produce 2 entry + 2 exit rows |
| §5 P3 (audit-table schema parity) | No *column* drift on the audit tables at 15:09 IST | `alerts` 13/13 and `orders` all columns match `db/models.py` exactly (introspected vs `PortfolioAlert.__table__` / `Order.__table__`), `alembic_version` = `019`. Recorded because BUG-23's log line told the operator the table was missing and to "apply migration 005" — the table is present and revision 005 is long applied; the empty tables are a persistence defect (BUG-36) and a forked revision chain (BUG-38), not schema drift. |
| §5 P1-DB | DB is not on the hot path | `pg_stat_activity` for `forward_test`, sampled every 60 s for **45 minutes** (13:08:25 → 13:52:26 IST, 45 samples, `tools/out/QA_DB_ACTIVITY.txt`): `pg_activity_total = 3`, `pg_activity_busy = 1` on **every single sample** with 14 live runners + SSE + WS + my API polling. The P1 shared-pool fix holds — but see BUG-21 for the spawn-time exception, and note the sampler's own `trades` query errored (`column "realized_pnl" does not exist`) throughout, which is BUG-14's schema gap caught empirically |
| §5 **P2-RING** | Held option legs never lose live depth while the ring recentres | Pre-subscribe at 12:43:07-08: `Subscribed 30 WS token(s) (mode=full)` + `26 WS token(s) (mode=full)` (NIFTY 64xxx / BANKNIFTY 62xxx rings). Recentre/evict churn visible in the broker log while books were open: 12:55:20 `+3 / −64247`, 13:00:44 `+2 / −64267`, 13:02:59 `+64267`, 13:09:36 `−64353` (after that structure closed), 13:22:37 `+64247` (re-entry). Against that, every runner reported `mark_stale: false`, `mtm_failures: 0`, `quote_error: null`, SSE `stale_marks_count: 0` for all 14 runners all session, and the open `NIFTY26NOV22750PE` leg was marked `mark_age_seconds: 0.055656` (SLA 240 s). **PASS on the runbook's stated criteria**; the recentre itself is not observable (below). |
| §5 P3 timing | Sub-second MTM | `last_mtm_ts`/`mark_ts` inside the same 100 ms (`2026-10-28T08:04:43.267796+00:00` → `…290493`), marks updating on tick rather than bar |
| TC-6.1.3 | Watch export writes real artifacts | `POST /api/portfolio/watch/export` → 200, `watch export: 4 runner(s) exported to C:\learning\back-test\charts\2026-10-28`; 4 × `.png` (30–122 kB) + 4 × `.csv` on disk at 13:24:30. The 15:30 automatic run is armed and logged: `market-close exporter armed: fires 15:30 IST daily (charts/)` — to be confirmed against `charts/2026-10-28/` mtimes after close |
| TC-6.1.4 | Option book has the rows the drawer renders | `options.open_structures_detail[0]`: `label "NIFTY long_put 22750"`, `side LONG`, `lot_size 65`, `entry_price 344.29`, `current_price 341.40`, `unrealized_pnl −187.85`, `entry_cost 22,378.85`, `open_pnl_pct −0.84 %`, `expiry 2026-11-23`, `bars_held 24`, `legs_detail[0].trading_symbol "NIFTY26NOV22750PE"` with its own mark/`mtm_failures` |
| TC-6.2 (data) | Signal Log + Trades tabs have per-runner data | `/api/portfolio/runner/<id>` for `PE_instant_buy` → `trades: 29`, `signals: 15`; newest signal `{"kind":"OPTION_MTM","reason":"option MTM +59","ts":"2026-10-28T08:04:39.905124+00:00"}`. Trade rows carry `entry_ts`, `exit_ts`, `exit_reason`, `gross_pnl`, `net_pnl`, `statutory_fees`, `structure_type`, `strikes`, `expiry`, `label` — so UX-1/BUG-11 is purely a column-render gap, not missing data. Equity-pool runner returns `signals` with pool ranks (`"BUY signal \| pool rank score −0.0018 → BUY 8 @ 11187.00"`), and `universe_symbols` is present for the chip cloud. **Rendering itself is `UI-NOT-VERIFIED` (hidden viewport).** |
| TC-6.3 (data) | Config tab has the fingerprints + exit policy | `strategy_bundle.bundle_files[]` with `path` + `sha256` per file (`plugins/strategies/pe_instant_buy.py` `e07302b6…`), plus `params`, `playbook_id/version/snapshot`, `strategy_code_changed`. `options.exit_policy` for `PE_instant_buy` is all-`null` (stop/target/points/max_bars) with `max_reentries_per_day: 2` — the "ride to settlement" truth UX-3 asks to surface. **Caveat: `exit_policy` and the expression's `exit` block are two different renderings of the same policy in one payload**; only the drawer shows the first, `Positions` shows neither. |
| TC-7.1 (API) | Playbook CRUD, versioning and default guard | `GET /api/playbooks` → 3 defaults; `POST` 201; `PUT` bumps `version` 1→2→3 with `updated_at` re-stamped and `created_at` preserved (both aware UTC); `playbook_id` is pinned on update (`:108`); `DELETE pb_default_banknifty_delta` → **400** `"Cannot delete built-in playbook pb_default_banknifty_delta"`; `DELETE` of my 4 QA playbooks → 200 and the store is back to 3 rows at v1 (verified, no residue). `POST /api/playbooks/<id>/spawn` is genuinely side-effect-free: 6 calls, runner count stayed 14 |
| TC-7.1 (spawn contract) | Phase-3 selections carry the playbook | `immediate_strangle` / `directional_options` → `runner_config` with `instrument {"type":"option"}`, `expression: null`, `playbook_selections {"type":"strangle","quantity":1,"exit":{"stop_loss_pct":0.5,"take_profit_pct":1.0,"min_days_to_expiry":1,"signal_flip":true,"reenter":false,"max_loss_inr":5000.0}}`, plus `playbook_id/version/snapshot`. `max_loss_per_trade: 5000` correctly maps to `exit.max_loss_inr` — the playbook's per-signal cap does reach the expression |
| TC-7.1 (catalogue) | the spawn form's data dependency is satisfied | `/api/strategies` → **29** rows, `signal_kind` present on 29/29, 11 `option` (`CE_instant_buy`, `PE_instant_buy`, `immediate_strangle`, `directional_options`, …) / 18 `equity`, each with `execution`, `eligible_instruments`, `params`, `readable_criteria`, `version` — so `loadSpawnForm()`'s equity/option optgroups have real data. The cold-load ordering half of `UX-6` stays `UI-NOT-VERIFIED` (hidden viewport); `window.loadSpawnForm` *is* exported (`portfolio.js:1746`), so the `prompt()` fallback at `playbooks.js:170` should only be hit when portfolio.js has not initialised |
| TC-8.1 (plumbing) | audit entries are well-formed and scope-filtered | `/api/portfolio/audit` → 39 rows, every one `{ts (aware UTC), action, scope, instance_id, detail}`, `scope` values `paper`/`playbook` as documented, `?scope=`/`?limit=` honoured (clamped 1–1000). Content is the problem — BUG-30 |
| §5 P4-RESTORE | restart fidelity | 8/8 runners restored, `created_ts 2026-10-27T08:57:51+00:00` preserved, no hold jumps to the 325–335 min band |
| §5 clock policy | aware UTC at rest | `trades.entry_time/exit_time` = `timestamp with time zone`; every API stamp carries `+00:00`; `test_timekeeping_policy` suite green (25 passed) at `85791e7` |
| bucket isolation | paper ≠ live at rest | `trades` joined to `portfolios.mode='live'` for today: **0 rows**; SSE `buckets.live.capital = 0`; all 14 runner rows `mode=paper` |
| merge safety | PR #46 merge introduced **no** test regressions | subset suite at merged tip `85791e7` and at PR tip `d6448c7` in scratch worktrees: **66 failed / 368 passed / 10 errors on both**, `diff` of the sorted FAILED lists → `IDENTICAL_FAILURES`. The 66 are pre-existing box/PR state, not this merge |
| leg arithmetic | leg P&L maths is right | CE `SHORT 65 @206.24 → 197.15 = +590.85` (591.5 expected), PE `@179.77 → 188.30 = −554.45`, structure net `−386.01 → −385.45` ✓ |

Runbook items **not executed** (and why): TC-4.4 §2 regime/limits edits (would mutate shared risk config mid-session), **TC-4.6 the −25 % stress crash — deliberately not run; see BUG-28** (`tighten_limits` defaults to `true` when omitted, the tightened limits are never restored, and `reset_daily_anchors()` `_persist_state()`s the re-baselined day, so executing it destroys the evidence §8 is still collecting and makes 28 Oct's P&L unrecoverable; its validation-bypass half was proved in a scratch process instead), TC-4.7 breaker reset + TC-3.1 snapshot tab clicks at pointer level (Browser viewport hidden — `UI-NOT-VERIFIED`), P3-EOD 15:10 IST square-off (time-boxed, **pending** — watched by `tools/out/qa_eod_watch.py` from 13:30). Note also: the runbook's `OPTIONS_INTRADAY_SQUAREOFF_MINUTES=20` pre-flight step does not exist in this build — the knob is `squareoff_minutes_before` inside the instrument *expression*, default 30 (`options_bridge.py:352`), and it is absent from `.env`/`config/*.yaml`.

---

## 6. Suggested fix order

1. **BUG-01 + BUG-02 + BUG-26 together — the three "the numbers are not real" bugs.** Everything else in this report is downstream of 206 churned option round trips and 259 cost-free equity round trips. Until entries respect the timeframe, the strangle thresholds are spread-aware, and the equity path calls the `CommissionCalculator` the repository already has, no P&L figure on this UI means anything.
2. **BUG-03 + BUG-04** — make the payload totals and the P&L columns one honest definition; re-derive capital from the bucket sums that already exist (the bucket aggregates are paisa-exact — §5), stop adding the dashboard book to `total_equity` but not `total_capital`, and state in the payload whether each P&L is gross or net. Fixing the payload also fixes the landing cards and `/risk` without touching `bucketMetrics()`, which is already doing its job.
3. **BUG-07 + BUG-28 + BUG-39** — the safety-surface bugs, and BUG-39 is the one with real money attached. BUG-07 is a one-line JS gate + send `mode`; it is a real-money footgun sitting above a paper page. BUG-28 is the same class with a wider blast radius: the breaker stress endpoint should be confirm-gated, bucket-scoped, and restore its limits before it can be run on a session anyone cares about. Until then, treat TC-4.6 as end-of-day-only and expect `daily_pnl` to reset — §8 ran it at 16:41 IST and all three consequences are measured (₹1,23,280.90 injected with no market behind it, breaker still `NORMAL` at 156 % of its own limit, `day_start_equity` re-baselined into `portfolio_state.json`). BUG-39 is the missing control rather than the missing display: nothing squares off or marks a structure whose expiry is not today, so the overnight book is unprotected and the next day's P&L inherits it (BUG-18).
4. **BUG-27** — one loader for `GlobalRiskConfig`, log the resolved limits at boot, label each displayed limit with its source. Cheap, and it is the difference between "we are protected" and "we think we are".
5. **BUG-05 + BUG-34 / BUG-06** — Greeks dark on the runner, Greeks modelled on an assumed IV on the Risk tab, and off-grid fills: all three decide whether paper results can be quoted to anyone as accuracy. BUG-05 is a surfacing join (`breakdown_by_strategy[].source_id` already equals `instance_id`, and `compute_ms` is 0.43 — nothing is expensive about it); BUG-34 must be fixed **with** it, because importing the intelligence numbers into the legs without the `assumed_iv` flag would move the fabrication from the Risk tab to the Positions tab; BUG-06 needs one line in `_apply_slippage` — round to the instrument's tick grid, not to 0.01.
6. **BUG-14 / BUG-23 / BUG-13 / BUG-36 / BUG-37 — the at-rest cluster, with BUG-38 as its gate.** BUG-14 is now a *writer* bug, not a fee-model bug (in-memory netting is exact, the row drops it), so it plus the missing `statutory_fees`/`structure_id` columns is a small, high-value change. **Do not start it with a migration: the chain is forked (BUG-38, two revisions named `020`, `current` = 019), so re-number first or every fix in this group lands on an ambiguous head.** BUG-36 is the one that changes what you can prove after the session — 213 alerts and 2,184 orders today with zero durable rows behind them, and an API that answers `source: "memory"` at 200 as if that were history. BUG-37 is the cheapest item in the report (test DB isolation + reap `portfolios` on boot) and it removes ₹1,13,00,000 of phantom capital from the table the landing page's "how much are we running" question eventually reads.
7. **BUG-21 / BUG-22** — warmup scans and post-restart CRITICAL staleness: both are "the box works, the signals lie" class. BUG-22 in particular costs on-call trust on any intraday restart.
8. **BUG-08 → BUG-11** — the display/class cleanup (single IST formatter helper; add the missing entry-time + hold columns; honour `instance_id`).
9. **BUG-12 → BUG-18, BUG-24 (+ its ratchet addendum), BUG-25, BUG-29, BUG-30 (buffer half)** — id uniqueness, `exit_reason`/`net_pnl` at rest, the never-tradeable runner state, post-close/pre-settlement session attribution, the second IST clock and the strftime blind spot in the AST ratchet, a log line for ring recentering, a `pause_source` on the runner payload, and persisting the audit trail. **Removed from this item and PARKED (§1.1): BUG-30's split-preview-from-deployment clause, BUG-31's `structure_type` validation and BUG-32's option-playbook-on-equity refusal — 29 Oct's fix queue has 3 fewer items than this report's finding count implies.**

## 7. Sign-off

Paper trading is functionally wired end-to-end — spawn, warmup, WS marks, ring depth, LOM, ledger, SSE, restart restore, bucket isolation, P1 pool discipline all work and refuse correctly with readable messages. It is **not** yet accurate enough to show as a paper-accuracy demo: the option strategies trade on a cadence nobody selected (74/76 and 69/74 entries off-boundary on the hourly and 15-min runners), their ₹100 bracket is smaller than the ₹193 fee the engine itself charges for using it, the landing/`/risk` headline totals are wrong by ₹82.5 L and ₹10 L, "Realized P&L" excludes the ₹39 k of option fees that "Daily P&L" includes, 80.5 % of option fills printed off the ₹0.05 tick, and the one strategy the board shows as a winner earns it by paying no costs at all.

If the 26-Oct stakeholder bar is "paper accuracy", the demo-safe surfaces today are the plumbing ones — live depth, marks, LOM refusals, restart restore, bucket isolation. The P&L surfaces are not. Two additions from the 14:00 sweep, both about what the page *claims* rather than what it shows: the daily-loss/drawdown gauges on the Risk tab are reporting a limit that exists only as a dataclass default (the values in `config/forward_testing.yaml` never reach the running book — BUG-27), and TC-4.6's "prove the breaker" endpoint can only prove it by permanently rewriting that limit and erasing the day's P&L from persisted state (BUG-28). Say in the demo what would actually stop the book: a bucket halt needs the whole paper book at −₹2,50,000 (projected close today: −₹1,92,800, i.e. it would not fire), an individual runner pauses only at −₹1,50,000 on a ₹10 L allocation, and the segment limits the page displays (₹30,000 / ₹50,000) can never trip for paper by design, with `runner_count: 0` behind both cards and the sole segment member being an equity runner (BUG-35) — while the runner rows will show 🟢 even when a strategy is structurally unable to trade (BUG-15). Add from the 14:47 re-read: the Risk tab's Γ/Θ/ν and the stress grid are computed at an IV the book never supplied — `IV unknown for 3 leg(s) — assumed 15%`, reported alongside `missing_greeks: []` (BUG-34) — so quote those numbers as modelled, not measured. And add from the 15:00-15:25 at-rest sweep, because it bounds what this session can be used for afterwards: **nothing about today survives in the database as an audit trail** — `alerts` 0 rows against 213 alerts fired, `orders`/`positions` 0 rows against 2,184 orders, `strategy_signals` 0 rows in 24 h (BUG-36), while `portfolios` holds 39 `active` rows worth ₹2,15,00,000 including 13 that a test fixture created with the name `LIVE-STRANGLE` and 27 orphans nobody reaped (BUG-37). Any post-session reconciliation must come from the log and `portfolio_state.json`, not from SQL — and the migration route to fix that is itself forked (BUG-38). Also from the close itself, one behavioural FAIL rather than a data FAIL: no EOD exit exists for a structure expiring after today, so three monthly legs rode into 29 Oct unmarked and unguarded (BUG-39, §8).

**Scope decision recorded with this report (28 Oct, after the sign-off above):** the playbook layer is **PARKED** — BUG-31, BUG-32, BUG-30's preview/deployment half, and §6 item 9's playbook clause come off the fix queue until the trading loop itself does the one thing this system exists for: take a trade as the strategy defines it and then take whichever of stop-loss or profit arrives first (§1.1 states the gate as BUG-01 + BUG-02 + first-hit exit + BUG-04/BUG-26 fee honesty + BUG-39). That re-sequences §6 in practice: items 1–3 are the work, and 29 Oct's queue is three findings shorter than this report's finding count implies.

No code was modified by this run.

---

## 8. EOD + stress results

**Pre-registered at 14:33 IST, before the close, so the EOD checks cannot be talked into whatever happens.**

### P3-EOD — prediction: the 15:10 IST intraday square-off **cannot** fire today

`_resolve_intraday_squareoff_minutes()` (`options_bridge.py:2628-2645`) returns `None` unless one of three things exists, and on this build none of them does:

| input the resolver reads | state at 14:33 IST |
|---|---|
| `expression["intraday_squareoff_minutes_before"]` | absent on **8/8** option runners — every live expression's key set is exactly `['exit','expression_version','quantity','type']` |
| `expression["intraday_squareoff"]` (truthy ⇒ default 20 m) | absent on 8/8 |
| `OPTIONS_INTRADAY_SQUAREOFF_MINUTES` env | not in `.env`, not in `config/*.yaml` (the runbook's pre-flight step for it does not exist in this build) |

`_maybe_intraday_squareoff()` (`:1408-1417`) returns at the first line when `mins_before is None`, so `exit_reason: "intraday_squareoff"` (`EXIT_INTRADAY_SQUAREOFF`, `:132`) must appear **zero** times in today's log and zero times in `trades`, and the open structure at `QA_EOD_SNAPSHOT_1415.json` (1 structure, `NIFTY26NOV22750PE` long_put, expiry **2026-11-23**) must ride into 29 Oct. Do not confuse the two knobs: `squareoff_minutes_before` (default 30, `:352`) belongs to `ExpiryManager` for **expiry-day** settlement and is also absent from all 8 expressions; it is not the intraday EOD path.

Watched by `tools/out/qa_eod_watch.py` (PID 18500, started 13:30:38, tails `qa_server_20261028_cutover.log` for `SQUAREOFF|squareoff|EXIT_INTRADAY|EOD|15:30 chart|Export charts` until 15:36) → `QA_EOD_SQUAREOFF.txt`.

### Already banked from the 13:24 IST manual export (read 14:55 IST)

`charts/2026-10-28/` holds 4 runner pairs (`PE_instant_buy_NIFTY_1min`, `immediate_strangle_NIFTY_1hour`, `QA14_strangle_NIFTY_1min`, `P3TICK_strangle_NIFTY_15min`; the `·` in runner names is sanitised to `_` by `export_runner_chart`). Two things fall out of the CSVs themselves:

1. **The `ts` column is naive and space-separated** — `2026-10-28 12:46:00,22717.95,-239.85,992181.85` — so nothing in the file states which clock it is. It is bar time (exchange) reaching the CSV through `str(ts or "")` in `_record_watch_point` (`paper_runner.py:3064`), while `watch_export.py` decides its *own* IST elsewhere (BUG-24); the repo's clock policy says a persisted timestamp must state its own clock.
2. **The hourly runner's own chart confirms BUG-01**: `immediate_strangle_NIFTY_1hour.csv` has rows at 13:15, 13:16, 13:17, 13:18, 13:19 — one-minute cadence on a runner declared `1hour` (and a 12-minute hole at 13:03→13:15, i.e. the series only fills while a structure is open).

### Expected, not predicted: 15:30 chart export

The market-close exporter is armed and logged (`market-close exporter armed: fires 15:30 IST daily (charts/)`); manual export at 13:24:30 wrote 4 × `.png` + 4 × `.csv`. Evidence to collect: `charts/2026-10-28/` file count and mtimes ≥ 15:30, plus whether the `watch_export.py` naive-`ts` path (BUG-24) is what lands in those CSVs.

### TC-4.6 — deliberately sequenced last

`POST /api/portfolio/test/breach {"crash_pct":0.25,"tighten_limits":false}` runs **only after** the 15:36 watch file, the final `QA_CAPITAL_DRIFT.jsonl` sample and the post-close open-position snapshot are banked, because `simulate_crash()` → `reset_daily_anchors()` → `_persist_state()` (`portfolio_manager.py:2006-2017`) rewrites today's persisted day-start equity (the same anchor BUG-33 is about), and `apply_markdown` overwrites `self._bars[symbol][-1]` in every runner's real OHLC buffer (`paper_runner.py:2641-2670`) — after 15:30 that is harmless to today's evidence, before it would destroy it.

**Results (appended 16:47 IST, after the close).** Every item below was measured after the pre-registered predictions were written, and the numbers are in `tools/out/QA_EOD_RESULTS.json` + `tools/out/QA_TC46_RESULT.json`.

1. **P3-EOD square-off: prediction confirmed, zero square-offs.** `grep -ci squareoff` over the full-day cutover log = **0** occurrences, and `tools/out/QA_EOD_SQUAREOFF.txt` contains only its own start (`13:30:38`) and stop (`15:36:03`) lines — the watcher saw no exit attributed to a square-off all session. The 15:10 intraday square-off did not fire, exactly as predicted from the three absent resolver inputs.
2. **The monthly structures rode into 29 Oct.** At 15:37:40 the post-close snapshot shows **3 open structures, all `expiry: 2026-11-23`** — `NIFTY long_put 22750` (+₹1,456.65, `bars_held 146`), `BANKNIFTY long_call 54300` (+₹506.40, 65), `BANKNIFTY long_put 54200` (+₹58.20, 15) — with `mark_ts` frozen at 15:37:41 IST. Bucket `open_positions_total: 8` (3 option legs + 5 equity). **Runbook verdict: FAIL** — there is no EOD exit for a structure whose expiry is not today, so "ride to settlement" carries positions across an overnight gap that nothing in this build marks, re-risks, or squares off. BUG-18 already booked one such overnight settlement into the next session's P&L (+₹10,627, hold 1,275 min), so this is not hypothetical.
3. **Chart export ran on schedule.** 6 option runners × csv+png under `charts/2026-10-28/`, mtimes **15:30:59 → 15:31:09**, matching `MARKET_CLOSE_HHMM = (15, 30)` and the `source == "mstock" and instrument.type == "option"` filter — no equity runner exported, as designed.
4. **The exported clock is naive.** `ts` in those CSVs is space-separated with no offset (`2026-10-28 14:25:00` … `2026-10-28 15:25:00`) — IST by convention, not self-describing. Row spacing matches each runner's timeframe (5-minute rows on the 5-min runner), and the last row is 15:25, i.e. the file stops at the last bar before the close. Recorded against AGENTS.md's "a value must state its own clock": the watch file is the one artefact at rest that still does not.
5. **BUG-03 reproduced at every one of 21 drift samples** (13:14 → 15:29 IST; sampler stopped when its process ended). Last sample: `total_capital 1,84,50,000.00` vs `bucket_capital_sum = runner_capital_sum = 1,02,00,000.00` (Δ **82,50,000.00**) and `total_equity 1,09,45,315.47` vs `bucket_equity_sum 99,45,315.47` (Δ exactly **10,00,000.00**). The two phantoms never moved all session.
6. **The day closed with four mutually inconsistent P&L statements in one pair of payloads** (paper bucket + `equity/snapshot` session, 15:37:40): `allocated_capital 1,02,00,000.00`, `equity 99,40,039.99` (⇒ −₹2,59,960.01 against allocation), `net_today −₹1,95,503.50`, `realized_total −₹1,39,719.08`, open structures +₹2,021.25 (⇒ realized+open −₹1,37,697.83), `day_start_equity 61,49,577.50`, `drawdown_pct 0.0202`, `win_rate 0.1946` (209/1,074). None of the derived figures reconciles to another — BUG-04 and BUG-33, now measured at the close rather than mid-session.
7. **TC-4.6 — the stress endpoint, run at 16:41:12 IST after all of the above was banked.** `POST /api/portfolio/test/breach {"crash_pct": 0.25, "tighten_limits": false}` → HTTP 200:
   - Injected loss **₹1,23,280.90**: `daily_pnl −₹2,65,872.72 → −₹3,89,153.62`, `daily_loss_used → 3,89,153.62`, `daily_loss_pct 1.0635 → 1.5566`, bucket equity `₹98,69,670.77 → ₹97,46,389.87`. That amount is 0.25 % of an implied **₹4,93,12,360** notional — **48 % of the entire ₹1.02 crore paper book** — with no market data behind it, at 16:41, i.e. 71 minutes after the exchange closed.
   - `realized_pnl` (−₹1,74,992.63), `peak_equity` (₹10,14,44,88.44) and `deployed_capital` (₹5,68,214.55) did **not** move: the crash lives only in the loss counter.
   - **The breaker did not react.** At 156 % of the ₹2,50,000 global daily-loss limit, `halt_state` stayed `NORMAL` and `halted: false` on both the summary and the paper bucket. So this endpoint proves the counter can be written, not that the protection fires — which is BUG-28's claim with its mechanism now visible: `RiskSupervisor` evaluates on ticks, and after the close there are none.
   - `global_config` is byte-identical before/after, so `tighten_limits: false` was honoured — the one part of the contract that worked. I did **not** run `tighten_limits: true`, deliberately: it rewrites live limits.
   - **It persisted.** `src/data/portfolio_state.json` has mtime 16:41 — the same second as the POST — and its `.manager.day_start_equity` is now **₹98,69,670.77**, the *pre-breach* equity, alongside `peak_equity 10,14,44,88.44`. The stress call re-baselined the anchor and wrote it to disk, so the next session is measured against a crash the market never delivered. It also incidentally overwrote BUG-33's stale ₹61.5 L anchor with a plausible number — exactly the kind of side effect that makes this endpoint unsafe as a demo tool rather than merely untidy. No restart was performed to test whether the re-baselined day survives restore.
