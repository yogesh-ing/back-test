# Live Order Management — Complete Explanation

> **Status:** Lives on `origin/arena/01a0ccdb-back-test` (commits `988baa4` *feat(portfolio): live order management — per-position actions + Orders tab* and `3a7eb1d` *feat(portfolio): phase 3 order management — amend, aging alerts, bounded auto-retry*). **Not yet merged** into `arena/01a0c511-back-test` — merging it needs only 4 trivial test-file conflict resolutions.
>
> **Source docs:** `docs/PORTFOLIO-CENTER.md` (endpoints & semantics), `PROJECT-CONTEXT.md` (state summary).

---

## 1. What it is (one paragraph)

Live Order Management (LOM) is the operator's **manual steering wheel** on top of the automated strategy runners. Until LOM, the Command Center could only *display* what strategies were doing — you watched positions and P&L but could not touch anything between strategy decisions. LOM adds two things: an **Actions column on the Open Positions tab** (modify stop-loss, modify target, close 50%, close all) and a dedicated **Orders tab** (the engine's own order ledger with cancel, amend, aging alerts, slippage, and bounded auto-retry). It is the difference between a dashboard you can only look at and a trading console you can actually drive.

## 2. Where it lives — integrated in Portfolio, NOT a separate page

**It is integrated into the existing Portfolio Command Center**, as tabs — there is no separate app, route, or service:

```
Portfolio Command Center (existing page)
├── Runners / Buckets      (existing — unchanged)
├── Risk / Breakers        (existing — unchanged)
├── Open Positions  ←── ENHANCED: new "Actions" column + leg drill-down
└── Orders          ←── NEW TAB: order ledger, cancel/amend, aging, filters
```

| Component | File | Role |
|---|---|---|
| Backend API | `src/backtest/api/portfolio.py` (+131 then +35 lines) | `POST /api/portfolio/position/action`, `GET /api/portfolio/orders`, `POST /api/portfolio/orders/<coid>/cancel`, `POST /api/portfolio/orders/<coid>/modify` |
| Engine | `src/backtest/forward/portfolio_manager.py` (+302/+185) | Executes actions on the position book; manual levels checked **every bar and every mark move** |
| Runner layer | `src/backtest/forward/paper_runner.py` (+590/+496) | `OrderLedger`, manual-level enforcement, Phase-3 amend/aging/auto-retry machinery |
| Venue bridge | `src/backtest/forward/options_bridge.py` (+347), `live_gateway.py` (+132) | Routes live actions/cancels/amends to the broker venue first |
| UI | `web/static/js/components/position_actions.js` (new, 359 lines), `orders_tab.js` (new, 309+228 lines), `_portfolio_center.html`, `portfolio.js` | The two tabs; Orders rows poll at 3 s **only while the tab is visible** |
| Persistence | `state_store.py` (+47) | Manual SL/TP levels round-trip across restarts |

### The two tabs in detail

**Open Positions (enhanced).** One flat row per open position across all runners (an option structure is one net row with its legs listed underneath). Each row shows side, size, entry, mark, P&L, armed Target/Stop levels, net Δ/Θ for option structures — plus the new Actions column:

| Button | Action | Semantics |
|---|---|---|
| 🛑 SL | `modify_stop_loss` | Refused if it would fire immediately; "Clear level" disarms |
| 🎯 TP | `modify_target` | Same validation on the other side of the mark |
| ◐ 50% | `close_fraction` | Equity only — option structures close atomically, pinned to 100% |
| ✕ All | `close_all` | Full exit at market |

**Orders (new tab).** The engine's own `OrderLedger` — not a second bookkeeping layer. Rows: PENDING (with age + the only cancel button in the app), FILLED (requested vs fill price → slippage, adverse-positive per unit), REJECTED. Status filters, an "⏰ Aging only" filter, and a summary strip counting working + rejected orders.

## 3. How it works — the mechanics that make it safe

### Manual levels are hard, bar-level enforced
Manual SL/TP are **prices**, not percentages (share price, or net premium per unit for an option structure — for credit structures the stop sits *above* the mark and the target *below*; the engine derives the side). They are validated server-side against the live mark, then checked **on every bar and on every mark move and even on stress markdowns**. Consequence: a manual stop exits at market even if the strategy never trades again, hangs, or disagrees. The strategy's own exit policy still runs — **whichever fires first wins**.

### Live actions return `placed`, never a fake fill
On a `mode=live` runner, a close/cancel goes to the venue first. The API returns `status: "placed"` with a client order id — the UI says "order placed" instead of lying that the position is closed. A locally-cancelled order that still rests at the broker is exactly how a ghost position opens after the operator was told it was dead; LOM refuses to fabricate that outcome.

