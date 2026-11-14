"""Search, verify, and apply at most two evidence-driven rescues."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any
from uuid import uuid4

from so import config
from so.contracts import CheckerVerdict, PageText, Result, RunContext, SearchOutcome
from so.memory import RunMemory
from so.policy import freshness_cutoff, load_rescue_table, next_action


@dataclass
class _Trace:
    """Forward events and retain this search's costs and original timings."""

    log: Any
    events: list[dict[str, Any]] = field(default_factory=list)

    def emit(self, event_type: str, **data: Any) -> Any:
        self.events.append({"type": event_type, "data": data})
        if self.log is not None:
            return self.log.emit(event_type, **data)
        return None

    def total(self, field_name: str) -> float:
        return sum(
            float(event["data"].get(field_name, 0))
            for event in self.events
            if event["type"] in {"search", "fetch", "llm"}
        )


def _merge(merged: dict[str, Result], results: list[Result]) -> None:
    """Keep stable output ranks while replacing duplicate URLs with newer evidence."""
    for result in results:
        rank = merged[result.url].rank if result.url in merged else len(merged) + 1
        merged[result.url] = result.model_copy(update={"rank": rank})


async def _check(query: str, goal: str, results: list[Result], ctx: RunContext) -> CheckerVerdict:
    from so.checker import check

    return await check(query, goal, results, ctx.today)


async def _check_page(query: str, goal: str, page: PageText, ctx: RunContext) -> CheckerVerdict:
    from so.checker import check_page

    return await check_page(query, goal, page, ctx.today)


def _record_check(trace: _Trace, verdict: CheckerVerdict) -> None:
    """Checker usage is attached separately from serialized verdict fields."""
    model = getattr(verdict, "model", "")
    if model:
        trace.emit(
            "llm", role="checker", model=model,
            **{key: getattr(verdict, key, 0) for key in ("tokens_in", "tokens_out", "cost_usd", "latency_ms")},
            cached=getattr(verdict, "cached", False),
        )
    trace.emit("check", **verdict.model_dump())


async def _fetch(url: str, goal: str, ctx: RunContext, trace: _Trace) -> PageText:
    from so.config import BudgetExceeded
    from so.tavily_client import tavily_extract
    from so.telem_client import fetch_page

    for via in ("tavily", "telem"):
        started = perf_counter()
        error: str | None = None
        try:
            if via == "tavily":
                page = await tavily_extract(url)
            else:
                page = await fetch_page(url, goal=goal, session=ctx.session)
        except BudgetExceeded:
            raise
        except Exception as exc:
            error = f"Page reader failed: {type(exc).__name__}"
            page = PageText(url=url, ok=False, latency_ms=(perf_counter() - started) * 1000)
        if error is None and (not page.ok or not page.text.strip()):
            error = "Page reader returned no usable text."
        trace.emit(
            "fetch", url=url, ok=page.ok, via=via, latency_ms=page.latency_ms,
            cost_usd=page.cost_usd, cached=page.cached, goal=goal, session=ctx.session,
            **({"error": error} if error else {}),
        )
        if ctx.session is None and page.session_id:
            ctx.session = page.session_id
        if page.ok and page.text.strip():
            return page
    return page


async def search(query: str, goal: str, ctx: RunContext) -> SearchOutcome:
    """Return verified evidence, or an explicit instruction to avoid guessing."""
    trace = _Trace(ctx.log)
    return await _search(query, goal, ctx, trace)


