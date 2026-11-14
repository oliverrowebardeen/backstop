import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from so import checker, config, decision_checker as dc, llm
from so.checker_eval import Case, SplitRefused
from so.contracts import LLMResponse, MatrixRow, Question, Result, RunContext

pytestmark = pytest.mark.checker_backend


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CHECKER_BACKEND", "cascade")
    monkeypatch.setattr(config, "CHECKER_CASCADE_THRESHOLD", .7)
    monkeypatch.setattr(config, "LLM_BUDGET", config.Budget(0))
    monkeypatch.setattr(config, "DECISION_BUDGET", config.Budget(0))
    monkeypatch.setattr(config, "CASCADE_CALIBRATION_PATH", tmp_path / "calibration.json")
    monkeypatch.setattr(config, "CASCADE_EVAL_REPORT_PATH", tmp_path / "report.json")


def decision(verdict="HIT", confidence=.6):
    return dc.DecisionVerdict(verdict=verdict, confidence=confidence, why="Decision evidence",
        evidence_rank=1, best_url="https://example.org/decision", model="decision-model",
        latency_ms=200, cost_usd=.0002, tokens_in=2000, cached=True, decision_calls=1)


def baseline(verdict="HIT"):
    return checker.CheckedVerdict(verdict=verdict, confidence=.9, why="LLM evidence",
        evidence_rank=2, best_url="https://example.org/llm", model="llm-model",
        latency_ms=1000, cost_usd=.0005, tokens_in=3000, tokens_out=100, cached=True)


def pair(qid="unit", *, hit=False, dec="HIT", confidence=.6, final="DEAD", engine="tavily", category="fact"):
    q = Question(id=qid, question="What is the value?", gold=["hidden gold"],
                 split="learn", category=category, source="unit")
    results = [Result(rank=i, title="Page", url=f"https://example.org/{i}", snippet="Evidence", engine=engine)
               for i in (1, 2)]
    case = Case(q, engine, results, hit)
    truth = MatrixRow(question_id=qid, engine=engine, hit=hit, hit_rank=1 if hit else None)
    left, right = baseline(final), decision(dec, confidence)
    return dc.CheckerPair(dc.Observation(case, truth, left, dc._usage(left), (1, 2)),
                          dc.Observation(case, truth, right, dc._usage(right), (1, 2)))


@pytest.mark.parametrize("verdict", ["SHALLOW", "STALE", "WRONG_ENTITY", "DEAD"])
async def test_non_hit_never_escalates(verdict, monkeypatch):
    first = decision(verdict, .1)
    monkeypatch.setattr(dc, "check", AsyncMock(return_value=first))
    fallback = AsyncMock()
    monkeypatch.setattr(checker, "check", fallback)
    result = await dc.cascade_check("query", "goal", [])
    assert result.verdict == verdict and not result.escalated
    assert result.best_url == first.best_url and result.evidence_rank == first.evidence_rank
    assert result.cost_usd == .0002 and result.latency_ms == 200
    fallback.assert_not_called()


@pytest.mark.parametrize("confidence", [.7, .9])
async def test_threshold_boundary_accepts_hit(confidence, monkeypatch):
    monkeypatch.setattr(dc, "check", AsyncMock(return_value=decision(confidence=confidence)))
    fallback = AsyncMock()
    monkeypatch.setattr(checker, "check", fallback)
    result = await dc.cascade_check("query", "goal", [])
    assert result.verdict == "HIT" and not result.escalated
    fallback.assert_not_called()


@pytest.mark.parametrize("verdict", ["HIT", "SHALLOW", "STALE", "WRONG_ENTITY", "DEAD"])
async def test_low_confidence_uses_llm_verdict_and_decision_pointers(verdict, monkeypatch):
    monkeypatch.setattr(dc, "check", AsyncMock(return_value=decision()))
    fallback = AsyncMock(return_value=baseline(verdict))
    monkeypatch.setattr(checker, "check", fallback)
    result = await dc.cascade_check("query", "goal", [])
    assert result.verdict == verdict and result.escalated and not result.fallback
    assert result.evidence_rank == 1 and result.best_url == "https://example.org/decision"
    assert result.cost_usd == pytest.approx(.0007) and result.latency_ms == 1200
    assert result.tokens_in == 5000 and result.tokens_out == 100
    assert result.confidence == .9 and result.cached
    assert set(result.model_dump()) == set(dc.CheckerVerdict.model_fields)
    fallback.assert_awaited_once()


