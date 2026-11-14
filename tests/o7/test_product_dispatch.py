from datetime import date
import sys
from types import ModuleType
from unittest.mock import AsyncMock

import pytest

from so import checker, config, decision_checker, second_opinion
from so.contracts import CheckerVerdict, PageText, Result, RunContext


@pytest.fixture
def ctx():
    return RunContext(run_id="dispatch-unit", question_id="unit", setup="second_opinion",
                      today=date(2026, 11, 14))


@pytest.mark.parametrize("backend", ["llm", "decision", "cascade"])
async def test_product_dispatch_preserves_arguments_and_verdict(backend, ctx, monkeypatch):
    monkeypatch.setattr(config, "CHECKER_BACKEND", backend)
    results = [Result(rank=1, title="Evidence", url="https://example.org/evidence",
                      snippet="The source states the answer.", engine="tavily")]
    verdict = CheckerVerdict(verdict="HIT", confidence=.9, why="Answer in source",
                             evidence_rank=1, best_url=results[0].url)
    original_bytes = verdict.model_dump_json().encode()
    calls = {name: AsyncMock(return_value=verdict) for name in ("llm", "decision", "cascade")}
    monkeypatch.setattr(checker, "check", calls["llm"])
    monkeypatch.setattr(decision_checker, "check", calls["decision"])
    monkeypatch.setattr(decision_checker, "cascade_check", calls["cascade"])

    actual = await second_opinion._check("original query", "original goal", results, ctx)

    calls[backend].assert_awaited_once_with("original query", "original goal", results, ctx.today, ctx=ctx)
    assert calls[backend].call_args.args[2] is results
    assert actual is verdict
    assert actual.model_dump_json().encode() == original_bytes
    for name, call in calls.items():
        if name != backend:
            call.assert_not_called()


async def test_default_path_preserves_adapter_without_context(ctx, monkeypatch):
    monkeypatch.setattr(config, "CHECKER_BACKEND", "llm")
    verdict = CheckerVerdict(verdict="DEAD", confidence=.8, why="No evidence")
    calls = []

    async def adapter(query, goal, results, today):
        calls.append((query, goal, results, today))
        return verdict

    module = ModuleType("so.checker")
    module.check = adapter
    monkeypatch.setitem(sys.modules, "so.checker", module)
    results = []
    assert await second_opinion._check("query", "goal", results, ctx) is verdict
    assert calls == [("query", "goal", results, ctx.today)]
    assert calls[0][2] is results


@pytest.mark.parametrize("backend", ["llm", "decision", "cascade"])
async def test_page_checks_keep_llm_for_every_backend(backend, ctx, monkeypatch):
    monkeypatch.setattr(config, "CHECKER_BACKEND", backend)
    page = PageText(url="https://example.org/evidence", ok=True, text="Full page evidence")
    verdict = CheckerVerdict(verdict="HIT", confidence=.9, why="Answer in page")
    page_check = AsyncMock(return_value=verdict)
    dispatch = AsyncMock()
    monkeypatch.setattr(checker, "check_page", page_check)
    monkeypatch.setattr(decision_checker, "configured_check", dispatch)
    actual = await second_opinion._check_page("query", "goal", page, ctx)
    page_check.assert_awaited_once_with("query", "goal", page, ctx.today, ctx=ctx)
    assert actual is verdict
    dispatch.assert_not_called()
