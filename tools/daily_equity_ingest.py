#!/usr/bin/env python3
"""Phase 5.0 CLI — NSE 1day equity-bar rollup, pilot ingest & day-by-day verifier.

Uses ``DatabaseManager.from_env(pool_size=1, max_overflow=0)`` so standalone runs
never starve the running Flask server's Postgres pool, and writes exclusively via
TimescaleDB-safe ``ON CONFLICT DO NOTHING`` (zero cross-chunk ``UPDATE``s).

Examples
--------
1. Roll up existing ``1min`` bars in ``market_data_cache`` into ``1day`` (zero HTTP):
   ``python tools/daily_equity_ingest.py rollup --universe NIFTY_50 --days 90 --apply``

2. Run a small 5-symbol mStock historical ``1day`` pilot (<=10 symbols, <=120 days):
   ``python tools/daily_equity_ingest.py fetch --days 60 --apply``

3. Verify day-by-day NSE session completeness (exits non-zero on any missing session):
   ``python tools/daily_equity_ingest.py verify --symbols RELIANCE,TCS,INFY --days 60``

4. Nightly post-close job (1min->1day rollup + day-by-day verification):
   ``python tools/daily_equity_ingest.py nightly --universe NIFTY_50 --days 7 --apply``
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from backtest.data.daily_bar_ingest import (  # noqa: E402
    DEFAULT_MIN_INTRADAY_BARS,
    DEFAULT_PILOT_SYMBOLS,
    IST_FIXED,
    PILOT_MAX_CALENDAR_DAYS,
    PILOT_MAX_SYMBOLS,
    BulkIngestGatedError,
    DailyCoverageAuditReport,
    ingest_mstock_daily,
    parse_iso_date,
    rollup_1min_to_1day,
    verify_daily_coverage,
)
from backtest.data.universe import get_universe_symbols  # noqa: E402
from backtest.db.manager import DatabaseManager  # noqa: E402


def _load_dotenv_if_present() -> None:
    env_file = ROOT / ".env"
    if not env_file.is_file():
        return
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        key = k.strip()
        val = v.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def _resolve_symbols(symbols_arg: str | None, universe_arg: str | None) -> list[str]:
    if symbols_arg:
        return [
            s.strip().upper()
            for s in symbols_arg.split(",")
            if s.strip()
        ]
    if universe_arg:
        return get_universe_symbols(universe_arg)
    return list(DEFAULT_PILOT_SYMBOLS)


def _resolve_date_range(
    from_arg: str | None,
    to_arg: str | None,
    days_arg: int,
) -> tuple[date, date]:
    today_ist = datetime.now(timezone.utc).astimezone(IST_FIXED).date()
    end_d = parse_iso_date(to_arg) if to_arg else today_ist
    if from_arg:
        start_d = parse_iso_date(from_arg)
    else:
        span = max(1, int(days_arg))
        start_d = end_d - timedelta(days=span - 1)
    return start_d, end_d


def _resolve_mstock_token() -> str:
    tok = os.getenv("MSTOCK_ACCESS_TOKEN", "").strip()
    if tok:
        return tok
    token_file = ROOT / ".mstock_session_token"
    if token_file.is_file():
        cached = token_file.read_text(encoding="utf-8").strip()
        if cached:
            return cached
    return ""


@contextmanager
def _managed_engine(injected_engine: Any = None) -> Iterator[Any]:
    if injected_engine is not None:
        yield injected_engine
        return
    with DatabaseManager.from_env(pool_size=1, max_overflow=0) as db:
        yield db.engine


def _print_coverage_audit(audit: DailyCoverageAuditReport) -> None:
    status_str = "PASS" if audit.passed else "FAIL (MISSING SESSIONS)"
    print(
        f"\n=== Day-by-Day 1day Coverage Audit [{audit.from_date} .. {audit.to_date}] "
        f"=> {status_str} ==="
    )
    print(
        f"Expected sealed NSE sessions: {len(audit.expected_trading_dates)} | "
        f"Symbols complete: {audit.symbols_complete}/{len(audit.symbols)}"
    )
    for item in audit.symbols:
        badge = "OK  " if item.passed else "MISS"
        miss_preview = ", ".join(item.missing_dates[:5])
        if len(item.missing_dates) > 5:
            miss_preview += f" (+{len(item.missing_dates) - 5} more)"
        extra = f" | missing=[{miss_preview}]" if item.missing_dates else ""
        if item.invalid_ohlc_dates:
            extra += f" | invalid_ohlc={item.invalid_ohlc_dates}"
        if item.duplicate_dates:
            extra += f" | duplicates={item.duplicate_dates}"
        print(
            f"  [{badge}] {item.symbol:<12} {item.present_sessions:>3}/"
            f"{item.expected_sessions:<3} ({item.coverage_pct:>6.2f}%)"
            f"{extra}"
        )


def _write_json_out(path_str: str | None, payload: dict[str, Any]) -> None:
    if not path_str:
        return
    out_path = Path(path_str)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _add_window_args(sub: argparse.ArgumentParser, default_days: int = 60) -> None:
    sub.add_argument("--symbols", help="Comma-separated NSE tickers (default: 5-stock pilot)")
    sub.add_argument("--universe", help="Registered universe alias (e.g. NIFTY_50)")
    sub.add_argument("--from", dest="from_date", help="Start date YYYY-MM-DD")
    sub.add_argument("--to", dest="to_date", help="End date YYYY-MM-DD (default: today IST)")
    sub.add_argument(
        "--days",
        type=int,
        default=default_days,
        help=f"Trailing calendar days when --from is omitted (default: {default_days})",
    )
    sub.add_argument("--json-out", help="Write structured JSON report to path")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    subparsers = parser.add_subparsers(dest="command", required=True)

    # 1. rollup
    p_rollup = subparsers.add_parser(
        "rollup",
        help="Aggregate complete 1min sessions in market_data_cache into 1day (zero HTTP)",
    )
    _add_window_args(p_rollup, default_days=90)
    p_rollup.add_argument(
        "--min-intraday-bars",
        type=int,
        default=DEFAULT_MIN_INTRADAY_BARS,
        help=(
            "Min 1min bars/day required to seal a 1day bar "
            f"(default: {DEFAULT_MIN_INTRADAY_BARS})"
        ),
    )
    p_rollup.add_argument(
        "--apply",
        action="store_true",
        help="Persist rolled-up 1day bars (default is dry-run)",
    )
    p_rollup.add_argument(
        "--allow-gaps",
        action="store_true",
        help="Return exit code 0 even when post-rollup day-by-day audit still finds gaps",
    )

    # 2. fetch (pilot-gated by default)
    p_fetch = subparsers.add_parser(
        "fetch",
        help=(
            f"Fetch missing 1day bars from mStock (pilot-gated to <={PILOT_MAX_SYMBOLS} "
            f"symbols & <={PILOT_MAX_CALENDAR_DAYS}d unless --allow-bulk)"
        ),
    )
    _add_window_args(p_fetch, default_days=60)
    p_fetch.add_argument(
        "--allow-bulk",
        action="store_true",
        help="Unlock >10 symbols or >120d multi-year backfill (USE ONLY AFTER MONDAY SOAK)",
    )
    p_fetch.add_argument(
        "--chunk-days",
        type=int,
        default=180,
        help="Calendar days per mStock historical HTTP request (default: 180)",
    )
    p_fetch.add_argument(
        "--delay",
        type=float,
        default=0.6,
        help="Base pacing delay in seconds between HTTP requests (default: 0.6)",
    )
    p_fetch.add_argument(
        "--checkpoint",
        default=str(ROOT / "tools" / "out" / "daily_ingest_checkpoint.json"),
        help="Path to write incremental JSON checkpoint",
    )
    p_fetch.add_argument(
        "--apply",
        action="store_true",
        help="Execute live HTTP fetch + DB insert (default is dry-run)",
    )

    # 3. verify
    p_verify = subparsers.add_parser(
        "verify",
        help="Verify stored 1day bars day-by-day against the NSE trading calendar",
    )
    _add_window_args(p_verify, default_days=60)

    # 4. nightly
    p_nightly = subparsers.add_parser(
        "nightly",
        help="Post-close 1min->1day rollup + optional pilot gap-fill + day-by-day verify",
    )
    _add_window_args(p_nightly, default_days=7)
    p_nightly.add_argument(
        "--min-intraday-bars",
        type=int,
        default=DEFAULT_MIN_INTRADAY_BARS,
    )
    p_nightly.add_argument(
        "--with-broker-gapfill",
        action="store_true",
        help="Attempt mStock 1day gap-fill for symbols still missing sessions (<=10 pilot cap)",
    )
    p_nightly.add_argument(
        "--allow-bulk",
        action="store_true",
        help="Allow >10 gap-fill symbols when --with-broker-gapfill is set",
    )
    p_nightly.add_argument(
        "--apply",
        action="store_true",
        help="Persist bars (default is dry-run)",
    )
    return parser


def main(
    argv: list[str] | None = None,
    *,
    engine: Any = None,
    http_get: Any = None,
    token_resolver: Any = None,
    now_utc: datetime | None = None,
) -> int:
    _load_dotenv_if_present()
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        symbols = _resolve_symbols(args.symbols, args.universe)
        start_d, end_d = _resolve_date_range(args.from_date, args.to_date, args.days)
    except Exception as exc:  # noqa: BLE001
        print(f"[ERROR] Invalid arguments: {exc}", file=sys.stderr)
        return 2

    with _managed_engine(engine) as eng:
        if args.command == "verify":
            audit = verify_daily_coverage(
                eng, symbols, start_d, end_d, now_utc=now_utc
            )
            _print_coverage_audit(audit)
            _write_json_out(args.json_out, {"audit": audit.to_dict()})
            return 0 if audit.passed else 1

        if args.command == "rollup":
            dry_run = not bool(args.apply)
            rep = rollup_1min_to_1day(
                eng,
                symbols,
                start_d,
                end_d,
                min_intraday_bars=args.min_intraday_bars,
                dry_run=dry_run,
                now_utc=now_utc,
            )
            mode_tag = "DRY-RUN" if dry_run else "APPLIED"
            print(
                f"[{mode_tag}] 1min->1day rollup {rep.from_date}..{rep.to_date}: "
                f"inserted={rep.total_inserted}, skipped_conflict={rep.total_skipped_conflict}, "
                f"elapsed={rep.elapsed_ms:.1f}ms"
            )
            for s_rep in rep.symbols:
                if s_rep.partial_intraday_days:
                    print(
                        f"  [WARN] {s_rep.symbol}: skipped thin 1min sessions "
                        f"(<{rep.min_intraday_bars} bars): {s_rep.partial_intraday_days}"
                    )
            if dry_run:
                _write_json_out(args.json_out, {"rollup": rep.to_dict()})
                return 0
            audit = verify_daily_coverage(
                eng, symbols, start_d, end_d, now_utc=now_utc
            )
            _print_coverage_audit(audit)
            _write_json_out(
                args.json_out, {"rollup": rep.to_dict(), "audit": audit.to_dict()}
            )
            if not audit.passed and not args.allow_gaps:
                return 1
            return 0

        if args.command == "fetch":
            dry_run = not bool(args.apply)
            try:
                fetch_rep = ingest_mstock_daily(
                    eng,
                    symbols,
                    start_d,
                    end_d,
                    access_token=_resolve_mstock_token() or None,
                    allow_bulk=bool(args.allow_bulk),
                    dry_run=dry_run,
                    chunk_days=args.chunk_days,
                    base_delay_s=args.delay,
                    checkpoint_path=args.checkpoint if not dry_run else None,
                    http_get=http_get,
                    token_resolver=token_resolver,
                    sleep_fn=(
                        (lambda _s: None)
                        if http_get is not None
                        else __import__("time").sleep
                    ),
                    now_utc=now_utc,
                )
            except BulkIngestGatedError as exc:
                print(f"[GATED] {exc}", file=sys.stderr)
                return 3

            mode_tag = "DRY-RUN" if dry_run else "APPLIED"
            print(
                f"[{mode_tag}] mStock 1day fetch {fetch_rep.from_date}..{fetch_rep.to_date} "
                f"(pilot={fetch_rep.pilot_mode}): http_requests={fetch_rep.http_requests}, "
                f"inserted={fetch_rep.total_inserted}, "
                f"skipped_conflict={fetch_rep.total_skipped_conflict}, "
                f"breaker_tripped={fetch_rep.circuit_breaker_tripped}"
            )
            for s_rep in fetch_rep.symbols:
                if s_rep.error or s_rep.failed_windows or s_rep.silent_empty_windows:
                    print(
                        f"  [WARN] {s_rep.symbol}: error={s_rep.error!r} "
                        f"failed_windows={s_rep.failed_windows} "
                        f"silent_empty={s_rep.silent_empty_windows}"
                    )
            if dry_run:
                _write_json_out(args.json_out, {"fetch": fetch_rep.to_dict()})
                return 0

            audit = verify_daily_coverage(
                eng, symbols, start_d, end_d, now_utc=now_utc
            )
            _print_coverage_audit(audit)
            _write_json_out(
                args.json_out, {"fetch": fetch_rep.to_dict(), "audit": audit.to_dict()}
            )
            if not fetch_rep.all_windows_clean or not audit.passed:
                return 1
            return 0

        if args.command == "nightly":
            dry_run = not bool(args.apply)
            roll_rep = rollup_1min_to_1day(
                eng,
                symbols,
                start_d,
                end_d,
                min_intraday_bars=args.min_intraday_bars,
                dry_run=dry_run,
                now_utc=now_utc,
            )
            print(
                f"[NIGHTLY {'DRY-RUN' if dry_run else 'APPLIED'}] rollup inserted="
                f"{roll_rep.total_inserted}, skipped_conflict={roll_rep.total_skipped_conflict}"
            )
            if dry_run:
                _write_json_out(args.json_out, {"rollup": roll_rep.to_dict()})
                return 0

            audit = verify_daily_coverage(
                eng, symbols, start_d, end_d, now_utc=now_utc
            )
            fetch_payload: dict[str, Any] | None = None
            incomplete_syms = [s.symbol for s in audit.symbols if not s.passed]
            if incomplete_syms and args.with_broker_gapfill:
                try:
                    gap_rep = ingest_mstock_daily(
                        eng,
                        incomplete_syms,
                        start_d,
                        end_d,
                        access_token=_resolve_mstock_token() or None,
                        allow_bulk=bool(args.allow_bulk),
                        dry_run=False,
                        http_get=http_get,
                        token_resolver=token_resolver,
                        sleep_fn=(
                            (lambda _s: None)
                            if http_get is not None
                            else __import__("time").sleep
                        ),
                        now_utc=now_utc,
                    )
                    fetch_payload = gap_rep.to_dict()
                except BulkIngestGatedError as exc:
                    print(f"[GATED] Nightly broker gap-fill refused: {exc}", file=sys.stderr)
                    return 3
                audit = verify_daily_coverage(
                    eng, symbols, start_d, end_d, now_utc=now_utc
                )

            _print_coverage_audit(audit)
            out_doc: dict[str, Any] = {
                "rollup": roll_rep.to_dict(),
                "audit": audit.to_dict(),
            }
            if fetch_payload is not None:
                out_doc["gapfill_fetch"] = fetch_payload
            _write_json_out(args.json_out, out_doc)
            return 0 if audit.passed else 1

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
