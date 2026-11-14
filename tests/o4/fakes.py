"""A deterministic agent substitute for HTTP integration tests."""
import asyncio
from pathlib import Path
from uuid import uuid4

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