async def _search(query: str, goal: str, ctx: RunContext, trace: _Trace) -> SearchOutcome:
    from so.telem_client import search_engines

    started = perf_counter()
    table = load_rescue_table()
    if ctx.memory is None:
        ctx.memory = RunMemory()
    memory: RunMemory = ctx.memory
    engines: list[str] = []
    merged: dict[str, Result] = {}
    rescues_used = 0
    current_query = query
    published_after: str | None = None
    engine = config.FIRST_ENGINE
    current_results: list[Result] = []

    while True:
        engines.append(engine)
        memory.record_search(current_query, engine, [])
        runs = await search_engines(
            current_query, [engine], num_results=config.TELEM_NUM_RESULTS, goal=goal,
            published_after=published_after, session=ctx.session,
        )
        run = runs[engine]
        if ctx.session is None and run.session_id:
            ctx.session = run.session_id
        current_results = run.results[:config.TELEM_NUM_RESULTS] if run.status == "ok" else []
        memory.urls_seen.update(result.url for result in current_results)
        _merge(merged, current_results)
        trace.emit(
            "search", query=current_query, goal=goal, engines=[engine],
            n_results=len(current_results), latency_ms=run.latency_ms,
            cost_usd=run.cost_usd, cached=run.cached, published_after=published_after,
            session=ctx.session, status=run.status,
            **({"error": "Engine returned an unsuccessful response."} if run.status != "ok" else {}),
        )
        verdict = await _check(current_query, goal, current_results, ctx)

        while True:
            _record_check(trace, verdict)
            action = next_action(verdict, engines, rescues_used, table)
            if action.kind in {"HIT", "NOT_FOUND"}:
                status = action.kind
                if status == "HIT":
                    evidence_url = verdict.best_url
                    if not evidence_url and verdict.evidence_rank and verdict.evidence_rank <= len(current_results):
                        evidence_url = current_results[verdict.evidence_rank - 1].url
                    evidence = merged.get(evidence_url or "")
                    location = f"result {evidence.rank}" if evidence else "the returned evidence"
                    note = f"Answer is in {location}. You can stop searching for this."
                else:
                    note = (
                        f"No engine surfaced this after {1 + rescues_used} tries. "
                        "If you cannot answer from other evidence, answer NOT FOUND."
                    )
                outcome = SearchOutcome(
                    status=status, results=list(merged.values()), note=note,
                    engines_called=engines, trace=trace.events.copy(), calls=len(engines),
                    fetches=sum(event["type"] == "fetch" for event in trace.events),
                    cost_usd=trace.total("cost_usd"),
                    latency_ms=max((perf_counter() - started) * 1000, trace.total("latency_ms")),
                )
                trace.emit("search_done", **outcome.model_dump(exclude={"results", "trace", "engines_called"}))
                outcome.trace = trace.events.copy()
                return outcome

            rescues_used += 1
            trace.emit(
                "rescue", action=action.kind, engine=action.engine,
                query=action.query or current_query, reason=verdict.why,
                **({"url": action.url} if action.url else {}),
            )
            if action.kind == "RETRY_ENGINE":
                assert action.engine is not None
                engine = action.engine
                current_query = action.query or current_query
                if action.fresh:
                    published_after = freshness_cutoff(goal, ctx.today)
                break

            assert action.url is not None
            memory.record_fetch(action.url)
            page = await _fetch(action.url, goal, ctx, trace)
            if page.ok and page.text.strip():
                original = merged.get(action.url)
                evidence = Result(
                    rank=1, title=original.title if original else action.url,
                    url=action.url, snippet=page.text[:config.PAGE_TEXT_CHARS],
                    published=original.published if original else None,
                    engine=original.engine if original else engine,
                )
                _merge(merged, [evidence])
                current_results = [evidence]
                verdict = await _check_page(
                    current_query, goal, page.model_copy(update={"text": evidence.snippet}), ctx,
                )
                if verdict.verdict == "HIT":
                    verdict = verdict.model_copy(update={"best_url": action.url})
            else:
                verdict = CheckerVerdict(
                    verdict="DEAD", confidence=1.0, why="Neither page reader returned usable text.",
                )


async def _main(query: str, goal: str) -> None:
    from so.eventlog import EventLog

    question_id = uuid4().hex
    run_id = f"second_opinion-{question_id}"
    log = EventLog(run_id, question_id, "second_opinion", log_dir=config.SO_LOG_DIR)
    ctx = RunContext(run_id=run_id, question_id=question_id, setup="second_opinion", log=log)
    outcome = await search(query, goal, ctx)
    print(outcome.model_dump_json(indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query")
    parser.add_argument("--goal", required=True)
    args = parser.parse_args()
    asyncio.run(_main(args.query, args.goal))


if __name__ == "__main__":
    main()
