"""Project-wide logging setup — one place that decides handlers, levels and format.

**Why this exists.** Every module in the project logs through
``logging.getLogger("backtest.<area>")`` but nobody installed a handler, so
``logger.info`` / ``logger.debug`` lines were silently dropped (Python's
last-resort handler only prints ``WARNING`` and above). The web app had no way
to turn verbosity up at all, which made a 400 from ``/api/backtest/run`` or a
"0 trades" backtest impossible to debug from the server side. This module fixes
that for every entry point:

>>> from backtest.logging_config import configure_logging, get_logger
>>> configure_logging(level="DEBUG")            # or level=None → env/default
>>> log = get_logger(__name__)                  # → "backtest.api.backtest"

Levels and destination come from arguments first, then the environment, so the
same binary can run quietly in production and noisily on a dev box::

    BACKTEST_LOG_LEVEL=DEBUG BACKTEST_LOG_FILE=logs/app.log

Correlation: every line carries the current HTTP request id (``-`` outside a
request). :func:`request_id_scope` sets it for the duration of a Flask request,
``after_request`` echoes it in the ``X-Request-Id`` header, and error responses
include it — so a message seen in the UI can be grepped straight out of the log.
"""

from __future__ import annotations

import contextlib
import functools
import logging
import os
import sys
import time
import uuid
from contextvars import ContextVar
from typing import Any, Iterator, Optional

__all__ = [
    "configure_logging",
    "get_logger",
    "resolve_level",
    "current_request_id",
    "new_request_id",
    "bind_request_id",
    "reset_request_id",
    "sanitize_request_id",
    "request_id_scope",
    "with_request_context",
    "timed",
    "Timer",
    "build_formatter",
    "RequestAwareFormatter",
    "RequestIdFilter",
]

#: Root logger name for the whole package; keeps ``backtest.*`` under one switch.
ROOT_LOGGER = "backtest"

#: Third-party chatter that is useless unless you are debugging it on purpose.
_NOISY_LOGGERS = ("urllib3", "sqlalchemy.engine", "matplotlib", "watchfiles", "PIL")

DEFAULT_FORMAT = "%(asctime)s %(levelname)-7s %(name)-28s %(req_id)s| %(message)s"
DEFAULT_DATEFMT = "%Y-%m-%d %H:%M:%S"
ENV_LEVEL = "BACKTEST_LOG_LEVEL"
ENV_FILE = "BACKTEST_LOG_FILE"

_REQUEST_ID: ContextVar[str] = ContextVar("backtest_request_id", default="")
_SENTINEL = "_backtest_logging_handler"
_NO_REQ_ID = "-"


# ---------------------------------------------------------------------------
# request-id plumbing
# ---------------------------------------------------------------------------


def new_request_id() -> str:
    """A short, grep-friendly request id (8 hex chars)."""
    return uuid.uuid4().hex[:8]


def current_request_id() -> str:
    """The id of the request currently being served, or ``""`` outside one."""
    return _REQUEST_ID.get()


#: Request ids can come from a client header, so they are clamped to a safe
#: charset (no CR/LF/ANSI escapes → no forged log lines, no header injection)
#: and truncated.
_REQ_ID_ALLOWED = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
_REQ_ID_MAX_LEN = 64


def sanitize_request_id(value: Optional[str]) -> str:
    """Keep only harmless characters in a caller-supplied request id."""
    if not value:
        return ""
    cleaned = "".join(ch for ch in str(value) if ch in _REQ_ID_ALLOWED)[:_REQ_ID_MAX_LEN]
    return cleaned


def bind_request_id(request_id: Optional[str] = None) -> tuple[str, Any]:
    """Set the request id and return ``(id, token)`` — for middleware that cannot
    use a ``with`` block across a request (Flask hooks). Pair with
    :func:`reset_request_id`.

    ``request_id`` is a *client-supplied* correlation id in practice, so it is
    sanitised; an empty result falls back to a generated id rather than disabling
    the prefix (a request with no id is much harder to grep for).
    """
    if request_id is None:
        rid = new_request_id()
    else:
        rid = sanitize_request_id(request_id) or new_request_id()
    return rid, _REQUEST_ID.set(rid)


def reset_request_id(token: Any) -> None:
    """Undo :func:`bind_request_id`."""
    with contextlib.suppress(Exception):
        _REQUEST_ID.reset(token)  # type: ignore[arg-type]


def with_request_context(fn):
    """Wrap ``fn`` so worker threads keep the caller's request id.

    ``contextvars`` do not cross thread boundaries, so log lines emitted inside
    a :class:`~concurrent.futures.ThreadPoolExecutor` would otherwise lose their
    correlation id. Only the request id is propagated (that is all the log
    format needs) — which also keeps this safe to call from several threads at
    once, unlike sharing one ``contextvars.Context`` object.
    """
    rid = current_request_id()

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        token = _REQUEST_ID.set(rid)
        try:
            return fn(*args, **kwargs)
        finally:
            _REQUEST_ID.reset(token)

    return wrapper


