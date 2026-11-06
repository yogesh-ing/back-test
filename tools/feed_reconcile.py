"""Upstox feed probe + mStock reconciliation harness — READ ONLY.

Answers three questions with live numbers, never with a guess:

1. ``probe``  — which interval vocabulary does Upstox's history API actually
   accept, and how many candles does one call return (the 1000-candle cap
   decides what ``chunk_days`` may be).
2. ``bars``   — do Upstox bars agree with the mStock-sourced bars already in
   ``market_data_cache`` for the same symbol/window: bar count, timestamp
   alignment, per-bar OHLC deltas.
3. ``chain``  — does Upstox's own option chain + quote surface mark contracts
   sensibly (bid/ask present, price >= intrinsic, CE/PE parity).

Nothing here writes: no DB writes, no cache inserts, no orders. Quote and
history calls are session-gated, so run it after logging into Upstox in this
checkout; ``chain``'s contract list itself works without a session (the
instrument master is public).

Examples (from the repo root):
    python tools/feed_reconcile.py probe --symbol RELIANCE
    python tools/feed_reconcile.py bars --symbol RELIANCE --timeframe 1min \\
        --from 2026-10-01 --to 2026-10-05
    python tools/feed_reconcile.py chain --underlying NIFTY
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd

# This box's console is cp1252: an em dash or a rupee sign in a report line
# otherwise raises UnicodeEncodeError and grep calls the output binary.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

API_ROOT = "https://api.upstox.com"
EXCHANGE_TZ = "Asia/Kolkata"

#: v2 path intervals, as the production fetcher maps them.
V2_INTERVALS = ("1minute", "3minute", "5minute", "15minute", "30minute",
                "60minute", "day", "week", "month")

#: v3 path pairs — ``unit`` plus an integer ``interval``.
V3_INTERVALS = (
    ("minute", 1), ("minute", 3), ("minute", 5), ("minute", 10),
    ("minute", 15), ("minute", 30), ("minute", 60),
    ("day", 1), ("week", 1), ("month", 1),
)

#: ``{canonical timeframe: (v2 name, v3 unit, v3 interval)}`` for the windows
#: the fetcher is asked for. ``4hour`` has no broker equivalent on purpose.
TF_TO_UPSTOX = {
    "1min": ("1minute", "minute", 1),
    "5min": ("5minute", "minute", 5),
    "15min": ("15minute", "minute", 15),
    "30min": ("30minute", "minute", 30),
    "1hour": ("60minute", "minute", 60),
    "1day": ("day", "day", 1),
}

#: Daily rows carry no session time on one of the two feeds, so they are
#: bucketed by calendar day instead of to-the-minute.
DAYLIKE = ("1day", "1week")

SLEEP_SECONDS = 0.25


# ---------------------------------------------------------------------------
# session + HTTP plumbing
# ---------------------------------------------------------------------------


def _server_broker():
    """Borrow the running app's in-memory session through the localhost bridge.

    ``GET /api/broker/session-token`` exists for exactly this (first-party
    scripts can't see the web app's session, and the persisted store goes
    stale on every re-login). It hands out the **UI-active** broker's token, so
    refuse rather than use an mStock token against Upstox. The token is kept in
    memory and never printed.
    """
    from backtest.brokers.upstox import UpstoxBroker

    base = os.getenv("FEED_RECONCILE_APP_URL", "http://127.0.0.1:5000")
    status = _get(f"{base}/api/broker/status", {})
    if status[0] != 200 or not isinstance(status[1], dict):
        return None
    payload = status[1]
    if payload.get("broker") != "upstox":
        print(f"(the app's UI-active broker is {payload.get('broker')!r}, not "
              "upstox — select Upstox in the app header, or log in here)")
        return None
    entry = (payload.get("sessions") or {}).get("upstox") or {}
    if entry.get("status") != "authenticated":
        return None
    got = _get(f"{base}/api/broker/session-token", {})
    if got[0] != 200 or not isinstance(got[1], dict) or not got[1].get("token"):
        print(f"(no token from the app: HTTP {got[0]})")
        return None
    broker = UpstoxBroker(http_timeout=30.0)
    broker.restore_session(str(got[1]["token"]), entry.get("expires_at"))
    print(f"Using the live app's Upstox session (expires "
          f"{entry.get('expires_at')}); token held in memory, never printed.")
    return broker if broker.get_session_token() else None


def _upstox_broker():
    """The logged-in Upstox broker for this checkout, or exit with advice."""
    from backtest.brokers.session_manager import get_session_manager

    try:
        broker = get_session_manager().get_broker("upstox")
        if broker.get_session_token():
            return broker
    except Exception:  # noqa: BLE001 — fall through to the app bridge
        pass

    borrowed = _server_broker()
    if borrowed is not None:
        return borrowed
    sys.exit(
        "No Upstox session this process can reach — neither a remembered token "
        "here nor the live app's.\nLog into Upstox in the app on :5000 (the "
        "bridge above reads it) and re-run. `chain --offline` needs no session."
    )


def _get(url: str, headers: dict) -> tuple[int, Any, str]:
    """One GET, returning (status, json-or-None, first 200 chars of the body).

    The body text is a diagnostic string, never a credential: Upstox error
    bodies do not carry the token, and the token is not printed anywhere.
    """
    import requests

    try:
        resp = requests.get(url, headers=headers, timeout=60)
    except requests.RequestException as exc:
        return 0, None, f"{type(exc).__name__}: {exc}"
    try:
        payload = resp.json()
    except ValueError:
        payload = None
    return resp.status_code, payload, (resp.text or "")[:200]


def _auth_headers(broker) -> dict:
    return {
        "Authorization": f"Bearer {broker.get_session_token()}",
        "Api-Version": "2.0",
        "Accept": "application/json",
    }


def _history_url(api: str, key: str, interval: str, win_to: str, win_from: str) -> str:
    """History URL for one probe. Upstox's path order is to_date THEN from_date."""
    if api == "v2":
        return f"{API_ROOT}/v2/historical-candle/{key}/{interval}/{win_to}/{win_from}"
    unit, _, steps = interval.partition(":")
    return (
        f"{API_ROOT}/v3/historical-candle/{key}/{unit}/{steps}/{win_to}/{win_from}"
    )


