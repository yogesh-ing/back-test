# How to use Strategy builder

**Page:** `/strategy-builder` · **Use it when…** you have a TradingView **Pine
Script v5** idea and want it as a Python strategy plugin.

Index: [USER-GUIDE.md](../USER-GUIDE.md) · authoring rules:
[STRATEGY-AUTHORING.md](../STRATEGY-AUTHORING.md),
[STRATEGY-TEMPLATE-GUIDE.md](../STRATEGY-TEMPLATE-GUIDE.md)

## The flow

1. **Strategy name** (optional — defaults to the script's title).
2. **Segment** (required) — every strategy runs in one capital partition so that
   backtest → paper → live share it. The form will not save without it.
3. Paste the **Pine Script v5 source** into the textarea (`//@version=5`).
4. `▶ Convert strategy` — the right pane shows the generated Python, and a
   **readable translation** appears underneath: entry criteria, entry strike
   (options), take-profit, stop-loss.
5. Anything the script left out must be typed into that summary — **`💾 Save as
   plugin` only appears once every required criterion has a value.**
6. `✅ Validate (backtest)` runs it against historical data before you trust it.
7. `📋 Copy LLM prompt` for scripts too complex for the converter — paste it into
   ChatGPT/Claude and bring the result back.

## After saving

The plugin lands in `plugins/strategies/` and appears in the Backtest / Compare /
Optimize dropdowns automatically, publishing its `eligible_instruments` for the
instrument picker. Then: [backtest.md](backtest.md).

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `Save as plugin` stays hidden | A required criterion has no value | Fill the readable translation fields |
| `Segment` blocks saving | No segment selected | Pick a segment (create one in [settings.md](settings.md) first) |
| Convert error panel | Unsupported Pine construct | Use `Copy LLM prompt` and convert out of band |
