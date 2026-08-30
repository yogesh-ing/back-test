"""Tests for the project-wide logging setup (tracker gap U1).

Two layers are covered:

* the plumbing in :mod:`backtest.logging_config` — levels, handlers, request-id
  correlation, ``timed()``;
* the *behaviour* the app now has to be debuggable: every ``/api`` call gets an
  id, error responses quote it, the backtest path logs its decisions, and the
  known silent-failure cases (no signals, unsupported timeframe, swallowed data
  fetch errors) emit a WARNING.

An autouse fixture restores the root logger after every test so the handlers
``create_app()`` installs never leak into other suites.
"""

from __future__ import annotations

import ast
import logging
import pathlib
import re
import threading

import pytest

from backtest.logging_config import (
    Timer,
    build_formatter,
    configure_logging,
    current_request_id,
    get_logger,
    request_id_scope,
    resolve_level,
    sanitize_request_id,
    timed,
    with_request_context,
)


# ---------------------------------------------------------------------------
# isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _restore_logging():
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    named = {
        name: (lg.level, list(lg.handlers))
        for name, lg in (("backtest", logging.getLogger("backtest")),
                         ("werkzeug", logging.getLogger("werkzeug")))
    }
    yield
    for handler in list(root.handlers):
        if handler not in handlers:
            root.removeHandler(handler)
            handler.close()
    root.handlers[:] = handlers
    root.setLevel(level)
    for name, (lvl, hds) in named.items():
        lg = logging.getLogger(name)
        lg.setLevel(lvl)
        lg.handlers[:] = hds


# ---------------------------------------------------------------------------
# configure_logging
# ---------------------------------------------------------------------------


def test_configure_logging_is_idempotent_and_reaches_debug():
    configure_logging("DEBUG")
    root = logging.getLogger()
    mine = [h for h in root.handlers if getattr(h, "_backtest_logging_handler", None)]
    assert len(mine) == 1, "one console handler, no matter how many times we reconfigure"

    configure_logging("DEBUG")
    mine = [h for h in root.handlers if getattr(h, "_backtest_logging_handler", None)]
    assert len(mine) == 1

    log = get_logger("probe")
    assert log.getEffectiveLevel() == logging.DEBUG


def test_third_party_noise_is_clamped_unless_debug():
    configure_logging("INFO")
    assert logging.getLogger("werkzeug").level == logging.WARNING
    assert logging.getLogger("urllib3").level == logging.WARNING
    # DEBUG means "show me everything": drop the clamp and inherit the root level.
    configure_logging("DEBUG")
    assert logging.getLogger("werkzeug").level == logging.NOTSET
    assert logging.getLogger("werkzeug").getEffectiveLevel() == logging.DEBUG


def test_log_file_is_written_and_replaced_on_reconfigure(tmp_path):
    first, second = tmp_path / "a.log", tmp_path / "b.log"
    configure_logging("INFO", str(first))
    get_logger("file").info("alpha")
    logging.getLogger().handlers[0].flush()
    assert "alpha" in first.read_text()

    configure_logging("INFO", str(second))
    get_logger("file").info("beta")
    for handler in logging.getLogger().handlers:
        handler.flush()
    assert "beta" in second.read_text()
    assert "beta" not in first.read_text()
    files = [h for h in logging.getLogger().handlers
             if getattr(h, "_backtest_logging_handler", None) == "file"]
    assert len(files) == 1, "the previous file handler must be replaced, not stacked"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("DEBUG", logging.DEBUG), ("info", logging.INFO), ("WARNING", logging.WARNING),
     ("", logging.INFO), ("NOT_A_LEVEL", logging.INFO), (10, 10), ("ALL", logging.DEBUG)],
)
def test_resolve_level(raw, expected):
    assert resolve_level(raw) == expected


def test_level_from_env(monkeypatch):
    monkeypatch.setenv("BACKTEST_LOG_LEVEL", "ERROR")
    assert resolve_level(None) == logging.ERROR
    assert resolve_level("DEBUG") == logging.DEBUG, "an explicit argument wins"


def test_get_logger_never_double_prefixes():
    assert get_logger("api.backtest").name == "backtest.api.backtest"
    assert get_logger("backtest.api.backtest").name == "backtest.api.backtest"
    assert get_logger().name == "backtest"


# ---------------------------------------------------------------------------
# request-id plumbing
# ---------------------------------------------------------------------------


