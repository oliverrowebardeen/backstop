from datetime import date

import pytest

from so.config import BudgetExceeded
from so.contracts import EngineRun, PageText, RescueTable, RunContext
from so.second_opinion import search
from tests.o2.fakes import FakeLog, Fakes, source, verdict


@pytest.fixture
def ctx():
    return RunContext(
        run_id="second_opinion-unit", question_id="unit", setup="second_opinion",
        session="session-unit", log=FakeLog(), today=date(2026, 11, 14),
    )


@pytest.fixture(autouse=True)
def table(monkeypatch):
    monkeypatch.setattr("so.second_opinion.load_rescue_table", lambda: RescueTable())


async def test_first_hit_stops_and_includes_checker_cost(ctx, monkeypatch):
    fake = Fakes([verdict("HIT", evidence_rank=1)])
    fake.install(monkeypatch)
    outcome = await search("query", "goal", ctx)
    assert outcome.status == "HIT"
    assert outcome.calls == 1 and outcome.fetches == 0
    assert outcome.engines_called == ["tavily"]
    assert outcome.cost_usd == pytest.approx(0.006)
    assert outcome.latency_ms >= 120
    assert outcome.note == "Answer is in result 1. You can stop searching for this."
    assert fake.searches[0]["session"] == "session-unit"
    assert fake.searches[0]["goal"] == "goal"
    assert [item["type"] for item in ctx.log.events] == ["search", "llm", "check", "search_done"]
    assert outcome.trace == ctx.log.events


@pytest.mark.parametrize("label", ["STALE", "WRONG_ENTITY", "DEAD"])
async def test_retry_uses_rewrite_and_passes_session_and_goal(label, ctx, monkeypatch):
    fake = Fakes([verdict(label, rewrite="new query"), verdict("HIT")])
    fake.install(monkeypatch)
    outcome = await search("query", "goal", ctx)
    assert outcome.status == "HIT" and outcome.calls == 2
    assert fake.searches[1]["query"] == "new query"
    assert fake.searches[1]["published_after"] == ("2026-08-16" if label == "STALE" else None)
    assert all(item["goal"] == "goal" and item["session"] == "session-unit" for item in fake.searches)
    assert ctx.memory.queries == ["query", "new query"]


async def test_stale_uses_date_in_goal(ctx, monkeypatch):
    fake = Fakes([verdict("STALE"), verdict("HIT")])
    fake.install(monkeypatch)
    await search("query", "find the 2023 valuation", ctx)
    assert fake.searches[1]["published_after"] == "2023-01-01"


async def test_two_rescues_then_not_found_without_repeating_engine(ctx, monkeypatch):
    fake = Fakes([verdict("DEAD")] * 3)
    fake.install(monkeypatch)
    outcome = await search("query", "goal", ctx)
    assert outcome.status == "NOT_FOUND" and outcome.calls == 3
    assert outcome.engines_called == ["tavily", "exa", "perplexity"]
    assert sum(item["type"] == "rescue" for item in outcome.trace) == 2
    assert outcome.cost_usd == pytest.approx(0.018)
    assert outcome.note == "No engine surfaced this after 3 tries. If you cannot answer from other evidence, answer NOT FOUND."


async def test_hit_on_final_rescue_is_returned(ctx, monkeypatch):
    fake = Fakes([verdict("DEAD"), verdict("WRONG_ENTITY"), verdict("HIT")])
    fake.install(monkeypatch)
    outcome = await search("query", "goal", ctx)
    assert outcome.status == "HIT" and outcome.calls == 3


async def test_merge_deduplicates_urls_and_remaps_evidence_rank(ctx, monkeypatch):
    first = source("https://example.com/one")
    duplicate = source("https://example.com/one", engine="exa", snippet="Better evidence")
    second = source("https://example.com/two", engine="exa", rank=2)
    fake = Fakes([verdict("DEAD"), verdict("HIT", evidence_rank=2)], runs=[
        EngineRun(engine="tavily", status="ok", results=[first]),
        EngineRun(engine="exa", status="ok", results=[duplicate, second]),
    ])
    fake.install(monkeypatch)
    outcome = await search("query", "goal", ctx)
    assert [item.url for item in outcome.results] == [first.url, second.url]
    assert [item.rank for item in outcome.results] == [1, 2]
    assert outcome.results[0].snippet == "Better evidence"
    assert "result 2" in outcome.note
    assert fake.checks[1]["results"] == [duplicate, second]


async def test_shallow_reads_tavily_and_returns_page_evidence(ctx, monkeypatch):
    url = source().url
    fake = Fakes([verdict("SHALLOW", best_url=url), verdict("HIT", evidence_rank=1)], pages=[
        PageText(url=url, ok=True, text="The valuation was $50 billion.", cost_usd=0.002, latency_ms=40),
    ])
    fake.install(monkeypatch)
    outcome = await search("query", "goal", ctx)
    assert outcome.status == "HIT" and outcome.calls == 1 and outcome.fetches == 1
    assert fake.fetches == [{"url": url, "via": "tavily"}]
    assert outcome.results[0].snippet == "The valuation was $50 billion."
    assert fake.checks[1]["page_text"] == outcome.results[0].snippet
    assert outcome.cost_usd == pytest.approx(0.009)
    assert outcome.latency_ms >= 180


