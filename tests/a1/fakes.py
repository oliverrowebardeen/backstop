"""Fake Telem search client and scripted checker; no paid calls."""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Any

from pydantic import Field
from telem import ProviderRun, SearchResponse, SearchResult
from telem.errors import BadRequestError

from so.contracts import CheckerVerdict


class CheckedVerdict(CheckerVerdict):
    """Verdict carrying checker usage, like so.checker's CheckedVerdict."""

    cost_usd: float = Field(default=0.0005, exclude=True)
    latency_ms: float = Field(default=10, exclude=True)
    tokens_in: int = Field(default=50, exclude=True)
    tokens_out: int = Field(default=10, exclude=True)
    model: str = Field(default="fake-checker", exclude=True)


def verdict(label: str, **kwargs: Any) -> CheckerVerdict:
    return CheckerVerdict(verdict=label, confidence=0.9, why=f"{label} assessment", **kwargs)


class FakeAsyncTelem:
    """Mimics AsyncTelem.search (real SearchResponse objects).

    reject: ledger field names that make the fake answer HTTP 400, like a strict router would.
    """

    def __init__(self, *, reject: set[str] | None = None,
                 reject_status: int = 400, **_: Any) -> None:
        self.calls: list[dict[str, Any]] = []
        self.reject = reject or set()
        self.reject_status = reject_status

    async def search(self, query: Any, **kwargs: Any) -> SearchResponse:
        self.calls.append({"query": query, **kwargs})
        bad = self.reject & {key for key, value in kwargs.items() if value is not None}
        if bad:
            error = BadRequestError(f"unknown field(s): {', '.join(sorted(bad))}", status_code=400, body={})
            error.status_code = self.reject_status
            raise error
        engines = kwargs.get("providers_include") or ["tavily"]
        runs = [ProviderRun(provider=engine, status="success", latency_ms=50, query=query, results=[
            SearchResult(url=f"https://{engine}.example/{len(self.calls)}", title=f"{engine} result",
                         summary="text", rank=1, provider=engine)]) for engine in engines]
        return SearchResponse(results=[r for run in runs for r in run.results], by_provider=runs,
                              session_id=kwargs.get("session") or "sess-new", interaction_id="i",
                              status="completed")

    async def aclose(self) -> None:
        return None

    async def __aenter__(self) -> FakeAsyncTelem:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


def install_checker(monkeypatch: Any, steps: list[Any]) -> list[dict[str, Any]]:
    """Replace so.checker with scripted verdicts; returns the calls it saw."""
    seen: list[dict[str, Any]] = []

    async def check(query, goal, results, today=None, **kwargs):
        seen.append({"query": query, "goal": goal, "urls": [item.url for item in results]})
        step = steps.pop(0) if steps else verdict("DEAD")
        if isinstance(step, Exception):
            raise step
        return CheckedVerdict(**step.model_dump())

    module = ModuleType("so.checker")
    module.check = check
    module.check_page = check
    monkeypatch.setitem(sys.modules, "so.checker", module)
    return seen
