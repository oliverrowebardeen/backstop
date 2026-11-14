from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from so import agent, config, setups
from so.contracts import EngineRun, LLMResponse, PageText, Question, Result, RunContext, SearchOutcome, ToolCall
from so.eventlog import read_log, summarize
from so.ledger import ledger

URL = "https://example.com/founder"


@pytest.fixture(autouse=True)
def agent_settings(monkeypatch, tmp_path):
    settings = {
        "AGENT_MAX_SEARCHES": 6, "AGENT_MAX_PAGE_READS": 12, "AGENT_MAX_TURNS": 24,
        "AGENT_PROMPT_PATH": Path(__file__).parents[2] / "so/prompts/agent.md",
        "SO_LOG_DIR": tmp_path, "SO_ENABLE_SECOND_OPINION": True,
    }
    for name, value in settings.items():
        monkeypatch.setattr(config, name, value)


def result(engine="tavily", url=URL):
    return Result(rank=1, title="Company leadership", url=url,
                  snippet="Ada is the CEO.", engine=engine)


def run(engine="tavily", *, cost=0.005, cached=False):
    return EngineRun(engine=engine, status="ok", results=[result(engine)],
                     cost_usd=cost, latency_ms=500, cached=cached, session_id="session-one")


def response(*calls, text="", cost=0.01):
    return LLMResponse(text=text, tool_calls=list(calls), cost_usd=cost, latency_ms=100,
                       model="same-agent", tokens_in=20, tokens_out=10)


def search_call(index=1, *, query=None):
    return ToolCall(id=f"s{index}", name="search",
                    arguments={"query": f"CEO {index}" if query is None else query, "goal": "Find current CEO"})


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
    label = ledger("initial", "UNCHECKED", config.FIRST_ENGINE if setup == "single" else "auto")
    assert wrapper.await_args.kwargs == {"goal": "Find current CEO", "session": None, **label}
    log = events(tmp_path)
    assert log[0]["type"] == "run_start"
    assert log[0]["data"]["effective_setup"] == setup
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
        assert call.args[0] == "agent"
        assert call.args[1] == Path(config.AGENT_PROMPT_PATH).read_text().replace("{{max_searches}}", "6")
        serialized = json.dumps(call.args[2])
        assert "SECRET GOLD" not in serialized and "SECRET TRAP NOTE" not in serialized


async def test_read_page_carries_same_session_and_counts_cost(question, wrappers, tmp_path):
    wrappers.complete.side_effect = [response(search_call()), response(ToolCall(
        id="read1", name="read_page", arguments={"url": URL})), final_response()]
    await agent.run_question(question, "single", log_dir=tmp_path)
    wrappers.fetch.assert_awaited_once_with(
        URL, session="session-one",
        metadata=ledger("read_page", None, config.TELEM_FETCH_PROVIDER)["metadata"],
    )
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
    wrappers.fetch.assert_awaited_once_with(
        URL, session=None,
        metadata=ledger("read_page", None, config.TELEM_FETCH_PROVIDER)["metadata"],
    )
    assert wrappers.pinned.await_args.kwargs["session"] == "session-one"


