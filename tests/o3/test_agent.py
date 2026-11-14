from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from so import agent, setups
from so.contracts import EngineRun, LLMResponse, PageText, Question, Result, RunContext, SearchOutcome, ToolCall
from so.eventlog import read_log, summarize

URL = "https://example.com/founder"


def result(engine="tavily", url=URL):
    return Result(rank=1, title="Company leadership", url=url,
                  snippet="Ada is the CEO.", engine=engine)


def run(engine="tavily", *, cost=0.005, cached=False):
    return EngineRun(engine=engine, status="ok", results=[result(engine)],
                     cost_usd=cost, latency_ms=500, cached=cached, session_id="session-one")


def response(*calls, text="", cost=0.01):
    return LLMResponse(text=text, tool_calls=list(calls), cost_usd=cost, latency_ms=100,
                       model="same-agent", tokens_in=20, tokens_out=10)


def search_call(index=1):
    return ToolCall(id=f"s{index}", name="search", arguments={"query": "CEO", "goal": "Find current CEO"})


def final_response():
    return response(text=json.dumps({"final": "Ada is the CEO.", "status": "ANSWERED", "citations": [URL]}))


def events(path):
    return [event for file in path.glob("*.jsonl") for event in read_log(file)]


@pytest.fixture
def question():
    return Question(id="q-1", question="Who is the CEO?", gold=["SECRET GOLD"],
                    category="trap", source="test", split="test", is_trap=True,
                    trap_note="SECRET TRAP NOTE")


@pytest.fixture
def wrappers(monkeypatch):
    pinned = AsyncMock(return_value={"tavily": run()})
    default = AsyncMock(return_value={"tavily": run(), "exa": run("exa"), "brave": run("brave")})
    fetch = AsyncMock(return_value=PageText(url=URL, ok=True, text="Ada is CEO.",
                                          cost_usd=0.002, latency_ms=200, session_id="session-one"))
    complete = AsyncMock(side_effect=[response(search_call()), final_response()])
    for name, fake in (("search_engines", pinned),
                       ("search_default", default), ("fetch_page", fetch)):
        monkeypatch.setattr(agent.telem_client, name, fake)
    monkeypatch.setattr(agent.llm, "complete", complete)
    return SimpleNamespace(pinned=pinned, default=default, fetch=fetch, complete=complete)


@pytest.mark.parametrize("setup,engine_calls,cost", [("single", 1, .005), ("telem_default", 3, .015)])
async def test_run_session_evidence_and_totals(question, wrappers, tmp_path, setup, engine_calls, cost):
    answer = await agent.run_question(question, setup, log_dir=tmp_path)
    assert answer.status == "ANSWERED" and answer.citations == [URL] and answer.searches == 1
    wrapper = wrappers.pinned if setup == "single" else wrappers.default
    assert wrapper.await_args.kwargs == {"goal": "Find current CEO", "session": None}
    log = events(tmp_path)
    assert log[0]["type"] == "run_start"
    assert [entry["seq"] for entry in log] == list(range(1, len(log) + 1))
    totals = log[-1]["data"]
    assert summarize(next(tmp_path.glob("*.jsonl"))) == totals
    assert totals["session"] == "session-one"
    assert totals["engine_calls"] == engine_calls and totals["searches"] == 1
    assert totals["cost_usd_search"] == pytest.approx(cost)
    assert totals["cost_usd_llm"] == pytest.approx(.02)
    assert totals["cost_usd_total"] == pytest.approx(cost + .02)
    assert totals["latency_ms"] >= 700
    for call in wrappers.complete.await_args_list:
        assert call.args[0] == "agent" and call.args[1] == agent.SYSTEM_PROMPT
        serialized = json.dumps(call.args[2])
        assert "SECRET GOLD" not in serialized and "SECRET TRAP NOTE" not in serialized


