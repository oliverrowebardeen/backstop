"""R8/R10: judge replies are validated, malformed output never becomes a label, cache keys carry provenance."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from evals import judge


def reply(obj, stop="end_turn"):
    return SimpleNamespace(text=json.dumps(obj), cost_usd=0.001, stop_reason=stop, json=None, parsed=None)


@pytest.fixture()
def scripted(eval_fakes, monkeypatch, tmp_path):
    monkeypatch.setattr(eval_fakes, "JUDGE_CACHE", tmp_path)
    queue = []

    async def complete(role, system, messages, **kw):
        return queue.pop(0)

    monkeypatch.setattr(eval_fakes, "llm", SimpleNamespace(complete=complete))
    return queue


Q = SimpleNamespace(question="Who founded X?", gold=["Ada"], is_trap=False, trap_note=None)
RESULTS = [{"title": "t", "snippet": "Ada founded X", "url": "https://a.example"}]


@pytest.mark.parametrize("bad", [
    {"hit": "false", "hit_rank": 999, "reason": ""},   # string boolean
    {"hit": True, "hit_rank": None, "reason": ""},      # hit without evidence
    {"hit": True, "hit_rank": 2, "reason": ""},         # rank outside the list
    {"hit": False, "hit_rank": 1, "reason": ""},        # miss with a rank
])
def test_malformed_hit_is_rejected_then_retried(scripted, bad):
    scripted += [reply(bad), reply(bad), reply(bad)]
    with pytest.raises(judge.JudgeError):
        asyncio.run(judge.judge_hit(Q, RESULTS))


def test_valid_reply_after_a_malformed_one(scripted):
    scripted += [reply({"hit": "yes"}), reply({"hit": True, "hit_rank": 1, "reason": "r1"})]
    assert asyncio.run(judge.judge_hit(Q, RESULTS)) == (True, 1, "r1")


def test_unknown_grade_is_an_error_not_wrong(scripted):
    scripted += [reply({"grade": "unknown", "reason": ""})] * 3
    with pytest.raises(judge.JudgeError):
        asyncio.run(judge.grade_answer(Q, "Ada"))


def test_truncated_reply_is_malformed(scripted):
    scripted += [reply({"grade": "correct", "reason": ""}, stop="max_tokens"),
                 reply({"grade": "correct", "reason": ""})]
    assert asyncio.run(judge.grade_answer(Q, "Ada")) == "correct"


def test_cache_key_changes_with_model_and_cache_off_is_honored(scripted, monkeypatch, tmp_path):
    scripted += [reply({"grade": "correct", "reason": ""})]
    assert asyncio.run(judge.grade_answer(Q, "Ada")) == "correct"
    assert len(list(tmp_path.glob("*.json"))) == 1
    monkeypatch.setattr(judge.config, "JUDGE_MODEL", "another-model")
    scripted += [reply({"grade": "wrong", "reason": ""})]
    assert asyncio.run(judge.grade_answer(Q, "Ada")) == "wrong"  # not served the other model's grade
    monkeypatch.setattr(judge.config, "SO_CACHE", "off")
    scripted += [reply({"grade": "declined", "reason": ""})]
    assert asyncio.run(judge.grade_answer(Q, "Ada")) == "declined"
    assert len(list(tmp_path.glob("*.json"))) == 2  # nothing written with the cache off


def test_corrupt_cache_entry_is_rejudged(scripted, tmp_path):
    scripted += [reply({"grade": "correct", "reason": ""})]
    asyncio.run(judge.grade_answer(Q, "Ada"))
    entry = next(tmp_path.glob("*.json"))
    entry.write_text(json.dumps({"grade": "maybe"}))
    scripted += [reply({"grade": "wrong", "reason": ""})]
    assert asyncio.run(judge.grade_answer(Q, "Ada")) == "wrong"
