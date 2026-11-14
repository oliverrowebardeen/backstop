"""Deterministic stand-ins for Telem, page readers and the checker (no paid calls)."""

from __future__ import annotations

import asyncio
import sys
from collections import deque
from datetime import date
from types import ModuleType
from typing import Any

from pydantic import Field

from so.contracts import CheckerVerdict, EngineRun, PageText, Result


class CheckedVerdict(CheckerVerdict):
    """Verdict carrying checker usage, like so.checker's CheckedVerdict."""

    cost_usd: float = Field(default=0.001, exclude=True)
    latency_ms: float = Field(default=20, exclude=True)
    tokens_in: int = Field(default=50, exclude=True)
    tokens_out: int = Field(default=10, exclude=True)
    model: str = Field(default="fake-checker", exclude=True)


class FakeLog:
    """Collects emitted events in memory."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, event_type: str, **data: Any) -> None:
        self.events.append({"type": event_type, "data": data})


Step = CheckerVerdict | Exception | float


class Fakes:
    """Scripted engine runs, pages and checker steps.

    A checker step is a verdict, an exception to raise, or a float number of
    seconds to sleep (to exercise timeouts).
    """

    def __init__(self, verdicts: list[Step], *, runs: list[EngineRun | Exception] | None = None,
                 pages: list[PageText | Exception] | None = None, search_delay_s: float = 0.0) -> None:
        self.verdicts = deque(verdicts)
        self.runs = deque(runs or [])
        self.pages = deque(pages or [])
        self.search_delay_s = search_delay_s
        self.searches: list[dict[str, Any]] = []
        self.fetches: list[dict[str, Any]] = []
        self.checks: list[dict[str, Any]] = []

    async def search_engines(self, query: str, engines: list[str], **kwargs: Any) -> dict[str, EngineRun]:
        self.searches.append({"query": query, "engines": engines, **kwargs})
        if self.search_delay_s:
            await asyncio.sleep(self.search_delay_s)
        engine = engines[0]
        run = self.runs.popleft() if self.runs else EngineRun(
            engine=engine, status="ok", results=[source(engine=engine)],
            cost_usd=0.005, latency_ms=100, session_id="session-created",
        )
        if isinstance(run, Exception):
            raise run
        return {engine: run}

    async def extract(self, url: str, *, goal: str | None = None) -> PageText:
        return self._page(url, "tavily")

    async def fetch_page(self, url: str, **kwargs: Any) -> PageText:
        return self._page(url, "telem")

    def _page(self, url: str, via: str) -> PageText:
        self.fetches.append({"url": url, "via": via})
        page = self.pages.popleft()
        if isinstance(page, Exception):
            raise page
        return page

    async def check(self, query: str, goal: str, results: list[Result], today: date) -> CheckerVerdict:
        self.checks.append({"query": query, "results": results})
        return await self._verdict()

    async def check_page(self, query: str, goal: str, page_text: PageText, today: date) -> CheckerVerdict:
        self.checks.append({"query": query, "page_text": page_text.text})
        return await self._verdict()

    async def _verdict(self) -> CheckerVerdict:
        step = self.verdicts.popleft()
        if isinstance(step, Exception):
            raise step
        if isinstance(step, float):
            await asyncio.sleep(step)
            step = self.verdicts.popleft()
        return CheckedVerdict(**step.model_dump())

    def install(self, monkeypatch: Any) -> None:
        """Replace the wrapper modules second_opinion imports lazily."""
        modules = {
            "so.telem_client": {"search_engines": self.search_engines, "fetch_page": self.fetch_page},
            "so.tavily_client": {"tavily_extract": self.extract},
            "so.checker": {"check": self.check, "check_page": self.check_page},
        }
        for name, attrs in modules.items():
            module = ModuleType(name)
            for key, value in attrs.items():
                setattr(module, key, value)
            monkeypatch.setitem(sys.modules, name, module)


def source(url: str = "https://example.com/source", *, engine: str = "tavily", rank: int = 1,
           snippet: str = "Evidence", title: str = "Source") -> Result:
    return Result(rank=rank, title=title, url=url, snippet=snippet, engine=engine)


def verdict(label: str, **kwargs: Any) -> CheckerVerdict:
    return CheckerVerdict(verdict=label, confidence=0.9, why="Evidence assessment", **kwargs)
