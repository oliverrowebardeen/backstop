"""limits, fail-open checking, engine errors, ranking and visible rewrites."""

from datetime import date

import pytest

from so import config
from so.checker import CheckerError
from so.contracts import EngineRun, PageText, RunContext
from so.second_opinion import SearchLimits, TelemUnavailable, rank_results, ranked_results, search
from tests.s6.fakes import FakeLog, Fakes, source, verdict


@pytest.fixture
def ctx():
    return RunContext(run_id="second_opinion-s6", question_id="s6", setup="second_opinion",
                      session="session-s6", log=FakeLog(), today=date(2026, 11, 14))


def _types(outcome):
    return [event["type"] for event in outcome.trace]


def _done(outcome):
    return next(event["data"] for event in reversed(outcome.trace) if event["type"] == "search_done")


# Limits -----------------------------------------------------------------------------

def test_limits_default_from_config(monkeypatch):
    assert SearchLimits.from_config() == SearchLimits(2, 0.03, 12.0, 4.0)
    monkeypatch.setattr(config, "SEARCH_TIMEOUT_S", 30.0)
    assert SearchLimits.from_config(max_rescues=1, timeout_s=None).timeout_s == 30.0
    assert SearchLimits.from_config(max_rescues=1).max_rescues == 1


@pytest.mark.parametrize("bad", [{"max_rescues": -1}, {"max_cost_usd": 0}, {"timeout_s": float("nan")}])
def test_limits_reject_invalid_values(bad):
    with pytest.raises(ValueError):
        SearchLimits.from_config(**bad)


def test_limits_reject_unknown_names():
    with pytest.raises(TypeError, match="Unknown search limit"):
        SearchLimits.from_config(max_engines=3)


async def test_default_behavior_unchanged_when_no_limit_hit(ctx, monkeypatch):
    Fakes([verdict("DEAD")] * 3).install(monkeypatch)
    outcome = await search("query", "goal", ctx)
    assert outcome.status == "NOT_FOUND" and outcome.calls == 3
    assert outcome.note == ("No engine surfaced this after 3 tries. "
                            "If you cannot answer from other evidence, answer NOT FOUND.")
    assert "stopped_by" not in _done(outcome)


async def test_per_call_max_rescues_override(ctx, monkeypatch):
    Fakes([verdict("DEAD")] * 4).install(monkeypatch)
    one = await search("query", "goal", ctx, limits=SearchLimits.from_config(max_rescues=1))
    assert one.calls == 2 and one.status == "NOT_FOUND"


async def test_per_call_max_rescues_can_exceed_global_cap(ctx, monkeypatch):
    Fakes([verdict("DEAD")] * 4).install(monkeypatch)
    outcome = await search("query", "goal", ctx, limits=SearchLimits.from_config(max_rescues=3))
    assert outcome.calls == 4
    assert outcome.engines_called == ["tavily", "exa", "perplexity", "brave"]


async def test_cost_limit_stops_rescue_and_says_why(ctx, monkeypatch):
    Fakes([verdict("DEAD")] * 3).install(monkeypatch)
    outcome = await search("query", "goal", ctx, limits=SearchLimits.from_config(max_cost_usd=0.01))
    # 0.006 spent; next rescue estimated at 0.005 engine + 0.001 checker = 0.012 > 0.01
    assert outcome.calls == 1 and outcome.status == "NOT_FOUND"
    assert "cost limit" in outcome.note
    assert outcome.note.startswith("No engine surfaced this after 1 tries.")
    assert "cost limit" in _done(outcome)["stopped_by"]
    assert "rescue" not in _types(outcome)


async def test_time_limit_stops_rescue_and_says_why(ctx, monkeypatch):
    Fakes([verdict("DEAD")] * 3, search_delay_s=0.05).install(monkeypatch)
    outcome = await search("query", "goal", ctx, limits=SearchLimits.from_config(timeout_s=0.08))
    assert outcome.calls == 1 and outcome.status == "NOT_FOUND"
    assert "time limit" in outcome.note and "timeout_s" in outcome.note


async def test_fetch_rescue_is_cost_checked(ctx, monkeypatch):
    monkeypatch.setattr(config, "TAVILY_EXTRACT_PRICE_USD", 0.5)
    Fakes([verdict("SHALLOW", best_url=source().url)]).install(monkeypatch)
    outcome = await search("query", "goal", ctx)
    assert outcome.fetches == 0 and "cost limit" in outcome.note


