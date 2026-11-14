"""Exercise routed searches with fake retrieval and checks through the real policy."""

from __future__ import annotations

from typing import Any

import pytest

from so import config, router, second_opinion
from so.contracts import CheckerVerdict, EngineRun, RescueTable, Result, RunContext


class Retrieval:
    def __init__(self, *, hit: str | None = None, failed: str | None = None) -> None:
        self.hit = hit
        self.failed = failed
        self.calls: list[dict[str, Any]] = []

    async def search(self, query: str, engines: list[str], **kwargs: Any) -> dict[str, EngineRun]:
        engine, = engines
        self.calls.append({"engine": engine, "query": query, **kwargs})
        results = [Result(rank=1, title="Evidence", url=f"https://example.com/{engine}",
                          snippet="Recorded evidence", engine=engine)]
        return {engine: EngineRun(engine=engine, status="error" if engine == self.failed else "ok",
                                  results=results, cost_usd=0.001)}

    async def check(self, query, goal, results, ctx) -> CheckerVerdict:
        hit = bool(results) and results[0].engine == self.hit
        return CheckerVerdict(verdict="HIT" if hit else "DEAD", confidence=1,
                              why="Unit evidence", evidence_rank=1 if hit else None)


@pytest.fixture
def routed_search(learn_data, monkeypatch):
    questions, rows = learn_data
    router.write_table(router.build(rows, questions), config.FIRST_ENGINE_TABLE_PATH)
    return RunContext(run_id="routing-unit", question_id="unit", setup="second_opinion", session="unit-session")


@pytest.mark.parametrize("query,expected", [
    ("Who built the observatory?", ["perplexity", "tavily", "exa"]),
    ("What is the revenue?", ["exa", "tavily", "perplexity"]),
])
async def test_search_uses_order_after_routed_engine(query, expected, routed_search, monkeypatch):
    table = RescueTable(order_after={"perplexity": ["tavily"], "exa": ["tavily"]},
                        fallback_order=["perplexity", "exa", "tavily"])
    monkeypatch.setattr(second_opinion, "load_rescue_table", lambda: table)
    fake = Retrieval(hit=expected[-1])
    monkeypatch.setattr(second_opinion, "_check", fake.check)
    outcome = await second_opinion.search(query, "Find evidence", routed_search, engine_search=fake.search)
    assert outcome.status == "HIT"
    assert outcome.engines_called == expected
    assert outcome.calls == 3
    assert all(call["session"] == "unit-session" and call["goal"] == "Find evidence" for call in fake.calls)
    assert [event["data"]["engine"] for event in outcome.trace if event["type"] == "rescue"] == expected[1:]


@pytest.mark.first_engine_routing
async def test_routed_search_loads_rescue_order_for_selected_engine(routed_search, monkeypatch):
    table = RescueTable(order_after={"exa": ["tavily"]}, fallback_order=["perplexity"])
    config.RESCUE_TABLE_PATH.write_text(table.model_dump_json(), encoding="utf-8")
    fake = Retrieval(hit="tavily")
    monkeypatch.setattr(second_opinion, "_check", fake.check)

    outcome = await second_opinion.search(
        "What was the annual revenue?", "Find revenue evidence", routed_search, engine_search=fake.search,
    )

    assert config.FIRST_ENGINE_ROUTING == "on"
    assert outcome.status == "HIT"
    assert [call["engine"] for call in fake.calls] == ["exa", "tavily"]
    assert outcome.engines_called == ["exa", "tavily"]
    rescue, = [event["data"] for event in outcome.trace if event["type"] == "rescue"]
    assert rescue["engine"] == table.order_after["exa"][0]
    assert rescue["engine"] != table.fallback_order[0]


async def test_missing_order_uses_fallback_and_never_repeats_engine(routed_search, monkeypatch):
    table = RescueTable(fallback_order=["perplexity", "tavily", "exa", "tavily"])
    monkeypatch.setattr(second_opinion, "load_rescue_table", lambda: table)
    fake = Retrieval()
    monkeypatch.setattr(second_opinion, "_check", fake.check)
    outcome = await second_opinion.search("Who built it?", "Find evidence", routed_search, engine_search=fake.search)
    assert outcome.status == "NOT_FOUND"
    assert outcome.engines_called == ["perplexity", "tavily", "exa"]
    assert sum(event["type"] == "rescue" for event in outcome.trace) == config.MAX_RESCUES


async def test_engine_failure_uses_routed_rescue_order(routed_search, monkeypatch):
    table = RescueTable(order_after={"perplexity": ["tavily"]}, fallback_order=["exa"])
    monkeypatch.setattr(second_opinion, "load_rescue_table", lambda: table)
    fake = Retrieval(hit="tavily", failed="perplexity")
    monkeypatch.setattr(second_opinion, "_check", fake.check)
    outcome = await second_opinion.search("Who built it?", "Find evidence", routed_search, engine_search=fake.search)
    assert outcome.engines_called == ["perplexity", "tavily"]
    assert outcome.status == "HIT"
    rescue, = [event["data"] for event in outcome.trace if event["type"] == "rescue"]
    assert rescue["counted"] is False


async def test_disabled_routing_keeps_configured_first_engine(routed_search, monkeypatch):
    monkeypatch.setattr(config, "FIRST_ENGINE_ROUTING", "off")
    fake = Retrieval(hit="tavily")
    monkeypatch.setattr(second_opinion, "_check", fake.check)
    outcome = await second_opinion.search("What is the revenue?", "Find evidence", routed_search, engine_search=fake.search)
    assert outcome.engines_called == ["tavily"]


async def test_invalid_table_fails_before_paid_work(routed_search):
    config.FIRST_ENGINE_TABLE_PATH.write_text("invalid")
    fake = Retrieval()
    with pytest.raises(router.RoutingTableError):
        await second_opinion.search("Who built it?", "Find evidence", routed_search, engine_search=fake.search)
    assert not fake.calls