@pytest.mark.parametrize("failure", [
    RuntimeError("extract unavailable"),
    PageText(url=source().url, ok=False, cost_usd=0.001),
    PageText(url=source().url, ok=True, text="  "),
])
async def test_shallow_falls_back_to_telem_with_session_and_goal(failure, ctx, monkeypatch):
    url = source().url
    fake = Fakes([verdict("SHALLOW", best_url=url), verdict("HIT")], pages=[
        failure, PageText(url=url, ok=True, text="Page answer", cost_usd=0.002),
    ])
    fake.install(monkeypatch)
    outcome = await search("query", "goal", ctx)
    assert outcome.status == "HIT" and outcome.fetches == 2
    assert fake.fetches[1] == {"url": url, "via": "telem", "goal": "goal", "session": "session-unit"}
    assert [item["data"]["via"] for item in outcome.trace if item["type"] == "fetch"] == ["tavily", "telem"]
    assert sum(item["type"] == "rescue" for item in outcome.trace) == 1


async def test_two_failed_readers_leave_only_one_rescue(ctx, monkeypatch):
    fake = Fakes([verdict("SHALLOW", best_url=source().url), verdict("DEAD")], pages=[
        RuntimeError("extract unavailable"), RuntimeError("fetch unavailable"),
    ])
    fake.install(monkeypatch)
    outcome = await search("query", "goal", ctx)
    assert outcome.status == "NOT_FOUND" and outcome.calls == 2 and outcome.fetches == 2
    assert sum(item["type"] == "rescue" for item in outcome.trace) == 2
    assert len(fake.checks) == 2


async def test_page_miss_can_retry_once_then_stops(ctx, monkeypatch):
    fake = Fakes([
        verdict("SHALLOW", best_url=source().url), verdict("STALE", rewrite="fresh query"), verdict("DEAD"),
    ], pages=[PageText(url=source().url, ok=True, text="Old evidence")])
    fake.install(monkeypatch)
    outcome = await search("query", "goal", ctx)
    assert outcome.status == "NOT_FOUND" and outcome.calls == 2 and outcome.fetches == 1
    assert fake.searches[1]["published_after"] == "2026-08-16"
    assert fake.searches[1]["query"] == "fresh query"


async def test_budget_failure_is_never_swallowed_by_fallback(ctx, monkeypatch):
    fake = Fakes([verdict("SHALLOW", best_url=source().url)], pages=[BudgetExceeded("budget exhausted")])
    fake.install(monkeypatch)
    with pytest.raises(BudgetExceeded):
        await search("query", "goal", ctx)
    assert len(fake.fetches) == 1


async def test_run_memory_persists_but_engine_limit_is_per_search(ctx, monkeypatch):
    fake = Fakes([verdict("HIT"), verdict("HIT")])
    fake.install(monkeypatch)
    await search("query", "goal", ctx)
    await search("query", "goal", ctx)
    assert ctx.memory.queries == ["query", "query"]
    assert ctx.memory.engines_by_query == {"query": {"tavily"}}
    assert len(fake.searches) == 2


async def test_first_telem_response_supplies_session_for_retry(ctx, monkeypatch):
    ctx.session = None
    fake = Fakes([verdict("DEAD"), verdict("HIT")])
    fake.install(monkeypatch)
    await search("query", "goal", ctx)
    assert fake.searches[0]["session"] is None
    assert fake.searches[1]["session"] == "session-created"
    assert ctx.session == "session-created"


async def test_cache_retains_original_cost_and_latency(ctx, monkeypatch):
    fake = Fakes([verdict("HIT")], runs=[EngineRun(
        engine="tavily", status="ok", results=[source()], cached=True, cost_usd=0.005, latency_ms=900,
    )])
    fake.install(monkeypatch)
    outcome = await search("query", "goal", ctx)
    assert outcome.cost_usd == pytest.approx(0.006)
    assert outcome.latency_ms >= 920
    assert outcome.trace[0]["data"]["cached"] is True


async def test_engine_error_does_not_check_unusable_results(ctx, monkeypatch):
    fake = Fakes([verdict("DEAD"), verdict("HIT")], runs=[
        EngineRun(engine="tavily", status="error", results=[source()]),
    ])
    fake.install(monkeypatch)
    await search("query", "goal", ctx)
    assert fake.checks[0]["results"] == []


async def test_final_page_hit_points_to_merged_rank(ctx, monkeypatch):
    url = "https://example.com/second"
    fake = Fakes([
        verdict("DEAD"), verdict("SHALLOW", best_url=url), verdict("HIT"),
    ], runs=[
        EngineRun(engine="tavily", status="ok", results=[source()]),
        EngineRun(engine="exa", status="ok", results=[source(url, engine="exa")]),
    ], pages=[PageText(url=url, ok=True, text="Answer on the second page")])
    fake.install(monkeypatch)
    outcome = await search("query", "goal", ctx)
    assert outcome.status == "HIT" and outcome.calls == 2 and outcome.fetches == 1
    assert outcome.note == "Answer is in result 2. You can stop searching for this."


async def test_no_logger_still_returns_costed_trace(ctx, monkeypatch):
    ctx.log = None
    fake = Fakes([verdict("HIT")])
    fake.install(monkeypatch)
    outcome = await search("query", "goal", ctx)
    assert outcome.cost_usd == pytest.approx(0.006)
    assert len(outcome.trace) == 4


async def test_transport_failure_preserves_issued_query_in_memory(ctx, monkeypatch):
    fake = Fakes([])
    fake.install(monkeypatch)

    async def fail(*args, **kwargs):
        raise RuntimeError("search unavailable")

    import sys
    monkeypatch.setattr(sys.modules["so.telem_client"], "search_engines", fail)
    with pytest.raises(RuntimeError, match="search unavailable"):
        await search("query", "goal", ctx)
    assert ctx.memory.queries == ["query"]
    assert ctx.memory.engines_by_query == {"query": {"tavily"}}