# Fail open --------------------------------------------------------------------------

async def test_checker_error_fails_open_with_results(ctx, monkeypatch):
    Fakes([CheckerError("model call failed")]).install(monkeypatch)
    outcome = await search("query", "goal", ctx)
    assert outcome.status == "UNCHECKED"
    assert [item.url for item in outcome.results] == [source().url]
    assert "Check skipped because the checker failed (CheckerError)" in outcome.note
    assert _done(outcome)["check_skipped"] == "the checker failed (CheckerError)"
    assert _types(outcome) == ["search", "search_done"]


async def test_checker_timeout_fails_open(ctx, monkeypatch):
    Fakes([0.5, verdict("HIT")]).install(monkeypatch)
    outcome = await search("query", "goal", ctx, limits=SearchLimits.from_config(checker_timeout_s=0.05))
    assert outcome.status == "UNCHECKED"
    assert "did not answer within 0.05s" in outcome.note


async def test_checker_failure_during_rescue_keeps_gathered_results(ctx, monkeypatch):
    Fakes([verdict("DEAD", rewrite="better"), RuntimeError("boom")], runs=[
        EngineRun(engine="tavily", status="ok", results=[source("https://a.example")], cost_usd=0.005),
        EngineRun(engine="exa", status="ok", results=[source("https://b.example", engine="exa")], cost_usd=0.005),
    ]).install(monkeypatch)
    outcome = await search("query", "goal", ctx)
    assert outcome.status == "UNCHECKED" and outcome.calls == 2
    assert {item.url for item in outcome.results} == {"https://a.example", "https://b.example"}


async def test_page_checker_failure_fails_open(ctx, monkeypatch):
    url = source().url
    Fakes([verdict("SHALLOW", best_url=url), RuntimeError("boom")],
          pages=[PageText(url=url, ok=True, text="page")]).install(monkeypatch)
    outcome = await search("query", "goal", ctx)
    assert outcome.status == "UNCHECKED" and outcome.fetches == 1


async def test_budget_exceeded_in_checker_still_propagates(ctx, monkeypatch):
    # Looked up at call time: other suites reload so.config, replacing the class.
    Fakes([config.BudgetExceeded("llm budget")]).install(monkeypatch)
    with pytest.raises(config.BudgetExceeded):
        await search("query", "goal", ctx)


# Engine errors ------------------------------------------------------------------------

async def test_engine_error_is_skipped_without_spending_a_rescue(ctx, monkeypatch):
    fake = Fakes([verdict("DEAD"), verdict("DEAD"), verdict("DEAD"), verdict("DEAD")], runs=[
        EngineRun(engine="tavily", status="error"),
    ])
    fake.install(monkeypatch)
    outcome = await search("query", "goal", ctx)
    assert outcome.engines_called == ["tavily", "exa", "perplexity", "brave"]
    rescues = [event["data"] for event in outcome.trace if event["type"] == "rescue"]
    assert rescues[0]["counted"] is False and "tavily failed" in rescues[0]["reason"]
    assert [item["counted"] for item in rescues[1:]] == [True, True]
    search_event = outcome.trace[0]["data"]
    assert search_event["status"] == "error" and "error" in search_event


async def test_engine_exception_after_first_call_is_skipped(ctx, monkeypatch):
    Fakes([verdict("DEAD", rewrite="q2"), verdict("HIT", evidence_rank=1)], runs=[
        EngineRun(engine="tavily", status="ok", results=[source()], cost_usd=0.005),
        RuntimeError("exa exploded"),
        EngineRun(engine="perplexity", status="ok", results=[source("https://p.example", engine="perplexity")]),
    ]).install(monkeypatch)
    outcome = await search("query", "goal", ctx)
    assert outcome.status == "HIT"
    assert outcome.engines_called == ["tavily", "exa", "perplexity"]
    exa_search = [event["data"] for event in outcome.trace if event["type"] == "search"][1]
    assert exa_search["error"] == "RuntimeError: exa exploded"


async def test_first_call_transport_failure_raises_typed_error(ctx, monkeypatch):
    Fakes([], runs=[RuntimeError("router down")]).install(monkeypatch)
    with pytest.raises(TelemUnavailable, match="router down"):
        await search("query", "goal", ctx)


