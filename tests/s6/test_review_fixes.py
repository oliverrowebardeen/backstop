"""Review fixes: hard timeout (#1), injected page reader (#7), billed cancelled checks (#8),
no checker verdict on a failed engine (#9). Each test fails on the code before its fix."""

from __future__ import annotations

import asyncio
import sys
from datetime import date
from time import perf_counter
from types import ModuleType
from typing import Any

import httpx
import pytest
from telem import FetchResponse, FetchResult

from backstop import BackstopTelem
from so.contracts import CheckerVerdict, EngineRun, PageText, RunContext
from so.second_opinion import SearchLimits, search
from tests.s6.fakes import CheckedVerdict, FakeLog, Fakes, source, verdict
from tests.s6.test_client import FakeAsyncTelem


@pytest.fixture
def ctx():
    return RunContext(run_id="second_opinion-review", question_id="review", setup="second_opinion",
                      session="session-review", log=FakeLog(), today=date(2026, 11, 14))


def _done(outcome):
    return next(e["data"] for e in reversed(outcome.trace) if e["type"] == "search_done")


def _types(outcome):
    return [e["type"] for e in outcome.trace]


# 1: timeout_s cancels in-flight work -------------------------------------------------------

async def test_hung_rescue_engine_is_cut_at_timeout(ctx, monkeypatch):
    Fakes([verdict("DEAD", rewrite="q2"), verdict("HIT")], runs=[
        EngineRun(engine="tavily", status="ok", results=[source()], cost_usd=0.005), 5.0,
    ]).install(monkeypatch)
    started = perf_counter()
    outcome = await search("q", "g", ctx, limits=SearchLimits.from_config(timeout_s=0.3))
    assert perf_counter() - started < 1.5
    assert outcome.status == "NOT_FOUND"
    assert _done(outcome)["stopped_by"] == "timeout" and "timeout" in outcome.note
    assert [r.url for r in outcome.results] == [source().url]
    cancelled = [e["data"] for e in outcome.trace if e["type"] == "search"][-1]
    assert cancelled["engines"] == ["exa"] and cancelled["status"] == "error" and "timeout" in cancelled["error"]
    assert outcome.calls == 2 and ctx.log.events == outcome.trace


async def test_hung_first_engine_returns_not_found_without_raising(ctx, monkeypatch):
    Fakes([], runs=[5.0]).install(monkeypatch)
    started = perf_counter()
    outcome = await search("q", "g", ctx, limits=SearchLimits.from_config(timeout_s=0.2))
    assert perf_counter() - started < 1.5
    assert outcome.status == "NOT_FOUND" and outcome.results == [] and _done(outcome)["stopped_by"] == "timeout"


async def test_slow_check_within_checker_limit_is_cut_by_search_timeout(ctx, monkeypatch):
    Fakes([5.0, verdict("HIT")]).install(monkeypatch)
    started = perf_counter()
    outcome = await search("q", "g", ctx, limits=SearchLimits.from_config(timeout_s=0.2, checker_timeout_s=10))
    assert perf_counter() - started < 1.5
    assert outcome.status == "UNCHECKED" and outcome.results
    assert _done(outcome)["stopped_by"] == "timeout" and "check_skipped" in _done(outcome)


async def test_hung_shallow_page_read_is_cut_at_timeout(ctx, monkeypatch):
    url = source().url
    Fakes([verdict("SHALLOW", best_url=url)], pages=[5.0, PageText(url=url, ok=True, text="late")]).install(monkeypatch)
    started = perf_counter()
    outcome = await search("q", "g", ctx, limits=SearchLimits.from_config(timeout_s=0.3))
    assert perf_counter() - started < 1.5
    assert outcome.status == "NOT_FOUND" and _done(outcome)["stopped_by"] == "timeout"


async def test_fast_search_is_unaffected_by_timeout(ctx, monkeypatch):
    Fakes([verdict("HIT", evidence_rank=1)]).install(monkeypatch)
    outcome = await search("q", "g", ctx, limits=SearchLimits.from_config(timeout_s=5))
    assert outcome.status == "HIT" and "stopped_by" not in _done(outcome)


# 9: a failed engine gets no checker verdict ----------------------------------------------------

async def test_failed_engine_produces_no_check_event(ctx, monkeypatch):
    fake = Fakes([verdict("HIT", evidence_rank=1)], runs=[EngineRun(engine="tavily", status="error")])
    fake.install(monkeypatch)
    outcome = await search("q", "g", ctx)
    assert outcome.status == "HIT" and outcome.engines_called == ["tavily", "exa"]
    assert len(fake.checks) == 1 and fake.checks[0]["results"]
    assert _types(outcome) == ["search", "rescue", "search", "llm", "check", "search_done"]
    failed = outcome.trace[0]["data"]
    assert failed["status"] == "error" and failed["error"]
    rescue = outcome.trace[1]["data"]
    assert rescue["counted"] is False and rescue["engine_error"] == failed["error"]


