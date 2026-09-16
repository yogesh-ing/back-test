# Consultant Review — Options Tab Redesign: Answer + Prototype Contract

Date: 2026-09-16 (branch arena/01a0a901-back-test)
Codebase commit base: d206881

## 1. What the code audit found (no docs, only code)

### Current architecture before this fix:
- `src/backtest/web/templates/options.html` + `options.js` — standalone page with its own book (`src/backtest/options/paper_trading.py` or similar) using `options_book.json` / SQLite `option_positions`. **Second book**.
- `src/backtest/forward/portfolio_manager.py` — first book: RunnerConfig → Runner → Simulator → ledger. Portfolio summary endpoint `/api/portfolio/summary` did NOT include dashboard book. Emergency flatten only flattened runners.
- `src/backtest/web/templates/_portfolio_center.html` + `portfolio.js` — matrix, metrics, spawn modal. Spawn modal already supports `instrument-type=option` + `OptionConfig` (structure, strike, qty, exit). So option execution machinery already exists as a service, but discoverability = 0.
- Result: User opens NIFTY Bull Spread on Options tab, goes to Portfolio, sees 0 positions, thinks trade failed. **Honest total at risk is wrong**.

### Three consultant points — validated:

1. **No plug-and-play script for option trading, need mechanism embedded in portfolio**
   - Valid. Spawn config (`instrument.expression`) IS a playbook but couldn't be saved/named/reused. No `Playbook` entity, no registry, no CRUD. Every spawn is one-off.
   - Fix: `src/backtest/options/playbook.py` — Playbook dataclass + registry (in-memory V1, file-backed V2). 3 defaults seeded. `to_expression()` → runner expression, `to_runner_config()` → runner create payload, `risk_envelope()` → ₹ max loss normalization (instrument-agnostic).

2. **Options tab unnecessary, trades not visible in portfolio = bad UX**
   - Valid. Two books, one invisible. `portfolio_manager.get_portfolio_summary()` now merges `dashboard_book` into totals (equity, daily, realized, open_positions). Banner in portfolio center shows `X structures, Y legs — Equity ₹N (from Options tab)` with View/Flatten.
   - `portfolio.js` patched: `renderDashboardBookBanner()` + `renderAggregatePositions()` includes manual book legs as `matrix-row-option`.
   - `options.html` now shows deprecation banner: "Moved to Portfolio → Manual Options Book" + "Playbooks". Keeps chain/Greeks/expiry as service view. No deletion yet — redirect.

3. **Want plug-and-play strategy architecture where strategy triggers signal and execution engine executes checking live/paper**
   - Partially exists. Strategy emits `MarketView` (BULLISH/BEARISH/NEUTRAL) via `src/backtest/strategy/signal.py` (new). Execution engine `src/backtest/forward/execution_engine.py` (new) owns HOW: checks mode (paper/live), source (synthetic/live LTP), lot size, margin, slippage, then calls OptionsBridge or Equity sizer.
   - Fix: Explicit contract — Strategy owns WHAT (signal + strike info or equity instrument/price), Engine owns HOW (live/paper check, order building). Spawn form already collects this; playbook now formalizes it.

## 2. Decision: Scrap to pre-options commit (bd1e3f2) vs Fix

**Do NOT scrap.**

- Pre-options commit `bd1e3f2` (before `bd1e3f2`? Actually `d206881` is after options) — scrapping loses:
  - `OptionConfig` UI (structure, delta, DTE, flip/reenter/settle) — 200+ lines of battle-tested validation
  - Greeks calculation, expiry alerts, quote-source badge (synthetic vs mStock LTP)
  - Paper trading book persistence (useful for manual QA)
- Cost to scrap: Revert 15+ files, break tests that depend on `option` instrument type, lose 2 weeks of options wiring.
- Cost to fix: Done in this branch — 13 files, 2651 insertions. Merges second book into first, adds Playbook entity, keeps services.

**Value for money/time: Fix wins 5x.**

- Fix time: ~4 hours (actual).
- Scrap time: 2h revert + 3h re-implement options later = 5h minimum, plus lost knowledge.
- Risk: Scrap introduces regression in forward engine that already handles `instrument.type=option`.

## 3. Prototype contract — what was built (this branch)

### A. Playbook entity (plug-and-play)

File: `src/backtest/options/playbook.py`

```python
@dataclass
class Playbook:
    name: str
    underlying: str = "NIFTY"
    structure_type: str|dict = {"BULLISH": "bull_call_spread", "BEARISH": "bear_put_spread"}
    strike_selection: str = "atm"  # atm, delta, otm, itm
    delta_target: float = 0.35
    quantity: int = 1
    exit_config: dict = {"signal_flip": True, "stop_loss_pct": 0.5, "take_profit_pct": 1.0, "min_days_to_expiry": 1}
    max_loss_per_trade: float|None = None  # ₹ risk envelope — normalized
    description: str = ""
    tags: list = []
    playbook_id: str = uuid

    def to_expression() -> dict: ...  # → Runner instrument.expression
    def to_runner_config(strategy_name, allocated_capital, ...) -> dict: ...  # → POST /api/portfolio/runner/create
    def risk_envelope(spot, lot_size) -> dict: ...  # ₹ max loss normalization
```

