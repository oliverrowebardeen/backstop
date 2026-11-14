"""Cached Telem search and page reads with bounded live spend.

The first live call creates a session. Copy its EngineRun.session_id into
RunContext.session and pass that session to subsequent searches and fetches.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from typing import Any

from telem import AsyncTelem

from so import config
from so.contracts import EngineRun, PageText, Result

_SUCCESS = {"ok", "success", "succeeded"}


def _snippet(row: dict[str, Any]) -> str:
    value = row.get("summary") or row.get("excerpt") or ""
    return (" ".join(value) if isinstance(value, list) else value)[:500]


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
                  session: str | None) -> dict[str, EngineRun]:
    if not query.strip() or not 1 <= num_results <= 20:
        raise ValueError("Search needs a nonempty query and 1 to 20 results")
    request: dict[str, Any] = dict(query=query, num_results=num_results,
                                  goal=goal or query, tier="default")
    if engines is not None:
        request["providers_include"] = engines
    else:
        request["auto_routing"] = "accuracy"
    if published_after is not None:
        request["published_after"] = published_after
    key = config.cache_key("telem-search-v1", request)
    async with config.semaphore(key, 1):
        cached = config.read_cache(key)
        if cached:
            return {name: EngineRun.model_validate(value).model_copy(
                update={"cached": True, "session_id": session})
                for name, value in cached["normalized"].items()}
        # Auto-routing can select any available web engine. Reserve all known prices.
        possible = engines if engines is not None else list(set(config.PRICE_USD) | {"exa"})
        estimate = sum(config.PRICE_USD.get(e, config.DEFAULT_PRICE_USD) for e in possible)
        async with config.semaphore("telem", config.TELEM_MAX_CONCURRENCY):
            config.SEARCH_BUDGET.reserve(estimate)
            start = time.perf_counter()
            try:
                async with AsyncTelem() as client:
                    response = await config.retry(lambda: client.search(**request, session=session))
                raw = response.model_dump(mode="json")
            except BaseException:
                config.SEARCH_BUDGET.settle(estimate, 0)
                raise
            elapsed = (time.perf_counter() - start) * 1000
            runs = _normalize(raw, elapsed, engines)
            cost = sum(r.cost_usd for r in runs.values())
            config.write_cache(key, {"request": request, "raw": raw,
                                    "normalized": {e: r.model_dump(mode="json") for e, r in runs.items()}})
            config.SEARCH_BUDGET.settle(estimate, cost)
            return runs


async def search_engines(query: str, engines: list[str], *, num_results: int = 5,
                         goal: str | None = None, published_after: str | None = None,
                         session: str | None = None) -> dict[str, EngineRun]:
    """Search only the named engines; keep failed engines in the returned mapping."""
    if not engines:
        raise ValueError("At least one engine is required")
    return await _search(query, list(dict.fromkeys(engines)), num_results=num_results,
                         goal=goal, published_after=published_after, session=session)


async def search_default(query: str, *, goal: str | None = None,
                         session: str | None = None) -> dict[str, EngineRun]:
    """Ask Telem to choose engines using accuracy routing."""
    return await _search(query, None, num_results=5, goal=goal,
                         published_after=None, session=session)


async def fetch_page(url: str, *, session: str | None = None) -> PageText:
    """Read one page through Telem's fetch API in the caller's session."""
    request = dict(urls=[url], providers=["tavily"], inline_content=True,
                   inline_max_chars=6000, content_format="text")
    key = config.cache_key("telem-fetch-v1", request)
    async with config.semaphore(key, 1):
        cached = config.read_cache(key)
        if cached:
            return PageText.model_validate(cached["normalized"]).model_copy(
                update={"cached": True, "session_id": session})
        async with config.semaphore("telem", config.TELEM_MAX_CONCURRENCY):
            estimate = config.TELEM_FETCH_PRICE_USD
            config.SEARCH_BUDGET.reserve(estimate)
            start = time.perf_counter()
            try:
                async with AsyncTelem() as client:
                    response = await config.retry(lambda: client.fetch(**request, session=session))
                raw = response.model_dump(mode="json")
            except BaseException:
                config.SEARCH_BUDGET.settle(estimate, 0)
                raise
            row = next(iter(raw.get("results") or []), {})
            ok = row.get("status") in _SUCCESS
            page = PageText(url=url, ok=ok, text=(row.get("content") or "")[:6000] if ok else "",
                            latency_ms=(time.perf_counter() - start) * 1000,
                            cost_usd=estimate if ok else 0, session_id=raw.get("session_id") or session)
            config.write_cache(key, {"request": request, "raw": raw, "normalized": page.model_dump(mode="json")})
            config.SEARCH_BUDGET.settle(estimate, page.cost_usd)
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
