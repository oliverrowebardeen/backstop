import json
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from so import checker, config, decision_checker as dc, llm
from so.checker_eval import Case, SplitRefused
from so.contracts import LLMResponse, MatrixRow, Question, Result, RunContext


@pytest.fixture(autouse=True)
def isolation(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(config, "SO_CACHE", "readwrite")
    monkeypatch.setattr(config, "LLM_BUDGET", config.Budget(3))
    monkeypatch.setattr(config, "DECISION_BUDGET", config.Budget(3))
    monkeypatch.setattr(config, "API_RETRY_BACKOFF_S", 0)
    monkeypatch.setenv("OPENAI_API_KEY", "test-placeholder")


@pytest.fixture
def results():
    return [Result(rank=i, title=f"Page {i}", url=f"https://example.org/{i}",
                   snippet="Evidence to evaluate", engine="tavily") for i in range(1, 6)]


def response(choice="HIT_2", confidence=.85, *, tokens=1000):
    return {"model": config.DECISION_MODEL, "answers": [{"name": "checker", "type": "choice",
        "choice": choice, "confidence": confidence, "probabilities": []}],
        "usage": {"input_tokens": tokens, "output_tokens": 0,
                  "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0}}}


def transport(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(handler)))


@pytest.mark.parametrize("choice", [f"{v}_{i}" for v in ("HIT", "SHALLOW") for i in range(1, 6)]
                         + ["STALE", "WRONG_ENTITY", "DEAD"])
async def test_every_option_maps_to_contract(choice, results, monkeypatch):
    sent = []

    def handle(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=response(choice))

    transport(monkeypatch, handle)
    verdict = await dc.check("query", "goal", results, date(2026, 1, 1))
    ranked = choice.startswith(("HIT_", "SHALLOW_"))
    expected = choice.rsplit("_", 1)[0] if ranked else choice
    assert verdict.verdict == expected
    assert verdict.rewrite is None and verdict.confidence == .85
    if ranked:
        rank = int(choice.rsplit("_", 1)[1])
        assert verdict.evidence_rank == rank
        assert verdict.best_url == results[rank - 1].url
    else:
        assert verdict.evidence_rank is None and verdict.best_url is None
    assert set(verdict.model_dump()) == set(dc.CheckerVerdict.model_fields)
    assert len(sent[0]["questions"][0]["choices"]) == 13
    assert verdict.cost_usd == pytest.approx(.0001)
    assert config.DECISION_BUDGET.spent == verdict.cost_usd
    assert config.LLM_BUDGET.spent == verdict.cost_usd


async def test_cache_preserves_cost_latency_and_avoids_new_spend(results, monkeypatch):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json=response())

    transport(monkeypatch, handle)
    first = await dc.check("query", "goal", results)
    second = await dc.check("query", "goal", results)
    assert len(calls) == 1 and second.cached
    assert second.cost_usd == first.cost_usd
    assert second.latency_ms == first.latency_ms
    assert config.LLM_BUDGET.spent == first.cost_usd


@pytest.mark.parametrize("bad", [response("HIT_99"), response(confidence=2), response(confidence=True),
    {**response(), "answers": []}, {**response(), "usage": {}},
    {**response(), "answers": [{"type": "refusal", "name": "checker"}]}])
async def test_malformed_or_refusal_is_explicit_and_charged(bad, results, monkeypatch):
    transport(monkeypatch, lambda request: httpx.Response(200, json=bad))
    fallback = AsyncMock()
    monkeypatch.setattr(checker, "check", fallback)
    with pytest.raises(llm.DecisionCallError) as caught:
        await dc.check("query", "goal", results)
    assert caught.value.kind in {"malformed", "refusal"}
    assert caught.value.usage.cost_usd > 0
    assert config.DECISION_BUDGET.spent > 0
    assert config.LLM_BUDGET.reserved == 0
    assert config.DECISION_BUDGET.reserved == 0
    fallback.assert_not_called()


async def test_timeout_falls_back_with_combined_usage(results, monkeypatch):
    def handle(request):
        raise httpx.ReadTimeout("timed out", request=request)

    transport(monkeypatch, handle)
    fallback = AsyncMock(return_value=checker.CheckedVerdict(verdict="DEAD", confidence=.8,
        why="No evidence", cost_usd=.001, latency_ms=200, model="fallback-model", cached=False))
    monkeypatch.setattr(checker, "check", fallback)
    verdict = await dc.check("query", "goal", results)
    assert verdict.fallback and verdict.failure == "timeout"
    assert verdict.verdict == "DEAD" and verdict.cost_usd > .001 and verdict.latency_ms >= 200
    assert config.DECISION_BUDGET.spent > 0
    fallback.assert_awaited_once()


async def test_budget_exhaustion_never_falls_back(results, monkeypatch):
    monkeypatch.setattr(config, "DECISION_BUDGET", config.Budget(0))
    fallback = AsyncMock()
    monkeypatch.setattr(checker, "check", fallback)
    with pytest.raises(config.BudgetExceeded):
        await dc.check("query", "goal", results)
    assert config.LLM_BUDGET.reserved == 0
    fallback.assert_not_called()


async def test_retry_http_429_and_partial_server_failure(results, monkeypatch):
    statuses = iter([429, 503, 200])
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(next(statuses), json=response())

    transport(monkeypatch, handle)
    verdict = await dc.check("query", "goal", results)
    assert verdict.verdict == "HIT" and len(calls) == 3
    assert config.LLM_BUDGET.spent == verdict.cost_usd


async def test_two_step_option_limit(results, monkeypatch):
    monkeypatch.setattr(config, "DECISION_MAX_CHOICES", 5)
    calls = []

    def handle(request):
        body = json.loads(request.content)
        calls.append(body)
        assert len(body["questions"][0]["choices"]) == 5
        return httpx.Response(200, json=response("HIT" if len(calls) == 1 else "3",
                                                .9 if len(calls) == 1 else .8))

    transport(monkeypatch, handle)
    verdict = await dc.check("query", "goal", results)
    assert verdict.verdict == "HIT" and verdict.evidence_rank == 3
    assert verdict.confidence == .8 and verdict.decision_calls == 2
    assert verdict.cost_usd == pytest.approx(.0002)


def test_definitions_reused_verbatim():
    instructions, definitions = dc._instructions()
    original = config.DECISION_PROMPT_PATH.read_text()
    for name, definition in definitions.items():
        assert f"- {name}: {definition}" in original
    assert "## Output" not in instructions and "## Examples" not in instructions


async def test_empty_results_make_no_call(monkeypatch):
    decide = AsyncMock()
    monkeypatch.setattr(llm, "decide", decide)
    verdict = await dc.check("query", "goal", [])
    assert verdict.verdict == "DEAD" and verdict.cost_usd == 0
    decide.assert_not_called()


async def test_configured_dispatcher_keeps_llm_default(results, monkeypatch):
    baseline, decision = AsyncMock(), AsyncMock()
    monkeypatch.setattr(checker, "check", baseline)
    monkeypatch.setattr(dc, "check", decision)
    monkeypatch.setattr(config, "CHECKER_BACKEND", "llm")
    await dc.configured_check("query", "goal", results)
    baseline.assert_awaited_once()
    decision.assert_not_called()
    monkeypatch.setattr(config, "CHECKER_BACKEND", "decision")
    await dc.configured_check("query", "goal", results)
    decision.assert_awaited_once()


def test_cache_rates_and_free_output():
    raw = response(tokens=1000)
    raw["usage"].update(output_tokens=100, input_tokens_details={"cached_tokens": 300, "cache_write_tokens": 200})
    usage = llm._decision_usage(raw, 12, llm._decision_rates())
    assert usage.cost_usd == pytest.approx((500 * .1 + 300 * .01 + 200 * .125) / 1e6)
    assert usage.tokens_in == 1000 and usage.tokens_out == 100


def observation(*, hit, verdict="HIT", confidence=.8, original_rank=3, category="finance", error=None):
    question = Question(id="unit", question="Question?", gold=["secret answer"], category=category,
                        source="unit", split="learn")
    result = Result(rank=original_rank, title="page", snippet="evidence", url="https://example.org", engine="tavily")
    case = Case(question, "tavily", [result], hit)
    truth = MatrixRow(question_id=question.id, engine="tavily", hit=hit, hit_rank=original_rank if hit else None)
    checked = dc.DecisionVerdict(verdict=verdict, confidence=confidence, evidence_rank=1,
        why="unit", model="unit", latency_ms=100, cost_usd=.0001, cached=True, failure=error)
    return dc.Observation(case, truth, checked, dc._usage(checked), (original_rank,), error)


def test_metrics_use_original_positions_and_cache_timings():
    rows = [observation(hit=True), observation(hit=False, verdict="DEAD")]
    result = dc.summarize(rows)
    assert result["miss_recall"] == 1 and result["false_alarm_rate"] == 0
    assert result["evidence_rank_agreement"] == 1
    assert result["latency_p50_ms"] == 100 and result["latency_p90_ms"] == 100
    assert result["cost_per_check"] == .0001 and result["cached_checks"] == 2
    rows[0].case.results[0].rank = 99
    assert dc.summarize(rows)["evidence_rank_agreement"] == 1


def test_thresholds_do_not_mutate_raw_verdicts():
    rows = [observation(hit=True, confidence=.6), observation(hit=False, confidence=.6)]
    result = dc.summarize(rows, threshold=.7)
    assert result["miss_recall"] == 1 and result["false_alarm_rate"] == 1
    assert all(r.verdict.verdict == "HIT" for r in rows)
    assert dc.summarize(rows, threshold=.6)["false_alarm_rate"] == 0


def test_failures_are_not_scored_as_decisions():
    rows = [observation(hit=True), observation(hit=False, verdict="DEAD", error="timeout")]
    report = dc.summarize(rows)
    assert report["attempted"] == 2 and report["n_searches"] == 1
    assert report["failed"] == 1 and report["true_misses"] == 0
    assert report["cost_usd"] == .0002


@pytest.mark.parametrize("split", ["test", "trap", "traps"])
async def test_rejects_other_splits_before_reading(split, monkeypatch):
    load = AsyncMock()
    monkeypatch.setattr(dc, "learn_questions", load)
    with pytest.raises(SplitRefused):
        await dc.compare(split, engines=["tavily"], today="2026-01-01", matrix=Path("missing"), results_path=Path("missing"))
    load.assert_not_called()


def test_recommendation_requires_finance_and_latency():
    baseline = dc.grouped([observation(hit=True), observation(hit=False, verdict="DEAD")])
    assert dc.recommend(baseline, baseline)["switch"]
    different = json.loads(json.dumps(baseline))
    different["finance"]["false_alarm_rate"] = .04
    different["overall"]["latency_p50_ms"] = 500
    recommendation = dc.recommend(baseline, different)
    assert not recommendation["switch"]
    assert len(recommendation["reasons"]) == 2


async def test_comparison_filters_and_never_sends_gold(tmp_path, monkeypatch):
    row = observation(hit=True)
    monkeypatch.setattr(dc, "learn_questions", lambda: [row.case.question])
    matrix, results_path = tmp_path / "matrix.jsonl", tmp_path / "results.jsonl"
    matrix.write_text(row.truth.model_dump_json() + "\n")
    contaminated = row.case.results[0].model_copy(update={"rank": 1, "url": "https://huggingface.co/datasets/simpleqa"})
    results_path.write_text(json.dumps({"question_id": "unit", "engine": "tavily",
        "results": [contaminated.model_dump(), row.case.results[0].model_dump()]}) + "\n")

    async def check(query, goal, results, today):
        assert "secret answer" not in query + goal + str(results)
        assert len(results) == 1 and results[0].rank == 3
        return row.verdict

    monkeypatch.setattr(checker, "check", check)
    monkeypatch.setattr(dc, "check", check)
    result = await dc.compare("learn", engines=["tavily", "ceramic"], today="2026-01-01",
                              matrix=matrix, results_path=results_path)
    assert result["rows"] == 1 and result["engines"] == ["tavily"]
    assert result["decision"]["overall"]["contaminated_results_dropped"] == 1


async def test_failed_response_is_cached_without_repeated_spend(results, monkeypatch):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json=response("unknown"))

    transport(monkeypatch, handle)
    for cached in (False, True):
        with pytest.raises(llm.DecisionCallError) as caught:
            await dc.check("query", "goal", results)
        assert caught.value.usage.cached == cached
        assert caught.value.kind == "malformed"
    assert len(calls) == 1 and config.LLM_BUDGET.spent == pytest.approx(.0001)