def _candles_of(payload: Any) -> list:
    """Dig the candle list out of either API version's envelope."""
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if isinstance(data, dict):
        candles = data.get("candles") or data.get("candleData")
        if isinstance(candles, list):
            return candles
        if isinstance(data, dict):  # v3: {key: {candles: [...]}}
            for value in data.values():
                if isinstance(value, dict) and isinstance(value.get("candles"), list):
                    return value["candles"]
    if isinstance(data, list):  # v3 intraday: [{candles: [...]}, ...]
        for value in data:
            if isinstance(value, dict) and isinstance(value.get("candles"), list):
                return value["candles"]
    return []


def _wall_clock(raw: Any) -> pd.Timestamp | None:
    """One Upstox stamp → naive exchange wall clock (the DB's read clock).

    Upstox sends ``...+05:30``; ``market_data_cache`` rows come back from
    :func:`to_exchange_wall_clock` as naive IST. Comparing the two without
    this conversion would score a 5h30 offset as total disagreement.
    """
    try:
        ts = pd.Timestamp(str(raw))
    except (ValueError, TypeError):
        return None
    if ts.tzinfo is not None:
        ts = ts.tz_convert(EXCHANGE_TZ).tz_localize(None)
    return ts


def _bar_from_candle(candle: list) -> dict | None:
    """``[ts, open, high, low, close, volume, oi]`` → the fetcher's bar dict."""
    if not isinstance(candle, (list, tuple)) or len(candle) < 6:
        return None
    ts = _wall_clock(candle[0])
    if ts is None:
        return None
    try:
        return {
            "t": ts,
            "o": float(candle[1]),
            "h": float(candle[2]),
            "l": float(candle[3]),
            "c": float(candle[4]),
            "v": int(candle[5] or 0),
        }
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 1. probe — interval vocabulary + candle cap
# ---------------------------------------------------------------------------


