"""public live races: opt-in, demo code, capacity, per-IP limit, cost cap, recordings."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from server.app import ServerSettings, create_app, replay_catalog
from so.contracts import AgentAnswer, Question
from so.eventlog import EventLog
from tests.o4.fakes import run_question

ROOT = Path(__file__).resolve().parents[2]


def settings(tmp_path: Path, **overrides) -> ServerSettings:
    values = dict(live_races=True, race_access="demo", ui_dir=ROOT / "ui", demo_dir=tmp_path / "demo",
                  fixture_dir=None, log_dir=tmp_path / "logs", results_path=tmp_path / "results.json",
                  frontier_path=tmp_path / "frontier.json", max_active_runs=8, max_question_chars=2000,
                  start_timeout=2, run_timeout=5, poll_seconds=.005, keepalive_seconds=.1)
    return ServerSettings(**(values | overrides))


def test_env_switch_selects_public_mode(monkeypatch):
    monkeypatch.setenv("BACKSTOP_LIVE_RACE", "true")
    monkeypatch.setenv("BACKSTOP_RACE_MAX_USD", "0.25")
    configured = ServerSettings.from_config()
    assert configured.live_races and configured.race_access == "demo" and configured.race_max_usd == .25
    monkeypatch.setenv("BACKSTOP_LIVE_RACE", "false")
    assert ServerSettings.from_config().live_races is False


def test_public_race_needs_no_key_and_validates(tmp_path):
    with TestClient(create_app(settings=settings(tmp_path), runner=run_question)) as client:
        config = client.get("/config").json()
        assert config["race_access"] == "demo" and config["max_question_chars"] == 300
        assert client.post("/race", json={"question": "Q?", "left": "single", "right": "single"}).status_code == 422
        assert client.post("/race", json={"question": "x" * 301}).status_code == 422
        response = client.post("/race", json={"question": "Q?", "left": "telem_default", "right": "second_opinion"})
        assert response.status_code == 200 and set(response.json()) == {"left_run_id", "right_run_id"}


def test_demo_code_and_per_ip_limit(tmp_path):
    app = create_app(settings=settings(tmp_path, demo_code="open-sesame", race_rate_per_hour=1), runner=run_question)
    with TestClient(app) as client:
        denied = client.post("/race", json={"question": "Q?"})
        assert denied.status_code == 401 and denied.json()["detail"]["code"] == "demo_code_required"
        headers = {"X-Backstop-Demo-Code": "open-sesame"}
        assert client.post("/race", json={"question": "Q?"}, headers=headers).status_code == 200
        limited = client.post("/race", json={"question": "Again?"}, headers=headers)
        assert limited.status_code == 429 and limited.json()["detail"]["code"] == "rate_limited"


async def test_busy_server_and_cost_cap(tmp_path):
    release = asyncio.Event()

    async def spender(question: Question, setup: str, *, log_dir: str) -> AgentAnswer:
        log = EventLog(f"{setup}-{question.id}-{uuid4().hex[:6]}", question.id, setup, log_dir=log_dir)
        log.emit("run_start", question=question.question)
        log.emit("search", query="q", goal="g", engines=["tavily"], n_results=5, latency_ms=1, cost_usd=.2, cached=False)
        await release.wait()
        raise AssertionError("the cost cap should have stopped this run")

    import httpx
    app = create_app(settings=settings(tmp_path, race_max_usd=.3, max_races=1), runner=spender)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        started = await client.post("/race", json={"question": "Spend"})
        assert started.status_code == 200
        busy = await client.post("/race", json={"question": "Another"})
        assert busy.status_code == 429 and busy.json()["detail"]["code"] == "busy"
        await asyncio.sleep(1)  # both sides spent $0.40 of $0.30; the watcher stops them
        errors = [json.loads(p.read_text()) for p in (tmp_path / "logs").rglob("*.error.json")]
        assert len(errors) == 2 and all(e["error_type"] == "RaceCostCap" for e in errors)
        assert "Stopped at cost cap" in errors[0]["message"]
        release.set()


def test_save_recording_and_shipped_recordings(tmp_path):
    with TestClient(create_app(settings=settings(tmp_path), runner=run_question)) as client:
        runs = client.post("/race", json={"question": "Save me?"}).json()
        for run_id in runs.values():
            assert "complete" in client.get(f"/stream/{run_id}").text
        saved = client.post("/recordings", json=runs)
        assert saved.status_code == 200
        assert saved.json()["name"] in [p["name"] for p in client.get("/replays").json()]
    shipped = replay_catalog(tmp_path / "none", None, ROOT / "ui" / "fixtures")
    assert shipped and not any(pair.fixture for pair in shipped.values())


def test_api_key_mode_is_unchanged(tmp_path):
    with TestClient(create_app(settings=replace(settings(tmp_path), race_access="api_key"), runner=run_question)) as client:
        assert client.post("/race", json={"question": "Q?"}).status_code == 401
