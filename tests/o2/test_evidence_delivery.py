"""Compact HIT delivery keeps every page used for evidence valid as a citation."""

from datetime import date
import json

import pytest

from so import config, telem_client
from so.agent import _answer, _tool_result
from so.contracts import EngineRun, PageText, RunContext, ToolCall
from so.second_opinion import _delivery_results, search
from tests.o2.fakes import FakeLog, Fakes, source, verdict

pytestmark = pytest.mark.evidence_delivery


@pytest.fixture(autouse=True)
def delivery_settings(monkeypatch):
    monkeypatch.setattr(config, "EVIDENCE_FILTER_ENABLED", True)
    monkeypatch.setattr(config, "EVIDENCE_EXTRA_RESULTS", 1)


def test_selection_preserves_all_pages_and_uses_original_engine_rank():
    cited = source("https://example.com/cited", rank=6)
    page = source("https://example.com/read", rank=2, snippet="Full extracted evidence")
    best = source("https://example.com/best", rank=8)
    other = source("https://example.com/other", rank=1)
    merged = {item.url: item for item in [other, page.model_copy(update={"snippet": "short"}), cited, best]}
    selected = _delivery_results(merged, {page.url: page},
                                 {best.url: {"exa": 1}, other.url: {"tavily": 3}}, cited.url, 1)
    assert selected == [cited, page, best]


@pytest.mark.parametrize("extras", [0, 1, 10])
def test_extras_do_not_limit_or_duplicate_mandatory_pages(extras):
    pages = [source(f"https://example.com/{index}", rank=index + 1) for index in range(3)]
    available = {page.url: page for page in pages}
    assert _delivery_results(available, available, {}, pages[0].url, extras) == pages


async def test_hit_retains_extracted_source_and_later_agent_reads(monkeypatch):
    page_url = "https://example.com/income"
    cited_url = "https://example.com/balance"
    extra_url = "https://example.com/report"
    ctx = RunContext(run_id="delivery-unit", question_id="unit", setup="second_opinion",
                     log=FakeLog(), today=date(2026, 11, 14))
    fake = Fakes([
        verdict("SHALLOW", best_url=page_url), verdict("DEAD"), verdict("HIT", evidence_rank=2),
    ], runs=[
        EngineRun(engine="tavily", status="ok", results=[source(extra_url), source(page_url, rank=2)]),
        EngineRun(engine="exa", status="ok", results=[
            source(page_url, engine="exa", snippet="Short replacement"),
            source(cited_url, engine="exa", rank=2),
            source("https://example.com/unused", engine="exa", rank=3),
        ]),
    ], pages=[PageText(url=page_url, ok=True, text="Extracted input used in the calculation")])
    fake.install(monkeypatch)
    outcome = await search("company report", "find the calculation inputs", ctx)
    assert outcome.status == "HIT"
    assert [item.url for item in outcome.results] == [cited_url, page_url, extra_url]
    assert outcome.results[1].snippet == "Extracted input used in the calculation"
    assert {entry["url"] for entry in outcome.trace[-1]["data"]["ranking"]} == {cited_url, page_url, extra_url}

    async def retrieved(*args):
        return outcome

    sources: set[str] = set()
    counts = {"searches": 0, "reads": 0}
    await _tool_result(ToolCall(id="search", name="search", arguments={
        "query": "company report", "goal": "find the calculation inputs",
    }), ctx, retrieved, counts, sources, {})
    later_url = "https://example.com/later"

    async def read(url, **kwargs):
        return PageText(url=url, ok=True, text="Additional independent evidence")

    monkeypatch.setattr(telem_client, "fetch_page", read)
    await _tool_result(ToolCall(id="read", name="read_page", arguments={"url": later_url}),
                       ctx, retrieved, counts, sources, {})
    answer = json.dumps({"final": "Calculated answer", "status": "ANSWERED",
                         "citations": [cited_url, page_url, later_url]})
    assert _answer(answer, sources, counts["searches"]).status == "ANSWERED"
    with pytest.raises(ValueError, match="citations from retrieved evidence"):
        _answer(json.dumps({"final": "Unsupported", "status": "ANSWERED",
                            "citations": ["https://example.com/unused"]}), sources, 1)