def test_request_id_scope_sets_and_restores():
    assert current_request_id() == ""
    with request_id_scope("abc123") as rid:
        assert rid == "abc123" and current_request_id() == "abc123"
    assert current_request_id() == ""


def test_request_id_is_stamped_on_formatted_lines():
    formatter = build_formatter()

    def emit() -> str:
        record = logging.LogRecord("backtest.fmt", logging.INFO, __file__, 1, "hello", (), None)
        return formatter.format(record)

    with request_id_scope("cafe01"):
        assert "[cafe01]" in emit()
    # Outside a request the placeholder keeps every line column-aligned.
    text = emit()
    assert "hello" in text and "-|" in text

    # A record that never saw our filter still formats (no "--- Logging error ---").
    raw = logging.LogRecord("werkzeug", logging.INFO, "x", 1, "hi", (), None)
    assert not hasattr(raw, "req_id") or raw.req_id
    assert "hi" in formatter.format(raw)


def test_with_request_context_crosses_threads():
    seen: list[str] = []

    def work():
        seen.append(current_request_id())

    with request_id_scope("thread77"):
        thread = threading.Thread(target=with_request_context(work))
        thread.start()
        thread.join()
    assert seen == ["thread77"], "worker threads must keep the caller's request id"
    assert current_request_id() == "", "and leave no residue afterwards"


def test_timed_logs_duration_and_reraises(caplog):
    log = get_logger("timer")
    with caplog.at_level(logging.DEBUG, logger="backtest.timer"):
        with timed(log, "work", logging.DEBUG) as t:
            pass
        assert t.elapsed_ms >= 0
        assert "work done in" in caplog.text

        with pytest.raises(ValueError):
            with timed(log, "boom"):
                raise ValueError("nope")
        assert "boom failed" in caplog.text and "nope" in caplog.text
    assert isinstance(Timer(log, "x"), Timer)


# ---------------------------------------------------------------------------
# web layer
# ---------------------------------------------------------------------------


@pytest.fixture()
def client():
    from backtest.web.app import create_app

    return create_app(source="synthetic").test_client()


def test_every_api_response_carries_a_request_id(client):
    ok = client.get("/api/strategies")
    assert ok.headers["X-Request-Id"], "successes are tagged too, for correlation"

    bad = client.post("/api/backtest/run", json={"strategy": "does-not-exist"})
    assert bad.status_code == 400
    rid = bad.headers["X-Request-Id"]
    assert rid and bad.get_json()["request_id"] == rid, "the toast can quote what the log has"


def test_inbound_request_id_is_honoured(client):
    resp = client.get("/health", headers={"X-Request-Id": "mine42"})
    assert resp.headers["X-Request-Id"] == "mine42"


def test_client_supplied_request_id_is_sanitised(client):
    """A header value must not be able to forge a log line or smuggle escapes."""
    evil = "abc | INFO backtest.evil -| forged" + "\x1b[31m" + "z" * 200
    rid = client.get("/health", headers={"X-Request-Id": evil}).headers["X-Request-Id"]
    # Structure is what makes a log line forgeable (separators, escapes, newlines);
    # the surviving words are harmless and keep the id greppable.
    assert rid.startswith("abc"), "harmless characters survive, so correlation still works"
    assert rid == sanitize_request_id(evil)
    assert set(rid) <= set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
    assert "\x1b" not in rid and "|" not in rid and " " not in rid and len(rid) <= 64

    # Werkzeug refuses CR/LF in a header outright — the sanitiser is defence in
    # depth for anything that bypasses the test client (real servers, proxies).
    with pytest.raises(ValueError):
        client.get("/health", headers={"X-Request-Id": "a\r\nINFO backtest.evil -| forged"})

    assert sanitize_request_id("a b\nc") == "abc"
    assert sanitize_request_id("") == ""
    assert sanitize_request_id(None) == ""


def test_http_exceptions_are_not_swallowed_by_the_error_handler(client):
    """The generic Exception handler must not turn a 404 route into a 500."""
    assert client.get("/definitely-not-a-page").status_code == 404


