"""Serve the race UI and stream contract events without changing their contents."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import re
import tempfile
from typing import Any, AsyncIterator, Awaitable, Callable
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from server import api_v1
from server.races import LiveRun, RaceManager, Runner
from server.live_race import (PUBLIC_MAX_QUESTION_CHARS, DemoGuard, LiveRaceEnv, golden_examples,
                              live_off_reason, missing_live_keys, save_recording)
from server.race_stream import follow_race
from server.usage import RateLimiter
from so import config
from so.contracts import AgentAnswer, Event, Frontier, Question, Results, Setup
from so.eventlog import read_log
from server.errors import RunFailure, describe_failure, read_failure

SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class RaceRequest(BaseModel):
    question: str = Field(min_length=1)
    left: Setup = "single"
    right: Setup = "second_opinion"


class RecordingRequest(BaseModel):
    left_run_id: str = Field(min_length=1, max_length=200)
    right_run_id: str = Field(min_length=1, max_length=200)


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
    cors_origins: tuple[str, ...] = ()
    # "api_key": live races need the hosted API key (SERVER_LIVE_RACES). "demo": public live races
    # behind the guards in server/live_race.py (BACKSTOP_LIVE_RACE=true).
    race_access: str = "api_key"
    race_max_usd: float | None = None
    race_timeout: float | None = None
    max_races: int = 2
    race_rate_per_hour: int = 10
    recordings_dir: Path | None = None
    # Provider keys a live race needs that are not set; live races stay off until they are.
    missing_keys: tuple[str, ...] = ()

    @property
    def live_ready(self) -> bool:
        return self.live_races and not self.missing_keys

    @classmethod
    def from_config(cls) -> ServerSettings:
        """Read deployment settings from the shared environment configuration."""
        fixture_dir = config.SERVER_FIXTURE_DIR
        live = LiveRaceEnv.read()
        public = live.explicit is True
        live_races = live.explicit if live.explicit is not None else config.SERVER_LIVE_RACES
        log_dir = Path(config.SERVER_LOG_DIR)
        if os.getenv("VERCEL") and not (os.getenv("SERVER_LOG_DIR") or os.getenv("SO_LOG_DIR")):
            # Vercel functions can only write under /tmp.
            log_dir = Path(tempfile.gettempdir()) / "backstop" / "logs"
        return cls(
            live_races=live_races, missing_keys=missing_live_keys() if live_races else (),
            race_access="demo" if public else "api_key",
            race_max_usd=live.max_usd, race_timeout=live.timeout_s, max_races=live.max_races,
            race_rate_per_hour=live.rate_per_hour,
            recordings_dir=Path(config.SERVER_UI_DIR) / "fixtures",
            cors_origins=tuple(config.SERVER_CORS_ORIGINS),
            ui_dir=Path(config.SERVER_UI_DIR), demo_dir=Path(config.SERVER_DEMO_DIR),
            fixture_dir=Path(fixture_dir) if fixture_dir else None,
            log_dir=log_dir, results_path=Path(config.SERVER_RESULTS_PATH),
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
        for origin in self.cors_origins:
            try:
                parsed = urlsplit(origin)
                valid = (parsed.scheme in {"http", "https"} and parsed.hostname and "*" not in origin
                         and parsed.username is None and parsed.password is None
                         and origin == f"{parsed.scheme}://{parsed.netloc}")
                parsed.port  # Validate the optional port as well as the origin shape.
            except ValueError:
                valid = False
            if not valid:
                raise ValueError("SERVER_CORS_ORIGINS must contain exact HTTP(S) origins without paths or wildcards")


# Provenance a recording manifest may carry; /replays passes it through for the race label.
REPLAY_META = ("recorded_at", "commit", "question_id", "split", "checker_backend", "first_engine_routing", "cache")


@dataclass
class Replay:
    name: str
    title: str
    question: str
    fixture: bool
    left: list[Event]
    right: list[Event]
    meta: dict[str, Any] | None = None
    order: int | None = None

    def summary(self) -> dict[str, Any]:
        return {"name": self.name, "title": self.title, "question": self.question,
                "fixture": self.fixture, "left": self.left[0].setup,
                "right": self.right[0].setup, **(self.meta or {})}


def read_events(path: Path) -> list[Event]:
    return [Event.model_validate(event) for event in read_log(path)]


def validated_events(path: Path) -> list[Event]:
    """One finished run, in order, as a replay needs it. Raises ValueError otherwise."""
    events = read_events(path)
    if not events or events[0].type != "run_start" or events[-1].type != "run_end":
        raise ValueError("Replay logs must include run_start and run_end")
    if any(event.run_id != events[0].run_id or event.setup != events[0].setup for event in events):
        raise ValueError("Replay logs must describe one run")
    if any(b.seq <= a.seq or elapsed(b, a) < 0 for a, b in zip(events, events[1:])):
        raise ValueError("Replay event order is invalid")
    for event in events:
        datetime.fromisoformat(event.ts.replace("Z", "+00:00"))
    return events


def replay_catalog(demo_dir: Path, fixture_dir: Path | None,
                   recordings_dir: Path | None = None) -> dict[str, Replay]:
    """Pairs use a JSON manifest with relative left/right JSONL filenames.

    demo_dir and recordings_dir (ui/fixtures, shipped real runs) hold recordings of live runs;
    fixture_dir holds synthetic test fixtures. Pairs whose manifest sets an integer "order" come
    first, lowest first; the rest keep directory and file name order."""
    pairs = {}
    for directory, synthetic in ((demo_dir, False), (recordings_dir, False), (fixture_dir, True)):
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
                left, right = (validated_events(path) for path in paths)
                name = ("fixture-" if synthetic else "") + manifest.stem
                if not SAFE_NAME.fullmatch(name):
                    raise ValueError("Replay manifest names must be URL-safe")
                meta = {key: data[key] for key in REPLAY_META
                        if isinstance(data.get(key), (str, int, float)) and not isinstance(data.get(key), bool)}
                order = data.get("order")
                pairs[name] = Replay(name, data.get("title", manifest.stem),
                                     left[0].data["question"],
                                     synthetic or bool(data.get("fixture")), left, right, meta,
                                     order if isinstance(order, int) and not isinstance(order, bool) else None)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise ValueError(f"Invalid replay manifest: {manifest.name}") from exc
    return dict(sorted(pairs.items(), key=lambda item: (item[1].order is None, item[1].order or 0)))


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
    public = settings.race_access == "demo"
    guard = DemoGuard(max_races=settings.max_races, rate_per_hour=settings.race_rate_per_hour)
    max_question = min(settings.max_question_chars, PUBLIC_MAX_QUESTION_CHARS) if public else settings.max_question_chars
    if not settings.live_races:
        mode = "replay only (set BACKSTOP_LIVE_RACE=true for live races)"
    elif settings.missing_keys:
        mode = f"replay only: live races need {', '.join(settings.missing_keys)}"
    elif public:
        mode = (f"live races ON, public demo mode: max {settings.max_races} at once, {settings.race_rate_per_hour}/hour per IP, "
                f"{f'${settings.race_max_usd:.2f}' if settings.race_max_usd else 'no cost cap'} and "
                f"{f'{settings.race_timeout:g}s' if settings.race_timeout else 'the run timeout'} per race")
    else:
        mode = "live races ON, hosted API key required (SERVER_LIVE_RACES)"
    logging.getLogger("uvicorn.error").info("Backstop race mode: %s", mode)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await manager.close()

    app = FastAPI(title="Backstop", lifespan=lifespan)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware, allow_origins=list(settings.cors_origins),
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type", "Last-Event-ID"],
            expose_headers=["Retry-After", "WWW-Authenticate"],
            allow_credentials=False,
        )
    app.mount("/ui", StaticFiles(directory=settings.ui_dir), name="ui")
    app.include_router(api_v1.router)  # Hosted API: /v1/search, /v1/usage, /health

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(settings.ui_dir / "index.html")

    live_reason = live_off_reason(settings.live_races, settings.missing_keys)

    @app.get("/config")
    async def ui_config() -> dict[str, Any]:
        return {"first_engine": config.FIRST_ENGINE, "max_question_chars": max_question,
                "first_engine_routing": config.FIRST_ENGINE_ROUTING == "on",
                "live_available": settings.live_ready, "live_reason": live_reason,
                "race_stream": "/race/stream",
                "race_access": settings.race_access if settings.live_ready else "replay",
                "race_max_usd": settings.race_max_usd if public else None,
                "race_timeout_s": settings.race_timeout if public else None,
                "examples": golden_examples(settings.demo_dir)}

    def catalog() -> dict[str, Replay]:
        try:
            return replay_catalog(settings.demo_dir, settings.fixture_dir, settings.recordings_dir)
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
        if not settings.live_ready:
            raise HTTPException(403, {"code": "live_races_disabled",
                                      "message": live_reason or "Live races are disabled. Watch a recorded replay instead."})

    def live_key(request: Request, _enabled: None = Depends(require_live_races),
                 authorization: str | None = Header(default=None),
                 limiter: RateLimiter = Depends(api_v1.get_rate_limiter)) -> str:
        if public:
            # Public races: no key or code in the browser; capacity and per-IP limits instead,
            # then the per-race cost cap and time limit while it runs.
            return guard.admit(request, manager.active_runs())
        return api_v1.rate_limited_key(api_v1.require_key(authorization), limiter)

    def race_question(body: RaceRequest) -> Question:
        """Validated before admission, so a rejected question does not use a visitor's hourly quota."""
        question_text = body.question.strip()
        if not question_text:
            raise HTTPException(422, "Enter a question first.")
        if len(question_text) > max_question:
            raise HTTPException(422, f"Questions are limited to {max_question} characters.")
        if public and body.left == body.right:
            raise HTTPException(422, "Choose two different setups to race.")
        return Question(id=f"race-{uuid4().hex}", question=question_text, gold=[],
                        category="fact", source="race-ui", split="test")

    @app.post("/race/stream")
    async def race_stream(body: RaceRequest, request: Request, question: Question = Depends(race_question),
                          api_key: str = Depends(live_key)) -> StreamingResponse:
        """Start both setups and stream both runs' events on this one response (serverless safe)."""
        jobs = manager.start(question, body.left, body.right, api_key=api_key)
        head = {"question": question.question, "left": body.left, "right": body.right,
                "race_max_usd": settings.race_max_usd, "race_timeout_s": settings.race_timeout}

        async def events() -> AsyncIterator[str]:
            yield sse("race", head)
            async for message in follow_race(jobs, poll_seconds=settings.poll_seconds,
                                             keepalive_seconds=settings.keepalive_seconds,
                                             is_disconnected=request.is_disconnected):
                yield ": keep-alive\n\n" if message is None else sse(*message)

        return stream_response(events())

    @app.post("/race")
    async def race(body: RaceRequest, question: Question = Depends(race_question),
                   api_key: str = Depends(live_key)) -> dict[str, str]:
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

    def recording_access(_enabled: None = Depends(require_live_races),
                         authorization: str | None = Header(default=None)) -> None:
        # Saving writes files on the server, so it needs the hosted API key in every mode.
        api_v1.require_key(authorization)

    @app.post("/recordings")
    async def save_live_recording(body: RecordingRequest, _: None = Depends(recording_access)) -> dict[str, str]:
        """Save two finished live runs as a replay pair in the demo folder."""
        try:
            return save_recording(settings.log_dir, settings.demo_dir, body.left_run_id, body.right_run_id,
                                  validated_events)
        except ValueError:
            raise HTTPException(409, {"code": "run_unfinished",
                                      "message": "Both runs must finish before they can be saved."}) from None

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


    from server.site import install_site
    install_site(app, settings)
    return app


app = create_app()