@pytest.mark.parametrize("kind", ["malformed", "refusal", "transport"])
async def test_decision_failures_fall_back_without_inventing_evidence(kind, monkeypatch):
    usage = LLMResponse(role="checker", model="decision-model", latency_ms=200, cost_usd=.0002)
    monkeypatch.setattr(dc, "check", AsyncMock(side_effect=llm.DecisionCallError("failed", usage=usage, kind=kind)))
    fallback = AsyncMock(return_value=baseline())
    monkeypatch.setattr(checker, "check", fallback)
    result = await dc.cascade_check("query", "goal", [])
    assert result.verdict == "HIT" and result.escalated and result.fallback
    assert result.decision_failure == kind and result.failure is None
    assert result.evidence_rank == 2 and result.best_url == baseline().best_url
    assert result.cost_usd == pytest.approx(.0007) and result.latency_ms == 1200
    fallback.assert_awaited_once()


async def test_timeout_fallback_is_not_called_twice(monkeypatch):
    completed = dc.DecisionVerdict(**baseline().model_dump(), model="llm-model", cost_usd=.0007,
        latency_ms=1200, fallback=True, failure="timeout", decision_calls=1)
    monkeypatch.setattr(dc, "check", AsyncMock(return_value=completed))
    fallback = AsyncMock()
    monkeypatch.setattr(checker, "check", fallback)
    result = await dc.cascade_check("query", "goal", [])
    assert result.decision_failure == "timeout" and result.escalated and result.fallback
    assert result.best_url == completed.best_url and result.evidence_rank == completed.evidence_rank
    assert result.cost_usd == .0007 and result.latency_ms == 1200
    fallback.assert_not_called()


@pytest.mark.parametrize("error", [config.BudgetExceeded("cap"), asyncio.CancelledError()])
async def test_budget_and_cancellation_do_not_fall_back(error, monkeypatch):
    monkeypatch.setattr(dc, "check", AsyncMock(side_effect=error))
    fallback = AsyncMock()
    monkeypatch.setattr(checker, "check", fallback)
    with pytest.raises(type(error)):
        await dc.cascade_check("query", "goal", [])
    fallback.assert_not_called()


async def test_failed_llm_escalation_retains_both_costs(monkeypatch):
    monkeypatch.setattr(dc, "check", AsyncMock(return_value=decision()))
    error = checker.CheckerError("invalid reply", kind="malformed", usage=dc._usage(baseline()))
    monkeypatch.setattr(checker, "check", AsyncMock(side_effect=error))
    with pytest.raises(checker.CheckerError) as caught:
        await dc.cascade_check("query", "goal", [])
    assert caught.value.usage.cost_usd == pytest.approx(.0007)
    assert caught.value.kind == "malformed"


async def test_failed_timeout_fallback_is_not_retried(monkeypatch):
    usage = LLMResponse(role="checker", cost_usd=.0007)
    monkeypatch.setattr(dc, "check", AsyncMock(side_effect=llm.DecisionCallError("both failed", usage=usage, kind="fallback_failed")))
    fallback = AsyncMock()
    monkeypatch.setattr(checker, "check", fallback)
    with pytest.raises(checker.CheckerError) as caught:
        await dc.cascade_check("query", "goal", [])
    assert caught.value.usage.cost_usd == .0007
    fallback.assert_not_called()


async def test_storage_failure_retains_decision_cost(monkeypatch):
    error = llm.DecisionStorageError("disk full", usage=dc._usage(decision()))
    monkeypatch.setattr(dc, "check", AsyncMock(side_effect=error))
    monkeypatch.setattr(checker, "check", AsyncMock(return_value=baseline()))
    result = await dc.cascade_check("query", "goal", [])
    assert result.cost_usd == pytest.approx(.0007) and result.best_url == baseline().best_url


