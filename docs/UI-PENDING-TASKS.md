# UI pending tasks — local live-market verification

**Created:** 1 October 2026 (IST)
**Owner:** Local operator / developer
**Status:** Pending verification on your machine
**Related plan:** [UI redesign plan and completed validation](UI-REDESIGN-PLAN.md)

## Purpose and current baseline

Verify the redesigned UI with your actual market feed, existing strategy instances, orders, positions, broker configuration and realistic financial values. Record what fails, update the affected UI, and retest it.

Already checked in the isolated preview: shared shell, empty/default screen layouts, both themes at 1440/768/390px, basic dialog/keyboard behavior and an actual generated-data backtest. The previous pass recorded 90 layout checks, 172 targeted Python tests and 29 JavaScript harnesses passing. **Those results do not establish that populated live-market workflows are correct.** The preview had no cached market bars and no live order instances.

All checkboxes below are intentionally open. For each task, record **Pass / Needs update / Blocked / Not applicable**, with evidence. Mark its update checkbox only after a fix and retest, or explicitly record **“No update needed — verified.”** Do not mark an unavailable market/order state as passed.

## 1. Safety boundaries — read before starting

- Observe existing live orders and positions; **do not place, modify, cancel or close a real order merely to test the UI.**
- Do not arm/disarm live execution, flatten, pause/resume runners, change risk limits, deploy a strategy or apply optimized parameters solely for this review. Normal operational risk procedures take precedence over QA.
- Test submission, cancellation, retries, settings saves and destructive confirmations in an **isolated paper/sandbox environment**, not against your active trading book. Only open a live dialog if opening it is known to be non-mutating; otherwise inspect its paper equivalent.
- Do not intentionally disconnect a feed, expire a shared broker session, log out, restart the trading server or simulate backend failures on the production process. Reproduce those states in isolation.
- Settings/editor/authentication flows may initialize configuration or alter sessions. Review them in a separate development copy with a non-production database and credentials unless an operationally approved read-only review is available.
- **Do not point `scripts/check_workspace_ui.py` at an actively trading instance.** It opens settings and confirmation dialogs; it is not a passive monitoring tool. Automated tests belong in the isolated live-disabled environment described in the redesign plan.
- Keep credentials, OTPs, authorization headers, session tokens, account identifiers and unredacted financial records out of Git, screenshots shared publicly, bug reports and this document. Sanitize any network captures before sharing.

## 2. Preparation and evidence

- [ ] Record app revision/branch, browser, OS, local timezone, date/time and market-session phase.
- [ ] Record the active data source, execution mode, broker/profile and relevant segment names without secrets. Distinguish market-data connectivity from broker execution readiness.
- [ ] Identify existing examples: running/paused/stopped instances; open/closed positions; orders in the statuses already present; equity/options; paper/live. Record missing examples as blocked or schedule a paper fixture.
- [ ] Use time-aligned engine/broker snapshots as the reference for numbers. Record currency, price timestamp, fee basis and realized/unrealized treatment; asynchronous or differently scoped totals are not automatically UI defects.
- [ ] Capture before-fix screenshots in dark/light themes at desktop 1440px, tablet 768px and mobile 390px. Also check your actual monitor size and 200% browser zoom.
- [ ] Save sanitized evidence outside Git, for example `.cache/ui-review/local/`. Do not commit databases, portfolio state, broker sessions, logs or market datasets as UI fixtures.

### State coverage to collect

| State | Where to obtain it safely | Result / evidence |
| --- | --- | --- |
| Market open with fresh ticks and existing instances | Observe your actual running system | Pending |
| Market closed / holiday / naturally idle feed | Observe the normal session transition | Pending |
| Pending/working, partially filled, filled, cancelled/rejected orders, where supported | Existing history; missing cases in paper/sandbox | Pending |
| Long/short, profitable/losing/flat and closed positions | Existing records; missing cases in paper/sandbox | Pending |
| Multi-leg options with real instrument metadata | Existing instances; paper fixture for missing structures | Pending |
| Multiple strategies, brokers or segments | Existing configuration or isolated fixture | Pending |
| Slow/error/stale data, disconnected broker, expired auth | Isolated environment only | Pending |
| Large tables, long names and large financial values | Existing records or isolated fixture; never create real exposure for screenshots | Pending |

