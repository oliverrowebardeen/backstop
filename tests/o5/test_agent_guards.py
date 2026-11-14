"""Goal validation and the idle-turn stop in the agent loop."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from so import agent, config
from so.contracts import EngineRun, LLMResponse, Question, Result, ToolCall

URL = "https://example.com/founder"
FINAL = LLMResponse(text=json.dumps({"final": "Ada is the CEO.", "status": "ANSWERED", "citations": [URL]}),
                    model="agent-model")


@pytest.fixture(autouse=True)
def settings(monkeypatch, tmp_path):
    for name, value in {"AGENT_MAX_SEARCHES": 6, "AGENT_MAX_PAGE_READS": 12, "AGENT_MAX_TURNS": 24,
                        "AGENT_MIN_GOAL_WORDS": 3, "AGENT_MAX_IDLE_TURNS": 2, "SO_LOG_DIR": tmp_path,
                        "AGENT_PROMPT_PATH": Path(__file__).parents[2] / "so/prompts/agent.md"}.items():
        monkeypatch.setattr(config, name, value)


@pytest.fixture
def question() -> Question:
    return Question(id="q-1", question="Who is the CEO?", gold=["Ada"], category="fact",
                    source="test", split="learn")


@pytest.fixture
def engine(monkeypatch) -> AsyncMock:
    run = EngineRun(engine="tavily", status="ok", cost_usd=0.005, latency_ms=50, results=[
        Result(rank=1, title="Leadership", url=URL, snippet="Ada is the CEO.", engine="tavily")])
    fake = AsyncMock(return_value={"tavily": run})
    monkeypatch.setattr(agent.telem_client, "search_engines", fake)
    return fake


@pytest.fixture
def complete(monkeypatch) -> AsyncMock:
    fake = AsyncMock()
    monkeypatch.setattr(agent.llm, "complete", fake)
    return fake


def search(query: str, goal: str, index: int = 1) -> LLMResponse:
    return LLMResponse(tool_calls=[ToolCall(id=f"s{index}", name="search",
                                            arguments={"query": query, "goal": goal})], model="agent-model")


def tool_results(history: list[dict]) -> list[dict]:
    return [json.loads(m["content"]) for m in history if m["role"] == "tool"]


async def test_placeholder_goal_is_rejected_without_a_search(question, engine, complete, tmp_path):
    complete.side_effect = [search("Ada CEO", "Find the CEO", 1), search("noop", "noop", 2), FINAL]
    answer = await agent.run_question(question, "single", log_dir=tmp_path)
    assert answer.searches == 1 and engine.await_count == 1
    rejected = tool_results(complete.await_args_list[2].args[2])[-1]
    assert "at least 3 words" in rejected["error"] and "final JSON answer" in rejected["error"]


async def test_idle_turns_withdraw_tools_and_force_an_answer(question, engine, complete, tmp_path):
    complete.side_effect = [search("Ada CEO", "Find the CEO", 1), search("x", "x", 2), search("x", "x", 3), FINAL]
    answer = await agent.run_question(question, "single", log_dir=tmp_path)
    assert answer.status == "ANSWERED" and complete.await_count == 4
    final_call = complete.await_args_list[3]
    assert final_call.kwargs["tools"] is None
    assert "did no new work" in final_call.args[2][-1]["content"]


async def test_search_limit_loop_ends_after_idle_turns(question, engine, complete, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AGENT_MAX_SEARCHES", 1)
    complete.side_effect = [search("Ada CEO", "Find the CEO", 1), search("Ada CEO now", "Find the current CEO", 2),
                            search("Ada CEO today", "Find the CEO today", 3), FINAL]
    await agent.run_question(question, "single", log_dir=tmp_path)
    assert engine.await_count == 1 and complete.await_count == 4
    assert complete.await_args_list[3].kwargs["tools"] is None


async def test_reused_queries_count_as_idle(question, engine, complete, tmp_path):
    repeat = search("Ada CEO", "Find the CEO", 1)
    complete.side_effect = [repeat, repeat, repeat, FINAL]
    await agent.run_question(question, "single", log_dir=tmp_path)
    assert engine.await_count == 1 and complete.await_args_list[3].kwargs["tools"] is None


async def test_new_work_resets_the_idle_count(question, engine, complete, tmp_path):
    complete.side_effect = [search("x", "x", 1), search("Ada CEO", "Find the CEO", 2), search("x", "x", 3), FINAL]
    await agent.run_question(question, "single", log_dir=tmp_path)
    assert [call.kwargs["tools"] is None for call in complete.await_args_list] == [False, False, False, False]


async def test_idle_stop_can_be_disabled(question, engine, complete, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AGENT_MAX_IDLE_TURNS", 0)
    monkeypatch.setattr(config, "AGENT_MAX_TURNS", 4)
    complete.side_effect = [search("x", "x", n) for n in range(4)] + [LLMResponse(text="NOT FOUND", model="m")]
    answer = await agent.run_question(question, "single", log_dir=tmp_path)
    assert answer.status == "NOT_FOUND" and complete.await_count == 5
    assert "Tool limit reached" in complete.await_args_list[4].args[2][-1]["content"]
