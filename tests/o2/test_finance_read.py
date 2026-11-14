"""Finance misses read one source and never spend another engine search."""

from datetime import date
import json

import pytest

from so import config
from so.contracts import EngineRun, PageText, RunContext
from so.second_opinion import SearchLimits, search
from so.ledger import ledger
from tests.o2.fakes import FakeLog, Fakes, source, verdict

pytestmark = pytest.mark.finance_deep_read


class FinanceFakes(Fakes):
    async def extract(self, url: str, *, goal: str | None = None,
                      chunks_per_source: int | None = None) -> PageText:
        return await self._fetch(url, "tavily", goal=goal, chunks_per_source=chunks_per_source)


@pytest.fixture
def ctx(monkeypatch):
    monkeypatch.setattr(config, "FINANCE_DEEP_READ", True)
    monkeypatch.setattr(config, "FINANCE_EXTRACT_CHUNKS", 5)
    return RunContext(run_id="finance-unit", question_id="unlabelled", setup="second_opinion",
                      session="unit-session", log=FakeLog(), today=date(2026, 11, 14))


@pytest.mark.parametrize("label", ["SHALLOW", "STALE", "WRONG_ENTITY", "DEAD"])
@pytest.mark.parametrize("page_hit", [True, False])
async def test_finance_miss_reads_once_then_stops(label, page_hit, ctx, monkeypatch):
    selected = source("https://example.com/report", rank=2)
    fake = FinanceFakes([
        verdict(label, best_url=selected.url, rewrite="another query"),
        verdict("HIT" if page_hit else "DEAD"),
    ], runs=[EngineRun(engine="tavily", status="ok", results=[source(), selected])],
        pages=[PageText(url=selected.url, ok=True, text="Revenue was $50 million.")])
    fake.install(monkeypatch)
    goal = "Find annual revenue in the financial statement"
    outcome = await search("company annual revenue", goal, ctx)
    assert outcome.status == ("HIT" if page_hit else "NOT_FOUND")
    assert outcome.calls == 1 and outcome.fetches == 1
    assert len(fake.searches) == 1
    assert fake.fetches == [{"url": selected.url, "via": "tavily", "goal": goal, "chunks_per_source": 5}]
    assert fake.checks[1]["page_text"] == "Revenue was $50 million."
    assert outcome.trace[-1]["data"]["rescues_used"] == 1


async def test_finance_uses_first_result_when_checker_has_no_page(ctx, monkeypatch):
    fake = FinanceFakes([verdict("DEAD"), verdict("HIT")],
                        pages=[PageText(url=source().url, ok=True, text="Revenue was $50 million.")])
    fake.install(monkeypatch)
    outcome = await search("annual revenue", "find annual revenue", ctx)
    assert outcome.status == "HIT" and fake.fetches[0]["url"] == source().url


async def test_finance_fetch_failure_does_not_search_again(ctx, monkeypatch):
    fake = FinanceFakes([verdict("DEAD")], pages=[RuntimeError("extract unavailable"),
                                               PageText(url=source().url, ok=False)])
    fake.install(monkeypatch)
    outcome = await search("annual revenue", "find annual revenue", ctx)
    assert outcome.status == "NOT_FOUND" and outcome.calls == 1
    assert [event["via"] for event in fake.fetches] == ["tavily", "telem"]
    assert fake.fetches[1]["session"] == "unit-session"


async def test_finance_without_results_does_not_research(ctx, monkeypatch):
    fake = FinanceFakes([verdict("DEAD")], runs=[EngineRun(engine="tavily", status="ok")])
    fake.install(monkeypatch)
    outcome = await search("annual revenue", "find annual revenue", ctx)
    assert outcome.status == "NOT_FOUND" and outcome.calls == 1 and outcome.fetches == 0


async def test_finance_obeys_rescue_limit(ctx, monkeypatch):
    fake = FinanceFakes([verdict("DEAD")])
    fake.install(monkeypatch)
    outcome = await search("annual revenue", "find annual revenue", ctx,
                           limits=SearchLimits.from_config(max_rescues=0))
    assert outcome.status == "NOT_FOUND" and outcome.fetches == 0


async def test_nonfinancial_query_preserves_engine_rescue(ctx, monkeypatch):
    fake = FinanceFakes([verdict("DEAD"), verdict("HIT", evidence_rank=1)])
    fake.install(monkeypatch)
    outcome = await search("mountain height", "find the height of the mountain", ctx)
    assert outcome.status == "HIT" and outcome.calls == 2 and outcome.fetches == 0


async def test_finance_composes_with_routing_and_initial_ledger(ctx, monkeypatch, tmp_path):
    table = tmp_path / "routing.json"
    table.write_text(json.dumps({
        "built_on_split": "learn", "n_questions": 2,
        "routing": {"fact": "tavily", "finance": "exa"},
        "price_usd": {"tavily": 0.005, "exa": 0.005},
        "counts": {
            category: {engine: {"n_questions": 1, "hits": int(engine == winner),
                                "hit_rate": float(engine == winner)} for engine in ("tavily", "exa")}
            for category, winner in (("fact", "tavily"), ("finance", "exa"))
        },
    }))
    monkeypatch.setattr(config, "FIRST_ENGINE_ROUTING", "on")
    monkeypatch.setattr(config, "FIRST_ENGINE_TABLE_PATH", table)
    fake = FinanceFakes([verdict("DEAD"), verdict("HIT")],
                        pages=[PageText(url=source().url, ok=True, text="Revenue was $50 million.")])
    fake.install(monkeypatch)
    outcome = await search("annual revenue", "find annual revenue", ctx)
    assert outcome.status == "HIT" and outcome.engines_called == ["exa"]
    assert {key: fake.searches[0][key] for key in ("context", "metadata")} == ledger("initial", None, "exa")
    assert fake.searches[0]["goal"] == "find annual revenue"
    assert fake.searches[0]["session"] == "unit-session"
