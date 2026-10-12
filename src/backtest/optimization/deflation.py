"""Deflated Sharpe Ratio — PRD backTest-enhance Part 2 §3.

The plain-language problem, from the PRD: if you flip a coin 500 times looking
for a heads-biased coin and eventually find one that gave 60% heads, that does
not mean the coin is biased — you had 500 tries and got lucky once. Parameter
searches are the same shape.

**The statistic** is Bailey & López de Prado (2014), *The Deflated Sharpe
Ratio: Correcting for Selection Bias, Backtest Overfitting and Non-Normality*,
JPM 40(5)::

    E[max Z]  ≈ (1 − γ)·Φ⁻¹(1 − 1/N) + γ·Φ⁻¹(1 − 1/(N·e))
    SR₀        = √Var[{SR_n}] · E[max Z]
    DSR        = Φ( (SR̂ − SR₀)·√(T−1) / √(1 − γ₃·SR̂ + ((γ₄−1)/4)·SR̂²) )

with ``γ ≈ 0.5772`` the Euler–Mascheroni constant and ``e`` Euler's number.

**Two numbers come out of this, on different scales, and conflating them is
the easiest mistake to make here.** The PRD's wireframe asks for "Deflated
Sharpe: 0.89" next to "Sharpe: 1.42" and compares it to 0.5 — a Sharpe-scale
value. The statistic itself is a probability on 0–1. So:

* :attr:`deflated_sharpe` — **SR₀**, the Sharpe the best of N trials had to
  beat. Sharpe-scale, directly comparable to the reported Sharpe, and what the
  wireframe means by "after correcting for 56 combinations tested".
* :attr:`probability` — the canonical DSR: the chance the true Sharpe is
  genuinely positive given you picked the best of N.

Reporting only one would be misleading in one direction or the other. The
Sharpe-scale threshold alone would read as a "corrected Sharpe" the paper does
not define; the probability alone would leave the wireframe's 0.5-vs-1.5 rule
comparing apples to oranges.

**All Sharpes must be unannualised** before this is applied — the trial
variance and the SR² term both change under annualisation, and the paper is
about per-observation Sharpes. Annualised Sharpes here are divided by
√periods_per_year on the way in.

**γ₄ is raw (Fisher=False) kurtosis**, so a normal distribution gives 3 and the
term collapses to Lo's √(1 + SR²/2). Passing excess kurtosis instead is a
well-known bug (vectorbt issue #10) and would silently inflate every score.
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Sequence

__all__ = ["deflated_sharpe", "expected_max_z", "EULER_GAMMA"]

#: Euler–Mascheroni constant, γ.
EULER_GAMMA = 0.5772156649015329

#: Below this many trials the "best of N" idea is not doing any work: with one
#: or two combinations there is no selection to deflate against.
MIN_TRIALS = 3


def _norm_ppf(p: float) -> float:
    """Inverse standard-normal CDF.

    ``scipy`` is not a dependency here and this is the only place the normal
    distribution is needed. Acklam's rational approximation is accurate to
    ~1.15e-9 over the whole range — far beyond what a displayed statistic needs.
    """
    if not 0.0 < p < 1.0:
        raise ValueError(f"norm_ppf requires 0 < p < 1, got {p!r}")
    a = (-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00)
    b = (-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01)
    c = (-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00)
    d = (7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00)
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        num = ((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]
        den = (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1
        return num / den
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        num = ((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]
        den = (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1
        return -num / den
    q = p - 0.5
    r = q * q
    num = (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q
    den = ((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1
    return num / den


def _norm_cdf(x: float) -> float:
    """Standard-normal CDF via the error function."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def expected_max_z(n_trials: int) -> float:
    """E[max of N iid standard normals], via the extreme-value approximation.

    N = 2 is exact: the maximum of two iid normals is the distribution of
    |Z|, whose mean is √(2/π).
    """
    if n_trials < 2:
        raise ValueError("expected_max_z needs at least 2 trials")
    if n_trials == 2:
        return math.sqrt(2.0 / math.pi)
    return (
        (1 - EULER_GAMMA) * _norm_ppf(1 - 1.0 / n_trials)
        + EULER_GAMMA * _norm_ppf(1 - 1.0 / (n_trials * math.e))
    )


def _annual_scale(periods_per_year: float | None) -> float:
    """√periods per year — the factor between per-observation and annualised
    Sharpe. Kept in one place so it can never be applied twice."""
    if not periods_per_year or periods_per_year <= 1:
        return 1.0
    return math.sqrt(float(periods_per_year))


def _unannualize(values: Iterable[float], periods_per_year: float | None) -> list[float]:
    """Annualised Sharpe -> per-observation Sharpe. Takes the *period count*,
    not the scale factor — confusing the two squares it and silently inflates
    every result.

    Non-finite values are dropped rather than carried: a single NaN makes the
    variance NaN, and a NaN Sharpe is indistinguishable from a good one once it
    is rounded and formatted.
    """
    scale = _annual_scale(periods_per_year)
    out = []
    for v in values:
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if math.isfinite(f):
            out.append(f / scale)
    return out


