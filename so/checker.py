"""Search result checker: decides whether a result list already answers the goal.

`check` labels the top 5 results of one search with a verdict from
SHARED_CONTEXT section 6.7. `check_page` re-checks a page read on the SHALLOW
path. Each makes one call on CHECKER_MODEL and never sees gold answers.

The model's JSON is validated, then hardened so a wrong HIT is unlikely:
a HIT must point at a real result and clear HIT_MIN_CONFIDENCE, otherwise it
becomes SHALLOW on that result so the page gets read instead of trusted.
A failed model call degrades to DEAD (one extra search), never to HIT.

Both functions return a `CheckedVerdict`: a `CheckerVerdict` that also carries
the checker's own cost, tokens and latency. Those extra fields are excluded
from `model_dump()`, so the serialized verdict matches the contract exactly.
Pass `ctx` to have the checker log its own `llm` event (role `checker`).
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import Field

from so.config import CHECKER_MODEL, BudgetExceeded
from so.contracts import CheckerVerdict, LLMResponse, PageText, Result, RunContext

PROMPTS = Path(__file__).resolve().parent / "prompts"
TOP_K = 5
SNIPPET_CHARS = 500
PAGE_CHARS = 6000
WHY_CHARS = 300
MAX_OUTPUT_TOKENS = 400
# A HIT below this confidence is treated as "probably the right page" and read.
HIT_MIN_CONFIDENCE = 0.6

VERDICTS = ("HIT", "STALE", "WRONG_ENTITY", "SHALLOW", "DEAD")
_ALIASES = {"WRONG": "WRONG_ENTITY", "WRONGENTITY": "WRONG_ENTITY", "ENTITY": "WRONG_ENTITY",
            "MISS": "DEAD", "NONE": "DEAD", "NOT_FOUND": "DEAD", "IRRELEVANT": "DEAD",
            "OUTDATED": "STALE", "OLD": "STALE", "PARTIAL": "SHALLOW"}

VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": list(VERDICTS)},
        "confidence": {"type": "number"},
        "why": {"type": "string"},
        "evidence_rank": {"type": ["integer", "null"]},
        "best_url": {"type": ["string", "null"]},
        "rewrite": {"type": ["string", "null"]},
    },
    "required": ["verdict", "confidence", "why", "evidence_rank", "best_url", "rewrite"],
    "additionalProperties": False,
}


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
    key = re.sub(r"[\s\-]+", "_", str(value or "").strip().upper())
    key = _ALIASES.get(key.replace("_", ""), _ALIASES.get(key, key))
    if key not in VERDICTS:
        raise ValueError(f"unknown verdict {value!r}")
    return key


def _confidence(value: Any) -> float:
    try:
        c = float(value)
    except (TypeError, ValueError):
        return 0.5
    if 1 < c <= 100:
        c /= 100
    return min(max(c, 0.0), 1.0)


def _rank(value: Any, n: int) -> int | None:
    try:
        r = int(value)
    except (TypeError, ValueError):
        return None
    return r if 1 <= r <= n else None


def _url_key(url: str) -> str:
    parts = urlsplit(url.strip())
    host = parts.netloc.lower().removeprefix("www.")
    return f"{host}{parts.path.rstrip('/')}?{parts.query}"


def match_url(candidate: Any, results: Sequence[Result]) -> str | None:
    """Return the result URL the model meant, or None if it is not in the results."""
    if not isinstance(candidate, str) or not candidate.strip():
        return None
    key = _url_key(candidate)
    for r in results:
        if r.url == candidate.strip() or _url_key(r.url) == key:
            return r.url
    return None


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
    why = _text(raw.get("why"), WHY_CHARS) or ""
    rank = _rank(raw.get("evidence_rank"), len(results))
    best_url = match_url(raw.get("best_url"), results)
    rewrite = _text(raw.get("rewrite"), 300) or query

    if verdict == "HIT":
        if rank is not None and confidence >= HIT_MIN_CONFIDENCE:
            return CheckerVerdict(verdict="HIT", confidence=confidence, why=why, evidence_rank=rank,
                                  best_url=results[rank - 1].url, rewrite=None)
        # An unsupported or unsure HIT: read the page it pointed at rather than trust it.
        page = results[rank - 1].url if rank is not None else best_url
        reason = "no evidence rank" if rank is None else f"confidence {confidence:.2f}"
        why = _one_line(f"Unsure hit ({reason}): {why}", WHY_CHARS)
        if page is None:
            return CheckerVerdict(verdict="DEAD", confidence=confidence, why=why, rewrite=rewrite)
        return CheckerVerdict(verdict="SHALLOW", confidence=confidence, why=why, best_url=page,
                              rewrite=rewrite)

    if verdict == "SHALLOW":
        page = best_url or (results[rank - 1].url if rank is not None else None)
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
    why = _text(raw.get("why"), WHY_CHARS) or ""
    rewrite = _text(raw.get("rewrite"), 300) or query
    if verdict == "HIT" and confidence >= HIT_MIN_CONFIDENCE:
        return CheckerVerdict(verdict="HIT", confidence=confidence, why=why, best_url=url, rewrite=None)
    if verdict in ("HIT", "SHALLOW"):
        why = _one_line(f"Page does not clearly state it: {why}", WHY_CHARS)
        verdict = "DEAD"
    return CheckerVerdict(verdict=verdict, confidence=confidence, why=why, rewrite=rewrite)


# Model calls

async def _call_model(system: str, user: str) -> LLMResponse:
    """One checker call through so.llm, the only path to a model."""
    from so import llm

    return await llm.complete("checker", system, [{"role": "user", "content": user}],
                              json_schema=VERDICT_SCHEMA)


def _with_usage(verdict: CheckerVerdict, resp: LLMResponse | None) -> CheckedVerdict:
    usage: dict[str, Any] = {}
    if resp is not None:
        usage = {"cost_usd": resp.cost_usd, "latency_ms": resp.latency_ms, "tokens_in": resp.tokens_in,
                 "tokens_out": resp.tokens_out, "model": resp.model or CHECKER_MODEL,
                 "cached": resp.cached}
    return CheckedVerdict(**verdict.model_dump(), **usage)


def _log_llm(ctx: RunContext | None, out: CheckedVerdict) -> None:
    if ctx is None or ctx.log is None or not out.model:
        return
    ctx.log.emit("llm", role="checker", model=out.model, tokens_in=out.tokens_in,
                 tokens_out=out.tokens_out, cost_usd=out.cost_usd, latency_ms=out.latency_ms)


def _failed(query: str, err: Exception) -> CheckerVerdict:
    why = _one_line(f"Checker error, treated as a miss: {type(err).__name__}", WHY_CHARS)
    return CheckerVerdict(verdict="DEAD", confidence=0.0, why=why, rewrite=query)


async def _run(system: str, user: str, parse: Callable[[str], CheckerVerdict],
               query: str) -> CheckedVerdict:
    """Call the model, parse, retry once on a malformed reply, and degrade to DEAD on failure."""
    resp: LLMResponse | None = None
    spent = LLMResponse()
    err: Exception | None = None
    for _ in range(2):
        try:
            resp = await _call_model(system, user)
        except BudgetExceeded:
            raise
        except Exception as exc:  # noqa: BLE001 - a checker outage must not stop the search
            err = exc
            break
        spent = spent.model_copy(update={
            "cost_usd": spent.cost_usd + resp.cost_usd, "latency_ms": spent.latency_ms + resp.latency_ms,
            "tokens_in": spent.tokens_in + resp.tokens_in, "tokens_out": spent.tokens_out + resp.tokens_out,
            "model": resp.model or CHECKER_MODEL, "cached": resp.cached})
        try:
            return _with_usage(parse(resp.text), spent)
        except (ValueError, json.JSONDecodeError) as exc:
            err = exc
    return _with_usage(_failed(query, err or RuntimeError("no reply")), spent if resp else None)


async def check(query: str, goal: str, results: Sequence[Result | dict[str, Any]],
                today: date | str | None = None, *, ctx: RunContext | None = None) -> CheckedVerdict:
    """Label one search's top 5 results HIT, STALE, WRONG_ENTITY, SHALLOW or DEAD.

    `evidence_rank` is the 1-based position in `results` of the result that states
    the answer. On HIT, `best_url` is that result's URL so callers can cite it.
    """
    top = as_results(results)
    if not top:
        return _with_usage(CheckerVerdict(verdict="DEAD", confidence=1.0, why="The search returned no results.",
                                          rewrite=query), None)
    user = render_user(query, goal, top, today)
    out = await _run(SYSTEM_PROMPT, user, lambda text: parse_verdict(text, top, query), query)
    _log_llm(ctx, out)
    return out


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
                                          rewrite=query), None)
    user = render_page_user(query, goal, url, text, today)
    out = await _run(PAGE_PROMPT, user, lambda reply: parse_page_verdict(reply, url, query), query)
    _log_llm(ctx, out)
    return out
