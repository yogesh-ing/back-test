from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from backtest.data.source_tags import app_source_tag
from backtest.engine.backtester import BacktestConfig
from backtest.engine.plotting import plot_comparison, plot_result
from backtest.logging_config import configure_logging
from backtest.runner import RunSpec, build_source, compare_strategies, run_on_source
from backtest.strategy.registry import get_strategy, list_strategies


def _print_metrics(result):
    m = result.metrics
    print(
        f"strategy={m.get('strategy', 'unknown')} symbol={m.get('symbol', 'NA')} final_equity={m.get('final_equity', 0):.2f} total_return={m.get('total_return', 0):.4f} sharpe={m.get('sharpe', 0):.4f}"
    )
    if m.get("stop_loss") is not None or m.get("take_profit") is not None:
        print(f"Risk: stop_loss={m.get('stop_loss')} take_profit={m.get('take_profit')}")


def list_command(args):
    for name in list_strategies():
        strategy_cls = get_strategy(name)
        doc = (strategy_cls.__doc__ or "").strip().splitlines()[0] if strategy_cls.__doc__ else ""
        params = getattr(strategy_cls, "params", {})
        summary = f"  {params}" if params else ""
        print(f"{name}{summary}")
        if doc:
            print(f"  {doc}")


def preflight_command(args):
    from backtest.live.preflight import print_preflight

    exit_code = print_preflight()
    raise SystemExit(exit_code)


def run_command(args):
    source = build_source(args.source, data_root=args.data_root)
    spec = RunSpec(
        strategy=args.strategy,
        symbol=args.symbol,
        start=args.from_date,
        end=args.to_date,
        interval=args.interval,
    )
    cfg = BacktestConfig(
        initial_capital=args.capital,
        commission_pct=args.commission,
        slippage_pct=args.slippage,
        stop_loss=args.stop_loss,
        take_profit=args.take_profit,
    )
    result = run_on_source(source, spec, cfg)
    _print_metrics(result)
    if (args.plot or not args.no_chart) and not args.json:
        default_path = f"charts/{args.strategy}_{args.symbol}_{args.interval}_{int(datetime.now().timestamp())}.png"
        out = args.plot if isinstance(args.plot, str) else default_path
        if args.chart_dir:
            out = f"{args.chart_dir}/{args.strategy}_{args.symbol}_{args.interval}_{int(datetime.now().timestamp())}.png"
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        plot_result(result, path=out)
        print(f"chart={out}")
    if args.json:
        print(json.dumps(result.metrics, indent=2, default=str))


def compare_command(args):
    source = build_source(args.source, data_root=args.data_root)
    names = [n.strip() for n in args.strategies.split(",") if n.strip()]
    cfg = BacktestConfig(
        initial_capital=args.capital,
        commission_pct=args.commission,
        slippage_pct=args.slippage,
        stop_loss=args.stop_loss,
        take_profit=args.take_profit,
    )
    results = compare_strategies(
        source, args.symbol, args.from_date, args.to_date, names, args.interval, config=cfg
    )
    rows = []
    for name, result in results.items():
        metric = result.metrics
        rows.append(
            {
                "name": name,
                "sharpe": metric.get("sharpe", 0),
                "cagr": metric.get("cagr", 0),
                "total_return": metric.get("total_return", 0),
                "final_equity": metric.get("final_equity", 0),
            }
        )
    sort_by = args.sort_by or "sharpe"
    reverse = sort_by != "volatility"
    rows.sort(key=lambda item: item[sort_by], reverse=reverse)
    for row in rows:
        print(
            f"{row['name']} sharpe={row['sharpe']:.4f} total_return={row['total_return']:.4f} final_equity={row['final_equity']:.2f}"
        )
    if args.json:
        print(json.dumps(rows, indent=2, default=str))
    if not args.no_chart:
        out = (
            args.chart_dir
            and f"{args.chart_dir}/compare_{args.symbol}_{args.interval}_{int(datetime.now().timestamp())}.png"
            or f"charts/compare_{args.symbol}_{args.interval}_{int(datetime.now().timestamp())}.png"
        )
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        plot_comparison({name: results[name] for name in results}, path=out)
        print(f"chart={out}")