async def test_read_page_carries_same_session_and_counts_cost(question, wrappers, tmp_path):
    wrappers.complete.side_effect = [response(search_call()), response(ToolCall(
        id="read1", name="read_page", arguments={"url": URL})), final_response()]
    await agent.run_question(question, "single", log_dir=tmp_path)
    wrappers.fetch.assert_awaited_once_with(URL, session="session-one")
    log = events(tmp_path)
    fetch_event = next(item for item in log if item["type"] == "fetch")
    assert fetch_event["data"]["via"] == "telem"
    totals = log[-1]["data"]
    assert totals["fetches"] == 1 and totals["cost_usd_search"] == pytest.approx(.007)
    assert totals["cost_usd_total"] == pytest.approx(.037)


async def test_first_page_read_opens_the_session_for_later_searches(question, wrappers, tmp_path):
    wrappers.complete.side_effect = [response(ToolCall(id="read1", name="read_page", arguments={"url": URL})),
                                       response(search_call()), final_response()]
    await agent.run_question(question, "single", log_dir=tmp_path)
    wrappers.fetch.assert_awaited_once_with(URL, session=None)
    assert wrappers.pinned.await_args.kwargs["session"] == "session-one"


async def test_cached_search_does_not_reuse_a_previous_runs_session(question, wrappers, tmp_path):
    cached = run(cached=True).model_copy(update={"session_id": None})
    wrappers.pinned.side_effect = [{"tavily": cached}, {"tavily": run()}, {"tavily": run()}]
    wrappers.complete.side_effect = [response(search_call(i)) for i in range(3)] + [final_response()]
    await agent.run_question(question, "single", log_dir=tmp_path)
    assert [call.kwargs["session"] for call in wrappers.pinned.await_args_list] == [None, None, "session-one"]
    assert events(tmp_path)[-1]["data"]["latency_ms"] >= 1900


async def test_parallel_tool_request_cannot_exceed_search_limit(question, wrappers, tmp_path):
    wrappers.complete.side_effect = [response(*(search_call(i) for i in range(8))), final_response()]
    answer = await agent.run_question(question, "single", log_dir=tmp_path)
    assert answer.searches == wrappers.pinned.await_count == 6
    assert wrappers.pinned.await_args_list[0].kwargs["session"] is None
    assert all(call.kwargs["session"] == "session-one" for call in wrappers.pinned.await_args_list[1:])
    tool_messages = [item for item in wrappers.complete.await_args.args[2] if item["role"] == "tool"]
    assert len(tool_messages) == 8
    assert all("Search limit reached" in item["content"] for item in tool_messages[-2:])


async def test_bad_tools_do_not_spend_search_budget(question, wrappers, tmp_path):
    wrappers.complete.side_effect = [response(
        ToolCall(id="bad1", name="search", arguments={"query": "CEO"}),
        ToolCall(id="bad2", name="read_page", arguments={"url": "file:///secret"}),
        ToolCall(id="bad3", name="unknown", arguments={})), response(text="NOT FOUND")]
    answer = await agent.run_question(question, "single", log_dir=tmp_path)
    assert answer.status == "NOT_FOUND" and answer.searches == 0
    wrappers.pinned.assert_not_awaited()
    wrappers.fetch.assert_not_awaited()


async def test_turn_and_read_limits(question, wrappers, tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "MAX_TURNS", 3)
    monkeypatch.setattr(agent, "MAX_PAGE_READS", 2)
    wrappers.complete.side_effect = None
    wrappers.complete.return_value = response(ToolCall(id="read", name="read_page", arguments={"url": URL}))
    answer = await agent.run_question(question, "single", log_dir=tmp_path)
    assert answer.status == "NOT_FOUND"
    assert wrappers.complete.await_count == 4 and wrappers.fetch.await_count == 2
    assert wrappers.complete.await_args.kwargs["tools"] is None


async def test_budget_error_is_propagated_with_terminal_totals(question, wrappers, tmp_path):
    wrappers.pinned.side_effect = RuntimeError("budget exhausted")
    with pytest.raises(RuntimeError, match="budget exhausted"):
        await agent.run_question(question, "single", log_dir=tmp_path)
    log = events(tmp_path)
    assert log[-1]["type"] == "run_end" and log[-1]["data"]["error"] == "RuntimeError"
    assert log[-1]["data"]["cost_usd_llm"] == pytest.approx(.01)
    assert not any(item["type"] == "answer" for item in log)