@pytest.mark.parametrize("setup", ["single", "telem_default", "second_opinion"])
@pytest.mark.parametrize("read_first", [False, True])
async def test_ledger_labels_follow_one_session_per_question(
        question, wrappers, tmp_path, monkeypatch, setup, read_first):
    from so import checker

    monkeypatch.setattr(config, "TELEM_FETCH_PROVIDER", "configured-fetch-provider")
    monkeypatch.setattr(checker, "check", AsyncMock(return_value=checker.CheckedVerdict(
        verdict="HIT", confidence=1, why="The requested fact is present.", evidence_rank=1,
        best_url=URL,
    )))
    wrapper = wrappers.default if setup == "telem_default" else wrappers.pinned
    for index, session in enumerate(("session-first", "session-next")):
        wrapper.reset_mock()
        wrappers.fetch.reset_mock()
        wrapper.return_value = {
            "tavily": run().model_copy(update={"session_id": session}),
        }
        wrappers.fetch.return_value = wrappers.fetch.return_value.model_copy(update={"session_id": session})
        read = ToolCall(id="read1", name="read_page", arguments={"url": URL})
        calls = [read, search_call()] if read_first else [search_call(), read]
        calls += [search_call(2), read.model_copy(update={"id": "read2"})]
        wrappers.complete.side_effect = [response(call) for call in calls] + [final_response()]

        answer = await agent.run_question(question.model_copy(update={"id": f"q-{index}"}), setup,
                                          log_dir=tmp_path)

        assert answer.searches == wrapper.await_count == 2
        assert [call.kwargs["session"] for call in wrapper.await_args_list] == [
            session if read_first else None, session,
        ]
        assert [call.kwargs["session"] for call in wrappers.fetch.await_args_list] == [
            None if read_first else session, session,
        ]
        for call in wrappers.fetch.await_args_list:
            assert call.kwargs["metadata"] == ledger("read_page", None, "configured-fetch-provider")["metadata"]
            assert "context" not in call.kwargs
        if setup != "second_opinion":
            label = ledger("initial", "UNCHECKED", config.FIRST_ENGINE if setup == "single" else "auto")
            for call in wrapper.await_args_list:
                assert {key: call.kwargs[key] for key in label} == label
    assert len(list(tmp_path.glob("*.jsonl"))) == 2


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
    monkeypatch.setattr(config, "AGENT_MAX_TURNS", 3)
    monkeypatch.setattr(config, "AGENT_MAX_PAGE_READS", 2)
    wrappers.complete.side_effect = None
    wrappers.complete.return_value = response(ToolCall(id="read", name="read_page", arguments={"url": URL}))
    with pytest.raises(RuntimeError, match="final turn limit"):
        await agent.run_question(question, "single", log_dir=tmp_path)
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
    from so import second_opinion

    async def checked(query, goal, ctx):
        assert ctx.session is None
        ctx.session = "session-one"
        ctx.log.emit("search", engines=["tavily"], cost_usd=.005, latency_ms=500)
        ctx.log.emit("llm", role="checker", model="checker", tokens_in=4, tokens_out=5,
                     cost_usd=.003, latency_ms=100)
        ctx.log.emit("search_done", cost_usd=.008, calls=1, fetches=0, latency_ms=600)
        return SearchOutcome(status="HIT", results=[result()], calls=1, cost_usd=.008)
    monkeypatch.setattr(second_opinion, "search", checked)
    await agent.run_question(question, "second_opinion", log_dir=tmp_path)
    totals = events(tmp_path)[-1]["data"]
    assert totals["engine_calls"] == 1
    assert totals["cost_usd_search"] == pytest.approx(.005)
    assert totals["cost_usd_llm"] == pytest.approx(.023)
    assert totals["cost_usd_total"] == pytest.approx(.028)


@pytest.mark.parametrize("text,sources", [
    ("Ada is CEO.", {URL}),
    (json.dumps({"final": "Ada", "citations": [URL], "status": "ANSWERED"}), set()),
    (json.dumps({"final": "Ada", "citations": [URL], "status": "NOT_FOUND"}), {URL}),
    (json.dumps({"final": None, "citations": [URL]}), {URL}),
    (json.dumps({"final": "Ada", "citations": [], "status": "ANSWERED"}), {URL}),
])
def test_malformed_or_uncited_answer_raises_clear_error(text, sources):
    with pytest.raises(ValueError, match="Invalid agent answer"):
        agent._answer(text, sources, 1)


def test_explicit_decline_and_parenthesized_citations():
    assert agent._answer("NOT FOUND", set(), 0).status == "NOT_FOUND"
    url = "https://example.com/Ada_(person)"
    text = json.dumps({"final": f"Ada. [Source]({url})", "citations": [url], "status": "ANSWERED"})
    assert agent._answer(text, {url}, 1).citations == [url]


