# Options Forward-Test Experiment Log — honest findings

> **Provenance note (2026-09-17, architect rebuild).** The original experiment
> log was one of the files lost in the uncommitted-working-tree incident (see
> `docs/ARCHITECT-REVIEW-2026-09-17.md` §1). This file is the rebuilt baseline:
> every finding below is **consolidated from committed sources only** — task
> records, architecture docs, code comments and the tests that pin them — each
> with its citation. Nothing here is re-measured or invented. New findings
> should be appended with date, setup, numbers, and the code change they caused.

---

## How we run experiments (protocol)

1. **Paper bucket only.** No experiment touches the live bucket; `mode=live`
   runners are a separate, later track (F-12 wiring).
2. **One change at a time.** A finding gets a number, a setup, an observation,
   and — when accepted — exactly one remediation with a test that pins it.
3. **Every number is reproducible.** The runner config (strategy, playbook
   snapshot, capital, source) is recorded; synthetic-source runs are
   deterministic by design (seeded feeds), so a finding can be replayed.
4. **Costs are real.** Runs use the India cost model (STT/exchange/SEBI/stamp/
   GST; options flat ₹20/order, sell-side STT) — never gross P&L.
5. **Honesty rule.** A run priced off synthetic chains is labelled as such
   (runner `quote_source`), and its absolute P&L is treated as machinery
   validation, **not** evidence of edge.

---

## E-1 — Same-bar re-entry churned −₹41,844 (2026-09-16) — CLOSED

* **Setup:** forward experiment, 2026-09-16; option runner with
  `reenter=true`; signal-flip exits.
* **Observation:** the old `_blocked_reentry` returned `not _should_reenter`,
  which allowed a same-bar flip-then-re-enter. The runner churned
  **−₹41,844** entering/exiting on consecutive bars around every flip —
  costs plus whipsaw, no informational edge between bars.
* **Sources:** `docs/UNIFIED-TRADING-TASKS.md` (U2.2/U3.2 as-builts),
  `docs/ARCHITECTURE-UNIFIED-TRADING.md` §"Re-entry",
  `src/backtest/forward/execution_engine.py` (evidence comment),
  `CONSULTANT_RESPONSE.md` §7 exit precedence.
* **Remediation (pinned):** same-bar re-entry is **impossible by design**
  (`_exit_bar_index == _bar_index` always blocks); `reenter` defaults
  **false**; `max_reentries_per_day` (default 2) caps deliberate churn in V1.1.
  Tests: `tests/forward/test_options_exit_policy.py` (same-bar block),
  `tests/test_playbooks_models.py` (default false).
* **Lesson:** exit/entry symmetry needs an explicit bar-delay policy or the
  cost model will eat the strategy through its own flips.

## E-2 — Exit precedence had to be total order, not vibes (2026-09-16) — CLOSED

* **Setup:** the consultant question "when `signal_flip` and `stop_loss_pct`
  trigger on the same bar, which wins?" — and the general case of stacked exits.
* **Decision (implemented, not just documented):** strict precedence,
  `forward/execution_engine.py::EXIT_PRECEDENCE`:
  `0 emergency (engine, unconditional) → 1 stop_loss_pct (playbook) →
  2 take_profit_pct (playbook) → 3 time/DTE square-off (playbook-configured,
  engine-executed) → 4 signal_flip (strategy, last)`.
* **Sources:** `docs/ARCHITECTURE-UNIFIED-TRADING.md` §1;
  `CONSULTANT_RESPONSE.md` §7.1 (exit precedence confirmed as open question 3).
* **Lesson:** any exit rule added later must slot into the list explicitly —
  "it seemed to work" is not a precedence.

## E-3 — ₹392 NIFTY: scale matters more than logic (2026-09, task D1) — CLOSED

* **Setup:** synthetic feed seeded every symbol at ₹80–450, including "NIFTY".
* **Observation:** the chain generator priced `NIFTY 400 CE` on a 50-point
  grid — two usable strikes, ₹75 lots, nothing like the real contract. Worse:
  `directional_options` ships `scale_points=100`, so at ₹392 spot the
  close-to-EMA distance never reached the confidence floor and the strategy
  emitted **no views at all** — an API-created option runner sat flat forever.