## 3. P0 — trading clarity and populated operations

Complete these before treating the UI as ready for everyday live-market use.

### UI-01 — truthful connection, source and market status

- [ ] **Verify:** Feed-active/idle/stale states follow actual tick freshness and session timing. A connected broker is not automatically an active feed or an execution-ready account. Paper/live, real/generated data, global gate and risk states remain distinct. Unknown/failed reads never look like zero, safe, connected or OFF. Market-close/holiday states do not imply a broken connection without evidence.
- [ ] **Update/retest:** Correct ambiguous labels, stale-state handling, timestamps, semantic colors and status alignment. In isolation, check reconnect/recovery without duplicated indicators or lost focus. Never manufacture a healthy state or weaken a guard.

### UI-02 — portfolio overview and strategy/instance matrix

**Pages:** `/portfolio`, `/portfolio/paper`, `/portfolio/live`

- [ ] **Verify:** Bucket totals, instance counts and row membership have the right scope; paper records never appear as live. Running/paused/stopped/error states are legible. Long strategy names, symbols, IDs and mixed instruments fit populated rows. Sorting/filtering, selection, pagination and details remain stable as updates arrive. Switching page, theme or viewport does not change an instance's state.
- [ ] **Update/retest:** Fix hierarchy, column widths, truncation with accessible full text, numeric alignment, sticky controls and internal scrolling. If a control targets the wrong row/bucket, classify it as P0 and stop interacting with it until resolved; do not hide the issue cosmetically.

### UI-03 — financial values and live parameters

- [ ] **Verify:** Equity, allocated/free capital, margin/exposure, prices, quantities, realized/unrealized/daily/net P&L and limit usage match the documented, time-aligned reference. Currency symbols and grouping are consistent; positive/negative/zero/unknown states are distinguishable. Where supplied by the backend, check bid/ask/spread, volume, OI/IV, indicator/signal values and quote age against their stated source; unsupported fields must not be invented. Percent/fraction, drawdown sign/magnitude, lots/units and price precision are not confused. Large, negative and missing values do not clip or change meaning when rounded for display.
- [ ] **Update/retest:** Fix formatting, labels, precision presentation, spacing and tooltips using the existing formatters and authoritative metadata. **Do not change trading calculations, round payloads or substitute frontend-derived totals to make the display look correct.** Log a separate backend/data issue when the reference itself disagrees.

### UI-04 — orders tab and order lifecycle

- [ ] **Verify:** Existing order statuses, requested/filled/remaining quantities, prices, timestamps, broker/instance identifiers and rejection reasons are clear. Filters and counts respect bucket/broker/strategy scope. Partial fills and asynchronous updates do not duplicate rows, erase selection or move focus to a different order. New activity is discoverable without forcing the operator to the top of the table.
- [ ] **Update/retest:** Refine status badges, reason wrapping, column priorities, filters, pagination and update indicators. Verify cancel/modify/retry targeting, pending/duplicate-submit/error states and keyboard behavior **only on paper/sandbox orders**. Do not click live order actions for QA.

### UI-05 — positions, option legs and action dialogs

- [ ] **Verify:** Long/short direction, open/closed state, entry/LTP/exit, P&L, quantity, lot size, margin, SL/TP and broker/instance linkage are unambiguous. Options show the correct underlying, strike, expiry, CE/PE, legs and available Greeks. Missing/stale quotes or Greeks remain unknown, not zero. Parent/leg grouping and totals do not hide offsetting exposure or mix scopes.
- [ ] **Update/retest:** Improve grouping, table density, detail disclosures, accessible descriptions and small-screen scrolling. In paper/sandbox, inspect exit/partial-exit/SL/TP dialogs: target, side, quantity/unit, scope, warnings, validation, Cancel/Escape, footer visibility and duplicate-submit handling. Never hard-code current exchange lot sizes or test exits on live positions.