async def test_non_json_is_charged_and_explicit(results, monkeypatch):
    transport(monkeypatch, lambda request: httpx.Response(200, text="invalid"))
    with pytest.raises(llm.DecisionCallError, match="validation") as caught:
        await dc.check("query", "goal", results)
    assert caught.value.kind == "malformed" and caught.value.usage.cost_usd > 0
    assert config.LLM_BUDGET.spent == caught.value.usage.cost_usd


def test_nested_cache_write_usage_from_live_api():
    raw = response(tokens=1000)
    raw["usage"]["input_tokens_details"] = {"cached_tokens": 100, "cache_write_tokens": 300,
        "cache_write_tokens_details": {"ttl_30m_tokens": 200, "ttl_12h_tokens": 100}}
    usage = llm._decision_usage(raw, 12, llm._decision_rates())
    assert usage.cost_usd == pytest.approx((600 * .1 + 100 * .01 + 200 * .125 + 100 * .25) / 1e6)


async def test_unknown_model_pricing_prevents_call(results, monkeypatch):
    monkeypatch.setattr(config, "DECISION_MODEL", "unpriced")
    with pytest.raises(ValueError, match="verified Decisions rates"):
        await dc.check("query", "goal", results)
    assert config.LLM_BUDGET.spent == 0


async def test_input_limit_prevents_call(results, monkeypatch):
    monkeypatch.setattr(config, "DECISION_MAX_INPUT_BYTES", 1)
    with pytest.raises(ValueError, match="DECISION_MAX_INPUT_BYTES"):
        await dc.check("query", "goal", results)
    assert config.LLM_BUDGET.spent == 0


