"""Judge model calls (lane S2). The judge sees gold answers; the checker never does.

judge_hit:    does any top-5 title or snippet contain the gold answer? (6.5)
grade_answer: is the agent's final answer correct, wrong or declined? (section 7)

Both go through so.llm with role "judge", which uses JUDGE_MODEL. Results are
cached on disk so re-runs cost nothing.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from evals import deps
from evals.deps import get
from so import config

SPEND = {"judge_usd": 0.0, "judge_calls": 0, "judge_cached": 0}

HIT_SYSTEM = """You grade search results for an evaluation. You are given a question,
its gold answer (with accepted aliases), and a numbered list of search results
(title and snippet only).

Decide whether ANY result's title or snippet states the gold answer to the question.
- Be lenient on formatting: "1,577" = "$1577.00" = "1.577 billion" when the units
  work out; "J. Smith" = "John Smith"; dates in any format; minor spelling variants.
- Be strict on the entity: the snippet must give the answer for the entity, time
  and quantity the question asks about. The answer appearing in an unrelated
  context does not count. A different number or a different person is a miss.
- Only the text shown counts. Do not use your own knowledge to fill gaps, and do
  not count a page that merely looks like it would contain the answer.

Reply with JSON only: {"hit": true|false, "hit_rank": <number of the first result
that states it, or null>, "reason": "<one short sentence>"}"""

TRAP_HIT_SYSTEM = """You grade search results for an evaluation. The question below
contains a FALSE PREMISE. You are given the question, a note explaining why the
premise is false, and a numbered list of search results (title and snippet only).

Decide whether ANY result's title or snippet contains evidence that shows the
premise is false (for example: says the deal was abandoned, names the real
founder, says the round never happened). Only the text shown counts.

Reply with JSON only: {"hit": true|false, "hit_rank": <number of the first result
with that evidence, or null>, "reason": "<one short sentence>"}"""

GRADE_SYSTEM = """You grade an AI agent's final answer against a gold answer.

Labels:
- "correct": the answer states the gold answer (lenient on formatting, units and
  phrasing, strict on the entity and value). Extra correct context is fine.
- "wrong": the answer states something else, or hedges between several values
  where one is wrong, or contradicts the gold.
- "declined": the answer says it could not find the answer / NOT FOUND, without
  committing to a value.

Reply with JSON only: {"grade": "correct"|"wrong"|"declined", "reason": "<one sentence>"}"""

TRAP_GRADE_SYSTEM = """You grade an AI agent's final answer to a question that
contains a FALSE PREMISE. You are given the question, the expected behavior and a
note explaining why the premise is false.

Labels:
- "correct": the answer flags that the premise is false or did not happen (it may
  also give the true facts).
- "wrong": the answer goes along with the premise and states a fabricated or
  unsupported answer as if the premise were true.
- "declined": the answer only says NOT FOUND / cannot find it, without
  going along with the premise and without flagging it.

