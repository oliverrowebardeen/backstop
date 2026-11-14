"""Benchmark contamination filter (lane S2).

SimpleQA and FinanceBench are public, so web search returns pages that copy the
benchmark itself (dataset viewers, published answer sheets). A hit on such a
page measures memorised benchmark text, not search quality. Every judged result
list passes through `filter_results` first.

A result is a benchmark copy when either:
  * its URL matches a known benchmark-dataset pattern, or
  * its title + snippet reproduce at least `threshold` of the question's word
    4-grams (organic pages rarely repeat a question's exact phrasing; copies of
    the dataset do).

Both are configurable through the environment:
  SO_CONTAM_THRESHOLD   float in (0, 1], default 0.5
  SO_CONTAM_URL_PATTERNS  extra regex alternatives, '|'-separated
  SO_CONTAM_OFF=1       disable the filter (for measuring raw numbers)
"""
from __future__ import annotations

import os
import re
from collections.abc import Iterable

_DEFAULT_URL_PATTERNS = (
    r"huggingface\.co/datasets/",
    r"kaggle\.com/datasets/",
    r"financebench",
    r"simple[-_]?qa",
    r"simple[-_]evals",
)
NGRAM = 4


def _url_regex() -> re.Pattern:
    extra = [p for p in os.getenv("SO_CONTAM_URL_PATTERNS", "").split("|") if p.strip()]
    return re.compile("|".join((*_DEFAULT_URL_PATTERNS, *extra)), re.I)


def threshold() -> float:
    value = float(os.getenv("SO_CONTAM_THRESHOLD", "0.5"))
    if not 0 < value <= 1:
        raise ValueError("SO_CONTAM_THRESHOLD must be in (0, 1]")
    return value


def enabled() -> bool:
    return os.getenv("SO_CONTAM_OFF") != "1"


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", (text or "").lower())


def _grams(tokens: list[str]) -> set[tuple[str, ...]]:
    return {tuple(tokens[i:i + NGRAM]) for i in range(len(tokens) - NGRAM + 1)}


def question_overlap(question: str, title: str, snippet: str) -> float:
    q = _grams(_tokens(question))
    if not q:
        return 0.0
    return len(q & _grams(_tokens(f"{title} {snippet}"))) / len(q)


def is_benchmark_copy(question: str, result, *, url_re: re.Pattern | None = None,
                      min_overlap: float | None = None) -> bool:
    get = (lambda k: result.get(k)) if isinstance(result, dict) else (lambda k: getattr(result, k, None))
    url_re = url_re or _url_regex()
    if url_re.search(get("url") or ""):
        return True
    limit = threshold() if min_overlap is None else min_overlap
    return question_overlap(question, get("title") or "", get("snippet") or "") >= limit


def filter_results(question: str, results: Iterable) -> tuple[list, int]:
    """Return (kept results in original order, number dropped as benchmark copies)."""
    results = list(results)
    if not enabled():
        return results, 0
    url_re, limit = _url_regex(), threshold()
    kept = [r for r in results if not is_benchmark_copy(question, r, url_re=url_re, min_overlap=limit)]
    return kept, len(results) - len(kept)