async def test_second_stage_failure_retains_first_stage_usage(results, monkeypatch):
    monkeypatch.setattr(config, "DECISION_MAX_CHOICES", 5)
    replies = iter([response("HIT"), response("unknown")])
    transport(monkeypatch, lambda request: httpx.Response(200, json=next(replies)))
    with pytest.raises(llm.DecisionCallError) as caught:
        await dc.check("query", "goal", results)
    assert caught.value.usage.cost_usd == pytest.approx(.0002)
    assert caught.value.usage.tokens_in == 2000


async def test_usage_logged_even_on_malformed_response(results, monkeypatch):
    transport(monkeypatch, lambda request: httpx.Response(200, json=response("unknown")))
    events = []

    class Log:
        def emit(self, kind, **data):
            events.append((kind, data))

    ctx = RunContext(run_id="unit", question_id="unit", setup="second_opinion", log=Log())
    with pytest.raises(llm.DecisionCallError):
        await dc.check("query", "goal", results, ctx=ctx)
    assert len(events) == 1 and events[0][0] == "llm"
    assert events[0][1]["cost_usd"] == pytest.approx(.0001)


async def test_checker_provider_override_preserves_agent_provider(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "anthropic")
    monkeypatch.setattr(config, "CHECKER_PROVIDER", "openai")
    monkeypatch.setattr(config, "CHECKER_PROVIDER_EXPLICIT", True)
    monkeypatch.setattr(config, "CHECKER_MODEL", "unit")
    monkeypatch.setitem(config.LLM_PRICE_USD, "unit", {"input": .1, "output": .5})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-placeholder")
    models = []

    def handle(request):
        models.append(json.loads(request.content)["model"])
        if request.url == config.OPENAI_CHAT_URL:
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}],
                                            "usage": {"prompt_tokens": 1, "completion_tokens": 1}})
        assert request.url == config.ANTHROPIC_MESSAGES_URL
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}],
                                        "usage": {"input_tokens": 1, "output_tokens": 1}})

    transport(monkeypatch, handle)
    await llm.complete("checker", "system", [])
    await llm.complete("agent", "system", [])
    await llm.complete("judge", "system", [])
    assert models == ["unit", config.AGENT_MODEL, config.JUDGE_MODEL]


