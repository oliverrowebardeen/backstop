"""Tavily Extract page reads sharing the search cache and live-spend budget."""
from __future__ import annotations

import time

from tavily import AsyncTavilyClient

from so import config
from so.contracts import PageText


CHUNK_SEPARATOR = "[...]"
"""Tavily joins the chunks of one source in raw_content with this marker."""


def split_chunks(text: str) -> list[str]:
    """Split Extract raw_content into its chunks; text without the marker is one chunk."""
    return [chunk.strip() for chunk in text.split(CHUNK_SEPARATOR) if chunk.strip()]


async def tavily_extract(url: str, *, goal: str | None = None,
                         chunks_per_source: int | None = None) -> PageText:
    """Extract a page, forwarding goal as Tavily's query for content ranking.

    With a goal, Extract returns only the `chunks_per_source` chunks (default
    config.TAVILY_EXTRACT_CHUNKS, 1 to 5, at most 500 characters each) most
    relevant to it, joined by CHUNK_SEPARATOR in `PageText.text`; use
    `split_chunks` to recover them. Tavily accepts chunks only with a query, so
    without a goal the full page text is returned. Basic depth, timeout
    config.TAVILY_EXTRACT_TIMEOUT_S.
    """
    request: dict[str, object] = dict(urls=[url], extract_depth=config.TAVILY_EXTRACT_DEPTH,
                                      format=config.PAGE_CONTENT_FORMAT, timeout=config.TAVILY_EXTRACT_TIMEOUT_S)
    if goal is not None:
        chunks = config.TAVILY_EXTRACT_CHUNKS if chunks_per_source is None else chunks_per_source
        if not 1 <= chunks <= 5:
            raise ValueError(f"chunks_per_source must be between 1 and 5, got {chunks}")
        request["query"] = goal
        request["chunks_per_source"] = chunks
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
