"""Optional race metadata survives event validation without changing decisions."""

from __future__ import annotations

import asyncio

import pytest

from so import config, router, second_opinion
from so.contracts import CheckerVerdict, EngineRun, PageText, Result, RunContext
from so.eventlog import EventLog, read_log
from tests.o8.test_integration import Retrieval


@pytest.fixture
def logged_context(learn_data, tmp_path):
    questions, rows = learn_data
    router.write_table(router.build(rows, questions), config.FIRST_ENGINE_TABLE_PATH)
    log = EventLog("routing-events", "event-unit", "second_opinion", log_dir=tmp_path / "logs")
    return RunContext(run_id=log.run_id, question_id=log.question_id, setup=log.setup,
                      session="event-session", log=log)


def event_data(ctx: RunContext, kind: str) -> list[dict]:
    return [event["data"] for event in read_log(ctx.log.path, allow_partial=False) if event["type"] == kind]


@pytest.mark.parametrize("query,category,engine", [
    ("Who built the observatory?", "fact", "perplexity"),
    ("What was the annual revenue?", "finance", "exa"),
])
async def test_initial_search_metadata_round_trips_through_eventlog(query, category, engine, logged_context, monkeypatch):
    fake = Retrieval(hit=engine)
    monkeypatch.setattr(second_opinion, "_check", fake.check)
    outcome = await second_opinion.search(query, "Find evidence", logged_context, engine_search=fake.search)

    search, = event_data(logged_context, "search")
    assert search["first_engine"] == engine == outcome.engines_called[0]
    assert search["question_type"] == category
    check, = event_data(logged_context, "check")
    assert check["evidence_title"] == "Evidence"
    assert outcome.status == "HIT" and outcome.calls == 1
    assert read_log(logged_context.log.path)[1]["data"] == outcome.trace[1]["data"]


@pytest.mark.parametrize("mode", ["off", "missing"])
async def test_initial_metadata_reports_actual_engine_when_routing_falls_back(mode, logged_context, monkeypatch):
    if mode == "off":
        monkeypatch.setattr(config, "FIRST_ENGINE_ROUTING", "off")
    else:
        config.FIRST_ENGINE_TABLE_PATH.unlink()
    fake = Retrieval(hit="tavily")
    monkeypatch.setattr(second_opinion, "_check", fake.check)
    await second_opinion.search("What was the amount?", "Find the annual revenue", logged_context, engine_search=fake.search)

    search, = event_data(logged_context, "search")
    assert search["first_engine"] == "tavily"
    assert search["question_type"] == "finance"


@pytest.mark.parametrize("failed", [None, "perplexity"])
async def test_only_initial_search_gets_routing_fields_including_engine_failure(failed, logged_context, monkeypatch):
    fake = Retrieval(hit="exa", failed=failed)
    monkeypatch.setattr(second_opinion, "_check", fake.check)
    outcome = await second_opinion.search("Who built it?", "Find evidence", logged_context, engine_search=fake.search)

    initial, rescue = event_data(logged_context, "search")
    assert initial["first_engine"] == "perplexity" and initial["question_type"] == "fact"
    assert initial["status"] == ("error" if failed else "ok")
    assert "first_engine" not in rescue and "question_type" not in rescue
    assert outcome.engines_called == ["perplexity", "exa"] and outcome.status == "HIT"
    assert all("evidence_title" not in check for check in event_data(logged_context, "check")[:-1])


async def test_initial_metadata_is_per_search_call_not_per_run(logged_context, monkeypatch):
    for query, category, engine in [("Who built it?", "fact", "perplexity"), ("Annual revenue", "finance", "exa")]:
        fake = Retrieval(hit=engine)
        monkeypatch.setattr(second_opinion, "_check", fake.check)
        await second_opinion.search(query, "Find evidence", logged_context, engine_search=fake.search)
        assert event_data(logged_context, "search")[-1]["question_type"] == category
    assert [event["first_engine"] for event in event_data(logged_context, "search")] == ["perplexity", "exa"]


@pytest.mark.parametrize("slow_engine", ["perplexity", "exa"])
async def test_timeout_search_event_keeps_initial_metadata_only(slow_engine, logged_context, monkeypatch):
    fake = Retrieval()
    monkeypatch.setattr(second_opinion, "_check", fake.check)

    async def retrieve(query, engines, **kwargs):
        if engines == [slow_engine]:
            await asyncio.Event().wait()
        return await fake.search(query, engines, **kwargs)

    outcome = await second_opinion.search(
        "Who built it?", "Find evidence", logged_context, engine_search=retrieve,
        limits=second_opinion.SearchLimits.from_config(timeout_s=0.05),
    )

    searches = event_data(logged_context, "search")
    assert searches[0]["first_engine"] == "perplexity" and searches[0]["question_type"] == "fact"
    assert searches[-1]["status"] == "error"
    if slow_engine == "exa":
        assert "first_engine" not in searches[-1] and "question_type" not in searches[-1]
    assert outcome.status == "NOT_FOUND"


@pytest.mark.parametrize("citation,expected", [
    ({"evidence_rank": 2}, "Second source"),
    ({"best_url": "https://example.com/second"}, "Second source"),
    ({"best_url": "https://example.com/second", "evidence_rank": 1}, "Second source"),
    ({"evidence_rank": 3}, None),
    ({"best_url": "https://example.com/unknown", "evidence_rank": 1}, None),
    ({}, None),
])
async def test_hit_title_resolves_the_same_citation_as_the_outcome(citation, expected, logged_context, monkeypatch):
    results = [Result(rank=index, title=title, url=f"https://example.com/{name}", engine="perplexity")
               for index, name, title in [(1, "first", "First source"), (2, "second", "Second source")]]
    verdict = CheckerVerdict(verdict="HIT", confidence=1, why="Cited evidence", **citation)

    async def retrieve(query, engines, **kwargs):
        return {engines[0]: EngineRun(engine=engines[0], status="ok", results=results)}

    async def check(*args):
        return verdict

    monkeypatch.setattr(second_opinion, "_check", check)
    outcome = await second_opinion.search("Who built it?", "Find evidence", logged_context, engine_search=retrieve)
    emitted, = event_data(logged_context, "check")
    if expected is None:
        assert "evidence_title" not in emitted
    else:
        assert emitted["evidence_title"] == expected
    assert {key: value for key, value in emitted.items() if key != "evidence_title"} == verdict.model_dump()
    assert outcome.status == "HIT" and outcome.calls == 1


async def test_page_hit_title_uses_the_original_result(logged_context, monkeypatch):
    fake = Retrieval()

    async def check(query, goal, results, ctx):
        return CheckerVerdict(verdict="SHALLOW", confidence=1, why="Read the cited page", best_url=results[0].url)

    async def fetch(url, *args, **kwargs):
        return PageText(url=url, ok=True, text="The requested evidence")

    async def check_page(*args):
        return CheckerVerdict(verdict="HIT", confidence=1, why="Page evidence", evidence_rank=1)

    monkeypatch.setattr(second_opinion, "_check", check)
    monkeypatch.setattr(second_opinion, "_fetch", fetch)
    monkeypatch.setattr(second_opinion, "_check_page", check_page)
    outcome = await second_opinion.search("Who built it?", "Find evidence", logged_context, engine_search=fake.search)

    shallow, hit = event_data(logged_context, "check")
    assert "evidence_title" not in shallow
    assert hit["evidence_title"] == "Evidence"
    assert outcome.status == "HIT" and outcome.calls == 1