def test_fenced_answer_after_explanation_is_validated():
    body = json.dumps({"final": "Ada is the CEO.", "status": "ANSWERED", "citations": [URL]})
    answer = agent._answer(f"The sources identify the CEO.\n\n```json\n{body}\n```", {URL}, 2)
    assert answer.final == "Ada is the CEO." and answer.searches == 2
    with pytest.raises(ValueError, match="retrieved evidence"):
        agent._answer(f"Explanation.\n```json\n{body}\n```", set(), 2)
    with pytest.raises(ValueError, match="one JSON answer block"):
        agent._answer(f"```json\n{body}\n```\n```json\n{body}\n```", {URL}, 2)


async def test_baseline_deduplicates_urls_and_keeps_uncertainty(wrappers):
    ctx = RunContext(run_id="r", question_id="q", setup="telem_default", session="session-one")
    outcome = await setups.telem_default("query", "goal", ctx)
    assert outcome.status == "UNCHECKED" and outcome.calls == 3 and len(outcome.results) == 1
    assert outcome.cost_usd == pytest.approx(.015)
    assert outcome.engines_called == ["tavily", "exa", "brave"]


def test_second_opinion_flag_and_loading(monkeypatch):
    monkeypatch.setattr(config, "SO_ENABLE_SECOND_OPINION", False)
    with pytest.raises(RuntimeError, match="second_opinion setup is disabled"):
        setups.get_search_tool("second_opinion")
    monkeypatch.setattr(config, "SO_ENABLE_SECOND_OPINION", True)
    missing = ModuleNotFoundError("missing", name="so.second_opinion")
    monkeypatch.setattr(setups.importlib, "import_module", lambda name: (_ for _ in ()).throw(missing))
    with pytest.raises(RuntimeError, match="not installed"):
        setups.get_search_tool("second_opinion")
    checked = AsyncMock()
    monkeypatch.setattr(setups.importlib, "import_module", lambda name: SimpleNamespace(search=checked))
    assert setups.get_search_tool("second_opinion") is checked


def test_missing_transitive_dependency_is_not_a_silent_fallback(monkeypatch):
    monkeypatch.setattr(config, "SO_ENABLE_SECOND_OPINION", True)
    def broken(name):
        raise ModuleNotFoundError("missing", name="so.checker")
    monkeypatch.setattr(setups.importlib, "import_module", broken)
    with pytest.raises(ModuleNotFoundError, match="missing"):
        setups.get_search_tool("second_opinion")


def test_invalid_setup():
    with pytest.raises(ValueError, match="Unknown setup"):
        setups.get_search_tool("typo")


async def test_partial_engine_failure_is_visible(wrappers):
    wrappers.default.return_value = {"tavily": run(), "exa": EngineRun(engine="exa", status="error")}
    ctx = RunContext(run_id="r", question_id="q", setup="telem_default")
    outcome = await setups.telem_default("query", "goal", ctx)
    assert outcome.status == "UNCHECKED" and len(outcome.results) == 1
    assert "These engines failed: exa" in outcome.note


@pytest.mark.parametrize("runs", [{}, {"tavily": EngineRun(engine="tavily", status="error")}])
async def test_total_engine_failure_is_not_an_empty_answer(question, wrappers, tmp_path, runs):
    wrappers.pinned.return_value = runs
    with pytest.raises(RuntimeError, match="No search engine succeeded"):
        await agent.run_question(question, "single", log_dir=tmp_path)
    assert events(tmp_path)[-1]["data"]["error"] == "RuntimeError"


