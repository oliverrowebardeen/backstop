"""POST /v1/memo-check (API key, metering) and POST /memo-check (live race demo guard), with fakes only."""
from __future__ import annotations

from pathlib import Path
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server import api_v1
from server.app import ServerSettings, create_app
from server.usage import RateLimiter, UsageStore
from so import claims, config
from tests.o4.fakes import run_question
from tests.s10.fakes import CLAIMS, MEMO, Fakes, result, verdict

KEY = "bk_test_memo"
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Fakes:
    monkeypatch.setattr(config, "SO_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setenv("BACKSTOP_API_KEYS", KEY)
    f = Fakes().install(monkeypatch)
    f.first = {c["query"]: [result("https://en.wikipedia.org/wiki/Stripe,_Inc.", "Stripe facts")] for c in CLAIMS}
    f.verify = {CLAIMS[0]["text"]: [verdict("VERIFIED", 1, "Stripe facts")]}
    return f


def api_client(tmp_path, limit: int = 60) -> tuple[TestClient, UsageStore]:
    store = UsageStore(tmp_path / "usage.db")
    app = FastAPI()
    app.include_router(api_v1.router)
    app.dependency_overrides[api_v1.get_usage_store] = lambda: store
    limiter = RateLimiter(limit=limit)
    app.dependency_overrides[api_v1.get_rate_limiter] = lambda: limiter
    return TestClient(app), store


def test_v1_memo_check_401_422_200(fakes: Fakes, tmp_path) -> None:
    client, store = api_client(tmp_path)
    assert client.post("/v1/memo-check", json={"text": MEMO}).status_code == 401
    assert client.post("/v1/memo-check", json={"text": MEMO},
                       headers={"Authorization": "Bearer wrong"}).status_code == 401
    auth = {"Authorization": f"Bearer {KEY}"}
    for body in ({"text": ""}, {"text": "   "}, {"text": "x" * (claims.MAX_MEMO_CHARS + 1)}, {}, {"text": MEMO, "x": 1}):
        assert client.post("/v1/memo-check", json=body, headers=auth).status_code == 422
    response = client.post("/v1/memo-check", json={"text": MEMO}, headers=auth)
    assert response.status_code == 200
    report = claims.MemoReport.model_validate(response.json())
    assert len(report.claims) == 4 and report.totals.verified == 1 and report.run_id.startswith("memo-")
    totals = store.totals(KEY)
    assert totals["verified_searches"] == 1 and totals["cost_usd"] == pytest.approx(report.cost_usd)
    assert report.telem_session_id == "sess-1" and store.owns_session(KEY, "sess-1")


def test_v1_memo_check_rate_limit(fakes: Fakes, tmp_path) -> None:
    client, _ = api_client(tmp_path, limit=1)
    auth = {"Authorization": f"Bearer {KEY}"}
    assert client.post("/v1/memo-check", json={"text": MEMO}, headers=auth).status_code == 200
    assert client.post("/v1/memo-check", json={"text": MEMO}, headers=auth).status_code == 429


def test_v1_memo_check_spend_cap(fakes: Fakes, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "BACKSTOP_KEY_SPEND_CAP_USD", 0.0001)
    client, _ = api_client(tmp_path)
    response = client.post("/v1/memo-check", json={"text": MEMO}, headers={"Authorization": f"Bearer {KEY}"})
    assert response.status_code == 402


def settings(tmp_path: Path, **overrides) -> ServerSettings:
    values = dict(live_races=True, race_access="demo", ui_dir=ROOT / "ui", demo_dir=tmp_path / "demo",
                  fixture_dir=None, log_dir=tmp_path / "logs", results_path=tmp_path / "results.json",
                  frontier_path=tmp_path / "frontier.json", max_active_runs=8, max_question_chars=2000,
                  start_timeout=2, run_timeout=5, poll_seconds=.005, keepalive_seconds=.1)
    return ServerSettings(**(values | overrides))


def test_demo_route_reuses_the_live_race_guard(fakes: Fakes, tmp_path) -> None:
    app = create_app(settings=settings(tmp_path, demo_code="open-sesame", race_rate_per_hour=1), runner=run_question)
    with TestClient(app) as client:
        denied = client.post("/memo-check", json={"text": MEMO})
        assert denied.status_code == 401 and denied.json()["detail"]["code"] == "demo_code_required"
        headers = {"X-Backstop-Demo-Code": "open-sesame"}
        assert client.post("/memo-check", json={"text": ""}, headers=headers).status_code == 422
        ok = client.post("/memo-check", json={"text": MEMO}, headers=headers)
        assert ok.status_code == 200 and ok.json()["totals"]["verified"] == 1
        # The same per-IP hourly limit as live races: the memo check used this address's one race.
        limited = client.post("/race", json={"question": "Q?"}, headers=headers)
        assert limited.status_code == 429 and limited.json()["detail"]["code"] == "rate_limited"


def test_demo_route_is_off_when_live_races_are_off(fakes: Fakes, tmp_path) -> None:
    with TestClient(create_app(settings=settings(tmp_path, live_races=False), runner=run_question)) as client:
        response = client.post("/memo-check", json={"text": MEMO})
        assert response.status_code == 403 and response.json()["detail"]["code"] == "live_disabled"


def test_demo_route_needs_a_key_outside_public_mode(fakes: Fakes, tmp_path, monkeypatch) -> None:
    app = create_app(settings=settings(tmp_path, race_access="api_key"), runner=run_question)
    app.dependency_overrides[api_v1.get_usage_store] = lambda: UsageStore(tmp_path / "usage.db")
    with TestClient(app) as client:
        assert client.post("/memo-check", json={"text": MEMO}).status_code == 401
        ok = client.post("/memo-check", json={"text": MEMO}, headers={"Authorization": f"Bearer {KEY}"})
        assert ok.status_code == 200


def test_recordings_are_listed_and_served(fakes: Fakes, tmp_path) -> None:
    folder = tmp_path / "demo" / "memo_examples"
    folder.mkdir(parents=True)
    (folder / "acme.json").write_text(json.dumps({"run_id": "memo-x", "recorded": {"label": "Acme", "memo_id": "acme"}}))
    with TestClient(create_app(settings=settings(tmp_path), runner=run_question)) as client:
        assert client.get("/memo/recordings").json() == [{"name": "acme", "label": "Acme", "memo_id": "acme"}]
        assert client.get("/memo/recordings/acme").json()["run_id"] == "memo-x"
        assert client.get("/memo/recordings/..%2Fsecret").status_code == 404
        assert client.get("/memo/recordings/missing").status_code == 404


def test_example_memos_and_recordings_are_valid() -> None:
    memos = json.loads((ROOT / "tests" / "s10" / "example_memos.json").read_text())["memos"]
    assert 2 <= len(memos) <= 3
    for memo in memos:
        assert len(memo["text"]) <= claims.MAX_MEMO_CHARS and "—" not in memo["text"]
    for path in (ROOT / "demo" / "memo_examples").glob("*.json"):
        claims.MemoReport.model_validate(json.loads(path.read_text()))
