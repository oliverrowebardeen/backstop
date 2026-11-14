"""Review fixes for the hosted API: per-key spend caps (#2), session ownership (#3), blank queries (#6)."""

from __future__ import annotations

import asyncio
import sys
from types import ModuleType
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import api_v1
from server.usage import RateLimiter, UsageStore
from so import autopsy as autopsy_module
from so import config
from so.client import BackstopSearchResponse
from tests.a1.fakes import CheckedVerdict, FakeAsyncTelem, FakeSessions, verdict

ALICE = "bk_test_alice"
BOB = "bk_test_bob"


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


class FakeClient:
    """Stands in for BackstopTelem: records search kwargs; spends cost_usd, capped by max_cost_usd."""

    def __init__(self, *, cost_usd: float = 0.01, delay_s: float = 0.0) -> None:
        self.cost_usd = cost_usd
        self.delay_s = delay_s
        self.calls: list[dict[str, Any]] = []
        self.sessions_made = 0

    async def search(self, query: str, **kwargs: Any) -> BackstopSearchResponse:
        self.calls.append({"query": query, **kwargs})
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        session = kwargs.get("session")
        if session is None:
            self.sessions_made += 1
            session = f"sess-{self.sessions_made}"
        cost = min(self.cost_usd, kwargs.get("max_cost_usd") or self.cost_usd)
        return BackstopSearchResponse(status="HIT", session_id=session, engine_calls=1, cost_usd=cost)


@pytest.fixture
def make_app(monkeypatch, tmp_path):
    monkeypatch.setenv("BACKSTOP_API_KEYS", f"{ALICE},{BOB}")
    monkeypatch.setattr(config, "BACKSTOP_KEY_SPEND_CAP_USD", 5.0)
    monkeypatch.setattr(config, "BACKSTOP_MAX_COST_USD", 0.10)
    monkeypatch.setattr(config, "BACKSTOP_MAX_RESCUES", 3)
    monkeypatch.setattr(config, "BACKSTOP_CHECK_COST_USD", 0.001)

    def build(client: FakeClient | None = None) -> tuple[FastAPI, UsageStore, FakeClient]:
        fake = client or FakeClient()
        store = UsageStore(tmp_path / "usage.db")
        app = FastAPI()
        app.include_router(api_v1.router)
        app.dependency_overrides[api_v1.get_client] = lambda: fake
        app.dependency_overrides[api_v1.get_usage_store] = lambda: store
        limiter = RateLimiter(limit=1000)
        app.dependency_overrides[api_v1.get_rate_limiter] = lambda: limiter
        return app, store, fake

    return build


# 6: blank query

@pytest.mark.parametrize("query", ["   ", "\n\t "])
def test_whitespace_query_is_422_not_500(make_app, query):
    app, _, fake = make_app()
    resp = TestClient(app, raise_server_exceptions=False).post("/v1/search", headers=auth(ALICE),
                                                               json={"query": query})
    assert resp.status_code == 422
    assert fake.calls == []


def test_query_and_goal_are_stripped(make_app):
    app, _, fake = make_app()
    resp = TestClient(app).post("/v1/search", headers=auth(ALICE), json={"query": "  q  ", "goal": "   "})
    assert resp.status_code == 200
    assert fake.calls[0]["query"] == "q" and fake.calls[0]["goal"] is None


# 2: clamps, per-key cap, concurrency, autopsy metering

def test_per_request_limits_are_clamped_to_server_maxima(make_app):
    app, _, fake = make_app()
    TestClient(app).post("/v1/search", headers=auth(ALICE),
                         json={"query": "q", "max_cost_usd": 1.0, "max_rescues": 5})
    assert fake.calls[0]["max_rescues"] == 3
    assert fake.calls[0]["max_cost_usd"] == pytest.approx(0.10)
    TestClient(app).post("/v1/search", headers=auth(ALICE), json={"query": "q", "max_cost_usd": 0.02, "max_rescues": 1})
    assert fake.calls[1]["max_rescues"] == 1 and fake.calls[1]["max_cost_usd"] == pytest.approx(0.02)


