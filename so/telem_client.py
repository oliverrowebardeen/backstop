"""Cached Telem search and page reads with bounded live spend.

The first live call creates a session. Copy its EngineRun.session_id into
RunContext.session and pass that session to subsequent searches and fetches.

Ledger fields: every search and fetch may carry Telem's `context` string and a
`metadata` dict naming the Backstop step (see so.ledger). They label the call in
Telem's session console only: they never enter the cache key, and if Telem
rejects them the call is retried once without them (so.ledger.call_with_ledger).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from collections.abc import Callable
from contextvars import ContextVar
from typing import Any

from telem import AsyncTelem

from so import config
from so.contracts import EngineRun, PageText, Result
from so.ledger import call_with_ledger

_SUCCESS = {"ok", "success", "succeeded"}



ResultFilter = Callable[[list[Result]], list[Result]]
#: Optional per-task filter over every engine's results, applied to fresh and cached
#: responses alike. Unset in normal use. An evaluation harness can set it (for example
#: to drop pages that copy a public benchmark) so every setup, the checker and the
#: agent see the same filtered lists. Results are renumbered 1..n after filtering.
RESULT_FILTER: ContextVar[ResultFilter | None] = ContextVar("telem_result_filter", default=None)


def _filtered(runs: dict[str, EngineRun]) -> dict[str, EngineRun]:
    keep = RESULT_FILTER.get()
    if keep is None:
        return runs
    return {name: run.model_copy(update={"results": [
        result.model_copy(update={"rank": rank}) for rank, result in enumerate(keep(list(run.results)), 1)]})
        for name, run in runs.items()}

def _snippet(row: dict[str, Any]) -> str:
    value = row.get("summary") or row.get("excerpt") or ""
    return (" ".join(value) if isinstance(value, list) else value)[:config.RESULT_SNIPPET_CHARS]


def _failure_cache_fresh(record: dict[str, Any]) -> bool:
    """Old failures without expiry metadata are retried immediately."""
    return float(record.get("expires_at", 0)) > time.time()


def _normalize(raw: dict[str, Any], elapsed_ms: float, engines: list[str] | None) -> dict[str, EngineRun]:
    runs = {}
    for run in raw.get("by_provider", []):
        engine = run["provider"]
        ok = run.get("status") in _SUCCESS
        results = [Result(
            rank=row.get("rank") or index,
            title=row.get("title") or "",
            url=row["url"],
            snippet=_snippet(row),
            published=row.get("publish_date"),
            engine=engine,
        ) for index, row in enumerate(run.get("results") or [], 1)] if ok else []
        runs[engine] = EngineRun(
            engine=engine, status="ok" if ok else "error", results=results,
            latency_ms=run.get("latency_ms") if run.get("latency_ms") is not None else elapsed_ms,
            cost_usd=config.PRICE_USD.get(engine, config.DEFAULT_PRICE_USD) if ok else 0,
            session_id=raw.get("session_id"),
        )
    for engine in engines or []:
        runs.setdefault(engine, EngineRun(engine=engine, status="error", latency_ms=elapsed_ms,
                                         session_id=raw.get("session_id")))
    return runs


async def _search(query: str, engines: list[str] | None, *, num_results: int,
                  goal: str | None, published_after: str | None,
                  session: str | None, context: str | None = None,
                  metadata: dict[str, Any] | None = None) -> dict[str, EngineRun]:
    if not query.strip() or not 1 <= num_results <= 20:
        raise ValueError("Search needs a nonempty query and 1 to 20 results")
    request: dict[str, Any] = dict(query=query, num_results=num_results,
                                  goal=goal or query, tier=config.TELEM_TIER)
    if engines is not None:
        request["providers_include"] = engines
    else:
        request["auto_routing"] = "accuracy"
    if published_after is not None:
        request["published_after"] = published_after
    key = config.cache_key("telem-search-v1", {"endpoint": config.TELEM_BASE_URL, **request})
    async with config.semaphore(key, 1):
        cached = config.read_cache(key)
        if cached:
            runs = {name: EngineRun.model_validate(value).model_copy(
                update={"cached": True, "session_id": session})
                for name, value in cached["normalized"].items()}
            if (runs and all(run.status == "ok" for run in runs.values())) or _failure_cache_fresh(cached):
                return _filtered(runs)
        # Auto-routing can select any available web engine. Reserve all known prices.
        possible = engines if engines is not None else config.TELEM_ROUTING_ENGINES
        estimate = sum(config.PRICE_USD.get(e, config.DEFAULT_PRICE_USD) for e in possible)
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
                runs = _normalize(raw, elapsed, engines)
                record["_cost_usd"] = sum(r.cost_usd for r in runs.values())
                record["normalized"] = {e: r.model_dump(mode="json") for e, r in runs.items()}
                if not runs or any(run.status != "ok" for run in runs.values()):
                    record["expires_at"] = time.time() + config.TELEM_ERROR_CACHE_TTL_S
            return _filtered(runs)


async def search_engines(query: str, engines: list[str], *, num_results: int = config.TELEM_NUM_RESULTS,
                         goal: str | None = None, published_after: str | None = None,
                         session: str | None = None, context: str | None = None,
                         metadata: dict[str, Any] | None = None) -> dict[str, EngineRun]:
    """Search only the named engines; keep failed engines in the returned mapping.

    context and metadata are ledger labels for Telem's console (see so.ledger).
    """
    if not engines:
        raise ValueError("At least one engine is required")
    return await _search(query, list(dict.fromkeys(engines)), num_results=num_results,
                         goal=goal, published_after=published_after, session=session,
                         context=context, metadata=metadata)


async def search_default(query: str, *, goal: str | None = None, session: str | None = None,
                         context: str | None = None,
                         metadata: dict[str, Any] | None = None) -> dict[str, EngineRun]:
    """Ask Telem to choose engines using accuracy routing."""
    return await _search(query, None, num_results=config.TELEM_NUM_RESULTS, goal=goal,
                         published_after=None, session=session, context=context, metadata=metadata)


async def fetch_page(url: str, *, session: str | None = None, goal: str | None = None,
                     metadata: dict[str, Any] | None = None) -> PageText:
    """Read a page in the caller's session. Telem fetch has no goal parameter.

    goal is accepted for caller compatibility and ignored, including in cache keys.
    metadata is a ledger label for Telem's console and is not part of the cache key.
    """
    request = dict(urls=[url], providers=[config.TELEM_FETCH_PROVIDER], inline_content=True,
                   inline_max_chars=config.PAGE_TEXT_CHARS, content_format=config.PAGE_CONTENT_FORMAT)
    key = config.cache_key("telem-fetch-v1", {"endpoint": config.TELEM_BASE_URL, **request})
    async with config.semaphore(key, 1):
        cached = config.read_cache(key)
        if cached:
            page = PageText.model_validate(cached["normalized"]).model_copy(
                update={"cached": True, "session_id": session})
            if page.ok or _failure_cache_fresh(cached):
                return page
        async with config.semaphore("telem", config.TELEM_MAX_CONCURRENCY):
            estimate = config.TELEM_FETCH_PRICE_USD
            config.SEARCH_BUDGET.reserve(estimate)
            start = time.perf_counter()
            try:
                async with AsyncTelem(api_key=config.api_key("telem"), base_url=config.TELEM_BASE_URL,
                                      timeout=config.TELEM_TIMEOUT_S) as client:
                    response = await call_with_ledger(
                        lambda extra: config.retry(lambda: client.fetch(**request, session=session, **extra)),
                        {"metadata": metadata})
                raw = response.model_dump(mode="json")
            except BaseException:
                config.SEARCH_BUDGET.settle(estimate, 0)
                raise
            with config.paid_response(key, request, raw, config.SEARCH_BUDGET, estimate) as record:
                row = next(iter(raw.get("results") or []), {})
                ok = row.get("status") in _SUCCESS
                page = PageText(url=url, ok=ok, text=(row.get("content") or "")[:config.PAGE_TEXT_CHARS] if ok else "",
                                latency_ms=(time.perf_counter() - start) * 1000,
                                cost_usd=estimate if ok else 0, session_id=raw.get("session_id") or session)
                record.update(normalized=page.model_dump(mode="json"), _cost_usd=page.cost_usd)
                if not page.ok:
                    record["expires_at"] = time.time() + config.TELEM_ERROR_CACHE_TTL_S
            return page


def main() -> None:
    parser = argparse.ArgumentParser(description="Search through Telem and print normalized results")
    parser.add_argument("query")
    parser.add_argument("--engines", nargs="+", default=[config.FIRST_ENGINE])
    parser.add_argument("--goal")
    parser.add_argument("--session")
    args = parser.parse_args()
    runs = asyncio.run(search_engines(args.query, args.engines, goal=args.goal, session=args.session))
    print(json.dumps({e: r.model_dump(mode="json") for e, r in runs.items()}, indent=2))
    if any(run.status != "ok" for run in runs.values()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
