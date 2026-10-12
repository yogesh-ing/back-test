"""Deflated Sharpe Ratio — PRD backTest-enhance Part 2 §3.

The point of the statistic is that it must *punish* a wide search, and that it
must not be faked by the annualisation convention. Most of what follows is
about those two things, because both fail silently.
"""

from __future__ import annotations

import math

import pytest

from backtest.optimization.deflation import (
    EULER_GAMMA,
    _norm_cdf,
    _norm_ppf,
    deflated_sharpe,
    deflation_warning,
    expected_max_z,
)


def trials(n, sd=0.6, seed=11):
    """A reproducible spread of annualised trial Sharpes."""
    import random

    rng = random.Random(seed)
    return [rng.gauss(0.0, sd) for _ in range(n)]


# ----------------------------------------------------------------- primitives
class TestTheNormalQuantile:
    @pytest.mark.parametrize(
        "p, want",
        [
            (0.5, 0.0),
            (0.975, 1.959963985),
            (0.025, -1.959963985),
            (0.99, 2.326347874),
        ],
    )
    def test_matches_published_quantiles(self, p, want):
        """A wrong Φ⁻¹ makes every number downstream wrong and still plausible."""
        assert _norm_ppf(p) == pytest.approx(want, abs=1e-6)

    def test_round_trips_through_the_cdf(self):
        for z in (-3.0, -1.0, 0.0, 0.5, 2.0, 3.5):
            assert _norm_ppf(_norm_cdf(z)) == pytest.approx(z, abs=1e-6)

    @pytest.mark.parametrize("p", [0.0, 1.0, -0.1, 1.5])
    def test_rejects_impossible_probabilities(self, p):
        with pytest.raises(ValueError):
            _norm_ppf(p)


class TestExpectedMax:
    def test_two_trials_is_exact(self):
        """E[max of 2 iid normals] = E|Z| = √(2/π)."""
        assert expected_max_z(2) == pytest.approx(math.sqrt(2 / math.pi), abs=1e-9)

    def test_it_grows_with_the_number_of_trials(self):
        values = [expected_max_z(n) for n in (2, 3, 10, 100, 1000)]
        assert values == sorted(values)

    def test_it_matches_the_refined_asymptotic(self):
        """
        E[max of N normals] ≈ √(2 ln N) − (ln ln N + ln 4π) / 2√(2 ln N).
        The second term is why the ratio to the naive √(2 ln N) sits below 1
        and creeps upward toward it — a real property, not an implementation
        error, so it is pinned rather than eyeballed.
        """
        for n in (1000, 5000, 10000):
            lead = math.sqrt(2 * math.log(n))
            refined = lead - (math.log(math.log(n)) + math.log(4 * math.pi)) / (2 * lead)
            assert expected_max_z(n) == pytest.approx(refined, rel=0.06), n
            assert expected_max_z(n) < lead

    def test_one_trial_is_nonsense(self):
        with pytest.raises(ValueError):
            expected_max_z(1)


# ------------------------------------------------------------------ the maths
class TestTheDeflation:
    def test_the_bar_is_the_trial_spread_times_the_extreme_value(self):
        """SR₀ = √Var[trials] · E[max Z] — pinned, not just plausible."""
        t = trials(56, sd=0.6)
        sd = math.sqrt(sum((v - sum(t) / len(t)) ** 2 for v in t) / (len(t) - 1))
        out = deflated_sharpe(t, 1.42, trials=56, observations=756)
        assert out["deflated_sharpe"] == pytest.approx(sd * expected_max_z(56), rel=1e-3)

    def test_a_wider_search_raises_the_bar(self):
        """The whole reason the statistic exists."""
        bars = [
            deflated_sharpe(trials(n), 1.42, trials=n, observations=756)["deflated_sharpe"]
            for n in (10, 100, 1000)
        ]
        assert bars == sorted(bars)
        assert bars[0] < bars[-1]

    def test_more_search_lowers_the_probability(self):
        probs = [
            deflated_sharpe(trials(n), 1.42, trials=n, observations=756)["probability"]
            for n in (10, 100, 1000)
        ]
        assert probs == sorted(probs, reverse=True)

    def test_a_tighter_spread_lowers_the_bar(self):
        """Two runs over the same N: a narrow search is less lucky, not more."""
        loose = deflated_sharpe(trials(56, sd=2.0), 1.42, trials=56, observations=756)
        tight = deflated_sharpe(trials(56, sd=0.2), 1.42, trials=56, observations=756)
        assert tight["deflated_sharpe"] < loose["deflated_sharpe"]

    def test_a_longer_history_raises_the_probability(self):
        """√(T−1) is the sample-size term; dropping it would reward short runs."""
        short = deflated_sharpe(trials(56), 1.42, trials=56, observations=250)
        long = deflated_sharpe(trials(56), 1.42, trials=56, observations=3000)
        assert long["probability"] > short["probability"]

    def test_it_stays_a_probability(self):
        out = deflated_sharpe(trials(56), 3.2, trials=56, observations=756)
        assert 0.0 <= out["probability"] <= 1.0

    def test_a_better_result_is_more_likely_to_be_real(self):
        t = trials(56)
        probs = [
            deflated_sharpe(t, best, trials=56, observations=756)["probability"]
            for best in (0.2, 1.0, 2.0, 4.0)
        ]
        assert probs == sorted(probs)

    def test_eulers_constant_is_the_documented_one(self):
        assert EULER_GAMMA == pytest.approx(0.5772156649015329)


