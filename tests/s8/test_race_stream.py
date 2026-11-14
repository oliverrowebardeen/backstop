"""O10: POST /race/stream runs a whole live race inside one request (serverless safe)."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from server.app import ServerSettings, create_app
from server.live_race import client_address, live_off_reason, missing_live_keys
from server.race_stream import follow_race
from server.races import LiveRun
from so import config
from so.contracts import AgentAnswer, Question
from so.eventlog import EventLog
from tests.o4.fakes import interrupted_question, run_question
from tests.s8.test_live_race import settings

KEYS = ("TELEM_API_KEY", "TAVILY_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY")


def frames(text: str) -> list[tuple[str, dict]]:
    """Parse an SSE body into (event, data) pairs, skipping comments."""
    out = []
    for block in text.split("\n\n"):
        lines = [line for line in block.split("\n") if line and not line.startswith(":")]
        if not lines:
            continue
        kind = next(line[7:] for line in lines if line.startswith("event: "))
        data = json.loads("".join(line[6:] for line in lines if line.startswith("data: ")))
        out.append((kind, data))
    return out


def test_one_request_streams_both_sides_to_the_end(tmp_path):
    with TestClient(create_app(settings=settings(tmp_path), runner=run_question)) as client:
        response = client.post("/race/stream", json={"question": "Who founded Telem?",
                                                     "left": "telem_default", "right": "second_opinion"})
        assert response.status_code == 200 and response.headers["content-type"].startswith("text/event-stream")
    rows = frames(response.text)
    assert rows[0] == ("race", {"question": "Who founded Telem?", "left": "telem_default",
                                "right": "second_opinion", "race_max_usd": None, "race_timeout_s": None})
    assert rows[-1] == ("done", {})
    for side, setup in (("left", "telem_default"), ("right", "second_opinion")):
        events = [data["event"] for kind, data in rows if kind == "log" and data["side"] == side]
        assert [event["type"] for event in events] == ["run_start", "search", "answer", "run_end"]
        assert {event["setup"] for event in events} == {setup}
        assert [event["seq"] for event in events] == sorted(event["seq"] for event in events)
        ends = [data for kind, data in rows if kind in {"complete", "stream_error"} and data["side"] == side]
        assert ends == [{"side": side, "run_id": events[0]["run_id"]}]
        assert rows.index(("complete", ends[0])) > max(i for i, (k, d) in enumerate(rows) if k == "log" and d["side"] == side)


def test_validation_and_guards_match_the_two_step_race(tmp_path):
    app = create_app(settings=settings(tmp_path, demo_code="open-sesame", race_rate_per_hour=1), runner=run_question)
    code = {"X-Backstop-Demo-Code": "open-sesame"}
    with TestClient(app) as client:
        assert client.post("/race/stream", json={"question": "Q?"}).json()["detail"]["code"] == "demo_code_required"
        assert client.post("/race/stream", json={"question": "Q?", "left": "single", "right": "single"},
                           headers=code).status_code == 422
        assert client.post("/race/stream", json={"question": "x" * 301}, headers=code).status_code == 422
        assert client.post("/race/stream", json={"question": "Q?"}, headers=code).status_code == 200
        limited = client.post("/race/stream", json={"question": "Again?"}, headers=code)
        assert limited.status_code == 429 and limited.json()["detail"]["code"] == "rate_limited"


def test_a_failed_side_ends_with_its_reason_and_the_other_still_completes(tmp_path):
    async def mixed(question: Question, setup: str, *, log_dir: str) -> AgentAnswer:
        runner = interrupted_question if setup == "telem_default" else run_question
        return await runner(question, setup, log_dir=log_dir)

    with TestClient(create_app(settings=settings(tmp_path), runner=mixed)) as client:
        rows = frames(client.post("/race/stream", json={"question": "Q?", "left": "telem_default",
                                                        "right": "second_opinion"}).text)
    ends = {data["side"]: (kind, data) for kind, data in rows if kind in {"complete", "stream_error"}}
    assert ends["right"][0] == "complete"
    assert ends["left"][0] == "stream_error" and ends["left"][1]["message"]
    assert rows[-1] == ("done", {})


def test_cost_cap_stops_both_sides_inside_the_stream(tmp_path):
    async def spender(question: Question, setup: str, *, log_dir: str) -> AgentAnswer:
        log = EventLog(f"{setup}-{question.id}-{uuid4().hex[:6]}", question.id, setup, log_dir=log_dir)
        log.emit("run_start", question=question.question)
        log.emit("search", query="q", goal="g", engines=["tavily"], n_results=5, latency_ms=1, cost_usd=.2, cached=False)
        try:
            await asyncio.sleep(30)
        finally:
            log.emit("run_end", engine_calls=1, fetches=0, searches=1, cost_usd_search=.2, cost_usd_llm=0,
                     cost_usd_total=.2, latency_ms=1, error="CancelledError")
        raise AssertionError("the cost cap should have stopped this run")

    with TestClient(create_app(settings=settings(tmp_path, race_max_usd=.3), runner=spender)) as client:
        rows = frames(client.post("/race/stream", json={"question": "Spend"}).text)
    errors = [data for kind, data in rows if kind == "stream_error"]
    assert {data["side"] for data in errors} == {"left", "right"}
    assert all(data["error_type"] == "RaceCostCap" and "Stopped at cost cap" in data["message"] for data in errors)


async def test_leaving_the_stream_cancels_both_agents(tmp_path):
    started = asyncio.Event()

    async def slow(directory: Path) -> AgentAnswer:
        log = EventLog(f"x-{uuid4().hex[:6]}", "q", "single", log_dir=str(directory))
        log.emit("run_start", question="Q?")
        started.set()
        await asyncio.sleep(30)
        raise AssertionError("should have been cancelled")

    jobs = {}
    for side in ("left", "right"):
        directory = tmp_path / side
        directory.mkdir()
        jobs[side] = LiveRun(directory, asyncio.create_task(slow(directory)))
    await started.wait()
    disconnected = False

    async def is_disconnected() -> bool:
        return disconnected

    stream = follow_race(jobs, poll_seconds=.01, keepalive_seconds=10, is_disconnected=is_disconnected)
    kind, data = await anext(stream)
    assert kind == "log" and data["event"]["type"] == "run_start"
    disconnected = True
    with pytest.raises(StopAsyncIteration):
        while True:
            await anext(stream)
    await asyncio.gather(*(job.task for job in jobs.values()), return_exceptions=True)
    assert all(job.task.cancelled() for job in jobs.values())


def test_live_is_off_with_a_plain_reason_until_keys_are_set(tmp_path, monkeypatch):
    for key in KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(config, "CHECKER_BACKEND", "cascade")
    monkeypatch.setattr(config, "LLM_PROVIDER", "anthropic")
    monkeypatch.setattr(config, "CHECKER_PROVIDER", "anthropic")
    assert missing_live_keys() == KEYS
    app = create_app(settings=settings(tmp_path, missing_keys=missing_live_keys()), runner=run_question)
    with TestClient(app) as client:
        data = client.get("/config").json()
        assert data["live_available"] is False and data["race_access"] == "replay"
        assert data["live_reason"] == ("Live races are off on this server because TELEM_API_KEY, TAVILY_API_KEY, "
                                       "ANTHROPIC_API_KEY and OPENAI_API_KEY are not set, so this section plays recorded races.")
        refused = client.post("/race/stream", json={"question": "Q?"})
        assert refused.status_code == 403 and "TELEM_API_KEY" in refused.json()["detail"]["message"]
    for key in KEYS:
        monkeypatch.setenv(key, "set")
    assert missing_live_keys() == ()
    assert live_off_reason(True, ()) is None
    assert "BACKSTOP_LIVE_RACE" in live_off_reason(False, ())
    with TestClient(create_app(settings=settings(tmp_path, live_races=False), runner=run_question)) as client:
        assert "BACKSTOP_LIVE_RACE" in client.get("/config").json()["live_reason"]


def test_vercel_uses_the_platform_client_ip_and_tmp_logs(monkeypatch):
    request = SimpleNamespace(headers={"x-vercel-forwarded-for": "203.0.113.7", "x-forwarded-for": "198.51.100.1"},
                              client=SimpleNamespace(host="10.0.0.1"))
    monkeypatch.delenv("VERCEL", raising=False)
    assert client_address(request) == "10.0.0.1"  # off Vercel, forwarded headers are not trusted
    monkeypatch.setenv("VERCEL", "1")
    assert client_address(request) == "203.0.113.7"
    monkeypatch.delenv("SERVER_LOG_DIR", raising=False)
    monkeypatch.delenv("SO_LOG_DIR", raising=False)
    assert ServerSettings.from_config().log_dir.parts[-2:] == ("backstop", "logs")
    assert str(ServerSettings.from_config().log_dir).startswith(("/tmp", "/var", "/private"))


def test_vercel_function_fits_a_race():
    root = Path(__file__).resolve().parents[2]
    functions = json.loads((root / "vercel.json").read_text())["functions"]
    assert functions["server/app.py"]["maxDuration"] >= 90 + 10  # the 90 s race limit plus start-up
    assert 'entrypoint = "server.app:app"' in (root / "pyproject.toml").read_text()


def test_shipped_replays_state_how_they_were_recorded_and_claim_no_selection():
    root = Path(__file__).resolve().parents[2]
    from server.app import replay_catalog
    shipped = replay_catalog(root / "none", None, root / "ui" / "fixtures")
    assert shipped
    for name, pair in shipped.items():
        meta = pair.summary()
        assert meta["commit"] and meta["recorded_at"], name
        assert (meta["checker_backend"], meta["first_engine_routing"], meta["cache"]) == ("cascade", "on", "off"), name
        assert "seed" not in meta and "selection" not in meta, name
    for manifest in (root / "ui" / "fixtures").glob("race-*.json"):
        text = manifest.read_text().lower()
        assert not any(word in text for word in ("seed", "selection", "random", "whatever the outcome")), manifest.name
