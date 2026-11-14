"""One-request live races: start both sides and stream their events on the same response.

On a serverless host such as Vercel, each request can land on a different instance, so a run
started by POST /race may not exist on the instance that serves GET /stream/{run_id}.
POST /race/stream keeps a whole race inside one request: it starts both agents, follows both
event logs as they grow and ends when both runs have ended. Nothing else can watch these runs,
so closing the response cancels both agents and stops their spending.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

from pydantic import ValidationError

from server.errors import RunFailure, describe_failure, read_failure
from server.races import LiveRun
from so.contracts import Event

Message = tuple[str, dict[str, Any]]

NO_ANSWER = RunFailure(error_type="AgentFailure",
                       message="The run failed before an answer was completed. Check the server logs for the reason.")
INTERRUPTED = RunFailure(error_type="InterruptedRun", message="The run stopped before final totals were recorded.")


@dataclass
class RunFollower:
    """Reads one side's event log as the agent appends to it and decides how the run ended."""

    side: str
    job: LiveRun
    path: Path | None = None
    position: int = 0
    pending: str = ""
    has_answer: bool = False
    finished: bool = False

    def _log(self) -> Path | None:
        if self.path is None:
            logs = sorted(self.job.directory.glob("*.jsonl"))
            if logs:
                self.path = self.job.path = logs[0]
        return self.path

    def _end(self, failure: RunFailure | None, run_id: str | None = None) -> list[Message]:
        self.finished = True
        head = {"side": self.side, "run_id": run_id}
        return [("stream_error", head | failure.model_dump())] if failure else [("complete", head)]

    def _task_failure(self) -> RunFailure:
        task = self.job.task
        if task.cancelled() or task.exception() is None:
            return INTERRUPTED
        return describe_failure(task.exception())[0]

    async def poll(self) -> list[Message]:
        """Events appended since the last poll. The last message is terminal once the run has ended."""
        if self.finished:
            return []
        messages: list[Message] = []
        path = self._log()
        if path is not None:
            try:
                with path.open(encoding="utf-8") as handle:
                    handle.seek(self.position)
                    chunk = handle.read()
                    self.position = handle.tell()
            except OSError:
                return self._end(RunFailure(error_type="UnreadableLog", message="The run's event log could not be read."))
            *lines, self.pending = (self.pending + chunk).split("\n")
            for line in lines:
                if not line.strip():
                    continue
                try:
                    event = Event.model_validate_json(line)
                except ValidationError:
                    return messages + self._end(RunFailure(error_type="InvalidEvent",
                                                           message="The run contains an invalid event."))
                self.has_answer = self.has_answer or event.type == "answer"
                messages.append(("log", {"side": self.side, "event": event.model_dump()}))
                if event.type == "run_end":
                    # run_end is also written by the agent's failure cleanup, so wait for the task
                    # (shielded: this response closing must not cancel it here) and read its outcome.
                    await asyncio.shield(asyncio.gather(self.job.task, return_exceptions=True))
                    failure = read_failure(path)
                    if failure is None and (event.data.get("error") or not self.has_answer):
                        failure = NO_ANSWER
                    return messages + self._end(failure, event.run_id)
        if self.job.task.done() and (path is None or path.stat().st_size <= self.position):
            # The task ended without a run_end line: a start failure, a cancellation or a crash.
            return messages + self._end((read_failure(path) if path else None) or self._task_failure())
        return messages


async def follow_race(jobs: dict[str, LiveRun], *, poll_seconds: float, keepalive_seconds: float,
                      is_disconnected: Callable[[], Awaitable[bool]]) -> AsyncIterator[Message | None]:
    """Messages from both sides in arrival order, None for a keep-alive, then ("done", {}).

    Leaving early (the client disconnected or the response was cancelled) cancels both agents."""
    followers = [RunFollower(side, job) for side, job in jobs.items()]
    loop = asyncio.get_running_loop()
    last_message = loop.time()
    try:
        while not all(follower.finished for follower in followers):
            if await is_disconnected():
                return
            for follower in followers:
                for message in await follower.poll():
                    last_message = loop.time()
                    yield message
            if loop.time() - last_message >= keepalive_seconds:
                last_message = loop.time()
                yield None
            if not all(follower.finished for follower in followers):
                await asyncio.sleep(poll_seconds)
        yield "done", {}
    finally:
        for job in jobs.values():
            if not job.task.done():
                job.task.cancel()
