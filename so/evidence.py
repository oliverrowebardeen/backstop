"""Build the verified evidence object attached to every Second Opinion HIT.

Everything here works on data the search already holds: the checker's HIT
explanation, the evidence text (a result snippet, Extract chunks or a fetched
page) and each engine's top results. No call in this module costs money.

The checker never sees gold answers, so the answer string comes from the
checker's own `why` on a HIT, which quotes the words that state the answer.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from so.contracts import Result

SNIPPET_CHARS = 500
"""Longest evidence snippet, matching one Tavily Extract chunk."""

_QUOTED = re.compile(r"\"([^\"]+)\"|“([^”]+)”|‘([^’]+)’|(?<!\w)'([^']+)'(?!\w)")
_RESULT_REF = re.compile(r"\b(?:results?|rank|pages?)\s*#?\d+\b", re.IGNORECASE)
_SCALES = {
    "thousand": 1e3, "k": 1e3, "million": 1e6, "mn": 1e6, "m": 1e6,
    "billion": 1e9, "bn": 1e9, "b": 1e9, "trillion": 1e12, "tn": 1e12, "t": 1e12,
}
_NUMBER = re.compile(
    r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?(?:\s*(thousand|million|billion|trillion|mn|bn|tn|k|m|b|t)\b)?",
    re.IGNORECASE,
)
_MIN_PHRASE_CHARS = 3


@dataclass(frozen=True)
class AnswerTerms:
    """What counts as the answer when matching text: quoted phrases and numbers."""

    phrases: tuple[str, ...] = ()
    numbers: tuple[float, ...] = field(default_factory=tuple)

    def __bool__(self) -> bool:
        return bool(self.phrases or self.numbers)


def _normalize(text: str) -> str:
    """Casefold, drop punctuation other than digits' separators, collapse spaces."""
    text = re.sub(r"[^\w\s.,%$]", " ", text.casefold())
    return re.sub(r"\s+", " ", text).strip()


def _numbers(text: str) -> list[tuple[float, int, int]]:
    """Numbers in `text` as (value, start, end), with scale words applied.

    Bare integers below 10 are skipped: they match almost any page.
    """
    found = []
    for match in _NUMBER.finditer(text):
        whole, fraction, scale = match.group(1), match.group(2) or "", match.group(3)
        value = float(whole.replace(",", "") + fraction)
        if scale:
            value *= _SCALES[scale.lower()]
        elif not fraction and value < 10:
            continue
        found.append((value, match.start(), match.end()))
    return found


def answer_terms(why: str, *, exclude: str = "") -> AnswerTerms:
    """Answer phrases and numbers from a checker HIT explanation.

    Quoted text is the answer when present; otherwise only numbers are taken
    from the whole explanation, since its prose is not the answer. Numbers that
    also appear in `exclude` (the query and goal) are dropped, so a year the
    agent asked about does not count as agreement.
    """
    quoted = [next(group for group in match.groups() if group) for match in _QUOTED.finditer(why)]
    phrases = tuple(dict.fromkeys(
        phrase for phrase in (_normalize(item) for item in quoted)
        if len(phrase) >= _MIN_PHRASE_CHARS and re.search(r"\w", phrase)
    ))
    source = " ".join(quoted) if quoted else _RESULT_REF.sub(" ", why)
    excluded = [value for value, _, _ in _numbers(exclude)]
    numbers = tuple(dict.fromkeys(
        value for value, _, _ in _numbers(source)
        if not any(math.isclose(value, other, rel_tol=1e-9) for other in excluded)
    ))
    return AnswerTerms(phrases=phrases, numbers=numbers)


def _phrase_pattern(phrase: str) -> re.Pattern[str]:
    """Match a normalized phrase's words in order, across any punctuation or spacing."""
    words = re.findall(r"\w+", phrase)
    return re.compile(r"(?<!\w)" + r"\W+".join(map(re.escape, words)) + r"(?!\w)", re.IGNORECASE)


def find_answer(text: str, terms: AnswerTerms) -> tuple[int, int] | None:
    """Span in `text` of the first answer number or phrase, or None if absent."""
    spans = [
        (start, end) for value, start, end in _numbers(text)
        if any(math.isclose(value, wanted, rel_tol=1e-9) for wanted in terms.numbers)
    ]
    for phrase in terms.phrases:
        match = _phrase_pattern(phrase).search(text)
        if match:
            spans.append(match.span())
    return min(spans) if spans else None


def _window(text: str, span: tuple[int, int] | None, limit: int) -> str:
    """At most `limit` characters of `text`, centred on `span` when given."""
    text = text.strip()
    if len(text) <= limit:
        return text
    if span is None:
        return text[:limit].rstrip()
    middle = (span[0] + span[1]) // 2
    start = max(0, min(middle - limit // 2, len(text) - limit))
    return text[start:start + limit].strip()


def best_snippet(candidates: Sequence[str], terms: AnswerTerms, limit: int = SNIPPET_CHARS) -> str:
    """The first candidate that states the answer (else the first one), cut to `limit` around it."""
    usable = [item for item in candidates if item and item.strip()]
    if not usable:
        return ""
    for item in usable:
        span = find_answer(item, terms) if terms else None
        if span is not None:
            return _window(item, span, limit)
    return _window(usable[0], None, limit)


def engines_agreeing(per_engine: Mapping[str, Sequence[Result]], terms: AnswerTerms) -> list[str]:
    """Engines whose already-fetched top results state the answer in a title or snippet."""
    if not terms:
        return []
    return sorted(
        engine for engine, results in per_engine.items()
        if any(find_answer(f"{result.title} {result.snippet}", terms) is not None for result in results)
    )


def build_evidence(*, url: str, why: str, candidates: Sequence[str], per_engine: Mapping[str, Sequence[Result]],
                   exclude: str = "", rank_reason: str | None = None, source: str = "search",
                   chunks: int | None = None) -> dict[str, Any]:
    """The evidence object recorded on a HIT's search_done event.

    `candidates` are the texts that support the HIT, best first: Extract chunks,
    a fetched page or the evidence result's snippet. `source` names where they
    came from (search, tavily_extract or telem_fetch).
    """
    terms = answer_terms(why, exclude=exclude)
    agreeing = engines_agreeing(per_engine, terms)
    evidence: dict[str, Any] = {
        "verified": True,
        "snippet": best_snippet(candidates, terms),
        "url": url,
        "engines_agreeing": len(agreeing),
        "agreeing_engines": agreeing,
        "source": source,
    }
    if rank_reason:
        evidence["rank_reason"] = rank_reason
    if chunks is not None:
        evidence["chunks"] = chunks
    return evidence
