"""Operational verification failures retain evidence and every known paid cost."""

from __future__ import annotations

import asyncio
import json
from datetime import date

import pytest

from so import config, llm, second_opinion, tavily_client, telem_client
from so.contracts import EngineRun, LLMResponse, PageText, RunContext
from so.eventlog import EventLog, read_log, summarize
from so.second_opinion import SearchLimits, search
from tests.o2.fakes import source


def response(verdict: str = "HIT", *, stop: str | None = None, malformed: bool = False) -> LLMResponse:
    text = '{"verdict":' if malformed else json.dumps({
        "verdict": verdict, "confidence": 0.9, "why": "Evidence", "rank": 1, "rewrite": None,
    })
    return LLMResponse(text=text, role="checker", model="unit-checker", cost_usd=0.02,
                       latency_ms=20, tokens_in=100, tokens_out=30, stop_reason=stop)


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    log = EventLog("second_opinion-operations", "operations", "second_opinion", log_dir=tmp_path)

    async def engines(query, engines, **kwargs):
        return {engines[0]: EngineRun(engine=engines[0], status="ok", results=[source()],
                                     cost_usd=0.005, latency_ms=100)}

    async def extract(url, **kwargs):
        return PageText(url=url, ok=True, text="Page evidence", cost_usd=0.002, latency_ms=10)

    monkeypatch.setattr(telem_client, "search_engines", engines)
    monkeypatch.setattr(tavily_client, "tavily_extract", extract)
    return RunContext(run_id=log.run_id, question_id="operations", setup="second_opinion",
                      session="unit-session", log=log, today=date(2026, 11, 14))


@pytest.fixture
def limits():
    return SearchLimits.from_config(max_cost_usd=1, timeout_s=10, checker_timeout_s=1)


def script(monkeypatch, *steps):
    queue = iter(steps)

    async def complete(*args, **kwargs):
        step = next(queue)
        if isinstance(step, BaseException):
            raise step
        return step

    monkeypatch.setattr(llm, "complete", complete)


@pytest.mark.parametrize("stop", ["max_tokens", "length"])
@pytest.mark.parametrize("page", [False, True])
async def test_truncation_is_unchecked_and_never_spends_a_rescue(stop, page, ctx, limits, monkeypatch):
    steps = [response(stop=stop), response(stop=stop)]
    if page:
        steps.insert(0, response("SHALLOW"))
    script(monkeypatch, *steps)

    outcome = await search("query", "goal", ctx, limits=limits)

    assert outcome.status == "UNCHECKED" and outcome.calls == 1
    assert outcome.fetches == int(page)
    assert len(outcome.results) == 1
    done = outcome.trace[-1]["data"]
    assert done["rescues_used"] == int(page)
    assert done["verification_failures"] == 1
    assert done["verification_failure_counts"] == {"truncated": 1}
    assert done["cost_usd_failed_checks"] == pytest.approx(0.04)
    assert outcome.cost_usd == pytest.approx(0.067 if page else 0.045)
    assert [event["data"]["verdict"] for event in outcome.trace if event["type"] == "check"] == (
        ["SHALLOW"] if page else []
    )
    assert summarize(ctx.log.path)["cost_usd_total"] == pytest.approx(outcome.cost_usd)


async def test_successful_retry_does_not_double_count_completed_responses(ctx, limits, monkeypatch):
    script(monkeypatch, response(malformed=True), response())
    outcome = await search("query", "goal", ctx, limits=limits)
    assert outcome.status == "HIT" and outcome.calls == 1
    assert outcome.cost_usd == pytest.approx(0.045)
    usage = [event for event in outcome.trace if event["type"] == "llm"]
    assert len(usage) == 2
    assert outcome.trace[-1]["data"]["verification_failures"] == 0
    assert summarize(ctx.log.path)["cost_usd_total"] == pytest.approx(0.045)


@pytest.mark.parametrize(("failure", "kind"), [(TimeoutError("request timed out"), "timeout"),
                                              (RuntimeError("provider unavailable"), "error")])
async def test_paid_retry_preceding_transport_failure_stays_in_totals(failure, kind, ctx, limits, monkeypatch):
    script(monkeypatch, response(malformed=True), failure)
    outcome = await search("query", "goal", ctx, limits=limits)
    assert outcome.status == "UNCHECKED"
    assert outcome.cost_usd == pytest.approx(0.025)
    done = outcome.trace[-1]["data"]
    assert done["verification_failure_counts"] == {kind: 1}
    assert done["cost_usd_failed_checks"] == pytest.approx(0.02)
    assert done["rescues_used"] == 0
    assert summarize(ctx.log.path)["cost_usd_total"] == pytest.approx(0.025)


@pytest.mark.parametrize("cancel", [False, True])
async def test_usage_is_durable_before_timeout_or_cancellation(cancel, ctx, limits, monkeypatch):
    waiting = asyncio.Event()
    calls = 0

    async def complete(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return response(malformed=True)
        waiting.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(llm, "complete", complete)
    limits = limits.with_overrides(checker_timeout_s=1 if cancel else 0.02)
    task = asyncio.create_task(search("query", "goal", ctx, limits=limits))
    await asyncio.wait_for(waiting.wait(), 1)
    events = read_log(ctx.log.path)
    assert [event["type"] for event in events] == ["search", "llm"]
    assert summarize(ctx.log.path)["cost_usd_total"] == pytest.approx(0.025)
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        outcome = await task
        assert outcome.status == "UNCHECKED" and outcome.cost_usd == pytest.approx(0.025)
    events = read_log(ctx.log.path)
    assert len([event for event in events if event["type"] == "llm"]) == 1
    done = events[-1]["data"]
    assert events[-1]["type"] == "search_done"
    assert done["cost_usd"] == pytest.approx(0.025)
    assert done["cost_usd_failed_checks"] == pytest.approx(0.02)
    assert done["verification_failure_counts"] == {"cancelled" if cancel else "timeout": 1}


async def test_budget_stop_keeps_preceding_paid_attempt(ctx, limits, monkeypatch):
    script(monkeypatch, response(malformed=True), config.BudgetExceeded("budget exhausted"))
    with pytest.raises(config.BudgetExceeded):
        await search("query", "goal", ctx, limits=limits)
    events = read_log(ctx.log.path)
    assert events[-1]["type"] == "search_done"
    assert events[-1]["data"]["cost_usd"] == pytest.approx(0.025)
    assert events[-1]["data"]["verification_failure_counts"] == {"budget": 1}


async def test_completed_engine_cost_is_logged_before_result_processing(ctx, limits, monkeypatch):
    def fail(*args):
        raise RuntimeError("result processing failed")

    monkeypatch.setattr(second_opinion, "_merge", fail)
    with pytest.raises(RuntimeError, match="result processing failed"):
        await search("query", "goal", ctx, limits=limits)
    events = read_log(ctx.log.path)
    assert [event["type"] for event in events] == ["search", "search_done"]
    assert events[-1]["data"]["cost_usd"] == pytest.approx(0.005)
    assert summarize(ctx.log.path)["cost_usd_total"] == pytest.approx(0.005)
