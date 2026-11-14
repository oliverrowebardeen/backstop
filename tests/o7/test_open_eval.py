import asyncio
from collections import Counter
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from so import config, open_eval as oe
from so.contracts import AgentAnswer, LLMResponse
from so.eventlog import EventLog

pytestmark = [pytest.mark.checker_backend, pytest.mark.first_engine_routing]


def settings(tmp_path):
    return oe.Settings(config.ROOT / "data_open/questions.json", tmp_path, 15, 3, 3, 4096)


def test_questions_are_new_and_frozen():
    seed, rows = oe.load_questions(settings(Path(".")).questions)
    assert seed == 14
    assert Counter(q.split for q in rows) == {"open-dev": 20, "open-holdout": 20}
    assert Counter((q.kind, q.split) for q in rows) == {(k, s): 4 for k in oe.KINDS for s in oe.SPLITS}
    existing = {json.loads(line)["question"].casefold().strip()
                for path in (config.ROOT / "data").glob("*.jsonl")
                for line in path.read_text().splitlines() if line.strip() and "question" in json.loads(line)}
    assert not existing & {q.question.casefold().strip() for q in rows}
    assert any(q.question == "What happened to the Nvidia CEO?" for q in rows)


def test_split_tampering_is_rejected(tmp_path):
    raw = oe.read_json(settings(tmp_path).questions)
    raw["questions"][0]["split"] = "open-dev" if raw["questions"][0]["split"] == "open-holdout" else "open-holdout"
    path = tmp_path / "questions.json"
    oe.write_json(path, raw)
    with pytest.raises(ValueError, match="original seeded"):
        oe.load_questions(path)


def test_shared_budget_reservations_and_restart(tmp_path):
    path = tmp_path / "spend.json"
    ledger = oe.SpendLedger(1, path)
    token = oe._scope.set("run/unit")
    try:
        ledger.reserve(.6)
        with pytest.raises(config.BudgetExceeded):
            ledger.reserve(.5)
        ledger.settle(.6, .2)
        ledger.reserve(.3)
    finally:
        oe._scope.reset(token)
    resumed = oe.SpendLedger(1, path)
    assert resumed.spent == pytest.approx(.5)
    assert resumed.reserved == 0
    assert resumed.by_scope == {"run/unit": .2, "uncertain_after_restart": .3}
    with pytest.raises(config.BudgetExceeded):
        resumed.reserve(.6)


async def test_observer_preserves_arguments_return_and_restores_after_failure(tmp_path, monkeypatch):
    answer = LLMResponse(model="test", role="agent", tokens_in=10, cost_usd=.01)
    original = AsyncMock(return_value=answer)
    monkeypatch.setattr(oe.llm, "complete", original)
    recorder = oe.Recorder(tmp_path / "obs.jsonl")
    token = oe._recorder.set(recorder)
    messages = [{"role": "user", "content": "question"}]
    try:
        with oe.instrument():
            assert await oe.llm.complete("agent", "system", messages, tools=None) is answer
            original.assert_awaited_once_with("agent", "system", messages, tools=None)
            original.side_effect = asyncio.CancelledError
            with pytest.raises(asyncio.CancelledError):
                await oe.llm.complete("agent", "system", messages, tools=None)
        assert oe.llm.complete is original
    finally:
        oe._recorder.reset(token)
    obs = oe.observations(recorder.path)
    assert [row["kind"] for row in obs] == ["model", "model_error"]
    assert obs[0]["tokens_in"] == 10 and obs[1]["error"] == "CancelledError"


def test_blind_payload_excludes_setup_cost_and_split():
    answers = {name: {"final": name[0], "status": "ANSWERED", "citations": ["https://example.org"],
                      "cost": 99, "split": "open-holdout"} for name in ("second_opinion", "telem_default")}
    documents = {"https://example.org": {"available": False, "text": "", "url": "https://example.org"}}
    payload = oe.blind_payload("question", answers, documents, ["telem_default", "second_opinion"])
    assert payload["A"]["answer"] == "t" and payload["B"]["answer"] == "s"
    encoded = json.dumps(payload)
    assert not any(value in encoded for value in ("second_opinion", "telem_default", "cost", "open-holdout"))
    assert payload["A"]["sources"]["https://example.org"]["available"] is False


def test_measurement_separates_costs_and_followup_notes(tmp_path):
    recorder = oe.Recorder(tmp_path / "obs.jsonl")
    recorder.emit("model", role="agent", cost_usd=.02, tokens_in=10, tokens_out=2, cached=False)
    recorder.emit("model", role="checker", cost_usd=.003, tokens_in=20, tokens_out=3, cached=False)
    recorder.emit("decision", choice="HIT_1", confidence=.6, cost_usd=.001, cached=False)
    recorder.emit("engines", runs={"tavily": {"cost_usd": .005, "cached": False}})
    recorder.emit("extract", page={"url": "https://example.org", "ok": True, "cost_usd": .002, "cached": False})
    recorder.emit("extract_error", error="TimeoutError")
    recorder.emit("check", verdict="DEAD", confidence=.9, escalated=True, fallback=False, wall_ms=100)
    for index, status in ((1, "NOT_FOUND"), (2, "HIT")):
        token = oe._search.set(index)
        recorder.emit("tool_result", actual_search=True, result={"status": status}, category="fact")
        oe._search.reset(token)
    row = {"observations": "obs.jsonl", "log": None, "spend_usd": .031, "wall_ms": 1000,
           "error": None}
    measured = oe.measure_run(row, tmp_path)
    assert measured["costs"] == {"agent": .02, "checker": .004, "engines": .005, "pages": .002}
    assert measured["unattributed_cost"] == pytest.approx(0)
    assert measured["not_hit_notes"] == measured["not_hit_then_search"] == 1
    assert measured["extract_reads"] == measured["fetches"] == 2
    assert measured["page_errors"] == 1
    summary = oe.aggregate([measured])
    assert summary["escalated"] == 1 and summary["accepted"] == 0
    assert summary["confidence"]["median"] == .6
    assert summary["check_wall_ms"] == 100