def cmd_probe(args) -> int:
    from backtest.brokers.upstox import resolve_instrument_key

    broker = _upstox_broker()
    headers = _auth_headers(broker)
    key = resolve_instrument_key(args.instrument_key or args.symbol, timeout=30.0)
    win_to, win_from = args.to, args.frm
    if args.days:
        win_to = date.today().isoformat()
        win_from = (date.today() - timedelta(days=args.days)).isoformat()

    print(f"instrument key : {key}")
    print(f"window         : {win_from} .. {win_to}")
    print(f"{'api':<5} {'interval':<12} {'http':<5} {'bars':>6}  first .. last")
    print("-" * 78)

    targets = []
    if args.api in ("v2", "both"):
        targets += [("v2", name) for name in V2_INTERVALS]
    if args.api in ("v3", "both"):
        targets += [("v3", f"{unit}:{steps}") for unit, steps in V3_INTERVALS]

    accepted = 0
    for api, interval in targets:
        url = _history_url(api, key, interval, win_to, win_from)
        status, payload, body = _get(url, headers)
        candles = _candles_of(payload)
        first = last = ""
        if candles:
            first = str(_wall_clock(candles[0][0]))[:19]
            last = str(_wall_clock(candles[-1][0]))[:19]
        note = "" if status == 200 else f"  <- {body[:38]}"
        print(f"{api:<5} {interval:<12} {status:<5} {len(candles):>6}  {first} .. {last}{note}")
        if status == 200 and candles:
            accepted += 1
        time.sleep(SLEEP_SECONDS)

    print("-" * 78)
    print(f"{accepted}/{len(targets)} interval forms returned bars.")
    print("A form returning exactly 1000 bars for a wider window is the candle "
          "cap, not the whole range — set --chunk-days from that row.")
    return 0


# ---------------------------------------------------------------------------
# 2. bars — Upstox vs the mStock-sourced cache
# ---------------------------------------------------------------------------


def _fetch_upstox_window(
    broker, key: str, timeframe: str, frm: str, to: str, chunk_days: int, api: str
) -> list[dict]:
    """Bars for one window through the URL forms ``probe`` ranked.

    Deliberately not the fetcher function itself: that one is hardcoded to v2
    and this harness exists to find out whether that is the right choice.
    """
    headers = _auth_headers(broker)
    v2_name, unit, steps = TF_TO_UPSTOX[timeframe]
    interval = f"{unit}:{steps}"
    out: list[dict] = []
    start = datetime.strptime(frm, "%Y-%m-%d").date()
    stop = datetime.strptime(to, "%Y-%m-%d").date()
    while start <= stop:
        win_to = min(start + timedelta(days=chunk_days - 1), stop)
        url = _history_url(
            api, key, interval if api == "v3" else v2_name,
            win_to.isoformat(), start.isoformat(),
        )
        status, payload, body = _get(url, headers)
        candles = _candles_of(payload)
        if status != 200:
            print(f"  window {start} .. {win_to}: HTTP {status} {body[:60]}")
        for candle in candles:
            bar = _bar_from_candle(candle)
            if bar is not None:
                out.append(bar)
        start = win_to + timedelta(days=1)
        time.sleep(SLEEP_SECONDS)
    return out


def _bucket(ts: pd.Timestamp, timeframe: str) -> Any:
    return ts.date() if timeframe in DAYLIKE else ts


