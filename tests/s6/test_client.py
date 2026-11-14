"""Part C: BackstopTelem is a one-line drop-in for telem.AsyncTelem."""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Any

import pytest
from telem import AsyncTelem, ProviderRun, SearchResponse, SearchResult

from backstop import BackstopSearchResponse, BackstopTelem, SearchLimits, TelemUnavailable
from tests.s6.fakes import CheckedVerdict, verdict

ENGINE_RESULTS = {
    "tavily": [("https://t1.example", "Tavily one"), ("https://shared.example", "Shared")],
    "exa": [("https://shared.example", "Shared"), ("https://evidence.example", "Evidence page")],
    "perplexity": [("https://p1.example", "Perplexity one")],
}


class FakeAsyncTelem:
    """Mimics AsyncTelem.search and returns real telem SearchResponse objects."""

    def __init__(self, *, fail_engines: set[str] | None = None, raise_all: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail_engines = fail_engines or set()
        self.raise_all = raise_all
        self.closed = False

    async def search(self, query: Any, **kwargs: Any) -> SearchResponse:
        self.calls.append({"query": query, **kwargs})
        if self.raise_all:
            raise RuntimeError("router unreachable")
        engines = kwargs.get("providers_include") or ["tavily", "exa"]
        runs = []
        for engine in engines:
            if engine in self.fail_engines:
                runs.append(ProviderRun(provider=engine, status="error", error={"message": "upstream 503"},
                                        latency_ms=50, query=query))
                continue
            results = [SearchResult(url=url, title=title, summary=f"{title} summary", rank=rank, provider=engine)
                       for rank, (url, title) in enumerate(ENGINE_RESULTS.get(engine, []), 1)]
            runs.append(ProviderRun(provider=engine, status="success", results=results, latency_ms=120, query=query))
        flat = [result for run in runs for result in run.results]
        return SearchResponse(results=flat, by_provider=runs, session_id=kwargs.get("session") or "sess-1",
                              interaction_id="int-1", status="completed")

    async def providers(self) -> list[str]:
        return ["tavily", "exa"]

    async def aclose(self) -> None:
        self.closed = True

    async def __aenter__(self) -> FakeAsyncTelem:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()


def install_checker(monkeypatch: Any, steps: list[Any]) -> list[dict[str, Any]]:
    """Script checker verdicts without a model call."""
    seen: list[dict[str, Any]] = []

    async def check(query, goal, results, today):
        seen.append({"query": query, "goal": goal, "urls": [item.url for item in results]})
        step = steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return CheckedVerdict(**step.model_dump())

    module = ModuleType("so.checker")
    module.check = check
    module.check_page = check
    monkeypatch.setitem(sys.modules, "so.checker", module)
    return seen


async def existing_call_site(client: Any) -> dict[str, Any]:
    """Code written against AsyncTelem, unchanged."""
    async with client:
        resp = await client.search("Stripe 2023 valuation", goal="find the valuation figure",
                                   session=None, include_domains=["reuters.com", "stripe.com"])
        return {
            "urls": [r.url for r in resp.results],
            "first": (resp.results[0].title, resp.results[0].summary, resp.results[0].rank, resp.results[0].provider),
            "content": resp.results[0].content,
            "providers": [(p.provider, p.status, p.error, p.latency_ms, [r.url for r in p.results]) for p in resp.by_provider],
            "session": resp.session_id,
        }


async def test_existing_call_site_works_unchanged_after_swap(monkeypatch):
    install_checker(monkeypatch, [verdict("DEAD", rewrite="Stripe Series I valuation 2023"),
                                  verdict("HIT", evidence_rank=2)])
    before = await existing_call_site(FakeAsyncTelem())
    fake = FakeAsyncTelem()
    after = await existing_call_site(BackstopTelem(telem=fake))
    assert set(before) == set(after)
    assert after["first"][0] == "Evidence page" and after["first"][2] == 1 and after["first"][3] == "exa"
    assert after["content"] == "Evidence page summary"
    assert [p[0] for p in after["providers"]] == ["tavily", "exa"]
    assert after["session"] == "sess-1"
    assert fake.closed
    # Pass-through kwargs reach every Telem call; engines are pinned one at a time.
    assert all(call["include_domains"] == ["reuters.com", "stripe.com"] for call in fake.calls)
    assert [call["providers_include"] for call in fake.calls] == [["tavily"], ["exa"]]
    assert fake.calls[1]["query"] == "Stripe Series I valuation 2023"
    assert fake.calls[1]["session"] == "sess-1"
    assert all(call["goal"] == "find the valuation figure" for call in fake.calls)


async def test_response_shape_matches_telem_plus_verification_fields(monkeypatch):
    install_checker(monkeypatch, [verdict("DEAD", rewrite="q2"), verdict("HIT", evidence_rank=2)])
    resp = await BackstopTelem(telem=FakeAsyncTelem()).search("q", goal="g")
    assert isinstance(resp, BackstopSearchResponse)
    telem_result_fields = {"url", "title", "summary", "rank", "provider"}
    telem_run_fields = {"provider", "status", "results", "error", "latency_ms"}
    assert telem_result_fields <= set(resp.results[0].model_dump())
    assert telem_run_fields <= set(resp.by_provider[0].model_dump())
    assert {"by_provider", "results", "session_id"} <= set(SearchResponse.model_fields) & set(BackstopSearchResponse.model_fields)
    assert resp.status == "HIT" and resp.found and resp.verdict == "HIT"
    assert resp.engine_calls == 2 and resp.engines_called == ["tavily", "exa"]
    assert resp.cost_usd == pytest.approx(0.012)
    assert resp.latency_ms > 0 and resp.trace
    assert [(r.url, r.rank_reason) for r in resp.results] == [
        ("https://evidence.example", "evidence"),
        ("https://shared.example", "agreement: 2 engines"),
        ("https://t1.example", "engine rank 1"),
    ]
    assert resp.note == "Answer is in result 1. You can stop searching for this."
    rescue = next(e["data"] for e in resp.trace if e["type"] == "rescue")
    assert (rescue["original_query"], rescue["rewritten_query"]) == ("q", "q2")


async def test_not_found_and_limits_per_call(monkeypatch):
    install_checker(monkeypatch, [verdict("DEAD")] * 4)
    client = BackstopTelem(telem=FakeAsyncTelem(), max_rescues=2)
    assert client.limits == SearchLimits.from_config(max_rescues=2)
    resp = await client.search("q", goal="g", max_rescues=0)
    assert resp.status == "NOT_FOUND" and resp.engine_calls == 1 and resp.verdict == "DEAD"
    assert "answer NOT FOUND" in resp.note


async def test_checker_failure_returns_unchecked(monkeypatch):
    install_checker(monkeypatch, [RuntimeError("model down")])
    resp = await BackstopTelem(telem=FakeAsyncTelem()).search("q")
    assert resp.status == "UNCHECKED" and resp.verdict is None
    assert "checker failed" in resp.why and resp.results


async def test_engine_error_shows_in_by_provider(monkeypatch):
    install_checker(monkeypatch, [verdict("DEAD"), verdict("HIT", evidence_rank=1)])
    resp = await BackstopTelem(telem=FakeAsyncTelem(fail_engines={"tavily"})).search("q")
    assert resp.status == "HIT"
    assert resp.by_provider[0].status == "error" and resp.by_provider[0].error == {"message": "upstream 503"}


async def test_telem_down_raises_typed_error(monkeypatch):
    install_checker(monkeypatch, [])
    with pytest.raises(TelemUnavailable, match="router unreachable"):
        await BackstopTelem(telem=FakeAsyncTelem(raise_all=True)).search("q")


async def test_loop_owned_options_are_ignored_and_other_methods_forwarded(monkeypatch, caplog):
    install_checker(monkeypatch, [verdict("HIT", evidence_rank=1)])
    fake = FakeAsyncTelem()
    client = BackstopTelem(telem=fake)
    await client.search("q", providers_include=["brave"], num_results=10, topic="news")
    assert fake.calls[0]["providers_include"] == ["tavily"] and fake.calls[0]["topic"] == "news"
    assert "providers_include" in caplog.text
    assert await client.providers() == ["tavily", "exa"]


async def test_log_dir_writes_a_replayable_event_log(monkeypatch, tmp_path):
    from so.eventlog import read_log, summarize

    install_checker(monkeypatch, [verdict("HIT", evidence_rank=1)])
    resp = await BackstopTelem(telem=FakeAsyncTelem(), log_dir=tmp_path).search("q")
    (path,) = tmp_path.glob("*.jsonl")
    assert [row["type"] for row in read_log(path)][-1] == "search_done"
    assert summarize(path)["cost_usd_total"] == pytest.approx(resp.cost_usd)


async def test_rejects_batch_queries_and_mixed_constructor_args():
    client = BackstopTelem(telem=FakeAsyncTelem())
    with pytest.raises(TypeError, match="batch"):
        await client.search(["a", "b"])
    with pytest.raises(TypeError):
        BackstopTelem(telem=FakeAsyncTelem(), base_url="https://x")


def test_default_constructor_builds_async_telem():
    client = BackstopTelem(api_key="test-key")
    assert isinstance(client.telem, AsyncTelem)


def test_public_import_alias():
    import backstop

    assert backstop.BackstopTelem is BackstopTelem and backstop.__version__ == "0.1.0"
