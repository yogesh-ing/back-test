"""Batch backtest for the swing-strategy plugin set + deployment quality gate.

Runs each (strategy, timeframe) combo across a liquid NSE basket through the
CANONICAL engine (backtest.engine.backtest_runner.run_backtest) with the
real mStock statutory fee stack and 5 bps/side slippage — the same engine
path /api/backtest/run uses, so a number here is the number the UI would show.

DATA LOADING NOTE (2026-10-02): the 1-day cache holds BOTH NSE and BSE rows
for most symbols under two timestamp conventions, and DbSource does not
filter by exchange — feeding that to a backtest doubles bars per day and
corrupts every indicator. This runner instead builds one clean NSE-preferred
bar per IST session date, and resamples weekly/hourly itself (hourly from
1-min rows, whose naive timestamps are UTC-of-IST-wall-clock and get fixed
back to IST here). Weekly/hourly candles from DbSource are unusable until
that exchange/tz handling is fixed in the shared loader.

Gates (per combo, all must pass to call the strategy "gate PASS"):
  G1 median profit factor        >= 1.3
  G2 median Sharpe (annualised)  >= 0.8
  G3 median max drawdown depth   <= 20 %
  G4 total closed trades >= 60 and >=70 % of symbols with >= 5 trades
  G5 net-profitable symbols      >= 60 %
  Beat buy&hold is NOT required (swing != alpha vs a bull market) but reported.

Usage:  PYTHONPATH=src python tools/swing_backtest.py
Writes tools/out/swing_report.json and prints a table.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:  # noqa: BLE001
    pass

import pandas as pd
from sqlalchemy import create_engine, text

from backtest.plugins import discover_plugins
from backtest.engine.backtest_runner import run_backtest

IST = "Asia/Kolkata"

CAPITAL = 100_000.0
SLIPPAGE_BPS = 5.0          # per side, conservative for NSE large-caps
BROKER = "mstock"           # statutory fee schedule (STT/exch/GST/stamp)

DAILY_BASKET = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "SBIN",
    "ITC", "LT", "AXISBANK", "BAJFINANCE", "ONGC", "TATAMOTORS",
    "MARUTI", "BHARTIARTL", "KOTAKBANK", "TITAN", "TATASTEEL", "HINDALCO",
]
HOURLY_BASKET = ["RELIANCE", "TCS", "HDFCBANK", "JIOFIN", "BAJFINANCE"]

_AGG = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}


def drop_reverting_spikes(df: pd.DataFrame, thresh: float = 0.30) -> pd.DataFrame:
    """Remove single-bar data poison: a bar whose close jumps >thresh% from
    its neighbour AND reverts on the next bar (true corporate actions do not
    come back). E.g. KOTAKBANK 2025-10-07/09 (+397% then -80%)."""
    c = df["close"]
    up = c / c.shift(1)
    dn = c.shift(-1) / c
    spike = ((up > 1 + thresh) & (dn < 1 - thresh)) | ((up < 1 - thresh) & (dn > 1 + thresh))
    n = int(spike.sum())
    if n:
        df = df[~spike.fillna(False)]
    return df


def split_adjust(df: pd.DataFrame, lo: float = 0.70, hi: float = 1.40) -> pd.DataFrame:
    """Back-adjust OHLC for unadjusted splits/bonuses in the stored series.

    The daily cache's older era (and mStock minute history) store raw traded
    prices, so a 5:1 split (KOTAKBANK 2024-10) or 1:1 bonus (TCS 2025-12)
    appears as a -80%/-50% one-bar cliff that no large-cap circuit allows.
    Any close-to-close ratio outside [lo, hi] is treated as a corporate
    action and every PRIOR bar is multiplied by the ratio, putting history on
    the post-event price basis. Returns a copy; volume is adjusted inversely.
    """
    if len(df) < 2:
        return df
    out = df.copy()
    closes = out["close"].values
    factors = [1.0] * len(closes)
    cum = 1.0
    for i in range(len(closes) - 1, 0, -1):
        factors[i] = cum                      # bar i sits AFTER its own event
        r = closes[i] / closes[i - 1] if closes[i - 1] else 1.0
        if r < lo or r > hi:
            cum *= r                          # earlier bars join the new basis
    factors[0] = cum
    fac = pd.Series(factors, index=out.index)
    for col in ("open", "high", "low", "close"):
        out[col] = out[col] * fac
    out["volume"] = out["volume"] / fac.replace(0.0, 1.0)
    return out

#: (combo id, strategy name, timeframe, params, start, end, basket)
COMBOS = [
    ("ema_pullback_1d", "swing_ema_pullback", "1day", {},
     "2019-01-01", "2026-08-25", DAILY_BASKET),
    ("donchian_1d", "swing_donchian_breakout", "1day",
     {"entry_n": 55, "exit_n": 20}, "2019-01-01", "2026-08-25", DAILY_BASKET),
    ("donchian_1w", "swing_donchian_breakout", "1week",
     {"entry_n": 40, "exit_n": 18, "atr_pct_min": 3.0},
     "2019-01-01", "2026-08-25", DAILY_BASKET),
    ("rsi2_meanrev_1d", "swing_rsi2_meanrev", "1day", {},
     "2019-01-01", "2026-08-25", DAILY_BASKET),
    ("supertrend_1h", "swing_hourly_supertrend", "1hour", {},
     "2022-01-01", "2026-09-24", HOURLY_BASKET),
    # --- one-shot robustness variants (sensitivity, NOT cherry-pick pool) ---
    ("ema_pullback_1d_fast_exit", "swing_ema_pullback", "1day",
     {"ema_slow": 25}, "2019-01-01", "2026-08-25", DAILY_BASKET),
    ("rsi2_meanrev_1d_deeper", "swing_rsi2_meanrev", "1day",
     {"rsi_buy": 5.0}, "2019-01-01", "2026-08-25", DAILY_BASKET),
    ("supertrend_1h_wider", "swing_hourly_supertrend", "1hour",
     {"st_factor": 4.0}, "2022-01-01", "2026-09-24", HOURLY_BASKET),
]

GATES = {"g1_median_pf": 1.3, "g2_median_sharpe": 0.8, "g3_median_dd": 20.0,
         "g4_total_trades": 60, "g5_profitable_share": 0.60}


def _read(engine, query, params) -> pd.DataFrame:
    df = pd.read_sql(text(query), engine, params=params)
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert(IST)
    return df


def load_daily(engine, symbol: str, start: str, end: str) -> pd.DataFrame:
    """One clean bar per IST trading date, NSE preferred, mock dropped."""
    df = _read(
        engine,
        """SELECT ts, open, high, low, close, volume, exchange
           FROM market_data_cache
           WHERE symbol=:s AND timeframe='1day' AND source <> 'mock'
             AND ts >= :a AND ts <= :b ORDER BY ts""",
        {"s": symbol, "a": start, "b": end + "T23:59:59Z"},
    )
    if df.empty:
        return df
    df["date"] = df["ts"].dt.tz_convert(IST).dt.date  # IST session calendar date
    nse = df[df["exchange"] == "NSE"]
    frame = nse if len(nse) >= len(df) * 0.2 else df[df["exchange"] == "BSE"]
    if frame.empty:
        frame = df
    # Same exchange can still carry two fetch conventions per date -> max-volume wins.
    frame = (frame.sort_values("volume").groupby("date", as_index=False).last()
                 .sort_values("date").reset_index(drop=True))
    idx = pd.DatetimeIndex(pd.to_datetime(frame["date"].tolist())).tz_localize("UTC").tz_convert(IST)
    frame.index = idx
    frame.index.name = "ts"
    cleaned = drop_reverting_spikes(frame[["open", "high", "low", "close", "volume"]])
    return split_adjust(cleaned)


def load_weekly(engine, symbol: str, start: str, end: str) -> pd.DataFrame:
    d = load_daily(engine, symbol, start, end)
    if d.empty:
        return d
    w = d.resample("W-FRI").agg(_AGG).dropna()
    return w


def load_hourly(engine, symbol: str, start: str, end: str) -> pd.DataFrame:
    """1-minute rows -> IST hourly bars anchored on the 09:15 session open.

    Cached rows are repaired at the source (``bar_timestamp`` writers plus
    ``scripts/migrate_cache_ts_to_session_clock.py``), so ``_read`` already
    hands back bars on the real IST session clock — no re-tagging here.
    """
    df = _read(
        engine,
        """SELECT ts, open, high, low, close, volume
           FROM market_data_cache
           WHERE symbol=:s AND timeframe='1min' AND ts >= :a AND ts <= :b
           ORDER BY ts""",
        {"s": symbol, "a": start, "b": end + "T23:59:59Z"},
    )
    if df.empty:
        return df
    df.index = df["ts"]
    df.index.name = "ts"
    h = (df[["open", "high", "low", "close", "volume"]]
         .resample("1h", offset="15min", label="left", closed="left")
         .agg(_AGG).dropna(subset=["open"]))
    # Keep only real session buckets (09:15 .. 15:15 IST); stray off-hours rows
    # (pollution bars at 12:45 etc.) would create phantom hourly candles.
    h = h[(h.index.hour >= 9) & (h.index.hour <= 15)]
    return split_adjust(drop_reverting_spikes(h))


def load_candles(engine, timeframe: str, symbol: str, start: str, end: str) -> pd.DataFrame:
    if timeframe == "1day":
        return load_daily(engine, symbol, start, end)
    if timeframe == "1week":
        return load_weekly(engine, symbol, start, end)
    if timeframe == "1hour":
        return load_hourly(engine, symbol, start, end)
    raise ValueError(timeframe)


def available_symbols(engine, wanted: list[str], timeframe: str, start: str, end: str) -> list[str]:
    src_tf = "1min" if timeframe == "1hour" else "1day"
    rows = engine.connect().execute(text(
        "SELECT DISTINCT symbol FROM market_data_cache WHERE timeframe=:tf "
        "AND symbol = ANY(:syms)"
    ), {"tf": src_tf, "syms": wanted}).fetchall()
    have = {r[0] for r in rows}
    return [s for s in wanted if s in have]


def run_combo(engine, combo_id, strategy, timeframe, params, start, end, basket):
    rows = []
    for sym in basket:
        t0 = time.time()
        try:
            candles = load_candles(engine, timeframe, sym, start, end)
        except Exception as exc:  # noqa: BLE001
            print(f"  {sym}: DATA FAIL {exc}")
            continue
        if len(candles) < 260:
            print(f"  {sym}: only {len(candles)} bars, skipped")
            continue
        try:
            res = run_backtest(
                candles, strategy, params, sym, CAPITAL,
                broker=BROKER, timeframe=timeframe, slippage_bps=SLIPPAGE_BPS,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  {sym}: ENGINE FAIL {type(exc).__name__}: {exc}")
            continue
        m = res.metrics
        bh_ret = (candles["close"].iloc[-1] / candles["close"].iloc[0] - 1) * 100.0
        rows.append({
            "symbol": sym,
            "bars": int(m.get("bars", len(candles))),
            "total_return_pct": float(m.get("total_return", 0.0) or 0.0) * 100.0,
            "cagr_pct": float(m.get("cagr", 0.0) or 0.0) * 100.0,
            "sharpe": float(m.get("sharpe", 0.0) or 0.0),
            "max_dd_depth_pct": abs(float(m.get("max_drawdown", 0.0) or 0.0)) * 100.0,
            "win_rate_pct": float(m.get("win_rate", 0.0) or 0.0) * 100.0,
            "profit_factor": float(m.get("profit_factor", 0.0) or 0.0),
            "closed_trades": int(m.get("closed_trades", 0) or 0),
            "avg_hold_bars": float(m.get("avg_holding_bars", 0.0) or 0.0),
            "fees_paid": float(m.get("fees_paid", 0.0) or 0.0),
            "buy_hold_pct": float(bh_ret),
            "run_secs": round(time.time() - t0, 1),
        })
        print(f"  {sym:11s} ret={rows[-1]['total_return_pct']:7.1f}%  bh={bh_ret:6.1f}%  "
              f"sharpe={rows[-1]['sharpe']:5.2f}  dd={rows[-1]['max_dd_depth_pct']:5.1f}%  "
              f"pf={rows[-1]['profit_factor']:4.2f}  trades={rows[-1]['closed_trades']:3d}")
    return rows


def summarise(rows: list[dict]) -> dict:
    if not rows:
        return {"symbols": 0}
    df = pd.DataFrame(rows)
    total_trades = int(df["closed_trades"].sum())
    out = {
        "symbols": len(df),
        "total_closed_trades": total_trades,
        "median_return_pct": round(float(df["total_return_pct"].median()), 2),
        "median_buy_hold_pct": round(float(df["buy_hold_pct"].median()), 2),
        "median_sharpe": round(float(df["sharpe"].median()), 2),
        "median_max_dd_pct": round(float(df["max_dd_depth_pct"].median()), 2),
        "worst_max_dd_pct": round(float(df["max_dd_depth_pct"].max()), 2),
        "median_profit_factor": round(float(df["profit_factor"].median()), 2),
        "median_win_rate_pct": round(float(df["win_rate_pct"].median()), 1),
        "median_avg_hold_bars": round(float(df["avg_hold_bars"].median()), 1),
        "profitable_share": round(float((df["total_return_pct"] > 0).mean()), 2),
        "trade_ge5_share": round(float((df["closed_trades"] >= 5).mean()), 2),
        "total_fees": round(float(df["fees_paid"].sum()), 0),
    }
    out["gates"] = {
        "g1_median_pf>=1.3": out["median_profit_factor"] >= GATES["g1_median_pf"],
        "g2_median_sharpe>=0.8": out["median_sharpe"] >= GATES["g2_median_sharpe"],
        "g3_median_dd<=20": out["median_max_dd_pct"] <= GATES["g3_median_dd"],
        "g4_total_trades>=60 & 70% syms>=5": (
            total_trades >= GATES["g4_total_trades"] and out["trade_ge5_share"] >= 0.7
        ),
        "g5_profitable_share>=0.6": out["profitable_share"] >= GATES["g5_profitable_share"],
    }
    out["GATE"] = "PASS" if all(out["gates"].values()) else "FAIL"
    return out


def main() -> int:
    import os
    db_url = os.environ.get("FORWARD_TEST_DB_URL")
    if not db_url:
        print("FORWARD_TEST_DB_URL missing from .env")
        return 1

    loaded = discover_plugins()
    print(f"plugins discovered: {len(loaded) if loaded else 0}")

    engine = create_engine(db_url)

    report: dict[str, dict] = {}
    for combo_id, strategy, tf, params, start, end, basket in COMBOS:
        syms = available_symbols(engine, basket, tf, start, end)
        if not syms:
            print(f"\n[{combo_id}] no symbols with data — skipped")
            continue
        print(f"\n[{combo_id}] {strategy} on {tf} — {len(syms)} symbols, {start}..{end}")
        rows = run_combo(engine, combo_id, strategy, tf, params, start, end, syms)
        summary = summarise(rows)
        report[combo_id] = {"strategy": strategy, "timeframe": tf, "params": params,
                            "rows": rows, "summary": summary}
        print(f"  -> summary: {json.dumps({k: v for k, v in summary.items() if k != 'gates'})}")
        print(f"  -> gates:   {summary.get('gates')}  => {summary.get('GATE')}")

    out_dir = ROOT / "tools" / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "swing_report.json"
    out_path.write_text(json.dumps(report, indent=2, default=str))
    print(f"\nreport written: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