def _compare(up: list[dict], db: pd.DataFrame, timeframe: str) -> int:
    """Print the reconciliation table; return a process exit code."""
    if not up:
        print("UPSTOX: 0 bars — nothing to reconcile.")
        return 1
    if db.empty:
        print("mStock baseline: 0 rows in market_data_cache for this "
              "symbol/timeframe/window.")
        print(f"Upstox alone: {len(up)} bars, "
              f"{_bucket(up[0]['t'], timeframe)} .. {_bucket(up[-1]['t'], timeframe)}")
        print("No second feed to check these against — verdict is UNVERIFIED, "
              "not VALIDATED.")
        for bar in up[:3] + up[-1:]:
            print(f"  {bar['t']}  o={bar['o']} h={bar['h']} l={bar['l']} "
                  f"c={bar['c']} v={bar['v']}")
        return 0

    #: the cache names its columns in full; Upstox bars use the fetcher's short keys
    FIELDS = (("open", "o"), ("high", "h"), ("low", "l"), ("close", "c"))

    db_map: dict[Any, dict] = {}
    for ts, row in db.iterrows():
        db_map[_bucket(pd.Timestamp(ts), timeframe)] = row.to_dict()
    up_map = {_bucket(bar["t"], timeframe): bar for bar in up}

    common = sorted(set(db_map) & set(up_map))
    only_db = sorted(set(db_map) - set(up_map))
    only_up = sorted(set(up_map) - set(db_map))

    print(f"{'':<22} {'mStock (cache)':>16} {'Upstox':>10}")
    print(f"{'bars':<22} {len(db_map):>16} {len(up_map):>10}")
    print(f"{'matched timestamps':<22} {len(common):>16}")
    print(f"{'only in one feed':<22} {len(only_db):>16} {len(only_up):>10}")
    if only_db:
        print(f"  only in cache : {[str(x)[:19] for x in only_db[:6]]}")
    if only_up:
        print(f"  only in Upstox: {[str(x)[:19] for x in only_up[:6]]}")
    if not common:
        print("\nNo timestamp overlap. Two feeds that never agree on the clock "
              "cannot be compared bar-for-bar — check the interval form and "
              "the session-time convention first.")
        return 1

    worst = {up_key: 0.0 for _, up_key in FIELDS}
    worst_at = {up_key: None for _, up_key in FIELDS}
    sum_abs_close = 0.0
    exact = 0
    for key in common:
        a, b = db_map[key], up_map[key]
        for db_col, up_key in FIELDS:
            diff = abs(float(a[db_col]) - float(b[up_key]))
            if diff > worst[up_key]:
                worst[up_key] = diff
                worst_at[up_key] = (key, float(a[db_col]), float(b[up_key]))
        sum_abs_close += abs(float(a["close"]) - float(b["c"]))
        if all(abs(float(a[c]) - float(b[u])) <= 0.01 for c, u in FIELDS):
            exact += 1

    n = len(common)
    rel = sorted(
        abs(float(db_map[k]["close"]) - float(up_map[k]["c"]))
        / max(abs(float(db_map[k]["close"])), 1.0) * 100.0 for k in common
    )
    worst_bars = sorted(
        common,
        key=lambda k: abs(float(db_map[k]["close"]) - float(up_map[k]["c"])),
        reverse=True,
    )[:4]

    def pct(p):
        return rel[min(int(p / 100.0 * n), n - 1)]

    print(f"\nOn the {n} shared bars (Rs = rupees):")
    print(f"  OHLC identical within Rs 0.01 : {exact}/{n} ({100.0 * exact / n:.1f}%)")
    print(f"  mean |close delta|          : Rs{sum_abs_close / n:.4f}")
    print("  max abs delta per field     : "
          + "  ".join(f"{k}=Rs{v:.4f}" for k, v in worst.items()))
    for db_col, up_key in FIELDS:
        hit = worst_at[up_key]
        if hit and hit[0] not in worst_bars:
            key, a_val, b_val = hit
            print(f"    {up_key} worst at {str(key)[:19]}: "
                  f"mStock=Rs{a_val:.2f} Upstox=Rs{b_val:.2f}")
    print(f"  close delta vs price, p50 / p95 / p99 / max : "
          f"{pct(50):.4f}% / {pct(95):.4f}% / {pct(99):.4f}% / {rel[-1]:.4f}%")
    for cut in (0.05, 0.1, 0.5):
        over = sum(1 for r in rel if r > cut)
        print(f"  bars differing > {cut}% of price : {over}/{n}")
    print("  largest gaps: "
          + "  ".join(
              f"{str(k)[:16]} d=Rs{abs(float(db_map[k]['close']) - float(up_map[k]['c'])):.2f}"
              for k in worst_bars))

    #: NSE large-cap tick is Rs 0.05. "Same minute, different feed" means the
    #: gap is a tick or two of tick-sampling, not a different market print.
    TICK = 0.05
    deltas = sorted(
        abs(float(db_map[k]["close"]) - float(up_map[k]["c"])) for k in common
    )
    over_1t = sum(1 for d in deltas if d > TICK)
    over_5t = sum(1 for d in deltas if d > 5 * TICK)
    print(f"  close gap in ticks (Rs{TICK:5}): >1 tick {over_1t}/{n}   "
          f">5 ticks {over_5t}/{n}   median Rs{deltas[n // 2]:.3f}")

    # Grading is on the gap as a fraction of price, never on tick counts: two
    # feeds sampling the same minute each catch a different trade inside it, so
    # a Rs 0.25 close gap at Rs 1200 is 0.02% and means nothing, while the same
    # Rs 0.25 on a Rs 5 option premium is a 5% misprice.
    if exact == n and not only_db and not only_up:
        print("\nVERDICT: identical — Upstox can serve as a drop-in or as a "
              "second feed that validates mStock.")
    elif pct(99) < 0.05 and rel[-1] < 0.1 and not only_db:
        print("\nVERDICT: the same prints, tick-sampled differently — safe as a "
              "cross-check feed. Not a reason to rewrite stored bars: a "
              "backtest on either feed may fill a limit order a tick apart.")
    elif pct(99) < 0.1 and rel[-1] < 0.5:
        print("\nVERDICT: agrees on the substance, differs on the edges "
              "(see the largest-gaps timestamps) — usable as a second feed, "
              "and the residual bars are worth one look before any switch.")
    else:
        print("\nVERDICT: divergent — do NOT switch this timeframe on Upstox "
              "until the difference is explained (see the max-delta row and "
              "the exclusive timestamps above).")
    return 0


