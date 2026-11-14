"""Live races bypass the disk cache for their own runs; the eval path keeps caching."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from server.races import RaceManager
from so import config, tavily_client
from so.contracts import AgentAnswer, Question


class FakeTavily:
    """Stands in for AsyncTavilyClient and counts provider calls."""
    calls = 0

    def __init__(self, **_: object) -> None: ...

    async def __aenter__(self) -> FakeTavily:
        return self

    async def __aexit__(self, *_: object) -> None: ...

    async def extract(self, **_: object) -> dict:
        FakeTavily.calls += 1
        return {"results": [{"raw_content": f"page text {FakeTavily.calls}"}]}


@pytest.fixture
def cached_tavily(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    FakeTavily.calls = 0
    monkeypatch.setattr(config, "SO_CACHE", "readwrite")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    monkeypatch.setattr(tavily_client, "AsyncTavilyClient", FakeTavily)
    return tmp_path / "cache"


async def test_eval_path_still_caches(cached_tavily: Path):
    first = await tavily_client.tavily_extract("https://example.com/a")
    second = await tavily_client.tavily_extract("https://example.com/a")
    assert FakeTavily.calls == 1 and not first.cached and second.cached
    assert len(list(cached_tavily.glob("*.json"))) == 1


async def test_live_race_makes_fresh_calls(cached_tavily: Path, tmp_path: Path):
    await tavily_client.tavily_extract("https://example.com/a")  # an earlier eval run cached this read
    cache_files = {p: p.read_text() for p in cached_tavily.glob("*.json")}
    pages = []

    async def runner(question: Question, setup: str, *, log_dir: str) -> AgentAnswer:
        pages.append(await tavily_client.tavily_extract("https://example.com/a"))
        # Work the agent starts in child tasks is part of the same live run.
        pages.append(await asyncio.create_task(tavily_client.tavily_extract("https://example.com/a")))
        return AgentAnswer(status="ANSWERED", final="x")

    settings = SimpleNamespace(log_dir=tmp_path / "logs", max_active_runs=4, run_timeout=5,
                               race_max_usd=None, race_timeout=None)
    manager = RaceManager(settings, runner=runner)
    jobs = manager.start(Question(id="q1", question="Q?", gold=["x"], category="fact", source="test", split="test"), "telem_default", "second_opinion",
                         api_key="demo")
    await asyncio.gather(*(job.task for job in jobs.values()))
    assert FakeTavily.calls == 1 + 4 and not any(page.cached for page in pages)
    assert {p: p.read_text() for p in cached_tavily.glob("*.json")} == cache_files  # nothing written
    # Outside the race the cache is on again.
    assert (await tavily_client.tavily_extract("https://example.com/a")).cached and FakeTavily.calls == 5


def test_bypass_is_scoped(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(config, "SO_CACHE", "readwrite")
    assert config.cache_active()
    with config.bypass_cache():
        assert not config.cache_active()
    assert config.cache_active()
