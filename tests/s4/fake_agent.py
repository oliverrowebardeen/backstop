"""Fake so.agent.run_question (section 6.10) that writes a full section 6.11 log.

Deterministic: every number comes from a hash of (question id, setup, step), so
re-running gives the same log apart from timestamps and the run_id suffix.

- single:          Tavily only, no check, status UNCHECKED.
- telem_default:   three engines per search (Telem's routing), no check.
- second_opinion:  Tavily first, a checker call per result list, up to two
                   rescues (retry on the next engine, or a Tavily Extract read
                   with Telem fetch as fallback), then HIT or NOT_FOUND.

The first search of a question uses the question text as its query, and the
first checker verdict agrees with the fake hit matrix (tests/s2/fakes.py) most
of the time, so the checker join in evals.score has realistic numbers to chew on.

Failure injection for tests: FAKE_AGENT_FAIL="id1,id2" makes those questions
raise halfway through, leaving a log with no run_end.
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from tests.s2.fakes import GOLD_MARKER, _h  # noqa: F401  (same hash as the fake matrix)

try:
    from so.contracts import AgentAnswer
except ImportError:
    from tests.s4.fake_contracts import AgentAnswer

PRICE = {"tavily": 0.005, "exa": 0.005, "brave": 0.005, "perplexity": 0.005,
         "parallel": 0.001, "ceramic": 0.00005}
ENGINES = ["tavily", "exa", "brave", "perplexity", "parallel", "ceramic"]
RESCUE_ORDER = ["exa", "perplexity", "brave", "parallel", "ceramic"]
FETCH_PRICE = {"tavily": 0.001, "telem": 0.001}
AGENT_MODEL, CHECKER_MODEL = "claude-sonnet-5-5", "claude-haiku-5-5"
# Probability of (correct, wrong); the rest is declined.
OUTCOME = {"single": (0.60, 0.27), "telem_default": (0.68, 0.20), "second_opinion": (0.67, 0.09)}


def _u(*parts) -> float:
    """Deterministic uniform in [0, 1)."""
    return (_h(*parts) % 10_000) / 10_000


def matrix_hit(question_text: str, engine: str) -> bool:
    """Same rule as the fake matrix: the gold marker lands in the top 5."""
    return (_h(question_text, engine) % 8) + 1 <= 5


class _Log:
    def __init__(self, log_dir: Path, run_id: str, question_id: str, setup: str):
        self.path = log_dir / f"{run_id}.jsonl"
        self.run_id, self.question_id, self.setup = run_id, question_id, setup
        self.seq = 0
        self.f = self.path.open("a")

    def emit(self, type_: str, **data) -> None:
        ev = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
              "run_id": self.run_id, "question_id": self.question_id, "setup": self.setup,
              "seq": self.seq, "type": type_, "data": data}
        self.seq += 1
        self.f.write(json.dumps(ev) + "\n")
        self.f.flush()

    def close(self) -> None:
        self.f.close()


class _Totals:
    def __init__(self):
        self.engine_calls = self.fetches = self.searches = 0
        self.cost_search = self.cost_llm = 0.0
        self.latency = 0.0


def _llm(log: _Log, t: _Totals, role: str, *key) -> None:
    if role == "agent":
        tin, tout = 1500 + _h("in", *key) % 2500, 80 + _h("out", *key) % 220
        cost, model = tin * 3e-6 + tout * 15e-6, AGENT_MODEL
        lat = 900 + _h("lat", *key) % 1600
    else:
        tin, tout = 900 + _h("in", *key) % 600, 40 + _h("out", *key) % 60
        cost, model = tin * 1e-6 + tout * 5e-6, CHECKER_MODEL
        lat = 250 + _h("lat", *key) % 400
    cost = round(cost, 6)
    t.cost_llm += cost
    t.latency += lat
    log.emit("llm", role=role, model=model, tokens_in=tin, tokens_out=tout, cost_usd=cost, latency_ms=lat)


def _search(log: _Log, t: _Totals, query: str, goal: str, engines: list[str], *key) -> float:
    cost = round(sum(PRICE.get(e, 0.005) for e in engines), 6)
    lat = 400 + _h("slat", *key) % 1400
    t.engine_calls += len(engines)
    t.cost_search += cost
    t.latency += lat
    log.emit("search", query=query, goal=goal, engines=engines, n_results=5 * len(engines),
             latency_ms=lat, cost_usd=cost, cached=False)
    return cost


def _fetch(log: _Log, t: _Totals, url: str, via: str, ok: bool, *key) -> float:
    cost = FETCH_PRICE[via]
    lat = 300 + _h("flat", *key) % 900
    t.fetches += 1
    t.cost_search += cost
    t.latency += lat
    log.emit("fetch", url=url, ok=ok, via=via, latency_ms=lat, cost_usd=cost)
    return cost


def _first_verdict(question_text: str, *key) -> str:
    """The checker on the first Tavily result list: right about 85 to 90 percent of the time."""
    if matrix_hit(question_text, "tavily"):
        return "HIT" if _u("v", *key) < 0.9 else "SHALLOW"
    if _u("v", *key) < 0.13:
        return "HIT"  # a missed miss
    rescuable = any(matrix_hit(question_text, e) for e in ENGINES if e != "tavily")
    if not rescuable and _u("d", *key) < 0.7:
        return "DEAD"
    return ["WRONG_ENTITY", "STALE", "SHALLOW", "DEAD"][_h("kind", *key) % 4]


def _second_opinion_call(log, t, q, query, goal, i) -> tuple[str, list[str]]:
    """One search tool call in the second_opinion setup. Returns (status, urls)."""
    key = (q.id, "second_opinion", i)
    start_cost, start_lat, calls, fetches = t.cost_search, t.latency, 0, 0
    urls = [f"https://tavily.example/{_h(query) % 997}/1"]
    _search(log, t, query, goal, ["tavily"], *key, 0)
    calls += 1
    tried = ["tavily"]
    _llm(log, t, "checker", *key, 0)
    verdict = _first_verdict(q.question, *key) if i == 0 else (
        "HIT" if _u("v", *key) < 0.6 else ["WRONG_ENTITY", "STALE", "SHALLOW", "DEAD"][_h("k", *key) % 4])
    log.emit("check", verdict=verdict, confidence=round(0.55 + _u("c", *key) * 0.4, 2),
             why=f"fake checker: {verdict.lower()}", evidence_rank=1 if verdict == "HIT" else None)
    rescues = 0
    while verdict != "HIT" and rescues < 2:
        rescues += 1
        rkey = (*key, rescues)
        if verdict == "SHALLOW":
            url = urls[-1]
            log.emit("rescue", action="FETCH_PAGE", engine=None, query=query, reason="answer not in snippet")
            ok = _u("fok", *rkey) < 0.85
            _fetch(log, t, url, "tavily", ok, *rkey)
            fetches += 1
            if not ok:
                _fetch(log, t, url, "telem", True, *rkey, "fallback")
                fetches += 1
        else:
            engine = next(e for e in RESCUE_ORDER if e not in tried)
            tried.append(engine)
            rq = f"{query} (reworded)"
            log.emit("rescue", action="RETRY_ENGINE", engine=engine, query=rq, reason=verdict.lower())
            _search(log, t, rq, goal, [engine], *rkey)
            calls += 1
            urls.append(f"https://{engine}.example/{_h(rq) % 997}/1")
        _llm(log, t, "checker", *rkey)
        hit = matrix_hit(q.question, tried[-1]) if i == 0 else _u("rv", *rkey) < 0.5
        verdict = "HIT" if hit else ["WRONG_ENTITY", "STALE", "DEAD"][_h("rk", *rkey) % 3]
        log.emit("check", verdict=verdict, confidence=round(0.55 + _u("rc", *rkey) * 0.4, 2),
                 why=f"fake checker: {verdict.lower()}", evidence_rank=1 if verdict == "HIT" else None)
    status = "HIT" if verdict == "HIT" else "NOT_FOUND"
    note = ("Answer is in result 1. You can stop searching for this." if status == "HIT" else
            f"No engine surfaced this after {calls + fetches} tries. If you cannot answer from other "
            "evidence, answer NOT FOUND.")
    log.emit("search_done", status=status, calls=calls, fetches=fetches,
             cost_usd=round(t.cost_search - start_cost, 6), latency_ms=round(t.latency - start_lat), note=note)
    return status, urls


async def run_question(question, setup: str, *, log_dir="out/logs") -> AgentAnswer:
    if setup not in OUTCOME:
        raise ValueError(f"unknown setup {setup!r}")
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    q = question
    run_id = f"{setup}-{q.id}-{uuid.uuid4().hex[:8]}"
    log = _Log(log_dir, run_id, q.id, setup)
    t = _Totals()
    citations: list[str] = []
    try:
        log.emit("run_start", question=q.question)
        n_searches = 1 + _h("n", q.id, setup) % 3
        fail_ids = {s for s in os.getenv("FAKE_AGENT_FAIL", "").split(",") if s}
        for i in range(n_searches):
            key = (q.id, setup, i)
            _llm(log, t, "agent", *key)
            query = q.question if i == 0 else f"{q.question} (follow-up {i})"
            goal = f"find the answer to: {q.question}"
            t.searches += 1
            if setup == "second_opinion":
                status, urls = _second_opinion_call(log, t, q, query, goal, i)
                citations.extend(urls[-1:])
            else:
                if setup == "single":
                    engines = ["tavily"]
                else:
                    pool = ["exa", "brave", "perplexity", "parallel"]
                    engines = ["tavily", pool[_h("p1", *key) % 4], pool[(_h("p1", *key) + 1 + _h("p2", *key) % 3) % 4]]
                start_cost, start_lat = t.cost_search, t.latency
                _search(log, t, query, goal, engines, *key)
                log.emit("search_done", status="UNCHECKED", calls=1, fetches=0,
                         cost_usd=round(t.cost_search - start_cost, 6), latency_ms=round(t.latency - start_lat),
                         note="")
                citations.append(f"https://{engines[0]}.example/{_h(query) % 997}/1")
            if q.id in fail_ids:
                raise RuntimeError(f"fake agent failure injected for {q.id}")
            if setup == "second_opinion" and status == "HIT":
                break
            await asyncio.sleep(0.002)
            if _u("read", *key) < 0.25:  # the agent's own read_page tool (Telem fetch)
                _llm(log, t, "agent", *key, "read")
                _fetch(log, t, citations[-1], "telem", True, *key, "read")
        _llm(log, t, "agent", q.id, setup, "final")
        x = 0.7 * _u("q", q.id) + 0.3 * _u("q", q.id, setup)  # correlated across setups
        p_correct, p_wrong = OUTCOME[setup]
        if x < p_correct:
            final, status = f"{q.gold[0]} (correct)", "ANSWERED"
        elif x < p_correct + p_wrong:
            final, status = "1987, according to one source (fabricated)", "ANSWERED"
        else:
            final, status, citations = "NOT FOUND", "NOT_FOUND", []
        citations = sorted(set(citations))
        log.emit("answer", final=final, status=status, citations=citations)
        log.emit("run_end", engine_calls=t.engine_calls, fetches=t.fetches, searches=t.searches,
                 cost_usd_search=round(t.cost_search, 6), cost_usd_llm=round(t.cost_llm, 6),
                 cost_usd_total=round(t.cost_search + t.cost_llm, 6), latency_ms=round(t.latency))
    finally:
        log.close()
    return AgentAnswer(final=final, status=status, citations=citations, searches=t.searches)
