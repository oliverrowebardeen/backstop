"""Search result checker: decides whether a result list already answers the goal.

`check` labels the top 5 results of one search with a `CheckerVerdict`
(HIT, STALE, WRONG_ENTITY, SHALLOW or DEAD; see so/prompts/checker.md for the
definitions). `check_page` re-checks a page read on the SHALLOW path. Each
makes one call on CHECKER_MODEL and never sees gold answers.

The model's JSON is validated, then hardened so a wrong HIT is unlikely:
a HIT must point at a real result and clear CHECKER_HIT_MIN_CONFIDENCE,
otherwise it becomes SHALLOW on that result so the page gets read instead of
trusted.

Errors are explicit. A malformed or truncated reply is retried once; a second
bad reply is logged and returns a DEAD verdict whose `why` is MALFORMED_WHY or
TRUNCATED_WHY, so a bad reply can cost one extra search but never produce a HIT.
A reply cut off at the output cap is never parsed, even if it looks complete. A model call that
fails after the llm wrapper's own retries raises `CheckerError`, and budget
errors propagate unchanged.

Settings live in the Checker section of so.config, backed by the environment:
    CHECKER_HIT_MIN_CONFIDENCE  minimum confidence to accept a HIT, default 0.6
    CHECKER_THINKING            model thinking mode, default disabled
    CHECKER_MAX_TOKENS          output cap per call, default 399

Both functions return a `CheckedVerdict`: a `CheckerVerdict` that also carries
the checker's own cost, tokens and latency. Those extra fields are excluded
from `model_dump()`, so the serialized verdict matches the contract exactly.
Pass `ctx` to have the checker log its own `llm` event (role `checker`).
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Sequence
from datetime import date
from pathlib import Path
from typing import Any

from pydantic import Field

from so import config
from so.config import CHECKER_MODEL, PAGE_TEXT_CHARS, RESULT_SNIPPET_CHARS, TELEM_NUM_RESULTS, BudgetExceeded
from so.contracts import CheckerVerdict, LLMResponse, PageText, Result, RunContext

logger = logging.getLogger(__name__)

PROMPTS = Path(__file__).resolve().parent / "prompts"
# The checker sees what one search returns, with the same text limits as results and pages.
TOP_K = TELEM_NUM_RESULTS
SNIPPET_CHARS = RESULT_SNIPPET_CHARS
PAGE_CHARS = PAGE_TEXT_CHARS
# Bounds on free text copied from the model into verdicts and event logs.
WHY_CHARS = 300
REWRITE_CHARS = 300

MALFORMED_WHY = "checker output malformed"
TRUNCATED_WHY = "checker output truncated"
# Stop reasons meaning the reply hit the output cap (Anthropic, OpenAI).
TRUNCATED_STOPS = frozenset({"max_tokens", "length"})
# Appended on the retry. It also changes the request, so the llm cache cannot
# serve the same bad reply again.
RETRY_NOTE = ("\n\nYour previous reply was cut off or was not a valid JSON object. Reply with the JSON "
              "object only, with why under 20 words.")

VERDICTS = ("HIT", "STALE", "WRONG_ENTITY", "SHALLOW", "DEAD")

# What the model returns. It names results by number, never by URL: a number is a
# few tokens where a URL can be sixty, and it cannot point outside the results.
# The code maps it onto the contract's evidence_rank and best_url.
VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": list(VERDICTS)},
        "confidence": {"type": "number"},
        "why": {"type": "string"},
        "rank": {"type": ["integer", "null"]},
        "rewrite": {"type": ["string", "null"]},
    },
    "required": ["verdict", "confidence", "why", "rank", "rewrite"],
    "additionalProperties": False,
}
PAGE_VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["HIT", "STALE", "WRONG_ENTITY", "DEAD"]},
        "confidence": {"type": "number"},
        "why": {"type": "string"},
        "rewrite": {"type": ["string", "null"]},
    },
    "required": ["verdict", "confidence", "why", "rewrite"],
    "additionalProperties": False,
}


class CheckerError(RuntimeError):
    """The checker model could not be reached or refused the request."""


class CheckedVerdict(CheckerVerdict):
    """A contract verdict plus the checker call's usage, kept out of serialization."""

    cost_usd: float = Field(default=0, ge=0, exclude=True)
    latency_ms: float = Field(default=0, ge=0, exclude=True)
    tokens_in: int = Field(default=0, ge=0, exclude=True)
    tokens_out: int = Field(default=0, ge=0, exclude=True)
    model: str = Field(default="", exclude=True)
    cached: bool = Field(default=False, exclude=True)


