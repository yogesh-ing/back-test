# Trading workspace — product design audit & implementation plan

**Date:** 1 October 2026

**Scope:** The existing Flask/Jinja trading application. Preserve its execution engines, API contracts, source policy, broker authentication, monetary semantics and risk safeguards.

**Design direction:** Precision — a calm, professional research and execution workspace, not a decorative dashboard.

**Progress:** Shared foundation, application shell and detailed Backtest redesign implemented and validated. Supporting screen compositions are implemented. Populated operational workflows remain a separate follow-up pass; see the remaining checklist below.

**Status (2026-10-06):** the redesign is implemented in the shipped templates —
the sidebar shell, token system, self-hosted fonts and pinned Chart.js are all
in `src/backtest/web/` (`templates/base.html`, `static/css/tokens.css`,
`static/fonts/`, `static/vendor/chart.umd-4.4.1.js`). Post-audit UI work (data
tab rework, Pine builder convert flow, broker-session alerts) is documented in
WEB-UI.md and UI-PENDING-TASKS.md.

## 1. What exists today

The application has 14 primary page views, a shared application shell, two stylesheets, and a substantial set of framework-free JavaScript components. Its workflows already cover:

- Single-strategy backtesting, comparison, robustness checks, and optimization.
- Strategy authoring via the Pine Script converter.
- Engine replay, multi-runner paper/live portfolios, orders, positions and playbooks.
- Global, bucket and runner risk controls; emergency flatten confirmation.
- Portfolio intelligence, strategy/broker analytics, P&L/tax reporting.
- Broker connections, cost models, segments, and historical data management.

The frontend is **Flask + Jinja + vanilla JavaScript + Chart.js**. A framework migration is unnecessary and would increase risk. The knowledge graph mentioned in `AGENTS.md` is absent in this checkout, so this audit uses the current UI documentation, templates, styles and controllers.

*Status: still accurate today; the knowledge graph (`graphify-out/`) is still absent from this worktree.*

### Findings

| Finding | User impact | Design response |
| --- | --- | --- |
| Eleven links share a fixed-height horizontal navigation bar | Important destinations crowd or overflow; research and trading are mixed | Grouped, persistent sidebar; responsive mobile drawer; clear current-page state |
| Dark base styles compete with a bright gradient theme and inline styling | No coherent visual language; primary, status and danger colors compete | One documented token system; quiet surfaces; restrained teal action accent |
| System UI fonts, browser button fonts, emoji headings and inconsistent numeric treatments | Reduced legibility, inconsistent tone and unstable financial alignment | Self-hosted Inter and JetBrains Mono; tabular numeric alignment; consistent SVG icons |
| Pages use different widths, heading styles, spacing and card hierarchies | Every screen requires relearning | Shared page headers, density/spacing scale and component conventions |
| Backtest starts with a mostly blank result panel | No orientation, guidance or explanation of what results will contain | Honest results workspace with a purposeful empty state and a short workflow guide |
| Portfolio overview repeats titles, architecture explanations and multiple nested panels | Metrics, strategy operations and risk actions compete for attention | Clear bucket summaries and command-center hierarchy, concise copy, contextual assistance |
| Chart styling hard-codes colors and the equity tooltip hard-codes dollars | Theme and currency can disagree with the rest of the app | Shared, theme-aware chart styling; existing currency formatter for monetary tooltips |
| Some states initially imply “LIVE” before portfolio status is known | Simulation or missing state can be mistaken for live execution | Neutral initial status; explicit paper/live and source labels remain authoritative |
| Focus, mobile layouts and modal keyboard support are uneven | Slower keyboard use and difficult small-screen operation | Visible focus, skip link, accessible navigation, reduced motion, targeted keyboard checks |
| Chart.js depends on an external CDN | Network failure can prevent core charts rendering | Pin and serve the existing Chart.js version locally with its license |

## 2. Product principles

1. **Truth before polish.** Never fabricate market data, returns, trades or a connected broker. Missing values remain `—`; empty portfolios remain empty.
2. **Separate research from execution.** Navigation and mode/source labels make this boundary obvious.
3. **Make risk unmistakable, not omnipresent decoration.** Red is reserved for losses, halts, live-money warnings and destructive actions. Confirmation flows remain intact.
4. **Optimize for repeated professional use.** Compact, readable tables; aligned amounts; stable layouts; less decorative noise.
5. **Use progressive disclosure.** Lead with decisions, then expose parameters, diagnostics and explanations where relevant.
6. **Improve incrementally.** Retain DOM IDs, data attributes and API requests used by existing controllers; validate each pass.

