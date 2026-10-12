# PRD-003 — Migration 014: order-lifecycle columns, and why they are not being added

**Status:** decided. **Date:** 2026-09-29. **Scope:** Cross-Broker Performance
Analytics & Execution Quality (V1).

## What the PRD asked for

PRD-003 lists, under "Database Changes":

> Migration 014 — add to `orders`: `requested_price`, `fill_price`,
> `request_ts`, `fill_ts`, `rejection_reason`. Backfill from the existing
> `order_events` ledger where possible.

## What the schema already has

Every one of those five fields already exists, under a different name and on a
different table, and the execution-quality service reads them from there. A
second copy on `orders` would be a denormalisation nothing reads, and the one
thing that would break is `tests/test_db_schema.py`, which asserts the
SQLAlchemy models match the 001 + 002 baseline.

| PRD-003 asks for | Already exists as | Table | Written by |
| --- | --- | --- | --- |
| `requested_price` | `average_fill_price` | `orders` (migration 001) | `OrderLedger._record_fill` |
| `fill_price` | `average_fill_price` | `orders` | same |
| `request_ts` | `created_ts` | `orders` | `OrderLedger.submit` |
| `fill_ts` | `updated_ts` / `filled_ts` | `orders` | `OrderLedger._record_fill` |
| `rejection_reason` | `reject_reason` | `orders` | `OrderLedger._record_reject` |

Migration 008 already added the broker identity columns the cross-broker work
needs (`orders.broker`, `orders.broker_order_id`); nothing in PRD-003 needs a
broker column re-added.

## Why the `orders` row is not the right source for execution quality

`orders` holds the *current* state of an order, and the execution-quality
metrics need the *lifecycle*:

- **Slippage is direction-aware.** Adverse slippage is
  `(fill − requested)` for a buy and `(requested − fill)` for a sell. Both
  prices exist on the `orders` row, but only for orders that reached a
  terminal state; the `order_events` ledger holds every transition, so a
  partially-filled-then-cancelled order contributes its partial fill instead
  of being counted as a rejection.
- **Fill time** is `fill_ts − request_ts`. Both timestamps are on the row,
  but a rejected order's `updated_ts` is the rejection stamp, not a fill —
  averaging the difference would silently mix rejections into fill latency.
- **Order aging** needs unresolved orders, which the `orders` table carries as
  `PENDING` rows only while the process is alive. The ledger's session-scoped
  event stream is the durable record of what actually happened.
- **Rejection reasons** come from the broker adapter's error string at
  rejection time. That string is written once, into the ledger, by
  `OrderLedger._record_reject`; nothing reconstructs it later from the row.

## What was done instead

`CrossBrokerAnalyticsService.get_execution_quality()` reads the
`OrderLedger` event stream directly — the same source the Orders tab and the
risk page's broker rollup already use — and derives:

- direction-aware slippage in bps, with the side in the sample;
- fill rate over *resolved* orders (`filled + rejected + cancelled`, pending
  excluded — see the note below);
- fill time from the request→fill event gap only;
- rejection rate and reason histogram;
- stale-order aging over orders still unresolved past the aging threshold.

Every response carries a `data_quality` block, and
`data_quality.session_scoped` is `true`: the ledger lives for the process
lifetime, so these numbers describe this session, not all history. The UI
surfaces that sentence verbatim rather than hiding it. The same block reports
`ledger_total` and `scanned` so a reader can see the sample they are judging.

**Fill-rate semantics** (a decision, not an accident): the denominator is
resolved orders, not all orders. A pending order has not yet failed, so
counting it against the fill rate would make a broker look worse simply
because the session ended mid-flight. Pending orders are not dropped, though —
they are exactly the population the stale-order aging metric reports on, so
nothing is hidden by the choice.

## Consequence for the migration graph

No new revision is added. `head` remains whatever it was before this work, and
the existing `tests/db/test_migrations_009_013.py` chain test is unchanged. If
a later PRD needs durable (restart-surviving) order history, the right move is
a dedicated `order_events` TABLE with a backfill from the `orders` snapshot —
a separate change with its own cost, not a sixth alias column on `orders`.
