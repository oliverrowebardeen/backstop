"""Bounded, evidence-based tool loop with one Telem session per question."""
from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path
from time import perf_counter
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from so import llm, telem_client
from so.config import AGENT_MODEL
from so.contracts import AgentAnswer, Question, RunContext, ToolCall
from so.eventlog import EventLog
from so.setups import SearchTool, get_search_tool, single

MAX_SEARCHES = 6
MAX_PAGE_READS = 12
MAX_TURNS = 24
SYSTEM_PROMPT = Path(__file__).with_name("prompts").joinpath("agent.md").read_text()
TOOLS = [
    {"type": "function", "function": {
        "name": "search", "description": "Find web evidence for a concrete goal.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}, "goal": {"type": "string"},
        }, "required": ["query", "goal"], "additionalProperties": False},
    }},
    {"type": "function", "function": {
        "name": "read_page", "description": "Read a source page through Telem fetch.",
        "parameters": {"type": "object", "properties": {"url": {"type": "string"}},
                       "required": ["url"], "additionalProperties": False},
    }},
]


class _RunLog:
    """Count the same events written to disk, including nested checker calls."""

    def __init__(self, log: EventLog) -> None:
        self.log = log
        self.engine_calls = 0
        self.fetches = 0
        self.cost_search = 0.0
        self.cost_llm = 0.0
        self.operation_ms = 0.0

    def emit(self, event_type: str, **data: Any) -> Any:
        event = self.log.emit(event_type, **data)
        if event_type == "search":
            self.engine_calls += len(data.get("engines", []))
        if event_type == "fetch":
            self.fetches += 1
        if event_type in {"search", "fetch"}:
            self.cost_search += data.get("cost_usd", 0)
        if event_type == "llm":
            self.cost_llm += data.get("cost_usd", 0)
        if event_type in {"search", "fetch", "llm"}:
            self.operation_ms += data.get("latency_ms", 0)
        return event


def _http_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    except ValueError:
        return False


def _answer(text: str, sources: set[str], searches: int) -> AgentAnswer:
    """Accept citations only from retrieved evidence, including plain-text replies."""
    final = text.strip()
    payload: dict[str, Any] = {}
    try:
        candidate = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", final))
        if isinstance(candidate, dict):
            payload = candidate
            value = payload.get("final")
            final = value.strip() if isinstance(value, str) else ""
    except json.JSONDecodeError:
        pass
    urls = re.findall(r"https?://[^\s<>\[\]\"']+", final)
    sources_by_length = sorted(sources, key=len, reverse=True)
    urls = [next((source for source in sources_by_length
                  if url.startswith(source) and not url[len(source):].strip(".,;:!?)}")),
                 url.rstrip(".,;:!?)}")) for url in urls]
    cited = payload.get("citations", [])
    if not isinstance(cited, list):
        cited = []
    citations = list(dict.fromkeys(url for url in [*cited, *urls]
                                  if isinstance(url, str) and url in sources))
    if (not final or final.upper().rstrip(".! ") in {"NOT FOUND", "NOT_FOUND"}
            or payload.get("status") == "NOT_FOUND" or not citations
            or any(url not in sources for url in urls)):
        return AgentAnswer(final="NOT FOUND", status="NOT_FOUND", searches=searches)
    return AgentAnswer(final=final, status="ANSWERED", citations=citations, searches=searches)