## 3. Design system specification

*Status: implemented — `static/css/tokens.css` (dark/light tokens), self-hosted
Inter + JetBrains Mono in `static/fonts/`, the ~224px sidebar and ~64px top bar
in `templates/base.html`, and the theme toggle persisting to
`localStorage["trading-workspace.theme"]`.*

### Color

- Default: low-glare graphite canvas, layered charcoal surfaces, subtle cool-gray borders.
- Primary: restrained teal, used for a current destination and the principal action.
- Typography: soft white primary text and accessible slate secondary text.
- Semantic states: emerald for positive/safe, rose for negative/destructive, amber for caution, blue for information.
- Optional light theme: warm-white canvas, clean white surfaces and darker semantic text. The choice persists locally and must not affect trading settings.
- Charts use the same theme tokens. Color supplements labels and icons; it never replaces them.

### Typography

- **Inter variable:** interface, labels and headings; self-hosted with a system fallback.
- **JetBrains Mono:** metrics, prices, quantities, percentages, code and timestamps; self-hosted with a monospace fallback.
- Page titles: 28px; section titles: 16px; body: 13–14px; labels/helper text: 11–12px.
- Tabular numerals for financial content; right-align numeric table columns.

### Geometry and behavior

- Spacing scale: 4, 8, 12, 16, 20, 24, 32px.
- Sidebar: approximately 224px. Top utility bar: approximately 64px.
- Surface radii: 8px controls, 12px cards, pills only for compact status.
- One dominant action per section. Secondary actions are outlined/quiet; destructive actions are distinct and separated where possible.
- Desktop: persistent navigation and multi-column workspaces. Tablet: narrower/reflowed panels. Mobile: navigation drawer, stacked configuration/results and internally scrollable tables.
- Respect `[hidden]`, disabled controls, high-contrast focus and reduced-motion preferences.

## 4. Implementation passes & checklist

*Status (2026-10-06): Passes 0–6 are complete as ticked below. The two unticked
items — populated paper-fixture validation of orders/positions/options and the
expanded editors / nested-auth review — remain open; see UI-PENDING-TASKS.md.*

### Pass 0 — Audit and plan
- [x] Inventory templates, assets, page routes and frontend contracts.
- [x] Review shared styles, source taxonomy, monetary formatting and risk controls.
- [x] Identify safe redesign boundaries; avoid trading-engine changes.
- [x] Save this plan and prioritized checklist.
- [x] Capture representative baseline desktop/mobile views.

### Pass 1 — Foundation and navigation
- [x] Introduce documented dark/light tokens and self-hosted fonts.
- [x] Replace the crowded top navigation with a grouped sidebar and compact utility bar.
- [x] Add current-page indication, mobile drawer, skip link and keyboard focus styles.
- [x] Add theme preference without changing execution preferences.
- [x] Preserve all broker/risk/alert mounts and source/currency body attributes.
- [x] Self-host the pinned Chart.js dependency.

### Pass 2 — Shared components
- [x] Normalize cards, buttons, labels, inputs and parameter fieldsets.
- [x] Refine metrics, provenance badges, tabs, progress bars and tables.
- [x] Refine notices, loading/error/empty states, toasts, drawers and modals.
- [x] Make chart styling theme-aware and monetary tooltips currency-aware.
- [x] Keep all existing frontend behavior tests passing.

### Pass 3 — Research workspace (first detailed screen)
- [x] Give Backtest a consistent page header and configuration hierarchy.
- [x] Design an honest, useful initial results workspace; do not insert fake performance.
- [x] Improve result chart/actions layout while retaining diagnostics and certification.
- [x] Carry the same hierarchy into Compare, Optimize and Strategy Builder.
- [x] Verify a real synthetic-data backtest in the safe preview environment.

### Pass 4 — Trading and portfolio
- [x] Refine bucket overview cards and paper/live scope headers.
- [x] Reduce repeated command-center copy; clarify metrics and runner operations.
- [x] Improve matrix controls, compact table treatments and consolidated-view tabs.
- [x] Preserve money-moving controls, source labels, scope separation and emergency confirmations.
- [x] Check empty portfolio views, broker board and add-instance modal.
- [ ] Validate populated orders/positions, their action dialogs and option-runner controls using a safe paper fixture.

