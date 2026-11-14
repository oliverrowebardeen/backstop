"""SHALLOW page reads through Extract chunks and the HIT evidence object."""

from datetime import date

import pytest

from so import config
from so.contracts import EngineRun, PageText, RunContext
from so.second_opinion import search
from tests.o2.fakes import FakeLog, Fakes, source, verdict

URL = "https://example.com/report"
CHUNKS = "Company overview [...] Revenue was $4.2 billion in fiscal 2025. [...] Board members"


@pytest.fixture
def ctx():
    return RunContext(
        run_id="second_opinion-a3", question_id="a3", setup="second_opinion",
        session="session-a3", log=FakeLog(), today=date(2026, 11, 14),
    )


def evidence_of(outcome):
    done = [event for event in outcome.trace if event["type"] == "search_done"][-1]
    return done["data"].get("evidence")


def hit(why='Page states "$4.2 billion" revenue.', **kwargs):
    return verdict("HIT", **kwargs).model_copy(update={"why": why})


async def test_extract_chunks_reach_page_recheck_and_evidence(ctx, monkeypatch):
    page = PageText(url=URL, ok=True, text=CHUNKS, cost_usd=config.TAVILY_EXTRACT_PRICE_USD, latency_ms=30)
    run = EngineRun(engine="tavily", status="ok", cost_usd=0.005, results=[
        source("https://example.com/other", engine="tavily"), source(URL, engine="tavily", rank=2),
    ])
    fake = Fakes([verdict("SHALLOW", best_url=URL), hit()], runs=[run], pages=[page])
    fake.install(monkeypatch)
    outcome = await search("acme revenue 2025", "find Acme's fiscal 2025 revenue", ctx)
    assert outcome.status == "HIT"
    assert [item["via"] for item in fake.fetches] == ["tavily"]
    assert fake.fetches[0]["goal"] == "find Acme's fiscal 2025 revenue"
    assert fake.checks[-1]["page_text"] == CHUNKS
    evidence = evidence_of(outcome)
    assert evidence["verified"] is True
    assert evidence["url"] == URL
    assert evidence["snippet"] == "Revenue was $4.2 billion in fiscal 2025."
    assert evidence["source"] == "tavily_extract" and evidence["chunks"] == 3
    assert evidence["rank_reason"] == "engine rank 2"


async def test_extract_cost_is_accounted(ctx, monkeypatch):
    page = PageText(url=URL, ok=True, text=CHUNKS, cost_usd=config.TAVILY_EXTRACT_PRICE_USD, latency_ms=30)
    fake = Fakes([verdict("SHALLOW", best_url=URL), hit()], pages=[page])
    fake.install(monkeypatch)
    outcome = await search("q", "goal", ctx)
    # One engine search (0.005), two checker calls (0.001 each), one Extract.
    assert outcome.cost_usd == pytest.approx(0.007 + config.TAVILY_EXTRACT_PRICE_USD)
    fetch = next(event for event in outcome.trace if event["type"] == "fetch")
    assert fetch["data"]["cost_usd"] == pytest.approx(config.TAVILY_EXTRACT_PRICE_USD)
    assert outcome.fetches == 1


async def test_extract_failure_falls_back_to_telem_fetch(ctx, monkeypatch):
    telem = PageText(url=URL, ok=True, text="Full page: revenue was $4.2 billion.", cost_usd=0.001)
    fake = Fakes([verdict("SHALLOW", best_url=URL), hit()], pages=[TimeoutError("slow"), telem])
    fake.install(monkeypatch)
    outcome = await search("q", "goal", ctx)
    assert outcome.status == "HIT"
    assert [item["via"] for item in fake.fetches] == ["tavily", "telem"]
    assert fake.checks[-1]["page_text"] == telem.text
    evidence = evidence_of(outcome)
    assert evidence["source"] == "telem_fetch" and "chunks" not in evidence
    assert evidence["snippet"] == telem.text


@pytest.mark.parametrize("empty", [PageText(url=URL, ok=True, text="  "), PageText(url=URL, ok=False)])
async def test_empty_extract_falls_back_to_telem_fetch(empty, ctx, monkeypatch):
    telem = PageText(url=URL, ok=True, text="Revenue was $4.2 billion.")
    fake = Fakes([verdict("SHALLOW", best_url=URL), hit()], pages=[empty, telem])
    fake.install(monkeypatch)
    outcome = await search("q", "goal", ctx)
    assert [item["via"] for item in fake.fetches] == ["tavily", "telem"]
    assert fake.checks[-1]["page_text"] == telem.text
    assert evidence_of(outcome)["source"] == "telem_fetch"


async def test_search_hit_evidence_counts_agreeing_engines(ctx, monkeypatch):
    shared = "https://example.com/acme"
    runs = [
        EngineRun(engine="tavily", status="ok", cost_usd=0.005, results=[
            source(shared, engine="tavily", snippet="Acme booked revenue of 4,200,000,000 dollars."),
        ]),
        EngineRun(engine="exa", status="ok", cost_usd=0.005, results=[
            source("https://other.example", engine="exa", snippet="Unrelated"),
            source(shared, engine="exa", rank=2, snippet="Acme revenue: $4.2bn in 2025."),
        ]),
    ]
    fake = Fakes([verdict("DEAD", rewrite="acme revenue"), hit(evidence_rank=2)], runs=runs)
    fake.install(monkeypatch)
    outcome = await search("acme 2025", "find Acme's 2025 revenue", ctx)
    assert outcome.status == "HIT" and outcome.fetches == 0
    evidence = evidence_of(outcome)
    assert evidence["engines_agreeing"] == 2
    assert evidence["agreeing_engines"] == ["exa", "tavily"]
    assert evidence["source"] == "search"
    assert evidence["url"] == shared
    assert evidence["rank_reason"] == "agreement: 2 engines"
    assert len(evidence["snippet"]) <= 500


async def test_no_evidence_object_without_hit(ctx, monkeypatch):
    fake = Fakes([verdict("DEAD"), verdict("DEAD"), verdict("DEAD")])
    fake.install(monkeypatch)
    outcome = await search("q", "goal", ctx)
    assert outcome.status == "NOT_FOUND"
    assert evidence_of(outcome) is None