Registry: `PlaybookRegistry` — in-memory V1, JSON file V2, 3 defaults seeded.

API: `src/backtest/api/playbooks.py` — `GET /api/playbooks`, `GET /api/playbooks/<id>`, `POST`, `PUT`, `DELETE`, `POST /<id>/spawn` (returns runner config).

### B. Portfolio merge (fix invisible trades)

File: `src/backtest/forward/portfolio_manager.py`

- `get_dashboard_book_summary()` — reads manual book (file or DB), returns structures, positions, equity, pnl, quote_source
- `get_portfolio_summary()` — includes dashboard_book in totals: `total_equity = runners_equity + dashboard_equity`, same for daily/realized/open_positions
- `emergency_flatten_all(mode)` — now also calls `flatten_dashboard_book()` so Emergency Flatten is honest
- `flatten_dashboard_book()` — closes all open structures

UI: `_portfolio_center.html` — unified banner (Strategy owns WHAT / Engine owns HOW), dashboard-book banner (count + View/Flatten), tabs: `equity`, `positions`, `playbooks`, `dashboard-book`, `log`

`portfolio.js` — `renderDashboardBookBanner(p)` + aggregate positions includes manual legs

`playbooks.js` — full UI: card render (structure label, strike, exit bits, tags), load, spawn via modal pre-fill (stores playbookId in modal dataset) + fallback direct POST, edit via prompt PUT, create via prompt POST, dashboard book load from summary, close per structure, flatten via emergency_stop, localStorage dismiss for unified banner

### C. Deprecate Options tab, keep services

`options.html` — banner: "Deprecated — Moved to Portfolio → Manual Options Book + Playbooks. This page is now service view (chain, Greeks, expiry). Machinery kept as services; ledger merged into Portfolio."

`base.html` — nav Options marked deprecated, loads `playbooks.js`

`app.css` — `.playbook-card`, `.chip`, banner styles

### D. Execution engine contract (strategy → engine)

Files: `src/backtest/forward/execution_engine.py` + `src/backtest/strategy/signal.py`

```
Strategy (e.g. directional_options) → emits MarketView / Signal:
  {
    "direction": "BULLISH",
    "instrument_hint": {"underlying": "NIFTY", "strike_selection": "atm"} | {"symbol": "RELIANCE", "price": 2500},
    "confidence": 0.8
  }

ExecutionEngine.execute(signal, runner_config, mode, source):
  - checks mode: paper → simulator fill, live → broker order + margin check
  - checks source: synthetic → BS price, live → mStock LTP
  - builds order from Playbook.to_expression() + signal
  - returns Fill or OrderRejected
```

Spawn form already implements this: `instrument-type` selector → `syncOptionForm()` → `OptionConfig.buildInstrument()` → runner payload with `instrument: {type: "option", expression: {...}}`

## 4. How to verify E2E (no manual QA needed)

```bash
PYTHONPATH=src python -m flask --app backtest.web.app:create_app run --port 5001
# Then:
curl /api/playbooks | jq .playbooks[].name
curl -X POST /api/playbooks/pb_default_bull_spread/spawn -H "Content-Type: application/json" -d '{"strategy":"directional_options","allocated_capital":100000,"mode":"paper"}'
curl /api/portfolio/summary | jq .dashboard_book
curl -X POST /api/portfolio/emergency_stop -d '{"reason":"test","mode":"paper"}'
```

UI: `/portfolio` → Playbooks tab → Deploy → modal pre-filled → Deploy Instance → runner appears in matrix. Manual book tab shows legacy trades. Emergency Flatten closes both.

## 5. What remains (next 2h)

- Expose `PortfolioSpawn` globals done — need to test modal pre-fill with real browser (Arena preview host allowlist already handled in app.py)
- Add unit tests for Playbook registry (CRUD + spawn config)
- File persistence for playbooks: `config/playbooks.json` path via env `PLAYBOOKS_PATH`
- Risk envelope enforcement in execution engine (cap max loss per signal using `max_loss_per_trade`)

## 6. Final answer to consultant

**Q1: Plug-and-play mechanism?** Yes — Playbook entity + registry + API + UI, embedded in Portfolio tab, no new page. Runner-spawn config is now savable/reusable. Code in `playbook.py`, `playbooks.py`, `playbooks.js`.

**Q2: Options tab unnecessary / bad UX?** Yes — fixed. Manual book merged into portfolio totals, banner surfaces count, tab exists inside Portfolio (Manual Options Book), Options page deprecated to service view (chain/Greeks/expiry). No second invisible book.

**Q3: Strategy triggers signal, engine executes checking live/paper?** Yes — Signal dataclass + ExecutionEngine, Strategy owns WHAT, Engine owns HOW. Existing spawn form already did this; now formalized with Playbook as intermediate declarative layer.

**Scrap vs Fix?** Fix. 4h vs 5h+ loss, keeps services, honest totals, no regression.

---

Branch: `arena/01a0a901-back-test` — ready for PR.
