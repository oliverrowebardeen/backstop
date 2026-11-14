"""Fake so.telem_client and so.llm with the production call signatures.

Deterministic: an engine "hits" when hash(query, engine) says so, and the hit
snippet contains the marker GOLD_MARKER so the fake judge can find it.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass, field

from so import config
from so.contracts import EngineRun, Result

PRICE = config.PRICE_USD
GOLD_MARKER = "<<gold>>"
CALLS = {"search": 0, "default": 0, "llm": 0}


def _h(*parts) -> int:
    return int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest(), 16)


def _run(query: str, engine: str, n: int) -> EngineRun:
    hit_rank = (_h(query, engine) % 8) + 1  # 1..8; >5 means a miss
    results = [
        Result(rank=i, title=f"{engine} result {i}", url=f"https://{engine}.example/{_h(query) % 997}/{i}",
               snippet=(f"snippet {GOLD_MARKER}" if i == hit_rank else "unrelated snippet"),
               engine=engine)
        for i in range(1, n + 1)
    ]
    return EngineRun(engine=engine, status="ok", results=results, latency_ms=100 + _h(engine) % 900,
                     cost_usd=PRICE.get(engine, config.DEFAULT_PRICE_USD), cached=False)


async def search_engines(query, engines, *, num_results=5, goal=None, published_after=None, session=None):
    CALLS["search"] += 1
    await asyncio.sleep(0)
    return {e: _run(query, e, num_results) for e in engines}


async def search_default(query, *, goal=None, session=None):
    CALLS["default"] += 1
    await asyncio.sleep(0)
    chosen = config.ENGINES[:3]
    return {e: _run(query, e, 5) for e in chosen}


async def fetch_page(url):
    return {"url": url, "ok": True, "text": "page", "latency_ms": 50, "cost_usd": 0.001, "cached": False}


@dataclass
class LLMResponse:
    text: str
    tool_calls: list = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0


async def complete(role, system, messages, *, tools=None, json_schema=None):
    """Fake judge: hit iff a numbered result line contains GOLD_MARKER."""
    CALLS["llm"] += 1
    await asyncio.sleep(0)
    prompt = messages[-1]["content"] if messages else ""
    if "ANSWER TO GRADE" in prompt:
        ans = prompt.split("ANSWER TO GRADE", 1)[1]
        verdict = "correct" if "correct" in ans.lower() else "wrong"
        return LLMResponse(text=json.dumps({"grade": verdict, "reason": "fake"}), cost_usd=0.001)
    rank = None
    for m in re.finditer(r"^\[(\d+)\].*$", prompt, re.M):
        block = prompt[m.start(): prompt.find("\n[", m.end()) if "\n[" in prompt[m.end():] else len(prompt)]
        if GOLD_MARKER in block:
            rank = int(m.group(1))
            break
    out = {"hit": rank is not None, "hit_rank": rank, "reason": "fake judge"}
    return LLMResponse(text=json.dumps(out), tokens_in=500, tokens_out=30, cost_usd=0.002, latency_ms=10)
