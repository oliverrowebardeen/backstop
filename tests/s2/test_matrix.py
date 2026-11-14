"""End-to-end matrix and judge checks on the opt-in fakes (no paid calls)."""
import asyncio
import importlib
import json
import os
import shutil
import sys

import pytest


@pytest.fixture()
def fake_env(monkeypatch, tmp_path):
    monkeypatch.setenv("SO_FAKE", "1")
    monkeypatch.setenv("SO_CACHE_DIR", str(tmp_path / "cache"))
    for mod in [m for m in sys.modules if m.startswith(("evals", "so.config", "tests.s2.fakes"))]:
        del sys.modules[mod]
    import evals.deps as deps
    shutil.rmtree(deps.OUT, ignore_errors=True)
    yield deps
    shutil.rmtree(deps.OUT, ignore_errors=True)


def test_fake_mode_never_writes_real_out(fake_env):
    assert fake_env.OUT.name == "fake"


def test_matrix_runs_and_resumes(fake_env):
    matrix = importlib.import_module("evals.matrix")
    asyncio.run(matrix.run(limit=6, concurrency=4, skip_default=False))
    rows = [json.loads(l) for l in matrix.MATRIX.read_text().splitlines()]
    assert len(rows) == 6 * len(fake_env.ENGINES)
    assert all({"question_id", "engine", "hit", "hit_rank", "judge_reason"} <= r.keys() for r in rows)
    assert all(r["hit_rank"] is None or 1 <= r["hit_rank"] <= 5 for r in rows)
    assert len(matrix.DEFAULT_MATRIX.read_text().splitlines()) == 6
    calls_before = matrix.SPEND["search_calls"]
    asyncio.run(matrix.run(limit=6, concurrency=4, skip_default=False))
    assert matrix.SPEND["search_calls"] == calls_before  # resume: nothing re-searched


def test_grade_answer_declines_not_found_without_a_call(fake_env):
    judge = importlib.import_module("evals.judge")
    from data.load import load_questions
    q = load_questions(split="test")[0]
    before = judge.SPEND["judge_calls"]
    assert asyncio.run(judge.grade_answer(q, "NOT FOUND")) == "declined"
    assert judge.SPEND["judge_calls"] == before


def test_record_errors_writes_flagged_miss(fake_env, monkeypatch):
    matrix = importlib.import_module("evals.matrix")

    async def boom(*a, **k):
        raise RuntimeError("503 from engine")

    monkeypatch.setattr(matrix.telem_client, "search_engines", boom)
    asyncio.run(matrix.run(limit=1, concurrency=1, skip_default=True, record_errors=True))
    rows = [json.loads(l) for l in matrix.MATRIX.read_text().splitlines()]
    assert len(rows) == len(fake_env.ENGINES)
    assert all(r["engine_error"] and r["hit"] is False and r["n_results"] == 0 for r in rows)
    asyncio.run(matrix.rejudge(concurrency=1))  # must not fail on rows without results