# ------------------------------------------------------ the annualisation trap
class TestAnnualisation:
    def test_periods_per_year_must_not_be_applied_twice(self):
        """
        A scale passed where a period-count is expected gets squared, and the
        bar comes out √252 times too large — a number that still looks like a
        Sharpe. Annualising and unannualising must cancel exactly.
        """
        t = trials(56)
        for ppy in (1, 12, 52, 252, 365):
            out = deflated_sharpe(t, 1.42, trials=56, observations=756, periods_per_year=ppy)
            assert out["deflated_sharpe"] == pytest.approx(1.389, abs=0.01), f"ppy={ppy}"

    def test_the_probability_is_computed_on_per_observation_sharpes(self):
        """
        The Sharpe-scale number is annualisation-invariant because it is
        rescaled on the way out. The probability is NOT, and should not be: it
        is defined on per-observation Sharpes, where the sqrt(1 + SR^2/2)
        non-normal term is meaningful. Pinned against a hand computation so the
        convention is visible rather than assumed.
        """
        t = trials(56)
        out = deflated_sharpe(t, 1.42, trials=56, observations=756, periods_per_year=252)

        scale = math.sqrt(252)
        sr = 1.42 / scale
        mean = sum(t) / len(t)
        var = sum((v / scale - mean / scale) ** 2 for v in t) / (len(t) - 1)
        sr0 = math.sqrt(var) * expected_max_z(56)
        z = (sr - sr0) * math.sqrt(755) / math.sqrt(1 - 0.0 * sr + ((3 - 1) / 4) * sr * sr)
        assert out["probability"] == pytest.approx(0.5 * (1 + math.erf(z / math.sqrt(2))), abs=1e-4)

    def test_the_kurtosis_term_is_raw_not_excess(self):
        """
        gamma_4 = 3 for a normal distribution, so (gamma_4 - 1)/4 = 1/2. Passing
        excess kurtosis instead (0 for a normal) is a known bug (vectorbt #10):
        it shrinks the denominator and inflates every probability.
        """
        t = trials(56)
        # periods_per_year=1 puts SR on the per-observation scale where the
        # SR^2 term is worth anything; at ppy=252 the term is ~0.4% of the
        # denominator and the two agree to displayed precision.
        raw = deflated_sharpe(t, 1.42, trials=56, observations=756, kurtosis=3.0,
                              periods_per_year=1)
        excess = deflated_sharpe(t, 1.42, trials=56, observations=756, kurtosis=0.0,
                                 periods_per_year=1)
        assert excess["probability"] > raw["probability"]

    def test_a_zero_or_absent_period_count_is_treated_as_one(self):
        t = trials(56)
        assert deflated_sharpe(t, 1.42, trials=56, observations=756, periods_per_year=0)[
            "deflated_sharpe"
        ] == pytest.approx(
            deflated_sharpe(t, 1.42, trials=56, observations=756, periods_per_year=1)[
                "deflated_sharpe"
            ]
        )


# ----------------------------------------------------------------- degraded
class TestWhenItCannotBeComputed:
    def test_a_tiny_search_has_nothing_to_deflate(self):
        """One or two combinations is not a selection problem."""
        out = deflated_sharpe([0.4, 0.9], 0.9, trials=2, observations=756)
        assert out["status"] == "insufficient_data"
        assert out["deflated_sharpe"] is None
        assert "no multiple-testing" in out["reason"]

    def test_missing_observations_are_reported_not_guessed(self):
        out = deflated_sharpe(trials(56), 1.42, trials=56, observations=None)
        assert out["status"] == "insufficient_data"
        assert "observations" in out["reason"]

    def test_no_best_result(self):
        out = deflated_sharpe(trials(56), None, trials=56, observations=756)
        assert out["status"] == "insufficient_data"
        assert "No valid result" in out["reason"]

    def test_a_degenerate_spread_is_reported(self):
        out = deflated_sharpe([1.0], 1.0, trials=56, observations=756)
        assert out["status"] == "insufficient_data"
        assert "dispersion" in out["reason"]

    def test_erroring_trials_still_count_as_trials(self):
        """
        N is the number of combinations *tried*, which can exceed the rows
        written when some evaluations errored. Both are honest inputs.
        """
        out = deflated_sharpe(trials(10), 1.42, trials=500, observations=756)
        assert out["trials"] == 500
        assert (
            out["deflated_sharpe"]
            > deflated_sharpe(trials(10), 1.42, trials=10, observations=756)["deflated_sharpe"]
        )

    def test_a_hostile_non_finite_value_does_not_poison_the_result(self):
        t = trials(56) + [float("nan")]
        out = deflated_sharpe(t, 1.42, trials=57, observations=756)
        assert math.isfinite(out["deflated_sharpe"])


# ------------------------------------------------------------------ the warning
class TestTheGapWarning:
    def test_it_fires_on_a_wide_gap_from_a_high_sharpe(self):
        d = deflated_sharpe(trials(56), 2.2, trials=56, observations=756)
        d = {**d, "deflated_sharpe": 0.3}
        w = deflation_warning(d, 2.2)
        assert w and w["code"] == "deflation_gap"
        assert "56 combinations" in w["message"]

    def test_it_stays_quiet_on_a_low_sharpe(self):
        """ "A large gap" on a Sharpe of 0.3 is a small absolute difference."""
        d = {
            **deflated_sharpe(trials(56), 0.3, trials=56, observations=756),
            "deflated_sharpe": 0.1,
        }
        assert deflation_warning(d, 0.3) is None

    def test_it_stays_quiet_when_the_deflated_value_is_healthy(self):
        d = {
            **deflated_sharpe(trials(56), 2.2, trials=56, observations=756),
            "deflated_sharpe": 0.9,
        }
        assert deflation_warning(d, 2.2) is None

    def test_it_stays_quiet_when_there_is_no_dsr(self):
        assert deflation_warning(None, 2.2) is None
        assert deflation_warning({"status": "insufficient_data"}, 2.2) is None
