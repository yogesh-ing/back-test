"""Test-suite-wide setup.

The data-source policy (``config/data_sources.yaml``) disables synthetic by
default, which is right for a deployment and wrong for a test suite: 89 test
files generate synthetic candles on purpose, and they must keep working. The
policy's own ``testing`` profile turns synthetic back on for exactly this.

Set at import time, before any test module can resolve and cache the policy.

This is a test-scoped opt-in, not a hole in the control. The tests that assert
the refusal build their own policy explicitly and are unaffected by this.
"""

from __future__ import annotations

import os

os.environ.setdefault("BACKTEST_DATA_PROFILE", "testing")

#: ``ENABLE_FEED`` in a developer's ``.env`` is the operator's capture switch, and
#: it applies to pytest too — so a fixture's made-up ladder used to land in
#: ``logs/feed_depth/`` beside real broker packets. Evidence files are what the
#: cross-broker thresholds get computed from, so the suite writes its own lines
#: somewhere else. Tests that want the recorder point it at a tmp dir themselves.
os.environ.setdefault("ENABLE_FEED_DIR", os.path.join("logs", "feed_depth", "pytest"))
