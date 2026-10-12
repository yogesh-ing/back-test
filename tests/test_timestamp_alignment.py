"""The clock the cached bars are stamped on — writer contract.

``market_data_cache.ts`` is ``timestamptz``. The fetch writers used to bind a
naive UTC wall clock into it (PostgreSQL then read that in ``Asia/Calcutta``,
putting every bar 5h30 early and splitting one NSE session over two UTC
calendar days). ``bar_timestamp`` stops that at the source: writers always
bind an aware UTC instant, whatever shape the broker sent.

Rows already written under the old convention are repaired once, in place,
by ``scripts/migrate_cache_ts_to_session_clock.py`` — not rescued per read.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from backtest.data.base import EXCHANGE_TZ, bar_timestamp

IST = timezone(timedelta(hours=5, minutes=30))


def test_the_same_instant_arrives_as_utc_ist_or_naive_session_clock():
    """One bar, five payloads, one instant: 09:15 IST is 03:45 UTC."""
    expected = datetime(2024, 1, 5, 3, 45, tzinfo=timezone.utc)
    for raw in (
        "2024-01-05T09:15:00+05:30",  # what mStock historical sends
        "2024-01-05T03:45:00+00:00",  # what a UTC-offset payload sends
        "2024-01-05 09:15:00",  # naive: the exchange's own wall clock
        datetime(2024, 1, 5, 9, 15, tzinfo=IST),
        pd.Timestamp("2024-01-05 09:15", tz=EXCHANGE_TZ),
    ):
        got = bar_timestamp(raw)
        assert got.tzinfo is not None, f"{raw!r} came back naive — Postgres would rezone it"
        assert got.utcoffset() == timedelta(0), f"{raw!r} is not UTC"
        assert got == expected, f"{raw!r} → {got}"


def test_the_old_writer_put_the_session_on_the_wrong_side_of_midnight():
    """The regression, stated as the bug it caused.

    ``tz_convert('UTC').tz_localize(None)`` produced 03:45 with no zone; the
    Asia/Calcutta server read that as 03:45 *IST* — 22:15 UTC on the day
    before, so the session opened five and a half hours early and its bars
    landed in two UTC calendar days.
    """
    naive_utc = pd.Timestamp("2024-01-05T09:15:00+05:30").tz_convert("UTC").tz_localize(None)
    broken = naive_utc.to_pydatetime().replace(tzinfo=IST)
    assert broken.astimezone(timezone.utc) == datetime(2024, 1, 4, 22, 15, tzinfo=timezone.utc)
    assert bar_timestamp("2024-01-05T09:15:00+05:30") > broken
