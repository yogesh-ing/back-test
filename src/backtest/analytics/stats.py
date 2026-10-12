"""Dependency-free significance tests for broker comparison (PRD-003 §3.4).

Why this module exists
----------------------
PRD-003 §3.4 sketches the comparison with ``scipy.stats.mannwhitneyu`` and a
chi-square test. ``scipy`` is **not** in ``requirements.txt`` and this project
does not take new heavyweight numeric dependencies for a read-only analytics
panel, so both tests are implemented here against ``math``/``statistics``.

Both are the textbook versions:

* **Mann-Whitney U** — rank-based, so it does not assume normally distributed
  per-trade slippage, with the standard normal approximation. Tie correction
  (``sum(t^3 - t)``) and continuity correction are applied, because broker
  slippage samples are heavily tied (paper fills are frequently exactly 0 bps)
  and an uncorrected U on tied data is badly anti-conservative.
* **Chi-square on a 2x2 table** — Pearson's test with Yates' continuity
  correction, used for fill/rejection RATES where the underlying data are
  counts rather than measurements. With one degree of freedom the survival
  function is ``2 * (1 - Phi(sqrt(chi2)))``, which reuses :func:`normal_sf`.

Both use the **normal approximation**. ``scipy.stats.mannwhitneyu`` defaults
to ``method="auto"``, which silently switches to the exact permutation
distribution below n=8 and no ties; the analytics panel compares books with
hundreds to thousands of orders per broker, where the exact test is not what
would run anyway and the approximation is accurate to well under a percent.

The honesty rule that matters more than the maths
------------------------------------------------
A p-value on 3 samples is not evidence. Every entry point here takes a
``min_sample`` and returns a ``testable: False`` block with an explicit
``reason`` when the sample is too small, so a caller can render "not enough
data" instead of a confident-looking ``p=0.041`` (PRD-003 §3.4 literally
asks for the string "not significant (need more data)").
"""

from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "ALPHA",
    "MIN_SAMPLE",
    "chi_square_2x2",
    "describe",
    "mann_whitney_u",
    "median",
    "normal_sf",
    "rate_significance_block",
    "significance_block",
    "stdev",
]

#: Conventional significance level. Callers may override per test.
ALPHA = 0.05

#: Below this many observations a rank test answers nothing. The PRD's own
#: fallback string — "need more data" — is what a sub-threshold sample gets.
MIN_SAMPLE = 20


def normal_sf(z: float) -> float:
    """Upper tail of the standard normal distribution, ``P(Z > z)``.

    ``erfc`` is the numerically stable primitive (``exp(-z^2/2)`` underflows
    for large |z| where a naive CDF loses every significant digit).
    """
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def _clean(values: Iterable[float]) -> List[float]:
    """Drop ``None``/NaN/inf and coerce to float — analytics must not crash
    a page on one bad ledger row."""
    out: List[float] = []
    for v in values:
        if v is None:
            continue
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        if math.isnan(fv) or math.isinf(fv):
            continue
        out.append(fv)
    return out


def _average_ranks(sorted_values: Sequence[float]) -> List[float]:
    """Mid-ranks for an already-sorted sequence (ties share the mean rank)."""
    ranks: List[float] = []
    i = 0
    n = len(sorted_values)
    while i < n:
        j = i
        while j + 1 < n and sorted_values[j + 1] == sorted_values[i]:
            j += 1
        # 1-based ranks i+1..j+1 → average
        midrank = (i + 1 + j + 1) / 2.0
        ranks.extend([midrank] * (j - i + 1))
        i = j + 1
    return ranks