async def test_configured_search_limit_and_log_directory(question, wrappers, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AGENT_MAX_SEARCHES", 2)
    wrappers.complete.side_effect = [response(*(search_call(i) for i in range(3))), final_response()]
    answer = await agent.run_question(question, "single")
    assert answer.searches == wrappers.pinned.await_count == 2
    assert "at most 2 times" in wrappers.complete.await_args.args[1]
    assert len(list(tmp_path.glob("*.jsonl"))) == 1


@pytest.mark.parametrize("setup,engine_calls,cost", [
    ("single", 1, .005), ("telem_default", 3, .015), ("second_opinion", 1, .005),
])
async def test_exact_query_reuse_preserves_outcome_without_new_cost(
        question, wrappers, tmp_path, monkeypatch, setup, engine_calls, cost):
    from so import checker

    check = AsyncMock(return_value=checker.CheckedVerdict(
        verdict="HIT", confidence=1, why="The answer is present.", evidence_rank=1,
        best_url=URL, model="test-checker", cost_usd=.003, latency_ms=100,
    ))
    monkeypatch.setattr(checker, "check", check)
    monkeypatch.setattr(config, "AGENT_MAX_SEARCHES", 1)
    repeat = search_call(2, query="CEO 1")
    wrappers.complete.side_effect = [response(search_call()), response(repeat), final_response()]

    answer = await agent.run_question(question, setup, log_dir=tmp_path)

    assert answer.searches == 1
    wrapper = wrappers.default if setup == "telem_default" else wrappers.pinned
    assert wrapper.await_count == 1
    assert check.await_count == (1 if setup == "second_opinion" else 0)
    replies = [json.loads(message["content"]) for message in wrappers.complete.await_args.args[2]
               if message["role"] == "tool"]
    original, reused = replies
    assert "already ran this exact query" in reused["note"]
    assert "Find current CEO" in reused["note"]
    assert reused["note"].endswith(original["note"])
    assert "already ran" not in original["note"]
    assert {key: value for key, value in reused.items() if key != "note"} == {
        key: value for key, value in original.items() if key != "note"}
    log = events(tmp_path)
    assert sum(event["type"] == "search_done" for event in log) == 1
    totals = log[-1]["data"]
    assert totals["engine_calls"] == engine_calls and totals["searches"] == 1
    assert totals["cost_usd_search"] == pytest.approx(cost)
    assert totals["cost_usd_llm"] == pytest.approx(.033 if setup == "second_opinion" else .03)
    assert all(call.args[1] == Path(config.AGENT_PROMPT_PATH).read_text().replace("{{max_searches}}", "1")
               for call in wrappers.complete.await_args_list)


async def test_query_reuse_is_exact_and_handles_same_turn_duplicates(question, wrappers, tmp_path):
    queries = ["CEO", "ceo", "CEO ", "CEO"]
    wrappers.complete.side_effect = [response(*(search_call(i, query=query) for i, query in enumerate(queries))),
                                       final_response()]
    answer = await agent.run_question(question, "single", log_dir=tmp_path)
    assert answer.searches == wrappers.pinned.await_count == 3
    assert [call.args[0] for call in wrappers.pinned.await_args_list] == queries[:3]


async def test_previous_searches_are_scoped_to_one_run(question, wrappers, tmp_path):
    wrappers.complete.side_effect = [response(search_call()), final_response()] * 2
    first = await agent.run_question(question, "single", log_dir=tmp_path)
    second = await agent.run_question(question, "single", log_dir=tmp_path)
    assert first.searches == second.searches == 1
    assert wrappers.pinned.await_count == 2
    assert len(list(tmp_path.glob("*.jsonl"))) == 2


async def test_not_found_outcome_is_also_reused(question, wrappers, tmp_path, monkeypatch):
    search = AsyncMock(return_value=SearchOutcome(status="NOT_FOUND", note="No evidence found.", calls=3))
    monkeypatch.setattr(setups, "single", search)
    wrappers.complete.side_effect = [response(search_call(), search_call(2, query="CEO 1")),
                                       response(text="NOT FOUND")]
    answer = await agent.run_question(question, "single", log_dir=tmp_path)
    assert answer.searches == search.await_count == 1
    reused = json.loads(wrappers.complete.await_args.args[2][-1]["content"])
    assert reused["status"] == "NOT_FOUND" and reused["calls"] == 3
    assert reused["note"].endswith("No evidence found.")


@pytest.mark.parametrize("setup", ["single", "telem_default", "second_opinion"])
async def test_changed_goal_gets_fresh_retrieval_and_goal_specific_check(
        question, wrappers, tmp_path, monkeypatch, setup):
    from so import checker

    check = AsyncMock(return_value=checker.CheckedVerdict(
        verdict="HIT", confidence=1, why="The requested fact is present.", evidence_rank=1,
        best_url=URL, model="test-checker", cost_usd=.003, latency_ms=100,
    ))
    monkeypatch.setattr(checker, "check", check)
    monkeypatch.setattr(config, "AGENT_MAX_SEARCHES", 2)
    goals = ["Find annual revenue", "Find employee count"]
    calls = [ToolCall(id=f"s{index}", name="search", arguments={
        "query": "Acme annual report", "goal": goal,
    }) for index, goal in enumerate(goals * 2)]
    wrappers.complete.side_effect = [response(call) for call in calls] + [final_response()]

    answer = await agent.run_question(question, setup, log_dir=tmp_path)

    wrapper = wrappers.default if setup == "telem_default" else wrappers.pinned
    assert answer.searches == wrapper.await_count == 2
    assert [call.kwargs["goal"] for call in wrapper.await_args_list] == goals
    assert check.await_count == (2 if setup == "second_opinion" else 0)
    if setup == "second_opinion":
        assert [call.args[1] for call in check.await_args_list] == goals
    replies = [json.loads(message["content"]) for message in wrappers.complete.await_args.args[2]
               if message["role"] == "tool"]
    assert all("already ran" not in reply["note"] for reply in replies[:2])
    assert all(goal in reply["note"] for goal, reply in zip(goals, replies[2:]))
    assert sum(event["type"] == "search_done" for event in events(tmp_path)) == 2


async def test_changed_goal_at_limit_cannot_reuse_another_goals_hit(question, wrappers, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AGENT_MAX_SEARCHES", 1)
    changed = search_call(2, query="CEO 1")
    changed.arguments["goal"] = "Find annual revenue"
    wrappers.complete.side_effect = [response(search_call()), response(changed), final_response()]
    await agent.run_question(question, "single", log_dir=tmp_path)
    reply = json.loads(wrappers.complete.await_args.args[2][-1]["content"])
    assert "Search limit reached" in reply["error"] and "status" not in reply
    assert wrappers.pinned.await_count == 1


async def test_disabled_second_opinion_refuses_run_before_paid_work(question, wrappers, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SO_ENABLE_SECOND_OPINION", False)
    with pytest.raises(RuntimeError, match="second_opinion setup is disabled"):
        await agent.run_question(question, "second_opinion", log_dir=tmp_path)
    wrappers.complete.assert_not_awaited()
    wrappers.pinned.assert_not_awaited()
    wrappers.default.assert_not_awaited()
    assert list(tmp_path.glob("*.jsonl")) == []


@pytest.mark.parametrize("requested,effective", [
    ("single", "telem_default"), ("single", "second_opinion"),
    ("telem_default", "single"), ("telem_default", "second_opinion"),
    ("second_opinion", "single"), ("second_opinion", "telem_default"),
])
async def test_effective_setup_mismatch_refuses_run(
        question, wrappers, tmp_path, monkeypatch, requested, effective):
    tool = setups.get_search_tool(effective)
    monkeypatch.setattr(agent, "get_search_tool", lambda setup: tool)
    with pytest.raises(RuntimeError, match=f"Requested setup '{requested}' resolved to '{effective}'"):
        await agent.run_question(question, requested, log_dir=tmp_path)
    wrappers.complete.assert_not_awaited()
    wrappers.pinned.assert_not_awaited()
    wrappers.default.assert_not_awaited()
    assert list(tmp_path.glob("*.jsonl")) == []


async def test_unregistered_search_implementation_is_not_mislabelled(question, wrappers, tmp_path, monkeypatch):
    search = AsyncMock()
    monkeypatch.setattr(agent, "get_search_tool", lambda setup: search)
    with pytest.raises(RuntimeError, match="Unrecognized search implementation"):
        await agent.run_question(question, "single", log_dir=tmp_path)
    search.assert_not_awaited()
    wrappers.complete.assert_not_awaited()
    assert list(tmp_path.glob("*.jsonl")) == []
