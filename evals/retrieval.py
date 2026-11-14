"""Offline truth for logged searches: judge the exact result list each search returned (lane S4).

Event logs record each search's query, goal, engines and freshness cutoff, but not
its results. The Telem wrapper caches every response under a key built from those
same fields, so the exact list can be recovered without a new search. This module:

  1. rebuilds the cache key for a logged search and loads its normalized results,
  2. applies the same benchmark-contamination filter the eval installed
     (so.telem_client.RESULT_FILTER) and keeps the top TELEM_NUM_RESULTS, which is
     the list the agent and the checker saw,
  3. judges that list against the gold answer with evals.judge.judge_hit.

Gold answers are used only here, offline, after the runs are finished. Uses:
  * checker metrics: every check is compared with the truth of the list it checked
    (not with a different, question-text search from the matrix),
  * retrieval coverage of each setup's agent runs: a question counts as covered
    when any search in its run surfaced the answer. This is the same metric as the
    matrix frontier's "answer found in results", on the same held-out questions.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evals import contamination
from evals.judge import JudgeError, judge_hit
from so import config, eventlog
from so.contracts import EngineRun, Result


def search_cache_key(query: str, engines: list[str] | None, goal: str | None,
                     published_after: str | None = None,
                     num_results: int | None = None) -> str:
    """The key so.telem_client uses for this search (kept in step by tests/s4/test_retrieval.py)."""
    request: dict[str, Any] = dict(query=query, num_results=num_results or config.TELEM_NUM_RESULTS,
                                   goal=goal or query, tier=config.TELEM_TIER)
    if engines is not None:
        request["providers_include"] = list(dict.fromkeys(engines))
    else:
        request["auto_routing"] = "accuracy"
    if published_after is not None:
        request["published_after"] = published_after
    return config.cache_key("telem-search-v1", {"endpoint": config.TELEM_BASE_URL, **request})


def cached_results(question: str, query: str, engines: list[str] | None, goal: str | None,
                   published_after: str | None = None) -> list[Result] | None:
    """The filtered, truncated list the agent saw for one logged search; None if not cached.

    With several engines (Telem default routing), each engine's top results are kept in
    engine order, as the setup presented them.
    """
    cached = config.read_cache(search_cache_key(query, engines, goal, published_after))
    if not cached or "normalized" not in cached:
        return None
    out: list[Result] = []
    for value in cached["normalized"].values():
        run = EngineRun.model_validate(value)
        if run.status != "ok":
            continue
        kept, _ = contamination.filter_results(question, list(run.results))
        out.extend(kept[: config.TELEM_NUM_RESULTS])
    return out


@dataclass
class JudgedSearch:
    question_id: str
    setup: str
    seq: int
    engines: list[str] | None
    query: str
    hit: bool | None            # None: results not in cache or judge failed
    verdict: str | None = None  # the checker verdict on exactly this list, if one followed
    call_index: int = 0         # which search tool call within the run (for rescue analysis)
    confidence: float | None = None  # the checker's confidence for that verdict


@dataclass
class RunTruth:
    searches: list[JudgedSearch] = field(default_factory=list)
    missing: int = 0
    judge_errors: int = 0


def _searches_with_checks(events: list[dict]) -> list[tuple[dict, dict | None, int]]:
    """(search event, data of a check made directly on its results, search-call index).

    A check counts only when no other non-llm event sits between it and the search,
    so page re-checks (after a fetch) are not paired with a result list.
    """
    out: list[tuple[dict, dict | None, int]] = []
    call_index = 0
    for i, ev in enumerate(events):
        if ev["type"] == "search_done":
            call_index += 1
        if ev["type"] != "search" or ev["data"].get("status", "ok") != "ok":
            continue
        check = None
        for nxt in events[i + 1:]:
            if nxt["type"] == "llm":
                continue
            if nxt["type"] == "check":
                check = nxt["data"]
            break
        out.append((ev, check, call_index))
    return out


async def judge_runs(runs: dict[tuple[str, str], Path], questions: dict, *,
                     concurrency: int | None = None) -> RunTruth:
    """Judge every successful logged search of the given (question_id, setup) runs."""
    truth = RunTruth()
    sem = asyncio.Semaphore(concurrency or config.LLM_MAX_CONCURRENCY)
    jobs = []
    for (qid, setup), path in runs.items():
        q = questions[qid]
        for ev, check, call_index in _searches_with_checks(eventlog.read_log(path)):
            d = ev["data"]
            engines = d.get("engines")
            pinned = engines if setup != "telem_default" else None  # default routing is not pinned
            results = cached_results(q.question, d["query"], pinned, d.get("goal"), d.get("published_after"))
            row = JudgedSearch(qid, setup, ev["seq"], engines, d["query"], None,
                               (check or {}).get("verdict"), call_index,
                               float(check["confidence"]) if check and check.get("confidence") is not None else None)
            truth.searches.append(row)
            if results is None:
                truth.missing += 1
                continue
            jobs.append((row, q, results))

    async def one(row: JudgedSearch, q, results: list[Result]) -> None:
        async with sem:
            try:
                row.hit, _, _ = await judge_hit(q, results)
            except JudgeError:
                truth.judge_errors += 1

    await asyncio.gather(*(one(*j) for j in jobs))
    return truth


def checker_metrics(truth: RunTruth) -> dict:
    """Miss recall, false alarms and DEAD-vs-rescuable on exactly the lists the checker saw."""
    checked = [s for s in truth.searches if s.verdict is not None and s.hit is not None]
    misses = [s for s in checked if not s.hit]
    hits = [s for s in checked if s.hit]
    caught = sum(s.verdict != "HIT" for s in misses)
    alarms = sum(s.verdict != "HIT" for s in hits)
    # A miss is rescuable when a later search in the same search call surfaced the answer.
    later_hit = {}
    for s in truth.searches:
        key = (s.question_id, s.setup, s.call_index)
        later_hit.setdefault(key, []).append(s)
    correct_dead = 0
    for s in misses:
        rest = [o for o in later_hit[(s.question_id, s.setup, s.call_index)] if o.seq > s.seq and o.hit]
        rescuable = bool(rest)
        correct_dead += (s.verdict == "DEAD") == (not rescuable)
    return {
        "n_searches": len(checked), "n_true_misses": len(misses), "n_true_hits": len(hits),
        "miss_recall": round(caught / len(misses), 4) if misses else 0.0,
        "false_alarm_rate": round(alarms / len(hits), 4) if hits else 0.0,
        "rescuable_vs_dead_acc": round(correct_dead / len(misses), 4) if misses else 0.0,
        "searches_not_in_cache": truth.missing, "judge_errors": truth.judge_errors,
        "method": ("every check compared with an offline judgment of the exact result list it saw "
                   "(recovered from the Telem response cache, contamination-filtered as in the eval)"),
    }


def coverage_by_setup(truth: RunTruth, scored: dict[str, set[str]]) -> dict:
    """Share of scored test questions whose agent run surfaced the answer in any search."""
    out = {}
    for setup, qids in scored.items():
        mine = [s for s in truth.searches if s.setup == setup and s.question_id in qids]
        covered = {s.question_id for s in mine if s.hit}
        judged = {s.question_id for s in mine if s.hit is not None}
        out[setup] = {"n_questions": len(qids), "covered": len(covered),
                      # None, not 0.0, when nothing could be judged: an absent measurement is not a zero.
                      "coverage": round(len(covered) / len(qids), 4) if qids and judged else None,
                      "questions_with_a_judged_search": len(judged),
                      "searches_judged": sum(s.hit is not None for s in mine)}
    return out
