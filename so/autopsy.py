"""Miss autopsy: replay a recorded Telem session through the Backstop checker.

`autopsy(session_id)` reads the session back from Telem's Sessions API
(`sessions.history(session_id, full=True)` and `sessions.results(session_id)`),
rebuilds the ordered searches (query, goal, providers, results), and runs the
existing checker on each one against the recorded goal (or the first query).
It reports where Backstop would have stopped (the first HIT), the first step
whose results missed, the action the rescue table would take next, and how
many searches the agent ran after that first miss.

Telem's session payloads carry no judge or quality field today (probed live on
Nov 14: interactions have empty postprocessor_runs, and the only per-result
"score" is the engine's own raw relevance inside raw_payload). The probe still
runs; when nothing is found `telem_judge` is null and `telem_judge_note` says
so. Nothing is inferred or fabricated.

Bounds: at most MAX_STEPS searches are checked and the whole call stops after
TIMEOUT_S seconds, returning the steps checked so far with `timed_out` set.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from collections.abc import Awaitable, Callable, Sequence
from datetime import date
from time import perf_counter
from typing import Any

from pydantic import BaseModel, Field

from so import config
from so.contracts import CheckerVerdict, Result
from so.policy import load_rescue_table, next_action

logger = logging.getLogger(__name__)

MAX_STEPS = 25
"""Most searches one autopsy checks; later ones are counted but not checked."""
TIMEOUT_S = 30.0
"""Wall clock budget for one autopsy, Telem reads and checker calls included."""
CHECK_CONCURRENCY = 8

# Interaction or postprocessor keys that would carry a Telem quality judgment.
# Engine payloads (raw_payload, request_payload) and per-result fields are not
# probed: an engine's own relevance score is not a judgment of the search.
_JUDGE_KEY = re.compile(r"judge|judgment|judgement|quality|relevance|grade", re.IGNORECASE)
_SKIP_KEYS = frozenset({"raw_payload", "request_payload", "results", "user_input"})
_JUDGE_ABSENT = ("Telem's session history and results carry no judge, quality or relevance field for this "
                 "session, so checker agreement cannot be computed.")

Checker = Callable[[str, str, Sequence[Result], date], Awaitable[CheckerVerdict]]


class AutopsyError(RuntimeError):
    """Base class for autopsy failures."""


class SessionNotFound(AutopsyError):
    """Telem has no session with this id for the caller's key, or it holds no searches."""


class AutopsyUnavailable(AutopsyError):
    """Telem's Sessions API failed or did not answer within the time budget."""


class AutopsyStep(BaseModel):
    """One recorded search, replayed through the checker."""

    index: int = Field(ge=0, description="0-based position in the session, oldest first")
    interaction_id: str | None = None
    created_at: str | None = None
    query: str
    goal: str | None = Field(default=None, description="Goal recorded on this search, if any")
    context: str | None = None
    providers: list[str] = Field(default_factory=list)
    results: list[Result] = Field(default_factory=list)
    backstop: dict[str, Any] | None = Field(default=None, description="Backstop ledger metadata, if recorded")
    checker_verdict: str | None = Field(default=None, description="HIT, STALE, WRONG_ENTITY, SHALLOW, DEAD")
    checker_confidence: float | None = None
    checker_why: str = ""
    checker_error: str | None = None
    telem_judge: Any = None


class NextRescue(BaseModel):
    """What the rescue table would do after the first failing step."""

    action: str
    engine: str | None = None
    url: str | None = None
    query: str | None = None


class Autopsy(BaseModel):
    """Where a recorded Telem session went wrong and where Backstop would have stopped."""

    session_id: str
    goal: str = Field(description="Goal the checker used: the session's recorded goal, else the first query")
    goal_source: str = Field(description="recorded or first_query")
    n_searches: int = Field(ge=0, description="Searches recorded in the session")
    steps: list[AutopsyStep] = Field(default_factory=list)
    truncated: bool = Field(default=False, description=f"True when the session had more than {MAX_STEPS} searches")
    timed_out: bool = False
    stop_index: int | None = Field(default=None, description="First HIT: where Backstop would have stopped")
    searches_after_stop: int = 0
    first_failure_index: int | None = None
    steps_after_first_failure: int = 0
    next_rescue: NextRescue | None = None
    telem_judge: dict[str, Any] | None = None
    telem_judge_note: str = ""
    elapsed_ms: float = 0