async def test_configured_cascade_dispatch(monkeypatch):
    monkeypatch.setattr(config, "CHECKER_BACKEND", "cascade")
    cascade = AsyncMock(return_value=baseline())
    monkeypatch.setattr(dc, "cascade_check", cascade)
    await dc.configured_check("query", "goal", [])
    cascade.assert_awaited_once()


async def test_simulation_matches_runtime(monkeypatch):
    row = pair(final="SHALLOW")
    monkeypatch.setattr(dc, "check", AsyncMock(return_value=row.decision.verdict))
    monkeypatch.setattr(checker, "check", AsyncMock(return_value=row.baseline.verdict))
    live = await dc.cascade_check("query", "goal", [])
    simulated = dc.simulate_cascade(row, .7)
    assert live.model_dump() == simulated.verdict.model_dump()
    assert live.cost_usd == simulated.usage.cost_usd and live.latency_ms == simulated.usage.latency_ms


def test_split_keeps_question_engines_together_and_is_order_independent():
    rows = [pair(str(i), engine=engine) for i in range(10) for engine in ("tavily", "exa")]
    train, heldout = dc.split_pairs(rows, 14)
    left, right = {p.baseline.case.question.id for p in train}, {p.baseline.case.question.id for p in heldout}
    assert len(left) == len(right) == 5 and not left & right
    assert len(train) == len(heldout) == 10
    reversed_train, _ = dc.split_pairs(list(reversed(rows)), 14)
    assert {p.baseline.case.question.id for p in reversed_train} == left


def test_threshold_selection_constraint_and_tie_break():
    rows = [pair("miss-a", confidence=.65), pair("miss-b", confidence=.75),
            pair("hit", hit=True, confidence=.85, final="HIT")]
    selected = dc.select_threshold(rows, [.6, .7, .8, .9])
    assert selected["threshold"] == .8
    assert selected["candidates"][.8]["misses_flagged"] == 2
    assert selected["candidates"][.8]["escalated"] == 2
    assert selected["candidates"][.9]["escalated"] == 3


def test_no_feasible_threshold_recommends_staying():
    rows = [pair("miss", dec="DEAD"), pair("hit", hit=True, dec="SHALLOW", final="HIT")]
    selected = dc.select_threshold(rows, [.6, .7])
    assert selected["threshold"] is None
    report = dc.recommend_cascade(dc.cascade_groups(rows, None))
    assert not report["switch"]


def test_selection_uses_no_heldout_labels():
    rows = [pair(str(i), hit=i % 2 == 0, final="HIT" if i % 2 == 0 else "DEAD") for i in range(12)]
    train, heldout = dc.split_pairs(rows, 14)
    selected = dc.select_threshold(train, [.6, .7, .8, .9])["threshold"]
    for row in heldout:
        row.baseline.case.truth_hit = not row.baseline.case.truth_hit
    assert dc.select_threshold(train, [.6, .7, .8, .9])["threshold"] == selected


def test_heldout_recall_loss_blocks_recommendation():
    rows = [pair("miss", confidence=.9), pair("hit", hit=True, final="HIT")]
    report = dc.recommend_cascade(dc.cascade_groups(rows, .7))
    assert not report["switch"] and "recall fell" in report["reasons"][0]


def test_heldout_false_alarm_increase_blocks_recommendation():
    rows = [pair("miss", dec="DEAD"), pair("hit", hit=True, dec="SHALLOW", final="HIT")]
    report = dc.recommend_cascade(dc.cascade_groups(rows, .7))
    assert not report["switch"] and "false alarms rose" in report["reasons"][0]


def test_sequential_latency_and_zero_call_rows():
    escalated = dc.simulate_cascade(pair("miss"), .7)
    empty = pair("empty", dec="DEAD").decision
    empty.usage = LLMResponse(role="checker")
    m = dc.cascade_metrics([escalated, empty])
    assert m["latency_mean_ms"] == 600 and m["latency_p90_ms"] == 1200
    assert m["share_escalated"] == .5 and m["cost_per_check"] == pytest.approx(.00035)