### UI-06 — risk, execution gates and emergency controls

**Page:** `/risk` plus global/bucket/instance risk controls

- [ ] **Verify:** Limit usage, halt/breach state, broker/segment scope and warning severity reflect actual configuration. A limit not configured is not a zero-used limit; an unknown gate is not OFF/ready. Emergency actions are separated from ordinary controls, and their displayed target/scope is accurate. Observe real alerts already present; do not create real breaches for testing.
- [ ] **Update/retest:** Clarify thresholds, units, warning hierarchy and confirmation copy. In isolation, check Cancel/Escape/focus restoration, changing/stale targets, disabled/busy/error states and accessible names. Preserve server-side live gates, scope filters and existing execution safeguards exactly.

## 4. P1 — connected configuration and realistic research states

### UI-07 — cached symbols, timeframes and market-data manager

**Pages:** `/data`, instrument selectors in research screens

- [ ] **Verify:** Inventory and coverage reflect your real cache; symbol search/category filters, available-only selection, interval lists and date boundaries match server metadata. Empty coverage is distinct from DB/network failure. Real data is not labelled synthetic; the generated-demo action is absent outside a configured synthetic source. Session gaps, stale data and timezone boundaries are explained rather than hidden.
- [ ] **Update/retest:** Improve coverage tables, error/empty/progress states and interval/date hints. Test download/sync/retry and interrupted work on an isolated cache only. Do not fabricate cached bars or silently replace a failed real source with generated prices.

### UI-08 — expanded broker cost and segment editors

**Page:** `/settings` — isolated configuration copy for editing

- [ ] **Verify:** Every expanded field has a label, correct unit, bounds/help and sensible grouping. Cost presets, validation status, segment capital/mode/risk limits, audit history and arming blockers are understandable. In-use/available profiles are not presented as authenticated connections. Saved/new-run vs currently running strategy behavior is explicit.
- [ ] **Update/retest:** Refine long editor layouts, modal/body scrolling, footer placement, validation/error messages and disabled/busy controls. Test save/cancel/reload/audit behavior only in the isolated copy. Verify unknown live-gate reads remain unavailable; never save production config or toggle its gate just to finish a checkbox.

### UI-09 — broker board and nested authentication

- [ ] **Verify:** Existing broker/segment cards have truthful connection, session and validation status without exposing secrets. Broker names and long errors fit. Authentication, OTP, expired-session and nested-dialog variants are reviewed with non-production credentials or a controlled fixture.
- [ ] **Update/retest:** Fix nested focus containment/return, Escape/Cancel, loading/error/retry copy and responsive layouts. Distinguish authentication, market feed, cost validation and live readiness. Do not log out, reauthenticate or intentionally expire the account used by active runners.

### UI-10 — Backtest with real cached data

**Page:** `/backtest` — isolated research process with a legitimate data snapshot

- [ ] **Verify:** Selected symbol, date range, interval, engine, parameters, capital and fees match returned provenance. Charts, ledger, metrics, diagnostics, cost shock and certification remain coherent with a populated result. Axis/tooltips preserve money/percentage/price units; open vs closed trades and insufficient-data warnings are clear. Check real-data success and legitimate failures, not only a visually attractive result.
- [ ] **Update/retest:** Reduce repeated warnings, improve dense result/detail layouts and verify loading/error/stale-result handling, exports and recent-run restoration. Retain actual backend data and failed/unproven certification; never turn a UI pass into a claim of trading edge.

### UI-11 — Compare populated strategies and symbols

**Page:** `/compare`

