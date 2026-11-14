"""Verify checker usage and page evidence with the real checker adapter."""

import json
from datetime import date
from inspect import signature

import pytest

from so import llm, tavily_client, telem_client
from so.contracts import EngineRun, LLMResponse, PageText, RescueTable, RunContext
from so.eventlog import EventLog, read_log, summarize
from so.second_opinion import search
from tests.o2.fakes import source


def model_reply(label: str, rank: int | None = None) -> str:
    """A checker model reply in the schema the model answers in (so.checker.VERDICT_SCHEMA)."""
    return json.dumps({"verdict": label, "confidence": 0.9, "why": "Evidence assessment", "rank": rank,
                       "rewrite": None})


async def test_real_checker_usage_is_emitted_once_and_page_url_is_retained(monkeypatch, tmp_path):
    url = source().url
    responses = iter([model_reply("SHALLOW", rank=1), model_reply("HIT")])

    async def complete(role, system, messages, **kwargs):
        if "PAGE TEXT:" in messages[0]["content"]:
            assert f"PAGE URL: {url}" in messages[0]["content"]
        return LLMResponse(
            text=next(responses), role="checker", model="fixture-model",
            cost_usd=0.001, latency_ms=25, tokens_in=100, tokens_out=30,
        )

    async def search_engines(query, engines, **kwargs):
        return {"tavily": EngineRun(
            engine="tavily", status="ok", results=[source()], cost_usd=0.005, latency_ms=100,
        )}

    async def extract(page_url, *, goal=None):
        assert page_url == url
        assert goal == "find the valuation figure"
        return PageText(url=url, ok=True, text="The valuation was $50 billion.", cost_usd=0.002, latency_ms=50)

    monkeypatch.setattr(llm, "complete", complete)
    monkeypatch.setattr(telem_client, "search_engines", search_engines)
    monkeypatch.setattr(tavily_client, "tavily_extract", extract)
    monkeypatch.setattr("so.second_opinion.load_rescue_table", lambda: RescueTable())
    log = EventLog("second_opinion-integration", "integration", "second_opinion", log_dir=tmp_path)
    ctx = RunContext(
        run_id="second_opinion-integration", question_id="integration", setup="second_opinion",
        log=log, session="integration-session", today=date(2026, 11, 14),
    )
    outcome = await search("Stripe 2023 valuation", "find the valuation figure", ctx)
    assert outcome.status == "HIT"
    assert outcome.cost_usd == pytest.approx(0.009)
    assert outcome.latency_ms >= 200
    assert outcome.note == "Answer is in result 1. You can stop searching for this."
    assert outcome.results[0].url == url
    events = read_log(log.path)
    checks = [event for event in events if event["type"] == "check"]
    assert checks[-1]["data"]["best_url"] == url
    usage = [event for event in events if event["type"] == "llm"]
    assert len(usage) == 2
    assert sum(event["data"]["cost_usd"] for event in usage) == pytest.approx(0.002)
    totals = summarize(log.path)
    assert totals["engine_calls"] == 1 and totals["fetches"] == 1
    assert totals["cost_usd_total"] == pytest.approx(outcome.cost_usd)


def test_page_reader_contracts_accept_goal_metadata():
    signature(tavily_client.tavily_extract).bind("https://example.com", goal="find evidence")
    signature(telem_client.fetch_page).bind("https://example.com", goal="find evidence", session="unit-session")


async def test_malformed_checker_retry_is_costed_without_semantic_rescue(monkeypatch, tmp_path, caplog):
    replies = iter(['{"verdict": "HIT"', '{"verdict": "HIT"'])

    async def complete(role, system, messages, **kwargs):
        return LLMResponse(
            text=next(replies), role="checker", model="fixture-model",
            cost_usd=0.001, latency_ms=25, tokens_in=100, tokens_out=30,
        )

    async def search_engines(query, engines, **kwargs):
        engine = engines[0]
        return {engine: EngineRun(
            engine=engine, status="ok", results=[source(f"https://example.com/{engine}", engine=engine)],
            cost_usd=0.005, latency_ms=100,
        )}

    monkeypatch.setattr(llm, "complete", complete)
    monkeypatch.setattr(telem_client, "search_engines", search_engines)
    monkeypatch.setattr("so.second_opinion.load_rescue_table", lambda: RescueTable())
    log = EventLog("second_opinion-malformed", "malformed", "second_opinion", log_dir=tmp_path)
    ctx = RunContext(
        run_id=log.run_id, question_id="malformed", setup="second_opinion", log=log,
        session="integration-session", today=date(2026, 11, 14),
    )

    outcome = await search("query", "goal", ctx)

    assert outcome.status == "UNCHECKED" and outcome.calls == 1 and outcome.fetches == 0
    assert outcome.engines_called == ["tavily"]
    assert outcome.cost_usd == pytest.approx(0.007)
    assert outcome.latency_ms >= 150
    checks = [event["data"] for event in outcome.trace if event["type"] == "check"]
    assert checks == []
    assert not any(event["type"] == "rescue" for event in outcome.trace)
    done = outcome.trace[-1]["data"]
    assert done["verification_failures"] == 1
    assert done["verification_failure_counts"] == {"malformed": 1}
    assert done["cost_usd_failed_checks"] == pytest.approx(0.002)
    assert done["rescues_used"] == 0
    usage = [event["data"] for event in outcome.trace if event["type"] == "llm"]
    assert len(usage) == 2
    assert sum(event["cost_usd"] for event in usage) == pytest.approx(0.002)
    assert sum(event["tokens_out"] for event in usage) == 60
    assert summarize(log.path)["cost_usd_total"] == pytest.approx(outcome.cost_usd)
    assert "malformed after a retry" in caplog.text
