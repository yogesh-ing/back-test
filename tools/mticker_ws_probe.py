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
                auth_instruments: str = "51334") -> dict:
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
                    frame = {"a": "subscribe",
                             "v": [int(t) for t in auth_instruments.split(",") if t.strip()],
                             "ACCESS_TOKEN": auth_token, "API_KEY": auth_key}
                    await ws.send(json.dumps(frame))
                    events.append(f"sent auth-in-frame: {json.dumps({**frame, 'ACCESS_TOKEN': '..masked..'})}")
                elif tokens:
                    frame = {"a": "subscribe",
                             "v": [int(t) for t in tokens.split(",") if t.strip()]}
                    await ws.send(json.dumps(frame))
                    events.append(f"sent subscribe frame: {json.dumps(frame)}")
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
    ap.add_argument("--only", default="",
                    help="comma-separated variant id filter, e.g. --only V2 (no-login baseline)")
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

    ak_q = quote(api_key, safe="")
    tok_q = quote(token or "", safe="")
    base = os.environ.get("MSTOCK_WEBSOCKET_URL", "wss://ws.mstock.trade")
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
                          auth_token=token, auth_key=api_key)
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