- [ ] **Verify:** Two-to-four slots, shared conditions, per-slot parameters and generalization mode remain clear with real results. Labels/IDs are unique; correlation/significance, failure/partial-result warnings and normalized equity versus money/percentage axes are correct and readable. Shared-date and data-source differences are not hidden.
- [ ] **Update/retest:** Improve slot widths, legends, result tables, chart contrast and comparison-panel wrapping. Check add/remove/rerender, keyboard use and hand-offs from Backtest without overwriting conditions or automatically running anything.

### UI-12 — Optimize setup, running job and completed results

**Pages:** `/optimize`, `/optimize/runs/<actual-run-id>`

- [ ] **Verify:** Current/min/max/step, constraints, estimates, walk-forward splits and data attestation have meaningful labels/units. Review actual queued/running/completed/failed/cancelled states, progress, ranked rows, parameter deltas and detailed charts—not just the not-found template. Source and certification limitations remain visible.
- [ ] **Update/retest:** Refine long tables, progress/error states, detail tabs and parameter layouts. Test draft/start/cancel/resume/apply flows only in the isolated research/paper process. Theme changes and opening results must never apply parameters to a live runner.

### UI-13 — Strategy Builder and generated parameter forms

**Page:** `/strategy-builder` — isolated authoring environment

- [ ] **Verify:** Long Pine input, conversion success/failure, warnings, generated strategy names and parameter schemas render correctly. Numeric, text, boolean and any supported choice controls have associated labels and useful errors. Editor output and diagnostics fit narrow screens and remain keyboard accessible.
- [ ] **Update/retest:** Improve editor/result hierarchy, wrapping and save/convert busy states. Verify downstream strategy selection and parameters using a non-production strategy; no automatic deployment, live activation or mutation of a running strategy.

### UI-14 — Engine Playground / Forward

**Page:** `/forward`

- [ ] **Verify:** Engine/data/mode, instrument, interval, time, feed or replay state, parameters and available charts are explicit. Recorded/live/replayed data are not conflated. Loading, no-tick, stopped and error states remain distinguishable, and interval labels reflect the source's actual capabilities.
- [ ] **Update/retest:** Improve controls, streaming-chart density and log wrapping. Exercise start/stop/replay/configuration actions only on isolated paper/replay instances; preserve authentication/source guards and do not interrupt live feeds for QA.

### UI-15 — Analytics, deep dives and portfolio intelligence

**Pages:** `/analytics` and portfolio intelligence/detail panels

- [ ] **Verify:** Multiple strategies/brokers/segments, period filters, equity/drawdown/distribution charts, cross-broker comparisons and trade history retain their scope and provenance. Low sample sizes, missing periods and unavailable metrics are not presented as established performance. Units and timestamps stay consistent across tiles, legends and tooltips.
- [ ] **Update/retest:** Refine populated grids, long legends, drill-down dialogs, empty/partial states and dense numeric tables. Check opening/closing details and filter changes do not reset unrelated selections or obscure safety status.

### UI-16 — Reporting, taxes, costs and reconciliation

**Page:** `/reporting`

- [ ] **Verify:** Paper/live and broker/date filters, gross/net/realized P&L, fees/taxes and reconciliation status match the appropriate reference and accounting basis. Warnings, sample/demo labels and unsupported/unmapped items are visible. CSV/downloaded amounts, signs, columns and dates agree with the screen.
- [ ] **Update/retest:** Improve populated summaries, cost breakdowns, long warnings and contrast for positive/negative/warning states. Review exports with sanitized records; do not change accounting rules or silently suppress reconciliation differences.

### UI-17 — alerts, logs and real-time update behavior

- [ ] **Verify:** Existing risk/strategy/broker alerts show readable severity, timestamp, source and context; no alert is indistinguishable from an unavailable alert feed. Large alert/log lists fit and do not cover important controls. New ticks/events preserve table selection, scroll position and input focus; notifications do not steal focus or duplicate on reconnect.
- [ ] **Update/retest:** Refine toast placement/lifetime, alert drawer/filter/settings layouts and long message wrapping. Test acknowledgement, configuration and error recovery only in isolation when they mutate state.

