"""Search methods: grid, random, Bayesian (GP + EI) and genetic.

All methods search the same *discretized* space (every parameter's
``min..max`` by ``step``), so every candidate is a legal grid point, results
are cacheable/comparable across methods, and heatmaps line up.

The methods only decide WHICH points to evaluate; evaluation, caching,
progress and cancellation live in :class:`~backtest.optimization.evaluator.Evaluator`
behind the ``evaluate`` callback. Each method returns nothing — results are
collected by the caller's ``on_result`` hook.

The Bayesian optimizer is a small numpy Gaussian process (RBF kernel,
length-scale picked by marginal likelihood, Expected Improvement over a
random candidate pool, constant-liar batching so parallel workers each get a
distinct point). It replaces ``scikit-optimize``'s ``gp_minimize`` from the
PRD sketch: skopt is unmaintained and does not support numpy 2, and a
discrete-grid GP is ~100 lines.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Sequence

import numpy as np

from backtest.optimization.config import OptimizationConfig, ParameterSpec

#: evaluate(list_of_param_dicts) -> list of (params, guidance_score)
EvaluateFn = Callable[[list[dict[str, Any]]], list[tuple[dict[str, Any], float]]]


# ---------------------------------------------------------------------------
# Search space
# ---------------------------------------------------------------------------


@dataclass
class SearchSpace:
    """Discrete grid over the optimized params (+ constant fixed params)."""

    specs: tuple[ParameterSpec, ...]
    fixed: dict[str, Any]

    @classmethod
    def from_config(cls, cfg: OptimizationConfig) -> "SearchSpace":
        return cls(specs=cfg.optimized, fixed=cfg.fixed_params)

    def __post_init__(self) -> None:
        self.values = [spec.values() for spec in self.specs]
        self.sizes = [len(v) for v in self.values]

    @property
    def names(self) -> list[str]:
        return [s.name for s in self.specs]

    @property
    def total(self) -> int:
        return int(math.prod(self.sizes)) if self.sizes else 1

    def point(self, indices: Sequence[int]) -> dict[str, Any]:
        params = dict(self.fixed)
        for spec, vals, i in zip(self.specs, self.values, indices):
            params[spec.name] = vals[int(i)]
        return params

    def decode(self, flat: int) -> list[int]:
        """Mixed-radix decode of a flat grid index (last param varies fastest)."""
        idx = []
        for size in reversed(self.sizes):
            idx.append(flat % size)
            flat //= size
        return list(reversed(idx))

    def iter_grid(self) -> Iterator[dict[str, Any]]:
        for combo in itertools.product(*[range(s) for s in self.sizes]):
            yield self.point(combo)

    def indices_of(self, params: dict[str, Any]) -> list[int]:
        return [spec.index_of(params[spec.name]) for spec in self.specs]

    def unit(self, indices: np.ndarray) -> np.ndarray:
        """Map grid indices to [0, 1]^d (for the GP)."""
        denom = np.array([max(s - 1, 1) for s in self.sizes], dtype=float)
        return np.asarray(indices, dtype=float) / denom

    def random_indices(self, rng: np.random.Generator, n: int) -> np.ndarray:
        return np.column_stack([rng.integers(0, s, size=n) for s in self.sizes]) \
            if self.sizes else np.zeros((n, 0), dtype=int)

    def sample_distinct(self, rng: np.random.Generator, n: int) -> list[list[int]]:
        """``n`` distinct grid points without replacement."""
        n = min(n, self.total)
        if self.total <= 5_000_000:
            flats = rng.choice(self.total, size=n, replace=False)
            return [self.decode(int(f)) for f in flats]
        seen: set[tuple] = set()  # pragma: no cover - astronomically large grids
        out = []
        while len(out) < n:
            idx = tuple(int(i) for i in self.random_indices(rng, 1)[0])
            if idx not in seen:
                seen.add(idx)
                out.append(list(idx))
        return out


def _chunks(it: Iterator[dict], size: int) -> Iterator[list[dict]]:
    while True:
        batch = list(itertools.islice(it, size))
        if not batch:
            return
        yield batch


# ---------------------------------------------------------------------------
# Grid / random
# ---------------------------------------------------------------------------


def grid_search(space: SearchSpace, evaluate: EvaluateFn, batch_size: int = 64) -> None:
    """Exhaustive: every combination, streamed in batches."""
    for batch in _chunks(space.iter_grid(), batch_size):
        evaluate(batch)


def random_search(
    space: SearchSpace, evaluate: EvaluateFn, n_samples: int, seed: int = 42,
    batch_size: int = 64,
) -> None:
    """``n_samples`` distinct uniformly-random grid points."""
    rng = np.random.default_rng(seed)
    points = [space.point(i) for i in space.sample_distinct(rng, n_samples)]
    for start in range(0, len(points), batch_size):
        evaluate(points[start:start + batch_size])


# ---------------------------------------------------------------------------
# Bayesian optimization (numpy GP + Expected Improvement)
# ---------------------------------------------------------------------------


def _erf(x: np.ndarray) -> np.ndarray:
    """Abramowitz–Stegun 7.1.26 (|error| < 1.5e-7) — numpy has no erf."""
    sign = np.sign(x)
    x = np.abs(x)
    t = 1.0 / (1.0 + 0.3275911 * x)
    poly = t * (0.254829592 + t * (-0.284496736 + t * (1.421413741
                                                       + t * (-1.453152027 + t * 1.061405429))))
    return sign * (1.0 - poly * np.exp(-x * x))


def _norm_cdf(z: np.ndarray) -> np.ndarray:
    return 0.5 * (1.0 + _erf(z / math.sqrt(2.0)))


def _norm_pdf(z: np.ndarray) -> np.ndarray:
    return np.exp(-0.5 * z * z) / math.sqrt(2.0 * math.pi)


class _GP:
    """Zero-mean GP with an isotropic RBF kernel on standardized targets."""

    LENGTH_SCALES = (0.08, 0.15, 0.25, 0.4, 0.7, 1.2)

    def __init__(self, noise: float = 1e-4) -> None:
        self.noise = noise

    @staticmethod
    def _kernel(a: np.ndarray, b: np.ndarray, ls: float) -> np.ndarray:
        d2 = ((a[:, None, :] - b[None, :, :]) ** 2).sum(-1)
        return np.exp(-0.5 * d2 / (ls * ls))

    def fit(self, X: np.ndarray, y: np.ndarray) -> "_GP":
        self.X = X
        self.mu_y = float(y.mean())
        self.sd_y = float(y.std()) or 1.0
        yn = (y - self.mu_y) / self.sd_y
        best = None
        for ls in self.LENGTH_SCALES:
            K = self._kernel(X, X, ls) + self.noise * np.eye(len(X))
            try:
                L = np.linalg.cholesky(K)
            except np.linalg.LinAlgError:
                continue
            alpha = np.linalg.solve(L.T, np.linalg.solve(L, yn))
            lml = -0.5 * yn @ alpha - np.log(np.diag(L)).sum()
            if best is None or lml > best[0]:
                best = (lml, ls, L, alpha)
        if best is None:  # pragma: no cover - jitter fallback
            ls = 0.3
            K = self._kernel(X, X, ls) + 1e-2 * np.eye(len(X))
            L = np.linalg.cholesky(K)
            best = (0.0, ls, L, np.linalg.solve(L.T, np.linalg.solve(L, yn)))
        _, self.ls, self.L, self.alpha = best
        return self

    def predict(self, Xs: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        Ks = self._kernel(Xs, self.X, self.ls)
        mu = Ks @ self.alpha
        v = np.linalg.solve(self.L, Ks.T)
        var = np.clip(1.0 - (v * v).sum(0), 1e-12, None)
        return mu * self.sd_y + self.mu_y, np.sqrt(var) * self.sd_y


def _expected_improvement(mu: np.ndarray, sd: np.ndarray, best: float,
                          xi: float = 0.01) -> np.ndarray:
    imp = mu - best - xi
    z = np.where(sd > 0, imp / sd, 0.0)
    ei = imp * _norm_cdf(z) + sd * _norm_pdf(z)
    return np.where(sd > 0, ei, 0.0)


def bayesian_search(
    space: SearchSpace,
    evaluate: EvaluateFn,
    n_calls: int,
    n_initial: int | None = None,
    seed: int = 42,
    batch_size: int = 1,
    candidate_pool: int = 2048,
) -> None:
    """GP-guided search: ``n_initial`` random points, then EI-chosen batches."""
    rng = np.random.default_rng(seed)
    n_calls = min(n_calls, space.total)
    n_init = n_initial or max(5, min(12, n_calls // 4))
    n_init = min(n_init, n_calls)

    observed: dict[tuple, float] = {}

    def run(points: list[list[int]]) -> None:
        results = evaluate([space.point(p) for p in points])
        for params, score in results:
            observed[tuple(space.indices_of(params))] = float(score)

    run(space.sample_distinct(rng, n_init))
    while len(observed) < n_calls:
        keys = list(observed)
        X = space.unit(np.array(keys))
        y = np.array([observed[k] for k in keys], dtype=float)
        finite = y[y > -1e5]
        if finite.size:  # penalized (failed / violating) points: just below the worst
            floor = float(finite.min()) - (float(finite.max() - finite.min()) or 1.0)
            y = np.where(y > -1e5, y, floor)
        want = min(batch_size, n_calls - len(observed))
        # candidates: the whole grid when small, else a random pool
        if space.total <= candidate_pool:
            cand = np.array([space.decode(i) for i in range(space.total)])
        else:
            cand = space.random_indices(rng, candidate_pool)
        seen = set(observed)
        mask = np.array([tuple(c) not in seen for c in cand.tolist()], dtype=bool)
        cand = cand[mask]
        if cand.size == 0:
            break
        chosen: list[list[int]] = []
        Xb, yb = X.copy(), y.copy()
        for _ in range(want):
            gp = _GP().fit(Xb, yb)
            mu, sd = gp.predict(space.unit(cand))
            ei = _expected_improvement(mu, sd, float(yb.max()))
            j = int(np.argmax(ei)) if np.any(ei > 0) else int(rng.integers(0, len(cand)))
            pick = cand[j].tolist()
            chosen.append(pick)
            # constant liar: pretend the pick scored its predicted mean
            Xb = np.vstack([Xb, space.unit(np.array([pick]))])
            yb = np.append(yb, mu[j])
            cand = np.delete(cand, j, axis=0)
            if cand.size == 0:
                break
        run(chosen)


# ---------------------------------------------------------------------------
# Genetic algorithm
# ---------------------------------------------------------------------------


def genetic_search(
    space: SearchSpace,
    evaluate: EvaluateFn,
    population: int = 20,
    generations: int = 10,
    seed: int = 42,
    mutation_rate: float = 0.2,
    elite: int = 2,
) -> None:
    """Tournament selection, uniform crossover, step mutation, elitism.

    Individuals are grid-index vectors, so offspring are always legal grid
    points. Duplicates are re-mutated to keep evaluations distinct; the
    budget is ``population × generations`` fresh evaluations (capped by the
    grid size).
    """
    rng = np.random.default_rng(seed)
    budget = min(population * generations, space.total)
    scores: dict[tuple, float] = {}

    def run(pop: list[tuple]) -> None:
        fresh = [p for p in dict.fromkeys(pop) if p not in scores]
        if fresh:
            for params, score in evaluate([space.point(p) for p in fresh]):
                scores[tuple(space.indices_of(params))] = float(score)

    pop = [tuple(p) for p in space.sample_distinct(rng, min(population, budget))]
    run(pop)
    stall = 0
    while len(scores) < budget and stall < 5:
        ranked = sorted(set(pop), key=lambda p: scores.get(p, -1e9), reverse=True)
        nxt: list[tuple] = ranked[:elite]

        def tournament() -> tuple:
            picks = [ranked[int(rng.integers(0, len(ranked)))] for _ in range(3)]
            return max(picks, key=lambda p: scores.get(p, -1e9))

        attempts = 0
        remaining = budget - len(scores)
        target_new = min(population - len(nxt), remaining)
        new_children: list[tuple] = []
        while len(new_children) < target_new and attempts < population * 20:
            attempts += 1
            a, b = tournament(), tournament()
            mask = rng.random(len(a)) < 0.5
            child = [a[i] if mask[i] else b[i] for i in range(len(a))]
            for i, size in enumerate(space.sizes):
                if rng.random() < mutation_rate:
                    delta = int(rng.integers(1, max(2, size // 4) + 1)) * (
                        1 if rng.random() < 0.5 else -1)
                    child[i] = int(min(max(child[i] + delta, 0), size - 1))
            ct = tuple(int(c) for c in child)
            if ct in scores or ct in new_children:
                continue
            new_children.append(ct)
        before = len(scores)
        pop = nxt + new_children
        run(pop)
        stall = stall + 1 if len(scores) == before else 0


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def run_method(
    cfg: OptimizationConfig,
    space: SearchSpace,
    evaluate: EvaluateFn,
    *,
    budget: int | None = None,
    batch_size: int = 8,
    method: str | None = None,
) -> None:
    """Run ``cfg.method`` (or ``method``) with the config's budgets.

    ``budget`` caps the number of evaluations (walk-forward splits pass
    ``max_evals_per_split``); a grid that does not fit the budget falls back
    to random sampling of that many points.
    """
    ms = cfg.method_settings
    method = method or cfg.method
    total = space.total
    if method == "grid":
        if budget is not None and total > budget:
            random_search(space, evaluate, budget, seed=ms.seed, batch_size=max(batch_size, 16))
        else:
            grid_search(space, evaluate, batch_size=max(batch_size, 16))
        return
    if method == "random":
        n = ms.n_samples or max(10, math.ceil(total * 0.2))
        if budget is not None:
            n = min(n, budget)
        random_search(space, evaluate, n, seed=ms.seed, batch_size=max(batch_size, 16))
        return
    if method == "bayesian":
        n_calls = ms.n_calls if budget is None else min(ms.n_calls, budget)
        n_init = ms.n_initial
        if n_init is not None:
            n_init = min(n_init, n_calls)
        bayesian_search(space, evaluate, n_calls=n_calls, n_initial=n_init,
                        seed=ms.seed, batch_size=max(1, batch_size))
        return
    if method == "genetic":
        gens = ms.generations
        if budget is not None:
            gens = max(1, min(gens, math.ceil(budget / max(ms.population, 1))))
        genetic_search(space, evaluate, population=ms.population, generations=gens,
                       seed=ms.seed)
        return
    raise ValueError(f"unknown method: {method}")
