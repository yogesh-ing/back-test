# Card 01 — Skeleton, environment, packaging

**Prerequisite:** Card 00. **Builds:** the empty project that imports cleanly.

## File tree
```
backtest/
  pyproject.toml
  requirements.txt
  .env.example
  .gitignore
  src/backtest/
    __init__.py
    __main__.py                 # calls cli.main()
    cli.py
    runner.py
    data/     __init__.py base.py synthetic.py csv_source.py mstock_client.py mstock_source.py
    strategy/ __init__.py base.py registry.py
    strategies/ __init__.py buy_and_hold.py sma_crossover.py rsi_reversion.py donchian_breakout.py
    engine/   __init__.py backtester.py metrics.py plotting.py
    forward/  __init__.py broker.py portfolio.py paper.py     # Card 05
  tests/
    test_backtest.py test_exits.py test_compare.py test_plotting.py test_mstock_auth.py test_forward.py
```

## `requirements.txt`
```
pandas>=2.0
numpy>=1.24
requests>=2.31
python-dotenv>=1.0
matplotlib>=3.7
pyarrow>=14.0
pytest>=8.0
```

## `pyproject.toml` (essentials)
- `[project]`: name `backtest`, `requires-python = ">=3.10"`, dependencies =
  runtime subset (pandas, numpy, requests, python-dotenv, matplotlib, pyarrow).
- `[tool.setuptools.packages.find] where = ["src"]`.
- `[tool.pytest.ini_options] pythonpath = ["src"]`, `testpaths = ["tests"]`.

## `.env.example`
Keys: `MSTOCK_API_KEY`, `MSTOCK_USERNAME`, `MSTOCK_PASSWORD`,
`MSTOCK_CHECKSUM=W`, `MSTOCK_AUTH_MODE=otp` (`otp`=SMS, `totp`=authenticator),
optional `MSTOCK_BASE_URL`.

## `.gitignore`
Ignore `__pycache__/`, `.env`, `.mstock_token.json`, `data-cache/`, `charts/`,
`*.png`, `equity*.csv`, `compare*.csv`, `.pytest_cache/`; keep `!images/*.png`.

## `__main__.py`
Import and call `cli.main()`.

**Verify:**
```
python -m pip install -r requirements.txt
$env:PYTHONPATH="src"; python -c "import backtest; print('ok')"
```