def cmd_bars(args) -> int:
    from backtest.brokers.upstox import resolve_instrument_key
    from backtest.data.db_source import DbSource

    if args.timeframe not in TF_TO_UPSTOX:
        sys.exit(f"unsupported timeframe {args.timeframe!r}; "
                 f"pick one of {', '.join(TF_TO_UPSTOX)}")

    broker = _upstox_broker()
    key = resolve_instrument_key(args.instrument_key or args.symbol, timeout=30.0)
    print(f"symbol {args.symbol} -> {key}  timeframe {args.timeframe}  "
          f"{args.frm} .. {args.to}  api {args.api}")

    up = _fetch_upstox_window(broker, key, args.timeframe, args.frm, args.to,
                              args.chunk_days, args.api)
    print(f"Upstox returned {len(up)} bars.")

    db_symbol = args.db_symbol or args.symbol
    try:
        db = DbSource().get_candles(db_symbol, args.frm, args.to, args.timeframe)
    except ValueError as exc:
        print(f"cache read for {db_symbol} {args.timeframe}: {exc}")
        db = pd.DataFrame()
    print(f"market_data_cache holds {len(db)} bars for {db_symbol}.")
    return _compare(up, db, args.timeframe)


# ---------------------------------------------------------------------------
# 3. chain — option chain + quote quality
# ---------------------------------------------------------------------------


