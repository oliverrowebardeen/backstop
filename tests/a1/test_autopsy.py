"""Task B: replay a Telem session through the checker (fake Sessions API and checker)."""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from telem.errors import APIStatusError, NotFoundError

from server import api_v1
from server.usage import RateLimiter, UsageStore
from so import autopsy as autopsy_module
from so.autopsy import (MAX_STEPS, Autopsy, AutopsyUnavailable, SessionNotFound, autopsy, cohens_kappa,
                        parse_history, parse_results)
from so.contracts import CheckerVerdict, RescueTable
from tests.a1.fakes import FakeAsyncTelem, FakeSessions, verdict

TODAY = date(2026, 11, 14)


def run(provider: str, query: str, urls: list[str], batch: int = 0) -> dict[str, Any]:
    return {"preprocessor_name": provider, "status": "succeeded", "batch_index": batch, "query": query,
            "raw_payload": {"results": [{"url": u, "score": 0.9} for u in urls]},
            "output_payload": {"results": [{"url": u, "title": f"T {u}", "summary": f"about {u}", "rank": i}
                                           for i, u in enumerate(urls, 1)]}}


def interaction(n: int, query: str, provider: str = "tavily", *, goal: str | None = None,
                metadata: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    meta = dict(metadata or {})
    if goal:
        meta["goal"] = goal
    return {"interaction_id": f"i{n}", "created_at": f"2026-11-14T19:00:{n:02d}Z", "status": "succeeded",
            "preprocessor_runs": [run(provider, query, [f"https://{provider}.example/{n}"])],
            "postprocessor_runs": [], "pinned_providers": [provider], "trajectory_goal": None,
            "user_input": {"query": query}, "metadata": meta, **extra}


def history(*items: dict[str, Any]) -> dict[str, Any]:
    """Telem lists interactions newest first."""
    return {"interactions": list(reversed(items))}


def scripted(labels: dict[str, str]):
    """Checker keyed by query; records goals it was given."""
    seen: list[tuple[str, str]] = []

    async def check(query, goal, results, today):
        seen.append((query, goal))
        label = labels[query]
        if label == "boom":
            raise RuntimeError("model down")
        return verdict(label, evidence_rank=1 if label == "HIT" else None,
                       best_url=results[0].url if label == "SHALLOW" else None)

    check.seen = seen
    return check


def client_for(hist: dict[str, Any], results: dict[str, Any] | None = None, **kwargs: Any) -> FakeAsyncTelem:
    return FakeAsyncTelem(sessions=FakeSessions(history=hist, results=results, **kwargs))


def test_parse_history_orders_oldest_first_and_reads_ledger():
    steps = parse_history(history(
        interaction(1, "first q", goal="find X", metadata={"rescue_step": "initial", "verdict": "none",
                                                          "engine": "tavily", "backstop_version": "0.1.0",
                                                          "context": "backstop step=initial verdict=none"}),
        interaction(2, "second q", "exa"),
    ))
    assert [s.query for s in steps] == ["first q", "second q"]
    assert [s.index for s in steps] == [0, 1]
    assert steps[0].goal == "find X" and steps[1].goal is None
    assert steps[0].context == "backstop step=initial verdict=none"
    assert steps[0].backstop == {"rescue_step": "initial", "verdict": "none", "engine": "tavily",
                                 "backstop_version": "0.1.0"}
    assert steps[1].providers == ["exa"]
    assert [r.url for r in steps[0].results] == ["https://tavily.example/1"]
    assert steps[0].results[0].snippet == "about https://tavily.example/1"
    assert steps[0].telem_judge is None


def test_parse_history_splits_batches_and_skips_empty():
    item = interaction(1, "a")
    item["preprocessor_runs"] = [run("tavily", "a", ["https://a.example"], 0), run("tavily", "b", ["https://b.example"], 1)]
    empty = {"interaction_id": "x", "created_at": "2026-11-14T19:00:05Z", "preprocessor_runs": [], "user_input": {}}
    steps = parse_history({"interactions": [empty, item, "junk"]})
    assert [s.query for s in steps] == ["a", "b"]


def test_parse_results_fallback():
    payload = {"goal": "g", "previous_preprocessor_results": [
        {"step": 2, "preprocessor_runs": [{"preprocessor_name": "exa", "query": "two",
                                           "results": [{"url": "https://e.example", "title": "E", "content": "c"}]}]},
        {"step": 1, "preprocessor_runs": [{"preprocessor_name": "tavily", "query": "one", "results": []}]},
    ]}
    steps = parse_results(payload)
    assert [s.query for s in steps] == ["one", "two"]
    assert steps[1].results[0].snippet == "c" and steps[1].providers == ["exa"]


async def test_first_failure_stop_index_and_next_rescue(monkeypatch):
    monkeypatch.setattr(autopsy_module, "load_rescue_table",
                        lambda: RescueTable(order_after={"tavily": ["brave", "exa"]}, fallback_order=["exa"]))
    fake = client_for(history(interaction(1, "q1", goal="find the CEO"), interaction(2, "q2", "exa"),
                              interaction(3, "q3", "brave"), interaction(4, "q4", "exa")))
    checker = scripted({"q1": "WRONG_ENTITY", "q2": "DEAD", "q3": "HIT", "q4": "HIT"})
    result = await autopsy("sess-1", client=fake, checker=checker, today=TODAY)
    assert isinstance(result, Autopsy)
    assert [s.checker_verdict for s in result.steps] == ["WRONG_ENTITY", "DEAD", "HIT", "HIT"]
    assert result.first_failure_index == 0 and result.steps_after_first_failure == 3
    assert result.stop_index == 2 and result.searches_after_stop == 1
    assert result.next_rescue.action == "RETRY_ENGINE" and result.next_rescue.engine == "brave"
    assert result.goal == "find the CEO" and result.goal_source == "recorded"
    assert {goal for _, goal in checker.seen} == {"find the CEO"}
    assert ("history", "sess-1", {"full": True}) in fake.sessions.calls
    assert ("results", "sess-1", {}) in fake.sessions.calls


async def test_shallow_failure_suggests_reading_the_page(monkeypatch):
    monkeypatch.setattr(autopsy_module, "load_rescue_table", lambda: RescueTable())
    fake = client_for(history(interaction(1, "q1")))
    result = await autopsy("s", client=fake, checker=scripted({"q1": "SHALLOW"}), today=TODAY)
    assert result.next_rescue.action == "FETCH_PAGE"
    assert result.next_rescue.url == "https://tavily.example/1"


async def test_goal_falls_back_to_session_results_then_first_query():
    fake = client_for(history(interaction(1, "q1"), interaction(2, "q2")), results={"goal": "results goal"})
    checker = scripted({"q1": "HIT", "q2": "HIT"})
    result = await autopsy("s", client=fake, checker=checker, today=TODAY)
    assert result.goal == "results goal" and result.goal_source == "recorded"
    assert result.first_failure_index is None and result.next_rescue is None
    assert result.stop_index == 0 and result.searches_after_stop == 1

    fake = client_for(history(interaction(1, "q1"), interaction(2, "q2")))
    checker = scripted({"q1": "DEAD", "q2": "DEAD"})
    result = await autopsy("s", client=fake, checker=checker, today=TODAY)
    assert result.goal == "q1" and result.goal_source == "first_query"
    assert {goal for _, goal in checker.seen} == {"q1"}
    assert result.stop_index is None


async def test_judge_absent_is_null_and_says_so():
    fake = client_for(history(interaction(1, "q1"), interaction(2, "q2")))
    result = await autopsy("s", client=fake, checker=scripted({"q1": "DEAD", "q2": "HIT"}), today=TODAY)
    assert result.telem_judge is None
    assert all(step.telem_judge is None for step in result.steps)
    assert "no judge" in result.telem_judge_note
    # The engine's own raw relevance score is not mistaken for a judge.
    assert "score" in str(fake.sessions.history_payload)


async def test_judge_present_reports_values_and_kappa():
    items = [interaction(i, f"q{i}", postprocessor_runs=[{"name": "judge", "output_payload": {"quality_score": score}}])
             for i, score in enumerate([0.9, 0.2, 0.8, 0.1], 1)]
    fake = client_for(history(*items))
    checker = scripted({"q1": "HIT", "q2": "DEAD", "q3": "DEAD", "q4": "DEAD"})
    result = await autopsy("s", client=fake, checker=checker, today=TODAY)
    assert [s.telem_judge for s in result.steps] == [0.9, 0.2, 0.8, 0.1]
    assert result.telem_judge["n_compared"] == 4 and result.telem_judge["agree"] == 3
    assert result.telem_judge["kappa"] == cohens_kappa([(True, True), (False, False), (False, True), (False, False)])


def test_cohens_kappa():
    assert cohens_kappa([]) is None
    assert cohens_kappa([(True, True), (False, False)]) == 1.0
    assert cohens_kappa([(True, True), (True, True)]) is None
    assert cohens_kappa([(True, False), (False, True)]) == -1.0


async def test_step_cap_truncates_and_counts_later_steps():
    items = [interaction(i, f"q{i}") for i in range(30)]
    checker = scripted({f"q{i}": "DEAD" for i in range(30)})
    result = await autopsy("s", client=client_for(history(*items)), checker=checker, today=TODAY)
    assert len(result.steps) == MAX_STEPS and result.n_searches == 30 and result.truncated
    assert len(checker.seen) == MAX_STEPS
    assert result.first_failure_index == 0 and result.steps_after_first_failure == 29
    small = await autopsy("s", client=client_for(history(*items)), checker=checker, max_steps=99, today=TODAY)
    assert len(small.steps) == MAX_STEPS


async def test_time_cap_returns_partial_steps():
    async def slow(query, goal, results, today):
        if query == "q2":
            await asyncio.sleep(5)
        return verdict("DEAD")

    fake = client_for(history(interaction(1, "q1"), interaction(2, "q2")))
    result = await autopsy("s", client=fake, checker=slow, timeout_s=0.2, today=TODAY)
    assert result.timed_out
    assert result.steps[0].checker_verdict == "DEAD"
    assert result.steps[1].checker_verdict is None and "budget" in result.steps[1].checker_error
    assert result.elapsed_ms < 2000


async def test_timeout_is_capped_at_thirty_seconds(monkeypatch):
    seen = {}

    async def fake_read(client, session_id, timeout_s):
        seen["timeout"] = timeout_s
        return history(interaction(1, "q1")), {}

    monkeypatch.setattr(autopsy_module, "_read_session", fake_read)
    await autopsy("s", client=object(), checker=scripted({"q1": "HIT"}), timeout_s=999, today=TODAY)
    assert seen["timeout"] == 30.0


async def test_checker_error_on_one_step_is_recorded_not_raised():
    fake = client_for(history(interaction(1, "q1"), interaction(2, "q2")))
    result = await autopsy("s", client=fake, checker=scripted({"q1": "boom", "q2": "DEAD"}), today=TODAY)
    assert result.steps[0].checker_verdict is None and "RuntimeError" in result.steps[0].checker_error
    assert result.first_failure_index == 1


async def test_session_api_errors_are_typed():
    missing = client_for({}, error=NotFoundError("no session", status_code=404, body={}))
    with pytest.raises(SessionNotFound):
        await autopsy("nope", client=missing, checker=scripted({}))
    down = client_for({}, error=APIStatusError("bad gateway", status_code=502, body={}))
    with pytest.raises(AutopsyUnavailable):
        await autopsy("s", client=down, checker=scripted({}))
    hung = client_for(history(interaction(1, "q1")), delay_s=5)
    with pytest.raises(AutopsyUnavailable):
        await autopsy("s", client=hung, checker=scripted({"q1": "HIT"}), timeout_s=0.1)
    with pytest.raises(SessionNotFound):
        await autopsy("empty", client=client_for({"interactions": []}), checker=scripted({}))
    with pytest.raises(ValueError):
        await autopsy("  ", client=client_for({}), checker=scripted({}))


KEY = "bk_test_autopsy"


@pytest.fixture
def api(monkeypatch, tmp_path):
    monkeypatch.setenv("BACKSTOP_API_KEYS", KEY)
    calls: list[str] = []
    outcome: dict[str, Any] = {}

    async def fake_autopsy(session_id: str, **kwargs: Any) -> Autopsy:
        calls.append(session_id)
        if "error" in outcome:
            raise outcome["error"]
        return Autopsy(session_id=session_id, goal="g", goal_source="recorded", n_searches=0)

    monkeypatch.setattr(api_v1, "autopsy", fake_autopsy)
    app = FastAPI()
    app.include_router(api_v1.router)
    limiter = RateLimiter(limit=60)
    app.dependency_overrides[api_v1.get_rate_limiter] = lambda: limiter
    store = UsageStore(tmp_path / "usage.db")
    for owned in ("sess-1", "af5396f2-a3a8-45bb", "s1"):
        store.bind_session(KEY, owned)
    app.dependency_overrides[api_v1.get_usage_store] = lambda: store
    return TestClient(app), calls, outcome


def test_route_requires_api_key(api):
    client, calls, _ = api
    assert client.get("/v1/autopsy/sess-1").status_code == 401
    assert client.get("/v1/autopsy/sess-1", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert calls == []


def test_route_returns_autopsy(api):
    client, calls, _ = api
    resp = client.get("/v1/autopsy/af5396f2-a3a8-45bb", headers={"Authorization": f"Bearer {KEY}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["session_id"] == "af5396f2-a3a8-45bb" and body["telem_judge"] is None
    assert calls == ["af5396f2-a3a8-45bb"]


@pytest.mark.parametrize("error,status,code", [
    (SessionNotFound("no such session"), 404, "session_not_found"),
    (AutopsyUnavailable("down"), 502, "telem_unavailable"),
])
def test_route_maps_errors(api, error, status, code):
    client, _, outcome = api
    outcome["error"] = error
    resp = client.get("/v1/autopsy/s1", headers={"Authorization": f"Bearer {KEY}"})
    assert resp.status_code == status and resp.json()["detail"]["code"] == code


def test_route_rejects_odd_session_ids(api):
    client, calls, _ = api
    resp = client.get("/v1/autopsy/bad%20id%3B", headers={"Authorization": f"Bearer {KEY}"})
    assert resp.status_code == 422 and calls == []