def test_key_over_its_cap_gets_402_and_other_keys_are_unaffected(make_app, monkeypatch):
    monkeypatch.setattr(config, "BACKSTOP_KEY_SPEND_CAP_USD", 0.05)
    app, store, fake = make_app(FakeClient(cost_usd=0.02))
    client = TestClient(app)
    codes = [client.post("/v1/search", headers=auth(ALICE), json={"query": "q"}).status_code for _ in range(4)]
    assert codes[:2] == [200, 200] and codes[-1] == 402
    body = client.post("/v1/search", headers=auth(ALICE), json={"query": "q"}).json()
    assert body["detail"]["code"] == "spend_cap_reached"
    assert store.spent(ALICE) <= 0.05 + 0.02
    assert client.post("/v1/search", headers=auth(BOB), json={"query": "q"}).status_code == 200


def test_max_cost_is_cut_to_what_the_key_has_left(make_app, monkeypatch):
    monkeypatch.setattr(config, "BACKSTOP_KEY_SPEND_CAP_USD", 0.05)
    app, store, fake = make_app()
    store.record(ALICE, status="HIT", engine_calls=1, fetches=0, cost_usd=0.03)
    TestClient(app).post("/v1/search", headers=auth(ALICE), json={"query": "q", "max_cost_usd": 0.10})
    assert fake.calls[0]["max_cost_usd"] == pytest.approx(0.02)


async def test_concurrent_requests_cannot_overrun_the_cap(make_app, monkeypatch):
    """With a slow engine, every request used to pass the cap check before any was metered."""
    monkeypatch.setattr(config, "BACKSTOP_KEY_SPEND_CAP_USD", 0.10)
    app, store, fake = make_app(FakeClient(cost_usd=0.03, delay_s=0.2))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        responses = await asyncio.gather(*[
            client.post("/v1/search", headers=auth(ALICE), json={"query": f"q{i}", "max_cost_usd": 0.03})
            for i in range(10)])
    codes = [r.status_code for r in responses]
    # Three full requests, then one cut to the $0.01 left (it still covers one engine call and a check).
    assert codes.count(200) == 4 and codes.count(402) == 6
    assert sorted(call["max_cost_usd"] for call in fake.calls) == pytest.approx([0.01, 0.03, 0.03, 0.03])
    assert store.spent(ALICE) <= 0.10 + 1e-9


def _install_autopsy_fakes(monkeypatch, n_steps: int, cost: float = 0.0005) -> list[str]:
    interactions = [{"interaction_id": f"i{n}", "created_at": f"2026-11-14T19:00:{n:02d}Z",
                     "preprocessor_runs": [{"preprocessor_name": "tavily", "query": f"q{n}", "output_payload": {
                         "results": [{"url": f"https://x.example/{n}", "title": "t", "summary": "s", "rank": 1}]}}],
                     "user_input": {"query": f"q{n}"}, "metadata": {}} for n in range(n_steps)]
    sessions = FakeSessions(history={"interactions": interactions})
    monkeypatch.setattr(autopsy_module, "_telem_client", lambda: FakeAsyncTelem(sessions=sessions))
    checked: list[str] = []

    async def check(query, goal, results, today=None, **kwargs):
        checked.append(query)
        return CheckedVerdict(**verdict("DEAD").model_dump(), cost_usd=cost)

    module = ModuleType("so.checker")
    module.check = check
    monkeypatch.setitem(sys.modules, "so.checker", module)
    return checked


def test_autopsy_checker_spend_is_metered(make_app, monkeypatch):
    checked = _install_autopsy_fakes(monkeypatch, n_steps=4)
    app, store, _ = make_app()
    store.bind_session(ALICE, "sess-a")
    resp = TestClient(app).get("/v1/autopsy/sess-a", headers=auth(ALICE))
    assert resp.status_code == 200 and len(checked) == 4
    totals = store.totals(ALICE)
    assert totals["autopsies"] == 1 and totals["cost_usd"] == pytest.approx(4 * 0.0005)