def cmd_chain(args) -> int:
    broker = _upstox_broker() if not args.offline else _offline_broker()
    contracts = broker.get_option_chain(args.underlying)
    if not contracts:
        print(f"No contracts for {args.underlying} (empty master, or the "
              "underlying name matches no rows).")
        return 1

    by_expiry: dict[Any, list] = {}
    for contract in contracts:
        by_expiry.setdefault(contract.expiry, []).append(contract)
    print(f"{args.underlying}: {len(contracts)} contracts across "
          f"{len(by_expiry)} expiries.")
    for expiry in sorted(by_expiry):
        rows = by_expiry[expiry]
        strikes = sorted({float(r.strike) for r in rows})
        print(f"  {expiry}  lots={sorted({r.lot_size for r in rows})}  "
              f"{len(rows)} legs, {len(strikes)} strikes "
              f"({strikes[0]:g} .. {strikes[-1]:g})")
    if args.offline:
        print("\nOffline: master rows only. Quotes need an Upstox session.")
        return 0

    spot = _index_spot(broker, args.underlying)
    if spot is None:
        print("\nNo index quote — cannot place ATM or check intrinsic. "
              "Quoting the nearest-expiry strikes as listed instead.")
    expiry = min(by_expiry)
    picked = _pick_strikes(by_expiry[expiry], spot, args.width, args.limit)
    print(f"\nQuoting {len(picked)} legs of expiry {expiry}"
          + (f" around spot {spot:g}" if spot else ""))
    print(f"{'symbol':<22} {'K':>8} {'ltp':>9} {'bid':>9} {'ask':>9} "
          f"{'intr':>9} {'extr':>9}  flag")
    print("-" * 96)
    marks: dict[float, dict] = {}
    for contract in picked:
        try:
            quote = broker.get_option_quote(contract.instrument_token)
        except Exception as exc:  # noqa: BLE001 — one bad leg must not stop the table
            print(f"{contract.trading_symbol:<22} {'-':>8}  quote failed: {exc}")
            continue
        strike = float(contract.strike)
        ltp = float(quote.get("ltp") or 0.0)
        bid = float(quote.get("bid") or 0.0)
        ask = float(quote.get("ask") or 0.0)
        intr = _intrinsic(spot, strike, contract.option_type)
        extr = ltp - intr if spot else None
        flags = []
        if ltp <= 0:
            flags.append("no-trade")
        if intr is not None and ask and ask < intr - 0.05:
            flags.append("BELOW-INTRINSIC")
        if bid and ask and ask < bid:
            flags.append("INVERTED-BOOK")
        marks.setdefault(strike, {})[contract.option_type] = ltp
        print(f"{contract.trading_symbol:<22} {strike:>8g} {ltp:>9.2f} "
              f"{bid:>9.2f} {ask:>9.2f} "
              + (f"{intr:>9.2f} {extr:>9.2f}  " if intr is not None else f"{'-':>9} {'-':>9}  ")
              + ",".join(flags))
        time.sleep(SLEEP_SECONDS)

    _parity(marks, spot, args.underlying)
    print("\nCompare these marks against the same contracts on mStock "
          "(tools/mticker_ws_probe.py) before trusting either as the book's feed.")
    return 0


def _intrinsic(spot, strike, option_type):
    if spot is None or strike is None:
        return None
    diff = (spot - strike) if option_type == "CE" else (strike - spot)
    return max(diff, 0.0)


def _index_spot(broker, underlying: str):
    """Upstox's own quote for the underlying index — single-source spot.

    ``/v3/market-quote/ltp`` keys its response with a **colon**
    (``NSE_INDEX:Nifty 50``) while the request carries a pipe
    (``NSE_INDEX|Nifty 50``), so read the single value rather than the key.
    """
    from backtest.brokers.upstox import resolve_instrument_key

    try:
        key = resolve_instrument_key(underlying, timeout=30.0)
    except Exception:  # noqa: BLE001
        return None
    status, payload, _ = _get(f"{API_ROOT}/v3/market-quote/ltp?instrument_key={key}",
                              _auth_headers(broker))
    if status != 200 or not isinstance(payload, dict):
        return None
    data = payload.get("data")
    rows = list(data.values()) if isinstance(data, dict) else (data or [])
    for row in rows:
        if isinstance(row, dict):
            price = row.get("last_price")
            if price:
                return float(price)
    return None


