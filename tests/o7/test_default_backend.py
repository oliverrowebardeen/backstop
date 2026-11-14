import json
import logging
import os
import subprocess
import sys
from unittest.mock import AsyncMock

import pytest

from evals import run_eval
from so import agent, checker, checker_eval, config, decision_checker as dc, llm, telem_client
from so.contracts import EngineRun, LLMResponse, Question, Result, ToolCall

pytestmark = pytest.mark.checker_backend


@pytest.fixture(autouse=True)
def isolate_warning_and_budget(monkeypatch, tmp_path):
    monkeypatch.setattr(dc, "_fallback_warning_emitted", False)
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(config, "LLM_BUDGET", config.Budget(0))
    monkeypatch.setattr(config, "DECISION_BUDGET", config.Budget(0))


@pytest.fixture
def results():
    return [Result(rank=1, title="Leadership", url="https://example.org/leadership",
                   snippet="Ada is the CEO.", engine=config.FIRST_ENGINE)]


def hit():
    return checker.CheckedVerdict(verdict="HIT", confidence=.9, why="The source names Ada.",
                                  evidence_rank=1, best_url="https://example.org/leadership",
                                  model="test-checker")


@pytest.mark.parametrize("key", [None, ""])
def test_default_cascade_starts_without_openai_key(key, tmp_path):
    env = {**os.environ, "SO_ENV_FILE": str(tmp_path / "absent.env"),
           "SO_OUT_DIR": str(tmp_path), "SERVER_LIVE_RACES": "false"}
    env.pop("CHECKER_BACKEND", None)
    env.pop("CHECKER_CASCADE_THRESHOLD", None)
    env.pop("OPENAI_API_KEY", None)
    if key is not None:
        env["OPENAI_API_KEY"] = key
    code = """
from fastapi.testclient import TestClient
from server.app import app
from so import config
assert config.CHECKER_BACKEND == 'cascade'
assert config.CHECKER_CASCADE_THRESHOLD == .7
with TestClient(app) as client:
    assert client.get('/').status_code == 200
"""
    subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True)


@pytest.mark.parametrize("backend", ["llm", "decision", "cascade"])
def test_explicit_environment_backend_overrides_product_default(backend, tmp_path):
    env = {**os.environ, "SO_ENV_FILE": str(tmp_path / "absent.env"), "CHECKER_BACKEND": backend}
    completed = subprocess.run([sys.executable, "-c", "from so import config; print(config.CHECKER_BACKEND)"],
                               env=env, capture_output=True, text=True, check=True)
    assert completed.stdout.strip() == backend


@pytest.mark.parametrize("key", [None, ""])
async def test_missing_key_falls_back_each_time_but_warns_once(key, results, monkeypatch, caplog):
    monkeypatch.setattr(config, "CHECKER_BACKEND", "cascade")
    if key is None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    else:
        monkeypatch.setenv("OPENAI_API_KEY", key)
    decisions = AsyncMock(side_effect=AssertionError("Missing credentials must bypass Decisions"))
    fallback = AsyncMock(return_value=hit())
    monkeypatch.setattr(dc, "check", decisions)
    monkeypatch.setattr(checker, "check", fallback)
    with caplog.at_level(logging.WARNING, logger=dc.__name__):
        for _ in range(2):
            verdict = await dc.configured_check("CEO", "Find current CEO", results)
            assert verdict.verdict == "HIT" and verdict.fallback
            assert verdict.decision_failure == "missing_api_key" and verdict.decision_calls == 0
            assert verdict.evidence_rank is None and verdict.best_url is None
    assert fallback.await_count == 2
    decisions.assert_not_called()
    warnings = [record for record in caplog.records if record.name == dc.__name__]
    assert len(warnings) == 1 and "missing_api_key" in warnings[0].message


@pytest.mark.parametrize("kind", ["timeout", "transport", "malformed"])
async def test_decision_failures_keep_falling_back_with_one_warning(kind, results, monkeypatch, caplog):
    monkeypatch.setenv("OPENAI_API_KEY", "test-placeholder")
    failure = llm.DecisionCallError("provider failed", kind=kind, usage=LLMResponse(role="checker"))
    decide = AsyncMock(side_effect=failure)
    fallback = AsyncMock(return_value=hit())
    monkeypatch.setattr(llm, "decide", decide)
    monkeypatch.setattr(checker, "check", fallback)
    with caplog.at_level(logging.WARNING, logger=dc.__name__):
        for _ in range(2):
            verdict = await dc.cascade_check("CEO", "Find current CEO", results)
            assert verdict.verdict == "HIT" and verdict.fallback
            assert verdict.decision_failure == kind
    assert decide.await_count == fallback.await_count == 2
    assert len([record for record in caplog.records if record.name == dc.__name__]) == 1


async def test_checker_eval_uses_llm_explicitly_under_cascade_default(results, monkeypatch):
    monkeypatch.setattr(config, "CHECKER_BACKEND", "cascade")
    check = AsyncMock(return_value=hit())
    dispatch = AsyncMock(side_effect=AssertionError("Calibration must not inherit the product backend"))
    monkeypatch.setattr(checker, "check", check)
    monkeypatch.setattr(dc, "configured_check", dispatch)
    question = Question(id="unit", question="Who is the CEO?", gold=["hidden"],
                        category="fact", source="unit", split="learn")
    case = checker_eval.Case(question, config.FIRST_ENGINE, results, True)
    scored = await checker_eval.score([case], "2026-11-14")
    assert scored[0].verdict.verdict == "HIT"
    check.assert_awaited_once_with(question.question, question.question, results, "2026-11-14")
    dispatch.assert_not_called()


@pytest.mark.parametrize("backend", ["llm", "decision", "cascade"])
async def test_eval_agent_search_uses_explicit_backend(backend, results, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CHECKER_BACKEND", backend)
    monkeypatch.setattr(config, "SO_ENABLE_SECOND_OPINION", True)
    monkeypatch.setattr(run_eval, "AGENT", agent)
    question = Question(id="eval-unit", question="Who is the CEO?", gold=["hidden"],
                        category="fact", source="unit", split="test")
    responses = [
        LLMResponse(tool_calls=[ToolCall(id="search-unit", name="search",
            arguments={"query": question.question, "goal": "Find current CEO"})]),
        LLMResponse(text=json.dumps({"final": "Ada", "status": "ANSWERED", "citations": [results[0].url]})),
    ]
    monkeypatch.setattr(llm, "complete", AsyncMock(side_effect=responses))
    engine = config.FIRST_ENGINE
    monkeypatch.setattr(telem_client, "search_engines", AsyncMock(return_value={
        engine: EngineRun(engine=engine, status="ok", results=results)}))
    calls = {name: AsyncMock(return_value=hit()) for name in ("llm", "decision", "cascade")}
    monkeypatch.setattr(checker, "check", calls["llm"])
    monkeypatch.setattr(dc, "check", calls["decision"])
    monkeypatch.setattr(dc, "cascade_check", calls["cascade"])
    state = await run_eval.run([question], [], ["second_opinion"], tmp_path / "logs", concurrency=1)
    assert state["done"] == 1 and not state["failures"]
    calls[backend].assert_awaited_once()
    for name, call in calls.items():
        if name != backend:
            call.assert_not_called()