### Pass 5 — Support screens and consistency
- [x] Apply consistent heading and layout treatment to Analytics, Risk and Reporting.
- [x] Improve Settings and Data Manager controls and responsive layouts.
- [x] Remove conflicting one-off colors in reviewed default states, research results and shared components.
- [ ] Review expanded broker/segment editors, nested authentication and other advanced operational states.
- [x] Check all routed pages in dark and light themes.

### Pass 6 — Quality gate and handoff
- [x] Add regression tests for shell, theme, navigation and shared frontend behavior.
- [x] Run existing Node component harnesses and relevant Python UI/API tests.
- [x] Inspect representative desktop, tablet and mobile layouts for overflow.
- [x] Check keyboard navigation, drawer close behavior and theme persistence.
- [x] Check browser console/errors, local assets and preview host compatibility.
- [x] Update this checklist with actual outcomes and any remaining work.

## 5. Screen priorities

*Status: all P0/P1/P2 screens ship the redesign; the priorities below were the
ordering used, not a list of outstanding work.*

| Priority | Screens | Main objective |
| --- | --- | --- |
| P0 | Shared shell, Backtest (`/` and `/backtest`) | Establish coherent product language and improve the first impression |
| P0 | Portfolio overview and paper/live command centers | Improve operational hierarchy without altering safety semantics |
| P1 | Compare, Optimize, Engine Playground | Make research workflows feel like one product |
| P1 | Risk, Analytics, Reporting | Improve data readability and action hierarchy |
| P2 | Settings, Data, Strategy Builder | Normalize utility screens and responsive layouts |

## 6. Acceptance criteria

*Status: verified by the 2026-10-01 preview runs in §7 (90 layout checks, axe
scans, keyboard checks, 172 Python + 29 Node harnesses).*

- Every existing page and destination remains reachable.
- Trading modes, data provenance, broker status and risk information stay explicit.
- No financial data is invented for design purposes.
- Existing control IDs, event integrations and API contracts remain compatible.
- No automatic run, trade, live arming, deployment or broker login is triggered by the redesign.
- Navigation, configuration and tables are usable at 390px, 768px and 1440px widths.
- Dark/light themes use readable text and chart labels, including after a theme change.
- Source policy and runtime state are not weakened or committed as design fixtures.
- Design dependencies are local, licensed and small; no new frontend framework or build pipeline.

## 7. Validation notes

The preview uses the existing **testing source profile**, generated data and `ALLOW_LIVE_ORDERS=0`. This is a UI verification environment, not a live trading connection. Baseline and test screenshots belong in ignored local tooling output, not in Git.

### Verified outcomes — 1 October 2026

- **15 route paths × 3 viewport sizes × 2 themes = 90 layout checks passed.** Sizes: 1440×1000, 768×1024 and 390×844. Each responded 200, selected the correct navigation item, had no document-level horizontal overflow, no duplicate dynamic control IDs and no unhandled page JavaScript errors. The route set includes `/`, `/backtest`, both portfolio scopes and the optimization-detail template's not-found state.
- **30 default-state axe scans passed** using the WCAG 2 A/AA rule tags. Additional scans passed for the add-instance, broker-board and global-flatten dialogs, and for actual generated-data Backtest results in both themes. This is automated coverage of those states, not a blanket screen-reader/WCAG certification.
- **Keyboard/interaction checks passed:** theme persists through reload; Ctrl+K filtering and one-key Escape dismissal; help dialog; mobile drawer containment/Escape/focus restoration; legacy modal containment; paper add-instance Escape/focus restoration; broker board and flatten cancellation. The 390px add-instance footer stays inside the viewport while its body scrolls.
- **Real generated-data backtest passed:** explicitly choose the configured synthetic instrument, select SMA crossover, run the existing fill-exact backend with virtual capital and inspect the returned results, ledger and all three chart tabs. A held request verified the busy/duplicate-click safeguard. A theme switch preserved the chart's numerical data and changed its palette; the equity tooltip used the configured INR symbol. No numbers were inserted as design fixtures. Synthetic provenance and failed/insufficient certification remain visible.
- **Live gate checks passed:** the live-arming proposal was cancelled and sent no PUT; an intercepted failed GET displayed “Unavailable (state unknown)” with the toggle disabled, not a misleading OFF state. The real preview gate stayed OFF, `ALLOW_LIVE_ORDERS=0`, with no runner creation, broker authentication or money-moving action confirmed.
- **172 targeted Python tests passed; all 29 standalone Node harnesses passed.** The two new harnesses cover chart/currency/data preservation (10 cases) and stable, unique parameter-label namespaces (9 cases). New Python tests cover 15 shell routes, local/licensed assets, controller/safety hooks and the explicitly source-scoped generated-instrument action. JavaScript syntax, new Python lint and `git diff --check` passed.