async def test_full_runner_resumes_without_repeating_calls(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CHECKER_BACKEND", "cascade")
    monkeypatch.setattr(config, "FIRST_ENGINE_ROUTING", "on")
    calls = []

    async def fake_agent(question, setup, *, log_dir):
        assert question.gold == []
        assert config._cache_bypass.get()
        assert config.LLM_BUDGET is config.SEARCH_BUDGET
        config.LLM_BUDGET.reserve(.01)
        config.LLM_BUDGET.settle(.01, .001)
        calls.append((question.id, setup))
        log = EventLog(f"{setup}-{question.id}-unit", question.id, setup, log_dir=log_dir)
        log.emit("run_start", question=question.question)
        answer = AgentAnswer(final="NOT FOUND", status="NOT_FOUND", citations=[])
        log.emit("answer", **answer.model_dump())
        log.emit("run_end", cost_usd_total=.001)
        return answer

    async def fake_judge(role, system, messages, **kwargs):
        assert role == "judge" and config._cache_bypass.get()
        config.LLM_BUDGET.reserve(.01)
        config.LLM_BUDGET.settle(.01, .001)
        return LLMResponse(text=oe.PairJudgment(claims_a=[], claims_b=[], support_winner="tie",
            completeness_winner="tie", overall_winner="tie", reason="Both decline").model_dump_json(),
            role="judge", cost_usd=.001)

    monkeypatch.setattr(oe.agent, "run_question", fake_agent)
    monkeypatch.setattr(oe.llm, "complete", fake_judge)
    original = config.LLM_BUDGET
    result = await oe.run(settings(tmp_path))
    assert len(calls) == 120 and result["quality"]["all"]["n"] == 40
    assert result["spend"]["spent"] == pytest.approx(.16)
    assert config.LLM_BUDGET is original
    await oe.run(settings(tmp_path))
    assert len(calls) == 120
    assert (tmp_path / "report.md").exists() and not (tmp_path / "running.lock").exists()


def test_total_budget_cannot_be_raised_above_cap(monkeypatch):
    monkeypatch.setenv("OPEN_EVAL_BUDGET_USD", "15.01")
    with pytest.raises(ValueError, match="15 USD"):
        oe.Settings.environment()


@pytest.mark.parametrize("uncited", [False, True])
async def test_judge_records_inaccessible_sources_and_rejects_uncited_claims(tmp_path, monkeypatch, uncited):
    monkeypatch.setattr(config, "API_MAX_RETRIES", 1)
    run_settings = settings(tmp_path)
    _, questions = oe.load_questions(run_settings.questions)
    q = questions[0]
    url = "https://example.org/evidence"
    answer = AgentAnswer(final="A factual statement", status="ANSWERED", citations=[url]).model_dump()
    for setup in ("second_opinion", "telem_default"):
        oe.write_json(tmp_path / "runs" / f"{q.id}.{setup}.json", {"answer": answer, "error": None})
    fetch = AsyncMock(side_effect=RuntimeError("Source unavailable"))
    monkeypatch.setattr(oe.telem_client, "fetch_page", fetch)
    judge_inputs = []

    async def judge(role, system, messages, **kwargs):
        assert role == "judge"
        payload = json.loads(messages[0]["content"])
        judge_inputs.append(payload)
        ledger.reserve(.01)
        ledger.settle(.01, .001)
        assert payload["A"]["sources"][url]["available"] is False
        claim = oe.ClaimAudit(claim="A factual statement", support="source_unavailable",
                             source_urls=["https://example.org/uncited" if uncited else url])
        return LLMResponse(text=oe.PairJudgment(claims_a=[claim], claims_b=[claim],
            support_winner="tie", completeness_winner="tie", overall_winner="tie",
            reason="Neither source could be read").model_dump_json(), role="judge")

    monkeypatch.setattr(oe.llm, "complete", judge)
    ledger = oe.SpendLedger(1, tmp_path / "spend.json")
    await oe.judge_pairs(run_settings, [q], 14, ledger)
    fetch.assert_awaited_once_with(url)
    result = oe.read_json(tmp_path / "judgments" / f"{q.id}.json")
    if uncited:
        assert "uncited source" in result["error"] and "winners" not in result
        await oe.judge_pairs(run_settings, [q], 14, ledger)
        assert len(judge_inputs) == 1
        uncited = False
        await oe.judge_pairs(run_settings, [q], 14, ledger, retry_errors=True)
        assert len(judge_inputs) == 2 and judge_inputs[0] == judge_inputs[1]
        assert ledger.spent == pytest.approx(.002)
        archived = oe.read_json(tmp_path / "judgments" / "attempts" / f"{q.id}.1.json")
        assert "uncited source" in archived["error"]
        assert "winners" in oe.read_json(tmp_path / "judgments" / f"{q.id}.json")
        fetch.assert_awaited_once_with(url)
    else:
        assert result["available_sources"] == 0 and result["source_count"] == 1
        assert result["winners"]["overall_winner"] == "tie"