def _text(value: Any) -> str:
    if isinstance(value, list):
        return " ".join(str(item) for item in value if item)
    return str(value or "")


def _results(rows: Any, engine: str) -> list[Result]:
    """Telem result rows (normalized or session-results shape) as contract Results."""
    out: list[Result] = []
    for index, row in enumerate(rows if isinstance(rows, list) else [], 1):
        if not isinstance(row, dict) or not row.get("url"):
            continue
        snippet = _text(row.get("summary") or row.get("excerpt") or row.get("content"))
        rank = row.get("rank") if isinstance(row.get("rank"), int) and row.get("rank") >= 1 else index
        out.append(Result(rank=rank, title=str(row.get("title") or ""), url=str(row["url"]),
                          snippet=snippet[:config.RESULT_SNIPPET_CHARS], published=row.get("publish_date"),
                          engine=str(row.get("provider") or engine)))
    return out[:config.TELEM_NUM_RESULTS]


def _probe_judge(node: Any) -> Any:
    """First judge-like scalar outside engine payloads and per-result rows, else None."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in _SKIP_KEYS:
                continue
            if _JUDGE_KEY.search(str(key)) and isinstance(value, (int, float, str, bool)) and value != "":
                return value
            found = _probe_judge(value) if isinstance(value, (dict, list)) else None
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _probe_judge(item)
            if found is not None:
                return found
    return None


def parse_history(history: dict[str, Any]) -> list[AutopsyStep]:
    """Ordered searches (oldest first) from a full `sessions.history` payload.

    Each interaction becomes one step per batch query. Interactions are ordered
    by created_at; Telem lists them newest first.
    """
    interactions = [item for item in history.get("interactions") or [] if isinstance(item, dict)]
    ordered = sorted(enumerate(interactions), key=lambda pair: (str(pair[1].get("created_at") or ""), -pair[0]))
    steps: list[AutopsyStep] = []
    for _, item in ordered:
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        user_input = item.get("user_input") if isinstance(item.get("user_input"), dict) else {}
        runs = [run for run in item.get("preprocessor_runs") or [] if isinstance(run, dict)]
        batches: dict[int, list[dict[str, Any]]] = {}
        for run in runs:
            batches.setdefault(int(run.get("batch_index") or 0), []).append(run)
        if not batches:
            batches[0] = []
        backstop = {key: metadata[key] for key in ("rescue_step", "verdict", "engine", "backstop_version")
                    if key in metadata} or None
        judge = _probe_judge({key: value for key, value in item.items() if key != "preprocessor_runs"})
        for batch in sorted(batches):
            batch_runs = batches[batch]
            query = next((str(run["query"]) for run in batch_runs if run.get("query")), None)
            query = query or _text(user_input.get("query"))
            if not query:
                continue
            results: list[Result] = []
            for run in batch_runs:
                payload = run.get("output_payload") if isinstance(run.get("output_payload"), dict) else {}
                results.extend(_results(payload.get("results"), str(run.get("preprocessor_name") or "")))
            providers = [str(run.get("preprocessor_name")) for run in batch_runs if run.get("preprocessor_name")]
            steps.append(AutopsyStep(
                index=len(steps), interaction_id=item.get("interaction_id") or item.get("id"),
                created_at=item.get("created_at"), query=query,
                goal=str(metadata.get("goal") or item.get("trajectory_goal") or "") or None,
                context=str(metadata.get("context") or "") or None,
                providers=providers or [str(p) for p in item.get("pinned_providers") or []],
                results=results, backstop=backstop, telem_judge=judge,
            ))
    return steps


def parse_results(payload: dict[str, Any]) -> list[AutopsyStep]:
    """Ordered searches from a `sessions.results` payload; used when history has none."""
    steps: list[AutopsyStep] = []
    rows = [row for row in payload.get("previous_preprocessor_results") or [] if isinstance(row, dict)]
    for row in sorted(rows, key=lambda item: int(item.get("step") or 0)):
        runs = [run for run in row.get("preprocessor_runs") or [] if isinstance(run, dict)]
        query = next((str(run["query"]) for run in runs if run.get("query")), "")
        if not query:
            continue
        results = [result for run in runs for result in _results(run.get("results"), str(run.get("preprocessor_name") or ""))]
        steps.append(AutopsyStep(
            index=len(steps), query=query, results=results[:config.TELEM_NUM_RESULTS],
            providers=[str(run["preprocessor_name"]) for run in runs if run.get("preprocessor_name")],
        ))
    return steps


def _binary(value: Any) -> bool | None:
    """A judge value as found/not found, or None when it has no clear binary reading."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and math.isfinite(value):
        return value >= (0.5 if 0 <= value <= 1 else 3)
    if isinstance(value, str):
        label = value.strip().lower()
        if label in {"hit", "good", "relevant", "pass", "yes", "true", "high"}:
            return True
        if label in {"miss", "bad", "irrelevant", "fail", "no", "false", "low"}:
            return False
    return None