@contextlib.contextmanager
def request_id_scope(request_id: Optional[str] = None) -> Iterator[str]:
    """Bind a request id for the duration of the block (used by the web layer).

    Passing ``None`` generates one; a supplied id is sanitised (see
    :func:`sanitize_request_id`).
    """
    if request_id is None:
        rid = new_request_id()
    else:
        rid = sanitize_request_id(request_id) or new_request_id()
    token = _REQUEST_ID.set(rid)
    try:
        yield rid
    finally:
        _REQUEST_ID.reset(token)


class RequestIdFilter(logging.Filter):
    """Inject ``record.req_id`` so any format string can resolve it."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D102
        if not hasattr(record, "req_id"):
            rid = current_request_id()
            record.req_id = f"[{rid}]" if rid else _NO_REQ_ID
        return True


class RequestAwareFormatter(logging.Formatter):
    """:class:`logging.Formatter` that never blows up on a missing ``req_id``.

    Records can reach our handlers from anywhere (Flask, werkzeug, a library),
    bypassing the filter, and a missing key in a format string prints
    ``--- Logging error ---`` noise. Filling the attribute in here makes the
    request id reliable for *every* line without callers having to care.
    """

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003 - stdlib name
        RequestIdFilter().filter(record)
        return super().format(record)


def build_formatter(fmt: Optional[str] = None, datefmt: Optional[str] = None) -> logging.Formatter:
    """The project's formatter — reuse it for any handler you add yourself."""
    return RequestAwareFormatter(fmt=fmt or DEFAULT_FORMAT, datefmt=datefmt or DEFAULT_DATEFMT)


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------


def resolve_level(level: "str | int | None" = None) -> int:
    """Map ``level`` (or ``$BACKTEST_LOG_LEVEL``) onto a numeric logging level.

    Unknown names fall back to INFO rather than exploding — a typo in an env var
    should not stop the app from booting.
    """
    raw: object = level if level not in (None, "") else os.getenv(ENV_LEVEL, "")
    if isinstance(raw, int):
        return raw
    text = str(raw).strip().upper()
    if text in ("", "DEFAULT"):
        return logging.INFO
    if text in ("ALL", "VERBOSE"):
        return logging.DEBUG
    resolved = logging.getLevelName(text)
    return resolved if isinstance(resolved, int) else logging.INFO


def _install_handlers(
    logger: logging.Logger,
    level: int,
    log_file: Optional[str],
    fmt: str,
    datefmt: str,
    force: bool,
) -> None:
    req_filter = RequestIdFilter()
    formatter = RequestAwareFormatter(fmt=fmt, datefmt=datefmt)

    if force:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            with contextlib.suppress(Exception):
                handler.close()

    # Console handler (stderr, so it never pollutes piped stdout).
    console = next(
        (h for h in logger.handlers if getattr(h, _SENTINEL, None) == "console"), None
    )
    if console is None:
        console = logging.StreamHandler(sys.stderr)
        console.addFilter(req_filter)
        console.setFormatter(formatter)
        setattr(console, _SENTINEL, "console")
        logger.addHandler(console)

    # Optional file handler — created once, reused on reconfigure.
    file_handler = next(
        (h for h in logger.handlers if getattr(h, _SENTINEL, None) == "file"), None
    )
    if log_file:
        if file_handler is None or getattr(file_handler, "baseFilename", "") != os.path.abspath(
            os.path.expanduser(log_file)
        ):
            if file_handler is not None:
                logger.removeHandler(file_handler)
                with contextlib.suppress(Exception):
                    file_handler.close()
            parent = os.path.dirname(os.path.abspath(os.path.expanduser(log_file)))
            if parent:
                os.makedirs(parent, exist_ok=True)
            file_handler = logging.FileHandler(os.path.expanduser(log_file), encoding="utf-8")
            file_handler.addFilter(req_filter)
            file_handler.setFormatter(formatter)
            setattr(file_handler, _SENTINEL, "file")
            logger.addHandler(file_handler)
    elif file_handler is not None:
        logger.removeHandler(file_handler)
        with contextlib.suppress(Exception):
            file_handler.close()

    logger.setLevel(level)
    for handler in logger.handlers:
        if getattr(handler, _SENTINEL, None):
            handler.setLevel(level)


