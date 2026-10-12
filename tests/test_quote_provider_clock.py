"""A6 determinism hardening — the synthetic generator's pinnable clock.

The 2026-09-24 (expiry day) and 2026-09-27/28 (post-roll) incidents were both
the same failure mode: ``SyntheticChainGenerator`` fell back to the WALL
clock (``date.today()`` / ``datetime.now()``) for expiry selection and
pricing, so the same test suite priced a ~0-DTE world one day and a ~31-DTE
world the next. The hardening gives the generator one pinnable reference and
routes every former inline fallback through a single audit point
(``_reference_date`` / ``_reference_datetime``) with the precedence:

    explicit ``reference`` argument  >  pinned reference  >  wall clock

Nothing pinned + nothing passed must stay byte-identical to V1 (wall clock).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

from backtest.options.quote_providers import SyntheticChainGenerator

# A fixed mid-month anchor. NSE moved index F&O expiries from the last
# Thursday to the last Tuesday of the month with effect from September 2025,
# so September 2026's monthly expiry is Tue 29th — the calendar the generator
# delegates to, not a weekday it computes for itself.
PINNED = datetime(2026, 9, 10, 9, 15)
SEP_EXPIRY = date(2026, 9, 29)


class TestDefaultIsWallClock:
    """No pin, no argument → V1 behaviour (today's calendar)."""

    def test_next_monthly_expiry_is_never_in_the_past(self):
        gen = SyntheticChainGenerator()
        assert gen.next_monthly_expiry() >= date.today()

    def test_available_expiries_start_from_today(self):
        gen = SyntheticChainGenerator()
        expiries = gen.available_expiries("NIFTY", count=3)
        assert len(expiries) == 3
        assert expiries == sorted(set(expiries))
        assert expiries[0] >= date.today()

    def test_price_contract_prices_off_the_wall_clock(self):
        gen = SyntheticChainGenerator()
        chain = gen.generate_chain("NIFTY")
        contract = chain[Decimal("24800")]
        assert gen.price_contract(contract) > 0.0


class TestPinnedReference:
    def test_pin_drives_expiry_selection(self):
        gen = SyntheticChainGenerator()
        gen.set_reference(PINNED)
        assert gen.next_monthly_expiry() == SEP_EXPIRY

    def test_pin_drives_available_expiries(self):
        gen = SyntheticChainGenerator()
        gen.set_reference(PINNED)
        expiries = gen.available_expiries("NIFTY", count=3)
        assert expiries[0] == SEP_EXPIRY
        assert expiries == sorted(set(expiries)) and len(expiries) == 3

    def test_pin_drives_pricing_time_value(self):
        """Same contract, same spot: 14 days out must carry more premium
        than expiry morning — proves ``price_contract`` reads the pin."""
        gen = SyntheticChainGenerator()
        gen.set_reference(PINNED)
        contract = gen.generate_chain("NIFTY", expiry=SEP_EXPIRY)[Decimal("24800")]
        two_weeks_out = gen.price_contract(contract)
        gen.set_reference(datetime.combine(SEP_EXPIRY, datetime.min.time()))
        expiry_morning = gen.price_contract(contract)
        assert two_weeks_out > expiry_morning > 0.0

    def test_constructor_reference_kwarg(self):
        gen = SyntheticChainGenerator(reference=PINNED)
        assert gen.next_monthly_expiry() == SEP_EXPIRY

    def test_pinned_run_is_reproducible(self):
        """Two generators pinned to the same instant agree on everything —
        the property the wall-clock fallback used to break across days."""
        a = SyntheticChainGenerator(reference=PINNED)
        b = SyntheticChainGenerator(reference=PINNED)
        assert a.available_expiries("NIFTY") == b.available_expiries("NIFTY")
        ca = a.generate_chain("NIFTY")[Decimal("24800")]
        cb = b.generate_chain("NIFTY")[Decimal("24800")]
        assert a.price_contract(ca) == b.price_contract(cb)


class TestPrecedence:
    def test_explicit_argument_beats_the_pin(self):
        gen = SyntheticChainGenerator(reference=PINNED)
        # Last Tuesday of November 2026 is the 24th (the NSE convention from
        # September 2025 — a generator that assumed Thursday said the 26th).
        assert gen.next_monthly_expiry(date(2026, 11, 2)) == date(2026, 11, 24)
        # And the pin still stands afterwards.
        assert gen.next_monthly_expiry() == SEP_EXPIRY

    def test_explicit_pricing_reference_beats_the_pin(self):
        gen = SyntheticChainGenerator(reference=PINNED)
        contract = gen.generate_chain("NIFTY", expiry=SEP_EXPIRY)[Decimal("24800")]
        pinned_price = gen.price_contract(contract)
        near_expiry = datetime.combine(SEP_EXPIRY - timedelta(days=1), datetime.min.time())
        assert gen.price_contract(contract, reference=near_expiry) < pinned_price

    def test_unpin_restores_wall_clock(self):
        gen = SyntheticChainGenerator(reference=PINNED)
        gen.set_reference(None)
        assert gen.next_monthly_expiry() >= date.today()