async def _tool_result(call: ToolCall, ctx: RunContext, search: SearchTool,
                       counts: dict[str, int], sources: set[str]) -> dict[str, Any]:
    arguments = call.arguments
    if call.name == "search":
        if counts["searches"] >= MAX_SEARCHES:
            return {"error": "Search limit reached. Answer from existing evidence or NOT FOUND."}
        if any(not isinstance(arguments.get(key), str) or not arguments[key].strip()
               for key in ("query", "goal")):
            return {"error": "search requires nonempty query and goal strings."}
        counts["searches"] += 1
        outcome = await search(arguments["query"], arguments["goal"], ctx)
        sources.update(result.url for result in outcome.results if _http_url(result.url))
        return outcome.model_dump(mode="json")
    if call.name == "read_page":
        if counts["reads"] >= MAX_PAGE_READS:
            return {"error": "Page read limit reached. Answer from existing evidence or NOT FOUND."}
        url = arguments.get("url")
        if not _http_url(url):
            return {"error": "read_page requires an HTTP or HTTPS URL."}
        counts["reads"] += 1
        page = await telem_client.fetch_page(url, session=ctx.session)
        if ctx.session is None and page.session_id:
            ctx.session = page.session_id
        ctx.log.emit("fetch", url=page.url, ok=page.ok, via="telem",
                     latency_ms=page.latency_ms, cost_usd=page.cost_usd,
                     cached=page.cached, session=ctx.session)
        if page.ok and page.text.strip():
            sources.add(page.url)
        return page.model_dump(mode="json")
    return {"error": f"Unknown tool: {call.name}"}


async def run_question(question: Question, setup: str, *, log_dir="out/logs") -> AgentAnswer:
    """Answer a question and write an auditable, fully costed event log."""
    search = get_search_tool(setup)
    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", question.id)
    run_id = f"{setup}-{safe_id}-{uuid4().hex[:8]}"
    log = _RunLog(EventLog(run_id, question.id, setup, log_dir=log_dir))
    counts = {"searches": 0, "reads": 0}
    started = perf_counter()
    error: str | None = None
    ctx = RunContext(run_id=run_id, question_id=question.id, setup=setup, log=log)
    try:
        log.emit("run_start", question=question.question,
                 effective_setup="single" if setup == "second_opinion" and search is single else setup)
        messages: list[dict[str, Any]] = [{"role": "user", "content": question.question}]
        sources: set[str] = set()
        answer = AgentAnswer(final="NOT FOUND", status="NOT_FOUND")
        for turn in range(MAX_TURNS + 1):
            finishing = turn == MAX_TURNS
            if finishing:
                messages.append({"role": "user", "content":
                                 "Tool limit reached. Give your final answer from the evidence or NOT FOUND."})
            response = await llm.complete("agent", SYSTEM_PROMPT, messages,
                                          tools=None if finishing else TOOLS)
            log.emit("llm", role="agent", model=response.model or AGENT_MODEL,
                     tokens_in=response.tokens_in, tokens_out=response.tokens_out,
                     cost_usd=response.cost_usd, latency_ms=response.latency_ms,
                     cached=response.cached)
            if not response.tool_calls:
                answer = _answer(response.text, sources, counts["searches"])
                break
            if finishing:
                break
            messages.append({"role": "assistant", "content": response.text or None,
                             "tool_calls": [{"id": call.id, "type": "function", "function": {
                                 "name": call.name, "arguments": json.dumps(call.arguments),
                             }} for call in response.tool_calls]})
            for call in response.tool_calls:
                result = await _tool_result(call, ctx, search, counts, sources)
                messages.append({"role": "tool", "tool_call_id": call.id,
                                 "content": json.dumps(result)})
        answer.searches = counts["searches"]
        log.emit("answer", **answer.model_dump())
        return answer
    except BaseException as exc:
        error = type(exc).__name__
        raise
    finally:
        wall_ms = (perf_counter() - started) * 1000
        log.emit("run_end", engine_calls=log.engine_calls, fetches=log.fetches,
                 searches=counts["searches"], cost_usd_search=log.cost_search,
                 cost_usd_llm=log.cost_llm, cost_usd_total=log.cost_search + log.cost_llm,
                 latency_ms=max(wall_ms, log.operation_ms), wall_latency_ms=wall_ms,
                 session=ctx.session,
                 **({"error": error} if error else {}))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--setup", choices=("single", "telem_default", "second_opinion"), required=True)
    parser.add_argument("--question", required=True)
    parser.add_argument("--log-dir", default="out/logs")
    args = parser.parse_args()
    question = Question(id="cli", question=args.question, gold=[], category="fact", source="cli", split="test")
    answer = asyncio.run(run_question(question, args.setup, log_dir=args.log_dir))
    print(answer.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