def mann_whitney_u(
    sample_a: Iterable[float],
    sample_b: Iterable[float],
    alternative: str = "two-sided",
    min_sample: int = MIN_SAMPLE,
) -> Dict[str, Any]:
    """Mann-Whitney U with tie + continuity correction (normal approximation).

    ``alternative`` is ``"two-sided"`` (default), ``"less"`` (is A smaller
    than B?) or ``"greater"`` — matching ``scipy.stats.mannwhitneyu``.

    Returns a dict with ``testable`` False and a human ``reason`` when either
    sample is below ``min_sample`` or a sample is empty, so callers never have
    to guess whether ``p_value`` is meaningful.
    """
    a = _clean(sample_a)
    b = _clean(sample_b)
    n_a, n_b = len(a), len(b)

    result: Dict[str, Any] = {
        "test": "mann_whitney_u",
        "alternative": alternative,
        "n_a": n_a,
        "n_b": n_b,
        "min_sample": int(min_sample),
        "u": None,
        "z": None,
        "p_value": None,
        "significant": False,
        "testable": False,
        "reason": None,
    }

    if n_a == 0 or n_b == 0:
        result["reason"] = "no samples — one broker has no comparable observations"
        return result
    if n_a < min_sample or n_b < min_sample:
        result["reason"] = (
            f"insufficient sample (n={n_a}/{n_b}, need ≥{min_sample} per group) — "
            "collect more trades before trusting a difference"
        )
        return result

    values = a + b
    n = n_a + n_b
    # Rank by VALUE, then map each rank back to its own sample. Sorting the
    # concatenated list and slicing off the first n_a ranks is wrong: the
    # leading ranks belong to whichever sample happens to hold the small
    # values, which silently flips the direction of every one-sided test
    # when sample A is the larger one.
    order = sorted(range(n), key=lambda i: values[i])
    ranks = _average_ranks([values[i] for i in order])
    rank_sum_a = sum(rank for rank, idx in zip(ranks, order) if idx < n_a)
    u_a = rank_sum_a - n_a * (n_a + 1) / 2.0

    # Tie correction: sigma^2 = n1*n2/12 * ((N+1) - sum(t^3-t)/(N(N-1)))
    tie_term = 0.0
    i = 0
    while i < n:
        j = i
        while j + 1 < n and values[order[j + 1]] == values[order[i]]:
            j += 1
        t = j - i + 1
        if t > 1:
            tie_term += t**3 - t
        i = j + 1
    variance = (n_a * n_b / 12.0) * ((n + 1) - tie_term / (n * (n - 1)))
    if variance <= 0:
        result["reason"] = "zero variance — every observation is identical"
        return result

    mu = n_a * n_b / 2.0
    # Continuity correction: nudge away from the mean by 0.5 in the direction
    # of the observed deviation.
    diff = u_a - mu
    corrected = diff - 0.5 if diff > 0 else (diff + 0.5 if diff < 0 else 0.0)
    z = corrected / math.sqrt(variance)

    # Direction matters: for "less" (is A smaller than B?) the evidence is a
    # very NEGATIVE z, so the p-value is the LEFT tail Φ(z) = 1 - SF(z).
    if alternative == "less":
        p = 1.0 - normal_sf(z)
    elif alternative == "greater":
        p = normal_sf(z)
    elif alternative == "two-sided":
        p = 2.0 * normal_sf(abs(z))
    else:
        raise ValueError(f"alternative must be two-sided|less|greater, got {alternative!r}")

    p = max(0.0, min(1.0, p))
    result.update(
        {
            "u": round(u_a, 4),
            "z": round(z, 4),
            "p_value": round(p, 6),
            "significant": p < ALPHA,
            "testable": True,
        }
    )
    return result


