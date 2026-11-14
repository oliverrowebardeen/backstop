"""End-to-end matrix and judge checks on the fakes injected by tests/s2/conftest.py (no paid calls)."""
import asyncio
import json

from evals import judge, matrix
from evals.deps import ENGINES


def test_outputs_go_to_tmp(eval_fakes, tmp_path):
    assert eval_fakes.OUT == tmp_path / "out"
    assert matrix.matrix_path().is_relative_to(tmp_path)


def test_matrix_runs_and_resumes():
    asyncio.run(matrix.run(limit=6, concurrency=4, skip_default=False))
    rows = [json.loads(l) for l in matrix.matrix_path().read_text().splitlines()]
    assert len(rows) == 6 * len(ENGINES)
    assert all({"question_id", "engine", "hit", "hit_rank", "judge_reason"} <= r.keys() for r in rows)
    assert all(r["hit_rank"] is None or 1 <= r["hit_rank"] <= 5 for r in rows)
    assert len(matrix.default_matrix_path().read_text().splitlines()) == 6
    calls_before = matrix.SPEND["search_calls"]
    asyncio.run(matrix.run(limit=6, concurrency=4, skip_default=False))
    assert matrix.SPEND["search_calls"] == calls_before  # resume: nothing re-searched


def test_grade_answer_declines_not_found_without_a_call():
    from data.load import load_questions
    q = load_questions(split="test")[0]
    before = judge.SPEND["judge_calls"]
    assert asyncio.run(judge.grade_answer(q, "NOT FOUND")) == "declined"
    assert judge.SPEND["judge_calls"] == before


def test_record_errors_writes_flagged_miss(eval_fakes, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("503 from engine")

    monkeypatch.setattr(eval_fakes.telem_client, "search_engines", boom)
    asyncio.run(matrix.run(limit=1, concurrency=1, skip_default=True, record_errors=True))
    rows = [json.loads(l) for l in matrix.matrix_path().read_text().splitlines()]
    assert len(rows) == len(ENGINES)
    assert all(r["engine_error"] and r["hit"] is False and r["n_results"] == 0 for r in rows)
    asyncio.run(matrix.rejudge(concurrency=1))  # must not fail on rows without results