def test_autopsy_steps_are_limited_by_remaining_cap(make_app, monkeypatch):
    checked = _install_autopsy_fakes(monkeypatch, n_steps=10)
    monkeypatch.setattr(config, "BACKSTOP_KEY_SPEND_CAP_USD", 0.05)
    app, store, _ = make_app()
    store.bind_session(ALICE, "sess-a")
    store.record(ALICE, status="HIT", engine_calls=1, fetches=0, cost_usd=0.047)
    resp = TestClient(app).get("/v1/autopsy/sess-a", headers=auth(ALICE))
    assert resp.status_code == 200 and len(checked) == 3 and resp.json()["truncated"]
    store.record(ALICE, status="HIT", engine_calls=1, fetches=0, cost_usd=0.01)
    resp = TestClient(app).get("/v1/autopsy/sess-a", headers=auth(ALICE))
    assert resp.status_code == 402 and len(checked) == 3


# 3: sessions belong to the key whose search returned them

def test_sessions_are_bound_to_the_key_that_created_them(make_app, monkeypatch):
    _install_autopsy_fakes(monkeypatch, n_steps=1)
    app, store, fake = make_app()
    client = TestClient(app)
    created = client.post("/v1/search", headers=auth(ALICE), json={"query": "q"}).json()["session_id"]
    assert created == "sess-1" and store.owns_session(ALICE, created)

    assert client.get(f"/v1/autopsy/{created}", headers=auth(BOB)).status_code == 404
    stolen = client.post("/v1/search", headers=auth(BOB), json={"query": "q", "session": created})
    assert stolen.status_code == 404 and stolen.json()["detail"]["code"] == "session_not_found"
    assert len(fake.calls) == 1

    assert client.get(f"/v1/autopsy/{created}", headers=auth(ALICE)).status_code == 200
    reused = client.post("/v1/search", headers=auth(ALICE), json={"query": "q2", "session": created})
    assert reused.status_code == 200 and fake.calls[-1]["session"] == created


def test_unknown_session_is_404_like_another_tenants(make_app):
    app, _, fake = make_app()
    client = TestClient(app)
    unknown = client.get("/v1/autopsy/never-seen", headers=auth(ALICE))
    assert unknown.status_code == 404
    assert client.post("/v1/search", headers=auth(ALICE),
                       json={"query": "q", "session": "never-seen"}).status_code == 404
    assert fake.calls == []


def test_usage_store_reservations_and_migration(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    with sqlite3.connect(path) as db:  # a database created before autopsies were metered
        db.execute("""CREATE TABLE usage (key_id TEXT PRIMARY KEY, verified_searches INTEGER NOT NULL DEFAULT 0,
                      hits INTEGER NOT NULL DEFAULT 0, not_found INTEGER NOT NULL DEFAULT 0,
                      unchecked INTEGER NOT NULL DEFAULT 0, engine_calls INTEGER NOT NULL DEFAULT 0,
                      fetches INTEGER NOT NULL DEFAULT 0, cost_usd REAL NOT NULL DEFAULT 0,
                      first_seen TEXT NOT NULL, last_seen TEXT NOT NULL)""")
    store = UsageStore(path)
    store.record_autopsy(ALICE, cost_usd=0.01)
    assert store.totals(ALICE)["autopsies"] == 1
    assert store.reserve(ALICE, 0.5, minimum=0.1, cap=1.0) == 0.5
    assert store.reserve(ALICE, 0.5, minimum=0.1, cap=1.0) == pytest.approx(0.49)
    assert store.reserve(ALICE, 0.5, minimum=0.1, cap=1.0) == 0.0
    store.release(ALICE, 0.5)
    assert store.reserve(ALICE, 0.2, minimum=0.1, cap=1.0) == 0.2
    assert store.bind_session(BOB, "x") and store.bind_session(BOB, "x")
    assert not store.bind_session(ALICE, "x") and not store.owns_session(ALICE, "x")