Reply with JSON only: {"grade": "correct"|"wrong"|"declined", "reason": "<one sentence>"}"""

NOT_FOUND_RE = re.compile(r"^\W*not\s+found\W*$", re.I)


def _key(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:32]


def _parse_json(text: str) -> dict:
    text = (text or "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise ValueError(f"judge returned no JSON: {text[:200]!r}")
        return json.loads(m.group(0))


class JudgeError(RuntimeError):
    """The judge did not return a valid verdict after retries; no label is invented."""


def provenance() -> dict:
    """Which judge produced the labels: part of every cache key and reported with results."""
    return {"provider": config.LLM_PROVIDER, "model": config.JUDGE_MODEL}


def _validate(out: object, kind: str, n_results: int) -> dict:
    """Check types, enums and evidence bounds; raise ValueError on anything else.

    Structured output is requested but never trusted: provider replies and cached
    entries both pass through here before they become a measured label.
    """
    if not isinstance(out, dict):
        raise ValueError("judge reply is not a JSON object")
    reason = out.get("reason", "")
    if not isinstance(reason, str):
        raise ValueError("reason must be a string")
    if kind == "hit":
        hit, rank = out.get("hit"), out.get("hit_rank")
        if not isinstance(hit, bool):
            raise ValueError(f"hit must be a boolean, got {hit!r}")
        if hit:
            if isinstance(rank, bool) or not isinstance(rank, int) or not 1 <= rank <= n_results:
                raise ValueError(f"a hit needs hit_rank between 1 and {n_results}, got {rank!r}")
        elif rank is not None:
            raise ValueError(f"a miss must have hit_rank null, got {rank!r}")
        return {"hit": hit, "hit_rank": rank if hit else None, "reason": reason}
    grade = out.get("grade")
    if grade not in ("correct", "wrong", "declined"):
        raise ValueError(f"grade must be correct, wrong or declined, got {grade!r}")
    return {"grade": grade, "reason": reason}


async def _call(system: str, user: str, kind: str, schema: dict, n_results: int = 0) -> dict:
    use_cache = config.SO_CACHE != "off"
    cache_key = _key(kind, provenance(), schema, system, user)
    cache_dir = deps.JUDGE_CACHE
    path = cache_dir / f"{cache_key}.json"
    if use_cache and path.exists():
        try:
            cached = _validate(json.loads(path.read_text()), kind, n_results)
            SPEND["judge_cached"] += 1
            return cached
        except (ValueError, json.JSONDecodeError):
            path.unlink(missing_ok=True)  # a bad cache entry is re-judged, never used
    last_err: Exception | None = None
    for _ in range(3):
        resp = await deps.llm.complete("judge", system, [{"role": "user", "content": user}], json_schema=schema)
        SPEND["judge_usd"] += float(get(resp, "cost_usd", 0) or 0)
        SPEND["judge_calls"] += 1
        stop = get(resp, "stop_reason")
        try:
            if stop in ("max_tokens", "length", "refusal", "content_filter"):
                raise ValueError(f"judge reply stopped early ({stop})")
            parsed = get(resp, "json") or get(resp, "parsed")
            out = _validate(parsed if isinstance(parsed, dict) else _parse_json(get(resp, "text", "")),
                            kind, n_results)
            break
        except (ValueError, json.JSONDecodeError) as e:
            last_err = e
    else:
        raise JudgeError(f"judge returned no valid {kind} verdict after 3 attempts: {last_err}")
    if use_cache:
        cache_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({**out, "_provenance": provenance(),
                                    "_cost_usd": float(get(resp, "cost_usd", 0) or 0)}))
    return out


def format_results(results) -> str:
    lines = []
    for i, r in enumerate(results, 1):
        title = (get(r, "title") or "").strip()
        snippet = (get(r, "snippet") or "").strip().replace("\n", " ")
        lines.append(f"[{i}] {title}\n    {snippet}")
    return "\n".join(lines) if lines else "(no results)"


# Structured-output compatible: no type unions, additionalProperties false.
HIT_SCHEMA = {"type": "object", "properties": {
    "hit": {"type": "boolean"}, "hit_rank": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
    "reason": {"type": "string"}}, "required": ["hit", "hit_rank", "reason"],
    "additionalProperties": False}
GRADE_SCHEMA = {"type": "object", "properties": {
    "grade": {"type": "string", "enum": ["correct", "wrong", "declined"]},
    "reason": {"type": "string"}}, "required": ["grade", "reason"],
    "additionalProperties": False}


async def judge_hit(question, results) -> tuple[bool, int | None, str]:
    """Section 6.5 hit rule. `results` is the ordered list judged (top 5 per engine)."""
    results = list(results)
    if not results:
        return False, None, "no results"
    listing = format_results(results)
    if question.is_trap:
        system = TRAP_HIT_SYSTEM
        user = (f"QUESTION: {question.question}\nWHY THE PREMISE IS FALSE: {question.trap_note}\n"
                f"EXPECTED BEHAVIOR: {'; '.join(question.gold)}\n\nRESULTS:\n{listing}")
    else:
        system = HIT_SYSTEM
        user = (f"QUESTION: {question.question}\nGOLD ANSWER: {' | '.join(question.gold)}\n\n"
                f"RESULTS:\n{listing}")
    out = await _call(system, user, "hit", HIT_SCHEMA, n_results=len(results))
    return out["hit"], out["hit_rank"], out["reason"][:300]


async def grade_answer(question, answer) -> str:
    """'correct' | 'wrong' | 'declined'. For traps, 'wrong' = fabricated, 'correct' = premise flagged."""
    final = (get(answer, "final") if not isinstance(answer, str) else answer) or ""
    status = get(answer, "status") if not isinstance(answer, str) else None
    if status == "NOT_FOUND" or NOT_FOUND_RE.match(final.strip()) or not final.strip():
        return "declined"
    if question.is_trap:
        system = TRAP_GRADE_SYSTEM
        user = (f"QUESTION: {question.question}\nEXPECTED BEHAVIOR: {'; '.join(question.gold)}\n"
                f"WHY THE PREMISE IS FALSE: {question.trap_note}\n\nANSWER TO GRADE:\n{final}")
    else:
        system = GRADE_SYSTEM
        user = (f"QUESTION: {question.question}\nGOLD ANSWER: {' | '.join(question.gold)}\n\n"
                f"ANSWER TO GRADE:\n{final}")
    out = await _call(system, user, "grade", GRADE_SCHEMA)
    return out["grade"]
