"""Shared admission, execution, and spend attribution for both live race routes."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import logging
from pathlib import Path
from typing import Awaitable, Protocol

from fastapi import HTTPException

from server.errors import log_failure, write_failure
from server.live_race import RaceStopped, cost_cap_failure, time_limit_failure
from server.usage import key_id
from so.contracts import AgentAnswer, Event, Question, Setup
from so.eventlog import read_log

logger = logging.getLogger(__name__)
usage_logger = logging.getLogger("uvicorn.error.race_usage")


class Runner(Protocol):
    def __call__(self, question: Question, setup: str, *, log_dir: str) -> Awaitable[AgentAnswer]: ...


class RaceSettings(Protocol):
    log_dir: Path
    max_active_runs: int
    run_timeout: float
    # Optional public-race guards (None means off): spend per race and seconds per race.
    race_max_usd: float | None
    race_timeout: float | None


class RaceBudget:
    """Recorded spend across both sides of one race, read from their event logs."""

    def __init__(self, directories: list[Path], cap: float) -> None:
        self.directories = directories
        self.cap = cap

    def spent(self) -> float:
        total = 0.0
        for directory in self.directories:
            try:
                total += recorded_usage(directory)[2]
            except (OSError, ValueError):
                continue  # A partially written line is read on the next poll.
        return total


@dataclass
class LiveRun:
    directory: Path
    task: asyncio.Task[AgentAnswer]
    path: Path | None = None


def recorded_usage(directory: Path) -> tuple[int, int, float]:
    """Read known spend, including partial runs, without counting summaries twice."""
    engine_calls, fetches, cost = 0, 0, 0.0
    for path in directory.glob("*.jsonl"):
        for raw in read_log(path):
            event = Event.model_validate(raw)
            if event.type == "search":
                engine_calls += len(event.data.get("engines", []))
            elif event.type == "fetch":
                fetches += 1
            if event.type in {"search", "fetch", "llm"}:
                cost += event.data.get("cost_usd", 0)
    return engine_calls, fetches, cost


class RaceManager:
    """One event-loop-owned pool shared by background and request-scoped races."""

    def __init__(self, settings: RaceSettings, runner: Runner | None = None) -> None:
        self.settings = settings
        self.runner = runner
        self.tasks: set[asyncio.Task[AgentAnswer]] = set()

    def active_runs(self) -> int:
        return sum(not task.done() for task in self.tasks)

    def start(self, question: Question, left: Setup, right: Setup, *,
              api_key: str) -> dict[str, LiveRun]:
        # There is no await between checking capacity and registering both tasks.
        if sum(not task.done() for task in self.tasks) + 2 > self.settings.max_active_runs:
            raise HTTPException(429, "The server's run capacity is full. Try again when a race finishes.")
        runner = self.runner
        if runner is None:
            try:
                from so.agent import run_question
                runner = run_question
            except ImportError:
                raise HTTPException(503, "The live agent is unavailable. Check the server installation.") from None
        directories = {side: self.settings.log_dir / question.id / side for side in ("left", "right")}
        for directory in directories.values():
            directory.mkdir(parents=True, exist_ok=True)
        cap = getattr(self.settings, "race_max_usd", None)
        budget = RaceBudget(list(directories.values()), cap) if cap else None
        jobs = {}
        for side, setup in (("left", left), ("right", right)):
            directory = directories[side]
            task = asyncio.create_task(self._execute(runner, question, setup, directory, key_id(api_key), budget))
            self.tasks.add(task)
            task.add_done_callback(self._finish)
            jobs[side] = LiveRun(directory, task)
        return jobs

    async def _execute(self, runner: Runner, question: Question, setup: Setup,
                       directory: Path, caller_id: str, budget: RaceBudget | None = None) -> AgentAnswer:
        failed = True
        race_timeout = getattr(self.settings, "race_timeout", None)
        timeout = min(self.settings.run_timeout, race_timeout) if race_timeout else self.settings.run_timeout
        try:
            try:
                async with asyncio.timeout(timeout):
                    coroutine = runner(question, setup, log_dir=str(directory))
                    answer = await (self._capped(coroutine, budget) if budget else coroutine)
            except TimeoutError as exc:
                if race_timeout and timeout == race_timeout:
                    raise RaceStopped(time_limit_failure(timeout)) from exc
                raise
            failed = False
            return answer
        except RaceStopped as exc:
            logger.warning("Race stopped (%s): %s", exc.failure.error_type, exc.failure.message)
            try:
                write_failure(directory, exc.failure)
            except OSError as write_error:
                log_failure(logger, write_error)
            raise
        except Exception as exc:
            failure = log_failure(logger, exc)
            try:
                write_failure(directory, failure)
            except OSError as write_error:
                log_failure(logger, write_error)
            raise
        finally:
            try:
                engine_calls, fetches, cost = recorded_usage(directory)
                usage_logger.info(json.dumps({
                    "event": "race_usage", "key_id": caller_id,
                    "question_id": question.id, "setup": setup,
                    "side": directory.name, "failed": failed,
                    "engine_calls": engine_calls, "fetches": fetches,
                    "cost_usd": cost,
                }, sort_keys=True))
            except Exception as exc:
                failure = log_failure(logger, exc)
                write_failure(directory, failure)
                raise

    @staticmethod
    async def _capped(coroutine: Awaitable[AgentAnswer], budget: RaceBudget) -> AgentAnswer:
        """Run the agent, stopping it once the race's recorded spend passes the cap."""
        inner = asyncio.ensure_future(coroutine)
        try:
            while True:
                done, _ = await asyncio.wait({inner}, timeout=0.25)
                if done:
                    return inner.result()
                spent = budget.spent()
                if spent > budget.cap:
                    inner.cancel()
                    await asyncio.gather(inner, return_exceptions=True)
                    raise RaceStopped(cost_cap_failure(spent, budget.cap))
        finally:
            if not inner.done():
                inner.cancel()
                await asyncio.gather(inner, return_exceptions=True)

    def _finish(self, task: asyncio.Task[AgentAnswer]) -> None:
        self.tasks.discard(task)
        if not task.cancelled():
            task.exception()

    async def close(self) -> None:
        pending = tuple(self.tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
