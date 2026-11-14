"""Fakes for Telem Monitors, Backstop search and the checker."""
from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from telem.errors import APIStatusError

from so.client import BackstopResult, BackstopSearchResponse


def hit(url: str, snippet: str, *, session: str = "sess-1", engines: tuple[str, ...] = ("tavily",)) -> BackstopSearchResponse:
    """A HIT response whose first result is the checker's evidence."""
    return BackstopSearchResponse(
        status="HIT", verdict="HIT", session_id=session, engines_called=list(engines), engine_calls=len(engines),
        cost_usd=0.0021, why="Result 1 states it.",
        results=[BackstopResult(url=url, title="Evidence", summary=snippet, rank=1, provider=engines[0],
                                rank_reason="evidence")])


def miss(*, session: str = "sess-1") -> BackstopSearchResponse:
    """A NOT_FOUND response after two rescues."""
    return BackstopSearchResponse(status="NOT_FOUND", session_id=session, engines_called=["tavily", "exa", "brave"],
                                  engine_calls=3, cost_usd=0.004, why="Nothing relevant.", note="Do not guess.")


class FakeSearch:
    """Answers by field query substring; records every call."""

    def __init__(self, answers: dict[str, BackstopSearchResponse | Exception]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, str, str | None]] = []

    async def __call__(self, query: str, goal: str, session: str | None) -> BackstopSearchResponse:
        self.calls.append((query, goal, session))
        for needle, answer in self.answers.items():
            if needle in query:
                if isinstance(answer, Exception):
                    raise answer
                return answer
        return miss(session=session or "sess-1")


class FakeCheck:
    """HIT when the result snippet contains any of `hit_words`."""

    def __init__(self, hit_words: tuple[str, ...] = ()) -> None:
        self.hit_words = hit_words
        self.calls: list[tuple[str, str, list[Any]]] = []

    async def __call__(self, query: str, goal: str, results: list[Any]) -> Any:
        self.calls.append((query, goal, results))
        text = results[0].snippet
        if any(word in text for word in self.hit_words):
            return SimpleNamespace(verdict="HIT", why="Result 1 names a newer value.", cost_usd=0.0005)
        return SimpleNamespace(verdict="DEAD", why="Nothing newer.", cost_usd=0.0005)


async def fake_extract(label: str, company: str, goal: str, title: str, snippet: str) -> str:
    """The first sentence of the snippet."""
    return snippet.split(".")[0]


@dataclass
class FakeMonitorsAPI:
    """In-memory Telem Monitors with poll queues: each queued item is returned once."""

    fail: dict[str, int] = field(default_factory=dict)  # op -> HTTP status to fail with
    monitors: dict[str, dict[str, Any]] = field(default_factory=dict)
    queued: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    calls: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)

    def _maybe_fail(self, op: str) -> None:
        if op in self.fail:
            raise APIStatusError(f"{op} failed", status_code=self.fail[op], body={"error": f"{op} broke"})

    async def create(self, query: str, **kwargs: Any) -> Any:
        self.calls.append(("create", query, kwargs))
        self._maybe_fail("create")
        monitor_id = f"mon_{len(self.monitors) + 1}"
        self.monitors[monitor_id] = {"query": query, **kwargs}
        self.queued[monitor_id] = []
        secret = "whsec_test" if kwargs.get("webhook_url") else None
        return SimpleNamespace(id=monitor_id, query=query, session_id="msess", webhook_secret=secret)

    async def poll(self, monitor_id: str, **kwargs: Any) -> Any:
        self.calls.append(("poll", monitor_id, kwargs))
        self._maybe_fail("poll")
        items, self.queued[monitor_id] = self.queued.get(monitor_id, []), []
        results = [SimpleNamespace(url=row["url"], title=row.get("title", ""), summary=row.get("summary"),
                                   excerpt=None, publish_date=row.get("publish_date"), provider="tavily")
                   for row in items]
        return SimpleNamespace(results=results, by_provider=[], session_id="msess")

    async def update(self, monitor_id: str, **kwargs: Any) -> Any:
        self.calls.append(("update", monitor_id, kwargs))
        self._maybe_fail("update")
        self.monitors[monitor_id].update(kwargs)
        return SimpleNamespace(id=monitor_id, **self.monitors[monitor_id])

    async def delete(self, monitor_id: str) -> None:
        self.calls.append(("delete", monitor_id, {}))
        self._maybe_fail("delete")
        self.monitors.pop(monitor_id, None)