def papertrade_command(args):
    from backtest.forward.paper_runner import run_live_papertrade, run_walkforward

    if args.mode == "walkforward":
        if not args.from_date or not args.to_date:
            raise ValueError("--from and --to required for walkforward mode")
        source = build_source(args.source, data_root=args.data_root)
        # Ticket #10: surface the canonical taxonomy tag at the point of use
        # (imported, never re-declared) — the CLI/UI must not hide which
        # classification a run was started under.
        print(f"data_source_tag={app_source_tag(args.source)} (--source={args.source})")
        names = [n.strip() for n in args.strategies.split(",") if n.strip()]
        allocations = {name: args.capital for name in names}
        result = run_walkforward(
            source,
            names,
            args.symbol,
            args.from_date,
            args.to_date,
            allocations=allocations,
            interval=args.interval,
        )
        for name, equity_list in result["equity"].items():
            final = equity_list[-1] if len(equity_list) > 0 else args.capital
            pnl_pct = (final - args.capital) / args.capital * 100
            print(f"strategy={name} final_equity={final:.2f} return={pnl_pct:.2f}%")
    elif args.mode == "live":
        if not args.from_date or not args.to_date:
            raise ValueError("--from and --to required for live mode")
        state_file = args.state_file or ".live_papertrade_state.json"
        source = build_source(args.source, data_root=args.data_root)
        # Ticket #10: surface the canonical taxonomy tag at the point of use
        # (imported, never re-declared) — the CLI/UI must not hide which
        # classification a run was started under.
        print(f"data_source_tag={app_source_tag(args.source)} (--source={args.source})")
        names = [n.strip() for n in args.strategies.split(",") if n.strip()]
        allocations = {name: args.capital for name in names}
        result = run_live_papertrade(
            source,
            names,
            args.symbol,
            allocations=allocations,
            from_date=args.from_date,
            to_date=args.to_date,
            interval=args.interval,
            state_file=state_file,
            poll_interval_s=args.poll_seconds,
            resume_on_start=args.resume_on_start,
        )
        for name, equity_list in result["equity"].items():
            final = equity_list[-1] if len(equity_list) > 0 else args.capital
            pnl_pct = (final - args.capital) / args.capital * 100
            print(f"strategy={name} final_equity={final:.2f} return={pnl_pct:.2f}%")
        print(
            f"state_file={state_file} processed_bars={result['state'].get('processed_bars', 0)} resume_count={result['state'].get('resume_count', 0)}"
        )
    else:
        raise ValueError(f"Unknown mode: {args.mode}")


