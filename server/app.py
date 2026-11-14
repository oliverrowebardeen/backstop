"""Serve the race UI and stream contract events without changing their contents."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import re
from typing import Any, AsyncIterator, Awaitable, Callable
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from server import api_v1
from server.races import LiveRun, RaceManager, Runner
from so import config
from so.contracts import AgentAnswer, Event, Frontier, Question, Results, Setup
from so.eventlog import read_log
from server.errors import RunFailure, describe_failure, read_failure

SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class RaceRequest(BaseModel):
    question: str = Field(min_length=1)
    left: Setup = "single"
    right: Setup = "second_opinion"


@dataclass(frozen=True)
class ServerSettings:
    ui_dir: Path
    demo_dir: Path
    fixture_dir: Path | None
    log_dir: Path
    results_path: Path
    frontier_path: Path
    max_active_runs: int
    max_question_chars: int
    start_timeout: float
    run_timeout: float
    poll_seconds: float
    keepalive_seconds: float
    live_races: bool = False

    @classmethod
    def from_config(cls) -> ServerSettings:
        """Read deployment settings from the shared environment configuration."""
        fixture_dir = config.SERVER_FIXTURE_DIR
        return cls(
            live_races=config.SERVER_LIVE_RACES,
            ui_dir=Path(config.SERVER_UI_DIR), demo_dir=Path(config.SERVER_DEMO_DIR),
            fixture_dir=Path(fixture_dir) if fixture_dir else None,
            log_dir=Path(config.SERVER_LOG_DIR), results_path=Path(config.SERVER_RESULTS_PATH),
            frontier_path=Path(config.SERVER_FRONTIER_PATH),
            max_active_runs=config.SERVER_MAX_ACTIVE_RUNS,
            max_question_chars=config.SERVER_MAX_QUESTION_CHARS,
            start_timeout=config.SERVER_START_TIMEOUT_SECONDS,
            run_timeout=config.SERVER_RUN_TIMEOUT_SECONDS,
            poll_seconds=config.SERVER_POLL_SECONDS,
            keepalive_seconds=config.SERVER_KEEPALIVE_SECONDS,
        )

    def __post_init__(self) -> None:
        if self.max_active_runs < 2 or self.max_question_chars < 1:
            raise ValueError("Server run capacity must be at least two and question length must be positive")
        if min(self.start_timeout, self.run_timeout, self.poll_seconds, self.keepalive_seconds) <= 0:
            raise ValueError("Server timeouts and polling intervals must be positive")


@dataclass
class Replay:
    name: str
    title: str
    question: str
    fixture: bool
    left: list[Event]
    right: list[Event]

    def summary(self) -> dict[str, Any]:
        return {"name": self.name, "title": self.title, "question": self.question,
                "fixture": self.fixture, "left": self.left[0].setup,
                "right": self.right[0].setup}


def read_events(path: Path) -> list[Event]:
    return [Event.model_validate(event) for event in read_log(path)]


def replay_catalog(demo_dir: Path, fixture_dir: Path | None) -> dict[str, Replay]:
    """Pairs use a JSON manifest with relative left/right JSONL filenames."""
    pairs = {}
    for directory, synthetic in ((demo_dir, False), (fixture_dir, True)):
        if directory is None:
            continue
        for manifest in sorted(directory.glob("*.json")):
            try:
                data = json.loads(manifest.read_text())
                if not isinstance(data, dict) or not ({"left", "right"} & data.keys()):
                    continue
                paths = [(directory / data[side]).resolve() for side in ("left", "right")]
                if any(not path.is_relative_to(directory.resolve()) for path in paths):
                    raise ValueError("Replay log paths must stay within the recording directory")
                left, right = (read_events(path) for path in paths)
                for events in (left, right):
                    if not events or events[0].type != "run_start" or events[-1].type != "run_end":
                        raise ValueError("Replay logs must include run_start and run_end")
                    if any(event.run_id != events[0].run_id or event.setup != events[0].setup for event in events):
                        raise ValueError("Replay logs must describe one run")
                    if any(b.seq <= a.seq or elapsed(b, a) < 0 for a, b in zip(events, events[1:])):
                        raise ValueError("Replay event order is invalid")
                    for event in events:
                        datetime.fromisoformat(event.ts.replace("Z", "+00:00"))
                name = ("fixture-" if synthetic else "") + manifest.stem
                if not SAFE_NAME.fullmatch(name):
                    raise ValueError("Replay manifest names must be URL-safe")
                pairs[name] = Replay(name, data.get("title", manifest.stem),
                                     left[0].data["question"],
                                     synthetic or bool(data.get("fixture")), left, right)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise ValueError(f"Invalid replay manifest: {manifest.name}") from exc
    return pairs


def sse(kind: str, data: Any, event_id: str | None = None) -> str:
    prefix = f"id: {event_id}\n" if event_id else ""
    return f"{prefix}event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def stream_response(events: AsyncIterator[str]) -> StreamingResponse:
    return StreamingResponse(events, media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def elapsed(event: Event, first: Event) -> float:
    return (datetime.fromisoformat(event.ts.replace("Z", "+00:00")) -
            datetime.fromisoformat(first.ts.replace("Z", "+00:00"))).total_seconds()


def create_app(*, settings: ServerSettings | None = None, runner: Runner | None = None,
               replay_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> FastAPI:
    settings = settings or ServerSettings.from_config()
    runs: dict[str, LiveRun] = {}
    manager = RaceManager(settings, runner)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await manager.close()

    app = FastAPI(title="Backstop", lifespan=lifespan)
    app.mount("/ui", StaticFiles(directory=settings.ui_dir), name="ui")
    app.include_router(api_v1.router)  # Hosted API: /v1/search, /v1/usage, /health

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(settings.ui_dir / "index.html")

    @app.get("/config")
    async def ui_config() -> dict[str, Any]:
        return {"first_engine": config.FIRST_ENGINE, "max_question_chars": settings.max_question_chars,
                "live_available": settings.live_races}

    def catalog() -> dict[str, Replay]:
        try:
            return replay_catalog(settings.demo_dir, settings.fixture_dir)
        except ValueError as exc:
            raise HTTPException(503, str(exc)) from None

    @app.get("/replays")
    async def replays() -> list[dict[str, Any]]:
        return [pair.summary() for pair in catalog().values()]

    @app.get("/replay/{name}")
    async def replay(name: str, request: Request) -> StreamingResponse:
        pair = catalog().get(name)
        if pair is None:
            raise HTTPException(404, "Recorded pair not found.")
        timeline = sorted((elapsed(event, events[0]), side, event.seq, event)
                          for side, events in (("left", pair.left), ("right", pair.right))
                          for event in events)

        async def events() -> AsyncIterator[str]:
            yield sse("replay", pair.summary())
            started = asyncio.get_running_loop().time()
            for at, side, _, event in timeline:
                if await request.is_disconnected():
                    return
                await replay_sleep(max(0, at - (asyncio.get_running_loop().time() - started)))
                yield sse("log", {"side": side, "event": event.model_dump()}, f"{side}-{event.seq}")
            yield sse("complete", {"replay": pair.name})

        return stream_response(events())

    def require_live_races() -> None:
        if not settings.live_races:
            raise HTTPException(403, {"code": "live_races_disabled",
                                      "message": "Live races are disabled. Watch a recorded replay instead."})

    def live_key(_enabled: None = Depends(require_live_races),
                 key: str = Depends(api_v1.rate_limited_key)) -> str:
        return key

    @app.post("/race")
    async def race(body: RaceRequest, api_key: str = Depends(live_key)) -> dict[str, str]:
        question_text = body.question.strip()
        if not question_text:
            raise HTTPException(422, "Enter a question first.")
        if len(question_text) > settings.max_question_chars:
            raise HTTPException(422, f"Questions are limited to {settings.max_question_chars} characters.")
        question = Question(id=f"race-{uuid4().hex}", question=question_text, gold=[],
                            category="fact", source="race-ui", split="test")
        jobs = list(manager.start(question, body.left, body.right, api_key=api_key).values())

        def forget(task: asyncio.Task[AgentAnswer]) -> None:
            for run_id, job in list(runs.items()):
                if job.task is task:
                    del runs[run_id]

        for job in jobs:
            job.task.add_done_callback(forget)

        async def identify(job: LiveRun) -> str:
            # The agent owns run IDs. Discover its first event before returning them.
            while True:
                for path in job.directory.glob("*.jsonl"):
                    with path.open() as handle:
                        line = handle.readline()
                    try:
                        first = Event.model_validate_json(line)
                    except ValidationError:
                        continue
                    job.path = path
                    if not job.task.done():
                        runs[first.run_id] = job
                    return first.run_id
                if job.task.done():
                    job.task.result()
                    raise RuntimeError("Agent finished without an event log")
                await asyncio.sleep(settings.poll_seconds)

        try:
            async with asyncio.timeout(settings.start_timeout):
                left, right = await asyncio.gather(*(identify(job) for job in jobs))
        except Exception as exc:
            for job in jobs:
                job.task.cancel()
            await asyncio.gather(*(job.task for job in jobs), return_exceptions=True)
            failure, _ = describe_failure(exc)
            raise HTTPException(503, f"The live run could not start. {failure.message}") from None
        return {"left_run_id": left, "right_run_id": right}

    @app.get("/stream/{run_id}")
    async def stream(run_id: str, request: Request) -> StreamingResponse:
        if not SAFE_NAME.fullmatch(run_id):
            raise HTTPException(404, "Run not found.")
        job = runs.get(run_id)
        path = job.path if job else next(settings.log_dir.rglob(f"{run_id}.jsonl"), None)
        if path is None or not path.is_file():
            raise HTTPException(404, "Run not found.")
        try:
            last_seq = int(request.headers.get("last-event-id", "0"))
        except ValueError:
            raise HTTPException(400, "Invalid event cursor.") from None

        async def events() -> AsyncIterator[str]:
            pending = ""
            position = 0
            has_answer = False
            last_keepalive = asyncio.get_running_loop().time()
            while not await request.is_disconnected():
                try:
                    with path.open() as handle:
                        handle.seek(position)
                        chunk = handle.read()
                        position = handle.tell()
                except OSError:
                    yield sse("stream_error", {"message": "The run's event log could not be read."})
                    return
                pending += chunk
                lines = pending.split("\n")
                pending = lines.pop()
                for line in lines:
                    if not line.strip():
                        continue
                    try:
                        event = Event.model_validate_json(line)
                    except ValidationError:
                        yield sse("stream_error", {"message": "The run contains an invalid event."})
                        return
                    if event.type == "answer":
                        has_answer = True
                    if event.type == "run_end" and job:
                        # run_end is also emitted by the agent's failure cleanup.
                        # Shield the runner so disconnecting cannot cancel its work.
                        await asyncio.shield(asyncio.gather(job.task, return_exceptions=True))
                    if event.seq > last_seq:
                        yield sse("log", event.model_dump(), str(event.seq))
                    if event.type == "run_end":
                        failure = read_failure(path)
                        if failure is None and (event.data.get("error") or not has_answer):
                            failure = RunFailure(error_type="AgentFailure", message="The run failed before an answer was completed. Check the server logs for the reason.")
                        if failure:
                            yield sse("stream_error", {"run_id": run_id, **failure.model_dump()})
                        else:
                            yield sse("complete", {"run_id": run_id})
                        return
                if job is None or job.task.done():
                    # A final append can arrive while the preceding events are sent.
                    if path.stat().st_size > position:
                        continue
                    if job and not job.task.cancelled():
                        job.task.exception()
                    failure = read_failure(path) or RunFailure(error_type="InterruptedRun", message="The run stopped before final totals were recorded.")
                    yield sse("stream_error", {"run_id": run_id, **failure.model_dump()})
                    return
                now = asyncio.get_running_loop().time()
                if now - last_keepalive >= settings.keepalive_seconds:
                    yield ": keep-alive\n\n"
                    last_keepalive = now
                await asyncio.sleep(settings.poll_seconds)

        return stream_response(events())

    @app.get("/results")
    async def results() -> dict[str, Any]:
        output: dict[str, Any] = {"results": None, "frontier": None}
        for name, model, path in (("results", Results, settings.results_path),
                                  ("frontier", Frontier, settings.frontier_path)):
            if path.is_file():
                try:
                    output[name] = model.model_validate_json(path.read_text()).model_dump()
                except (ValueError, OSError):
                    raise HTTPException(503, f"The {name} artifact is not ready to display.") from None
        return output

    from server.presentation import install_presentation
    install_presentation(app, settings, manager, live_key)
    from server.site import install_site
    install_site(app, settings)
    return app


app = create_app()
