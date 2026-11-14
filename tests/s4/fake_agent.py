"""Test double for so.agent.run_question (section 6.10); tests install it as evals.run_eval.AGENT.

Writes a full section 6.11 log through the real so.eventlog.EventLog, so every
event is validated against so.contracts.Event. Prices, models and engines come
from so.config. Every other number comes from a hash of (question id, setup,
step), so re-running gives the same log apart from timestamps and run_id.

- single:          FIRST_ENGINE only, no check, status UNCHECKED.
- telem_default:   FIRST_ENGINE plus two more engines per search, no check.
- second_opinion:  FIRST_ENGINE, a checker call per result list, up to two
                   rescues (next engine in FALLBACK_ORDER, or a Tavily Extract
                   read with Telem fetch as fallback), then HIT or NOT_FOUND.

The first search of a question uses the question text as its query, and the
first checker verdict agrees with the fake hit matrix (tests/s2/fakes.py) most
of the time, so the checker join in evals.score gets realistic numbers.

Failure injection for tests: FAKE_AGENT_FAIL="id1,id2" makes those questions
raise after their first search call, leaving a log with no run_end.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from typing import get_args

from so.config import (AGENT_MODEL, CHECKER_MODEL, DEFAULT_PRICE_USD, ENGINES, FALLBACK_ORDER, FIRST_ENGINE,
                       LLM_PRICE_USD, PRICE_USD, TAVILY_EXTRACT_PRICE_USD, TELEM_FETCH_PRICE_USD)
from so.contracts import AgentAnswer, Setup
from so.eventlog import EventLog
from tests.s2.fakes import GOLD_MARKER, _h, _run

MAX_RESCUES = 2  # section 6.8
MISS_VERDICTS = ["WRONG_ENTITY", "STALE", "SHALLOW", "DEAD"]
FETCH_PRICE = {"tavily": TAVILY_EXTRACT_PRICE_USD, "telem": TELEM_FETCH_PRICE_USD}
# Fake outcome mix per setup: P(correct), P(wrong); the rest is declined.
OUTCOME = {"single": (0.60, 0.27), "telem_default": (0.68, 0.20), "second_opinion": (0.67, 0.09)}
assert set(OUTCOME) == set(get_args(Setup))


def _u(*parts) -> float:
    """Deterministic uniform in [0, 1)."""
    return (_h(*parts) % 10_000) / 10_000


def matrix_hit(question_text: str, engine: str) -> bool:
    """Whether the fake matrix (query = question text) scores this engine a hit."""
    return any(GOLD_MARKER in r.snippet for r in _run(question_text, engine, 5).results)


class _Run:
    """One fake run: emits events and keeps the run_end totals."""

    def __init__(self, log: EventLog):
        self.log = log
        self.engine_calls = self.fetches = self.searches = 0
        self.cost_search = self.cost_llm = self.latency = 0.0

    def llm(self, role: str, *key) -> None:
        if role == "agent":
            model, tin, tout, lat = (AGENT_MODEL, 1500 + _h("in", *key) % 2500, 80 + _h("out", *key) % 220,
                                     900 + _h("lat", *key) % 1600)
        else:
            model, tin, tout, lat = (CHECKER_MODEL, 900 + _h("in", *key) % 600, 40 + _h("out", *key) % 60,
                                     250 + _h("lat", *key) % 400)
        rates = LLM_PRICE_USD[model]  # USD per million tokens
        cost = (tin * rates["input"] + tout * rates["output"]) / 1e6
        self.cost_llm += cost
        self.latency += lat
        self.log.emit("llm", role=role, model=model, tokens_in=tin, tokens_out=tout, cost_usd=cost, latency_ms=lat)

    def search(self, query: str, goal: str, engines: list[str], *key) -> None:
        cost = sum(PRICE_USD.get(e, DEFAULT_PRICE_USD) for e in engines)
        lat = 400 + _h("slat", *key) % 1400
        self.engine_calls += len(engines)
        self.cost_search += cost
        self.latency += lat
        self.log.emit("search", query=query, goal=goal, engines=engines, n_results=5 * len(engines),
                      latency_ms=lat, cost_usd=cost, cached=False)

    def fetch(self, url: str, via: str, ok: bool, *key) -> None:
        lat = 300 + _h("flat", *key) % 900
        self.fetches += 1
        self.cost_search += FETCH_PRICE[via]
        self.latency += lat
        self.log.emit("fetch", url=url, ok=ok, via=via, latency_ms=lat, cost_usd=FETCH_PRICE[via])

    def check(self, verdict: str, *key) -> None:
        self.log.emit("check", verdict=verdict, confidence=round(0.55 + _u("c", *key) * 0.4, 2),
                      why=f"fake checker: {verdict.lower()}", evidence_rank=1 if verdict == "HIT" else None)

    def search_done(self, status: str, calls: int, fetches: int, cost0: float, lat0: float, note: str) -> None:
        self.log.emit("search_done", status=status, calls=calls, fetches=fetches,
                      cost_usd=self.cost_search - cost0, latency_ms=self.latency - lat0, note=note)


def _first_verdict(question_text: str, *key) -> str:
    """The checker on the first FIRST_ENGINE result list: right about 85 to 90 percent of the time."""
    if matrix_hit(question_text, FIRST_ENGINE):
        return "HIT" if _u("v", *key) < 0.9 else "SHALLOW"
    if _u("v", *key) < 0.13:
        return "HIT"  # a missed miss
    rescuable = any(matrix_hit(question_text, e) for e in ENGINES if e != FIRST_ENGINE)
    if not rescuable and _u("d", *key) < 0.7:
        return "DEAD"
    return MISS_VERDICTS[_h("kind", *key) % len(MISS_VERDICTS)]


def _second_opinion_call(r: _Run, q, query: str, goal: str, i: int) -> tuple[str, str]:
    """One search tool call in the second_opinion setup. Returns (status, url to cite)."""
    key = (q.id, "second_opinion", i)
    cost0, lat0, calls, fetches = r.cost_search, r.latency, 1, 0
    url = f"https://{FIRST_ENGINE}.example/{_h(query) % 997}/1"
    tried = [FIRST_ENGINE]
    r.search(query, goal, [FIRST_ENGINE], *key, 0)
    r.llm("checker", *key, 0)
    if i == 0:
        verdict = _first_verdict(q.question, *key)
    else:
        verdict = "HIT" if _u("v", *key) < 0.6 else MISS_VERDICTS[_h("k", *key) % len(MISS_VERDICTS)]
    r.check(verdict, *key)
    for rescue in range(1, MAX_RESCUES + 1):
        if verdict == "HIT":
            break
        rkey = (*key, rescue)
        if verdict == "SHALLOW":
            r.log.emit("rescue", action="FETCH_PAGE", engine=None, query=query, reason="answer not in snippet")
            ok = _u("fok", *rkey) < 0.85
            r.fetch(url, "tavily", ok, *rkey)
            fetches += 1
            if not ok:
                r.fetch(url, "telem", True, *rkey, "fallback")
                fetches += 1
        else:
            engine = next(e for e in FALLBACK_ORDER if e not in tried)
            tried.append(engine)
            rq = f"{query} (reworded)"
            r.log.emit("rescue", action="RETRY_ENGINE", engine=engine, query=rq, reason=verdict.lower())
            r.search(rq, goal, [engine], *rkey)
            calls += 1
            url = f"https://{engine}.example/{_h(rq) % 997}/1"
        r.llm("checker", *rkey)
        hit = matrix_hit(q.question, tried[-1]) if i == 0 else _u("rv", *rkey) < 0.5
        verdict = "HIT" if hit else MISS_VERDICTS[_h("rk", *rkey) % len(MISS_VERDICTS)]
        r.check(verdict, *rkey)
    status = "HIT" if verdict == "HIT" else "NOT_FOUND"
    note = ("Answer is in result 1. You can stop searching for this." if status == "HIT" else
            f"No engine surfaced this after {calls + fetches} tries. If you cannot answer from other "
            "evidence, answer NOT FOUND.")
    r.search_done(status, calls, fetches, cost0, lat0, note)
    return status, url


def _baseline_call(r: _Run, q, setup: str, query: str, goal: str, i: int) -> str:
    """One search tool call in single or telem_default. Returns the url to cite."""
    key = (q.id, setup, i)
    engines = [FIRST_ENGINE]
    if setup == "telem_default":
        others = [e for e in ENGINES if e != FIRST_ENGINE]
        a = _h("p1", *key) % len(others)
        engines += [others[a], others[(a + 1 + _h("p2", *key) % (len(others) - 1)) % len(others)]]
    cost0, lat0 = r.cost_search, r.latency
    r.search(query, goal, engines, *key)
    r.search_done("UNCHECKED", 1, 0, cost0, lat0, "")
    return f"https://{engines[0]}.example/{_h(query) % 997}/1"


async def run_question(question, setup: str, *, log_dir="out/logs") -> AgentAnswer:
    if setup not in OUTCOME:
        raise ValueError(f"unknown setup {setup!r}")
    q = question
    r = _Run(EventLog(f"{setup}-{q.id}-{uuid.uuid4().hex[:8]}", q.id, setup, log_dir=Path(log_dir)))
    fail_ids = {s for s in os.getenv("FAKE_AGENT_FAIL", "").split(",") if s}
    citations: list[str] = []
    r.log.emit("run_start", question=q.question)
    for i in range(1 + _h("n", q.id, setup) % 3):
        r.llm("agent", q.id, setup, i)
        query = q.question if i == 0 else f"{q.question} (follow-up {i})"
        goal = f"find the answer to: {q.question}"
        r.searches += 1
        if setup == "second_opinion":
            status, url = _second_opinion_call(r, q, query, goal, i)
        else:
            status, url = "UNCHECKED", _baseline_call(r, q, setup, query, goal, i)
        citations.append(url)
        if q.id in fail_ids:
            raise RuntimeError(f"fake agent failure injected for {q.id}")
        await asyncio.sleep(0)
        if status == "HIT":
            break
        if _u("read", q.id, setup, i) < 0.25:  # the agent's own read_page tool (Telem fetch)
            r.llm("agent", q.id, setup, i, "read")
            r.fetch(url, "telem", True, q.id, setup, i, "read")
    r.llm("agent", q.id, setup, "final")
    x = 0.7 * _u("q", q.id) + 0.3 * _u("q", q.id, setup)  # correlated across setups
    p_correct, p_wrong = OUTCOME[setup]
    if x < p_correct:
        final, status = f"{q.gold[0]} (correct)", "ANSWERED"
    elif x < p_correct + p_wrong:
        final, status = "1987, according to one source (fabricated)", "ANSWERED"
    else:
        final, status, citations = "NOT FOUND", "NOT_FOUND", []
    citations = sorted(set(citations))
    r.log.emit("answer", final=final, status=status, citations=citations)
    r.log.emit("run_end", engine_calls=r.engine_calls, fetches=r.fetches, searches=r.searches,
               cost_usd_search=r.cost_search, cost_usd_llm=r.cost_llm,
               cost_usd_total=r.cost_search + r.cost_llm, latency_ms=r.latency)
    return AgentAnswer(final=final, status=status, citations=citations, searches=r.searches)