def _pick_strikes(rows, spot, width, limit):
    """``width`` strikes either side of ATM, both wings, capped at ``limit`` legs."""
    strikes = sorted({float(r.strike) for r in rows})
    if spot:
        ATM = min(strikes, key=lambda s: abs(s - spot))
        i = strikes.index(ATM)
        strikes = strikes[max(0, i - width): i + width + 1]
    by_key = {}
    for row in rows:
        by_key.setdefault((float(row.strike), row.option_type), row)
    picked = []
    for strike in strikes:
        for side in ("CE", "PE"):
            contract = by_key.get((strike, side))
            if contract is not None:
                picked.append(contract)
    return picked[:limit]


def _parity(marks: dict, spot, underlying: str) -> None:
    """C - P should sit at S - K (undiscounted). A large gap means one wing is
    being marked by a stale or fabricated ladder, which is the defect the
    straddle's fake profit came from."""
    usable = {k: v for k, v in marks.items() if "CE" in v and "PE" in v}
    if not usable or spot is None:
        return
    print(f"\nPut-call parity for {underlying} at spot {spot:g} "
          "(C - P vs S - K):")
    worst = 0.0
    for strike, wings in sorted(usable.items()):
        left = wings["CE"] - wings["PE"]
        right = spot - strike
        gap = abs(left - right)
        worst = max(worst, gap)
        print(f"  K={strike:>8g}  C-P={left:>8.2f}  S-K={right:>8.2f}  gap={gap:.2f}")
    print(f"  worst gap Rs{worst:.2f} — treat gaps of a few percent of spot as "
          "one wing being untraded, not as an edge.")


def _offline_broker():
    """An Upstox broker object with no session, for the master-only chain read."""
    from backtest.brokers.upstox import UpstoxBroker

    # The master is a few MB; the default 10s HTTP timeout is for order calls.
    return UpstoxBroker(http_timeout=30.0)


# ---------------------------------------------------------------------------


def _default_window(days: int) -> tuple[str, str]:
    to = date.today() - timedelta(days=1)
    return (to - timedelta(days=days)).isoformat(), to.isoformat()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("probe", help="which interval forms Upstox accepts")
    p.add_argument("--symbol", default="RELIANCE")
    p.add_argument("--instrument-key", default=None,
                   help="skip resolution, e.g. NSE_FO|NSE_INDEX keys")
    p.add_argument("--api", choices=("v2", "v3", "both"), default="both")
    p.add_argument("--from", dest="frm", default=None)
    p.add_argument("--to", default=None)
    p.add_argument("--days", type=int, default=3)
    p.set_defaults(func=cmd_probe)

    b = sub.add_parser("bars", help="reconcile Upstox bars against the cache")
    b.add_argument("--symbol", required=True)
    b.add_argument("--db-symbol", default=None,
                   help="cache symbol key if it differs from the broker symbol")
    b.add_argument("--instrument-key", default=None)
    b.add_argument("--timeframe", default="1min", choices=sorted(TF_TO_UPSTOX))
    b.add_argument("--from", dest="frm", default=None)
    b.add_argument("--to", default=None)
    b.add_argument("--chunk-days", type=int, default=None)
    b.add_argument("--api", choices=("v2", "v3"), default="v3")
    b.set_defaults(func=cmd_bars)

    c = sub.add_parser("chain", help="Upstox option chain + quote sanity")
    c.add_argument("--underlying", default="NIFTY")
    c.add_argument("--width", type=int, default=2, help="strikes each side of ATM")
    c.add_argument("--limit", type=int, default=10, help="max legs to quote")
    c.add_argument("--offline", action="store_true",
                   help="master rows only, no session needed")
    c.set_defaults(func=cmd_chain)

    args = parser.parse_args(argv)
    if args.cmd in ("probe", "bars") and not (args.frm and args.to):
        args.frm, args.to = _default_window(args.days if args.cmd == "probe" else 3)
    if args.cmd == "bars" and args.chunk_days is None:
        args.chunk_days = 60 if args.timeframe in DAYLIKE else 1
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