## 5. P2 — cross-screen usability and accessibility

- [ ] Recheck **populated/expanded** states in both themes at 1440/768/390px and your usual monitor size. Tables may scroll internally; the whole document should not scroll horizontally. Titles, warnings, modal actions and broker/source status must not be clipped.
- [ ] Check large financial values, long identifiers, many rows/legs, wrapped errors and missing values. Use existing records or isolated fixtures, not new real trades.
- [ ] Confirm Inter/JetBrains Mono load locally, tabular numerals align, units/signs remain visible and SVG/status icons have meaningful text. Replace residual one-off colors with semantic tokens, not color-only meaning.
- [ ] Switch theme with populated charts visible. Data, labels, domains, selection, parameters, trading mode and runtime state must remain unchanged; only presentation changes.
- [ ] Keyboard-test search, filters, tabs, disclosures, pagination and every expanded dialog. Check labels, focus visibility, containment, Escape/Cancel and return to the invoker. Never use Enter/Space on live trading actions as a keyboard test.
- [ ] Check 200% zoom, reduced motion and a manual screen-reader pass. Announce important errors/status changes without reading every tick or replacing the operator's focus.
- [ ] Run axe on these expanded/populated states in the isolated copy. Investigate applicable contrast, label, role and duplicate-ID findings; an automated pass does not replace the manual checks.
- [ ] Check sanitized browser console/network information during observation and after isolated fixes. Separate UI exceptions from legitimate backend/market/session failures; failures should be explained in the UI, not masked.

## 6. Where to update when a check fails

Paths below are relative to `src/backtest/web/`. Start with the current file/DOM hook; preserve existing IDs, `data-*` keys, APIs, units, scope filters and confirmation handlers.

| Area | Likely UI files |
| --- | --- |
| Shared theme, sizing, primitives | `static/css/tokens.css`, `static/css/app.css`, `static/css/workspace.css` |
| Shell, navigation, keyboard/dialog presentation | `templates/base.html`, `templates/_macros.html`, `static/js/workspace.js` |
| Portfolio hierarchy/matrix/spawn | `templates/portfolio*.html`, `templates/_portfolio_center.html`, `static/js/portfolio.js`, `static/js/components/landing_risk.js` |
| Orders/position dialogs | `static/js/components/orders_tab.js`, `static/js/components/position_actions.js`, `static/js/components/option_view.js`, `templates/_portfolio_center.html` |
| Broker board/auth | `static/js/broker_status.js`, `static/js/broker_board.js`, `static/js/broker_auth_modal.js` |
| Risk and alerts | `templates/risk.html`, `static/js/components/risk_page.js`, `static/js/components/risk_strip.js`, `static/js/components/alert_widget.js` |
| Settings | `templates/settings.html` (includes the controller) |
| Cached data and instrument/timeframe controls | `templates/data_manager.html`, `static/js/data_manager.js`, `static/js/components/symbol_picker.js`, `static/js/components/timeframes.js` |
| Research and parameter labels | `templates/backtest.html`, `static/js/backtest.js`, `static/js/components/params_form.js`, `static/js/charts/`, `static/js/components/chart_theme.js` |
| Compare/Optimize | `templates/compare.html`, `static/js/compare.js`, `static/js/compare/`, `templates/optimize*.html`, `static/js/optimize_setup.js`, `static/js/optimize_run.js` |
| Builder/Forward | `templates/strategy_builder.html`, `templates/forward.html`, `static/js/forward.js` |
| Analytics/intelligence/reporting | `templates/analytics.html`, `static/js/analytics.js`, `static/js/cross_broker.js`, `static/js/deep_dive.js`, `static/js/components/portfolio_intelligence.js`, `templates/reporting.html`, `static/js/reporting.js` |

### Fix loop

