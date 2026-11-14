"""The agent replays the model's own assistant blocks, thinking included, on the next turn."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from so import agent, config, llm
from so.contracts import EngineRun, LLMResponse, Question, Result, ToolCall

URL = "https://example.com/founder"
THINKING = {"type": "thinking", "thinking": "", "signature": "sig-1"}
TOOL_USE = {"type": "tool_use", "id": "toolu_1", "name": "search",
            "input": {"query": "CEO", "goal": "Find the CEO"}}
FINAL = json.dumps({"final": "Ada is the CEO.", "status": "ANSWERED", "citations": [URL]})


@pytest.fixture(autouse=True)
def settings(monkeypatch, tmp_path):
    for name, value in {"AGENT_MAX_SEARCHES": 6, "AGENT_MAX_PAGE_READS": 12, "AGENT_MAX_TURNS": 24,
                        "SO_LOG_DIR": tmp_path,
                        "AGENT_PROMPT_PATH": Path(__file__).parents[2] / "so/prompts/agent.md"}.items():
        monkeypatch.setattr(config, name, value)


@pytest.fixture
def question() -> Question:
    return Question(id="q-1", question="Who is the CEO?", gold=["Ada"], category="fact",
                    source="test", split="learn")


@pytest.fixture
def complete(monkeypatch) -> AsyncMock:
    run = EngineRun(engine="tavily", status="ok", cost_usd=0.005, latency_ms=50, results=[
        Result(rank=1, title="Leadership", url=URL, snippet="Ada is the CEO.", engine="tavily")])
    monkeypatch.setattr(agent.telem_client, "search_engines", AsyncMock(return_value={"tavily": run}))
    fake = AsyncMock()
    monkeypatch.setattr(agent.llm, "complete", fake)
    return fake


def tool_turn(blocks: list[dict] | None) -> LLMResponse:
    call = ToolCall(id="toolu_1", name="search", arguments=TOOL_USE["input"])
    return LLMResponse(tool_calls=[call], model="agent-model", content=blocks or [])


def test_anthropic_normalization_keeps_raw_blocks():
    raw = {"content": [THINKING, TOOL_USE], "stop_reason": "tool_use",
           "usage": {"input_tokens": 10, "output_tokens": 5}}
    response = llm._normalize(raw, "anthropic", "agent", config.AGENT_MODEL, 100)
    assert response.content == [THINKING, TOOL_USE]
    assert response.tool_calls[0].id == "toolu_1"


def test_block_lists_pass_through_the_anthropic_request_unchanged():
    messages = [{"role": "user", "content": "Who?"}, {"role": "assistant", "content": [THINKING, TOOL_USE]},
                {"role": "tool", "tool_call_id": "toolu_1", "content": "{}"}]
    converted = llm._anthropic_messages(messages)
    assert converted[1] == {"role": "assistant", "content": [THINKING, TOOL_USE]}
    assert converted[2]["content"][0]["tool_use_id"] == "toolu_1"


async def test_thinking_blocks_are_replayed_on_the_next_turn(question, complete, tmp_path):
    complete.side_effect = [tool_turn([THINKING, TOOL_USE]), LLMResponse(text=FINAL, model="agent-model")]
    answer = await agent.run_question(question, "single", log_dir=tmp_path)
    assert answer.status == "ANSWERED"
    history = complete.await_args_list[1].args[2]
    assert history[1] == {"role": "assistant", "content": [THINKING, TOOL_USE]}
    assert history[2]["role"] == "tool" and history[2]["tool_call_id"] == "toolu_1"


async def test_responses_without_native_blocks_use_tool_call_messages(question, complete, tmp_path):
    complete.side_effect = [tool_turn(None), LLMResponse(text=FINAL, model="agent-model")]
    await agent.run_question(question, "single", log_dir=tmp_path)
    turn = complete.await_args_list[1].args[2][1]
    assert turn["tool_calls"][0]["id"] == "toolu_1" and "content" in turn


async def test_repair_turn_replays_blocks_and_never_sends_an_empty_reply(question, complete, tmp_path):
    complete.side_effect = [tool_turn([THINKING, TOOL_USE]), LLMResponse(text="", model="agent-model"),
                            LLMResponse(text=FINAL, model="agent-model")]
    await agent.run_question(question, "single", log_dir=tmp_path)
    history = complete.await_args_list[2].args[2]
    assert all(m.get("content") != "" for m in history)
    assert history[-1]["role"] == "user" and "answer format" in history[-1]["content"]

    complete.reset_mock()
    prose = [THINKING, {"type": "text", "text": "Ada runs it."}]
    complete.side_effect = [tool_turn([THINKING, TOOL_USE]),
                            LLMResponse(text="Ada runs it.", model="agent-model", content=prose),
                            LLMResponse(text=FINAL, model="agent-model")]
    await agent.run_question(question, "single", log_dir=tmp_path)
    assert complete.await_args_list[2].args[2][-2] == {"role": "assistant", "content": prose}
