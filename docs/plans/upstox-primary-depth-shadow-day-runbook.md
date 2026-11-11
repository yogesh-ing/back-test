# Upstox-primary shadow day — runbook (Phase 4)

**The question this session answers:** does 30-level NSE depth improve decisions
over mStock top-5? The answer is the **scorecard S1–S7 — counts with their
denominators** — not a P&L diff. At n=1 session a P&L difference between two
marked books is noise, not evidence (plan §5, review item 4).

Status: the two pre-open fixes are in —
(1) the `empty_ladder` vs `no book` vocabulary drift is gone
(`count_impossible_ladders.py` speaks the cadence tool's words;
scorecard S1's coverage := `two_sided / frames`), and
(2) every criterion's computing command is fixed below and implemented in
`tools/out/score_shadow_day.py` (commit `b256d15`). If a rule must change,
change it **before the bell** and re-pin it here — the scorecard cannot be
assembled post-hoc.

---

## 1. Before the bell

| Setting | Value | Why |
|---|---|---|
| `OPTION_DEPTH_SOURCE` | `upstox` | the selector (§7 Q3). `config.source` must be **unset** — an explicit config value silently wins and the selector is inert. Verify at spawn: the runner's label says `upstox`. |
| `OPTION_EXIT_COLLAR` | `shadow` | gate **D** (collar-bounded protective exits). Shadow journals, never blocks. |
| `OPTION_CROSS_BROKER_SANITY` | `shadow` | gate **F** (L1 corroboration at the fill seam). The witness votes; enforcement would-be refusals are the S4 population. |
| `OPTION_QUOTE_SANITY`, `OPTION_PARITY_SANITY` | as deployed — **record the values in the session plan** | their enforce-mode refusals are S3's population; the denominator needs to know. |
| `ENABLE_FEED` / `ENABLE_FEED_DIR` | `true` / `logs/feed_depth` | every criterion reads these tapes. |
| execution venue | **mStock** (unchanged) | B7: orders, margin and fill records stay mStock; the primary only marks the book. |

- Both feeds up: **Upstox = primary** (marks + depth), **mStock = witness**.
  A missing witness costs one log line (`FEED_LOSS_WITNESS`), never a halt.
  Losing the primary fires the critical `DATA_FEED_STALE` alert — that episode
  is S6 evidence, not a reason to switch feeds mid-session.
- Copy this runbook into the session plan with the table in §2 filled in.

## 2. The scorecard — criteria and computing commands

One command scores the day (each criterion below is its own section of the
same output):

```
python tools/out/score_shadow_day.py <YYYY-MM-DD> \
    --primary upstox --witness mstock --collar-log <session app log>
```

| # | Criterion | Pass | Computing command (fixed before the bell) |
|---|---|---|---|
| S1 | Frames served with a two-sided book | ≥ the mStock baseline of the **same session** | scorecard S1 (or `python tools/out/measure_feed_cadence.py <day>`). Coverage := `two_sided / frames` in the shared vocabulary (two_sided / one_sided / no_book). |
| S2 | Structurally impossible ladders published by the primary | 0 | scorecard S2 (or `python tools/out/count_impossible_ladders.py <day> --broker upstox`). The structural proof: bids step down, asks step up, book uncrossed. |
| S3 | Fill-seam refusals attributable to the *venue*, not to an empty book | 0 | scorecard S3: `sanity_flag` rows with `mode=enforce`; a row carrying **any** level is venue-attributable, a row with nothing is an honest empty-book refusal (allowed). |
| S4 | L1 vetoes by the mStock witness, split "venue basis" vs "garbage" | every veto explained; unexplained = 0 | scorecard S4: `sanity_flag` rows with `reason=cross_broker_l1` (any mode — they count what enforcement *would* have refused). Split by proof, **no threshold**: *garbage* = the row's own ladder is structurally impossible; *venue basis* = the print is self-consistent and the venues genuinely differ; *unexplained* = nothing to classify. |
| S5 | Collar decisions journaling a peer mark | 100 % coverage | scorecard S5 with `--collar-log`: `peer_mark` trip rows / `EXIT_COLLAR_*` log lines = 100 % (the item-4 unit test pins one row per evaluated leg; the log lines are the independent denominator). |
| S6 | Primary-feed-loss alert fires on socket drop; witness-only loss does **not** | fires / does not fire | `PYTHONPATH=src python -m pytest tests/test_feed_loss_alert.py` (green) **plus** the day's live evidence: `grep` the alert store for `PRIMARY feed loss` and the log for `FEED_LOSS_WITNESS`; witness-only loss must show the log line and no alert. |
| S7 | Fills that would have priced differently on the other venue | listed with both marks; **no threshold at n=1** | scorecard S7: `peer_mark` rows where the peer's *same-side* level (ask for a BUY cover, bid for a SELL) differs from `fill_price` — any difference counts. Listed with our fill, our mark and the peer's level. |

Every criterion is reported as **count + denominator**. A verdict is only
available for a quantity measured on both sides.

## 3. During the session

- **Flip nothing mid-session.** The selector reads at spawn; a collar/sanity
  mode change mid-session corrupts the denominators. The settings in §1 are
  the session.
- The primary feed dropping is data, not a disaster: the alert fires (S6),
  the witness keeps one log line per episode, and trading continues on the
  same rule the whole day is testing.
- Intra-day numbers are **provisional** (the §1b partials explicitly were).

## 4. After the close

1. Re-run the scorecard on the **full session** tape
   (`score_shadow_day.py`, plus the two tape tools standalone for their
   native reports). Partial numbers stay provisional.
2. Report every criterion as count + denominator, with S7's listing attached.
3. **Enforce only on S1–S6 green.** S7 is a listing, never a gate. Green
   S1–S6 is the only condition under which `OPTION_DEPTH_SOURCE=upstox`
   moves from shadow to serving a live book — mirroring how B, D and F were
   each introduced. A red criterion means: stop, fix, and take the next
   session as the scorecard day. Never tune a threshold to make a number
   green (junk-ask incident §6).

## 5. Evidence locations

- Tapes: `logs/feed_depth/feed_{mstock,upstox,shadow}_<day>.jsonl` —
  packets, `sanity_flag`, `price_used`, `shadow_diff`, `peer_mark` rows.
- Feed quality + loss episodes: `data/feed_quality.log` (`FEED_LOSS_WITNESS`
  lines) and the alert store (`DATA_FEED_STALE`, subject `broker:symbol`).
- The split rules (S3/S4) and the S7 comparison are pinned in
  `tools/out/score_shadow_day.py`'s header — the single source of the
  definitions this runbook summarizes.