async def test_every_engine_erroring_raises_typed_error(ctx, monkeypatch):
    Fakes([verdict("DEAD")] * 6, runs=[EngineRun(engine=e, status="error") for e in
                                      ["tavily", "exa", "perplexity", "brave", "parallel", "ceramic"]]).install(monkeypatch)
    with pytest.raises(TelemUnavailable, match="every engine"):
        await search("query", "goal", ctx)


# Ranking ------------------------------------------------------------------------------

async def test_ranking_evidence_then_agreement_then_engine_rank(ctx, monkeypatch):
    shared = "https://shared.example"
    Fakes([verdict("DEAD", rewrite="q2"), verdict("HIT", evidence_rank=3)], runs=[
        EngineRun(engine="tavily", status="ok", results=[
            source("https://t1.example", rank=1), source(shared, rank=2), source("https://t3.example", rank=3)]),
        EngineRun(engine="exa", status="ok", results=[
            source(shared, engine="exa", rank=1), source("https://e2.example", engine="exa", rank=2),
            source("https://evidence.example", engine="exa", rank=3)]),
    ]).install(monkeypatch)
    outcome = await search("query", "goal", ctx)
    ranked = ranked_results(outcome)
    assert [(item.url, item.rank_reason) for item in ranked] == [
        ("https://evidence.example", "evidence"),
        (shared, "agreement: 2 engines"),
        ("https://t1.example", "engine rank 1"),
        ("https://e2.example", "engine rank 2"),
        ("https://t3.example", "engine rank 3"),
    ]
    assert [item.rank for item in ranked] == [1, 2, 3, 4, 5]
    assert ranked[1].engines == ["exa", "tavily"]
    assert _done(outcome)["ranking"][0] == {
        "url": "https://evidence.example", "rank": 1, "rank_reason": "evidence", "engines": ["exa"]}


def test_rank_results_without_evidence_orders_by_agreement_then_rank():
    merged = [source("https://a", rank=1), source("https://b", rank=2), source("https://c", rank=3)]
    seen = {"https://a": {"tavily": 4}, "https://b": {"tavily": 2, "exa": 5}, "https://c": {"exa": 1}}
    ranked = rank_results(merged, seen, None)
    assert [item.url for item in ranked] == ["https://b", "https://c", "https://a"]


def test_ranked_results_falls_back_for_unchecked_outcomes():
    from so.contracts import SearchOutcome

    outcome = SearchOutcome(status="UNCHECKED", results=[source("https://a"), source("https://b", rank=2)])
    assert [item.rank_reason for item in ranked_results(outcome)] == ["engine rank 1", "engine rank 2"]


# Query rewriting ------------------------------------------------------------------------

async def test_rewrite_is_recorded_before_and_after(ctx, monkeypatch):
    Fakes([verdict("WRONG_ENTITY", rewrite="Stripe payments valuation 2023"), verdict("HIT")]).install(monkeypatch)
    outcome = await search("Stripe valuation", "goal", ctx)
    rescue = next(event["data"] for event in outcome.trace if event["type"] == "rescue")
    assert rescue["original_query"] == "Stripe valuation"
    assert rescue["rewritten_query"] == "Stripe payments valuation 2023"
    assert rescue["query"] == "Stripe payments valuation 2023"
    assert ctx.log.events == outcome.trace


async def test_no_rewrite_fields_when_query_unchanged(ctx, monkeypatch):
    Fakes([verdict("DEAD"), verdict("HIT")]).install(monkeypatch)
    outcome = await search("query", "goal", ctx)
    rescue = next(event["data"] for event in outcome.trace if event["type"] == "rescue")
    assert "rewritten_query" not in rescue


async def test_events_validate_against_event_contract(ctx, monkeypatch, tmp_path):
    from so.eventlog import EventLog, read_log

    ctx.log = EventLog("second_opinion-s6", "s6", "second_opinion", log_dir=tmp_path)
    Fakes([CheckerError("x")]).install(monkeypatch)
    await search("query", "goal", ctx)
    Fakes([verdict("DEAD", rewrite="q2"), verdict("HIT")]).install(monkeypatch)
    await search("query", "goal", ctx)
    assert len(read_log(ctx.log.path)) >= 6
