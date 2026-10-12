# Archived Docs

Historical documents kept for provenance — **not** current truth. For live
status see `docs/OPEN-ITEMS-TRACKER.md`; for architecture see
`docs/ARCHITECTURE.md` and `docs/ARCHITECTURE-UNIFIED-TRADING.md`.

## Shipped epics — task logs (all work landed)

| Document | Epic | Where the truth lives now |
|---|---|---|
| `UNIFIED-TRADING-TASKS.md` | Playbooks / unified trading (P0–P6, 23 tasks) | `docs/ARCHITECTURE-UNIFIED-TRADING.md`, the code |
| `OPTIONS-BACKTEST-TASKS.md` | Options backtest engine, Phase A | `docs/OPTIONS-BACKTEST-PRD.md`, the code |
| `ANALYTICS-TAB-GAPS.md` | `/analytics` quant review — 🔴/🟠 fixes landed 2026-09-28 | `src/backtest/api/analytics_service.py`, `tests/test_analytics_math.py` |
| `REFACTOR-PORTFOLIO-LIVE-PAPER-SEPARATION.md` | Live/Paper bucket separation (✅ implemented) | `docs/PORTFOLIO-CENTER.md` |
| `options-PRD-Task.md` | Options paper/live epic (self-marked HISTORICAL) | `docs/OPTIONS-PAPER-LIVE.md` |
| `Gap-Analysis-Remediation-PRD.md` | Gap remediation epic — shipped | `docs/ARCHITECTURE-UNIFIED-TRADING.md` |
| `BACKLOG.md` | Post-backtester backlog (optimization engine, multi-symbol… shipped via PR 28/31) | `docs/OPEN-ITEMS-TRACKER.md` |

## Merged into other docs (content preserved, file retired)

| Document | Absorbed into |
|---|---|
| `STRATEGIES.md` | `docs/STRATEGY-AUTHORING.md` §6 — catalog, registry API, engine guarantees (its "class-level stop_loss is enforced" claim was WRONG — only quick_screen honours it; the corrected warning lives in §2/§6) |
| `ADDING-NEW.md` | Recipes distributed to their subsystems: data source → `docs/DATA-SOURCES.md`, DB table → `docs/DATABASE.md`, API endpoint → `docs/ARCHITECTURE.md`, UI page → `docs/WEB-UI.md`; strategy flow was already superseded by `docs/STRATEGY-AUTHORING.md` |
| `ORDER-MANAGEMENT-EXPLAINED.md` | `docs/PORTFOLIO-CENTER.md` LOM section — the plain-English "why" table and key-file map (its F-12-closed status also fixed the stale "live fills open" claims there) |

## Status snapshots & audits (point-in-time)

| Document | What it was | Superseded by |
|---|---|---|
| `STATUS-AND-NEXT-STEPS.md` | 2026-09-17 status snapshot | `docs/OPEN-ITEMS-TRACKER.md` |
| `ARCHITECT-REVIEW-2026-09-17.md` | Cross-verification of the above | sign-off notes inside it |
| `RUNNER-FAQ-AND-UI-GAPS.md` | Live-session gap notes (2026-09-21), all resolved | `docs/OPEN-ITEMS-TRACKER.md` |
| `ARCHITECTURE-BLUEPRINT.md` | v2.1 audit blueprint, self-declared STALE body | `docs/ARCHITECTURE.md`, `instructions/graph.txt` |

## Legacy release-era guides

`FULL-RELEASE-GUIDE.md`, `MANUAL-TESTING-CHECKLIST.md`,
`WINDOWS-POSTGRES-QUICKSTART-GUIDE.md`, `RELEASE-NOTES.md`, `LOCAL-TESTING-MANUAL.md`,
`DOCKER-AWS-SERVICE-ARCHITECTURE.md`, `DATABASE-MIGRATION-SEQUENCE-GUIDE.md`,
`mstock-typea-api-reference.md`, `TASK-*`, `PHASE-5-*`, `UI-READINESS.md` —
the original 24-step build; how-to content that is still valid has been folded
into the current guides under `docs/`.
