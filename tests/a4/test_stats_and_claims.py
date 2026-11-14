"""Clopper-Pearson bounds, claim labeling and rescue-per-dollar logic (lane A4). No network."""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from analysis import hn_claims
from analysis.stats import (INCONCLUSIVE, NOT_SUPPORTED, SUPPORTED, bootstrap_mean_ci, clopper_pearson,
                            combine_labels, label_direction)

ENGINES = ["a", "b", "c", "d"]


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


def _arrays(n=200, seed=1):
    """Engine a hits 95%, b and c 60%, d 5%, independently per question."""
    rng = np.random.default_rng(seed)
    p = np.array([0.95, 0.6, 0.6, 0.05])
    arr = (rng.random((n, 4)) < p).astype(float)
    return {"overall": arr, "finance": arr[:60], "fact": arr[60:], "recent_2025_2026": np.zeros((0, 4))}


PRICES = {"billed": {"a": 0.005, "b": 0.005, "c": 0.005, "d": 0.00005},
          "list": {"a": 0.008, "b": 0.005, "c": None, "d": 0.00005}}


def _claims():
    return [
        {"id": "strong_a", "engine": "a", "text": "a is strong", "caveat": None,
         "parts": [{"what": "strong", "kind": "vs_median", "subset": "overall", "direction": "above"}]},
        {"id": "cheap_good_d", "engine": "d", "text": "d is cheap and good", "caveat": None,
         "parts": [{"what": "cheap", "kind": "price"},
                   {"what": "good", "kind": "vs_median", "subset": "overall", "direction": "above"}]},
        {"id": "b_not_best", "engine": "b", "text": "b not best on finance", "caveat": None,
         "parts": [{"what": "not best", "kind": "vs_best", "subset": "finance", "direction": "below"}]},
        {"id": "news", "engine": "a", "text": "a bad at news", "caveat": None,
         "parts": [{"what": "news", "kind": "vs_median", "subset": "recent_2025_2026", "direction": "below"}]},
    ]


def test_claim_labeling_logic():
    out = {c["id"]: c for c in hn_claims.judge_claims(_arrays(), ENGINES, PRICES, np.random.default_rng(3),
                                                      claims=_claims(), n_boot=2000)}
    assert out["strong_a"]["label"] == SUPPORTED
    d = out["cheap_good_d"]
    assert [p["label"] for p in d["parts"]] == [SUPPORTED, NOT_SUPPORTED]
    assert d["label"] == NOT_SUPPORTED
    assert out["b_not_best"]["label"] == SUPPORTED
    assert out["b_not_best"]["parts"][0]["against"] == "best other engine (a)"
    assert out["news"]["label"] == INCONCLUSIVE and out["news"]["parts"][0]["n"] == 0


def test_price_label():
    assert hn_claims.price_label("d", PRICES["billed"], ENGINES)[0] == SUPPORTED
    assert hn_claims.price_label("a", PRICES["billed"], ENGINES)[0] == NOT_SUPPORTED
    assert hn_claims.price_label("c", PRICES["list"], ENGINES)[0] == INCONCLUSIVE


def test_mentions_recent():
    q = SimpleNamespace(question="What was revenue in FY2025?", gold=["$3B"])
    assert hn_claims.mentions_recent(q)
    assert hn_claims.mentions_recent(SimpleNamespace(question="When?", gold=["March 2026"]))
    assert not hn_claims.mentions_recent(SimpleNamespace(question="In 20251 units?", gold=["2024"]))


def test_rescues_per_dollar_order_and_unpriced():
    hits = {f"q{i}": {"a": False, "b": i < 5, "c": i < 2, "d": i < 1} for i in range(10)}
    hits["q99"] = {"a": True, "b": True, "c": True, "d": True}  # not a miss: ignored
    r = hn_claims.rescues_per_dollar(hits, list(hits), {"b": 0.005, "c": 0.001, "d": None}, "a", ENGINES)
    assert r["misses"] == 10
    rows = {x["engine"]: x for x in r["rows"]}
    assert rows["b"]["rescued"] == 5 and rows["b"]["rescues_per_dollar"] == pytest.approx(100.0)
    assert rows["c"]["rescues_per_dollar"] == pytest.approx(200.0)
    assert r["order"] == ["c", "b"] and r["unpriced"] == ["d"]


def test_oracle_capture_simulated_and_measured():
    arr = np.array([[1, 0], [0, 1], [0, 0], [1, 1]], dtype=float)
    frontier = {"points": [{"kind": "second_opinion", "coverage": 0.6, "cost_per_q": 0.006,
                            "simulated": True, "label": "simulated"}]}
    sim = hn_claims.oracle_capture(arr, None, frontier)
    assert sim["oracle_rate"] == 0.75 and sim["headline"] == "simulated"
    assert sim["simulated"]["share_of_ceiling"] == pytest.approx(0.8)
    assert sim["simulated"]["cost_per_captured"] == pytest.approx(0.01)
    res = {"setups": {"second_opinion": {"n": 4, "accuracy": 0.5, "cost_per_correct": 0.02}}}
    meas = hn_claims.oracle_capture(arr, res, frontier)
    assert meas["headline"] == "measured"
    assert meas["measured"]["share_of_ceiling"] == pytest.approx(0.5 / 0.75, abs=1e-4)
