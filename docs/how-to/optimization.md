# How to use Parameter optimization

**Pages:** `/optimize` (setup) · `/optimize/runs/<id>` (one run) · **Use it when…**
you want a systematically better parameter set — validated out-of-sample — instead
of hand-tuning.

Index: [USER-GUIDE.md](../USER-GUIDE.md) · engine detail: [OPTIMIZATION-ENGINE.md](../OPTIMIZATION-ENGINE.md)

## The setup form, section by section

1. **1 · Strategy & data** — `Load preset…` (start from a saved preset's values as
   the *current* column), Strategy, Symbol, Timeframe (only granularities actually
   stored are offered), From / To, Initial capital; an options **Strike selector**
   appears for option strategies.
2. **2 · Parameters** — table of `Opt ☐ · Parameter · Current · Min · Max · Step · #`;
   the counter updates live ("2 selected · 55 combinations"). Up to 8 parameters.
3. **3 · Objective & search** — `Maximize` (e.g. Sharpe) and Method:
   `Grid / Random / Bayesian / Genetic`, with the method's own knobs (samples,
   evaluations, population, generations).
4. **4 · Constraints** — `+ Add` risk constraints (max drawdown, minimum trades,
   win rate, …) a combination must satisfy to be a candidate.
5. **5 · Walk-forward validation** — train window, test window, step, max evals per
   split. This is what catches a curve-fit winner.

Then check **Run estimate** and press `▶ Start optimization` (or `Save as draft`).
**Recent runs** lists previous searches.

## The run dashboard (`/optimize/runs/<id>`)

Live progress while running, results afterwards. Six tabs:

| Tab | What it gives you |
|-----|-------------------|
| `Overview` | Best run vs baseline, validation checks, robustness score and plain-English overfitting warnings |
| `Heatmaps` | Any two parameters as a surface — look for a **plateau**, not a spike |
| `Walk-forward` | Train/test folds: does the winner hold out of sample? |
| `Sensitivity` | One-at-a-time sweeps around the winner |
| `All results` | Every candidate, sortable, with CSV export |
| `Audit` | Every apply, with rollback and the originating run |

## Applying the winner (3-step wizard)

1. **Review the checks** — walk-forward, robustness, deflated Sharpe, Monte Carlo,
   data source, trade count; each ✅ / ⚠️ / ❌.
2. **Choose the target** — new paper runner, replace existing, A/B, or live. Live
   carries an inline explanation of the **≥30-day paper-history gate**.
3. **Confirm** by typing `CONFIRM`.

Applying to **live** needs explicit confirmation plus walk-forward validation and
is refused for overfitted runs. Every apply lands in the audit trail with rollback
and names the originating backtest when the run came from one.

Storage: PostgreSQL (migrations 009–015) or the dev SQLite profile.

## API

`POST /api/optimize/*` (grid / random / Bayesian / genetic + walk-forward),
`GET /optimize/runs/<run_id>/{progress,results}`.
