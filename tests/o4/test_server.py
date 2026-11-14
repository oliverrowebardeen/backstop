"""Exercise the same contract streams consumed by the browser, without paid calls."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import shutil

from fastapi.testclient import TestClient
import pytest

from server.app import ServerSettings, create_app, read_events, replay_catalog
from so.config import ROOT
from tests.o4.fakes import interrupted_question, run_question


def messages(response):
    parsed = []
    for block in response.text.split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        if "data" in fields:
            parsed.append((fields["event"], json.loads(fields["data"])))
    return parsed


@pytest.fixture
def root(tmp_path):
    shutil.copytree(ROOT / "tests/o4/fixtures", tmp_path / "tests/o4/fixtures")
    return tmp_path


def settings(root: Path, **overrides) -> ServerSettings:
    values = dict(ui_dir=ROOT / "ui", demo_dir=root / "demo",
                  fixture_dir=root / "tests/o4/fixtures", log_dir=root / "out/logs",
                  results_path=root / "out/results.json", frontier_path=root / "out/frontier.json",
                  max_active_runs=8, max_question_chars=2000, start_timeout=1,
                  run_timeout=2, poll_seconds=.005, keepalive_seconds=.1)
    return ServerSettings(**(values | overrides))


def test_replay_preserves_all_events_and_original_timing(root):
    waits = []

    async def sleep(delay):
        waits.append(delay)
        await asyncio.sleep(delay)

    with TestClient(create_app(settings=settings(root), replay_sleep=sleep)) as client:
        pairs = client.get("/replays").json()
        assert len(pairs) == 1
        assert pairs[0]["fixture"] is True
        response = client.get("/replay/fixture-stripe")
        assert response.headers["content-type"].startswith("text/event-stream")
        rows = messages(response)
        assert rows[0][0] == "replay"
        assert rows[-1][0] == "complete"
        for side, setup in (("left", "single"), ("right", "second_opinion")):
            actual = [data["event"] for kind, data in rows if kind == "log" and data["side"] == side]
            expected = [event.model_dump() for event in read_events(root / f"tests/o4/fixtures/{setup}.jsonl")]
            assert actual == expected
        assert sum(waits) == pytest.approx(3.7, abs=.1)
        assert len(waits) == 25


def test_replay_catalog_ignores_traversal_and_non_pair_manifests(root):
    directory = root / "demo"
    directory.mkdir()
    (directory / "unsafe.json").write_text(json.dumps({"left": "../tests/o4/fixtures/single.jsonl", "right": "../tests/o4/fixtures/second_opinion.jsonl"}))
    (directory / "golden.json").write_text('[{"question": "a golden question"}]')
    with pytest.raises(ValueError, match="unsafe.json"):
        replay_catalog(directory, root / "tests/o4/fixtures")
    (directory / "unsafe.json").unlink()
    assert list(replay_catalog(directory, root / "tests/o4/fixtures")) == ["fixture-stripe"]


def test_unknown_runs_and_replays_are_404(root):
    with TestClient(create_app(settings=settings(root))) as client:
        assert client.get("/stream/unknown").status_code == 404
        assert client.get("/stream/..secret").status_code == 404
        assert client.get("/replay/unknown").status_code == 404


def test_ui_and_assets_are_available(root):
    with TestClient(create_app(settings=settings(root))) as client:
        assert 'id="race-form"' in client.get("/").text
        assert client.get("/ui/app.js").status_code == 200
        assert client.get("/ui/style.css").status_code == 200
        assert client.get("/ui/race-state.js").status_code == 200
        assert client.get("/ui/../so/config.py").status_code == 404


def test_live_race_returns_agent_owned_ids_and_streams_both_runs(root):
    with TestClient(create_app(settings=settings(root), runner=run_question)) as client:
        response = client.post("/race", json={"question": "Test question", "left": "single", "right": "second_opinion"})
        assert response.status_code == 200
        ids = response.json()
        assert ids["left_run_id"] != ids["right_run_id"]
        for run_id in ids.values():
            rows = messages(client.get(f"/stream/{run_id}"))
            events = [data for kind, data in rows if kind == "log"]
            assert len(events) == 4
            assert all(event["run_id"] == run_id for event in events)
            assert events[-1]["data"]["cost_usd_total"] == .005
            assert rows[-1][0] == "complete"
            resumed = messages(client.get(f"/stream/{run_id}", headers={"Last-Event-ID": "3"}))
            assert [data["seq"] for kind, data in resumed if kind == "log"] == [4]
            assert client.get(f"/stream/{run_id}", headers={"Last-Event-ID": "invalid"}).status_code == 400


def test_same_setup_on_both_sides_still_gets_distinct_logs(root):
    with TestClient(create_app(settings=settings(root), runner=run_question)) as client:
        ids = client.post("/race", json={"question": "Same setup", "left": "single", "right": "single"}).json()
        assert ids["left_run_id"] != ids["right_run_id"]
        assert len(list((root / "out/logs").rglob("*.jsonl"))) == 2


@pytest.mark.parametrize("body", [{"question": " "}, {"question": ""}, {"question": "test", "left": "invalid"}, {"question": "x" * 2001}])
def test_invalid_races_are_rejected(root, body):
    with TestClient(create_app(settings=settings(root), runner=run_question)) as client:
        assert client.post("/race", json=body).status_code == 422


def test_interrupted_agent_produces_stream_error(root):
    with TestClient(create_app(settings=settings(root), runner=interrupted_question)) as client:
        ids = client.post("/race", json={"question": "Interrupt"}).json()
        rows = messages(client.get(f"/stream/{ids['left_run_id']}"))
        assert rows[-1][0] == "stream_error"
        assert not any(kind == "complete" for kind, _ in rows)


def test_failed_agent_start_is_a_service_error(root):
    async def failed(*args, **kwargs):
        raise RuntimeError("Private provider error")

    with TestClient(create_app(settings=settings(root), runner=failed)) as client:
        response = client.post("/race", json={"question": "Fail"})
        assert response.status_code == 503
        assert "Private provider error" not in response.text


def test_results_missing_is_explicit_and_not_fabricated(root):
    with TestClient(create_app(settings=settings(root))) as client:
        assert client.get("/results").json() == {"results": None, "frontier": None}


def test_results_validates_both_artifacts(root):
    out = root / "out"
    out.mkdir()
    (out / "results.json").write_text(json.dumps({"generated_at": "2026-11-14", "n_test": 200, "n_traps": 30, "setups": {"single": {"correct": 100, "wrong": 60, "declined": 40}}}))
    (out / "frontier.json").write_text(json.dumps({"split": "test", "points": [{"kind": "second_opinion", "coverage": .8, "cost_per_q": .01}]}))
    with TestClient(create_app(settings=settings(root))) as client:
        payload = client.get("/results").json()
        assert payload["results"]["n_test"] == 200
        assert payload["results"]["n_traps"] == 30
        assert payload["frontier"]["points"][0]["coverage"] == .8
        (out / "frontier.json").write_text('{"split": "learn", "points": []}')
        assert client.get("/results").status_code == 503


def test_persisted_log_can_be_streamed_after_restart(root):
    logs = root / "out/logs"
    logs.mkdir(parents=True)
    shutil.copy(root / "tests/o4/fixtures/single.jsonl", logs / "single-fixture-stripe-o4.jsonl")
    with TestClient(create_app(settings=settings(root))) as client:
        rows = messages(client.get("/stream/single-fixture-stripe-o4"))
        assert len([kind for kind, _ in rows if kind == "log"]) == 8
        assert rows[-1][0] == "complete"


def test_fixture_recordings_are_opt_in(root):
    with TestClient(create_app(settings=settings(root, fixture_dir=None))) as client:
        assert client.get("/replays").json() == []
        assert client.get("/replay/fixture-stripe").status_code == 404


def test_invalid_recording_is_an_explicit_service_error(root):
    (root / "tests/o4/fixtures/single.jsonl").write_text('{"invalid": true}\n')
    with TestClient(create_app(settings=settings(root))) as client:
        response = client.get("/replays")
        assert response.status_code == 503
        assert "Invalid replay manifest" in response.json()["detail"]


def test_run_capacity_is_enforced_and_timeout_ends_the_stream(root):
    async def stalled(question, setup, *, log_dir):
        from so.eventlog import EventLog
        log = EventLog(f"{setup}-{question.id}-stalled", question.id, setup, log_dir=log_dir)
        log.emit("run_start", question=question.question)
        await asyncio.Event().wait()

    configured = settings(root, max_active_runs=2, run_timeout=.15)
    with TestClient(create_app(settings=configured, runner=stalled)) as client:
        ids = client.post("/race", json={"question": "First"}).json()
        assert client.post("/race", json={"question": "Second"}).status_code == 429
        rows = messages(client.get(f"/stream/{ids['left_run_id']}"))
        assert rows[-1][0] == "stream_error"


def test_start_timeout_returns_a_clear_error(root):
    async def stalled(*args, **kwargs):
        await asyncio.Event().wait()

    with TestClient(create_app(settings=settings(root, start_timeout=.02), runner=stalled)) as client:
        response = client.post("/race", json={"question": "Start"})
        assert response.status_code == 503
        assert "could not start" in response.json()["detail"]


def test_settings_and_question_limits_are_used_by_api(root):
    configured = settings(root, max_question_chars=8)
    with TestClient(create_app(settings=configured, runner=run_question)) as client:
        assert client.get("/config").json()["max_question_chars"] == 8
        assert client.post("/race", json={"question": "ninechars"}).status_code == 422
    with pytest.raises(ValueError):
        replace(configured, poll_seconds=0)


def test_stream_waits_for_a_complete_jsonl_line(root):
    async def partial(question, setup, *, log_dir):
        from so.contracts import AgentAnswer
        from so.eventlog import EventLog

        log = EventLog(f"{setup}-{question.id}-partial", question.id, setup, log_dir=log_dir)
        start = log.emit("run_start", question=question.question)
        await asyncio.sleep(.03)
        final = start.model_copy(update={"seq": 2, "type": "run_end", "data": {
            "engine_calls": 0, "fetches": 0, "searches": 0, "cost_usd_search": 0,
            "cost_usd_llm": 0, "cost_usd_total": 0, "latency_ms": 40}})
        line = final.model_dump_json() + "\n"
        middle = len(line) // 2
        with log.path.open("a") as handle:
            handle.write(line[:middle])
        await asyncio.sleep(.03)
        with log.path.open("a") as handle:
            handle.write(line[middle:])
        return AgentAnswer(final="NOT FOUND", status="NOT_FOUND")

    with TestClient(create_app(settings=settings(root), runner=partial)) as client:
        ids = client.post("/race", json={"question": "Partial write"}).json()
        rows = messages(client.get(f"/stream/{ids['left_run_id']}"))
        assert [data["seq"] for kind, data in rows if kind == "log"] == [1, 2]
        assert rows[-1][0] == "complete"


def test_results_view_fixtures_cover_all_setups_and_frontier_kinds(root):
    fixture_dir = root / "tests/o4/fixtures"
    configured = settings(root, results_path=fixture_dir / "results.json",
                          frontier_path=fixture_dir / "frontier.json")
    with TestClient(create_app(settings=configured)) as client:
        payload = client.get("/results").json()
        assert set(payload["results"]["setups"]) == {"single", "telem_default", "second_opinion"}
        assert len(payload["frontier"]["points"]) == 4