def configure_logging(
    level: str | int | None = None,
    log_file: Optional[str] = None,
    *,
    fmt: Optional[str] = None,
    datefmt: Optional[str] = None,
    force: bool = False,
    quiet_third_party: bool = True,
) -> logging.Logger:
    """Install handlers/levels for ``backtest.*`` (and, by propagation, Flask).

    Idempotent: calling it twice re-uses the handlers it installed rather than
    duplicating log lines. Returns the project root logger.

    Parameters
    ----------
    level:
        Name or numeric level. Falls back to ``$BACKTEST_LOG_LEVEL`` then INFO.
        ``DEBUG``/``ALL`` also un-quiets urllib3 / SQLAlchemy / werkzeug.
    log_file:
        Append a copy of every record to this path. Falls back to
        ``$BACKTEST_LOG_FILE``. Parent directories are created.
    force:
        Drop *all* pre-existing root handlers first (used by CLI entry points
        that want to own the output).
    quiet_third_party:
        Clamp the noisy libraries to WARNING unless DEBUG was requested.
    """
    resolved = resolve_level(level)
    target_file = log_file if log_file not in (None, "") else os.getenv(ENV_FILE) or None
    root = logging.getLogger()

    # Attaching to the root logger is what lets third-party loggers (and
    # ``flask.app``) share our handlers — Flask's own default handler is then
    # skipped, because ``flask.logging.has_level_handler`` sees ours.
    _install_handlers(
        root,
        resolved,
        target_file,
        fmt or DEFAULT_FORMAT,
        datefmt or DEFAULT_DATEFMT,
        force,
    )
    logging.getLogger(ROOT_LOGGER).setLevel(resolved)

    if quiet_third_party:
        # DEBUG (or lower) means "show me everything", so let the libs talk too.
        clamp = logging.NOTSET if resolved <= logging.DEBUG else logging.WARNING
        for name in (*_NOISY_LOGGERS, "werkzeug"):
            logging.getLogger(name).setLevel(clamp)

    logging.getLogger(ROOT_LOGGER).debug(
        "logging configured: level=%s file=%s handlers=%s",
        logging.getLevelName(resolved),
        target_file or "-",
        [type(h).__name__ for h in root.handlers],
    )
    return logging.getLogger(ROOT_LOGGER)


def _logger_name_for_main() -> Optional[str]:
    """Recover a real dotted name when a module was launched as ``__main__``.

    ``python -m backtest.web.app`` sets ``__name__ == "__main__"``, which would
    otherwise put every line under the useless name ``backtest.__main__``.
    """
    main = sys.modules.get("__main__")
    source = getattr(main, "__file__", None)
    if not source:
        return None
    path = os.path.realpath(source)
    marker = os.sep + ROOT_LOGGER + os.sep
    index = path.rfind(marker)
    if index < 0:
        return None
    dotted = path[index + 1:].replace(os.sep, ".")
    if dotted.endswith(".py"):
        dotted = dotted[:-3]
    return dotted


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """Logger under the ``backtest`` namespace.

    ``get_logger(__name__)`` and ``get_logger("api.backtest")`` both work and
    never double-prefix. Passing ``"__main__"`` (the usual fate of an entry point
    launched with ``python -m``) resolves to that file's real dotted name.
    """
    if not name:
        return logging.getLogger(ROOT_LOGGER)
    if name == "__main__":
        recovered = _logger_name_for_main()
        if recovered:
            return logging.getLogger(recovered)
        return logging.getLogger(ROOT_LOGGER)
    if name == ROOT_LOGGER or name.startswith(ROOT_LOGGER + "."):
        return logging.getLogger(name)
    return logging.getLogger(f"{ROOT_LOGGER}.{name}")


# ---------------------------------------------------------------------------
# helpers used by the API/engine layers
# ---------------------------------------------------------------------------


class Timer:
    """``with timed(log, "backtest sma_crossover") as t: …`` → duration on exit.

    Records ``t.elapsed_ms`` and logs a WARNING-with-traceback if the block
    raises (the exception is re-raised untouched).
    """

    __slots__ = ("log", "label", "level", "_start", "elapsed_ms")

    def __init__(self, log: logging.Logger, label: str, level: int = logging.DEBUG) -> None:
        self.log = log
        self.label = label
        self.level = level
        self._start = 0.0
        self.elapsed_ms = 0.0

    def __enter__(self) -> "Timer":
        self._start = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:  # noqa: ANN001 - stdlib signature
        self.elapsed_ms = round((time.perf_counter() - self._start) * 1000, 1)
        if exc is not None:
            self.log.error("%s failed after %.1f ms: %s", self.label, self.elapsed_ms, exc,
                           exc_info=exc)
        else:
            self.log.log(self.level, "%s done in %.1f ms", self.label, self.elapsed_ms)
        return False


@contextlib.contextmanager
def timed(log: logging.Logger, label: str, level: int = logging.DEBUG) -> Iterator[Timer]:
    """Context-manager form of :class:`Timer`."""
    timer = Timer(log, label, level)
    with timer:
        yield timer
