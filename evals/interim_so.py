"""Interim stand-in for so.telem_client and so.llm (lane S2 only).

Used by evals/deps.py ONLY when so.telem_client / so.llm are not merged yet, so
the hit matrix can start on time. It follows the section 6.4 signatures and
reuses Oliver's so.config helpers (prices, cache, budget, semaphore, retry).
Once O0's wrappers land, deps.py picks them up automatically and this file is
no longer imported. Matrix rows already written stay valid (same contracts).
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

from so import config
from so.contracts import EngineRun, Result

config.load_env()


def _snippet(r) -> str:
    text = getattr(r, "summary", None) or getattr(r, "excerpt", None) or ""
    return str(text)[:500]


def _result(r, engine: str, i: int) -> Result:
    return Result(rank=int(getattr(r, "rank", None) or i), title=str(getattr(r, "title", "") or ""),
                  url=str(getattr(r, "url", "") or ""), snippet=_snippet(r),
                  published=(str(getattr(r, "publish_date")) if getattr(r, "publish_date", None) else None),
                  engine=engine)


def _price(engine: str) -> float:
    return config.PRICE_USD.get(engine, config.DEFAULT_PRICE_USD)


async def _telem_search(namespace: str, request: dict, expected_cost: float) -> dict[str, EngineRun]:
    from telem import AsyncTelem

    key = config.cache_key(namespace, request)
    hit = config.read_cache(key)
    if hit is not None:
        return {e: EngineRun.model_validate({**r, "cached": True}) for e, r in hit["runs"].items()}
    async with config.semaphore("telem", config.TELEM_MAX_CONCURRENCY):
        config.SEARCH_BUDGET.reserve(expected_cost)
        actual = 0.0
        try:
            t0 = time.perf_counter()

            async def call():
                async with AsyncTelem() as client:
                    q = request["query"]
                    kw = {k: v for k, v in request.items() if k != "query" and v is not None}
                    return await client.search(q, **kw)

            resp = await config.retry(call)
            wall = (time.perf_counter() - t0) * 1000
            runs = {}
            for run in resp.by_provider:
                engine = run.provider
                ok = run.status in ("succeeded", "ok", "success") and not run.error
                cost = _price(engine) if ok else 0.0
                actual += cost
                runs[engine] = EngineRun(
                    engine=engine, status="ok" if ok else "error",
                    results=[_result(r, engine, i) for i, r in enumerate(run.results or [], 1)],
                    latency_ms=float(run.latency_ms or wall), cost_usd=cost, cached=False,
                    session_id=getattr(resp, "session_id", None))
        finally:
            config.SEARCH_BUDGET.settle(expected_cost, actual)
    if runs and all(r.status == "ok" for r in runs.values()):
        config.write_cache(key, {"runs": {e: r.model_dump() for e, r in runs.items()}})
    return runs


async def search_engines(query, engines, *, num_results=5, goal=None, published_after=None, session=None):
    request = {"query": query, "providers_include": list(engines), "num_results": num_results,
               "goal": goal, "published_after": published_after, "session": session}
    runs = await _telem_search("interim-search", request, sum(_price(e) for e in engines))
    for e in engines:  # an engine Telem silently dropped counts as an error
        runs.setdefault(e, EngineRun(engine=e, status="error"))
    return runs


async def search_default(query, *, goal=None, session=None):
    request = {"query": query, "auto_routing": "accuracy", "goal": goal, "session": session}
    return await _telem_search("interim-default", request, 3 * config.DEFAULT_PRICE_USD)


async def fetch_page(url):
    raise NotImplementedError("fetch_page is not needed by the matrix")


@dataclass
class LLMResponse:
    text: str
    tool_calls: list = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    json: dict | None = None


_MODELS = {"judge": lambda: config.JUDGE_MODEL, "checker": lambda: config.CHECKER_MODEL,
           "agent": lambda: config.AGENT_MODEL}


def _strict(schema: dict) -> dict:
    """Structured outputs want additionalProperties false and no type unions."""
    s = json.loads(json.dumps(schema))
    if s.get("type") == "object":
        s["additionalProperties"] = False
        for k, v in s.get("properties", {}).items():
            if isinstance(v.get("type"), list):
                s["properties"][k] = {"anyOf": [{"type": t} for t in v["type"]]}
    return s


async def complete(role, system, messages, *, tools=None, json_schema=None):
    from anthropic import AsyncAnthropic

    model = _MODELS[role]()
    price_in, price_out = config.LLM_PRICE_USD.get(model, (2.0, 10.0))
    kwargs = dict(model=model, max_tokens=1024, system=system, messages=messages)
    if json_schema:
        kwargs["output_config"] = {"format": {"type": "json_schema", "schema": _strict(json_schema)}}
    async with config.semaphore("llm", config.LLM_MAX_CONCURRENCY):
        config.LLM_BUDGET.reserve(0.02)
        cost = 0.0
        try:
            t0 = time.perf_counter()
            async with AsyncAnthropic() as client:
                msg = await client.messages.create(**kwargs)
            latency = (time.perf_counter() - t0) * 1000
            u = msg.usage
            cost = (u.input_tokens * price_in + u.output_tokens * price_out) / 1e6
        finally:
            config.LLM_BUDGET.settle(0.02, cost)
    if msg.stop_reason == "refusal":
        raise RuntimeError(f"model refused: {getattr(msg, 'stop_details', None)}")
    text = "".join(b.text for b in msg.content if b.type == "text")
    parsed = None
    if json_schema:
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", text, re.S)
            parsed = json.loads(m.group(0)) if m else None
    return LLMResponse(text=text, tokens_in=u.input_tokens, tokens_out=u.output_tokens,
                       cost_usd=cost, latency_ms=latency, json=parsed)
