"""Batched Telem search: many queries in one interaction, mapped back by batch_index.

Telem's search accepts a list of query strings and runs them as one
interaction; every ProviderRun in the response carries the `query` it served
and its `batch_index`. This module reuses so.telem_client's cache, budget,
retry, normalization and ledger fail-open logic without changing that module.
"""
from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, Field
from telem import AsyncTelem

from so import config
from so.contracts import EngineRun
from so.ledger import call_with_ledger
from so.telem_client import _failure_cache_fresh, _filtered, _normalize


class BatchResult(BaseModel):
    """One batched call: runs[i] holds query i's engine runs."""

    runs: list[dict[str, EngineRun]] = Field(default_factory=list)
    session_id: str | None = None
    interaction_id: str | None = None
    cached: bool = False
    latency_ms: float = 0.0

    @property
    def cost_usd(self) -> float:
        return sum(run.cost_usd for runs in self.runs for run in runs.values())


def _index(run: dict[str, Any], queries: list[str]) -> int | None:
    """Map a ProviderRun to its query: batch_index first, then the echoed query text."""
    index = run.get("batch_index")
    if isinstance(index, int) and 0 <= index < len(queries):
        return index
    text = run.get("query")
    if isinstance(text, str) and text in queries:
        return queries.index(text)
    if len(queries) == 1:
        return 0
    return None


def split_by_batch(raw: dict[str, Any], queries: list[str], engines: list[str],
                   elapsed_ms: float) -> list[dict[str, EngineRun]]:
    """Normalize a raw batched response into one {engine: EngineRun} per query."""
    groups: list[list[dict[str, Any]]] = [[] for _ in queries]
    for run in raw.get("by_provider") or []:
        index = _index(run, queries)
        if index is not None:
            groups[index].append(run)
    return [_normalize({"by_provider": group, "session_id": raw.get("session_id")}, elapsed_ms, engines)
            for group in groups]


async def search_batch(queries: list[str], engines: list[str], *, num_results: int = 5,
                       goal: str | None = None, session: str | None = None,
                       context: str | None = None, metadata: dict[str, Any] | None = None,
                       include_domains: list[str] | None = None) -> BatchResult:
    """Search every query on the pinned engines in one Telem interaction.

    Cached like so.telem_client (labels never enter the cache key), reserved
    against SEARCH_BUDGET at the full worst case (queries x engines), and
    retried on 429/5xx. If Telem rejects context/metadata, retries once without.
    """
    queries = [q.strip() for q in queries]
    if not queries or not all(queries) or not engines or not 1 <= num_results <= 20:
        raise ValueError("Batch search needs nonempty queries, at least one engine and 1 to 20 results")
    engines = list(dict.fromkeys(engines))
    request: dict[str, Any] = dict(query=queries, providers_include=engines, num_results=num_results,
                                  goal=goal or "verify each factual claim", tier=config.TELEM_TIER)
    if include_domains:
        request["include_domains"] = list(dict.fromkeys(include_domains))[:20]
    key = config.cache_key("telem-batch-v1", {"endpoint": config.TELEM_BASE_URL, **request})
    async with config.semaphore(key, 1):
        cached = config.read_cache(key)
        if cached:
            runs = [{name: EngineRun.model_validate(value).model_copy(update={"cached": True, "session_id": session})
                     for name, value in group.items()} for group in cached["normalized"]]
            ok = runs and all(run.status == "ok" for group in runs for run in group.values())
            if ok or _failure_cache_fresh(cached):
                return BatchResult(runs=[_filtered(group) for group in runs], session_id=session,
                                   interaction_id=None, cached=True)
        estimate = len(queries) * sum(config.PRICE_USD.get(e, config.DEFAULT_PRICE_USD) for e in engines)
        async with config.semaphore("telem", config.TELEM_MAX_CONCURRENCY):
            config.SEARCH_BUDGET.reserve(estimate)
            start = time.perf_counter()
            try:
                async with AsyncTelem(api_key=config.api_key("telem"), base_url=config.TELEM_BASE_URL,
                                      timeout=config.TELEM_TIMEOUT_S) as client:
                    response = await call_with_ledger(
                        lambda extra: config.retry(lambda: client.search(**request, session=session, **extra)),
                        {"context": context, "metadata": metadata})
                raw = response.model_dump(mode="json")
            except BaseException:
                config.SEARCH_BUDGET.settle(estimate, 0)
                raise
            elapsed = (time.perf_counter() - start) * 1000
            with config.paid_response(key, request, raw, config.SEARCH_BUDGET, estimate) as record:
                runs = split_by_batch(raw, queries, engines, elapsed)
                record["_cost_usd"] = sum(r.cost_usd for group in runs for r in group.values())
                record["normalized"] = [{e: r.model_dump(mode="json") for e, r in group.items()} for group in runs]
                if any(run.status != "ok" for group in runs for run in group.values()):
                    record["expires_at"] = time.time() + config.TELEM_ERROR_CACHE_TTL_S
            return BatchResult(runs=[_filtered(group) for group in runs], session_id=raw.get("session_id"),
                               interaction_id=raw.get("interaction_id"), latency_ms=elapsed)