def _variance(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return sum((v - mean) ** 2 for v in values) / (len(values) - 1)


def _insufficient(reason: str, **extra: Any) -> dict[str, Any]:
    """The same keys, with the numbers missing. Callers can render this without
    special-casing — an absent DSR is a normal outcome, not an error."""
    out: dict[str, Any] = {
        "deflated_sharpe": None,
        "probability": None,
        "expected_max_sharpe": None,
        "trials": None,
        "observations": None,
        "sharpe_variance": None,
        "status": "insufficient_data",
        "reason": reason,
    }
    out.update(extra)
    return out


def deflated_sharpe(
    trial_sharpes: Sequence[float],
    best_sharpe: float | None,
    *,
    trials: int | None = None,
    observations: int | None = None,
    periods_per_year: float = 252.0,
    skew: float = 0.0,
    kurtosis: float = 3.0,
) -> dict[str, Any]:
    """Deflate ``best_sharpe`` for having been chosen out of ``trials`` tries.

    :param trial_sharpes: Sharpe of **every** combination tested, annualised,
        in the same units as ``best_sharpe``. Errors and constraint failures
        are trials too and belong in this list — they were still chances taken.
    :param best_sharpe: the reported (annualised) Sharpe being deflated.
    :param trials: the number of combinations *tried*. Defaults to the length
        of ``trial_sharpes``; pass it explicitly when some evaluations errored
        and the row was never written.
    :param observations: T, the number of return observations. The √(T−1)
        factor is the sample-size term; without it the statistic silently
        rewards a long history over a short one.
    :param periods_per_year: used to unannualise. The paper is about
        per-observation Sharpes; annualised ones are not interchangeable.
    :param skew: γ₃, sample skewness of the selected strategy's returns.
    :param kurtosis: γ₄, **raw** kurtosis (3 for a normal distribution).
    """
    n = int(trials if trials is not None else len(trial_sharpes))
    if n < MIN_TRIALS:
        return _insufficient(
            f"Only {n} combination(s) tested — there is no multiple-testing "
            f"selection to correct for.",
            trials=n,
        )
    if not observations or observations < 3:
        return _insufficient(
            "Return observations are unknown — the sample-size term cannot be "
            "computed.",
            trials=n,
        )
    if best_sharpe is None or not math.isfinite(float(best_sharpe)):
        return _insufficient("No valid result to deflate.", trials=n)

    scale = _annual_scale(periods_per_year)
    sharpes = _unannualize(trial_sharpes, periods_per_year)
    if len(sharpes) < 2:
        return _insufficient(
            "Not enough trial Sharpes to estimate their dispersion.", trials=n
        )
    sr = float(best_sharpe) / scale

    var = _variance(sharpes)
    emax = expected_max_z(n)
    sr0 = math.sqrt(var) * emax

    # Lo (2002) generalised by the non-normal moment terms. With γ₃=0, γ₄=3
    # this collapses to √(1 + SR²/2).
    moment = 1.0 - skew * sr + ((kurtosis - 1.0) / 4.0) * sr * sr
    if moment <= 0 or not math.isfinite(moment):
        return _insufficient(
            "Non-normal moment term is not usable for this result.", trials=n
        )
    z = (sr - sr0) * math.sqrt(observations - 1) / math.sqrt(moment)

    return {
        "deflated_sharpe": round(sr0 * scale, 3),
        "probability": round(_norm_cdf(z), 4),
        "expected_max_sharpe": round(sr0 * scale, 3),
        "trials": n,
        "observations": int(observations),
        "sharpe_variance": round(var, 6),
        "observed_sharpe": round(float(best_sharpe), 3),
        "status": "ok",
        "reason": None,
    }


def deflation_warning(dsr: dict[str, Any] | None, observed: float | None) -> dict[str, str] | None:
    """PRD §3's orange warning: a wide gap between the two Sharpes.

    Only fires when the raw Sharpe was high enough to be worth deflating — a
    large gap on a Sharpe of 0.3 is a small absolute difference, and saying so
    would be noise.
    """
    if not dsr or dsr.get("status") != "ok" or observed is None:
        return None
    if observed <= 1.5:
        return None
    deflated = dsr.get("deflated_sharpe")
    if deflated is None or deflated >= 0.5:
        return None
    return {
        "level": "warning",
        "code": "deflation_gap",
        "message": (
            f"Large gap between observed ({observed:.2f}) and deflated "
            f"({deflated:.2f}) Sharpe. The result may be a statistical artifact "
            f"of testing {dsr.get('trials')} combinations."
        ),
    }
