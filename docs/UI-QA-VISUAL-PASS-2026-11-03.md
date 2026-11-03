# UI visual pass — 2026-11-03 (QA mirror, populated ledger)

**Environment:** worktree `C:\learning\bt-qa-mirror`, branch `qa/environment` (`5f5cdf8` + `62a4a44`),
serving `http://127.0.0.1:5001`, DB `forward_test_qa` / role `ft_qa`, seeded with 5 equity symbols'
bars and a ledger cloned to exact parity with live (trades ≈3k, positions ≈1k, orders ≈2k, portfolios 66).
Market state: NSE closed, all three brokers unauthenticated, 6 runners restored PAUSED, "Simulated fills" banner.
**Method:** headless Chromium (Playwright 1.64 / chromium-1248), real viewports 1440×900 and 390×844,
dark + light (`data-theme="light"`), all 14 pages, read-only (navigation + computed-style probes only;
no clicks, no POSTs, no broker authentication).
**Evidence (outside Git, do not commit):** `C:\learning\qa-mirror-scratch\ui\` —
`shots/*_1440_dark.png`, `shots/*_390_dark.png`, `shots/*_1440_light.png`,
`report_1440_dark.json`, `report_390_dark.json`, `report_light.json`, `probe_out.json`,
drivers `pw_pass.js`, `pw_light.js`, `pw_probe.js`.

Money amounts are deliberately omitted from this document per §2 of
[UI-PENDING-TASKS.md](UI-PENDING-TASKS.md); read them from the local evidence files instead.

## Clean results (no action)

- **Zero JavaScript errors, zero failed requests, zero HTTP ≥ 400** across all 14 pages in both themes.
  The 2026-10-02 walkthrough's `/reporting` 500 did not reproduce at page load.
- No duplicate element IDs on any page.
- No broken images; fonts load locally with `font-display: swap`.
- Chart canvases reported as `0×0` are inside `[hidden]` panes (inactive tabs / awaiting-a-run empty
  states), not broken layout. Retracted.

---

## Findings, in the pending-tasks issue-log format

### F1 — the whole document scrolls horizontally at 390px on every page → §5 rule + UI-01/UI-17

```
Issue ID / task ID: UI-01 (global chrome), also violates §5 "the whole document should not scroll horizontally"
Priority: P1 (the app is unreadable on a phone; no desktop trading workflow is blocked)
Environment: observed on QA mirror, all 14 pages
Page, theme, viewport: any route, dark + light, 390×844
Expected: documentElement.scrollWidth === clientWidth; tables may scroll internally
Actual: scrollWidth 626–651 vs viewport 390 on 14/14 pages
Root cause: .app-topbar (workspace.css:39) is a non-wrapping flex row whose right cluster
  (#halt-pill, the search trigger, the data + market + freshness chips, #broker-status 176px,
  #theme-toggle) has a min-content width of ~600px. The ≤560px block (workspace.css:601-619)
  hides only .command-open and shrinks gaps; it never removes or wraps the chips or the broker strip.
  Offenders measured unclipped by any scroll ancestor: #halt-pill right=412, .broker-strip right=567,
  .broker-strip-item ×3, #broker-status right=593, #theme-toggle right=627.
Evidence: shots/<page>_390_dark.png (14 files)
Classification: UI
Files: src/backtest/web/static/css/workspace.css:39, :601-619; templates/base.html (topbar markup)
```

**Fix shape:** at ≤560px collapse the topbar to two rows (`flex-wrap: wrap` on `.app-topbar`,
`order` the search out), move `.broker-strip` into the existing broker popover only, and let the
market/freshness chips truncate with `min-width: 0` + `text-overflow: ellipsis`.
**Acceptance:** the existing probe (`pw_pass.js 390 844`) reports `ow: 0` for all 14 routes.

Status: DONE — `ow: 0` on 14/14 routes (was 626–651), desktop 1440 pass unchanged (0→0, clip 0→0).
What actually shipped, and where it differs from the sketch:
- The bar wraps to two rows (location, then the control cluster at `flex-basis: 100%`) and is
  **static** at ≤560px instead of sticky: a 91px wrapped bar pinned above `.risk-strip` would have
  covered it, because `.risk-strip` sticks at `top: var(--topbar-height)` (58px). The survival layer
  is the thing that should stay on screen on a phone, so `--topbar-height` no longer reserves space
  there and `.risk-strip { top: 0 }`.
- `.broker-strip-item::after` (the per-broker names) is hidden; the dots stay and the Broker Board
  the same button opens carries the names — no information is lost, ~150px is.
- The chips truncate with `min-width: 0` + ellipsis, and the shrink is **prioritised**: the market
  chip is `flex-shrink: 0` (capped at 160px) so session state outranks staleness; the freshness chip
  absorbs the squeeze. Nothing is hidden by best-case framing — the stale count still leads the label.
- Two non-topbar offenders surfaced once /reporting actually rendered (F2 was masking them): the
  server-built report tables. Fixed with one rule — `.reporting-page > div[id^="rp-"] { overflow-x: auto }`
  — so every report block scrolls internally rather than widening the document.
Files: src/backtest/web/static/css/workspace.css:39, :465, :601-620 (≤560px block)

### F2 — `/reporting` consolidated P&L does not return; UI shows only "Loading…" → UI-16

```
Issue ID / task ID: UI-16
Priority: P0 (the page is unusable; on a ledger this size it holds a server worker for minutes)
Environment: QA mirror (2,951 records / 2,022 selected); reproduced live on :5000
Actual: 271,295 ms and 360,937 ms for one consolidated report; >60 s live. Payload ≈1.8 MB because
  include_trades defaults to True (api/reporting.py:128).
Root cause (measured 2026-11-03): TWO sites, and the first profile found only the cheap one.
  (a) consolidator.py:430 `excluded = [t for t in all_records if t not in selected]` — `in` over a
      list of dataclasses compares ~20 Decimal fields per pair. On the same 3,000-record fixture:
      3,999,000 __eq__ calls and 4.28s standalone vs 0 calls and 0.14s with an identity set.
  (b) sources.py:250 `_broker_from_runner_name(self._manager, …)` called **per DB row**, and the
      helper ran `manager.list_instances()` — which snapshots every runner with `get_state()` under
      the manager lock. Standalone (manager=None) skips it entirely, which is why the standalone
      profile could never reproduce the hang. On the mirror: 1,645 rows × 16 runners = 84.4s; the
      full FY window (2,951 rows) = 157.8s. The two timings scale with row count, not period width.
UI symptom: reporting.js:382-399 sets message("Loading…") and never distinguishes slow from stuck;
  five report blocks stay empty meanwhile.
Classification: backend-performance + UI feedback
Status: FIXED in this worktree, both sites.
  (a) identity set at consolidator.py:430-431, guarded by
      tests/test_reporting_core.py::test_the_excluded_record_scan_never_compares_trade_values
      (asserts zero __eq__ calls, so it cannot pass on the old code).
  (b) the runner→broker labels are resolved once per fetch by `_runner_broker_map()` (sources.py),
      and `_broker_from_runner_name()` is now a dict lookup over that map. Guarded by
      tests/test_reporting_sources.py::test_the_runner_lookup_happens_once_per_fetch_not_once_per_row
      (counts `list_instances()` calls; cannot pass on the old code).
  Measured on the wire after a mirror restart: 157,835 ms → **1,173 ms cold / 845 ms warm** for the
  same FY report (2,710 trades, identical payload). Reporting suites green: 64 passed
  (test_reporting_sources + test_reporting_core), full UI batch 134 passed.
  Not yet committed.
Decision recorded: include_trades stays default True. The 1.8 MB payload is the per-trade ledger the
  page renders; making it opt-out would need a UI toggle, which is a product decision, not a fix.
Files: src/backtest/reporting/consolidator.py:430-431; src/backtest/reporting/sources.py:145-165,
  236-252, 557-594; src/backtest/api/reporting.py:117-131;
  src/backtest/web/static/js/reporting.js:382-399
```

**Remaining work in this slice:** none — the elapsed-time loading state landed with (a).
**Acceptance:** met — report completes in 1.2 s end-to-end on the QA ledger after the mirror restart
(was 157.8 s), and the two regression tests fail on the pre-fix code.

### F3 — "Feed active · 0 running · N ticks" while no broker is connected → UI-01

```
Issue ID / task ID: UI-01
Priority: P0 (a false "active" claim about the feed is exactly the "unknown never looks connected" rule)
Environment: QA mirror and live :5000, identical digits
Actual: risk strip reads "Feed active · 0 running · 6145 ticks" with capability.broker_connected=false
  and all three brokers unauthenticated, market closed.
Root cause: static/js/components/risk_strip.js:113 keys liveness off p.tick, which is the persisted
  tick_index (forward/portfolio_manager.py:136-137, saved :2585-2586, restored :2797-2798,
  exposed :3484-3485) — a durable counter, not feed liveness.
Classification: UI (display of an existing truthful field)
Status: FIXED in this worktree — liveness now comes from `capability.broker_connected` (server truth,
  already on the payload: portfolio_manager.py:3368-3371 → /api/portfolio/summary), the counter is
  labelled "ticks this session", and a missing capability block fails closed to "No broker feed".
  Market-closed + connected reads "Feed paused (market closed)".
  Guarded by tests/js/test_risk_strip_feed_state.mjs (5 cases, renders the real component in a VM).
  Verified on the wire: the mirror's live summary returns `broker_connected: false, tick: 6145,
  running: 0`, and the strip now reads "No broker feed · 0 running · 6145 ticks this session".
Files: src/backtest/web/static/js/components/risk_strip.js:110-133
```

**Fix shape:** gate the phrase on a liveness field the payload already carries (last-tick age and/or
`capability.broker_connected`), and label the counter as a session total, e.g.
"Feed idle · 0 running · 6,145 ticks this session".
**Acceptance:** `tests/js/test_risk_strip*.mjs` asserts idle when the feed is stale/disconnected;
verified on both :5001 (idle) and a market-open observation (active).

### F4 — Backtest rail: results content is laid out in the 308px column, leaving 822px empty → UI-10

```
Issue ID / task ID: UI-10
Priority: P1 (the flagship research page reads as broken)
Environment: QA mirror, dark + light, 1440×900
Actual: .backtest-grid measures 308px + 822px (workspace.css:262). The right column holds only the
  About card (height ≈101px) while the left column is ≈1,733px tall: the "Performance workspace"
  card, the equity empty-state and the "A more deliberate research process" guide all auto-place
  into column 1. Measured: .research-canvas 308×693 (overflow:hidden), .research-steps grid columns
  70px/70px/70px, .preview-metrics 4 columns ≈55px each.
  Consequence: one-word-per-line wrapping ("Define / the / idea"), KPI labels wrapping to three lines,
  and the equity chart empty state squeezed into a 308px gutter.
Root cause: workspace.css:307-308 assigns `grid-column: 2` to `.strategy-card` and `.results-panel`
  but not to `.research-canvas` / `.research-guide`, which are also results-column content.
Classification: UI
Files: src/backtest/web/static/css/workspace.css:262, :296, :307-308; templates/backtest.html:16, :157
```

**Fix shape:** place `.research-canvas` and `.research-guide` in column 2 (or restructure the rail so
only the config panel is in column 1). Re-check the ≤1190px and ≤900px breakpoints afterwards.
**Acceptance:** at 1440px, `.research-steps` columns ≥200px, `.preview-metrics` values render on one
line, and no left-column card is narrower than its content requires; screenshot diff both themes.

Status: DONE. Both cards are inside `#emptyState`, so the placement belongs on that section, not on
the two inner cards: `.backtest-grid > .backtest-empty { grid-column: 2 }` (workspace.css:309).
Measured at 1440×900 and 1440×1080 — identical: `.backtest-empty` 308×… → **822×820** (right=1408),
`.research-step` widths **241px** each (was 70px), all four `.preview-metric-value` rows **1 line**.
`shots/backtest_empty_s4.png` shows the Performance workspace and the research guide filling the
results column. The ≤780px single-column case gets `.backtest-grid > * { grid-column: auto }`, because
a forced column 2 in a one-column template would spawn an implicit second column.

### F5 — the run-configuration form is half-hidden behind an unaffordanced inner scroll → UI-10

```
Issue ID / task ID: UI-10
Priority: P1
Actual: .backtest-config (workspace.css:263) is sticky with max-height calc(100dvh - 280px) and
  overflow-y:auto; measured clientHeight 618 vs scrollHeight 1236 — i.e. the timeframe, date range,
  capital and the whole Strategy Params fieldset are below an inner fold. `.config-actions` is sticky
  at the bottom, so the first screen ends at "Timeframe" + a big Run button, which reads as a complete
  form. scrollbar-width: thin is the only affordance.
Classification: UI
Files: src/backtest/web/static/css/workspace.css:263, :268
```

**Fix shape:** once F4 frees the right column, let the config rail grow with content (drop the
`max-height` in favour of a taller viewport-aware cap, or move the date/capital/params sections into
the results column as a second step). If an inner scroll must stay, add a visible gradient/edge cue.
**Acceptance:** every required field is reachable without discovering a hidden scroller, at 900px and
1080px viewport heights, both themes.

Status: DONE — `.backtest-config` lost `max-height: calc(100dvh - 280px)` and `overflow-y: auto`
(workspace.css:263); the rail is now as tall as its content and the page scroll reaches it. Measured
at 1440×900 and 1440×1080: `clientHeight 1236 / scrollHeight 1236` → **no inner scroller** (was
618/1236), deepest field bottom 1595px, rail bottom 1759px, document 2692px — every field, the
Strategy Params fieldset and "Fast preview" included, is on the page flow.
`.config-actions` stopped being `position: sticky; bottom: -20px`: inside a non-scrolling rail that
floats the Run button over a half-read form, which is the same misreading F5 reported. It now sits at
the end of the form. The ≤780px overrides for both rules became dead and were removed rather than
left as no-ops.

### F6 — `/diary` truncates money values and hides table columns → UI-03, UI-02

```
Issue ID / task ID: UI-03 (financial values), UI-02 (table fit)
Priority: P1
Actual:
  (a) The "EXTREMES & STREAK" KPI renders "+<best> / −<worst…">" with the second number cut off by an
      ellipsis. Cause: .diary-kpi-value sets white-space:nowrap; overflow:hidden; text-overflow:ellipsis
      (static/css/diary.css:212-221) on an 18px mono value; measured 15px of loss.
  (b) .diary-tree-table measures 1202px inside a 1122px overflow-x:auto wrapper: the last column(s)
      (R-multiple) sit off-screen with no scroll cue, and the header row is visibly cut mid-glyph.
Classification: UI
Files: src/backtest/web/static/css/diary.css:212-221; templates/diary.html:127 (#dkpi-extremes)
```

**Fix shape:** never ellipsize a number — let the extremes KPI wrap to two lines or drop to a
`font-size` step-down; give the KPI grid `min-width: 0` and let the pair stack. For the table, add a
column-priority collapse (hide the least important column first) or a persistent scroll shadow.
**Acceptance:** a populated QA ledger shows both extremes in full; the R-multiple column is reachable
with a visible affordance; `tests/test_workspace_design.py` + a CSS assertion that `.diary-kpi-value`
has no `text-overflow: ellipsis`.

**Status: DONE (S6, 2026-11-03).**
- (a) `.diary-kpi-value` (diary.css:212) lost `nowrap + overflow:hidden + text-overflow:ellipsis`;
  it now wraps (`white-space: normal; overflow-wrap: anywhere`). Measured on the populated ledger:
  `+₹17,630.76 / −₹62,567.76` was losing **15px at 1440** → **0px lost at 1440 / 1024 / 768 / 390**,
  two lines at 1440, one line below.
- (b) Two parts. The unbreakable identifier in the name column was what pushed the tree table to
  1,202px, so the name column now wraps with a **200px floor** (without the floor it collapsed to
  ~40px at 1024 and read as a vertical ribbon — caught on screenshot, not in the first measurement)
  and the tree table's cell padding went 10px → 7px. **1440: 1122px content in a 1122px box, all nine
  columns on screen, nothing hidden.** Below that the table still overflows, and it now says so:
  `markScrollCues()` in diary.js inserts a `.diary-scroll-cue` line above the wrap naming the columns
  still out of view, and gives the wrap `tabindex=0` + `role=region` so ← → reaches them. The cue
  clears itself when scrolled to the end, skips tables in hidden tabs, and is reused across renders.
  Measured: 1024 → "Commission, Net P&L, Running MTM out of view…"; 768 → same; 390 → seven columns
  named; 1440 → cue empty, tabindex removed.
- Tests: `tests/test_workspace_design.py::test_diary_never_ellipsizes_a_money_value` and
  `::test_diary_table_columns_stay_reachable` (CSS contracts, the precedent at :174), plus
  `tests/js/test_diary_scroll_cue.mjs` (4 cases: names the hidden columns and makes the box focusable;
  clears at the end of the scroll; never measures a hidden tab; one cue element, not one per render).
  The stub keeps `tabIndex` and the `tabindex` attribute as one thing so the "focus stop goes away"
  assertion cannot pass for the wrong reason.
- Evidence: `shots/f6_kpi_1440.png` (both extremes in full), `shots/f6_cue_1024.png` (cue line above
  the table, labels legible), `shots/f6_diary_{1440,1024,768,390}.png`.
- **New finding while measuring this:** the topbar's broker strip overflows the *document* at
  1024px and 768px on every page (90–135px of horizontal page scroll; offenders
  `.broker-status`, `.broker-strip`, `.theme-toggle`). F1's fix is scoped to ≤560px, so this band was
  never in the matrix — recorded as **F13** below.

### F7 — the alert widget floats over page content → UI-17

```
Issue ID / task ID: UI-17
Priority: P2
Actual: .alert-widget is position:fixed; right:16px; bottom:16px; z-index:150 (static/css/app.css:1227).
  In the 900px viewport it lands on top of whatever is there: it covers the last calendar row on
  /diary, the "0 brokers" chip on /analytics, and sits in dead space on / and /risk. Observed with
  data-state="minimized" and no alerts, i.e. it costs screen space while saying nothing.
Classification: UI
Files: src/backtest/web/static/css/app.css:1227-1228; templates/base.html:200;
  static/js/components/alert_widget.js
```

**Fix shape:** when there are no alerts, render nothing (or dock it into the topbar next to the market
chip); when expanded, use a drawer/panel that reserves layout instead of overlaying.
**Acceptance:** no page element is covered at 1440×900 or 390×844 with zero alerts; with one alert the
widget does not obscure a table, KPI or button.

### F8 — `/analytics` presents loading and zero states as measurements → UI-15

```
Issue ID / task ID: UI-15
Priority: P1 (UI-01's "unknown must not look like zero" applied to analytics)
Actual (12s after load on a mirror whose overview takes 7.1s):
  (a) "Running Strategies" table still says "Loading strategies…" (templates/analytics.html:255) with
      no error path — a slow read is indistinguishable from a dead one.
  (b) KPI tiles show an em-dash value and an em-dash sub-value; "0 Active Strategies" (:44) and
      "0 brokers" (:82) are rendered as facts.
  (c) "Portfolio Equity Curve" is a heading over a ≈200px empty void — the canvas is hidden and there
      is no empty state, unlike Backtest's designed `.empty-chart`.
Classification: UI
Files: src/backtest/web/templates/analytics.html:44, :82, :255; static/js/analytics.js
```

**Fix shape:** skeleton/"fetching" chips while in flight, an explicit "unavailable" state on failure,
and reuse the `.empty-chart` empty state for the equity curve.
**Acceptance:** with the overview deliberately slowed, the page reads "loading", then numbers; with
zero strategies it says "no strategies deployed", not "0".

**Status: DONE (S6 tail, 2026-11-03).**
- Measured first (`pw_f8.js`, scratch, 1440×900; the mirror's overview read takes 2.9s): at 500ms and
  at 2s the pills read "0 Active Strategies" / "0 brokers", the trade sub read "0 trades", the table
  read "Loading strategies…", and the equity heading sat over a 245px void holding a blank 300×150
  canvas with no empty state. With the overview aborted, every one of those strings stayed forever
  and only a top banner said anything. With a zero-card payload the pill read "0 / 0 Active
  Strategies". All three confirmed as facts on the wire before touching code.
- One vocabulary, three states: `data-state="fetching|ready|empty|unavailable"` on every
  placeholder, styled by `.state-chip` / `.state-note` in workspace.css (info-blue with a pulsing dot
  while fetching, danger red when unavailable, grey when genuinely empty). The template ships all ten
  placeholders fetching, so the first paint cannot state a number nobody has measured.
- `analytics.js`: `markOverviewPending()` runs before the fetch; both failure paths call
  `markOverviewUnavailable()`; `renderOverviewMetrics` writes ready/empty ("No strategies deployed"
  when `total_runners` is 0); `renderPortfolioEquityChart` builds a chart only when there is at least
  one finite equity point, otherwise `showEquityPlaceholder()` reuses Backtest's `.empty-chart`
  treatment (sized to the 220px section by a `.chart-section .empty-chart` rule) with a reason per
  state — fetching / unavailable / "no equity points in this period".
- `cross_broker.js`: `markXb()` gives the cross-broker card the same three states, including "No
  brokers in scope" when `broker_count` is 0.
- Found while fixing: `showError()` mounted the banner in `.page-content`, a selector that has never
  existed in this app, so it fell back to `<body>` — measured banner x=0..1440 with the fixed sidebar
  covering 0..224, i.e. the first words of the sentence were exactly the unreadable part. It now
  mounts in `#main-content` (measured x=256, w=1152).
- Two more locale-less numbers on this page, caught by the F9 sweep: the overview trade sub and the
  comparison table's Trades column now go through `Money.count` ("1,800 trades (784W / 842L)").
- Tests: `tests/js/test_analytics_states.mjs` (4 — failed read, in-flight read, empty book, arrived
  read; each asserts the placeholder text AND its data-state) and
  `tests/test_analytics_state_contract.py` (6 — the served HTML ships fetching with no zero-as-fact
  string, the `.empty-chart`/state-chip vocabulary has CSS rules, both JS files walk the three states).
- Wire evidence: `pw_f8.js` in sample/fail/zero × dark/light, screenshots
  `shots/f8_*_{dark,light}_1440.png` plus `*_inflight_*`. Settled sample: pills `ready`, chart drawn,
  placeholder hidden. Fail: every note "unavailable", table row "Could not read the strategy table —
  Failed to fetch.", equity box "Equity curve unavailable … this is not a flat portfolio". Zero:
  "No strategies deployed" + the empty-state card, equity box "No equity points in this period".
- Deviation from the fix shape: no skeleton shimmer — the fetching state is a labelled chip with a
  pulsing dot (the house `.data-freshness-chip` vocabulary), because a grey block would hide *which*
  number is still missing.
- Gate: `pytest tests/test_analytics_state_contract.py tests/test_number_formatting.py
  tests/test_workspace_design.py tests/test_reporting_core.py tests/test_reporting_sources.py
  tests/test_timekeeping_policy.py` → 128 passed; every `tests/js/test_*.mjs` passes except the two
  documented non-tests/pre-existing entries (`render_option_views.mjs` is a CLI tool needing args;
  `test_mtm_staleness.mjs` fails at HEAD on portfolio.js, untouched by this slice).

### F9 — two money-grouping systems on one page → UI-03

```
Issue ID / task ID: UI-03
Priority: P1
Actual: /portfolio shows the same equity as "<lakh-grouped>" in the PAPER bucket card
  (.bucket-equity, templates/portfolio.html) and "<western-grouped>" in the Paper Sandbox summary
  table; allocated capital likewise. The risk strip uses lakh grouping. Both are on screen at once.
Root cause: mixed formatters — 13 Jinja sites use "{:,.0f}" (e.g. templates/portfolio.html:38, :40,
  :41, :42, :69) which is locale-less → western grouping, while JS uses toLocaleString("en-IN").
  31 JS sites call a bare toLocaleString() and inherit the browser locale.
Classification: UI
Files: templates/portfolio.html:38-69 and the other Jinja "{:,…}" sites; the bare toLocaleString()
  call sites across static/js/
```

**Fix shape:** one house formatter. Add a Jinja filter (`inr(...)`) backed by the same lakh-grouping
rule as the JS helper, convert the 13 template sites and the 31 bare JS calls, and forbid the pattern
with a test.
**Acceptance:** `tests/test_workspace_design.py` (or a new test) greps the web templates and JS for
`"{:,` / `toLocaleString()` with no locale and fails on any hit; screenshots of /portfolio, /diary,
/risk show one grouping system.

**Status: DONE (S5, 2026-11-03).**
- Server half: `src/backtest/web/formatting.py` (new) + two Jinja filters registered in `create_app`.
  **Deviation from the fix shape:** the filters are `money` and `count`, not `inr` — the currency is
  configurable (`--currency` / `BACKTEST_CURRENCY`), so naming a filter after one rupee would lie.
  `money` reads the same `CURRENCY_SYMBOL`/`CURRENCY_LOCALE` config the `<body data-currency-*>`
  attributes come from, so a non-IN deployment changes both halves at once.
- All 16 locale-less format calls in the templates converted (13 lines: `portfolio.html` 8,
  `portfolio_live.html` 4, `portfolio_paper.html` 4).
- Client half: `Money.num()` and `Money.count()` added to `components/currency.js`; 35 locale-less JS
  calls routed through them (analytics 11, optimize_run 8, data_manager 5, optimize_setup 4,
  optimize_common 3, symbol_picker 1, portfolio_intelligence 1 date, plus the two `$`-hardcoded
  fallbacks in metrics_cards/trade_table now name `en-IN`).
- Ratchet: `tests/test_number_formatting.py` — 2 regex sweeps (templates for `"{:,`, JS for
  `.toLocaleString()`/`(undefined`) with a **frozen empty allowlist**, plus unit tests for the
  grouping itself. `tests/js/test_currency_count.mjs` pins the JS half to the same rule.
- Proof the ratchet bites: at `HEAD` the same regexes match 13 template lines and 35 JS calls; on
  this slice's tree they match 0 and 0.
- Wire evidence: `node pw_money.js` (scratch) walks every text node on 10 pages and compares each
  `₹` figure against `Intl.NumberFormat("en-IN")` — **817 figures checked, 0 wrong** across
  /portfolio, /portfolio/paper, /portfolio/live, /diary, /risk, /analytics, /reporting, /forward, /,
  /settings. Screenshots: `shots/money_*_{dark,light}.png` (the paper command center reads
  ₹22,00,000 in the card and ₹10,00,000 in the server-rendered Alloc column, while the risk strip
  beside it reads ₹4,683.28 / ₹2,50,000.00 — one system, both halves).
- Side effect worth knowing: `tests/js/test_data_manager_labels.mjs`, `test_symbol_picker.mjs`,
  `test_deflated_sharpe.mjs` and `test_attestation_box.mjs` had to be taught about `Money` — the
  first two now load `currency.js` into their VM (the pattern `test_metrics_cards.mjs` already used),
  the last two stub `OptCommon` by hand and gained `fmtCount`. The components themselves stay
  unguarded because `base.html` loads `currency.js` before every script.
- Gate: the slice regression command above → 9 failures, exactly the documented known-bad set
  (8 live-arming + 1 stale count), 96 passed.

### F10 — strategy descriptions render merged words → UI-10, UI-11, UI-12

```
Issue ID / task ID: UI-10 (About panel), also /compare and /optimize
Priority: P2 (copy defect, but it is on the flagship card and reads as a broken build)
Actual: "…below the lower Bollinger band on theassumption the stretch snaps back toward the mean."
Root cause: ~80 implicit string-concatenation literals across 12 strategy modules are missing their
  trailing space, e.g. src/backtest/strategies/bollinger_reversion.py:29-34 ('… band on the' followed
  by 'assumption …'). The backend string is the defect; the UI only displays it.
Classification: backend-data (copy) surfaced in UI
Files: src/backtest/strategies/*.py (12 files, ~80 sites)
```

**Fix shape:** sweep the multi-line description literals for a missing trailing space (or join with
explicit `" "`). Add a test that no strategy `description` contains a lowercase-lowercase run without
a space.
**Acceptance:** `/backtest`, `/compare`, `/optimize` About panels render clean prose; the new test
passes for every registered strategy.

**Status: DONE (S7 head, 2026-11-03).**
- The finding's count was low. Tokenizing every strategy module for *adjacent STRING tokens* (skipping
  NL/COMMENT/INDENT/DEDENT, so only true implicit concatenations pair) found **188 fused joins across
  28 modules**: 77 in the 12 built-ins under `src/backtest/strategies/`, and 111 more in the 16-file
  plugin pack under `plugins/strategies/`, which the finding never looked at because those strategies
  register through `discover_plugins()` at app start rather than through `strategy.registry.get_all()`
  in a bare interpreter. Both dirs are swept; `docs/archive/strategy-snapshot-2026-10-15/` is an
  archive and stays as it is.
- The sweep is a script, not a hand edit: for each offending pair it inserts one space before the
  closing quote of the first literal, asserting the character at that column really is a quote. Diffs
  are exactly 77/77 and 111/111 lines — one space per line, nothing else moved. Rescan of both dirs: 0.
- Ratchet: `tests/test_strategy_prose.py` (3 tests). The source half tokenizes both strategy dirs and
  fails on any adjacent-literal pair whose join fuses a lowercase word into the next one — the defect
  at the place it is born, since Python folds the literals at compile time and the fused word never
  appears in the source text. Red-green proven against `git show HEAD:` blobs (bollinger_reversion 7,
  supertrend_equity 8, swing_hourly_supertrend 7 joins). Two details that cost a pass each: the regex
  must end `\Z` not `$` (`$` also matches before a trailing newline, which produced phantom hits in
  `db/config.py` and `pine/codegen.py`), and quote detection must strip `r/b/f/u` prefixes before
  testing for a triple quote (a literal containing `"""` inside single quotes fooled it).
- The runtime half calls `discover_plugins()` first, so the artifact checks (no double space, no space
  before punctuation, no padding) cover all 196 registered prose strings — 84 built-in, 112 plugin —
  and the test fails loudly if the plugin pack ever stops registering.
- Wire evidence: `pw_f10.js` (scratch) iterates all 17 strategies in the `/backtest` select, expands
  `#strategyCardSections`, and reads `#strategyCard`; it also reads `/compare`, `/optimize` and the
  61 KB `/api/strategies` payload. Its word list is not hand-sampled — it is all **149 distinct fused
  tokens** derived from the HEAD blobs (tail word + head word of every offending join), none of which
  is a real English word, so a hit is always the defect. Result: `fused: []` on all four surfaces,
  with the corrected phrases present ("band on the assumption", "trend flag is up", "long only above
  the regime"). Screenshots `shots/f10_{bollinger_reversion,supertrend_equity}_1440.png`.
- Deviation from the fix shape: no explicit `" "` joins were introduced — the trailing space stays
  inside the first literal, so the diff is one character per site instead of a rewrap of 188 lines.
- Gate: the documented slice regression command plus the two prose/description suites → **9 failed,
  107 passed**, the 9 being exactly the known-bad baseline (8 live-arming + 1 stale harness count).
  All 44 `tests/js/test_*.mjs` pass except the pre-existing `test_mtm_staleness.mjs`.
  `tests/test_phase2_rewrites.py` (62) confirms nothing asserts on the pre-fix description text.

### F11 — raw identifiers leak into labels → UI-02, UI-14, UI-06

```
Issue ID / task ID: UI-02 (run cards), UI-14 (Engine playground), UI-06 (risk rulebook)
Priority: P2
Actual: strategy keys and segment codes are shown verbatim where a human label exists:
  "sma_crossover" / "bollinger_reversion" / "rsi_reversion" / "mysupertrendv1" on the Recent Runs
  cards; "Linked segment: equity_intraday" on /backtest; "CE_instant_buy" and "options_index" on
  /forward; "bear_put_spread, bull_call_spread, long_call, …" and "Source: config/risk_limits.yaml"
  on /risk; "strangle_wing_points", "hf_equity_scalp", "banknifty_ema_momentum" as row labels on /diary.
Classification: UI
Files: static/js/backtest.js (recent-run cards), templates/forward.html, templates/risk.html,
  static/js/diary.js — all should go through the existing display-name/label maps
```

**Fix shape:** one label resolver (strategy registry already carries display names — the strategy
dropdown on /backtest shows "Bollinger reversion") used by every surface that currently prints the key;
segment codes mapped to words; keep the raw key available as a tooltip/`title` for operators.
**Acceptance:** no snake_case token in visible page text on the 14 routes (the content probe in
`pw_pass.js` already reports these; add it as an assertion).

**Status: DONE (S7, 2026-11-03).**

- One resolver, two halves — the F9 shape again. `static/js/components/labels.js` (`Labels.humanise /
  .strategy / .segment / .structure / .structureShort / .warm`) resolves for the browser;
  `format_label()` in `web/formatting.py`, registered as the `label` Jinja filter, resolves for the two
  server-rendered portfolio tables. Both carry the same ACRONYMS/PROPER vocabulary, and
  `tests/test_label_resolver.py` fails if they drift — a template and a page script cannot print the
  same strategy two ways.
- Canonical vocabularies reused, not reinvented: `Labels.structure` reads `OptionConfig.STRUCTURES`
  (which mirrors `options_bridge.py:200`), `Labels.segment` reads `/api/segments`' `display_name`
  (cached by `warm()`) with `humanise()` only as the pre-warm fallback. `structureShort()` is the
  compact variant — the label minus its parenthetical gloss — because "Direction-aware (bull → call
  spread, bear → put spread)" is right in a picker and unreadable in a table row. It replaced a second
  resolver that had grown inside `components/option_view.js`.
- Deliberately **not** resolved, because the identifier *is* the name: runner and pool names
  (`QA13_sma_TCS_15min`, `NIFTY_50`, `ema_pullback·NIFTY50·15min`), broker-profile ids and
  commission-model codes on /settings (`td_ameritrade`, `india_tiered`, `per_share`), and file paths.
  Rewriting a runner name would make two different runners read the same. Every labelled site keeps
  the raw key in a `title`.
- Deviation from the acceptance wording: the assertion is **not** a blanket "no snake_case token". That
  rule would flag operator-chosen runner names, which must survive. The ratchet (`pw_f11.js`) instead
  derives its governed vocabulary from the server itself — 37 keys from `/api/strategies`,
  `/api/segments` and `/api/portfolio/risk/limits` — and only calls a token a defect when it is one of
  those **and** it stands alone in its text node. A key embedded in a longer identifier is an
  operator's composite name and is allowed by construction, so that class can never be flagged by
  accident. /settings is exempted wholesale with the reason written into the report.
- **The coverage hole that mattered.** The first ratchet reported `defects: {}` across the 14 routes
  while 24 raw keys were on screen. Tab panels are `display:none` until clicked, so the visibility
  test skipped every one of them — the Playbooks tab was printing `bull_call_spread` the whole time.
  The ratchet now walks `button.tab[data-tab]` on each route: **48 views** (14 routes + 34 tabs), which
  found the 24. Anything in a modal, drawer or widget that no fixture opens is still invisible to it —
  see the static ratchet below.
- The 24 fell into three shapes:
  - **A labelled table cell** (4 hits) — `components/portfolio_intelligence.js` printed `r.strategy`
    and `f.strategy` raw in the greeks breakdown and the VIX regime-fit table, both inside the
    portfolio's Risk & intelligence tab. Now `Labels.strategy(...)` with the key in a `title`.
  - **A transcript** (20 hits) — the activity log (`#audit-log`) and the /risk audit timeline
    (`#audit-tbody`) print stored audit records. These are exempt, but not by a route rule: both
    containers now carry `data-raw-key`, the marker the scanner already honoured, with the reason in a
    comment beside it. Narrow by construction and visible to whoever edits the container.
  - **A raw JSON blob** — the audit timeline's Details column read `e.reason || e.details`, but the
    payload key is `detail` (singular, present on every record), so every row fell through to
    `JSON.stringify(e).slice(0,120)` and the column showed `{"action":"SPAWN QA13_…` instead of
    `strategy=ema_pullback capital=500000.0`. Field name fixed; the JSON dump is now a `title` on an
    em-dash rather than cell text, so nothing is lost but nothing is printed unreadable.
- **Three more the wire ratchet cannot see at all** — found by the static half, and the reason it
  exists. `components/alert_widget.js` (4 sites: the alert attribution table and both subscription
  lists), `components/risk_board.js:290` (closed-trades Strategy column) and
  `components/orders_tab.js:221` (the segment sub-line) all rendered governed keys raw. No QA fixture
  opened the alert widget, the risk board's closed-trades table was empty, and no order carried a
  segment — so all three passed a clean wire scan. `tests/test_label_resolver.py` now fails on any
  line under `static/js` that interpolates a governed key into `${…}`, `innerHTML`, `textContent` or
  `esc(…)` in a file that never mentions `Labels`. Line-level on purpose: `tune_this.js` reads
  `cfg.strategy` into a prefill payload and must stay free to do so.
- Red proofs, because a green ratchet that has never been red proves nothing:
  - `f11_selftest.js` drives the **same** exported `SCAN`/`judge`/`fetchGoverned` the ratchet uses (a
    red-proof against a copy of the rule proves nothing) over 6 injected fixtures on /risk:
    governed+standalone → defect, embedded composite name → allowed, unknown token → allowed, inside
    `<code>` → skipped, /settings → exempt. 6/6.
  - Both static ratchets were reverted at one site each and re-run: they failed with
    `risk_board.js:290` and `portfolio_paper.html:25` named, then went green on restore.
- Wire evidence: `pw_f11_shots.js` clicks into the three hidden tabs and reads the cells —
  Playbooks `Bull call spread / Bear put spread` (title `bull_call_spread / bear_put_spread`),
  portfolio Risk tab `EMA pullback` / `HF equity scalp` / `SMA crossover` with raw titles, /risk audit
  Details `strategy=ema_pullback capital=500000.0`. Screenshots
  `shots/f11_{playbooks,portfolio_risk_tab,risk_audit_details}_1440.png`.
- Gate: the documented slice command plus `test_label_resolver.py` (8), `test_number_formatting.py`,
  `test_strategy_prose.py` → **9 failed, 110 passed**, the 9 being exactly the known-bad baseline
  (8 live-arming + the stale `test_orders_tab.mjs` count). JS: **44 passed, 1 failed**, the failure
  being the pre-existing `test_mtm_staleness.mjs` (its 4 unit tests pass; it fails on the portfolio.js
  SSE assertion, unchanged). Five harnesses gained the labels.js dependency
  (`test_option_view`, `render_option_views`, `test_mtm_staleness`, `test_alert_widget`,
  `test_alert_widget_relogin`, `test_orders_tab`); `test_option_view` also had one expectation
  corrected — `structureLabel("exotic_thing")` was asserted as `"exotic thing"`, the old
  underscores-to-spaces guess, and is now `"Exotic thing"`, because a key outside the vocabulary still
  gets the house casing.
- **New finding spun out (F14, below):** `components/risk_page.js` has 22 `innerHTML` assignments and
  zero escaping — no `esc()` helper exists in the file at all. `cur.halt_reason` goes straight into
  the circuit-breaker banner and `e.message` into two error cards. Found while fixing the Details
  column in the same file; left alone because a partial fix in one row of a wholly unescaped file buys
  false confidence. Needs its own pass.

### F12 — small consistency items (batch) → UI-03, UI-06, UI-15

```
Priority: P2
- "1 trades" on Recent Runs cards: static/js/backtest.js:369 renders `${r.total_trades ?? 0} trades`
  while static/js/diary.js:411 already pluralises correctly — reuse that helper.
- Emoji glyphs used as section/tab/button icons on /risk and /analytics (⚙️ 📜 📊 📈 🔴 💾  🏦  🚨)
  while the rest of the app uses the `icon()` SVG macro. The 🟢/ status dots elsewhere are a
  deliberate aria-hidden convention — leave those. Replace only the emoji used as icons.
- "What is held right now, with the manual controls. nothing held" — sentence starts lowercase after a
  full stop (portfolio centre template).
- Commission-as-%-of-gross exceeds 100% and reads like a bug ("/diary" shows e.g. (1494%) when gross is
  tiny): cap the display or switch to "cost > gross" wording.
- #bucket-positions-paper renders an empty numeric input with no placeholder while LIVE shows 5 —
  an unset limit must read "not set", not blank.
- "certification-grade" badge on the QA mirror overstates a 5-symbol cache; the badge should reflect
  the actual certified universe.
```

### F13 — the topbar scrolls the whole document sideways at tablet widths → UI-01, UI-17

```
Issue ID / task ID: UI-01 (chrome), UI-17 (global rule: no horizontal page scroll)
Priority: P1 (same defect class as F1, found while measuring F6 on 2026-11-03)
Actual: horizontal document scroll at 1024px and 768px on every page — 90px on /settings, 122px on
  /risk, 135px on /diary. Offenders: .broker-status, .broker-strip, .broker-strip-item, .theme-toggle.
Root cause: F1's fix is scoped to @media (max-width: 560px). Between 561 and ~1100px the topbar keeps
  its single-row flex with the full broker strip, so the row is wider than the viewport and the page
  scrolls instead of the chrome shrinking.
Classification: UI
Files: static/css/workspace.css (.app-topbar / .broker-strip breakpoint band)
```

**Fix shape:** extend the F1 treatment up to the width where the row actually stops fitting — let the
strip wrap or collapse to dots, keep every broker's state visible (a dot still says connected/not).
**Acceptance:** `document.scrollWidth === innerWidth` at 1024 and 768 on all 14 routes (the pw_pass.js
geometry probe already computes this; add the two widths to its matrix).

### F14 — risk_page.js interpolates server strings into innerHTML with no escaper (found during F11)

```
Issue ID / task ID: none — new, found 2026-11-03 while fixing the audit Details column (F11)
Priority: P2 (single-operator local app, so exploitation needs an operator-controlled string to
  already be in the DB; but a runner name is exactly that, and it is set from a form)
Actual: static/js/components/risk_page.js has 22 innerHTML assignments and zero escaping — the file
  defines no esc()/escapeHtml() helper at all. Server-derived strings go straight into markup:
  `cur.halt_reason` and `cur.halt_mode` into the circuit-breaker banner (:161), `e.message` into two
  error cards (:62, :77), and the audit timeline's action/detail/runner cells (:347-353). A runner
  named `<img src=x onerror=…>` would be stored by the spawn form and replayed into the audit table.
Classification: robustness/security, not visual — nothing looks wrong today, which is why the pixel
  pass did not see it
Files: static/js/components/risk_page.js
```

**Fix shape:** one `esc()` helper in the file (or import the house one) applied at every interpolation
of a server string; the audit row edited for F11 is a start, not a fix.
**Acceptance:** no `innerHTML` assignment in risk_page.js interpolates a value that did not pass
through `esc()` or a numeric formatter — assertable statically the way `test_label_resolver.py`
asserts the label rule. Deliberately **not** folded into F11: escaping one row of a wholly unescaped
file buys false confidence, and the two rules would then fail together for unrelated reasons.

---

## Suggested order of work

| Slice | Contents | Why here |
| --- | --- | --- |
| S1 | F2 (consolidator O(n²) + `include_trades` default + loading UX) — **DONE 2026-11-03** | P0, one-line algorithmic fix, unblocks UI-16 |
| S2 | F3 (truthful feed state) — **DONE 2026-11-03** | P0, one component, covered by an existing JS harness |
| S3 | F1 (topbar at ≤560px) — **DONE 2026-11-03** | One CSS block, fixes 14 pages at once |
| S4 | F4 + F5 (backtest rail + config fold) — **DONE 2026-11-03** | Same two columns, must be designed together |
| S5 | F9 (one money formatter + ratchet test) — **DONE 2026-11-03** | Touches many files; land before other template edits |
| S6 | F6 (diary truncation) **DONE 2026-11-03** + F8 (analytics states) **DONE 2026-11-03** | Per-page, independent |
| S7 | F10 (description spacing sweep) **DONE 2026-11-03** + F11 (label resolver) **DONE 2026-11-03** + F12 | Copy/consistency batch |
| S8 | F13 (topbar at 561–1100px) — found during S6 | Same rule as S3, wrong breakpoint band |
| S9 | F14 (risk_page.js escaping) — found during S7 | One file, no visual change; needs its own static rule so it cannot be half-done |

Regression command for every slice (from [UI-REDESIGN-PLAN.md](UI-REDESIGN-PLAN.md):217-227), run in
this isolated mirror, never against :5000:

```
PYTHONPATH=src python -m pytest tests/test_workspace_design.py tests/test_web_components.py \
  tests/test_broker_ui.py tests/test_web_taxonomy.py tests/test_portfolio_ui_views.py \
  tests/test_reporting_ui.py tests/test_options_spawn_ui.py \
  tests/test_number_formatting.py tests/test_label_resolver.py tests/test_strategy_prose.py -q
for test in tests/js/test_*.mjs; do node "$test" || exit 1; done
```

The last three pytest files are the ratchets this pass added (S5 money, S7 label resolver, S7 prose
spacing); they are in the slice command, not a separate run, so a later slice cannot regress them
without the gate going red.

Known failures on this mirror that are **not** regressions (verified against a clean HEAD worktree,
2026-11-03): 8 in `test_portfolio_ui_views.py` — every one arms a live runner and dies in
`assert_live_arming_allowed()` (segments_store.py:335), which the mirror's own DB state deliberately
trips; 1 in `test_web_components.py` — `test_orders_tab.mjs` prints 23 and the Python wrapper asserts
"21 tests passed" (a stale count, harness itself exits 0); `tests/js/test_mtm_staleness.mjs` fails on
its `portfolio.js` SSE assertion. Its four `option_view.js` unit tests pass, including after F11 made
`option_view.js` depend on `labels.js`.

Static edits (CSS/JS) are served from disk, but **Python and template edits need a mirror restart** —
the reporting fix measured 157.8 s until PID 21136 was replaced, and F11's `data-raw-key` markers only
took effect after one. Launch (see QA-MIRROR.md):

```bash
cd /c/learning/bt-qa-mirror
PYTHONPATH=src "C:/Users/Nirvika/AppData/Local/Programs/Python/Python311/python.exe" \
  -u -m backtest.web.app > server_5001.log 2>&1 &
```

System Python, not `.venv`: only Python311 has `twisted`, and the mirror still loads the mStock feed
module at import. Confirm the boot line reads `serving 183 routes on http://127.0.0.1:5001` before
trusting a probe — a stale listener on :5001 answers with the old build.

Visual re-test harness (re-runs the whole matrix, ~2 min per pass):

```
cd /c/learning/qa-mirror-scratch/ui
node pw_pass.js 1440 900 dark 1440_dark      # geometry + contrast + console, 14 pages
node pw_pass.js 390 844 dark 390_dark        # responsive
node pw_light.js                              # light theme, 8 key pages
node pw_probe.js                              # targeted root-cause probes
node pw_money.js                              # F9: every ₹ figure vs Intl en-IN, 10 pages
node pw_f8.js sample|fail|zero [dark|light]   # F8: analytics fetching/unavailable/empty states
node pw_f10.js                                # F10: all 17 About cards vs 149 HEAD-derived fused tokens
node pw_f11.js                                # F11: 37 server-derived keys vs 48 views (14 routes + 34 tabs)
node f11_selftest.js                          # F11 red-proof: 6 fixtures drive the ratchet's own SCAN/judge
node pw_f11_shots.js                          # F11 evidence: the 3 tab panels that hid the leaks
```

`scripts/check_workspace_ui.py` is **not** usable from this agent session: it opens settings and
confirmation dialogs, and §1 of the pending-tasks doc forbids pointing it at an actively trading
instance. The QA mirror is live-disabled, but dialog-opening runs are a deliberate action to take
only after the read-only slices land.

## Retracted during this pass (do not chase)

- "1.23 contrast" on primary buttons (`Run backtest`, `Save Global Config`, …): the probe composites
  ancestor `background-color` and misses fills painted by a gradient/pseudo-element. Measured in-page
  the button is `rgb(89,217,187)` on `rgb(9,42,35)` — passes.
- `empty_buttons: 1` on every page: that is the hidden sidebar backdrop carrying an `aria-label`
  (`templates/base.html:52`).
- "Skip to workspace" low contrast: an off-screen skip link.
- Font-preload warnings seen earlier were artifacts of the hidden in-app Browser surface, not the page.
- `0×0` canvases: inside `[hidden]` panes.
- 🟢 / ⚪ status dots: deliberate, `aria-hidden`, house convention.
