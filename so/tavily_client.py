"""Tavily Extract page reads sharing the search cache and live-spend budget."""
from __future__ import annotations

import os
import time

from tavily import AsyncTavilyClient

from so import config
from so.contracts import PageText


async def tavily_extract(url: str) -> PageText:
    """Extract one page; failed URLs return ok=False for Telem fallback."""
    request = dict(urls=[url], extract_depth="basic", format="text")
    key = config.cache_key("tavily-extract-v1", request)
    async with config.semaphore(key, 1):
        cached = config.read_cache(key)
        if cached:
            return PageText.model_validate(cached["normalized"]).model_copy(update={"cached": True})
        api_key = os.getenv("TAVILY_API_KEY")
        if not api_key:
            raise ValueError("TAVILY_API_KEY is required")
        async with config.semaphore("telem", config.TELEM_MAX_CONCURRENCY):
            estimate = config.TAVILY_EXTRACT_PRICE_USD
            config.SEARCH_BUDGET.reserve(estimate)
            start = time.perf_counter()
            try:
                async with AsyncTavilyClient(api_key=api_key) as client:
                    raw = await config.retry(lambda: client.extract(**request))
            except BaseException:
                config.SEARCH_BUDGET.settle(estimate, 0)
                raise
            row = next(iter(raw.get("results") or []), None)
            page = PageText(url=url, ok=row is not None,
                            text=(row.get("raw_content") or "")[:6000] if row else "",
                            latency_ms=(time.perf_counter() - start) * 1000,
                            cost_usd=estimate if row is not None else 0)
            config.write_cache(key, {"request": request, "raw": raw, "normalized": page.model_dump(mode="json")})
            config.SEARCH_BUDGET.settle(estimate, page.cost_usd)
            return page
