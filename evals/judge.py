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

from evals.deps import ROOT, FAKE, get, llm

CACHE = ROOT / "cache" / ("judge_fake" if FAKE else "judge")
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


async def _call(system: str, user: str, cache_key: str, schema: dict) -> dict:
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / f"{cache_key}.json"
    if path.exists():
        SPEND["judge_cached"] += 1
        return json.loads(path.read_text())
    last_err = None
    for _ in range(3):
        resp = await llm.complete("judge", system, [{"role": "user", "content": user}], json_schema=schema)
        SPEND["judge_usd"] += float(get(resp, "cost_usd", 0) or 0)
        SPEND["judge_calls"] += 1
        parsed = get(resp, "json") or get(resp, "parsed")
        try:
            out = parsed if isinstance(parsed, dict) else _parse_json(get(resp, "text", ""))
            break
        except (ValueError, json.JSONDecodeError) as e:
            last_err = e
    else:
        raise RuntimeError(f"judge failed 3 times: {last_err}")
    out["_cost_usd"] = float(get(resp, "cost_usd", 0) or 0)
    path.write_text(json.dumps(out))
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
    out = await _call(system, user, _key("hit", system, user), HIT_SCHEMA)
    hit = bool(out.get("hit"))
    rank = out.get("hit_rank")
    rank = int(rank) if hit and isinstance(rank, (int, float)) and 1 <= int(rank) <= len(results) else None
    return hit, rank, str(out.get("reason", ""))[:300]


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
    out = await _call(system, user, _key("grade", system, user), GRADE_SCHEMA)
    grade = str(out.get("grade", "")).lower()
    return grade if grade in ("correct", "wrong", "declined") else "wrong"
