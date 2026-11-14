"""Deterministic wrappers for policy acceptance without paid API calls."""

from __future__ import annotations

from collections import deque
from datetime import date
from types import ModuleType
from typing import Any
import sys

from pydantic import Field

from so.contracts import CheckerVerdict, EngineRun, PageText, Result


class FakeCheckedVerdict(CheckerVerdict):
    cost_usd: float = Field(default=0.001, exclude=True)
    latency_ms: float = Field(default=20, exclude=True)
    tokens_in: int = Field(default=50, exclude=True)
    tokens_out: int = Field(default=10, exclude=True)
    model: str = Field(default="fake-checker", exclude=True)


class FakeLog:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def emit(self, event_type: str, **data: Any) -> None:
        self.events.append({"type": event_type, "data": data})


class Fakes:
    def __init__(
        self, verdicts: list[CheckerVerdict], *, runs: list[EngineRun] | None = None,
        pages: list[PageText | Exception] | None = None,
    ) -> None:
        self.verdicts = deque(verdicts)
        self.runs = deque(runs or [])
        self.pages = deque(pages or [])
        self.searches: list[dict[str, Any]] = []
        self.fetches: list[dict[str, Any]] = []
        self.checks: list[dict[str, Any]] = []

    async def search_engines(self, query: str, engines: list[str], **kwargs: Any) -> dict[str, EngineRun]:
        self.searches.append({"query": query, "engines": engines, **kwargs})
        engine = engines[0]
        run = self.runs.popleft() if self.runs else EngineRun(
            engine=engine, status="ok", results=[source(engine=engine)],
            cost_usd=0.005, latency_ms=100, session_id="session-created",
        )
        return {engine: run}

    async def extract(self, url: str) -> PageText:
        return await self._fetch(url, "tavily")

    async def fetch_page(self, url: str, **kwargs: Any) -> PageText:
        return await self._fetch(url, "telem", **kwargs)

    async def _fetch(self, url: str, via: str, **kwargs: Any) -> PageText:
        self.fetches.append({"url": url, "via": via, **kwargs})
        page = self.pages.popleft()
        if isinstance(page, Exception):
            raise page
        return page

    async def check(self, query: str, goal: str, results: list[Result], today: date) -> CheckerVerdict:
        self.checks.append({"query": query, "goal": goal, "results": results, "today": today})
        return self._verdict()

    async def check_page(self, query: str, goal: str, page_text: PageText, today: date) -> CheckerVerdict:
        self.checks.append({"query": query, "goal": goal, "page_text": page_text.text, "today": today})
        return self._verdict()

    def _verdict(self) -> CheckerVerdict:
        return FakeCheckedVerdict(**self.verdicts.popleft().model_dump())

    def install(self, monkeypatch: Any) -> None:
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


def source(url: str = "https://example.com/source", *, engine: str = "tavily", rank: int = 1, snippet: str = "Evidence") -> Result:
    return Result(rank=rank, title="Source", url=url, snippet=snippet, engine=engine)


def verdict(label: str, **kwargs: Any) -> CheckerVerdict:
    return CheckerVerdict(verdict=label, confidence=0.9, why="Evidence assessment", **kwargs)