async def test_checker_events_are_included_once(question, wrappers, tmp_path, monkeypatch):
    async def checked(query, goal, ctx):
        assert ctx.session is None
        ctx.session = "session-one"
        ctx.log.emit("search", engines=["tavily"], cost_usd=.005, latency_ms=500)
        ctx.log.emit("llm", role="checker", model="checker", tokens_in=4, tokens_out=5,
                     cost_usd=.003, latency_ms=100)
        ctx.log.emit("search_done", cost_usd=.008, calls=1, fetches=0, latency_ms=600)
        return SearchOutcome(status="HIT", results=[result()], calls=1, cost_usd=.008)
    monkeypatch.setattr(agent, "get_search_tool", lambda setup: checked)
    await agent.run_question(question, "second_opinion", log_dir=tmp_path)
    totals = events(tmp_path)[-1]["data"]
    assert totals["engine_calls"] == 1
    assert totals["cost_usd_search"] == pytest.approx(.005)
    assert totals["cost_usd_llm"] == pytest.approx(.023)
    assert totals["cost_usd_total"] == pytest.approx(.028)


@pytest.mark.parametrize("text,sources,status", [
    ("NOT FOUND", {URL}, "NOT_FOUND"),
    ("Ada is CEO.", {URL}, "NOT_FOUND"),
    (f"Ada is CEO. [Source]({URL})", {URL}, "ANSWERED"),
    (f"Ada is CEO. [Source]({URL})", set(), "NOT_FOUND"),
    (f"Ada is CEO. {URL} https://invented.example", {URL}, "NOT_FOUND"),
    (json.dumps({"final": "Ada", "citations": [URL], "status": "NOT_FOUND"}), {URL}, "NOT_FOUND"),
    (json.dumps({"final": None, "citations": [URL]}), {URL}, "NOT_FOUND"),
    ("Ada. [Source](https://example.com/Ada_(person))", {"https://example.com/Ada_(person)"}, "ANSWERED"),
])
def test_final_answer_requires_retrieved_citation(text, sources, status):
    assert agent._answer(text, sources, 1).status == status


async def test_baseline_deduplicates_urls_and_keeps_uncertainty(wrappers):
    ctx = RunContext(run_id="r", question_id="q", setup="telem_default", session="session-one")
    outcome = await setups.telem_default("query", "goal", ctx)
    assert outcome.status == "UNCHECKED" and outcome.calls == 3 and len(outcome.results) == 1
    assert outcome.cost_usd == pytest.approx(.015)
    assert outcome.engines_called == ["tavily", "exa", "brave"]


def test_second_opinion_flag_and_auto_loading(monkeypatch):
    monkeypatch.setenv("SO_ENABLE_SECOND_OPINION", "0")
    assert setups.get_search_tool("second_opinion") is setups.single
    monkeypatch.setenv("SO_ENABLE_SECOND_OPINION", "auto")
    missing = ModuleNotFoundError("missing", name="so.second_opinion")
    monkeypatch.setattr(setups.importlib, "import_module", lambda name: (_ for _ in ()).throw(missing))
    assert setups.get_search_tool("second_opinion") is setups.single
    monkeypatch.setenv("SO_ENABLE_SECOND_OPINION", "1")
    with pytest.raises(ModuleNotFoundError):
        setups.get_search_tool("second_opinion")
    checked = AsyncMock()
    monkeypatch.setattr(setups.importlib, "import_module", lambda name: SimpleNamespace(search=checked))
    assert setups.get_search_tool("second_opinion") is checked


def test_missing_transitive_dependency_is_not_a_silent_fallback(monkeypatch):
    monkeypatch.setenv("SO_ENABLE_SECOND_OPINION", "auto")
    def broken(name):
        raise ModuleNotFoundError("missing", name="so.checker")
    monkeypatch.setattr(setups.importlib, "import_module", broken)
    with pytest.raises(ModuleNotFoundError, match="missing"):
        setups.get_search_tool("second_opinion")


def test_invalid_setup():
    with pytest.raises(ValueError, match="Unknown setup"):
        setups.get_search_tool("typo")