# 7: the Telem page-read fallback uses BackstopTelem's own client ---------------------------------

class FetchingTelem(FakeAsyncTelem):
    def __init__(self) -> None:
        super().__init__()
        self.fetch_calls: list[dict[str, Any]] = []

    async def fetch(self, urls, **kwargs) -> FetchResponse:
        self.fetch_calls.append({"urls": list(urls), **kwargs})
        return FetchResponse(results=[FetchResult(url=urls[0], status="success", content="The answer is $50 billion.")],
                             session_id=kwargs.get("session") or "sess-1", interaction_id="i", status="completed")


def install_page_checker(monkeypatch, steps: list[CheckerVerdict]) -> list[Any]:
    seen: list[Any] = []

    async def check(query, goal, results_or_page, today):
        seen.append(results_or_page)
        return CheckedVerdict(**steps.pop(0).model_dump())

    module = ModuleType("so.checker")
    module.check = check
    module.check_page = check
    monkeypatch.setitem(sys.modules, "so.checker", module)
    return seen


async def test_client_page_fallback_uses_injected_telem_client(monkeypatch):
    import so.tavily_client
    import so.telem_client

    async def extract_fails(url, *, goal=None):
        raise RuntimeError("extract down")

    async def env_fetch(*args, **kwargs):
        raise AssertionError("env-credential fetch_page must not be used by BackstopTelem")

    monkeypatch.setattr(so.tavily_client, "tavily_extract", extract_fails)
    monkeypatch.setattr(so.telem_client, "fetch_page", env_fetch)
    install_page_checker(monkeypatch, [verdict("SHALLOW", best_url="https://t1.example"), verdict("HIT")])
    fake = FetchingTelem()
    resp = await BackstopTelem(telem=fake).search("q", goal="g")
    assert resp.status == "HIT" and resp.fetches == 2
    assert fake.fetch_calls and fake.fetch_calls[0]["urls"] == ["https://t1.example"]
    assert fake.fetch_calls[0]["session"] == "sess-1"
    vias = [e["data"]["via"] for e in resp.trace if e["type"] == "fetch"]
    assert vias == ["tavily", "telem"]


# 8: a checker call cancelled after sending is billed at its estimate ----------------------------

async def test_cancelled_checker_call_is_settled_and_logged(ctx, monkeypatch):
    from so import llm

    cfg = llm.config
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(cfg, "SO_CACHE", "off")
    monkeypatch.setattr(cfg, "LLM_PROVIDER", "anthropic")
    monkeypatch.setattr(cfg, "CHECKER_PROVIDER_EXPLICIT", False, raising=False)
    sent = asyncio.Event()

    async def hang(self, *args, **kwargs):
        sent.set()
        await asyncio.sleep(10)

    monkeypatch.setattr(httpx.AsyncClient, "post", hang)

    async def check(query, goal, results, today):
        await llm.complete("checker", "system", [{"role": "user", "content": "results"}])
        raise AssertionError("unreachable")

    module = ModuleType("so.checker")
    module.check = check
    module.check_page = check
    Fakes([]).install(monkeypatch)
    monkeypatch.setitem(sys.modules, "so.checker", module)
    spent_before = cfg.LLM_BUDGET.spent
    outcome = await search("q", "g", ctx, limits=SearchLimits.from_config(checker_timeout_s=0.1))
    assert sent.is_set() and outcome.status == "UNCHECKED"
    usage = [e["data"] for e in outcome.trace if e["type"] == "llm"]
    assert len(usage) == 1 and usage[0]["estimated"] is True and usage[0]["role"] == "checker"
    assert usage[0]["cost_usd"] > 0
    assert cfg.LLM_BUDGET.spent - spent_before == pytest.approx(usage[0]["cost_usd"])
    assert cfg.LLM_BUDGET.reserved == pytest.approx(0, abs=1e-12)
    assert outcome.cost_usd == pytest.approx(0.005 + usage[0]["cost_usd"])


async def test_llm_call_cancelled_before_sending_costs_nothing(monkeypatch):
    from so import llm

    cfg = llm.config
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(cfg, "SO_CACHE", "off")
    monkeypatch.setattr(cfg, "LLM_PROVIDER", "anthropic")
    monkeypatch.setattr(cfg, "CHECKER_PROVIDER_EXPLICIT", False, raising=False)
    real_semaphore = cfg.semaphore

    def semaphore(name, limit):
        if name == "llm":
            class Never:
                async def __aenter__(self):
                    await asyncio.sleep(10)

                async def __aexit__(self, *exc):
                    return None
            return Never()
        return real_semaphore(name, limit)

    monkeypatch.setattr(cfg, "semaphore", semaphore)
    spent_before = cfg.LLM_BUDGET.spent
    with llm.capture_cancelled_usage() as sink:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(llm.complete("checker", "s", [{"role": "user", "content": "x"}]), 0.05)
    assert sink == [] and cfg.LLM_BUDGET.spent == spent_before