def load_prompt(name: str) -> str:
    return (PROMPTS / f"{name}.md").read_text()


SYSTEM_PROMPT = load_prompt("checker")
PAGE_PROMPT = load_prompt("checker_page")


# Rendering

def _iso(today: date | str | None) -> str:
    if today is None:
        return date.today().isoformat()
    return today.isoformat() if isinstance(today, date) else str(today)


def _one_line(text: str | None, limit: int) -> str:
    return re.sub(r"\s+", " ", text or "").strip()[:limit]


def as_results(results: Sequence[Result | dict[str, Any]]) -> list[Result]:
    """Accept Result models or plain dicts; keep the top TOP_K in order."""
    return [r if isinstance(r, Result) else Result.model_validate(r) for r in list(results)[:TOP_K]]


def render_results(results: Sequence[Result]) -> str:
    """Number results by position, 1..n. evidence_rank refers to these numbers."""
    blocks = []
    for i, r in enumerate(results, 1):
        blocks.append(
            f"[{i}] {_one_line(r.title, 200)}\n"
            f"    url: {r.url}\n"
            f"    published: {r.published or 'unknown'}\n"
            f"    snippet: {_one_line(r.snippet, SNIPPET_CHARS) or '(empty)'}"
        )
    return "\n".join(blocks)


def render_user(query: str, goal: str, results: Sequence[Result], today: date | str | None) -> str:
    return (f"TODAY: {_iso(today)}\nQUERY: {query}\nGOAL: {goal}\n\n"
            f"RESULTS:\n{render_results(results)}")


def render_page_user(query: str, goal: str, url: str, text: str, today: date | str | None) -> str:
    return (f"TODAY: {_iso(today)}\nQUERY: {query}\nGOAL: {goal}\n\n"
            f"PAGE URL: {url}\nPAGE TEXT:\n{text[:PAGE_CHARS]}")


# Parsing and validation

def extract_json(text: str) -> dict[str, Any]:
    """Parse the first JSON object in a model reply, tolerating code fences and prose."""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            raise ValueError(f"no JSON object in checker reply: {text[:200]!r}") from None
        obj = json.loads(match.group(0))
    if not isinstance(obj, dict):
        raise ValueError("checker reply is not a JSON object")
    return obj


def normalize_verdict(value: Any) -> str:
    """Accept the five verdict names in any case, with spaces or hyphens for underscores."""
    if not isinstance(value, str):
        raise ValueError(f"verdict must be a string, got {value!r}")
    key = re.sub(r"[\s\-]+", "_", value.strip().upper())
    if key not in VERDICTS:
        raise ValueError(f"unknown verdict {value!r}")
    return key