@pytest.mark.parametrize("split", ["test", "trap"])
async def test_non_learn_refused_before_loading(split):
    with pytest.raises(SplitRefused):
        await dc.collect_pairs(split, engines=["tavily"], today="2026-01-01", matrix=Path("missing"), results_path=Path("missing"))


async def test_choice_is_saved_before_heldout_scoring(monkeypatch):
    rows = [pair(str(i), hit=i % 2 == 0, final="HIT" if i % 2 == 0 else "DEAD") for i in range(12)]
    monkeypatch.setattr(dc, "collect_pairs", AsyncMock(return_value=rows))
    original = dc.cascade_groups

    def groups(pairs, threshold):
        frozen = json.loads(config.CASCADE_CALIBRATION_PATH.read_text())
        assert frozen["threshold"] == threshold
        return original(pairs, threshold)

    monkeypatch.setattr(dc, "cascade_groups", groups)
    result = await dc.evaluate_cascade("learn", engines=["tavily"], today="2026-01-01", matrix=Path("unused"), results_path=Path("unused"))
    assert result["heldout"]["overall"]["llm"]["n_questions"] == 6
    assert config.CASCADE_EVAL_REPORT_PATH.exists()


def test_cascade_cli_refuses_test_split():
    with pytest.raises(SystemExit) as caught:
        dc.main(["--cascade-eval", "--split", "test"])
    assert caught.value.code == 2


async def test_external_cancellation_reports_reserved_decision_usage(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(config, "LLM_BUDGET", config.Budget(1))
    monkeypatch.setattr(config, "DECISION_BUDGET", config.Budget(1))
    monkeypatch.setenv("OPENAI_API_KEY", "test-placeholder")
    started = asyncio.Event()

    async def response(request):
        started.set()
        await asyncio.Event().wait()

    client = httpx.AsyncClient
    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda **kwargs: client(transport=httpx.MockTransport(response)))
    choices = [llm.DecisionChoice(value=v, description=v) for v in ("yes", "no")]
    with llm.capture_cancelled_usage() as cancelled:
        task = asyncio.create_task(llm.decide("Check evidence", "Evidence", choices))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(cancelled) == 1 and cancelled[0].stop_reason == "cancelled"
    assert cancelled[0].cost_usd == config.LLM_BUDGET.spent == config.DECISION_BUDGET.spent
    assert cancelled[0].cost_usd > 0


async def test_internal_timeout_does_not_duplicate_cancellation_usage(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(config, "LLM_BUDGET", config.Budget(1))
    monkeypatch.setattr(config, "DECISION_BUDGET", config.Budget(1))
    monkeypatch.setattr(config, "DECISION_TIMEOUT_S", .01)
    monkeypatch.setenv("OPENAI_API_KEY", "test-placeholder")

    async def response(request):
        await asyncio.Event().wait()

    client = httpx.AsyncClient
    monkeypatch.setattr(llm.httpx, "AsyncClient", lambda **kwargs: client(transport=httpx.MockTransport(response)))
    choices = [llm.DecisionChoice(value=v, description=v) for v in ("yes", "no")]
    with llm.capture_cancelled_usage() as cancelled:
        with pytest.raises(llm.DecisionCallError) as caught:
            await llm.decide("Check evidence", "Evidence", choices)
    assert caught.value.kind == "timeout" and not cancelled
    assert caught.value.usage.cost_usd == config.LLM_BUDGET.spent


async def test_completed_first_stage_is_logged_before_second_stage_cancellation(monkeypatch):
    monkeypatch.setattr(config, "DECISION_MAX_CHOICES", 5)
    first = llm.DecisionResponse(choice="HIT", confidence=.8, model="decision-model", role="checker", cost_usd=.0002)
    monkeypatch.setattr(llm, "decide", AsyncMock(side_effect=[first, asyncio.CancelledError()]))
    events = []

    class Log:
        def emit(self, kind, **data):
            events.append((kind, data))

    ctx = RunContext(run_id="unit", question_id="unit", setup="second_opinion", log=Log())
    with pytest.raises(asyncio.CancelledError):
        await dc.check("query", "goal", pair().baseline.case.results, ctx=ctx)
    assert len(events) == 1 and events[0][1]["cost_usd"] == .0002
