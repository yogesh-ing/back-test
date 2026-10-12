# PRD: Multi-Broker Capital Allocation & Segmented Trading

> **Status:** IMPLEMENTED — Phases A–D shipped 2026-09-28 on `arena/01a0e42c-back-test`
> (sessions v2, segments & routing, per-broker execution, cross-broker risk).
> Remaining: real-credential smoke test, open questions 3–7 (§8).
> **Update 2026-10-01:** spawn-time broker choice shipped — the Add Instance
> modal has a dedicated **Broker** dropdown wired to `execution_broker`
> (§4's "must carry a broker field" for spawns), and `dhan` is unlocked as a
> runner data source (`SOURCE_TAGS` + migration 016 widening
> `ck_portfolios_source`; the repo's multi-broker schema migration is
> **008** — `008_multi_broker_segments.sql`). Schema drift found while
> applying 016: the live DB was
> create_all-built + stamped, missing 008's columns — reconciled and the ORM
> mirror completed (portfolios.segment/execution_broker, positions/orders.broker).
> **Author:** drafted 2026-09-27 from the working session on `arena/01a0c511-back-test`.
> **Replaces in spirit:** the current "one active broker session at a time" rule in `BrokerSessionManager` (which was a deliberate V1 simplification, not a product goal).

---

## 0. Problem Statement

Actual trading capital is **spread across 3–4 brokers**, and each broker owns a
**different segment of trading**:

| Broker (example) | Segment | Notes |
|---|---|---|
| mStock | Index options (NIFTY/BANKNIFTY spreads, straddles) | current data source too |
| Dhan | Equity intraday + swing | current second login |
| Broker C | Stock options | future |
| Broker D | Swing/positional equity | future |

The platform today authenticates **exactly one broker at a time**
(`switch_broker` drops the previous session). That forces:

- sequential logins whenever a different segment must trade,
- one shared data-source decision for the whole app,
- no honest capital picture across brokers,
- risk limits computed against a single broker's book while real exposure
  lives at four.

**Goal:** authenticate **all brokers simultaneously**, route each runner's
*orders* and *data* to the broker assigned to its segment, and roll up risk,
margin, and P&L across all of them.

**Non-goal (V1):** cross-broker order-routing optimization ("smart order
routing"), cross-broker netting, or arbitrage between brokers. One runner =
one broker, fixed at creation.

---

## 1. Design Principles (carried from the existing architecture)

1. **Strategies never see brokers** (C2 data-ownership rule, already enforced
   by the execution engine). The broker is a property of the *runner/engine*,
   never of the signal.
2. **Fail-closed everywhere.** A runner whose broker session is expired
   pauses new entries; it never falls back to another broker's session.
3. **Tokens never leave the backend.** Multi-session storage is per-broker
   and in-memory, same as today.
4. **One registry, N instances.** The `_BROKER_REGISTRY` (name → factory)
   stays; what changes is that the manager holds a *map* of instances
   instead of one active instance.
5. **Data ≠ orders.** The DATA broker (one at a time is fine) and the
   EXECUTION broker (per-runner) are separate decisions that must not be
   conflated — today they accidentally are.

---

## 2. Current State (what exists to build on)

| Component | Today | Multi-broker gap |
|---|---|---|
| `BrokerAuthBase` (brokers/base.py) | two-step auth contract | ✅ broker-agnostic, reusable as-is |
| `BrokerOrderBase` (brokers/base.py) | place/modify/cancel/book/margin contract | ✅ reusable; only mStock implements it today |
| `BrokerSessionManager` | holds ONE active broker; `switch_broker()` drops the other | ❌ core change target |
| `_BROKER_REGISTRY` | name → lazy factory | ✅ stays |
| `remember_session` | saves ONE session (token+expiry+broker) | ❌ must become per-broker |
| Expiry monitor thread | polls the single session | must poll N sessions |
| `/api/broker/list` | static list | ✅ stays |
| `/api/broker/select` | switches the one session | repurposed: "activate for UI" only |
| Feed routing (`portfolio_manager`) | `mstock_feed` + `dhan_feed` both run; runner's `source` picks the feed | ✅ already multi-broker for DATA |
| Order path (F-12 gap) | `BrokerFillProvider`/`poll_fill` not yet wired in the forward engine | ⚠️ the natural moment to add per-broker execution |
| `RunnerConfig.source` | data-source tag (synthetic/replay/mstock/dhan) | needs a sibling: `execution_broker` |
| Bucket model (paper/live) | bucket-level breakers | buckets gain a broker dimension |
| Intelligence/alerts | portfolio-wide | ✅ broker-agnostic; needs broker dimension in reports |
| Playbooks | spawn runners via `to_runner_config()` | must carry a broker/segment field |
| UI broker modal | one session at a time, "Login" replaces | becomes a per-broker status card grid |

---

## 3. Target Architecture

```
                    ┌───────────────────────────────┐
                    │    BrokerSessionManager v2    │
                    │   sessions: {name → Session}  │
                    │  (all brokers live at once)   │
                    └──────────────┬────────────────┘
             ┌─────────────────────┼─────────────────────┐
             ▼                     ▼                     ▼
       ┌───────────┐        ┌───────────┐         ┌───────────┐
       │ mstock    │        │ dhan      │         │ brokerC   │
       │ auth+order│        │ auth+order│         │ auth+order│
       └─────┬─────┘        └─────┬─────┘         └───────────┘
             │                    │
   DATA (pick one active)         │
   ════════════════════           │  EXECUTION (per-runner)
   mstock_feed ──┐                │  ═══════════════════════
   dhan_feed  ───┤ bars           │  runner(seg=options) → mstock
                 ▼                │  runner(seg=equity)  → dhan
          PortfolioManager ───────┘  runner(seg=swing)   → brokerC
          (tick loop unchanged; one bar stream, many books)
```

### 3.1 Segment model (new concept)

A **Segment** is the user's own partition of capital and mandate:

```yaml
# config/segments.yaml (NEW file — implementation decision 2026-09:
# config/brokers.yaml is the fee-model config and stays untouched;
# segments + data-broker routing live in their own file, overridable
# via the SEGMENTS_CONFIG_PATH env var)
segments:
  options_index:
    display_name: "Index Options"
    broker: mstock
    mode: live            # paper | live
    allocated_capital: 1200000
  equity_intraday:
    display_name: "Equity Intraday"
    broker: dhan
    mode: live
    allocated_capital: 800000
  stock_options:
    display_name: "Stock Options"
    broker: broker_c
    mode: paper           # start paper, flip later
    allocated_capital: 400000
  swing:
    display_name: "Swing / Positional"
    broker: broker_d
    mode: paper
    allocated_capital: 600000

data:
  primary: mstock         # the ONE broker feeding bars to all runners
  fallback: dhan          # optional, see §6.3
```

- A segment maps 1:1 to a **bucket-like risk envelope** (per-segment daily
  loss, drawdown, deployed-capital cap) — reusing the existing per-bucket
  breaker machinery rather than inventing a new one.
- Runners are created *into* a segment (or with an explicit
  `execution_broker`), never directly onto a broker.

### 3.2 `BrokerSessionManager` v2 — session map

Replace "single active broker" with "session per broker, all can be live":

```python
class BrokerSessionManager:
    # v2
    _sessions: dict[str, BrokerAuthBase]          # name → live instance
    _ui_active: str | None                        # which broker the UI edits

    def login(self, broker_name, username, password) -> ...   # per-broker
    def verify_totp(self, broker_name, code) -> ...
    def logout(self, broker_name) -> ...
    def get_status(self, broker_name: str | None = None)      # one or all
    def is_authenticated(self, broker_name) -> bool
    def get_session_token(self, broker_name) -> str | None    # per-broker
    # DEPRECATED but kept one release:
    def switch_broker(name)  # = set_ui_active(name) — no longer drops sessions
```

Invariants:

- Logging into broker B **must not** touch broker A's session (the current
  `switch_broker` behaviour becomes a bug and is removed).
- The expiry monitor iterates all sessions; flags become
  `(broker, kind)` notifications.
- `remember_session` storage becomes a per-broker file/map
  (`{"mstock": {...}, "dhan": {...}}`), same opt-in toggle.

### 3.3 Execution routing (the real new machinery)

With F-12 (live fills) still unwired, this PRD defines the order path in the
same stroke so it is built once, correctly:

- `RunnerConfig` gains `execution_broker: str | None` (default: segment's
  broker; `None` + `mode=paper` → paper broker as today).
- The engine resolves orders through an **ExecutionRouter**:

```
ExecutionRouter.order_for(runner) -> BrokerOrderBase | PaperBroker
```

- Guarantees (mirror of the existing LOM rules, extended):
  - session expired for the runner's broker → runner pauses entries
    (`RETRY_REFUSED` semantics; no silent rerouting),
  - every order row in the Orders tab gains a `broker` column,
  - fill polling (`poll_fill`) is per-broker (mStock order book vs Dhan
    order book endpoints),
  - a manual close from the Positions tab goes to the **position's** broker,
    discovered from the position, not from UI state.

### 3.4 Data routing (already mostly built — formalize)

- Keep `data.primary` = exactly one broker feeding bars (the current
  mstock_feed/dhan_feed pattern generalizes to the registry).
- Runner `source` keeps deciding which feed a runner consumes — unchanged.
- §6.3 covers optional failover.

### 3.5 Risk, capital & intelligence across brokers

- Each segment contributes its own breaker envelope (reuse per-bucket
  breakers; segment ≈ bucket key).
- Global breakers (daily loss, drawdown) evaluate against the **sum of all
  segments** — this is the honest cross-broker risk the current design
  cannot see.
- Portfolio Intelligence (`/monitor`, Greeks, concentration) gains a
  `broker`/`segment` dimension in breakdowns; concentration by *broker*
  matters as much as by underlying (broker failure = operational risk).
- Orders/Positions tabs show per-broker sub-totals.

---

## 4. API Changes

| Endpoint | Change |
|---|---|
| `GET /api/broker/list` | gains per-broker live status (`authenticated/expired/...`) |
| `POST /api/broker/login` | takes explicit `broker` field (already does); logs into that broker **without dropping others** |
| `POST /api/broker/select` | repurposed to "set UI-active broker"; returns warning that it no longer logs the other out |
| `GET /api/broker/status` | returns map `{mstock: {...}, dhan: {...}}` |
| `POST /api/segments` | CRUD for segments (V1: file-backed config + validate) |
| `POST /api/portfolio/runner/create` | accepts `segment` or `execution_broker` |
| `GET /api/portfolio/summary` | runners, positions, orders carry `broker`/`segment`; per-broker capital rollups |
| `GET /api/portfolio/orders` | rows gain `broker` |

Backwards compatibility: every endpoint must keep working when only ONE
broker is logged in (today's mental model).

---

## 5. UI Changes (broker modal → broker board)

- The single modal becomes a **Broker Board**: one status card per registered
  broker (name, status pill, expires-at, segment(s) served, [Login]/[Logout]
  per card). Multiple cards can be Authenticated at once.
- The "Broker" dropdown inside a login card pre-fills that card only.
- Runner creation form gains a **Segment** select (shows broker + mode +
  allocated capital); Playbook spawn passes its segment through.
- Header indicator: `● mStock ● Dhan ○ BrokerC` style status strip (replaces
  the single broker pill).
- Positions/Orders tabs gain a Broker column + per-broker filter.

---

## 6. Risks, Edge Cases & Controls

### 6.1 Token/session lifetime asymmetry
mStock and Dhan sessions expire differently (mStock daily token; Dhan
accessToken with its own expiry). The expiry monitor already handles
per-session lifecycles; segment runners must surface *which* broker's expiry
paused them (toast: "Dhan session expired — equity runner paused").

### 6.2 Simultaneous exposure honesty
The global breaker must see the SUM. Risk: one broker's book hiding behind
another's headroom. Control: cross-broker aggregate evaluation runs on every
tick (the intelligence sweep already runs per-tick; extend it).

### 6.3 Data failover (explicitly V2, gated)
Auto-switching the data feed to `fallback` on primary outage is tempting but
dangerous (different chains, different bars → phantom signals). V1: feed
quality alerts fire (already built); the operator switches manually.
V2 (opt-in): failover **pauses entries on all live runners first**, then
switches — never mid-bar.

### 6.4 Per-broker rate limits
Feed polling + order-book polling per broker multiplies API calls. Controls:
existing 3s-visible-tab polling, per-broker poll budgets in
`config/brokers.yaml`, feed-quality monitor already tracks error rates per
broker.

### 6.5 Order-book reconciliation per broker
LOM's venue-first rule per broker: cancel/amend asks the right venue; a
restart must reconcile open orders at *each* broker before resuming runners
(V1: manual reconcile button per broker card).

### 6.6 Credential hygiene
Per-broker login still just-in-time; never stored; remember-session becomes
per-broker opt-in; the `DHAN_API_KEY` pattern generalizes to per-broker
API-key env vars (`<BROKER>_API_KEY`).

### 6.7 Migration path
Everything must degrade to exactly today's behaviour with one broker logged
in. No flag day: old `switch_broker` calls map to `set_ui_active`.

---

## 7. Delivery Plan (4 phases, each independently shippable)

### Phase A — Sessions v2 (the foundation) ~2–3 days
- Session map in `BrokerSessionManager` (login/verify/status/logout per
  broker; monitor loops all; remember-session per broker).
- API: `/api/broker/status` map; `login` no longer drops others;
  `/select` = UI-active only.
- UI: Broker Board cards (login/logout per broker, status strip).
- Tests: multi-session lifecycle, cross-broker isolation (login A ≠ logout
  B), expiry monitor per broker, remember-session per broker.
- **Demo:** mStock + Dhan both Authenticated simultaneously.

### Phase B — Segments & runner routing ~2–3 days
- `config/segments.yaml` segments + `POST /api/segments` validation.
- `RunnerConfig.execution_broker` + segment-aware runner creation (API +
  UI select). Paper mode ignores broker (paper broker everywhere).
- Summary/Orders/Positions carry broker/segment.
- Tests: routing resolution, paper/live modes, unknown-broker refusal.

### Phase C — Execution & fills per broker (rides F-12) ~3–4 days
- `ExecutionRouter` + `poll_fill` per broker; Dhan implements
  `BrokerOrderBase` (order book, place, cancel, margin) — the contract is
  already defined, Dhan currently only has auth + data.
- LOM venue-first semantics per broker; per-broker reconcile button.
- Tests: mock-venue order lifecycle per broker, LOM amend/cancel routing.

### Phase D — Cross-broker risk & intelligence ~2 days
- Global breakers over summed segment equity; per-segment breaker envelopes
  in the Risk page; broker dimension in intelligence breakdowns and
  concentration (by-broker exposure alert).
- Tests: aggregate breaker math, per-segment breach isolation.

**Total: ~9–12 working days.** Phases A and B alone deliver the user-visible
goal (both brokers logged in, runners assigned); C is needed only when live
ordering through the platform goes real; D closes the honest-risk loop.

---

## 8. Open Questions (for the architecture discussion)

1. ~~**Segment ↔ bucket mapping:**~~ **RESOLVED (2026-09-28, as recommended):**
   segment *is* the bucket for live mode (`segment:<name>` bucket keys); the
   paper bucket stays shared. Implemented in `portfolio_manager.py`.
2. ~~**Who owns the DATA role long-term?**~~ **RESOLVED for V1 (2026-09-28, as
   recommended):** single data broker via `config/segments.yaml` `data.primary`
   (with a declared-but-inert `data.fallback` field); per-segment data stays V2+.
3. **Broker C/D adapters:** which brokers next (Zerodha? Upstox?), and do we
   buy/borrow an SDK or hand-roll like mStock/Dhan? The registry makes each
   a module + entry, but order-contract work is real.
4. **Same-broker multiple accounts?** (Sub-broker codes / multiple client
   IDs at one broker.) V1 assumes one account per broker.
5. **Compliance:** distributing strategy execution across brokers is the
   user's own capital arrangement — the platform just needs per-broker
   audit trails (the order ledger is already per-venue-tagged).
6. **Failure drill:** what happens when a broker API is down for a day while
   its segment's runners hold open positions? (Need a per-broker "degraded"
   state in the UI + alert, distinct from session-expired.)
7. **Margin fidelity:** margin is per-broker (`calculate_order_margin` per
   venue) — does the user want a consolidated margin dashboard V1, or
   per-broker cards only?

---

## 9. Success Criteria

- [x] mStock **and** Dhan (and later C/D) simultaneously Authenticated;
      either can trade without a re-login dance.
      *(2026-09-28: `BrokerSessionManager` session map + Broker Board UI;
      `tests/brokers/test_multi_session.py`. Real-credential smoke still pending.)*
- [x] Each runner's orders route to its segment's broker; the Orders tab
      shows which.
      *(2026-09-28: `ExecutionRouter.order_for` + broker/segment columns;
      `tests/brokers/test_segments_routing.py`.)*
- [x] A runner whose broker session expires pauses entries and raises an
      alert naming the broker — no cross-broker fallback, ever.
      *(2026-09-28: `BrokerSessionExpired` fail-closed path, runner pauses entries.)*
- [x] Global risk breakers evaluate the cross-broker sum; per-segment
      breakers fire independently.
      *(2026-09-28: segment-keyed bucket breakers + global SUM;
      `tests/test_cross_broker_risk.py`.)*
- [x] One broker remains the single data source; feed-quality alerts keep
      working unchanged.
      *(2026-09-28: `config/segments.yaml` `data.primary`; feed path untouched.)*
- [x] With exactly one broker logged in, every screen behaves exactly as
      today (no regression for the current single-broker flow).
      *(2026-09-28: full suite 2922 passed / 0 failed incl. all legacy
      single-broker API-shape tests.)*