def _confidence(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
        raise ValueError(f"confidence must be a number from 0 to 1, got {value!r}")
    return float(value)


def _why(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError(f"why must be a string, got {value!r}")
    return _one_line(value, WHY_CHARS)


def _rank(value: Any, n: int) -> int | None:
    try:
        r = int(value)
    except (TypeError, ValueError):
        return None
    return r if 1 <= r <= n else None


def _text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = _one_line(value, limit)
    return value or None


def parse_verdict(text: str, results: Sequence[Result], query: str) -> CheckerVerdict:
    """Validate a result-list verdict and enforce the rules that keep HIT trustworthy."""
    raw = extract_json(text)
    verdict = normalize_verdict(raw.get("verdict"))
    confidence = _confidence(raw.get("confidence"))
    why = _why(raw.get("why"))
    rank = _rank(raw.get("rank"), len(results))
    rewrite = _text(raw.get("rewrite"), REWRITE_CHARS) or query
    page = results[rank - 1].url if rank is not None else None

    if verdict == "HIT":
        if page is not None and confidence >= config.CHECKER_HIT_MIN_CONFIDENCE:
            return CheckerVerdict(verdict="HIT", confidence=confidence, why=why, evidence_rank=rank,
                                  best_url=page, rewrite=None)
        # An unsupported or unsure HIT: read the page it pointed at rather than trust it.
        reason = "no valid result number" if page is None else f"confidence {confidence:.2f}"
        why = _one_line(f"Unsure hit ({reason}): {why}", WHY_CHARS)
        if page is None:
            return CheckerVerdict(verdict="DEAD", confidence=confidence, why=why, rewrite=rewrite)
        return CheckerVerdict(verdict="SHALLOW", confidence=confidence, why=why, best_url=page,
                              rewrite=rewrite)

    if verdict == "SHALLOW":
        if page is None:
            why = _one_line(f"Right page not identified: {why}", WHY_CHARS)
            return CheckerVerdict(verdict="DEAD", confidence=confidence, why=why, rewrite=rewrite)
        return CheckerVerdict(verdict="SHALLOW", confidence=confidence, why=why, best_url=page,
                              rewrite=rewrite)

    return CheckerVerdict(verdict=verdict, confidence=confidence, why=why, rewrite=rewrite)


def parse_page_verdict(text: str, url: str, query: str) -> CheckerVerdict:
    """Validate a page verdict. SHALLOW has no meaning after a read, so it becomes DEAD."""
    raw = extract_json(text)
    verdict = normalize_verdict(raw.get("verdict"))
    confidence = _confidence(raw.get("confidence"))
    why = _why(raw.get("why"))
    rewrite = _text(raw.get("rewrite"), REWRITE_CHARS) or query
    if verdict == "HIT" and confidence >= config.CHECKER_HIT_MIN_CONFIDENCE:
        return CheckerVerdict(verdict="HIT", confidence=confidence, why=why, best_url=url, rewrite=None)
    if verdict in ("HIT", "SHALLOW"):
        why = _one_line(f"Page does not clearly state it: {why}", WHY_CHARS)
        verdict = "DEAD"
    return CheckerVerdict(verdict=verdict, confidence=confidence, why=why, rewrite=rewrite)


# Model calls

async def _call_model(system: str, user: str, schema: dict[str, Any]) -> LLMResponse:
    """One checker call through so.llm, the only path to a model."""
    from so import llm

    return await llm.complete("checker", system, [{"role": "user", "content": user}], json_schema=schema)


def _add_usage(total: LLMResponse, resp: LLMResponse) -> LLMResponse:
    return total.model_copy(update={
        "cost_usd": total.cost_usd + resp.cost_usd, "latency_ms": total.latency_ms + resp.latency_ms,
        "tokens_in": total.tokens_in + resp.tokens_in, "tokens_out": total.tokens_out + resp.tokens_out,
        "model": resp.model or CHECKER_MODEL, "cached": total.cached and resp.cached})


def _with_usage(verdict: CheckerVerdict, usage: LLMResponse | None = None) -> CheckedVerdict:
    extra: dict[str, Any] = {}
    if usage is not None and usage.model:
        extra = {"cost_usd": usage.cost_usd, "latency_ms": usage.latency_ms, "tokens_in": usage.tokens_in,
                 "tokens_out": usage.tokens_out, "model": usage.model, "cached": usage.cached}
    return CheckedVerdict(**verdict.model_dump(), **extra)


def _log_usage(ctx: RunContext | None, usage: LLMResponse) -> None:
    """Emit the checker's `llm` event so its cost lands in the run totals."""
    if ctx is None or ctx.log is None or not usage.model:
        return
    ctx.log.emit("llm", role="checker", model=usage.model, tokens_in=usage.tokens_in,
                 tokens_out=usage.tokens_out, cost_usd=usage.cost_usd, latency_ms=usage.latency_ms)


async def _run(system: str, schema: dict[str, Any], user: str, parse: Callable[[str], CheckerVerdict],
               query: str, ctx: RunContext | None) -> CheckedVerdict:
    """Call the model and parse its reply, retrying once if the reply is malformed or truncated.

    Usage from every attempt is summed onto the verdict and logged. Two bad
    replies return a DEAD verdict with why=MALFORMED_WHY or TRUNCATED_WHY. A
    failed call raises CheckerError; BudgetExceeded propagates unchanged.
    """
    usage = LLMResponse(role="checker", cached=True)
    failure, detail, reply = MALFORMED_WHY, "", ""
    for attempt in range(2):
        try:
            resp = await _call_model(system, user if attempt == 0 else user + RETRY_NOTE, schema)
        except BudgetExceeded:
            _log_usage(ctx, usage)
            raise
        except Exception as exc:
            _log_usage(ctx, usage)
            raise CheckerError(f"checker model call failed: {type(exc).__name__}: {exc}") from exc
        usage = _add_usage(usage, resp)
        reply = resp.text
        if resp.stop_reason in TRUNCATED_STOPS:
            failure, detail = TRUNCATED_WHY, f"stopped at {resp.tokens_out} output tokens ({resp.stop_reason})"
            continue
        try:
            verdict = parse(reply)
        except ValueError as exc:
            failure, detail = MALFORMED_WHY, str(exc)
            continue
        _log_usage(ctx, usage)
        return _with_usage(verdict, usage)
    logger.warning("%s after a retry: %s; last reply: %r", failure, detail, reply[:300])
    _log_usage(ctx, usage)
    return _with_usage(CheckerVerdict(verdict="DEAD", confidence=0.0, why=failure, rewrite=query), usage)


async def check(query: str, goal: str, results: Sequence[Result | dict[str, Any]],
                today: date | str | None = None, *, ctx: RunContext | None = None) -> CheckedVerdict:
    """Label one search's top 5 results HIT, STALE, WRONG_ENTITY, SHALLOW or DEAD.

    `evidence_rank` is the 1-based position in `results` of the result that states
    the answer. On HIT, `best_url` is that result's URL so callers can cite it.
    """
    top = as_results(results)
    if not top:
        return _with_usage(CheckerVerdict(verdict="DEAD", confidence=1.0, why="The search returned no results.",
                                          rewrite=query))
    user = render_user(query, goal, top, today)
    return await _run(SYSTEM_PROMPT, VERDICT_SCHEMA, user, lambda reply: parse_verdict(reply, top, query),
                      query, ctx)


async def check_page(query: str, goal: str, page_text: str | PageText, today: date | str | None = None,
                     *, url: str | None = None, ctx: RunContext | None = None) -> CheckedVerdict:
    """Re-check after reading a page on the SHALLOW path. Never returns SHALLOW.

    `page_text` may be the page's text or a `PageText`; on HIT, `best_url` is the page URL.
    """
    if isinstance(page_text, PageText):
        url = url or page_text.url
        text = page_text.text if page_text.ok else ""
    else:
        text = page_text or ""
    url = url or ""
    if not text.strip():
        return _with_usage(CheckerVerdict(verdict="DEAD", confidence=1.0, why="The page could not be read.",
                                          rewrite=query))
    user = render_page_user(query, goal, url, text, today)
    return await _run(PAGE_PROMPT, PAGE_VERDICT_SCHEMA, user,
                      lambda reply: parse_page_verdict(reply, url, query), query, ctx)
