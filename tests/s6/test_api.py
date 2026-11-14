"""hosted /v1 API with bearer auth, metering, rate limits and JSON errors."""

from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import api_v1
from server.usage import RateLimiter, UsageStore, key_id
from so import config
from so.client import BackstopTelem
from so.second_opinion import TelemUnavailable
from tests.s6.test_client import FakeAsyncTelem, install_checker
from tests.s6.fakes import verdict

KEY = "bk_test_alpha"
OTHER = "bk_test_beta"


class RaisingTelem(FakeAsyncTelem):
    def __init__(self, exc: Exception) -> None:
        super().__init__()
        self.exc = exc

    async def search(self, query, **kwargs):
        raise self.exc


@pytest.fixture
def make_app(monkeypatch, tmp_path):
    monkeypatch.setenv("BACKSTOP_API_KEYS", f"{KEY}, {OTHER}")

    def build(telem=None, limit: int = 60) -> tuple[TestClient, UsageStore, FakeAsyncTelem]:
        fake = telem or FakeAsyncTelem()
        store = UsageStore(tmp_path / "usage.db")
        app = FastAPI()
        app.include_router(api_v1.router)
        app.dependency_overrides[api_v1.get_client] = lambda: BackstopTelem(telem=fake)
        app.dependency_overrides[api_v1.get_usage_store] = lambda: store
        limiter = RateLimiter(limit=limit)
        app.dependency_overrides[api_v1.get_rate_limiter] = lambda: limiter
        return TestClient(app), store, fake

    return build


def auth(key: str = KEY) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def test_health_needs_no_key(make_app):
    client, _, _ = make_app()
    assert client.get("/health").json() == {"status": "ok", "version": "0.1.0"}


def test_search_returns_verdict_and_meters_usage(make_app, monkeypatch):
    install_checker(monkeypatch, [verdict("DEAD", rewrite="q2"), verdict("HIT", evidence_rank=2)])
    client, store, fake = make_app()
    response = client.post("/v1/search", headers=auth(),
                           json={"query": "Stripe 2023 valuation", "goal": "find the valuation", "max_rescues": 1})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "HIT" and body["verdict"] == "HIT"
    assert body["results"][0]["rank_reason"] == "evidence"
    assert {"by_provider", "trace", "engine_calls", "cost_usd", "latency_ms", "why"} <= set(body)
    assert fake.calls[0]["goal"] == "find the valuation"
    usage = client.get("/v1/usage", headers=auth()).json()
    assert usage["verified_searches"] == 1 and usage["hits"] == 1
    assert usage["engine_calls"] == 2 and usage["cost_usd"] == pytest.approx(0.012)
    assert usage["key_id"] == key_id(KEY) and KEY not in str(usage)
    assert client.get("/v1/usage", headers=auth(OTHER)).json()["verified_searches"] == 0


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": f"Basic {KEY}"},
                                     {"Authorization": "Bearer "}])
def test_bad_or_missing_key_is_401(make_app, headers):
    client, _, _ = make_app()
    for response in (client.post("/v1/search", headers=headers, json={"query": "q"}),
                     client.get("/v1/usage", headers=headers)):
        assert response.status_code == 401
        assert response.json()["detail"]["code"] == "unauthorized"
        assert response.headers["www-authenticate"] == "Bearer"


def test_no_configured_keys_rejects_everyone(make_app, monkeypatch):
    client, _, _ = make_app()
    monkeypatch.setenv("BACKSTOP_API_KEYS", "")
    assert client.post("/v1/search", headers=auth(), json={"query": "q"}).status_code == 401


@pytest.mark.parametrize("body", [{}, {"query": ""}, {"query": "q", "max_rescues": -1},
                                  {"query": "q", "unknown": 1}, {"query": "q", "timeout_s": 0}])
def test_bad_body_is_422(make_app, body):
    client, _, _ = make_app()
    response = client.post("/v1/search", headers=auth(), json=body)
    assert response.status_code == 422 and "detail" in response.json()


def test_rate_limit_is_429_per_key(make_app, monkeypatch):
    install_checker(monkeypatch, [verdict("HIT", evidence_rank=1)] * 3)
    client, _, _ = make_app(limit=2)
    codes = [client.post("/v1/search", headers=auth(), json={"query": "q"}).status_code for _ in range(3)]
    assert codes == [200, 200, 429]
    limited = client.post("/v1/search", headers=auth(), json={"query": "q"})
    assert limited.json()["detail"]["code"] == "rate_limited" and int(limited.headers["retry-after"]) >= 1
    assert client.post("/v1/search", headers=auth(OTHER), json={"query": "q"}).status_code == 200


def test_telem_failure_is_502(make_app, monkeypatch):
    install_checker(monkeypatch, [])
    client, store, _ = make_app(telem=RaisingTelem(RuntimeError("router down")))
    response = client.post("/v1/search", headers=auth(), json={"query": "q"})
    assert response.status_code == 502 and response.json()["detail"]["code"] == "telem_unavailable"
    assert store.totals(KEY)["verified_searches"] == 0


def test_budget_exhausted_is_503(make_app, monkeypatch):
    install_checker(monkeypatch, [])
    client, _, _ = make_app(telem=RaisingTelem(config.BudgetExceeded("spent")))
    assert client.post("/v1/search", headers=auth(), json={"query": "q"}).status_code == 503


def test_keys_are_never_logged(make_app, monkeypatch, caplog):
    install_checker(monkeypatch, [])
    client, _, _ = make_app(telem=RaisingTelem(TelemUnavailable("down")))
    with caplog.at_level(logging.DEBUG):
        client.post("/v1/search", headers=auth(), json={"query": "q"})
        client.post("/v1/search", headers=auth("bk_wrong_secret"), json={"query": "q"})
    assert KEY not in caplog.text and "bk_wrong_secret" not in caplog.text


def test_usage_store_tracks_statuses_and_hashes_keys(tmp_path):
    store = UsageStore(tmp_path / "u.db")
    store.record(KEY, status="NOT_FOUND", engine_calls=3, fetches=1, cost_usd=0.02)
    store.record(KEY, status="UNCHECKED", engine_calls=1, fetches=0, cost_usd=0.005)
    totals = store.totals(KEY)
    assert (totals["not_found"], totals["unchecked"], totals["engine_calls"], totals["fetches"]) == (1, 1, 4, 1)
    assert KEY.encode() not in (tmp_path / "u.db").read_bytes()
    with pytest.raises(ValueError):
        store.record(KEY, status="MAYBE", engine_calls=0, fetches=0, cost_usd=0)


def test_rate_limiter_window_slides():
    limiter = RateLimiter(limit=2, window_s=60)
    assert limiter.allow(KEY, now=0)[0] and limiter.allow(KEY, now=1)[0]
    allowed, retry = limiter.allow(KEY, now=2)
    assert not allowed and retry == pytest.approx(58)
    assert limiter.allow(KEY, now=60.5)[0]


def test_main_app_mounts_the_hosted_api():
    from server.app import app

    client = TestClient(app)
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/v1/usage").status_code == 401
    assert client.post("/v1/search", json={"query": "q"}).status_code == 401
