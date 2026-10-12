"""Gunicorn config — production (Linux) server for the Flask app.

    gunicorn -c gunicorn.conf.py backtest.web.wsgi:app

The worker model is deliberately conservative: this app keeps live trading
state IN-PROCESS (per-process PortfolioManager runners, broker session, alert
broker, feed registry). Scaling beyond one worker requires externalising that
state first (STATUS-AND-NEXT-STEPS item 13) — a second worker would keep its
own copy of the books and the risk breakers.
"""

import os

# Single process: exactly one copy of the trading state. Do not raise without
# moving state out of process first (two books = double orders).
workers = 1

# Threaded HTTP concurrency (the dev server is threaded too). The forward loop
# runs on its own thread; request threads share the same in-process state.
worker_class = "gthread"
threads = int(os.getenv("GUNICORN_THREADS", "8"))

# preload must stay OFF: create_app() and wsgi.py start per-process daemon
# threads at import time (session-expiry monitor, market-close exporter,
# portfolio intelligence). With --preload the import happens in the MASTER
# before fork, so the forked worker would inherit none of those threads —
# alerts and the exporter would silently never run.
preload_app = False

bind = os.getenv("GUNICORN_BIND", "127.0.0.1:5000")

# Backtest/compare requests can legitimately run for minutes; the default
# 30 s timeout would kill them mid-run.
timeout = int(os.getenv("GUNICORN_TIMEOUT", "600"))
graceful_timeout = 30

# The app logs its own request lines (with request ids) — no duplicate access
# log by default. Set GUNICORN_ACCESS_LOG="-" for stdout, or a path.
accesslog = os.getenv("GUNICORN_ACCESS_LOG")
errorlog = "-"
