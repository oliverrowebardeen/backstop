"""Tavily Extract page reads sharing the search cache and live-spend budget."""
from __future__ import annotations

import time

from tavily import AsyncTavilyClient

from so import config
from so.contracts import PageText


async def tavily_extract(url: str) -> PageText:
    """Extract one page; failed URLs return ok=False for Telem fallback."""
    request = dict(urls=[url], extract_depth=config.TAVILY_EXTRACT_DEPTH,
                   format=config.PAGE_CONTENT_FORMAT, timeout=config.TAVILY_TIMEOUT_S)
    key = config.cache_key("tavily-extract-v1", {"endpoint": config.TAVILY_API_URL, **request})
    async with config.semaphore(key, 1):
        cached = config.read_cache(key)
        if cached:
            return PageText.model_validate(cached["normalized"]).model_copy(update={"cached": True})
        api_key = config.api_key("tavily")
        async with config.semaphore("telem", config.TELEM_MAX_CONCURRENCY):
            estimate = config.TAVILY_EXTRACT_PRICE_USD
            config.SEARCH_BUDGET.reserve(estimate)
            start = time.perf_counter()
            try:
                async with AsyncTavilyClient(api_key=api_key, api_base_url=config.TAVILY_API_URL) as client:
                    raw = await config.retry(lambda: client.extract(**request))
            except BaseException:
                config.SEARCH_BUDGET.settle(estimate, 0)
                raise
            with config.paid_response(key, request, raw, config.SEARCH_BUDGET, estimate) as record:
                row = next(iter(raw["results"]), None)
                page = PageText(url=url, ok=row is not None,
                                text=(row.get("raw_content") or "")[:config.PAGE_TEXT_CHARS] if row else "",
                                latency_ms=(time.perf_counter() - start) * 1000,
                                cost_usd=estimate if row is not None else 0)
                record.update(normalized=page.model_dump(mode="json"), _cost_usd=page.cost_usd)
            return page
