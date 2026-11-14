"""Paid races retain opt-in, API authentication, and shared admission controls."""
import asyncio
from dataclasses import replace
import json
import logging

from fastapi.testclient import TestClient
import httpx
import pytest

from server import api_v1
from server.app import ServerSettings, create_app
from server.races import RaceManager
from server.usage import RateLimiter, UsageStore, key_id
from so.contracts import AgentAnswer, Question
from so.eventlog import EventLog
from tests.o4.access import AUTH, KEY
from tests.o4.fakes import run_question
from tests.o4.test_server import settings

ROUTES = ("/race",)


@pytest.fixture
def build(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKSTOP_API_KEYS", KEY)

    def make(*, live=True, runner=run_question, limit=60, capacity=8):
        app = create_app(settings=settings(tmp_path, live_races=live, max_active_runs=capacity), runner=runner)
        limiter = RateLimiter(limit=limit)
        store = UsageStore(tmp_path / "usage.db")
        app.dependency_overrides[api_v1.get_rate_limiter] = lambda: limiter
        app.dependency_overrides[api_v1.get_usage_store] = lambda: store
        return app

    return make


def test_default_app_disables_paid_routes_even_with_provider_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEM_API_KEY", "test-only-provider-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-only-provider-key")
    called = []

    async def forbidden(*args, **kwargs):
        called.append(True)
        raise AssertionError("A disabled route must never start an agent")

    configured = replace(ServerSettings.from_config(), log_dir=tmp_path / "logs", live_races=False)
    with TestClient(create_app(settings=configured, runner=forbidden)) as client:
        for route in ROUTES:
            for headers in ({}, AUTH):
                response = client.post(route, headers=headers, json={"question": "A question"})
                assert response.status_code == 403
                assert response.json()["detail"]["code"] == "live_races_disabled"
        assert client.get("/config").json()["live_available"] is False
        assert client.get("/replays").status_code == 200
    assert called == [] and not configured.log_dir.exists()


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": f"Basic {KEY}"}])
def test_enabled_races_require_the_hosted_api_key(build, tmp_path, route, headers):
    with TestClient(build()) as client:
        response = client.post(route, headers=headers, json={"question": "A question"})
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"
    assert not (tmp_path / "out/logs").exists()


@pytest.mark.parametrize("first", ROUTES)
def test_races_share_the_api_rate_limit(build, first):
    with TestClient(build(limit=1), headers=AUTH) as client:
        assert client.post(first, json={"question": "A question"}).status_code == 200
        for route in ROUTES:
            response = client.post(route, json={"question": "Another question"})
            assert response.status_code == 429
            assert response.json()["detail"]["code"] == "rate_limited"
            assert int(response.headers["retry-after"]) >= 1
        response = client.post("/v1/search", json={"query": "Another question"})
        assert response.status_code == 429
        assert response.json()["detail"]["code"] == "rate_limited"


def test_api_admission_also_limits_races(build):
    app = build(limit=1)

    class UnusedClient:
        async def search(self, *args, **kwargs):
            raise AssertionError("The invalid request must not search")

    app.dependency_overrides[api_v1.get_client] = UnusedClient
    with TestClient(app, headers=AUTH) as client:
        assert client.post("/v1/search", json={"query": ""}).status_code == 422
        for route in ROUTES:
            assert client.post(route, json={"question": "A question"}).status_code == 429


@pytest.mark.parametrize("first", ROUTES)
async def test_races_share_capacity_and_release_it(build, first):
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def waiting(question, setup, *, log_dir):
        calls.append(setup)
        if len(calls) == 2:
            started.set()
        await release.wait()
        return await run_question(question, setup, log_dir=log_dir)

    app = build(runner=waiting, capacity=2)
    other = "/race"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers=AUTH) as client:
        pending = asyncio.create_task(client.post(first, json={"question": "Hold the shared capacity"}))
        try:
            await asyncio.wait_for(started.wait(), 1)
            denied = await client.post(other, json={"question": "Must not start"})
            assert denied.status_code == 429 and "capacity" in denied.text
            assert len(calls) == 2
        finally:
            release.set()
        response = await pending
        assert response.status_code == 200
        if first == "/race":
            for run_id in response.json().values():
                await client.get(f"/stream/{run_id}")
        assert (await client.post(other, json={"question": "Capacity released"})).status_code == 200


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("failed", [False, True])
def test_each_run_logs_hashed_attribution_and_known_cost_once(build, route, failed, caplog):
    async def metered(question, setup, *, log_dir):
        log = EventLog(f"{setup}-{question.id}", question.id, setup, log_dir=log_dir)
        log.emit("run_start", question=question.question)
        log.emit("search", engines=["tavily"], cost_usd=.005)
        log.emit("llm", cost_usd=.012)
        log.emit("fetch", via="telem", cost_usd=.001)
        log.emit("search_done", cost_usd=.018)
        if failed:
            raise RuntimeError("Test failure after paid events")
        answer = AgentAnswer(status="ANSWERED", final="Test evidence")
        log.emit("answer", **answer.model_dump())
        log.emit("run_end", engine_calls=1, fetches=1, cost_usd_total=.018, latency_ms=1)
        return answer

    with caplog.at_level(logging.INFO, logger="uvicorn.error.race_usage"):
        with TestClient(build(runner=metered), headers=AUTH) as client:
            response = client.post(route, json={"question": "A question"})
            assert response.status_code == 200
            if route == "/race":
                for run_id in response.json().values():
                    client.get(f"/stream/{run_id}")
    rows = [json.loads(record.message) for record in caplog.records if record.name == "uvicorn.error.race_usage"]
    assert len(rows) == 2
    assert {row["side"] for row in rows} == {"left", "right"}
    assert all(row["key_id"] == key_id(KEY) and row["failed"] == failed for row in rows)
    assert all(row["engine_calls"] == 1 and row["fetches"] == 1 and row["cost_usd"] == pytest.approx(.018) for row in rows)
    assert KEY not in caplog.text


async def test_cancelled_runs_release_capacity_and_log_partial_cost(tmp_path, caplog):
    started = asyncio.Event()

    async def waiting(question, setup, *, log_dir):
        log = EventLog(setup + question.id, question.id, setup, log_dir=log_dir)
        log.emit("llm", cost_usd=.01)
        started.set()
        await asyncio.Event().wait()

    manager = RaceManager(settings(tmp_path, max_active_runs=2), waiting)
    question = Question(id="cancel", question="A question", gold=[], category="fact", source="test", split="test")
    with caplog.at_level(logging.INFO, logger="uvicorn.error.race_usage"):
        manager.start(question, "single", "second_opinion", api_key=KEY)
        await started.wait()
        await manager.close()
    rows = [json.loads(record.message) for record in caplog.records if record.name == "uvicorn.error.race_usage"]
    assert len(rows) == 2 and all(row["failed"] and row["cost_usd"] == .01 for row in rows)
    assert not manager.tasks
