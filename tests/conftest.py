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