def build_parser():
    # Diagnostics live on a parent parser so they work after the subcommand
    # (`backtest run … --log-level DEBUG`), which is where people type them.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--log-level",
        default="WARNING",
        help="DEBUG | INFO | WARNING (default) | ERROR — go to DEBUG to see the "
        "strategy/engine path and the traceback of any failure",
    )
    common.add_argument("--log-file", default=None, help="also append log lines to this file")

    parser = argparse.ArgumentParser(prog="backtest", parents=[common])
    sub = parser.add_subparsers(dest="command", required=True)

    list_parser = sub.add_parser("list", parents=[common])
    list_parser.set_defaults(func=list_command)

    preflight_parser = sub.add_parser("preflight", parents=[common])
    preflight_parser.set_defaults(func=preflight_command)

    run_parser = sub.add_parser("run", parents=[common])
    run_parser.add_argument("--strategy", required=True)
    run_parser.add_argument("--source", default="synthetic")
    run_parser.add_argument("--symbol", default="DEMO")
    run_parser.add_argument("--from", dest="from_date", required=True)
    run_parser.add_argument("--to", dest="to_date", required=True)
    run_parser.add_argument("--interval", default="1day")
    run_parser.add_argument("--capital", type=float, default=100_000.0)
    run_parser.add_argument("--commission", type=float, default=0.0003)
    run_parser.add_argument("--slippage", type=float, default=0.0005)
    run_parser.add_argument("--stop-loss", type=float, default=None)
    run_parser.add_argument("--take-profit", type=float, default=None)
    run_parser.add_argument("--param", action="append", default=[])
    run_parser.add_argument("--data-root", default="data")
    run_parser.add_argument("--json", action="store_true")
    run_parser.add_argument("--plot", nargs="?", const="auto", default=None)
    run_parser.add_argument("--chart-dir", default=None)
    run_parser.add_argument("--no-chart", action="store_true")
    run_parser.set_defaults(func=run_command)

    compare_parser = sub.add_parser("compare", parents=[common])
    compare_parser.add_argument("--strategies", required=True)
    compare_parser.add_argument("--source", default="synthetic")
    compare_parser.add_argument("--symbol", default="DEMO")
    compare_parser.add_argument("--from", dest="from_date", required=True)
    compare_parser.add_argument("--to", dest="to_date", required=True)
    compare_parser.add_argument("--interval", default="1day")
    compare_parser.add_argument("--capital", type=float, default=100_000.0)
    compare_parser.add_argument("--commission", type=float, default=0.0003)
    compare_parser.add_argument("--slippage", type=float, default=0.0005)
    compare_parser.add_argument("--stop-loss", type=float, default=None)
    compare_parser.add_argument("--take-profit", type=float, default=None)
    compare_parser.add_argument("--data-root", default="data")
    compare_parser.add_argument(
        "--sort-by",
        choices=[
            "sharpe",
            "cagr",
            "total_return",
            "max_drawdown",
            "calmar",
            "win_rate",
            "volatility",
            "num_trades",
        ],
        default="sharpe",
    )
    compare_parser.add_argument("--json", action="store_true")
    compare_parser.add_argument("--chart-dir", default=None)
    compare_parser.add_argument("--no-chart", action="store_true")
    compare_parser.set_defaults(func=compare_command)

    papertrade_parser = sub.add_parser("papertrade", parents=[common])
    papertrade_parser.add_argument("--mode", choices=["walkforward", "live"], default="walkforward")
    papertrade_parser.add_argument("--strategies", required=True)
    papertrade_parser.add_argument("--source", default="synthetic")
    papertrade_parser.add_argument("--symbol", default="DEMO")
    papertrade_parser.add_argument("--interval", default="1day")
    papertrade_parser.add_argument("--from", dest="from_date", default=None)
    papertrade_parser.add_argument("--to", dest="to_date", default=None)
    papertrade_parser.add_argument("--capital", type=float, default=100_000.0)
    papertrade_parser.add_argument("--commission", type=float, default=0.0003)
    papertrade_parser.add_argument("--slippage", type=float, default=0.0005)
    papertrade_parser.add_argument("--stop-loss", type=float, default=None)
    papertrade_parser.add_argument("--take-profit", type=float, default=None)
    papertrade_parser.add_argument("--state-file", default=None)
    papertrade_parser.add_argument("--poll-seconds", type=int, default=60)
    papertrade_parser.add_argument("--resume-on-start", action="store_true")
    papertrade_parser.add_argument("--data-root", default="data")
    papertrade_parser.set_defaults(func=papertrade_command)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    # CLI stays quiet by default (stdout is the product); --log-level DEBUG turns
    # on the same diagnostics the web app shows, including tracebacks.
    log = configure_logging(args.log_level, args.log_file)
    try:
        args.func(args)
    except SystemExit:
        raise
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        # Without this the CLI swallowed the traceback entirely and a failure was
        # one line of prose. DEBUG prints it inline; otherwise point at the switch.
        log.debug("command %s failed", args.command, exc_info=True)
        if not log.isEnabledFor(logging.DEBUG):
            print("hint: re-run with --log-level DEBUG for the full traceback", file=sys.stderr)
        raise SystemExit(1)
