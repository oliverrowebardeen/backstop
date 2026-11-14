"""Clopper-Pearson bounds, bootstrap intervals and statistical labels. No network."""
from __future__ import annotations

import numpy as np
import pytest

from analysis.stats import (INCONCLUSIVE, NOT_SUPPORTED, SUPPORTED, bootstrap_mean_ci, clopper_pearson,
                            combine_labels, label_direction)


# ---------------------------------------------------------------- Clopper-Pearson

@pytest.mark.parametrize("k,n,lo,hi", [
    (0, 10, 0.0, 1 - 0.025 ** (1 / 10)),
    (10, 10, 0.025 ** (1 / 10), 1.0),
    (5, 10, 0.1871, 0.8129),
    (3, 18, 0.0358, 0.4142),
    (1, 100, 0.00025, 0.0545),
])
def test_clopper_pearson_reference_values(k, n, lo, hi):
    got = clopper_pearson(k, n)
    assert got[0] == pytest.approx(lo, abs=2e-4)
    assert got[1] == pytest.approx(hi, abs=2e-4)


def test_clopper_pearson_edges():
    assert clopper_pearson(0, 0) == (0.0, 1.0)
    with pytest.raises(ValueError):
        clopper_pearson(3, 2)
    lo90, hi90 = clopper_pearson(4, 40, level=0.90)
    lo95, hi95 = clopper_pearson(4, 40)
    assert lo95 < lo90 < 0.1 < hi90 < hi95


def test_bootstrap_ci_contains_mean_and_handles_empty():
    rng = np.random.default_rng(0)
    lo, hi = bootstrap_mean_ci([1] * 30 + [0] * 70, rng, n_boot=2000)
    assert lo < 0.3 < hi
    assert bootstrap_mean_ci([], rng) is None


# ---------------------------------------------------------------- labels

def test_label_direction():
    assert label_direction((0.02, 0.10), "above") == SUPPORTED
    assert label_direction((-0.10, -0.02), "above") == NOT_SUPPORTED
    assert label_direction((-0.02, 0.10), "above") == INCONCLUSIVE
    assert label_direction((-0.10, -0.02), "below") == SUPPORTED
    assert label_direction((0.02, 0.10), "below") == NOT_SUPPORTED
    assert label_direction(None, "below") == INCONCLUSIVE


def test_combine_labels():
    assert combine_labels([SUPPORTED, SUPPORTED]) == SUPPORTED
    assert combine_labels([SUPPORTED, NOT_SUPPORTED]) == NOT_SUPPORTED
    assert combine_labels([SUPPORTED, INCONCLUSIVE]) == INCONCLUSIVE
    assert combine_labels([INCONCLUSIVE, NOT_SUPPORTED]) == NOT_SUPPORTED
    assert combine_labels([]) == INCONCLUSIVE