def cohens_kappa(pairs: Sequence[tuple[bool, bool]]) -> float | None:
    """Cohen's kappa for two binary raters; None with no pairs or no variation to compare."""
    n = len(pairs)
    if n == 0:
        return None
    observed = sum(a == b for a, b in pairs) / n
    p_a = sum(a for a, _ in pairs) / n
    p_b = sum(b for _, b in pairs) / n
    expected = p_a * p_b + (1 - p_a) * (1 - p_b)
    if expected >= 1:
        return None
    return round((observed - expected) / (1 - expected), 4)


def _judge_summary(steps: Sequence[AutopsyStep]) -> tuple[dict[str, Any] | None, str]:
    judged = [step for step in steps if step.telem_judge is not None]
    if not judged:
        return None, _JUDGE_ABSENT
    pairs = [(step.checker_verdict == "HIT", _binary(step.telem_judge)) for step in judged
             if step.checker_verdict is not None]
    usable = [(a, b) for a, b in pairs if b is not None]
    kappa = cohens_kappa(usable)
    note = (f"Telem judge values found on {len(judged)} of {len(steps)} steps; "
            f"kappa over {len(usable)} steps with a checker verdict and a binary judge reading.")
    if kappa is None:
        note += " Kappa is undefined for these steps (none comparable, or no variation)."
    return {"n_judged": len(judged), "n_compared": len(usable),
            "agree": sum(a == b for a, b in usable), "kappa": kappa}, note


async def _default_checker(query: str, goal: str, results: Sequence[Result], today: date) -> CheckerVerdict:
    from so.checker import check

    return await check(query, goal, results, today)


def _telem_client() -> Any:
    from telem import AsyncTelem

    return AsyncTelem(api_key=config.api_key("telem"), base_url=config.TELEM_BASE_URL,
                      timeout=min(config.TELEM_TIMEOUT_S, TIMEOUT_S))


def _dump(model: Any) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump(mode="json")
    return model if isinstance(model, dict) else {}


async def _read_session(client: Any, session_id: str, timeout_s: float) -> tuple[dict[str, Any], dict[str, Any]]:
    """Both Sessions API payloads; results is optional, history is required."""
    from telem.errors import NotFoundError

    history_call = client.sessions.history(session_id, full=True)
    results_call = client.sessions.results(session_id)
    try:
        history, results = await asyncio.wait_for(
            asyncio.gather(history_call, results_call, return_exceptions=True), timeout_s)
    except TimeoutError as exc:
        raise AutopsyUnavailable(f"Telem's Sessions API did not answer within {timeout_s:.0f}s") from exc
    if isinstance(history, NotFoundError):
        raise SessionNotFound(f"Telem has no session {session_id!r} for this key") from history
    if isinstance(history, BaseException):
        if isinstance(history, config.BudgetExceeded):
            raise history
        raise AutopsyUnavailable(f"Telem session history failed: {type(history).__name__}") from history
    if isinstance(results, BaseException):
        logger.warning("Telem session results unavailable for autopsy (%s); using history only",
                       type(results).__name__)
        results = {}
    return _dump(history), _dump(results)