1. Record task ID, priority, environment, exact state and expected/actual behavior before editing.
2. Separate presentation problems from backend/data/execution defects. Wrong money values, action targets or scope are **P0**, not cosmetic fixes.
3. Make UI changes in a development worktree/copy; do not hot-reload untested changes into an actively trading process.
4. Add a focused regression test when possible. Existing `tests/js/` harnesses and `tests/test_workspace_design.py` cover important contracts; use the redesign plan's test commands only in an isolated, non-production environment.
5. Retest the original state plus its opposite/error case, both themes and relevant widths. Confirm unchanged numeric data, payloads, source/mode and action targeting.
6. Record the fix/files/test/evidence. Roll out to the live machine only through your normal controlled deployment process; this document does not authorize a real trading action.

## 7. Issue / update log

Copy one entry per issue. Leave private data out of the evidence.

**Reconciled against code 2026-10-06:** the two functional findings from the
2026-10-02 QA walkthrough ([UI-WALKTHROUGH-AND-QA-2026-10-02.md](UI-WALKTHROUGH-AND-QA-2026-10-02.md))
are **fixed in source on this branch** — recorded below for live retest, not
listed as open bugs.

```text
Issue ID / task ID: QA finding #1 → UI-16 (/reporting)
Priority: P0 (page unusable with paper trades on)
Environment: observed live (QA 2026-10-02)
Fix: src/backtest/reporting/consolidator.py — the trade sort now uses an aware
     datetime.min.replace(tzinfo=timezone.utc) sentinel and coerces naive
     exit_time values to UTC, so naive and aware timestamps no longer mix.
Result: code-verified 2026-10-06; needs a live retest with include_paper=true.
Update decision: Fixed in code — pending live retest

Issue ID / task ID: QA finding #2 → UI-02 (bucket isolation)
Priority: P0 (paper position shown on the live page)
Environment: observed live (QA 2026-10-02)
Fix: src/backtest/web/static/js/portfolio.js render() now filters p.positions
     by (pos.mode || "paper") === PAGE_MODE next to the existing p.runners
     filter, so scoped pages drop other buckets' positions.
Result: code-verified 2026-10-06; needs a live retest with paper + live books.
Update decision: Fixed in code — pending live retest
```

Blank template for new issues:

```text
Issue ID / task ID:
Priority: P0 (safety/data/scope) / P1 (workflow) / P2 (visual polish)
Environment: observed live / isolated paper / isolated research
App revision, browser, date/time/timezone:
Page, theme, viewport, market/session state:
Existing instance/order/position state (sanitized):
Expected behavior and authoritative reference/basis:
Actual behavior:
Safe reproduction steps:
Evidence location (not committed; sanitized):
Classification: UI / backend-data-execution follow-up
Files changed / related issue:
Retest: original state, alternate/error state, dark/light, widths, keyboard
Result: Pass / Needs update / Blocked / Not applicable (reason)
Update decision: Fixed and retested / No update needed — verified
Operational actions performed: None, or explicitly record approved actions separately
```

## 8. Completion / local handoff

- [ ] All P0 tasks verified with relevant populated states; blocking safety/data/scope issues resolved or explicitly prevent sign-off.
- [ ] P1 workflows verified or individually documented as blocked/not applicable with a reason and follow-up owner.
- [ ] UI updates retested in isolation, with regressions and applicable accessibility checks passing.
- [ ] Pending advanced states, backend issues and deployment limitations are recorded rather than marked complete.
- [ ] No broker secrets, runtime state, logs, databases or market datasets added to Git.
- [ ] Record whether the live review was observation-only; distinguish any separately approved operational activity from UI QA.
- [ ] Update this checklist and the redesign plan with actual local results, screenshots/evidence references and remaining tasks.

**Local review date:** __________
**Reviewer:** __________
**Revision reviewed / revision deployed:** __________
**P0 open issues:** __________
**Next tasks / owner:** __________