async def test_comparison_requires_contamination_filter(monkeypatch):
    monkeypatch.setenv("SO_CONTAM_OFF", "1")
    with pytest.raises(ValueError, match="contamination filter"):
        await dc.compare("learn", engines=["tavily"], today="2026-01-01",
                         matrix=Path("missing"), results_path=Path("missing"))


async def test_comparison_refuses_missing_truth(monkeypatch, tmp_path):
    monkeypatch.setattr(dc, "learn_questions", lambda: [observation(hit=True).case.question])
    with pytest.raises(ValueError, match="lack matrix truth"):
        await dc.compare("learn", engines=["tavily"], today="2026-01-01",
                         matrix=tmp_path / "matrix", results_path=tmp_path / "results")


async def test_timeout_cache_preserves_usage_and_skips_another_api_call(results, monkeypatch):
    calls = []

    def handle(request):
        calls.append(request)
        raise httpx.ReadTimeout("timeout", request=request)

    transport(monkeypatch, handle)
    monkeypatch.setattr(checker, "check", AsyncMock(return_value=checker.CheckedVerdict(
        verdict="DEAD", confidence=.8, why="No evidence", model="fallback", cached=True)))
    first = await dc.check("query", "goal", results)
    second = await dc.check("query", "goal", results)
    assert len(calls) == 1 and second.cached and second.fallback
    assert first.cost_usd == second.cost_usd == config.DECISION_BUDGET.spent


async def test_failed_cache_write_still_settles_both_budgets(results, monkeypatch):
    transport(monkeypatch, lambda request: httpx.Response(200, json=response()))

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(config, "write_cache", fail)
    with pytest.raises(OSError, match="disk full"):
        await dc.check("query", "goal", results)
    assert config.LLM_BUDGET.spent == config.DECISION_BUDGET.spent == pytest.approx(.0001)
    assert config.LLM_BUDGET.reserved == config.DECISION_BUDGET.reserved == 0