### Refinements found by the browser gate

- Fixed mobile portfolio overflow with an internal equity-table scroll container.
- Removed competing small-button rules so neutral/cancel actions no longer inherit danger styling.
- Constrained modals with a scrolling body and a persistent, visible footer.
- Corrected Settings' DOM-node empty state and distinguished unavailable segments/live-gate reads from valid empty/OFF state.
- Corrected light-theme backgrounds and contrast for comparison modes, synthetic attestation, reporting semantics, paper badges and per-broker risk values.
- Associated dynamic risk, comparison, optimizer and spawn fields with meaningful labels. Each anonymous comparison parameter form now has its own stable namespace; sanitization cannot make two parameter IDs collide.
- Made Escape close page search even while a native search input contains a query.

### Environment limitation — real cached-market workflows

This preview has no `market_data_cache` table/cached market bars. The server still reports its known-instrument catalogue but no available cached instruments; `/api/data/inventory` therefore returns a handled backend error. The normal cached-market symbol flow, historical download/sync and connected-broker workflows cannot be certified here. **No cached bars or broker readiness were fabricated to conceal this.**

Backtest now offers **“Use generated demo instrument” only when the server-configured active source is synthetic**. It chooses the existing generator's DEMO instrument, labels it “generated random walk / not real market prices”, offers the generator's actual daily interval, and does not start a run, enable a source or alter a guard. Production DB/CSV views do not show this action; shared picker/API availability contracts are unchanged.

### Reproduce the quality gate

The browser gate is optional development tooling, not an application dependency:

```bash
python -m pip install playwright
python -m playwright install chromium
python scripts/check_workspace_ui.py --url http://127.0.0.1:5000 \
  --synthetic-backtest --screenshots .cache/ui-review
```

Run against an already-running **synthetic, live-disabled** preview. For persistent Settings metadata, use an isolated local SQLite file under `.cache/`, not a production DB or separate per-manager in-memory databases. `--axe /path/to/axe.min.js` enables the accessibility scans; `--browser` / `--browser-config` support a separately installed Chromium. All screenshots and database journals are ignored, not design fixtures committed to Git.

Regression command used (the testing DB profile isolates mocked live-runner tests from the deliberately OFF preview gate; it does not change that gate):

```bash
FORWARD_TEST_DB_PROFILE=testing BACKTEST_DATA_PROFILE=testing PYTHONPATH=src \
  python -m pytest tests/test_workspace_design.py tests/test_web_components.py \
  tests/test_broker_ui.py tests/test_web_taxonomy.py tests/test_portfolio_ui_views.py \
  tests/test_reporting_ui.py tests/test_options_spawn_ui.py tests/test_api_backtest.py \
  tests/test_api_backtest_comparison.py tests/test_api_backtest_parallel.py \
  tests/test_api_backtest_provenance.py tests/test_api_data_source_policy.py \
  tests/data/test_sources_policy.py -q
for test in tests/js/test_*.mjs; do node "$test" || exit 1; done
```

### Next passes — deliberately not claimed complete

*Status (2026-10-06): items 1–4 are still open; none of the UI changes since
this plan (data tab rework, builder convert flow, broker-session alerts)
claims to have closed them.*

1. Populate an isolated safe paper book and verify orders/positions, option workflows and action dialogs without any live broker session.
2. Review expanded settings editors, nested auth and detailed result states for Compare/Optimize/Strategy Builder; continue reducing local inconsistencies.
3. With a legitimately configured market cache, validate cached-symbol/date coverage, data management and real-data research hand-offs.
4. Perform manual screen-reader and broader keyboard review of the advanced states. Do not arm live or authenticate a broker merely to validate the redesign.
