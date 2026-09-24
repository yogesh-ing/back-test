# Open Items Tracker

**How to use:** tick `[x]` when done. "Who: You" = needs your action (credentials/money/account). "Who: Agent" = I can do it on request. Everything stays on this page — nothing held in your head.

**Last updated:** 2026-09-24 · tests/forward/ 244 pass (was 31 fail) · gap-remediation 40 pass (was 12 fail)

> **Expiry-day pricing fix (2026-09-24):** `SyntheticChainGenerator.MIN_PRICING_YEARS`
> (half a trading day) in `price_contract` — near/post-expiry synthetic options keep a
> real premium smile instead of collapsing to ₹0, which used to trip the fail-closed
> ltp=0 guard and block all entries on monthly expiry days. 31+12 date-brittle test
> failures fixed, 0 introduced.

---

## 🔥 Do today (market is open)

- [ ] **1. Real-session smoke test (T9.5)** — *~30 min — Who: You + Agent*
  Start app → mStock login + TOTP → spawn ONE option runner `source: mstock, mode: paper` → confirm quote label is NOT `synthetic:bs`.
  Command: `cd src && python -m backtest.web.app --port 5000`

- [ ] **2. Start chain snapshot capture** — *~5 min to start, runs all day — Who: You*
  Command: `PYTHONPATH=src python scripts/snapshot_option_chains.py --interval-seconds 300`
  Win: today's option chain data starts accruing for future backtests.

- [ ] **3. Intraday strategy paper run** — *~1 hr of market time — Who: Agent (on request)*
  Spawn runner on the new strategy, watch it trade real 1-hour bars until close.

- [ ] **4. Record findings** — *~10 min after close — Who: Agent*
  Log what worked/broke in `docs/OPTIONS-FORWARD-TEST-EXPERIMENT.md`.

---

## 🧠 HFT strategy — what's real (opinion)

**True HFT is not possible here.** No tick feed, no sub-second latency, no colocation.
**What IS possible: high-turnover intraday on 1-hour bars** (shortest real feed).
Plan: new strategy in `plugins/strategies/` from the option template + tighter
exit knobs (`reenter: true`, `max_reentries_per_day: 4`). Est: half a day of
agent work. Blocked only by item 1 passing.

---

## 🟠 This week

- [ ] **5. HFT-style strategy built + conformance-tested** — *0.5 day — Who: Agent*
- [ ] **6. CI workflow push** (`.github/workflows/ci.yml` needs your account — I can't push workflows) — *15 min — Who: You*
- [ ] **7. Portfolio-page option trade rows (UI polish)** — *2 hrs — Who: Agent*
- [ ] **8. Consultant answers on 6 open questions** — `CONSULTANT_RESPONSE.md` §0.4 — *Who: You*

## 🟢 Later (no date)

- [x] 9. Options-tab hard delete — ✅ DONE early (2026-09-22): GAP-3 resolved as REMOVE — /options page, options.js, Manual Options Book tab/banner deleted; JSON endpoints + book singleton kept (portfolio merge + emergency flatten). Review date no longer needed.
- [ ] 10. Risk envelope V2 (BS+IV+SPAN) — after consultant answers
- [ ] 11. Multi-leg structures (straddles/condors) — Phase B, don't bundle
- [ ] 12. Sizing presets + richer metrics (Sortino, profit factor) — vectorized path

---

**Wins today so far:** ✅ Gap validation done — P1.1 live chain wiring, F-12 equity fills, P2.4 persistence, fail-closed trader all verified in code + tests. One date-brittle test fixed.
