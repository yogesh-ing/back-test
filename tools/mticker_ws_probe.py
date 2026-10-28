#!/usr/bin/env python3
"""Standalone mStock MTicker WebSocket probe (2026-10-22 incident).

The live server's depth socket closes cleanly (code 1000, no reason) every ~3s
right after "connection established" and never delivers a tick, so option
entries fail closed with NO_EXECUTION_QUOTE. This script logs in through the
real broker flow (credential step + one-time TOTP), then replays the MTicker
protocol with the *raw* `websockets` library so every server frame and the
close code/reason are printed verbatim.

Usage (Git Bash, from C:/learning/back-test):
    PYTHONPATH=src python tools/mticker_ws_probe.py --totp 123456
    # optionally: --totp-stdin, --duration 8, --tokens 51334,26000
    # Monday 09:15 production-stack 26-token MODE_FULL soak (no re-login):
    PYTHONPATH=src python tools/mticker_ws_probe.py --from-store --mticker-soak --auto-ring NIFTY --duration 180
    # Or with explicit token list:
    PYTHONPATH=src python tools/mticker_ws_probe.py --from-store --mticker-soak --tokens <26_tokens> --duration 180

WARNING: each login kicks the broker's active session — the running :5000
server (and the mStock app) will need a re-login afterwards.

Never prints the access token or API key.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from urllib.parse import quote

import websockets


def load_env(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def make_client(ws_url: str, duration: float, tokens: str, label: str,
                send_login_first: bool = False, headers: dict | None = None,
                auth_token: str = "", auth_key: str = "",
                auth_instruments: str = "51334",
                stream_mode: str = "") -> dict:
    """Connect once, optionally send the MTicker subscribe frame, dump all frames."""
    events: list[str] = []
    t0 = time.time()
    ws = None

    async def _run() -> None:
        nonlocal ws
        kwargs = dict(ping_interval=None, open_timeout=10, max_size=None)
        if headers is not None:
            kwargs["extra_headers"] = headers
        try:
            async with websockets.connect(ws_url, **kwargs) as ws:
                events.append(f"OPEN handshake ok (+{time.time()-t0:.2f}s)")
                if send_login_first:
                    await ws.send(f"LOGIN:{auth_token}")
                    events.append("sent LOGIN:<access_token> text (send_login_after_connect)")
                if tokens == "__FRAME__":
                    tok_ints = [int(t) for t in auth_instruments.split(",") if t.strip()]
                    frame = {"a": "subscribe",
                             "v": tok_ints,
                             "ACCESS_TOKEN": auth_token, "API_KEY": auth_key}
                    await ws.send(json.dumps(frame))
                    events.append(f"sent auth-in-frame: {json.dumps({**frame, 'ACCESS_TOKEN': '..masked..'})}")
                    if stream_mode and tok_ints:
                        mframe = {"a": "mode", "v": [stream_mode, tok_ints]}
                        await ws.send(json.dumps(mframe))
                        events.append(f"sent mode frame: {json.dumps(mframe)}")
                elif tokens:
                    tok_ints = [int(t) for t in tokens.split(",") if t.strip()]
                    frame = {"a": "subscribe", "v": tok_ints}
                    await ws.send(json.dumps(frame))
                    events.append(f"sent subscribe frame: {json.dumps(frame)}")
                    if stream_mode and tok_ints:
                        mframe = {"a": "mode", "v": [stream_mode, tok_ints]}
                        await ws.send(json.dumps(mframe))
                        events.append(f"sent mode frame: {json.dumps(mframe)}")
                deadline = t0 + duration
                while time.time() < deadline:
                    try:
                        msg = await asyncio.wait_for(ws.recv(),
                                                     timeout=max(0.5, deadline - time.time()))
                    except asyncio.TimeoutError:
                        continue
                    dt = time.time() - t0
                    if isinstance(msg, bytes):
                        events.append(f"BIN  +{dt:.2f}s {len(msg)}B {msg[:200]!r}")
                    else:
                        events.append(f"TXT  +{dt:.2f}s {msg[:400]}")
                await ws.close()
                events.append(f"client closed at {time.time()-t0:.2f}s (held {duration}s)")
        except Exception as exc:  # handshake failure is itself a result
            events.append(f"EXC  +{time.time()-t0:.2f}s {type(exc).__name__}: {exc}")
        if ws is not None:
            code = getattr(ws, "close_code", None) or getattr(getattr(ws, "close", None), "code", None)
            reason = getattr(ws, "close_reason", None) or getattr(getattr(ws, "close", None), "reason", None)
            events.append(f"server close: code={code} reason={reason!r}")

    asyncio.run(_run())
    return {"label": label, "events": events}


def _resolve_auto_ring_tokens(
    api_key: str,
    access_token: str,
    underlyings_csv: str,
) -> list[int]:
    """Resolve live ATM +/- 6 CE/PE option tokens + index tokens via REST."""
    from datetime import datetime, timedelta
    import requests
    from backtest.brokers.mstock import MStockBroker
    from backtest.options.quote_providers import HotStrikeRing

    broker = MStockBroker()
    broker._session_token = access_token
    broker._expires_at = datetime.now() + timedelta(hours=4)

    headers = {"X-Mirae-Version": "1", "Authorization": f"token {api_key}:{access_token}"}
    base_url = broker._base_url()
    idx_query = {"NIFTY": "NSE:NIFTY 50", "BANKNIFTY": "NSE:NIFTY BANK"}

    resolved: list[int] = []
    for raw_u in underlyings_csv.split(","):
        u = raw_u.strip().upper()
        if not u:
            continue
        idx_tok = broker.UNDERLYING_INDEX_TOKENS.get(u)
        if idx_tok:
            resolved.append(idx_tok)

        spot = 0.0
        q_sym = idx_query.get(u, f"NSE:{u}")
        try:
            resp = requests.get(
                f"{base_url}/openapi/typea/instruments/quote/ltp",
                headers=headers,
                params=[("i", q_sym)],
                timeout=10.0,
            )
            data = resp.json().get("data") or {}
            inner = data.get(q_sym) or next(iter(data.values()), {})
            spot = float(inner.get("last_price") or inner.get("ltp") or 0.0)
        except Exception as exc:
            print(f"warn: spot lookup for {u} failed ({exc})", file=sys.stderr)

        contracts = broker.get_option_chain(u)
        if spot <= 0.0 and contracts:
            strikes = sorted({float(c.strike) for c in contracts})
            spot = strikes[len(strikes) // 2]

        captured: list[int] = []

        class _RecorderBroker:
            def subscribe_tokens_ws(self, toks, mode="full"):
                captured.extend(int(t) for t in toks)
                return list(captured)

        ring = HotStrikeRing(_RecorderBroker())
        ring.recenter(u, spot, contracts)
        print(
            f"auto-ring {u}: spot={spot:.2f} center={ring.center_strike(u)} "
            f"ring_tokens={len(captured)}"
        )
        resolved.extend(captured)

    dedup: list[int] = []
    seen: set[int] = set()
    for t in resolved:
        if t not in seen:
            seen.add(t)
            dedup.append(t)
    return dedup


def run_mticker_soak(
    ws_url: str,
    api_key: str,
    access_token: str,
    token_list: list[int],
    duration: float,
    mode: str = "full",
) -> int:
    """Run the real Twisted MTicker + MStockWebSocketWrapper soak test."""
    from twisted.python import log as twisted_log
    from backtest.data.mticker import MTicker
    from backtest.data.mstock_ws_wrapper import MStockWebSocketWrapper

    reactor_errors: list[str] = []
    closes: list[tuple[object, object]] = []
    reconnects: list[int] = []
    tick_counts: dict[str, int] = {}
    depth_counts: dict[str, int] = {}
    frame_times: list[float] = []

    def _tw_observer(event: dict) -> None:
        if event.get("isError"):
            failure = event.get("failure")
            text = str(failure.getTraceback() if failure else event.get("message"))
            reactor_errors.append(text)

    twisted_log.addObserver(_tw_observer)
    ticker = MTicker(
        api_key=api_key,
        access_token=access_token,
        root=ws_url,
        debug=False,
        reconnect=True,
        reconnect_max_tries=10,
        reconnect_max_delay=30,
    )
    wrapper = MStockWebSocketWrapper(ticker, coalesce_ms=150)

    orig_on_ticks = ticker.on_ticks
    orig_on_close = ticker.on_close

    def _counted_ticks(ws, ticks):
        now_m = time.monotonic()
        frame_times.append(now_m)
        for t in ticks or []:
            tok = str(t.get("instrument_token", ""))
            if not tok:
                continue
            tick_counts[tok] = tick_counts.get(tok, 0) + 1
            depth = t.get("depth") or {}
            bids = depth.get("bid") or []
            asks = depth.get("ask") or []
            if bids and asks and (bids[0].get("price") or asks[0].get("price")):
                depth_counts[tok] = depth_counts.get(tok, 0) + 1
        if orig_on_ticks:
            orig_on_ticks(ws, ticks)

    def _counted_close(ws, code, reason):
        closes.append((code, reason))
        if orig_on_close:
            orig_on_close(ws, code, reason)

    ticker.on_ticks = _counted_ticks
    ticker.on_close = _counted_close
    ticker.on_reconnect = lambda attempt: reconnects.append(int(attempt))

    subscribed_once = False

    def _on_open(tkr) -> None:
        nonlocal subscribed_once
        tkr.send_login_after_connect()
        if not subscribed_once:
            subscribed_once = True
            tkr.subscribe(token_list)
            mode_map = {
                "ltp": tkr.MODE_LTP,
                "quote": tkr.MODE_QUOTE,
                "full": tkr.MODE_FULL,
            }
            tkr.set_mode(mode_map.get(mode.lower(), tkr.MODE_FULL), token_list)

    ticker.on_open = _on_open
    print(
        f"\n=== MTicker Production Soak: {len(token_list)} tokens, mode={mode}, "
        f"duration={duration:.0f}s ==="
    )
    ticker.connect(threaded=True)
    if not wrapper.wait_for_connection(timeout=10.0):
        print("FAIL: MTicker did not connect within 10s", file=sys.stderr)
        wrapper.close()
        return 5

    t_start = time.monotonic()
    deadline = t_start + duration
    next_report = t_start + 30.0
    while time.monotonic() < deadline:
        time.sleep(0.5)
        now_m = time.monotonic()
        if now_m >= next_report:
            print(
                f"  +{now_m - t_start:5.1f}s  frames={len(frame_times)} "
                f"tokens_seen={len(tick_counts)}/{len(token_list)} "
                f"depth_tokens={len(depth_counts)} closes={len(closes)} "
                f"reactor_errors={len(reactor_errors)}"
            )
            next_report += 30.0

    # Compute max silence gap across the socket
    gaps: list[float] = []
    if frame_times:
        gaps.append(frame_times[0] - t_start)
        for i in range(1, len(frame_times)):
            gaps.append(frame_times[i] - frame_times[i - 1])
    max_gap = max(gaps) if gaps else duration

    missing = [t for t in token_list if str(t) not in tick_counts]
    wrapper.close()
    twisted_log.removeObserver(_tw_observer)

    print("\n=== MTICKER SOAK SUMMARY ===")
    print(f"  Tokens subscribed : {len(token_list)}")
    print(f"  Tokens w/ frames  : {len(tick_counts)}/{len(token_list)}")
    print(f"  Tokens w/ depth   : {len(depth_counts)}")
    print(f"  Total binary msgs : {len(frame_times)}")
    print(f"  Max silence gap   : {max_gap:.2f}s")
    print(f"  Closes / flaps    : {len(closes)} {closes[:3]}")
    print(f"  Reconnects        : {len(reconnects)}")
    print(f"  Reactor errors    : {len(reactor_errors)}")
    if missing:
        print(f"  Quiet/no-tick toks: {missing}")
    if reactor_errors:
        for err in reactor_errors[:3]:
            print("  REACTOR TRACEBACK:\n", err)

    passed = (len(closes) == 0 and len(reactor_errors) == 0 and len(frame_times) > 0)
    print(f"  VERDICT           : {'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--totp", help="6-digit TOTP (single-use; a fresh login is done)")
    ap.add_argument("--totp-stdin", action="store_true", help="read TOTP from stdin")
    ap.add_argument("--token-file", help="use an access token from this file; skip login")
    ap.add_argument("--from-store", action="store_true",
                    help="use the remembered mStock session (.broker_sessions.json); no login")
    ap.add_argument("--duration", type=float, default=8.0, help="seconds to hold each socket open")
    ap.add_argument("--tokens", default="51334,26000",
                    help="comma-separated instrument tokens for the subscribe frame")
    ap.add_argument("--auto-ring", default="",
                    help="auto-resolve live ATM+/-6 ring tokens for underlying(s), e.g. NIFTY or NIFTY,BANKNIFTY")
    ap.add_argument("--mode", default="",
                    help="optional mode frame ('full', 'quote', 'ltp'); default 'full' under --mticker-soak")
    ap.add_argument("--mticker-soak", action="store_true",
                    help="run the real Twisted MTicker + MStockWebSocketWrapper soak instead of raw protocol matrix")
    ap.add_argument("--only", default="",
                    help="comma-separated variant id filter, e.g. --only V10 (LOGIN+subscribe)")
    args = ap.parse_args()

    load_env()
    api_key = os.environ.get("MSTOCK_API_KEY", "")
    if not api_key:
        print("MSTOCK_API_KEY missing from .env", file=sys.stderr)
        return 2

    if args.from_store:
        from backtest.brokers.remember_session import load_session

        rec = load_session("mstock") or {}
        token = str(rec.get("token") or "")
        if not token:
            print("no remembered mStock session in .broker_sessions.json", file=sys.stderr)
            return 4
        print("using remembered session token "
              f"({len(token)} chars, expires {rec.get('expires_at')}) — no login, session untouched")
    elif args.token_file:
        token = open(args.token_file).read().strip()
        print("using token from file — no login performed (app session untouched)")
    elif args.only and all(v.strip().upper() in ("V2",) for v in args.only.split(",")):
        token = "probe-no-token"
        print("no-auth baseline requested — skipping login (app session untouched)")
    else:
        if not args.totp and not args.totp_stdin:
            print("need --totp or --totp-stdin or --token-file", file=sys.stderr)
            return 2
        code = args.totp or sys.stdin.readline().strip()

        from backtest.brokers.mstock import MStockBroker

        broker = MStockBroker()
        broker._init_websocket_feed = lambda access_token: None  # keep the probe's socket single
        user = os.environ.get("MSTOCK_USERNAME", "")
        pwd = os.environ.get("MSTOCK_PASSWORD", "")
        r = broker.login(user, pwd)
        print("step1 login:", r.get("success"), "-", r.get("message"))
        if not r.get("success"):
            return 3
        r2 = broker.verify_totp(code)
        print("step2 verify_totp:", r2.get("success"), "-", r2.get("message"))
        if not r2.get("success"):
            return 3
        token = broker._session_token
        print(f"access token captured ({len(token or '')} chars) — session now ACTIVE, "
              "the :5000 server/app session was kicked")

    base = os.environ.get("MSTOCK_WEBSOCKET_URL", "wss://ws.mstock.trade")
    if args.auto_ring:
        resolved_ring = _resolve_auto_ring_tokens(api_key, token or "", args.auto_ring)
        if not resolved_ring:
            print("auto-ring resolved 0 tokens", file=sys.stderr)
            return 6
        args.tokens = ",".join(str(t) for t in resolved_ring)

    if args.mticker_soak:
        tok_ints = [int(t) for t in args.tokens.split(",") if t.strip()]
        return run_mticker_soak(
            ws_url=base,
            api_key=api_key,
            access_token=token or "",
            token_list=tok_ints,
            duration=args.duration,
            mode=args.mode or "full",
        )

    ak_q = quote(api_key, safe="")
    tok_q = quote(token or "", safe="")
    # Production builds the URL with plain %-NO formatting (mticker.py:261):
    # a '+' in the key crosses the wire as a space. V1b replicates it exactly.
    ak_raw = api_key
    tok_raw = token or ""

    variants = [
        ("V1 AUTH + subscribe (MTicker exactly as built in prod, raw params)",
         f"{base}?ACCESS_TOKEN={tok_raw}&API_KEY={ak_raw}", args.tokens, None, False),
        ("V5 Authorization header on bare url (REST auth style)",
         base, args.tokens, {"Authorization": f"token {api_key}:{token}"}, False),
        ("V6 auth inside subscribe frame, bare url",
         base, "__FRAME__", None, False),
        ("V7 ACCESS_TOKEN raw only (no API_KEY param)",
         f"{base}?ACCESS_TOKEN={tok_raw}", args.tokens, None, False),
        ("V8 API_KEY raw only + subscribe (no token)",
         f"{base}?API_KEY={ak_raw}", args.tokens, None, False),
        ("V9 passive V1 url (does the close depend on subscribing?)",
         f"{base}?ACCESS_TOKEN={tok_raw}&API_KEY={ak_raw}", "", None, False),
        ("V10 V1 url + LOGIN:<token> text then subscribe (send_login_after_connect)",
         f"{base}?ACCESS_TOKEN={tok_raw}&API_KEY={ak_raw}", args.tokens, None, True),
        ("V11 passive V1 url + LOGIN:<token> only (no subscribe)",
         f"{base}?ACCESS_TOKEN={tok_raw}&API_KEY={ak_raw}", "", None, True),
    ]

    if args.only:
        keep = {v.strip().upper() for v in args.only.split(",")}
        variants = [v for v in variants if v[0].split()[0].upper() in keep]
        if not variants:
            print("no variants matched --only", file=sys.stderr)
            return 2

    results = []
    for label, url, toks, hdrs, login_first in variants:
        if toks == "__FRAME__":
            hdrs = None
            login_first = False
        print(f"\n=== {label} ===")
        res = make_client(url, args.duration, toks, label,
                          send_login_first=login_first, headers=hdrs,
                          auth_token=token, auth_key=api_key,
                          stream_mode=args.mode)
        for e in res["events"]:
            print("   ", e)
        results.append((label, res["events"]))

    print("\n=== SUMMARY ===")
    for label, evs in results:
        held = any("client closed" in e for e in evs)
        got_data = any(e.startswith(("TXT", "BIN")) for e in evs)
        exc = next((e for e in evs if e.startswith("EXC")), "")
        close = next((e for e in evs if e.startswith("server close")), "")
        verdict = "HELD" if held else ("EXC" if exc else "CLOSED-EARLY")
        print(f"  {verdict:14s} data={'Y' if got_data else 'N'}  {label}  {close} {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
