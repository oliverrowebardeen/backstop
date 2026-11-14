"""Small exact and resampling statistics for lane A4 (no scipy dependency).

- `clopper_pearson`: exact binomial confidence interval, by bisection on the
  binomial tail computed in log space.
- `bootstrap_mean_ci`: percentile bootstrap interval for a mean of 0/1 values.
- `label_direction`: turns an interval on a difference into SUPPORTED,
  NOT SUPPORTED or INCONCLUSIVE.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Literal

import numpy as np

SUPPORTED = "SUPPORTED"
NOT_SUPPORTED = "NOT SUPPORTED"
INCONCLUSIVE = "INCONCLUSIVE"
Direction = Literal["above", "below"]


def _log_pmf(i: int, n: int, p: float) -> float:
    return (math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1)
            + i * math.log(p) + (n - i) * math.log1p(-p))


def binom_cdf(k: int, n: int, p: float) -> float:
    """P(X <= k) for X ~ Binomial(n, p)."""
    if k < 0:
        return 0.0
    if k >= n or p <= 0.0:
        return 1.0
    if p >= 1.0:
        return 0.0
    return min(1.0, sum(math.exp(_log_pmf(i, n, p)) for i in range(k + 1)))


def _bisect(f, target: float, increasing: bool, iters: int = 100) -> float:
    lo, hi = 0.0, 1.0
    for _ in range(iters):
        mid = (lo + hi) / 2
        if (f(mid) < target) == increasing:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def clopper_pearson(k: int, n: int, level: float = 0.95) -> tuple[float, float]:
    """Exact two-sided Clopper-Pearson interval for k successes in n trials.

    Returns (0.0, 1.0) when n is 0. Raises ValueError on k outside [0, n].
    """
    if n < 0 or k < 0 or k > n:
        raise ValueError(f"need 0 <= k <= n, got k={k}, n={n}")
    if n == 0:
        return 0.0, 1.0
    a = (1 - level) / 2
    # Lower: P(X >= k | p) = a, i.e. 1 - cdf(k - 1, p) = a; increasing in p.
    lower = 0.0 if k == 0 else _bisect(lambda p: 1 - binom_cdf(k - 1, n, p), a, increasing=True)
    # Upper: P(X <= k | p) = a; decreasing in p.
    upper = 1.0 if k == n else _bisect(lambda p: binom_cdf(k, n, p), a, increasing=False)
    return lower, upper


def bootstrap_mean_ci(values: Sequence[float] | np.ndarray, rng: np.random.Generator,
                      n_boot: int = 10_000, level: float = 0.95) -> tuple[float, float] | None:
    """Percentile bootstrap interval for the mean. None when there are no values."""
    x = np.asarray(values, dtype=float)
    if x.size == 0:
        return None
    idx = rng.integers(0, x.size, size=(n_boot, x.size))
    means = x[idx].mean(axis=1)
    a = (1 - level) / 2
    return float(np.quantile(means, a)), float(np.quantile(means, 1 - a))


def label_direction(ci: tuple[float, float] | None, direction: Direction) -> str:
    """Label a claimed difference from its interval.

    direction "above": the claim says the difference is positive. SUPPORTED if
    the whole interval is above 0, NOT SUPPORTED if it is wholly below 0,
    INCONCLUSIVE if it contains 0 or there is no data. "below" is the mirror.
    """
    if ci is None:
        return INCONCLUSIVE
    lo, hi = ci
    if direction == "below":
        lo, hi = -hi, -lo
    if lo > 0:
        return SUPPORTED
    if hi < 0:
        return NOT_SUPPORTED
    return INCONCLUSIVE


def combine_labels(labels: Sequence[str]) -> str:
    """A claim with several parts: any NOT SUPPORTED part sinks it; all SUPPORTED supports it."""
    if not labels:
        return INCONCLUSIVE
    if NOT_SUPPORTED in labels:
        return NOT_SUPPORTED
    if all(lab == SUPPORTED for lab in labels):
        return SUPPORTED
    return INCONCLUSIVE
