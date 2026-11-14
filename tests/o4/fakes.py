"""A deterministic agent substitute for HTTP integration tests."""
import asyncio
from pathlib import Path
from uuid import uuid4

import httpx

from so.contracts import AgentAnswer, Question
from so.eventlog import EventLog


async def run_question(question: Question, setup: str, *, log_dir: str) -> AgentAnswer:
    log = EventLog(f"{setup}-{question.id}-{uuid4().hex[:6]}", question.id, setup, log_dir=log_dir)
    log.emit("run_start", question=question.question, session_id="test-session")
    await asyncio.sleep(0.01)
    log.emit("search", query=question.question, goal="Find supporting evidence", engines=["tavily"],
             n_results=1, latency_ms=10, cost_usd=.005, cached=False)
    answer = AgentAnswer(final="Supported test answer", status="ANSWERED", citations=["https://example.org/source"], searches=1)
    log.emit("answer", **answer.model_dump())
    log.emit("run_end", engine_calls=1, fetches=0, searches=1, cost_usd_search=.005,
             cost_usd_llm=0, cost_usd_total=.005, latency_ms=20)
    assert Path(log_dir).is_dir()
    return answer


async def interrupted_question(question: Question, setup: str, *, log_dir: str) -> AgentAnswer:
    log = EventLog(f"{setup}-{question.id}-interrupted", question.id, setup, log_dir=log_dir)
    log.emit("run_start", question=question.question)
    await asyncio.sleep(.1)
    raise RuntimeError("Test agent stopped")


async def usage_limit_question(question: Question, setup: str, *, log_dir: str) -> AgentAnswer:
    """Match the agent's run_end-in-finally behavior when the provider rejects a call."""
    log = EventLog(f"{setup}-{question.id}-usage-limit", question.id, setup, log_dir=log_dir)
    log.emit("run_start", question=question.question)
    try:
        await asyncio.sleep(.01)
        request = httpx.Request("POST", "https://provider.invalid/messages")
        response = httpx.Response(400, request=request, json={"error": {
            "type": "invalid_request_error", "message": "Your account has reached its usage limit."}})
        response.raise_for_status()
    finally:
        log.emit("run_end", engine_calls=0, fetches=0, searches=0, cost_usd_search=0,
                 cost_usd_llm=0, cost_usd_total=0, latency_ms=10, error="HTTPStatusError")
        # Let a stream observe run_end before the task raises, as can happen in cleanup.
        await asyncio.sleep(.03)
