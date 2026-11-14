"""Search, verify, and apply at most two evidence-driven rescues.

Every call is bounded by `SearchLimits` (rescues, spend, wall clock, checker
time). The loop fails open: a checker that errors or times out yields the
results gathered so far with status UNCHECKED instead of an exception.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import math
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, fields, replace
from time import perf_counter
from typing import Any
from uuid import uuid4

from pydantic import Field

from so import config
from so.contracts import CheckerVerdict, EngineRun, PageText, Result, RunContext, SearchOutcome
from so.ledger import ledger
from so.memory import RunMemory
from so.policy import Action, freshness_cutoff, load_rescue_table, next_action


EngineSearch = Callable[..., Awaitable[dict[str, EngineRun]]]
"""Signature of so.telem_client.search_engines; injectable for alternate Telem clients."""


class SearchError(RuntimeError):
    """Base class for errors raised by the Second Opinion search loop."""


class TelemUnavailable(SearchError):
    """Telem failed entirely: no engine returned a usable response for this call."""


@dataclass(frozen=True)
class SearchLimits:
    """Per call bounds on one Second Opinion search.

    Defaults come from so.config (MAX_RESCUES, SEARCH_MAX_COST_USD,
    SEARCH_TIMEOUT_S, CHECKER_TIMEOUT_S) and are read when `from_config` runs,
    so environment overrides apply.
    """

    max_rescues: int = 2
    max_cost_usd: float = 0.03
    timeout_s: float = 12.0
    checker_timeout_s: float = 4.0

    def __post_init__(self) -> None:
        if self.max_rescues < 0:
            raise ValueError("max_rescues must be 0 or more")
        for name in ("max_cost_usd", "timeout_s", "checker_timeout_s"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a positive number, got {value!r}")

    @classmethod
    def from_config(cls, **overrides: Any) -> SearchLimits:
        """Build limits from so.config defaults, applying any non-None overrides."""
        base = cls(
            max_rescues=config.MAX_RESCUES,
            max_cost_usd=config.SEARCH_MAX_COST_USD,
            timeout_s=config.SEARCH_TIMEOUT_S,
            checker_timeout_s=config.CHECKER_TIMEOUT_S,
        )
        return base.with_overrides(**overrides)

    def with_overrides(self, **overrides: Any) -> SearchLimits:
        """Return a copy with the given limits replaced; None values are ignored."""
        known = {item.name for item in fields(self)}
        unknown = set(overrides) - known
        if unknown:
            raise TypeError(f"Unknown search limit(s): {', '.join(sorted(unknown))}")
        return replace(self, **{key: value for key, value in overrides.items() if value is not None})


class RankedResult(Result):
    """A returned result with the reason for its position in the ranked list."""

    rank_reason: str
    engines: list[str] = Field(default_factory=list)


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

    def checker_costs(self) -> list[float]:
        return [
            float(event["data"].get("cost_usd", 0))
            for event in self.events
            if event["type"] == "llm" and event["data"].get("role") == "checker"
        ]


def _merge(merged: dict[str, Result], results: list[Result]) -> None:
    """Keep stable output ranks while replacing duplicate URLs with newer evidence."""
    for result in results:
        rank = merged[result.url].rank if result.url in merged else len(merged) + 1
        merged[result.url] = result.model_copy(update={"rank": rank})


def _observe(seen: dict[str, dict[str, int]], results: list[Result]) -> None:
    """Record which engines returned each URL and at what original rank."""
    for result in results:
        by_engine = seen.setdefault(result.url, {})
        by_engine[result.engine] = min(by_engine.get(result.engine, result.rank), result.rank)


def rank_results(
    merged: Sequence[Result], seen: dict[str, dict[str, int]], evidence_url: str | None,
) -> list[RankedResult]:
    """Rank merged, URL-deduplicated results.

    Order: the checker's evidence first, then results returned by more than one
    engine (more engines first), then by best original engine rank.
    """
    scored = []
    for index, result in enumerate(merged):
        by_engine = seen.get(result.url) or {result.engine: result.rank}
        count = len(by_engine)
        best = min(by_engine.values())
        is_evidence = evidence_url is not None and result.url == evidence_url
        if is_evidence:
            reason = "evidence"
        elif count > 1:
            reason = f"agreement: {count} engines"
        else:
            reason = f"engine rank {best}"
        scored.append(((not is_evidence, -count, best, index), result, reason, sorted(by_engine)))
    scored.sort(key=lambda item: item[0])
    return [
        RankedResult(**result.model_dump(exclude={"rank"}), rank=position, rank_reason=reason, engines=engines)
        for position, (_, result, reason, engines) in enumerate(scored, 1)
    ]


def ranked_results(outcome: SearchOutcome) -> list[RankedResult]:
    """Return an outcome's results in ranked order with a rank_reason each.

    Uses the ranking recorded in the outcome's search_done trace event. Outcomes
    without one (unchecked setups) keep their order with "engine rank N".
    """
    ranking: list[dict[str, Any]] = []
    for event in reversed(outcome.trace):
        if event.get("type") == "search_done":
            ranking = list(event.get("data", {}).get("ranking") or [])
            break
    by_url = {result.url: result for result in outcome.results}
    ranked: list[RankedResult] = []
    for entry in ranking:
        result = by_url.pop(entry.get("url", ""), None)
        if result is None:
            continue
        ranked.append(RankedResult(
            **result.model_dump(exclude={"rank"}), rank=len(ranked) + 1,
            rank_reason=str(entry.get("rank_reason", f"engine rank {result.rank}")),
            engines=list(entry.get("engines") or [result.engine]),
        ))
    for result in by_url.values():
        ranked.append(RankedResult(
            **result.model_dump(exclude={"rank"}), rank=len(ranked) + 1,
            rank_reason=f"engine rank {result.rank}", engines=[result.engine],
        ))
    return ranked


async def _check(query: str, goal: str, results: list[Result], ctx: RunContext) -> CheckerVerdict:
    from so.checker import check

    return await check(query, goal, results, ctx.today)


async def _check_page(query: str, goal: str, page: PageText, ctx: RunContext) -> CheckerVerdict:
    from so.checker import check_page

    return await check_page(query, goal, page, ctx.today)


async def _guarded_check(call: Awaitable[CheckerVerdict], timeout_s: float) -> tuple[CheckerVerdict | None, str | None]:
    """Run a checker call, converting failures and timeouts into a skip reason.

    BudgetExceeded is an operator stop, not a checker failure, and propagates.
    """
    try:
        return await asyncio.wait_for(call, timeout_s), None
    except config.BudgetExceeded:
        raise
    except TimeoutError:
        return None, f"the checker did not answer within {timeout_s:g}s"
    except Exception as exc:
        return None, f"the checker failed ({type(exc).__name__})"


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


@dataclass(frozen=True)
class _PageEvidence:
    """Text a SHALLOW page read handed the checker, kept for the HIT evidence object."""

    url: str
    candidates: list[str]
    source: str
    chunks: int | None = None


def _page_evidence(page: PageText, trace: _Trace) -> _PageEvidence:
    """Split a successful page read into evidence candidates, naming the reader that produced it.

    Tavily Extract with a goal returns ranked chunks joined by CHUNK_SEPARATOR;
    the Telem fetch fallback returns plain page text, kept as one candidate.
    """
    from so.evidence import split_chunks

    via = next((event["data"].get("via") for event in reversed(trace.events)
                if event["type"] == "fetch" and event["data"].get("ok") and "error" not in event["data"]), "telem")
    if via == "tavily":
        chunks = split_chunks(page.text)
        return _PageEvidence(page.url, chunks, "tavily_extract", len(chunks))
    return _PageEvidence(page.url, [page.text], "telem_fetch")


def _hit_evidence(
    *, evidence: Result | None, evidence_url: str | None, why: str, exclude: str,
    page: _PageEvidence | None, current_results: Sequence[Result], merged: dict[str, Result],
    seen: dict[str, dict[str, int]], per_engine: dict[str, list[Result]],
) -> dict[str, Any]:
    """The verified evidence object for a HIT's search_done data. Makes no calls.

    rank_reason is the evidence URL's position reason from rank_results without
    the evidence boost (agreement across engines or best engine rank).
    """
    from so.evidence import build_evidence

    url = evidence.url if evidence else (evidence_url or "")
    if page is not None and page.url == url:
        candidates, source, chunks = page.candidates, page.source, page.chunks
    elif evidence is not None:
        candidates, source, chunks = [evidence.snippet], "search", None
    else:
        candidates, source, chunks = [item.snippet for item in current_results], "search", None
    rank_reason = next((item.rank_reason for item in rank_results(list(merged.values()), seen, None)
                        if item.url == url), None)
    return build_evidence(url=url, why=why, candidates=candidates, per_engine=per_engine,
                          exclude=exclude, rank_reason=rank_reason, source=source, chunks=chunks)


async def _fetch(url: str, goal: str, ctx: RunContext, trace: _Trace, trigger: str = "SHALLOW") -> PageText:
    """Read a SHALLOW page: Tavily Extract chunks for the goal first, then the Telem fetch.

    Extract runs at basic depth with the goal as its query, so it returns the
    config.TAVILY_EXTRACT_CHUNKS chunks most relevant to the goal under
    config.TAVILY_EXTRACT_TIMEOUT_S. A failed or empty Extract falls back to the
    Telem fetch unchanged. Each attempt is a fetch trace event carrying its cost.
    """
    from so.config import BudgetExceeded
    from so.tavily_client import tavily_extract
    from so.telem_client import fetch_page

    fields = {"metadata": ledger("extract_recheck", trigger, config.TELEM_FETCH_PROVIDER)["metadata"]}

    for via in ("tavily", "telem"):
        started = perf_counter()
        error: str | None = None
        try:
            if via == "tavily":
                page = await tavily_extract(url, goal=goal)
            else:
                page = await fetch_page(url, goal=goal, session=ctx.session,
                                        **_ledger_kwargs(fetch_page, fields))
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


def _next_engine(tried: Sequence[str], table: Any) -> str | None:
    """Next untried engine in the rescue order after the last one tried."""
    previous = tried[-1] if tried else ""
    candidates = [*table.order_after.get(previous, []), *table.fallback_order]
    return next((item for item in candidates if item not in tried), None)


def _ledger_kwargs(call: Callable[..., Any], fields: dict[str, Any]) -> dict[str, Any]:
    """The subset of Telem ledger fields (context, metadata) that `call` accepts.

    Injected engine_search and page reader callables predating the ledger keep working unchanged.
    """
    try:
        params = inspect.signature(call).parameters
    except (TypeError, ValueError):
        return {}
    if any(param.kind is inspect.Parameter.VAR_KEYWORD for param in params.values()):
        return fields
    return {key: value for key, value in fields.items() if key in params}


def _error_text(exc: BaseException) -> str:
    message = str(exc).strip().replace("\n", " ")[:200]
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


async def search(query: str, goal: str, ctx: RunContext, *, limits: SearchLimits | None = None,
                 engine_search: EngineSearch | None = None) -> SearchOutcome:
    """Return verified evidence, or an explicit instruction to avoid guessing.

    `limits` overrides the so.config defaults for this call only. `engine_search`
    replaces so.telem_client.search_engines (same signature), which the drop-in
    client uses to route calls through its own Telem client. Raises
    TelemUnavailable only when Telem returns nothing usable at all.
    """
    trace = _Trace(ctx.log)
    return await _search(query, goal, ctx, trace, limits or SearchLimits.from_config(), engine_search)


async def _search(query: str, goal: str, ctx: RunContext, trace: _Trace, limits: SearchLimits,
                  engine_search: EngineSearch | None = None) -> SearchOutcome:
    if engine_search is None:
        from so.telem_client import search_engines as engine_search
    search_engines = engine_search

    started = perf_counter()
    table = load_rescue_table()
    if ctx.memory is None:
        ctx.memory = RunMemory()
    memory: RunMemory = ctx.memory
    engines: list[str] = []
    merged: dict[str, Result] = {}
    seen: dict[str, dict[str, int]] = {}
    # Each engine's already-fetched top results, for the HIT evidence object's agreement count.
    per_engine: dict[str, list[Result]] = {}
    page_read: _PageEvidence | None = None
    rescues_used = 0
    ok_runs = 0
    current_query = query
    published_after: str | None = None
    engine = config.FIRST_ENGINE
    current_results: list[Result] = []
    step_seconds: list[float] = []
    # Ledger label for the next Telem search: which Backstop step it is and the verdict behind it.
    step_name, trigger = "initial", None

    def finish(status: str, note: str, evidence_url: str | None = None, **extra: Any) -> SearchOutcome:
        outcome = SearchOutcome(
            status=status, results=list(merged.values()), note=note,
            engines_called=engines, trace=trace.events.copy(), calls=len(engines),
            fetches=sum(event["type"] == "fetch" for event in trace.events),
            cost_usd=trace.total("cost_usd"),
            latency_ms=max((perf_counter() - started) * 1000, trace.total("latency_ms")),
        )
        ranking = [
            {"url": item.url, "rank": item.rank, "rank_reason": item.rank_reason, "engines": item.engines}
            for item in rank_results(outcome.results, seen, evidence_url)
        ]
        trace.emit(
            "search_done", **outcome.model_dump(exclude={"results", "trace", "engines_called"}),
            ranking=ranking, telem_session_id=ctx.session, **extra,
        )
        outcome.trace = trace.events.copy()
        return outcome

    def not_found(stop: str | None = None) -> SearchOutcome:
        note = (
            f"No engine surfaced this after {1 + rescues_used} tries. "
            "If you cannot answer from other evidence, answer NOT FOUND."
        )
        if stop:
            note += f" Rescues stopped early by the {stop}."
        return finish("NOT_FOUND", note, **({"stopped_by": stop} if stop else {}))

    def unchecked(reason: str) -> SearchOutcome:
        note = f"Check skipped because {reason}. These results are unverified; do not treat them as confirmed."
        return finish("UNCHECKED", note, check_skipped=reason)

    def limit_stop(estimated_cost: float) -> str | None:
        """Name the limit the next step would break, or None if it fits."""
        spent = trace.total("cost_usd")
        if spent + estimated_cost > limits.max_cost_usd + 1e-12:
            return (f"cost limit (next step would bring spend to ${spent + estimated_cost:.4f}, "
                    f"over max_cost_usd ${limits.max_cost_usd:.4f})")
        elapsed = perf_counter() - started
        expected = sum(step_seconds) / len(step_seconds) if step_seconds else 0.0
        if elapsed + expected > limits.timeout_s:
            return (f"time limit (next step expected to end at {elapsed + expected:.1f}s, "
                    f"over timeout_s {limits.timeout_s:g}s)")
        return None

    def checker_estimate() -> float:
        costs = trace.checker_costs()
        return sum(costs) / len(costs) if costs else 0.0

    while True:
        step_started = perf_counter()
        engines.append(engine)
        memory.record_search(current_query, engine, [])
        error: str | None = None
        try:
            runs = await search_engines(
                current_query, [engine], num_results=config.TELEM_NUM_RESULTS, goal=goal,
                published_after=published_after, session=ctx.session,
                **_ledger_kwargs(search_engines, ledger(step_name, trigger, engine)),
            )
            run = runs.get(engine) or EngineRun(engine=engine, status="error")
        except config.BudgetExceeded:
            raise
        except Exception as exc:
            if ok_runs == 0 and len(engines) == 1:
                raise TelemUnavailable(f"Telem search failed: {_error_text(exc)}") from exc
            error = _error_text(exc)
            run = EngineRun(engine=engine, status="error", latency_ms=(perf_counter() - step_started) * 1000)
        if ctx.session is None and run.session_id:
            ctx.session = run.session_id
        engine_failed = run.status != "ok"
        if engine_failed:
            error = error or "Engine returned an unsuccessful response."
            current_results = []
        else:
            ok_runs += 1
            current_results = run.results[:config.TELEM_NUM_RESULTS]
            _observe(seen, current_results)
            per_engine.setdefault(engine, []).extend(current_results)
        memory.urls_seen.update(result.url for result in current_results)
        _merge(merged, current_results)
        trace.emit(
            "search", query=current_query, goal=goal, engines=[engine],
            n_results=len(current_results), latency_ms=run.latency_ms,
            cost_usd=run.cost_usd, cached=run.cached, published_after=published_after,
            session=ctx.session, status=run.status,
            **({"error": error} if engine_failed else {}),
        )
        checked, skipped = await _guarded_check(
            _check(current_query, goal, current_results, ctx), limits.checker_timeout_s,
        )
        step_seconds.append(perf_counter() - step_started)
        if checked is None:
            return unchecked(skipped or "the checker failed")
        verdict: CheckerVerdict = checked

        while True:
            _record_check(trace, verdict)
            if engine_failed:
                # An engine error is not a miss: move on without spending a rescue.
                engine_failed = False
                successor = _next_engine(engines, table)
                if successor is None:
                    if ok_runs == 0:
                        raise TelemUnavailable(f"Telem search failed on every engine; last error: {error}")
                    return not_found()
                stop = limit_stop(config.PRICE_USD.get(successor, config.DEFAULT_PRICE_USD) + checker_estimate())
                if stop:
                    if ok_runs == 0:
                        raise TelemUnavailable(f"Telem search failed and the {stop} prevented a retry; last error: {error}")
                    return not_found(stop)
                trace.emit(
                    "rescue", action="RETRY_ENGINE", engine=successor, query=current_query,
                    reason=f"Engine {engine} failed: {error}", counted=False, engine_error=error,
                )
                engine = successor
                step_name, trigger = "engine_retry", "ENGINE_ERROR"
                break

            if verdict.verdict != "HIT" and rescues_used >= limits.max_rescues:
                action = Action("NOT_FOUND")
            else:
                # The per call max_rescues gate above replaces the policy's global cap.
                action = next_action(verdict, engines, 0, table)
            if action.kind == "HIT":
                evidence_url = verdict.best_url
                if not evidence_url and verdict.evidence_rank and verdict.evidence_rank <= len(current_results):
                    evidence_url = current_results[verdict.evidence_rank - 1].url
                evidence = merged.get(evidence_url or "")
                location = f"result {evidence.rank}" if evidence else "the returned evidence"
                note = f"Answer is in {location}. You can stop searching for this."
                proof = _hit_evidence(
                    evidence=evidence, evidence_url=evidence_url, why=verdict.why,
                    exclude=f"{query} {current_query} {goal}", page=page_read,
                    current_results=current_results, merged=merged, seen=seen, per_engine=per_engine,
                )
                return finish("HIT", note, evidence_url=evidence.url if evidence else None, evidence=proof)
            if action.kind == "NOT_FOUND":
                return not_found()

            if action.kind == "RETRY_ENGINE":
                assert action.engine is not None
                estimate = config.PRICE_USD.get(action.engine, config.DEFAULT_PRICE_USD) + checker_estimate()
            else:
                estimate = config.TAVILY_EXTRACT_PRICE_USD + checker_estimate()
            stop = limit_stop(estimate)
            if stop:
                return not_found(stop)

            rescues_used += 1
            rewritten = action.kind == "RETRY_ENGINE" and bool(action.query) and action.query != current_query
            trace.emit(
                "rescue", action=action.kind, engine=action.engine,
                query=action.query or current_query, reason=verdict.why,
                counted=True, estimated_cost_usd=round(estimate, 6),
                **({"url": action.url} if action.url else {}),
                **({"original_query": query, "rewritten_query": action.query} if rewritten else {}),
            )
            if action.kind == "RETRY_ENGINE":
                engine = action.engine
                step_name, trigger = f"rescue_{rescues_used}", verdict.verdict
                current_query = action.query or current_query
                if action.fresh:
                    published_after = freshness_cutoff(goal, ctx.today)
                break

            assert action.url is not None
            step_started = perf_counter()
            memory.record_fetch(action.url)
            page = await _fetch(action.url, goal, ctx, trace, verdict.verdict)
            if page.ok and page.text.strip():
                page_read = _page_evidence(page, trace)
                original = merged.get(action.url)
                evidence = Result(
                    rank=1, title=original.title if original else action.url,
                    url=action.url, snippet=page.text[:config.PAGE_TEXT_CHARS],
                    published=original.published if original else None,
                    engine=original.engine if original else engine,
                )
                _merge(merged, [evidence])
                current_results = [evidence]
                checked, skipped = await _guarded_check(
                    _check_page(current_query, goal, page.model_copy(update={"text": evidence.snippet}), ctx),
                    limits.checker_timeout_s,
                )
                step_seconds.append(perf_counter() - step_started)
                if checked is None:
                    return unchecked(skipped or "the checker failed")
                verdict = checked
                if verdict.verdict == "HIT":
                    verdict = verdict.model_copy(update={"best_url": action.url})
            else:
                step_seconds.append(perf_counter() - step_started)
                verdict = CheckerVerdict(
                    verdict="DEAD", confidence=1.0, why="Neither page reader returned usable text.",
                )


def _format_text(outcome: SearchOutcome) -> str:
    """Human-readable ranked results and trace for the CLI."""
    lines = [f"status: {outcome.status}", f"note: {outcome.note}",
             f"engines: {', '.join(outcome.engines_called)}  calls: {outcome.calls}  fetches: {outcome.fetches}  "
             f"cost: ${outcome.cost_usd:.4f}  latency: {outcome.latency_ms:.0f} ms", "", "ranked results:"]
    for item in ranked_results(outcome):
        lines.append(f"  {item.rank}. [{item.rank_reason}] {item.title} ({item.engine})")
        lines.append(f"     {item.url}")
    lines += ["", "trace:"]
    for event in outcome.trace:
        data = event["data"]
        if event["type"] == "search":
            detail = f"{','.join(data.get('engines', []))} q={data.get('query')!r} n={data.get('n_results')}"
        elif event["type"] == "check":
            detail = f"{data.get('verdict')} ({data.get('confidence')}) {data.get('why')}"
        elif event["type"] == "rescue":
            detail = f"{data.get('action')} -> {data.get('engine') or data.get('url')}: {data.get('reason')}"
            if data.get("rewritten_query"):
                detail += f" | rewrite {data['original_query']!r} -> {data['rewritten_query']!r}"
        elif event["type"] == "fetch":
            detail = f"{data.get('via')} {data.get('url')} ok={data.get('ok')}"
        elif event["type"] == "llm":
            detail = f"{data.get('role')} {data.get('model')} ${float(data.get('cost_usd', 0)):.5f}"
        else:
            detail = f"{data.get('status')} {data.get('note')}"
        lines.append(f"  {event['type']:<11} {detail}")
    return "\n".join(lines)


async def _main(query: str, goal: str, limits: SearchLimits, output: str) -> None:
    from so.eventlog import EventLog

    question_id = uuid4().hex
    run_id = f"second_opinion-{question_id}"
    log = EventLog(run_id, question_id, "second_opinion", log_dir=config.SO_LOG_DIR)
    ctx = RunContext(run_id=run_id, question_id=question_id, setup="second_opinion", log=log)
    outcome = await search(query, goal, ctx, limits=limits)
    if output == "text":
        print(_format_text(outcome))
    else:
        payload = outcome.model_dump(mode="json")
        payload["ranked_results"] = [item.model_dump(mode="json") for item in ranked_results(outcome)]
        print(json.dumps(payload, indent=2))


def main() -> None:
    """Command line entry point: run one verified search and print the outcome."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query")
    parser.add_argument("--goal", required=True)
    parser.add_argument("--max-rescues", type=int)
    parser.add_argument("--max-cost-usd", type=float)
    parser.add_argument("--timeout-s", type=float)
    parser.add_argument("--checker-timeout-s", type=float)
    parser.add_argument("--format", choices=("json", "text"), default="json")
    args = parser.parse_args()
    limits = SearchLimits.from_config(
        max_rescues=args.max_rescues, max_cost_usd=args.max_cost_usd,
        timeout_s=args.timeout_s, checker_timeout_s=args.checker_timeout_s,
    )
    asyncio.run(_main(args.query, args.goal, limits, args.format))


if __name__ == "__main__":
    main()
