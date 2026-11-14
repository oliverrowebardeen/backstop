"""Exercise ledger forwarding through the real SDK with an in-memory router."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
from typing import Any

import pytest
from telem import AsyncTelem
from telem.errors import BadRequestError

from so import config, telem_client
from so.contracts import EngineRun, PageText


@dataclass
class Router:
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    reject_status: int | None = None


@pytest.fixture
def router(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEM_API_KEY", "test-placeholder")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(config, "SO_CACHE", "readwrite")
    monkeypatch.setattr(config, "SEARCH_BUDGET", config.Budget(1))
    state = Router()

    async def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        state.calls.append((path, body))
        if state.reject_status and ({"context", "step"} & body["metadata"].keys()):
            raise BadRequestError("Unsupported labels", status_code=state.reject_status, body={})
        run = {"preprocessor_name": "tavily", "status": "succeeded", "output_payload": {"results": []}}
        if path == "/v1/fetch":
            run = {"preprocessor_name": "web_fetch", "status": "succeeded", "query": body["urls"][0],
                   "output_payload": {"fetched_results": [{"content": "page"}]}}
        return {
            "normalized_schema_version": 2,
            "session_id": body.get("session_id", "session-new"),
            "interaction_id": "interaction",
            "status": "succeeded",
            "preprocessor_runs": [run],
        }

    monkeypatch.setattr(AsyncTelem, "_post", post)
    return state


async def invoke(operation: str, **kwargs: Any) -> EngineRun | PageText:
    if operation == "fetch":
        return await telem_client.fetch_page("https://example.com/page", goal="goal", **kwargs)
    if operation == "default":
        runs = await telem_client.search_default("query", goal="goal", **kwargs)
    else:
        runs = await telem_client.search_engines("query", ["tavily"], goal="goal", **kwargs)
    return runs["tavily"]


@pytest.mark.parametrize("operation", ["pinned", "default", "fetch"])
async def test_forwarded_labels_share_cache_entries(router, operation):
    labels = {"context": "initial search", "metadata": {"step": "initial"}}
    first = await invoke(operation, session="session-first", **labels)
    assert first.ok if isinstance(first, PageText) else first.status == "ok"
    cached = await invoke(operation, session="session-second", **labels)
    assert len(router.calls) == 1 and cached.cached
    assert cached.session_id == "session-second"
    assert cached.cost_usd == first.cost_usd and cached.latency_ms == first.latency_ms
    path, body = router.calls[0]
    assert path == ("/v1/fetch" if operation == "fetch" else "/v1/interactions")
    assert body["session_id"] == "session-first"
    assert body["metadata"]["context"] == labels["context"]
    assert body["metadata"]["step"] == "initial"
    if operation != "fetch":
        assert body["metadata"]["goal"] == "goal"

    assert (await invoke(operation, context="rescue search", metadata=labels["metadata"])).cached
    assert (await invoke(operation, context=labels["context"], metadata={"step": "rescue"})).cached
    assert (await invoke(operation)).cached
    assert (await invoke(operation, context="", metadata={})).cached
    assert len(router.calls) == 1
    assert len(list(config.CACHE_DIR.glob("*.json"))) == 1
    assert config.SEARCH_BUDGET.spent == pytest.approx(first.cost_usd)
    assert config.SEARCH_BUDGET.reserved == 0


async def test_fetch_context_merges_without_mutating_metadata(router):
    metadata = {"context": "metadata context", "step": "extract"}
    await telem_client.fetch_page("https://example.com/page", context="explicit context", metadata=metadata)
    assert metadata == {"context": "metadata context", "step": "extract"}
    assert router.calls[0][1]["metadata"] == {"context": "explicit context", "step": "extract"}
    cached = await telem_client.fetch_page("https://example.com/page", context="explicit context",
        metadata={"context": "shadowed context", "step": "extract"}, goal="ignored goal")
    assert cached.cached and len(router.calls) == 1


@pytest.mark.parametrize("status", [400, 422])
@pytest.mark.parametrize("operation", ["pinned", "default", "fetch"])
async def test_rejected_labels_retry_once_and_reuse_the_cache(router, status, operation):
    router.reject_status = status
    labels = {"context": "initial search", "metadata": {"step": "initial"}}
    first = await invoke(operation, session="session-first", **labels)
    assert first.ok if isinstance(first, PageText) else first.status == "ok"
    assert not first.cached and len(router.calls) == 2
    assert all(body["session_id"] == "session-first" for _, body in router.calls)
    assert "context" in router.calls[0][1]["metadata"]
    assert "context" not in router.calls[1][1]["metadata"]
    assert "step" not in router.calls[1][1]["metadata"]
    assert (await invoke(operation)).cached
    assert len(router.calls) == 2
    cache, = config.CACHE_DIR.glob("*.json")
    accepted = json.loads(cache.read_text())["request"]
    assert "context" not in accepted and "metadata" not in accepted

    router.reject_status = None
    assert (await invoke(operation, **labels)).cached
    assert len(router.calls) == 2
    assert config.SEARCH_BUDGET.spent == pytest.approx(first.cost_usd)
    assert config.SEARCH_BUDGET.reserved == 0


@pytest.mark.parametrize("operation", ["pinned", "default", "fetch"])
async def test_adding_labels_to_a_cached_plain_call_does_not_pay_again(router, operation):
    first = await invoke(operation)
    cached = await invoke(operation, context="rescue search", metadata={"step": "rescue"})
    assert cached.cached and len(router.calls) == 1
    assert cached.cost_usd == first.cost_usd == config.SEARCH_BUDGET.spent


@pytest.mark.parametrize("operation", ["pinned", "default", "fetch"])
async def test_concurrent_calls_with_different_labels_pay_once(router, operation):
    results = await asyncio.gather(*(invoke(operation, context=f"step {index}", metadata={"step": index})
                                     for index in range(8)))
    assert len(router.calls) == 1
    assert sum(result.cached for result in results) == 7
    assert config.SEARCH_BUDGET.spent == pytest.approx(results[0].cost_usd)
    assert config.SEARCH_BUDGET.reserved == 0
