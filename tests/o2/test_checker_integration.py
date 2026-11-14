"""Verify checker usage and page evidence with the real checker adapter."""

from datetime import date

import pytest

from so import llm, tavily_client, telem_client
from so.contracts import EngineRun, LLMResponse, PageText, RescueTable, RunContext
from so.eventlog import EventLog, read_log, summarize
from so.second_opinion import search
from tests.o2.fakes import source, verdict


async def test_real_checker_usage_is_emitted_once_and_page_url_is_retained(monkeypatch, tmp_path):
    url = source().url
    responses = iter([
        verdict("SHALLOW", best_url=url),
        verdict("HIT"),
    ])

    async def complete(role, system, messages, **kwargs):
        if "PAGE TEXT:" in messages[0]["content"]:
            assert f"PAGE URL: {url}" in messages[0]["content"]
        return LLMResponse(
            text=next(responses).model_dump_json(), role="checker", model="fixture-model",
            cost_usd=0.001, latency_ms=25, tokens_in=100, tokens_out=30,
        )

    async def search_engines(query, engines, **kwargs):
        return {"tavily": EngineRun(
            engine="tavily", status="ok", results=[source()], cost_usd=0.005, latency_ms=100,
        )}

    async def extract(page_url):
        assert page_url == url
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
