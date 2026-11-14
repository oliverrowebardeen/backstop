"""BackstopTelem: a drop-in replacement for telem.AsyncTelem that verifies every search.

Adoption is one line::

    from backstop import BackstopTelem
    client = BackstopTelem()            # was: AsyncTelem()
    resp = await client.search("What was Stripe's valuation in its 2023 round?",
                               goal="find the valuation figure")
    resp.status        # "HIT", "NOT_FOUND" or "UNCHECKED"
    resp.results[0]    # evidence first, with url, title, summary, rank, provider, rank_reason

`search` asks one engine through Telem, has a checker decide whether the results
answer the goal, rescues misses on the engine the learn split says catches them,
and returns NOT_FOUND instead of guessing. Every other AsyncTelem method (fetch,
providers, sessions, ...) is forwarded to the wrapped Telem client unchanged.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from so import config
from so.contracts import EngineRun, RunContext, SearchOutcome
from so.second_opinion import SearchLimits, SearchError, TelemUnavailable, ranked_results
from so.second_opinion import search as second_opinion_search

__all__ = [
    "BackstopEvidence",
    "BackstopProviderRun",
    "BackstopRankEntry",
    "BackstopResult",
    "BackstopSearchResponse",
    "BackstopTelem",
    "SearchError",
    "SearchLimits",
    "TelemUnavailable",
]

logger = logging.getLogger(__name__)

# Telem options that would undo engine pinning or the checker's top-5 contract.
# They are accepted for drop-in compatibility, logged, and not forwarded.
_LOOP_OWNED = frozenset({
    "providers_include", "providers_exclude", "auto_routing", "max_routing_providers",
    "num_results", "published_after",
})


class BackstopResult(BaseModel):
    """One search result, shaped like telem.SearchResult plus ranking detail."""

    url: str
    title: str = ""
    summary: str | None = None
    excerpt: list[str] | None = None
    publish_date: str | None = None
    rank: int
    provider: str
    rank_reason: str | None = None
    engines: list[str] = Field(default_factory=list)

    @property
    def content(self) -> str:
        """Best available text, matching telem.SearchResult.content."""
        return self.summary or " ".join(self.excerpt or [])


class BackstopEvidence(BaseModel):
    """Verified evidence behind a HIT (built by so.evidence; no extra calls).

    Unknown keys from newer versions of the evidence builder are kept as-is.
    """

    model_config = ConfigDict(extra="allow")

    verified: bool = True
    url: str = ""
    snippet: str = ""
    engines_agreeing: int = 0
    agreeing_engines: list[str] = Field(default_factory=list)
    source: str = ""
    rank_reason: str | None = None
    chunks: int | None = None


class BackstopRankEntry(BaseModel):
    """One position in the ranked result list and why it is there."""

    url: str
    rank: int
    rank_reason: str
    engines: list[str] = Field(default_factory=list)


class BackstopProviderRun(BaseModel):
    """One engine call, shaped like telem.ProviderRun."""

    provider: str
    status: str
    results: list[BackstopResult] = Field(default_factory=list)
    error: dict[str, Any] | None = None
    latency_ms: int | None = None
    query: str = ""
    batch_index: int = 0


class BackstopSearchResponse(BaseModel):
    """A telem.SearchResponse-shaped answer with the verification verdict attached.

    `status` is the Second Opinion outcome (HIT, NOT_FOUND or UNCHECKED), not
    Telem's interaction status. `results` are ranked: the checker's evidence
    first, then results several engines agreed on, then by engine rank.
    """

    results: list[BackstopResult] = Field(default_factory=list)
    by_provider: list[BackstopProviderRun] = Field(default_factory=list)
    session_id: str | None = None
    status: Literal["HIT", "NOT_FOUND", "UNCHECKED"]
    verdict: str | None = None
    why: str = ""
    note: str = ""
    engines_called: list[str] = Field(default_factory=list)
    engine_calls: int = 0
    fetches: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    trace: list[dict[str, Any]] = Field(default_factory=list)
    evidence: BackstopEvidence | None = None
    ranking: list[BackstopRankEntry] | None = None

    @property
    def found(self) -> bool:
        """True when the checker confirmed the answer is in the results."""
        return self.status == "HIT"


class BackstopTelem:
    """Drop-in replacement for telem.AsyncTelem whose search verifies its results.

    Args:
        api_key: Telem key; falls back to TELEM_API_KEY like AsyncTelem.
        telem: An existing AsyncTelem-compatible client to wrap instead of building one.
        max_rescues: Default rescue cap per search (so.config.MAX_RESCUES).
        max_cost_usd: Default spend cap per search, checker included (SEARCH_MAX_COST_USD).
        timeout_s: Default wall-clock budget per search (SEARCH_TIMEOUT_S).
        checker_timeout_s: Default checker time limit; slower checks fail open as UNCHECKED.
        log_dir: If set, each search writes a JSONL event log there (replayable in the UI).
        **telem_options: Passed to the AsyncTelem constructor (base_url, timeout, default_tier, ...).
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        telem: Any | None = None,
        max_rescues: int | None = None,
        max_cost_usd: float | None = None,
        timeout_s: float | None = None,
        checker_timeout_s: float | None = None,
        log_dir: str | Path | None = None,
        **telem_options: Any,
    ) -> None:
        self.limits = SearchLimits.from_config(
            max_rescues=max_rescues, max_cost_usd=max_cost_usd,
            timeout_s=timeout_s, checker_timeout_s=checker_timeout_s,
        )
        self.log_dir = Path(log_dir) if log_dir is not None else None
        if telem is None:
            from telem import AsyncTelem

            if api_key is not None:
                telem_options["api_key"] = api_key
            telem_options.setdefault("base_url", config.TELEM_BASE_URL)
            telem_options.setdefault("timeout", config.TELEM_TIMEOUT_S)
            telem = AsyncTelem(**telem_options)
        elif api_key is not None or telem_options:
            raise TypeError("Pass either telem= or AsyncTelem constructor options, not both")
        self._telem = telem

    @property
    def telem(self) -> Any:
        """The wrapped Telem client, for calls that should bypass verification."""
        return self._telem

    async def search(
        self,
        query: str,
        *,
        goal: str | None = None,
        session: str | None = None,
        max_rescues: int | None = None,
        max_cost_usd: float | None = None,
        timeout_s: float | None = None,
        checker_timeout_s: float | None = None,
        **telem_kwargs: Any,
    ) -> BackstopSearchResponse:
        """Run one verified search and return a Telem-shaped response with a verdict.

        Args:
            query: The search text. Batched (list) queries are not supported.
            goal: Why the agent is searching; the checker verifies against it. Defaults to the query.
            session: Telem session id to group calls; the response carries the session used.
            max_rescues, max_cost_usd, timeout_s, checker_timeout_s: Per-call limit overrides.
            **telem_kwargs: Forwarded to every Telem search call (include_domains, tier, topic, ...).
                Options the loop controls (engine pinning, num_results, published_after) are ignored.

        Raises:
            TypeError: query is not a single string.
            TelemUnavailable: Telem returned nothing usable from any engine.
        """
        if not isinstance(query, str):
            raise TypeError("BackstopTelem.search takes one query string; batch queries are not supported")
        if not query.strip():
            raise ValueError("query must be a nonempty string")
        ignored = sorted(set(telem_kwargs) & _LOOP_OWNED)
        if ignored:
            logger.warning("Ignoring Telem options controlled by the verification loop: %s", ", ".join(ignored))
        forwarded = {key: value for key, value in telem_kwargs.items() if key not in _LOOP_OWNED}
        limits = self.limits.with_overrides(
            max_rescues=max_rescues, max_cost_usd=max_cost_usd,
            timeout_s=timeout_s, checker_timeout_s=checker_timeout_s,
        )
        calls: list[BackstopProviderRun] = []

        async def engine_search(
            search_query: str, engines: list[str], *, num_results: int = config.TELEM_NUM_RESULTS,
            goal: str | None = None, published_after: str | None = None, session: str | None = None,
            context: str | None = None, metadata: dict[str, Any] | None = None,
        ) -> dict[str, EngineRun]:
            return await self._engine_search(
                search_query, engines, num_results=num_results, goal=goal,
                published_after=published_after, session=session, forwarded=forwarded, calls=calls,
                context=context, metadata=metadata,
            )

        question_id = uuid4().hex[:12]
        run_id = f"second_opinion-backstop-{question_id}"
        log = None
        if self.log_dir is not None:
            from so.eventlog import EventLog

            log = EventLog(run_id, question_id, "second_opinion", log_dir=self.log_dir)
        ctx = RunContext(run_id=run_id, question_id=question_id, setup="second_opinion", session=session, log=log)
        outcome = await second_opinion_search(
            query, goal or query, ctx, limits=limits, engine_search=engine_search,
        )
        return _response(outcome, calls, ctx.session)

    async def _engine_search(
        self, query: str, engines: list[str], *, num_results: int, goal: str | None,
        published_after: str | None, session: str | None, forwarded: dict[str, Any],
        calls: list[BackstopProviderRun], context: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, EngineRun]:
        """One pinned-engine Telem call, normalized to EngineRun and recorded for by_provider.

        context and metadata are Telem ledger labels; a caller's own forwarded values win,
        and a rejected label is dropped on one retry (so.ledger.call_with_ledger).
        """
        from so.ledger import call_with_ledger
        from so.telem_client import _normalize

        ledger_fields = {key: value for key, value in (("context", context), ("metadata", metadata))
                         if key not in forwarded}

        request: dict[str, Any] = {
            **forwarded, "providers_include": engines, "num_results": num_results, "goal": goal or query,
        }
        if published_after is not None:
            request["published_after"] = published_after
        estimate = sum(config.PRICE_USD.get(engine, config.DEFAULT_PRICE_USD) for engine in engines)
        config.SEARCH_BUDGET.reserve(estimate)
        started = time.perf_counter()
        try:
            response = await call_with_ledger(
                lambda extra: config.retry(lambda: self._telem.search(query, session=session, **request, **extra)),
                ledger_fields,
            )
        except BaseException:
            config.SEARCH_BUDGET.settle(estimate, 0)
            raise
        elapsed = (time.perf_counter() - started) * 1000
        raw = response.model_dump(mode="json")
        runs = _normalize(raw, elapsed, engines)
        config.SEARCH_BUDGET.settle(estimate, sum(run.cost_usd for run in runs.values()))
        by_name = {run.get("provider"): run for run in raw.get("by_provider", [])}
        for engine in engines:
            provider = by_name.get(engine, {})
            normalized = runs[engine]
            calls.append(BackstopProviderRun(
                provider=engine, status=str(provider.get("status") or normalized.status),
                results=[BackstopResult(
                    url=item.url, title=item.title, summary=item.snippet or None, publish_date=item.published,
                    rank=item.rank, provider=engine, rank_reason=f"engine rank {item.rank}", engines=[engine],
                ) for item in normalized.results],
                error=provider.get("error") or (None if normalized.status == "ok" else {"message": "Engine returned no response."}),
                latency_ms=int(provider["latency_ms"]) if provider.get("latency_ms") is not None else int(elapsed),
                query=query,
            ))
        return runs

    async def aclose(self) -> None:
        """Close the wrapped Telem client."""
        close = getattr(self._telem, "aclose", None)
        if close is not None:
            await close()

    async def __aenter__(self) -> BackstopTelem:
        """Enter an async context manager, returning this client."""
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Exit the async context manager, closing the wrapped client."""
        await self.aclose()

    def __getattr__(self, name: str) -> Any:
        """Forward every other AsyncTelem attribute (fetch, providers, sessions, ...)."""
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._telem, name)


def _response(outcome: SearchOutcome, calls: Sequence[BackstopProviderRun], session: str | None) -> BackstopSearchResponse:
    """Convert a SearchOutcome into the Telem-shaped response."""
    ranked = ranked_results(outcome)
    results = [BackstopResult(
        url=item.url, title=item.title, summary=item.snippet or None, publish_date=item.published,
        rank=item.rank, provider=item.engine, rank_reason=item.rank_reason, engines=item.engines,
    ) for item in ranked]
    checks = [event["data"] for event in outcome.trace if event["type"] == "check"]
    done = next((event["data"] for event in reversed(outcome.trace) if event["type"] == "search_done"), {})
    verdict = checks[-1].get("verdict") if checks and outcome.status != "UNCHECKED" else None
    why = str(done.get("check_skipped") or (checks[-1].get("why", "") if checks else ""))
    note = outcome.note
    if outcome.status == "HIT":
        position = next((item.rank for item in results if item.rank_reason == "evidence"), None)
        if position is not None:
            note = f"Answer is in result {position}. You can stop searching for this."
    evidence = done.get("evidence") if outcome.status == "HIT" else None
    ranking = [BackstopRankEntry(url=item.url, rank=item.rank, rank_reason=item.rank_reason or "",
                                 engines=item.engines) for item in results] if results else None
    return BackstopSearchResponse(
        results=results, by_provider=list(calls), session_id=session, status=outcome.status,
        verdict=verdict, why=why, note=note, engines_called=list(outcome.engines_called),
        engine_calls=outcome.calls, fetches=outcome.fetches, cost_usd=outcome.cost_usd,
        latency_ms=outcome.latency_ms, trace=outcome.trace,
        evidence=BackstopEvidence.model_validate(evidence) if isinstance(evidence, dict) else None,
        ranking=ranking,
    )
