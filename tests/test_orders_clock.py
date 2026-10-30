"""B6 — one clock on the Portfolio page (source-level guard).

The Orders tab stamped fills ``04:45:00`` — the UTC hour sliced straight out of
the stored string — while the Positions table beside it rendered the same
instant as IST wall-clock. The 2026-10-27 clock policy stores aware UTC and
declares IST the *presentation* zone; ``iso_utc`` at rest never licensed
bare-UTC on screen.

Behaviour is pinned in the Node harness ``tests/js/test_orders_tab.mjs`` (the
rendered cell). This guard exists because ``portfolio.js`` is a self-running IIFE
with no export a harness can reach, and because the whole point of the ticket is
that the two columns cannot drift apart again: both must render through an
explicit IST zone, and neither may read a naive stamp as browser time (that
would silently re-base an aware-UTC value by ±offset on any non-IST machine).
"""

from __future__ import annotations

from pathlib import Path

JS = Path("src/backtest/web/static/js")

ORDERS = (JS / "components/orders_tab.js").read_text(encoding="utf-8")
PORTFOLIO = (JS / "portfolio.js").read_text(encoding="utf-8")


def test_both_tables_render_through_the_ist_zone():
    for label, src in (("orders_tab.js", ORDERS), ("portfolio.js", PORTFOLIO)):
        assert "Asia/Kolkata" in src, f"{label} renders P&L stamps without a declared zone"
        assert "timeZone: IST_ZONE" in src, (
            f"{label} names the zone but never passes it to the formatter"
        )


def test_the_orders_cell_does_not_slice_the_utc_hour_off_the_string():
    """The bug was textual: a slice returns whatever clock the row was stored on."""
    assert "text.slice(11, 19)" not in ORDERS
    assert "parseUtc(" in ORDERS, "the Orders tab must read its stamps through the UTC door"


def test_a_naive_stamp_is_assumed_utc_not_local():
    """Legacy rows were written by ``utcnow()`` with no offset; guessing local
    re-bases them by the browser's offset and puts the two columns apart again."""
    assert 'text + "Z"' in ORDERS
    assert 's += "Z"' in PORTFOLIO