def chi_square_2x2(
    a: int,
    b: int,
    c: int,
    d: int,
    alpha: float = ALPHA,
    min_total: int = MIN_SAMPLE,
) -> Dict[str, Any]:
    """Chi-square test of independence on a 2x2 count table (Yates-corrected).

    ``a``/``b`` are the first group's [event, non-event] counts and ``c``/``d``
    the second group's. Used for fill rate and rejection rate, where the honest
    test is on the contingency table, not on two separately-estimated rates.

    df = 1, so ``p = 2 * (1 - Phi(sqrt(chi2)))``.
    """
    a_i, b_i, c_i, d_i = int(a), int(b), int(c), int(d)
    result: Dict[str, Any] = {
        "test": "chi_square",
        "table": [[a_i, b_i], [c_i, d_i]],
        "chi_square": None,
        "p_value": None,
        "significant": False,
        "testable": False,
        "reason": None,
    }

    if min(a_i, b_i, c_i, d_i) < 0:
        result["reason"] = "negative count — impossible contingency table"
        return result

    n = a_i + b_i + c_i + d_i
    if n == 0:
        result["reason"] = "no orders in either group"
        return result
    if n < min_total:
        result["reason"] = (
            f"insufficient sample (n={n}, need ≥{min_total} orders) — "
            "collect more orders before trusting a difference"
        )
        return result

    row1, row2 = a_i + b_i, c_i + d_i
    col1, col2 = a_i + c_i, b_i + d_i
    if row1 == 0 or row2 == 0 or col1 == 0 or col2 == 0:
        result["reason"] = "degenerate table — every order took the same outcome"
        return result

    # Yates: |ad - bc| - N/2, squared. Without the correction a 2x2 chi-square
    # overstates significance by ~5% (McNeil 1967) — on a fill-rate delta this
    # is exactly the comparison traders act on.
    chi_sq = (n * (abs(a_i * d_i - b_i * c_i) - n / 2.0) ** 2) / (row1 * row2 * col1 * col2)
    chi_sq = max(0.0, chi_sq)
    p = max(0.0, min(1.0, 2.0 * normal_sf(math.sqrt(chi_sq))))

    result.update(
        {
            "chi_square": round(chi_sq, 4),
            "p_value": round(p, 6),
            "significant": p < alpha,
            "testable": True,
        }
    )
    return result


def significance_block(
    sample_a: Optional[Iterable[float]],
    sample_b: Optional[Iterable[float]],
    alternative: str = "two-sided",
    alpha: float = ALPHA,
    min_sample: int = MIN_SAMPLE,
) -> Dict[str, Any]:
    """A comparison's ``statistical_significance`` block, shaped for the API.

    Always returns a dict with the same keys so the frontend never has to
    branch on "did the test run?" — a non-testable comparison still ships a
    block, it just says ``significant: false`` with a ``reason``.
    """
    block = mann_whitney_u(
        sample_a or [],
        sample_b or [],
        alternative=alternative,
        min_sample=min_sample,
    )
    block["alpha"] = alpha
    if block["testable"] and block["p_value"] is not None:
        block["confidence"] = f"{(1.0 - block['p_value']) * 100.0:.1f}%"
    else:
        block["confidence"] = None
    return block


def rate_significance_block(
    event_a: int,
    total_a: int,
    event_b: int,
    total_b: int,
    alpha: float = ALPHA,
    min_sample: int = MIN_SAMPLE,
) -> Dict[str, Any]:
    """``statistical_significance`` block for a RATE comparison (fill/reject).

    Same shape as :func:`significance_block` but tests the 2x2 contingency
    table, which is the correct test when the data are counts.
    """
    block = chi_square_2x2(
        int(event_a),
        int(total_a) - int(event_a),
        int(event_b),
        int(total_b) - int(event_b),
        alpha=alpha,
        min_total=min_sample,
    )
    block["alpha"] = alpha
    if block["testable"] and block["p_value"] is not None:
        block["confidence"] = f"{(1.0 - block['p_value']) * 100.0:.1f}%"
    else:
        block["confidence"] = None
    return block


def median(values: Iterable[float]) -> Optional[float]:
    """Median with ``None`` for an empty sample (never a fake 0.0)."""
    clean = sorted(_clean(values))
    if not clean:
        return None
    mid = len(clean) // 2
    if len(clean) % 2:
        return float(clean[mid])
    return (clean[mid - 1] + clean[mid]) / 2.0


def stdev(values: Iterable[float]) -> Optional[float]:
    """Sample standard deviation (``ddof=1``); ``None`` below 2 points."""
    clean = _clean(values)
    if len(clean) < 2:
        return None
    mean = sum(clean) / len(clean)
    var = sum((v - mean) ** 2 for v in clean) / (len(clean) - 1)
    return math.sqrt(var)


def describe(samples: Iterable[float], digits: int = 2) -> Tuple[Optional[float], ...]:
    """``(mean, median, std)`` rounded for display, ``None`` when undefined."""
    clean = _clean(samples)
    if not clean:
        return None, None, None
    mean = sum(clean) / len(clean)
    med = median(clean)
    sd = stdev(clean)
    rnd = lambda v: None if v is None else round(v, digits)  # noqa: E731
    return rnd(mean), rnd(med), rnd(sd)
