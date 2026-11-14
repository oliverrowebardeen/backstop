"""Fake Telem, LLM and Extract for Memo Check tests. Nothing here touches a network."""
from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

from so import claims
from so.contracts import EngineRun, LLMResponse, PageText, Result, SearchOutcome
from so.telem_batch import BatchResult

MEMO = ("Stripe was founded in 2010 by Patrick and John Collison. It is headquartered in South San Francisco. "
        "Stripe processed 9 trillion dollars in 2023. Its internal mascot is a llama named Stripey.")

CLAIMS = [
    {"text": "Stripe was founded in 2010.", "quote": "founded in 2010", "kind": "date",
     "entities": ["Stripe"], "query": "Stripe founded year", "goal": "the year Stripe was founded"},
    {"text": "Stripe is headquartered in South San Francisco.", "quote": "headquartered in South San Francisco",
     "kind": "other", "entities": ["Stripe"], "query": "Stripe headquarters", "goal": "Stripe's headquarters city"},
    {"text": "Stripe processed 9 trillion dollars in 2023.", "quote": "processed 9 trillion dollars in 2023",
     "kind": "number", "entities": ["Stripe"], "query": "Stripe total payment volume 2023",
     "goal": "Stripe's total payment volume in 2023"},
    {"text": "Stripe's internal mascot is a llama named Stripey.", "quote": "mascot is a llama named Stripey",
     "kind": "other", "entities": ["Stripe"], "query": "Stripe mascot llama Stripey",
     "goal": "Stripe's internal mascot"},
]


def result(url: str, snippet: str, title: str = "Stripe", engine: str = "tavily", rank: int = 1) -> Result:
    return Result(rank=rank, title=title, url=url, snippet=snippet, engine=engine)


def llm_response(payload: dict[str, Any], cost: float = 0.0002) -> LLMResponse:
    return LLMResponse(text=json.dumps(payload), cost_usd=cost, tokens_in=100, tokens_out=50,
                       model="claude-haiku-5-5", role="checker")


def verdict(v: str, rank: int | None = 1, quote: str = "", why: str = "because", confidence: float = 0.9) -> dict:
    return {"verdict": v, "confidence": confidence, "why": why, "evidence_rank": rank, "evidence_quote": quote}


class Fakes:
    """Install with `install(monkeypatch)`; tweak behavior through the attributes."""

    def __init__(self) -> None:
        self.claims = list(CLAIMS)
        # claim text -> list of verify replies, consumed in order (last one repeats)
        self.verify: dict[str, list[dict[str, Any]]] = {}
        # claim query -> first-pass results
        self.first: dict[str, list[Result]] = {}
        self.domain_results: list[Result] = []
        self.cascade: Callable[[str, str], SearchOutcome] | None = None
        self.page: PageText | None = None
        self.batch_calls: list[dict[str, Any]] = []
        self.cascade_calls: list[str] = []
        self.extract_calls: list[str] = []
        self.llm_calls: list[str] = []
        self.verify_delay = 0.0
        self.batch_error: Exception | None = None
        self.verify_error_for: str | None = None
        self.extract_max_tokens: int | None = None

    async def complete(self, role: str, system: str, messages: list[dict[str, Any]], **kwargs: Any) -> LLMResponse:
        from so import config

        self.llm_calls.append(system[:20])
        user = messages[0]["content"]
        if system == claims.EXTRACT_PROMPT:
            self.extract_max_tokens = config.LLM_MAX_TOKENS[role]
            return llm_response({"claims": self.claims}, cost=0.001)
        if self.verify_delay:
            await asyncio.sleep(self.verify_delay)
        claim_text = user.split("CLAIM: ", 1)[1].split("\n", 1)[0]
        if self.verify_error_for and self.verify_error_for == claim_text:
            raise RuntimeError("verifier exploded")
        replies = self.verify.get(claim_text) or [verdict("IRRELEVANT", None)]
        reply = replies.pop(0) if len(replies) > 1 else replies[0]
        return llm_response(reply)

    async def search_batch(self, queries: list[str], engines: list[str], **kwargs: Any) -> BatchResult:
        self.batch_calls.append({"queries": queries, "engines": engines, **kwargs})
        if self.batch_error and not kwargs.get("include_domains"):
            raise self.batch_error
        runs = []
        for query in queries:
            items = self.domain_results if kwargs.get("include_domains") else self.first.get(query, [])
            runs.append({engines[0]: EngineRun(engine=engines[0], status="ok", results=items, cost_usd=0.005,
                                                session_id="sess-1")})
        return BatchResult(runs=runs, session_id="sess-1", interaction_id="int-1", latency_ms=12)

    async def search(self, query: str, goal: str, ctx: Any, **kwargs: Any) -> SearchOutcome:
        self.cascade_calls.append(query)
        if self.cascade is not None:
            return self.cascade(query, goal)
        return SearchOutcome(status="NOT_FOUND", engines_called=["exa"], calls=1, cost_usd=0.006)

    async def tavily_extract(self, url: str, **kwargs: Any) -> PageText:
        self.extract_calls.append(url)
        return self.page or PageText(url=url, ok=False)

    def install(self, monkeypatch: Any) -> "Fakes":
        monkeypatch.setattr(claims.llm, "complete", self.complete)
        monkeypatch.setattr(claims.telem_batch, "search_batch", self.search_batch)
        monkeypatch.setattr(claims.second_opinion, "search", self.search)
        monkeypatch.setattr(claims.tavily_client, "tavily_extract", self.tavily_extract)
        return self