async def autopsy(session_id: str, *, client: Any | None = None, checker: Checker | None = None,
                  max_steps: int = MAX_STEPS, timeout_s: float = TIMEOUT_S,
                  today: date | None = None) -> Autopsy:
    """Replay a Telem session through the checker and locate its first miss.

    Args:
        session_id: A Telem session id (from a search response or sessions.list).
        client: An AsyncTelem-compatible client; one is built from the environment if omitted.
        checker: Replacement for so.checker.check (same arguments); for tests and offline use.
        max_steps: Most searches to check (capped at MAX_STEPS).
        timeout_s: Wall clock budget for the whole autopsy (capped at TIMEOUT_S).
        today: Date the checker reasons from; defaults to today.

    Raises:
        ValueError: session_id is empty.
        SessionNotFound: Telem has no such session, or it holds no searches.
        AutopsyUnavailable: Telem's Sessions API failed or timed out.
    """
    if not session_id or not session_id.strip():
        raise ValueError("session_id must be a nonempty string")
    max_steps = max(1, min(max_steps, MAX_STEPS))
    timeout_s = min(timeout_s, TIMEOUT_S)
    started = perf_counter()
    checker = checker or _default_checker
    today = today or date.today()

    owned = client is None
    client = client or _telem_client()
    try:
        history, results_payload = await _read_session(client, session_id, timeout_s)
    finally:
        if owned:
            await client.aclose()

    steps = parse_history(history) or parse_results(results_payload)
    if not steps:
        raise SessionNotFound(f"Telem session {session_id!r} holds no searches")
    n_searches = len(steps)
    steps = steps[:max_steps]

    recorded = next((step.goal for step in steps if step.goal), None) or str(results_payload.get("goal") or "") or None
    goal = recorded or steps[0].query
    goal_source = "recorded" if recorded else "first_query"

    semaphore = asyncio.Semaphore(CHECK_CONCURRENCY)
    verdicts: dict[int, CheckerVerdict] = {}

    async def run_check(step: AutopsyStep) -> None:
        async with semaphore:
            try:
                verdict = await checker(step.query, step.goal or goal, step.results, today)
            except config.BudgetExceeded:
                raise
            except Exception as exc:
                step.checker_error = f"checker failed ({type(exc).__name__})"
                return
        verdicts[step.index] = verdict
        step.checker_verdict = verdict.verdict
        step.checker_confidence = verdict.confidence
        step.checker_why = verdict.why

    remaining = max(0.0, timeout_s - (perf_counter() - started))
    tasks = [asyncio.create_task(run_check(step)) for step in steps]
    timed_out = False
    if tasks:
        done, pending = await asyncio.wait(tasks, timeout=remaining)
        for task in pending:
            task.cancel()
        if pending:
            timed_out = True
            await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            if task.exception() is not None:
                raise task.exception()  # type: ignore[misc]
    for step in steps:
        if step.checker_verdict is None and step.checker_error is None:
            step.checker_error = f"not checked within the {timeout_s:g}s budget"

    stop_index = next((step.index for step in steps if step.checker_verdict == "HIT"), None)
    first_failure = next((step.index for step in steps
                          if step.checker_verdict is not None and step.checker_verdict != "HIT"), None)
    rescue = None
    if first_failure is not None:
        failing = steps[first_failure]
        tried = list(dict.fromkeys(failing.providers or [config.FIRST_ENGINE]))
        action = next_action(verdicts[first_failure], tried, 0, load_rescue_table())
        rescue = NextRescue(action=action.kind, engine=action.engine, url=action.url, query=action.query)

    judge, note = _judge_summary(steps)
    return Autopsy(
        session_id=session_id, goal=goal, goal_source=goal_source, n_searches=n_searches, steps=steps,
        truncated=n_searches > len(steps), timed_out=timed_out, stop_index=stop_index,
        searches_after_stop=(n_searches - 1 - stop_index) if stop_index is not None else 0,
        first_failure_index=first_failure,
        steps_after_first_failure=(n_searches - 1 - first_failure) if first_failure is not None else 0,
        next_rescue=rescue, telem_judge=judge, telem_judge_note=note,
        elapsed_ms=round((perf_counter() - started) * 1000, 1),
    )