### Phase 3: amend, aging, bounded auto-retry

**Amend a working order** (`✎ Amend`, live orders only — quantity and/or limit price). The **venue is asked first**: a refusal leaves local state untouched and is shown verbatim with the modal still open. The client order id, original `requested_price` (slippage keeps measuring the *original* decision), and fill history survive — an amend is not a cancel-and-replace.

**Order aging alerts** — a working order past **60 s** is `warn`, past **5 min** `alert`. The row is tinted and badged (⏰/🚨), the summary strip counts bands, and the engine writes **one audit entry per band per order** (an alert that repeats every tick is noise an operator learns to ignore).

**Auto-retry on rejection** — opt-in per runner (`RunnerConfig.retry_policy`), **off by default**. Retryable refusals are re-sent on later bars up to `max_attempts`, no faster than `cooldown_s`, at most once per bar; every attempt is its own audited ledger row carrying `retry_of`/`retry_attempt` (a lineage, not duplicate mystery orders). It is deliberately narrow:

- **Only paper refusals are retried.** A live placement *error* is never auto-resent — a timeout can mean "accepted, acknowledgment lost", and re-sending is how one intent becomes two live orders. Those are blocked with `RETRY_REFUSED` + a "reconcile before re-sending" note in the signal log.
- **Finite budget.** `max_attempts=2` = at most 3 sends. A spent budget is remembered (`order_retries.blocked`) so the next bar's identical signal cannot quietly re-open it (transition signals keep firing while their condition holds).
- **Block lifts two ways:** the strategy stops asking for that action (a genuinely new decision), or `rearm_after_s` (default 300 s) expires — starting a new episode at a bounded rate instead of giving up forever on a venue outage or hammering it.
- `get_state()["order_retries"]` reports `pending / blocked / raised / recovered / exhausted`.

## 4. How it helps / supports trading

| Without LOM | With LOM |
|---|---|
| Strategy hangs or a news event invalidates the thesis → you watch the position bleed until the strategy itself exits | You hit ✕ All and flatten at market, or arm a manual 🛑 SL that the engine enforces on the very next tick |
| Stop too wide / target too close → wait for the strategy config, restart the runner | 🛑 / 🎯 buttons re-arm levels in place, validated against the live mark |
| One leg of an option structure goes bad → no way to act | Structures close atomically; net row + legs shown together |
| Order stuck PENDING at the broker → invisible | Orders tab shows age with warn(60 s)/alert(5 min) bands; cancel or amend at the venue |
| Rejected order (margin blip, circuit filter) → strategy may silently stop trading | Bounded auto-retry (opt-in) recovers safe refusals with a full audit lineage |
| "Did I get a fair fill?" → guesswork | FILLED rows show requested vs fill → per-unit adverse slippage, measurable |

**In one line:** LOM turns the Command Center from a *monitor* into a *console* — the automation keeps trading, but a human can always intervene instantly on positions (risk control) and see/manage every order in flight (execution quality), with venue-first semantics so the UI never shows state the broker doesn't agree with.

## 5. Known limitations (documented by the authors)

- **Ledger orders are not persisted** — the Orders tab starts empty after a restart and fills as new orders route through. (Positions/levels *do* persist.)
- `mode=live` buckets: tags/UI wired, but **live broker fills are still open** (findings F-12 — `BrokerFillProvider` + `MStockLiveFeed` exist; forward-engine wiring and `poll_fill` in the broker ABC remain). Until then, live actions are placement-first (`placed` status) but fill confirmation is not yet polled from the broker.
- ◐ 50% is equity-only by design (option structures close atomically).

## 6. Merge impact on our branch

- ~6,460 added lines across 2 commits; UI work is confined to the Portfolio Center (positions tab + new Orders tab); **no overlap with our uncommitted Dhan/feed-quality work** (`paper_runner.py` hunks don't collide with ours at lines 633/1225).
- Only genuine merge conflicts: **4 test files** where 01a0ccdb committed its own take on fixes we already have (`test_api_strategies.py`, `test_options_forward_mtm.py`, `test_state_persistence.py`, `test_options_gap_remediation.py`) — all trivial assertion-strength/wording differences; resolve in favor of our (more tolerant) versions.
- Test mass on our side of the merge: `tests/test_position_management.py` (909+636 lines), `tests/js/test_orders_tab.mjs`, `tests/js/test_position_actions.mjs` (Node harnesses, skipped when node absent).