def test_unhandled_error_becomes_json_and_logs_a_traceback(client, monkeypatch, caplog):
    import backtest.api.backtest as backtest_api

    def explode(*args, **kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(backtest_api, "BacktestAdapter", explode)
    with caplog.at_level(logging.DEBUG, logger="backtest"):
        resp = client.post("/api/backtest/run", json={
            "strategy": "sma_crossover", "symbol": "DEMO", "timeframe": "1D",
            "from_date": "2024-01-01", "to_date": "2024-12-31", "capital": 10000,
            "params": {"fast": 5, "slow": 20},
        })
    assert resp.status_code == 500
    body = resp.get_json()
    assert "kaboom" in body["error"] and body["request_id"]
    text = caplog.text
    assert "unhandled error" in text and "Traceback" in text
    assert body["request_id"] in text, "the log line and the response share one id"


def test_backtest_run_logs_config_and_result(client, caplog):
    with caplog.at_level(logging.DEBUG, logger="backtest"):
        client.post("/api/backtest/run", json={
            "strategy": "sma_crossover", "symbol": "DEMO", "timeframe": "1D",
            "from_date": "2024-01-01", "to_date": "2024-12-31", "capital": 10000,
            "params": {"fast": 20, "slow": 50},
        })
    text = caplog.text
    assert "[run] strategy=sma_crossover symbol=DEMO timeframe=1D→day" in text
    assert "[result] run/sma_crossover" in text and "bars=262" in text
    assert "→ POST /api/backtest/run" in text and "← POST /api/backtest/run 200" in text


def test_flat_run_explains_itself(client, caplog):
    """A vacuous backtest used to be silent — it must now say why (G1/G2 era bug hunt)."""
    with caplog.at_level(logging.WARNING, logger="backtest"):
        resp = client.post("/api/backtest/run", json={
            "strategy": "sma_crossover", "symbol": "DEMO", "timeframe": "1D",
            "from_date": "2024-01-01", "to_date": "2024-12-31", "capital": 10000,
            "params": {"fast": 200, "slow": 250},
        })
    assert resp.status_code == 200 and not resp.get_json()["trades"]
    assert "produced NO signals" in caplog.text
    assert "produced 0 trades" in caplog.text


def test_out_of_range_params_are_flagged(client, caplog):
    with caplog.at_level(logging.WARNING, logger="backtest"):
        client.post("/api/backtest/run", json={
            "strategy": "sma_crossover", "symbol": "DEMO", "timeframe": "1D",
            "from_date": "2024-01-01", "to_date": "2024-12-31", "capital": 10000,
            "params": {"fast": 9999, "slow": 50},
        })
    assert "fast=9999 is above max 100" in caplog.text


def test_unsupported_timeframe_is_flagged(client, caplog):
    with caplog.at_level(logging.WARNING, logger="backtest"):
        client.post("/api/backtest/run", json={
            "strategy": "sma_crossover", "symbol": "DEMO", "timeframe": "3H",
            "from_date": "2024-01-01", "to_date": "2024-12-31", "capital": 10000,
            "params": {},
        })
    assert "unsupported timeframe '3H'" in caplog.text


def test_sources_say_when_they_ignore_the_interval(caplog):
    from backtest.data.synthetic import SyntheticSource

    with caplog.at_level(logging.WARNING, logger="backtest"):
        SyntheticSource().get_candles("DEMO", "2024-01-01", "2024-12-31", "4hour")
    assert "interval '4hour' is not supported" in caplog.text


def test_run_many_logs_each_slot(client, caplog):
    with caplog.at_level(logging.DEBUG, logger="backtest"):
        client.post("/api/backtest/run-many", json={
            "shared": {"symbol": "DEMO", "from_date": "2024-01-01",
                       "to_date": "2024-12-31", "capital": 10000},
            "slots": [{"id": 1, "strategy": "sma_crossover", "timeframe": "1D", "params": {}},
                      {"id": 2, "strategy": "missing_one", "timeframe": "1D", "params": {}}],
        })
    assert "[result] slot 1/sma_crossover@1D" in caplog.text
    assert "[slot 2] failed" in caplog.text
    assert "[run-many] done in" in caplog.text and "1 ok, 1 failed" in caplog.text


def test_forward_lifecycle_is_logged(client, caplog):
    import backtest.api.forward as fwd

    fwd._reset_session()
    with caplog.at_level(logging.DEBUG, logger="backtest"):
        client.post("/api/forward/start", json={
            "strategy": "sma_crossover", "symbol": "DEMO", "mode": "synthetic",
            "from_date": "2024-01-01", "to_date": "2024-06-30", "capital": 10000,
        })
        client.get("/api/forward/status")
        client.post("/api/forward/stop", json={})

        client.post("/api/forward/start",
                    json={"strategy": "sma_crossover", "symbol": "DEMO", "mode": "live"})
    text = caplog.text
    assert "[forward] /start strategy=sma_crossover symbol=DEMO mode=synthetic" in text
    # Session-scoped lines are tagged [forward:<short id>] so two replays can be
    # told apart in the log.
    assert re.search(r"\[forward:[0-9a-f]{8}\] replay running: 130 bars", text)
    assert re.search(r"\[forward:[0-9a-f]{8}\] stopped at bar", text)
    assert "broker session not authenticated" in text


def test_polling_endpoints_stay_quiet_at_info(client, caplog):
    """/status is polled every couple of seconds — it must not flood an INFO log."""
    with caplog.at_level(logging.INFO, logger="backtest.web.app"):
        client.get("/api/forward/status")
    assert "→ GET /api/forward/status" not in caplog.text

    with caplog.at_level(logging.DEBUG, logger="backtest.web.app"):
        client.get("/api/forward/status")
    assert "→ GET /api/forward/status" in caplog.text


def test_boot_logs_the_data_source(client, caplog):
    with caplog.at_level(logging.DEBUG, logger="backtest"):
        pass
    from backtest.web.app import create_app

    with caplog.at_level(logging.INFO, logger="backtest"):
        create_app(source="csv")
    assert "source=csv" in caplog.text


# ---------------------------------------------------------------------------
# entry points + a guard against re-introducing silent failures
# ---------------------------------------------------------------------------


def test_cli_accepts_log_flags_after_the_subcommand():
    """`backtest run … --log-level DEBUG` is where people type it."""
    from backtest.cli import build_parser

    args = build_parser().parse_args(
        ["run", "--strategy", "sma_crossover", "--symbol", "DEMO",
         "--from", "2024-01-01", "--to", "2024-12-31", "--log-level", "DEBUG"]
    )
    assert args.log_level == "DEBUG" and args.log_file is None
    # and the default must stay quiet, because CLI stdout is the product
    quiet = build_parser().parse_args(["list"])
    assert quiet.log_level == "WARNING"


def test_engine_entry_point_reuses_the_shared_setup():
    """``forward.engine`` used to call ``basicConfig`` itself; now it delegates.

    Order-independent: a shared handler may already exist from another test, so
    what we pin down is that the engine adds no *foreign* handler, keeps exactly
    one console handler, and applies its configured level through the shared
    setup (werkzeug clamped back to WARNING).
    """
    from backtest.forward.engine import ForwardTestingEngine, SystemConfig

    configure_logging("INFO")
    logging.getLogger("werkzeug").setLevel(logging.NOTSET)  # pretend someone un-muted it

    engine = ForwardTestingEngine.__new__(ForwardTestingEngine)
    engine.config = type("C", (), {"system": SystemConfig(log_level="WARNING")})()
    root = logging.getLogger()
    before = list(root.handlers)

    engine._setup_logging()

    added = [h for h in root.handlers if h not in before]
    assert all(getattr(h, "_backtest_logging_handler", None) for h in added), (
        "engine._setup_logging must not install its own handler (that was basicConfig)"
    )
    console = [h for h in root.handlers
               if getattr(h, "_backtest_logging_handler", None) == "console"]
    assert len(console) == 1, "exactly one shared console handler, whatever the test order"
    assert logging.getLogger("backtest.forward.engine").getEffectiveLevel() == logging.WARNING
    assert logging.getLogger("werkzeug").getEffectiveLevel() == logging.WARNING


@pytest.mark.parametrize("module_dir", ["api", "web", "data", "engine", "adapters"])
def test_no_swallowed_exceptions_in_the_debuggable_layers(module_dir):
    """A bare ``except: pass`` is how "nothing shows up in the log" starts.

    Guard: inside the layers that back the UI, an ``except`` block must either
    log something, or return/raise — never silently ``pass``/``continue``.
    """
    root = pathlib.Path(__file__).resolve().parents[1] / "src" / "backtest" / module_dir
    offenders = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            statements = [st for st in node.body if not isinstance(st, ast.Pass)]
            ignores_only = all(isinstance(st, ast.Continue) for st in statements) or not statements
            if not ignores_only:
                continue
            around = "".join(path.read_text().splitlines()[max(0, node.lineno - 1):node.lineno + 2])
            if "noqa" in around:
                continue
            logs = any(
                isinstance(st, ast.Expr) and isinstance(st.value, ast.Call)
                for st in node.body
            )
            if not logs:
                offenders.append(f"{path.relative_to(root)}:{node.lineno}")
    assert not offenders, f"silent except blocks (log them or re-raise): {offenders}"