* **Remediation (pinned):** `INDEX_BANDS` in `forward/feed.py` starts index
  symbols inside their real bands (NIFTY ~24.5–25.5k, BANKNIFTY ~51–53k);
  equity symbols keep byte-identical draws. Tests:
  `tests/forward/test_synthetic_feed_scale.py` (bands contain the generator's
  default spots; a default-param option runner opens AND closes structures).
* **Lesson:** strategy defaults and data scale are coupled; a "broken"
  strategy is sometimes a correctly-coded strategy on wrongly-scaled data.

## E-4 — Option MTM pinned at zero without a market clock (task A1) — CLOSED

* **Setup:** option runner left its legs at entry premium through the run.
* **Observation:** `unrealized_pnl` stayed 0.0 — the bridge had no per-bar
  pricing clock, so theta/spot never moved the marks.
* **Remediation (pinned):** `OptionsBridge.on_bar` is the forward loop's
  market clock: moves the shared generator's spot per bar close, pins the
  quote provider's pricing reference to the **bar** timestamp (replay clock,
  not wall clock), marks the book to market. Tests:
  `tests/forward/test_options_forward_mtm.py`.
* **Lesson:** marks must move with the replay clock; wall-clock pricing makes
  forward tests unreproducible.

## E-5 — N option runners = N private chains (the U6.2 overload) — CLOSED

* **Setup:** two option runners on the same underlying.
* **Observation:** each `OptionsBridge` built a private
  `SyntheticChainGenerator` + quote provider — two different views of NIFTY
  priced off two different last-bars, double chain cost, O(n) growth.
* **Remediation (pinned):** the Shared Market Data Bus
  (`forward/feed_registry.py`): one chain generator per underlying via
  `ChainBus`, one bar feed per `(source, symbol, timeframe)`, one mStock poll
  thread per manager. Tests: `tests/forward/test_feed_registry.py` (the
  bridge1-`_sync_market`-visible-in-bridge2 proof),
  `tests/forward/test_mstock_live_bus.py` (two runners = one API call/sweep).
* **Lesson:** per-runner market state must be limited to book state; anything
  market-defining (spots, chains, bars) is shared infrastructure.

## E-6 — Options P&L on synthetic chains is machinery validation, not edge — OPEN (standing caveat)

* **Setup:** every options run to date, `quote_source = synthetic:bs`.
* **Observation:** the option stack (selector → structure → exits → expiry
  settlement) is exercised end-to-end, but premiums come from Black-Scholes
  with flat IV — `docs/OPTIONS-BACKTEST-PRD.md` says so explicitly
  ("does not validate edge against [real premiums]").
* **Status:** open until live chain/quotes are wired (status doc P1.1) and,
  separately, historical chain snapshots are captured for research
  (architect review §3.4). Until then: report synthetic runs as plumbing
  checks with `quote_source` attached; make no edge claims from them.
* **What would close it:** (a) real LTP chains in forward tests; (b) an
  accumulating `option_chain_snapshots` store so backtests can price off
  recorded premiums/IV.

## E-7 — Forward-test state is memory-only — OPEN (known, scheduled)

* **Observation:** runner/portfolio state lives in process memory
  (`PortfolioManager` singletons; playbook registry file-backed only when
  `PLAYBOOKS_PATH` is set). A restart loses books and resurrects halted
  breakers — the persistence "V2" gap, prerequisite for Gunicorn.
* **Interim discipline:** treat long-running forward sessions as disposable
  evidence; export/record anything you intend to cite (the ledger and audit
  log live in the process).

---

## Appendix — numbers ledger

| Finding | Number | First recorded | Source |
|---|---|---|---|
| E-1 churn | −₹41,844 | 2026-09-16 | UNIFIED-TRADING-TASKS U0.1/C3, ARCHITECTURE-UNIFIED-TRADING §"Re-entry", execution_engine comment |
| E-3 dead spot | NIFTY @ ₹392, lot 75 | 2026-08 (D1) | tests/forward/test_synthetic_feed_scale.py header |
| E-5 overload | 2 runners → 2 chains | 2026-09-16 (U6.2) | UNIFIED-TRADING-TASKS U6.2 as-built, feed_registry module doc |
