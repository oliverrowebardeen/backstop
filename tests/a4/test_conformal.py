"""Conformal threshold selection, held-out evaluation and leakage guards (lane A4). No network.

Nothing from evals is imported at module level, so collecting this file cannot
freeze evals.deps before another lane's conftest sets SO_FAKE.
"""
from __future__ import annotations

import pytest

from analysis import conformal
from analysis.conformal import CheckPoint, LeakageError, assert_disjoint, evaluate, select_threshold
from so.eventlog import EventLog


def _pts(spec: list[tuple[float, bool]], extra_misses: int = 0) -> list[CheckPoint]:
    """HIT points from (confidence, truth_hit) pairs, plus non-HIT checks."""
    pts = [CheckPoint(f"q{i}", "HIT", c, t) for i, (c, t) in enumerate(spec)]
    pts += [CheckPoint(f"m{i}", "DEAD", 0.9, False) for i in range(extra_misses)]
    return pts


def synthetic() -> list[CheckPoint]:
    # 20 HITs at 0.95 all right, 10 at 0.85 with 4 wrong, 10 at 0.70 with 5 wrong.
    spec = [(0.95, True)] * 20 + [(0.85, True)] * 6 + [(0.85, False)] * 4 \
        + [(0.70, True)] * 5 + [(0.70, False)] * 5
    return _pts(spec, extra_misses=7)


def test_threshold_known_answer():
    pts = synthetic()
    # lambda 0.70: (9 + 1) / (40 + 1) = 0.244; 0.85: (4 + 1) / (30 + 1) = 0.161; 0.95: 1 / 21 = 0.048.
    assert select_threshold(pts, 0.10).lam == 0.95
    assert select_threshold(pts, 0.05).lam == 0.95
    t = select_threshold(pts, 0.20)
    assert (t.lam, t.n_hits, t.n_wrong, t.vacuous) == (0.85, 30, 4, False)
    assert t.bound == pytest.approx(5 / 31, abs=1e-6)
    assert select_threshold(pts, 0.25).lam == 0.70


def test_threshold_is_smallest_even_when_ratio_not_monotone():
    # Few HITs at the top make the ratio rise there; the rule still picks the smallest passing lambda.
    spec = [(0.96, True)] * 2 + [(0.90, True)] * 40 + [(0.80, True)] * 10 + [(0.80, False)] * 1
    pts = _pts(spec)
    t = select_threshold(pts, 0.10)
    assert t.lam == 0.80 and t.n_hits == 53 and t.n_wrong == 1
    assert conformal.conformal_ratio(pts, 0.96)[0] > 0.10


def test_vacuous_when_too_few_hits():
    pts = _pts([(0.9, True)] * 5)  # best possible ratio 1/6
    t = select_threshold(pts, 0.10)
    assert t.vacuous and t.lam is None
    assert evaluate(pts, t.lam)["hits_kept"] == 0
    assert "vacuous" in conformal.slide_sentence(t, evaluate(pts, t.lam))


def test_alpha_validated():
    with pytest.raises(ValueError):
        select_threshold(synthetic(), 0.0)


def test_evaluate_counts_and_interval():
    pts = _pts([(0.95, True)] * 15 + [(0.95, False)] * 3 + [(0.80, False)] * 2, extra_misses=11)
    r = evaluate(pts, 0.9)
    assert (r["hits_kept"], r["wrong_hits_kept"], r["hits_total"]) == (18, 3, 20)
    assert r["wrong_hit_rate"] == pytest.approx(3 / 18, abs=1e-4)
    assert r["wrong_hit_rate_ci95"] == pytest.approx([0.0358, 0.4142], abs=2e-4)
    assert r["coverage"] == pytest.approx(18 / 31, abs=1e-4)
    assert r["hit_retention"] == pytest.approx(0.9)


def test_assert_disjoint_raises_on_overlap():
    assert_disjoint({"a", "b"}, {"c"})
    with pytest.raises(LeakageError):
        assert_disjoint({"a", "b"}, {"b", "c"})


async def test_test_id_cannot_reach_calibration():
    from data.load import load_questions

    learn_id = load_questions(split="learn")[0].id
    # Claim a learn id is a test id: calibration must refuse before any checker call.
    with pytest.raises(LeakageError):
        await conformal.calibration_points("2026-11-14", forbidden_ids={learn_id})


def _events():
    def ev(t, **data):
        return {"type": t, "data": data}
    return [
        ev("search", engines=["tavily"]), ev("check", verdict="SHALLOW", confidence=0.8),
        ev("check", verdict="DEAD", confidence=0.9),  # page re-check: not a first check
        ev("search", engines=["exa"]), ev("check", verdict="HIT", confidence=0.95),
        ev("search", engines=["tavily"]), ev("check", verdict="HIT", confidence=0.93),
    ]


def test_check_join_matches_evals_score():
    # Imported here, not at module level: evals.deps reads SO_FAKE at import time and the
    # s4 conftest sets it during collection, after this module is collected.
    from evals.score import first_engine_checks

    events = _events()
    ours = conformal.first_engine_check_events(events, "tavily")
    assert [d["verdict"] for d in ours] == first_engine_checks(events, "tavily") == ["SHALLOW", "HIT"]
    assert [d["confidence"] for d in ours] == [0.8, 0.93]


def _write_run(log_dir, qid, checks, setup="second_opinion"):
    log = EventLog(f"{setup}-{qid}-abc", qid, setup, log_dir=log_dir)
    log.emit("run_start", question="q")
    for verdict, conf in checks:
        log.emit("search", query="q", goal="g", engines=["tavily"], n_results=5, latency_ms=1.0,
                 cost_usd=0.005, cached=True)
        log.emit("check", verdict=verdict, confidence=conf, why="", evidence_rank=None)
    log.emit("answer", final="x", status="ANSWERED", citations=[])
    log.emit("run_end", engine_calls=1, fetches=0, searches=1, cost_usd_search=0.005, cost_usd_llm=0.0,
             cost_usd_total=0.005, latency_ms=1.0)


def test_test_points_join_with_matrix(tmp_path):
    _write_run(tmp_path, "t1", [("HIT", 0.95)])
    _write_run(tmp_path, "t2", [("SHALLOW", 0.8), ("HIT", 0.9)])
    _write_run(tmp_path, "l1", [("HIT", 0.95)])  # not a test id: ignored
    _write_run(tmp_path, "t3", [("HIT", 0.95)], setup="single")  # other setup: ignored
    matrix = {("t1", "tavily"): True, ("t2", "tavily"): False}
    pts, counts = conformal.test_points(tmp_path, matrix, {"t1", "t2", "t3"}, "tavily")
    assert sorted((p.question_id, p.verdict, p.truth_hit) for p in pts) == [
        ("t1", "HIT", True), ("t2", "HIT", False), ("t2", "SHALLOW", False)]
    assert counts["finished_runs"] == 2 and counts["outside_test"] == 1
