"""Search implementations shared by the agent's three comparison setups."""
from __future__ import annotations

import importlib
from collections.abc import Awaitable, Callable

from so import config, telem_client
from so.contracts import EngineRun, Result, RunContext, SearchOutcome
from so.ledger import ledger

SearchTool = Callable[[str, str, RunContext], Awaitable[SearchOutcome]]


def _unchecked(query: str, goal: str, ctx: RunContext,
               runs: dict[str, EngineRun]) -> SearchOutcome:
    results: dict[str, Result] = {}
    for run in runs.values():
        if ctx.session is None and run.session_id:
            ctx.session = run.session_id
        if run.status == "ok":
            for result in run.results:
                results.setdefault(result.url, result)
    engines = [run.engine for run in runs.values()]
    cost = sum(run.cost_usd for run in runs.values())
    latency = max((run.latency_ms for run in runs.values()), default=0)
    failed = [run.engine for run in runs.values() if run.status == "error"]
    if ctx.log is not None:
        ctx.log.emit("search", query=query, goal=goal, engines=engines,
                     n_results=sum(len(run.results) for run in runs.values()),
                     latency_ms=latency, cost_usd=cost,
                     cached=bool(runs) and all(run.cached for run in runs.values()),
                     session=ctx.session, failed_engines=failed)
    if not runs or len(failed) == len(runs):
        raise RuntimeError(f"No search engine succeeded: {', '.join(failed) or 'no engine runs returned'}")
    note = "Results have not been checked. Answer only from evidence and cite its URLs."
    if failed:
        note += f" These engines failed: {', '.join(failed)}."
    outcome = SearchOutcome(
        status="UNCHECKED", results=list(results.values()), note=note,
        engines_called=engines, calls=len(runs), cost_usd=cost, latency_ms=latency,
    )
    if ctx.log is not None:
        ctx.log.emit("search_done", **outcome.model_dump(exclude={"results", "trace", "engines_called"}))
    return outcome


async def single(query: str, goal: str, ctx: RunContext) -> SearchOutcome:
    """Search Tavily through Telem without checking its results."""
    runs = await telem_client.search_engines(
        query, [config.FIRST_ENGINE], goal=goal, session=ctx.session,
        **ledger("initial", "UNCHECKED", config.FIRST_ENGINE),
    )
    return _unchecked(query, goal, ctx, runs)


async def telem_default(query: str, goal: str, ctx: RunContext) -> SearchOutcome:
    """Use Telem's accuracy routing without checking its results."""
    runs = await telem_client.search_default(query, goal=goal, session=ctx.session,
                                             **ledger("initial", "UNCHECKED", "auto"))
    return _unchecked(query, goal, ctx, runs)


def get_search_tool(setup: str) -> SearchTool:
    """Select the requested setup or fail without substituting another one."""
    if setup == "single":
        return single
    if setup == "telem_default":
        return telem_default
    if setup != "second_opinion":
        raise ValueError(f"Unknown setup: {setup}")
    if not config.SO_ENABLE_SECOND_OPINION:
        raise RuntimeError("The requested second_opinion setup is disabled by SO_ENABLE_SECOND_OPINION")
    try:
        module = importlib.import_module("so.second_opinion")
    except ModuleNotFoundError as exc:
        if exc.name == "so.second_opinion":
            raise RuntimeError("Second Opinion is enabled but so.second_opinion is not installed") from exc
        raise
    return module.search


def get_search_setup(search: SearchTool) -> str:
    """Identify a registered search implementation before labelling a run."""
    if search is single:
        return "single"
    if search is telem_default:
        return "telem_default"
    if search is importlib.import_module("so.second_opinion").search:
        return "second_opinion"
    raise RuntimeError("Unrecognized search implementation; cannot verify the requested setup")
