"""Citation filtering and the bounded, costed repair of unsupported citations."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from so import agent, checker
from so.contracts import LLMResponse, ToolCall
from tests.o3.test_agent import URL, events, response, result, run, search_call
from tests.o3.test_agent import agent_settings, question, wrappers  # noqa: F401 (fixtures)

UNRETRIEVED_URL = "https://example.com/unretrieved"
UNUSED_URL = "https://example.com/another-source"


def cited_reply(citations: list[str], *, cost: float = .01) -> LLMResponse:
    return response(text=json.dumps({
        "final": "Ada is the CEO.", "status": "ANSWERED", "citations": citations,
    }), cost=cost)


def answer_event(path):
    return next(event["data"] for event in events(path) if event["type"] == "answer")


@pytest.mark.parametrize("setup", ["single", "telem_default", "second_opinion"])
async def test_sqa_1447_shape_keeps_valid_citation_and_drops_unretrieved_url(
        question, wrappers, tmp_path, monkeypatch, setup):
    monkeypatch.setattr(checker, "check", AsyncMock(return_value=checker.CheckedVerdict(
        verdict="HIT", confidence=1, why="The requested fact is present.", evidence_rank=1,
        best_url=URL,
    )))
    wrappers.complete.side_effect = [response(search_call()), cited_reply([URL, UNRETRIEVED_URL])]

    answer = await agent.run_question(question, setup, log_dir=tmp_path)

    assert answer.status == "ANSWERED" and answer.final == "Ada is the CEO."
    assert answer.citations == [URL] and answer.searches == 1
    assert wrappers.complete.await_count == 2
    assert answer_event(tmp_path)["citations_dropped"] == 1
    assert "error" not in events(tmp_path)[-1]["data"]


async def test_all_unretrieved_citations_get_one_successful_costed_repair(question, wrappers, tmp_path):
    wrappers.pinned.return_value = {
        "tavily": run().model_copy(update={"results": [result(), result(url=UNUSED_URL)]}),
    }
    wrappers.complete.side_effect = [
        response(search_call(), cost=.01), cited_reply([UNRETRIEVED_URL], cost=.02),
        cited_reply([URL], cost=.03),
    ]

    answer = await agent.run_question(question, "single", log_dir=tmp_path)

    assert answer.status == "ANSWERED" and answer.citations == [URL]
    assert UNUSED_URL not in answer.citations
    assert wrappers.complete.await_count == 3
    repair_call = wrappers.complete.await_args_list[-1]
    assert repair_call.kwargs["tools"] is None
    prompt = repair_call.args[2][-1]["content"]
    assert "no citations from retrieved evidence" in prompt
    assert json.dumps(sorted([URL, UNUSED_URL])) in prompt
    assert UNRETRIEVED_URL not in prompt
    log = events(tmp_path)
    llm_events = [event["data"] for event in log if event["type"] == "llm"]
    assert [event["cost_usd"] for event in llm_events] == [.01, .02, .03]
    assert llm_events[-1]["repair"] == "citations"
    assert log[-1]["data"]["cost_usd_llm"] == pytest.approx(.06)
    assert log[-1]["data"]["cost_usd_total"] == pytest.approx(.065)
    assert answer_event(tmp_path)["citations_dropped"] == 1


@pytest.mark.parametrize("repair_citations", [[UNRETRIEVED_URL], []])
async def test_citation_repair_without_valid_citations_returns_not_found(
        question, wrappers, tmp_path, repair_citations):
    wrappers.complete.side_effect = [response(search_call()), cited_reply([UNRETRIEVED_URL]),
                                     cited_reply(repair_citations, cost=.04)]

    answer = await agent.run_question(question, "single", log_dir=tmp_path)

    assert answer.status == "NOT_FOUND" and answer.final == "NOT FOUND"
    assert answer.citations == [] and answer.searches == 1
    assert wrappers.complete.await_count == 3 and wrappers.pinned.await_count == 1
    assert answer_event(tmp_path)["citations_dropped"] == 1 + len(repair_citations)
    totals = events(tmp_path)[-1]["data"]
    assert "error" not in totals
    assert totals["cost_usd_llm"] == pytest.approx(.06)
    assert totals["cost_usd_total"] == pytest.approx(.065)


async def test_no_retrieved_sources_are_not_replaced_with_model_citations(question, wrappers, tmp_path):
    wrappers.complete.side_effect = [cited_reply([UNRETRIEVED_URL]), cited_reply([UNRETRIEVED_URL])]

    answer = await agent.run_question(question, "single", log_dir=tmp_path)

    assert answer.status == "NOT_FOUND" and answer.citations == [] and answer.searches == 0
    assert wrappers.complete.await_count == 2
    assert "Valid retrieved URLs: []" in wrappers.complete.await_args.args[2][-1]["content"]
    wrappers.pinned.assert_not_awaited()


async def test_successful_page_reads_are_valid_citation_sources(question, wrappers, tmp_path):
    wrappers.complete.side_effect = [response(ToolCall(id="read1", name="read_page", arguments={"url": URL})),
                                     cited_reply([UNRETRIEVED_URL, URL])]

    answer = await agent.run_question(question, "single", log_dir=tmp_path)

    assert answer.status == "ANSWERED" and answer.citations == [URL] and answer.searches == 0
    assert answer_event(tmp_path)["citations_dropped"] == 1
    assert wrappers.fetch.await_count == 1 and wrappers.complete.await_count == 2
    wrappers.pinned.assert_not_awaited()


def test_inline_citations_are_filtered_and_counted_once_per_url():
    text = json.dumps({"final": f"Ada is the CEO. {URL} {UNRETRIEVED_URL}", "status": "ANSWERED",
                       "citations": [URL, UNRETRIEVED_URL, UNRETRIEVED_URL]})
    dropped: list[str] = []

    answer = agent._answer(text, {URL, UNUSED_URL}, 1, dropped_citations=dropped)

    assert answer.citations == [URL]
    assert dropped == [UNRETRIEVED_URL]


@pytest.mark.parametrize("text", [
    "Invalid JSON",
    json.dumps({"final": "Ada", "status": "ANSWERED", "citations": [42]}),
    json.dumps({"final": None, "status": "ANSWERED", "citations": [URL]}),
    json.dumps({"final": "NOT FOUND", "status": "ANSWERED", "citations": [URL]}),
    json.dumps({"final": "NOT FOUND", "status": "NOT_FOUND", "citations": [URL]}),
])
async def test_citation_repair_preserves_malformed_output_errors(question, wrappers, tmp_path, text):
    wrappers.complete.side_effect = [response(search_call()), cited_reply([UNRETRIEVED_URL]),
                                     response(text=text)]

    with pytest.raises(ValueError, match="Invalid agent answer"):
        await agent.run_question(question, "single", log_dir=tmp_path)

    assert wrappers.complete.await_count == 3
    assert events(tmp_path)[-1]["data"]["error"] == "ValueError"
    assert not any(event["type"] == "answer" for event in events(tmp_path))


async def test_format_and_citation_errors_share_one_repair_turn(question, wrappers, tmp_path):
    wrappers.complete.side_effect = [response(search_call()), response(text="Ada."),
                                     cited_reply([UNRETRIEVED_URL])]

    answer = await agent.run_question(question, "single", log_dir=tmp_path)

    assert answer.status == "NOT_FOUND" and answer.final == "NOT FOUND" and answer.citations == []
    assert wrappers.complete.await_count == 3
    assert URL in wrappers.complete.await_args.args[2][-1]["content"]
    assert answer_event(tmp_path)["citations_dropped"] == 1
